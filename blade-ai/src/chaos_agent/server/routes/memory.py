"""``/api/v1/memory`` — TS TUI session-memory inspection + cleanup.

Surface mirrors Python's ``/memory`` slash family:
  - ``GET    /api/v1/memory/{tui_session_id}`` → session metadata + recent
    task ids + stats + resolved memory_dir.
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
import re

from fastapi import Request

from chaos_agent.models.schemas import JSONEnvelope, ResponseCode
from chaos_agent.server.routes import memory_router

logger = logging.getLogger(__name__)


# Whitelist for ``tui_session_id`` path params. Server-side
# ``createSession`` produces ``sess_<12 hex>`` (see
# ``server/routes/sessions.py``); we accept the same shape plus a
# generous superset (alnum + dash + underscore, max 128 chars) so a
# user-managed deployment can pick its own id format without
# patching the server. Anything else (slashes, dots, ``..``,
# percent-encoded path separators after FastAPI decode) is
# rejected before the value is composed into a Path.
#
# Without this gate ``tui_session_id="../../etc/passwd"`` would
# produce ``<session_dir>/../../etc/passwd.json`` which escapes
# the sessions directory. ``unlink`` on that path is a confirmed
# delete primitive against any user-readable file the server
# process can reach.
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


def _validate_session_id(tui_session_id: str, req_id: str):
    """Return None when ``tui_session_id`` is safe; otherwise return a
    fail envelope the caller can surface directly. Single-shot helper
    so both GET and DELETE keep the validation literal in one place."""
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
