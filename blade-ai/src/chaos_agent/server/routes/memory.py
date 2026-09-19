"""``/api/v1/memory`` — TS TUI session-memory inspection + cleanup.

Surface mirrors Python's ``/memory`` slash family:
  - ``GET    /api/v1/memory/resumable``        → sessions with an events
    jsonl on disk (the ``/resume`` picker's data source).
  - ``GET    /api/v1/memory/{tui_session_id}`` → session metadata + recent
    task ids + stats + resolved memory_dir.
  - ``GET    /api/v1/memory/{tui_session_id}/events`` → the session's full
    StreamEvent audit trail (jsonl), for ``/resume <sid>`` visual rebuild.
  - ``DELETE /api/v1/memory/{tui_session_id}`` → drop the persisted TUI
    session file. Does NOT clear LangGraph checkpoint threads — those
    are tied to specific task ids and the user reaches them via
    ``/recover`` instead. Conservative scope so an accidental
    ``/memory clear`` can't blow away inject state another flow is
    still using.

Why a TUI-session id (not just task id):
  ``TuiSessionStore`` is the bridge between a user-visible TUI session
  (``state.tui_session_id``) and the N tasks they ran inside it. The
  Python TUI's ``/memory`` family operates on this layer — show all
  tasks under the current session, clear the file when the user wants
  a fresh slate.
"""

from __future__ import annotations

import logging

from fastapi import Request

from chaos_agent.memory.tui_session_store import SESSION_ID_PATTERN
from chaos_agent.models.schemas import JSONEnvelope, ResponseCode
from chaos_agent.server.routes import memory_router

logger = logging.getLogger(__name__)


# Whitelist re-exported from the store (single source — the sessions
# resume route imports the same constant; see
# ``tui_session_store.SESSION_ID_PATTERN`` for the rationale).
#
# Without this gate ``tui_session_id="../../etc/passwd"`` would
# produce ``<session_dir>/../../etc/passwd.json`` which escapes
# the sessions directory. ``unlink`` on that path is a confirmed
# delete primitive against any user-readable file the server
# process can reach.
_SESSION_ID_PATTERN = SESSION_ID_PATTERN


def _validate_session_id(tui_session_id: str, req_id: str):
    """Return None when ``tui_session_id`` is safe; otherwise return a
    fail envelope the caller can surface directly. Single-shot helper
    so GET and DELETE keep the validation literal in one place."""
    if not _SESSION_ID_PATTERN.match(tui_session_id):
        return JSONEnvelope.fail(
            code=ResponseCode.INVALID_PARAMS,
            message=(
                f"invalid tui_session_id '{tui_session_id}' — must be 1–128 "
                "characters of [A-Za-z0-9_-]"
            ),
            request_id=req_id,
        )
    return None


# Cap on sessions returned by ``/resumable`` lives in
# ``TuiSessionStore.list_resumable_sessions`` (default 50 — beyond
# ~50 rows the terminal list stops being a picker and becomes a wall
# of text; disk-order + mtime sort means the cap only ever hides the
# *stalest* sessions). The first-input extraction helpers moved to
# ``tui_session_store`` with it.


@memory_router.get("/resumable")
async def list_resumable(req: Request):
    """Sessions that carry an events jsonl on disk.

    Registered BEFORE ``/{tui_session_id}`` so the literal path wins
    over the parameterised one (otherwise FastAPI would bind the
    literal string ``resumable`` as a session id).

    Row shape: see ``TuiSessionStore.list_resumable_sessions`` (single
    source — the CLI's ``blade-ai resume`` picker shares it).
    """
    from chaos_agent.memory.tui_session_store import (
        get_global_tui_session_store,
    )

    req_id = getattr(req.state, "request_id", "")
    store = get_global_tui_session_store()
    if store is None:
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message="TUI session store is not initialised",
            request_id=req_id,
        )
    try:
        rows = store.list_resumable_sessions()
    except OSError as e:
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message=f"failed to scan events dir: {e}",
            request_id=req_id,
        )
    return JSONEnvelope.ok(
        data={"sessions": rows, "total": len(rows)},
        request_id=req_id,
    )


@memory_router.get("/{tui_session_id}/events")
async def read_memory_events(tui_session_id: str, req: Request):
    """Full StreamEvent audit trail for a session (the ``/resume``
    visual-rebuild source). 404-shaped fail envelope when the events
    file doesn't exist — the client reports that as a hard error (no
    fallback chain by design)."""
    from chaos_agent.memory.tui_session_store import (
        get_global_tui_session_store,
    )

    req_id = getattr(req.state, "request_id", "")
    bad = _validate_session_id(tui_session_id, req_id)
    if bad is not None:
        return bad
    store = get_global_tui_session_store()
    if store is None:
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message="TUI session store is not initialised",
            request_id=req_id,
        )
    events = store.read_events(tui_session_id)
    if not events:
        # read_events returns [] for both "missing" and "empty" — treat
        # them the same: nothing to rebuild. The distinguishing detail
        # doesn't change the client's behaviour (hard error either way).
        return JSONEnvelope.fail(
            code=ResponseCode.TASK_NOT_FOUND,
            message=f"no events jsonl for session '{tui_session_id}'",
            request_id=req_id,
        )
    return JSONEnvelope.ok(
        data={"events": events, "total": len(events)},
        request_id=req_id,
    )


@memory_router.get("/{tui_session_id}")
async def read_memory(tui_session_id: str, req: Request):
    """Snapshot of the named TUI session.

    Returns ``status: fail`` with ``TASK_NOT_FOUND`` when the session
    file does not exist; the TS handler treats that the same as "no
    memory yet — start a turn". Mirrors Python's ``_cmd_memory_show``
    fields so the renderings stay isomorphic.
    """
    from chaos_agent.config.settings import settings as s
    from chaos_agent.memory.tui_session_store import (
        get_global_tui_session_store,
    )

    req_id = getattr(req.state, "request_id", "")
    bad = _validate_session_id(tui_session_id, req_id)
    if bad is not None:
        return bad
    store = get_global_tui_session_store()
    if store is None:
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message="TUI session store is not initialised",
            request_id=req_id,
        )
    data = store.read(tui_session_id) or {}
    if not data:
        return JSONEnvelope.fail(
            code=ResponseCode.TASK_NOT_FOUND,
            message=f"no TUI session record for '{tui_session_id}'",
            request_id=req_id,
        )
    # Pull just the recent task tail — Python caps at 3, we mirror so
    # the textual log line stays bounded for users who've run dozens
    # of tasks in the same session.
    task_ids = list(data.get("task_ids", []) or [])
    recent_tail = task_ids[-3:]
    return JSONEnvelope.ok(
        data={
            "tui_session_id": tui_session_id,
            "cluster_name": data.get("cluster_name") or "",
            "namespace": data.get("namespace") or "",
            "started_at": data.get("started_at") or "",
            "status": data.get("status") or "active",
            "task_ids_recent": recent_tail,
            "task_count_total": len(task_ids),
            "stats": dict(data.get("stats") or {}),
            "memory_dir": str(s.resolved_memory_dir),
        },
        request_id=req_id,
    )


@memory_router.delete("/{tui_session_id}")
async def clear_memory(tui_session_id: str, req: Request):
    """Delete the on-disk TUI session file.

    The graph-checkpoint thread for any task referenced by this session
    is intentionally NOT touched — recover flows resolve via task ids,
    and dropping the messages out from under them would corrupt
    in-flight execute / verify state. This op is the equivalent of
    ``rm ~/.blade-ai/sessions/{sid}.json``.
    """
    from chaos_agent.memory.tui_session_store import (
        get_global_tui_session_store,
    )

    req_id = getattr(req.state, "request_id", "")
    bad = _validate_session_id(tui_session_id, req_id)
    if bad is not None:
        return bad
    store = get_global_tui_session_store()
    if store is None:
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message="TUI session store is not initialised",
            request_id=req_id,
        )
    file_path = store.session_dir / f"{tui_session_id}.json"
    cleared = False
    if file_path.exists():
        try:
            file_path.unlink()
            cleared = True
        except Exception as e:  # pragma: no cover — best effort
            logger.exception("failed to delete TUI session file")
            return JSONEnvelope.fail(
                code=ResponseCode.INTERNAL_ERROR,
                message=f"failed to delete session file: {e}",
                request_id=req_id,
            )
    return JSONEnvelope.ok(
        data={
            "tui_session_id": tui_session_id,
            "cleared_session_file": cleared,
            "session_file_path": str(file_path),
        },
        request_id=req_id,
    )
