"""TUI session lifecycle endpoints (M1 of TS TUI rollout).

Provides:
  - POST   /api/v1/sessions             create session
  - DELETE /api/v1/sessions/{sid}       destroy session
  - GET    /api/v1/sessions/{sid}/state read state
  - POST   /api/v1/sessions/{sid}/turn  (in turn.py) — SSE turn stream
  - POST   /api/v1/sessions/{sid}/interrupt (in interrupt.py)
  - POST   /api/v1/sessions/{sid}/cancel    (in interrupt.py)

Sessions are kept in an in-memory dict for M1. The store is process-local
and dies with the server. M3+ may migrate to sqlite if cross-process
session sharing becomes a requirement.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from chaos_agent.memory.tui_session_store import get_global_tui_session_store
from chaos_agent.models.schemas import JSONEnvelope, ResponseCode
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


# Single router shared by sessions.py / turn.py / interrupt.py.
# Each module imports this and registers its handlers via the shared
# decorator, keeping the URL prefix in one place.
sessions_router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    cluster: str | None = None
    namespace: str | None = "default"
    model_name: str | None = None


class SessionStore:
    """In-memory TUI session store.

    Holds session metadata and a registry of in-flight interrupt
    resolution futures so ``/turn`` (which yields SSE) can hand off
    the wait to ``/interrupt`` (which posts the answer).
    """

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        # interrupt_id (== task_id for M1) → asyncio.Future[str]
        # /turn awaits this; /interrupt sets the result.
        self._pending: dict[str, asyncio.Future[str]] = {}

    # -- session CRUD ------------------------------------------------

    def create(self, body: CreateSessionRequest) -> str:
        # Populate model_name + kubeconfig from settings so the TS TUI
        # can render the welcome card from /sessions/<sid>/state alone
        # — without waiting for the slower /preflight call. The welcome
        # card paints first; the doctor card lands later from the
        # preflight phase.
        from chaos_agent.config.settings import settings
        from chaos_agent.preflight import expand_kubeconfig_path

        sid = f"sess_{uuid4().hex[:12]}"
        self._items[sid] = {
            "id": sid,
            "cluster": body.cluster or "",
            "namespace": body.namespace or "default",
            "model_name": body.model_name or settings.model_name or "",
            "kubeconfig": expand_kubeconfig_path(settings.kubeconfig_path) or "",
            "created_at": now_iso(),
            "task_ids": [],
            # One session ↔ one LangGraph thread. Allocated up-front
            # at session create so every ``/turn`` call has a stable
            # thread_id to feed into the checkpointer — that's what
            # makes intent_clarification see the full multi-turn
            # message history (``messages`` accumulates via the
            # ``add_messages`` reducer; the checkpointer rehydrates
            # it on each invocation). NEVER reset within the
            # session: a successful inject / recover does NOT start
            # a new thread, so the LLM keeps remembering the
            # earlier conversation. Only a fresh ``POST /sessions``
            # produces a new thread_id.
            "conversation_thread_id": f"conv-{uuid4().hex[:12]}",
            # First-turn marker. Controls whether ``/turn`` builds
            # the full initial_state (session-level fields like
            # kubeconfig / interaction_mode / operation that the
            # checkpointer needs to see at least once) or the
            # selective-reset turn_input (subsequent turns).
            "first_turn_done": False,
        }
        return sid

    def delete(self, sid: str) -> None:
        self._items.pop(sid, None)

    def get(self, sid: str) -> dict[str, Any] | None:
        return self._items.get(sid)

    def add_task(self, sid: str, task_id: str) -> None:
        """Track a task in the in-memory session record only.

        Why no on-disk mirror here: every turn (including chat/Q&A and
        intent clarification rounds) allocates a task_id, but the
        session file's ``task_ids`` list is meant to be an audit trail
        of *real operations* (inject / recover) — not every keystroke.
        The disk mirror is therefore deferred to ``turn.py``, which
        only calls ``TuiSessionStore.add_task`` once the turn's intent
        has been classified as inject or recover. Pure-chat turns leave
        the on-disk list untouched.
        """
        sess = self._items.get(sid)
        if sess is not None:
            sess.setdefault("task_ids", []).append(task_id)

    # -- interrupt resolution registry -------------------------------

    def register_interrupt(self, interrupt_id: str) -> asyncio.Future[str]:
        """Allocate a future that ``/turn`` awaits and ``/interrupt``
        resolves. Replaces any existing one with the same id (last write
        wins; in practice ids are uuid-based and never collide)."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending[interrupt_id] = fut
        return fut

    def resolve_interrupt(self, interrupt_id: str, answer: str) -> bool:
        fut = self._pending.pop(interrupt_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(answer)
        return True

    def cancel_interrupt(self, interrupt_id: str) -> None:
        fut = self._pending.pop(interrupt_id, None)
        if fut is not None and not fut.done():
            fut.cancel()


# Module-level singleton. The store lives for the server's lifetime.
# Importable by turn.py / interrupt.py without going through app.state.
_GLOBAL_STORE = SessionStore()


def get_store() -> SessionStore:
    return _GLOBAL_STORE


# -- HTTP handlers ----------------------------------------------------


@sessions_router.post("")
async def create_session(body: CreateSessionRequest) -> dict[str, str]:
    sid = _GLOBAL_STORE.create(body)
    # Persist to disk via the shared TuiSessionStore so the TS TUI
    # session ends up under ``~/.blade-ai/memory/sessions/<sid>.json``
    # — same schema/path the legacy Python TUI uses. Failure here is
    # non-fatal: the agent still works, the user just won't get the
    # post-session audit trail.
    store = get_global_tui_session_store()
    if store is not None:
        try:
            store.create(
                sid,
                cluster_name=body.cluster or "",
                namespace=body.namespace or "default",
            )
        except Exception as e:
            logger.warning(f"TuiSessionStore.create failed for {sid}: {e}")
    logger.info(f"Created TUI session {sid}")
    return {"session_id": sid}


@sessions_router.delete("/{sid}")
async def delete_session(sid: str) -> dict[str, bool]:
    # Finalize on disk BEFORE dropping the in-memory entry so the
    # ``.jsonl`` increment log gets compacted into a final ``.json``
    # snapshot and the file is marked ``status: completed``. Mirrors
    # ``tui/app.py:416`` exactly.
    store = get_global_tui_session_store()
    if store is not None:
        try:
            store.finalize(sid, status="completed")
        except Exception as e:
            logger.warning(f"TuiSessionStore.finalize failed for {sid}: {e}")
    _GLOBAL_STORE.delete(sid)
    return {"ok": True}


@sessions_router.get("/{sid}/state")
async def get_state(sid: str) -> dict[str, Any]:
    sess = _GLOBAL_STORE.get(sid)
    if sess is None:
        raise HTTPException(404, "Session not found")
    return sess


class SessionStatsPayload(BaseModel):
    """Goodbye-card numbers PATCH'd by the TS TUI before deleteSession.

    Field names are snake_case to match the schema TuiSessionStore
    persists (which the Python TUI populates via ``update_stats({...})``).
    All fields optional so a partial update (e.g. only message_count) is
    valid — TuiSessionStore.update_stats merges rather than replaces.
    """
    message_count: int | None = None
    injection_count: int | None = None
    injection_success: int | None = None
    injection_fail: int | None = None
    recovery_count: int | None = None


class CompactRequest(BaseModel):
    """Body for ``POST /sessions/{sid}/compact``.

    ``thread_id`` is optional — when omitted the server picks the
    most recent task id stored against the TUI session, mirroring
    the Python TUI's ``self._conversation.conversation_thread_id``
    auto-resolution. Pass it explicitly when the client wants to
    target a specific in-progress thread (e.g., compacting a stale
    inject task whose checkpoint is still bloating).
    """

    thread_id: str | None = None


@sessions_router.post("/{sid}/compact")
async def compact_session(sid: str, body: CompactRequest, req: Request):
    """Force-compact the conversation thread tied to a TUI session.

    Mirror of Python TUI's ``/compact`` slash (``tui/controllers/
    commands.py:1038`` → ``_compact_thread``). Reaches into the
    LangGraph inject agent's checkpointed state, runs
    ``compact_if_needed`` on the message list, and applies the
    result via ``aupdate_state(... RemoveMessage tombstones +
    compacted)``. Returns before/after token counts so the TS handler
    renders an honest "saved Ntokens / X%" line.

    Resolves the thread id in this order:
      1. ``body.thread_id`` if the client supplied one.
      2. The last entry in ``TuiSessionStore.task_ids`` for ``sid``.
    Both must end up non-empty; otherwise we return TASK_NOT_FOUND
    so the TS handler can show "no active conversation to compact".
    """
    from chaos_agent.config.settings import settings as s
    from chaos_agent.memory.compactor import compact_if_needed
    from chaos_agent.memory.context_manager import count_tokens_approx
    from chaos_agent.server.routes.memory import _validate_session_id

    req_id = getattr(req.state, "request_id", "")

    # Validate ``sid`` before passing into ``TuiSessionStore.read``,
    # which builds ``session_dir / f"{sid}.json"`` internally — a
    # crafted ``../../etc/passwd`` would escape the sessions
    # directory. Same gate as ``/api/v1/memory/{sid}``.
    bad = _validate_session_id(sid, req_id)
    if bad is not None:
        return bad

    # Resolve thread id.
    thread_id = (body.thread_id or "").strip()
    if not thread_id:
        store = get_global_tui_session_store()
        if store is not None:
            data = store.read(sid) or {}
            tasks = list(data.get("task_ids") or [])
            if tasks:
                thread_id = tasks[-1]
    if not thread_id:
        return JSONEnvelope.fail(
            code=ResponseCode.TASK_NOT_FOUND,
            message=(
                f"no conversation thread found for session '{sid}' — "
                "start a /run first or pass thread_id in the body"
            ),
            request_id=req_id,
        )

    agents = getattr(req.app.state, "agents", None) or {}
    graph = agents.get("inject")
    if graph is None:
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message="inject agent is not initialised on this server",
            request_id=req_id,
        )

    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": s.recursion_limit,
    }
    try:
        snapshot = await graph.aget_state(config)
    except Exception as e:  # pragma: no cover — defensive
        logger.exception("compact: aget_state failed")
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message=f"failed to read thread state: {e}",
            request_id=req_id,
        )

    messages = list((snapshot.values or {}).get("messages") or [])
    before = count_tokens_approx(messages)
    if before == 0:
        return JSONEnvelope.ok(
            data={
                "thread_id": thread_id,
                "tokens_before": 0,
                "tokens_after": 0,
                "tokens_saved": 0,
                "compacted": False,
                "layer": "noop",
                "message": "no messages to compact",
            },
            request_id=req_id,
        )

    # Same budget formula as Python TUI's ``_compact_thread`` (see
    # ``tui/controllers/commands.py:1070``). Lower-bound 1 so a
    # mis-configured ``context_compact_ratio`` doesn't request 0.
    budget = max(1, int(s.context_max_tokens * s.context_compact_ratio))
    # The factory writes the live LLM into the agents dict (see
    # ``agent/factory.py``); fall back to None so an older server
    # without the field still produces a valid Layer-1 (lightweight)
    # compaction without crashing.
    llm = agents.get("llm")
    try:
        compacted, used_lightweight = await compact_if_needed(
            messages=messages,
            max_tokens=budget,
            llm=llm,
        )
    except Exception as e:
        logger.exception("compact_if_needed failed")
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message=f"compaction failed: {e}",
            request_id=req_id,
        )

    after = count_tokens_approx(compacted)
    if after >= before:
        # Either nothing was over budget or the compactor preserved
        # everything (rare — happens when budget ≥ before). Tell the
        # TS handler explicitly so it can show "no compaction needed"
        # rather than "saved 0 tokens".
        return JSONEnvelope.ok(
            data={
                "thread_id": thread_id,
                "tokens_before": before,
                "tokens_after": after,
                "tokens_saved": 0,
                "compacted": False,
                "layer": "noop",
                "budget": budget,
            },
            request_id=req_id,
        )

    # Apply the compaction: emit RemoveMessage tombstones for the old
    # ids LangGraph still has on the checkpoint, then append the
    # compacted list. Mirror of ``_compact_thread`` line 1088.
    try:
        from langchain_core.messages import RemoveMessage

        removals = [
            RemoveMessage(id=m.id) for m in messages if getattr(m, "id", None)
        ]
        await graph.aupdate_state(
            config, {"messages": removals + list(compacted)}
        )
    except Exception as e:
        logger.exception("compact: aupdate_state failed")
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message=f"failed to apply compaction: {e}",
            request_id=req_id,
        )

    saved = before - after
    return JSONEnvelope.ok(
        data={
            "thread_id": thread_id,
            "tokens_before": before,
            "tokens_after": after,
            "tokens_saved": saved,
            "compacted": True,
            "layer": "lightweight" if used_lightweight else "llm_summary",
            "budget": budget,
        },
        request_id=req_id,
    )


@sessions_router.patch("/{sid}/stats")
async def patch_session_stats(
    sid: str, payload: SessionStatsPayload
) -> dict[str, bool]:
    """Merge a stats dict into the session file. Idempotent and safe
    to call multiple times (last write wins per field). Designed for
    the TS TUI's exit-time flush: ``cleanup()`` posts the goodbye-card
    counters right before issuing ``DELETE /sessions/<sid>`` so the
    finalize snapshot below carries them."""
    if _GLOBAL_STORE.get(sid) is None:
        raise HTTPException(404, "Session not found")
    store = get_global_tui_session_store()
    if store is None:
        # Not fatal — TS TUI's cleanup catches HTTP errors. Returning a
        # bare ok also avoids leaking server config details.
        return {"ok": False}
    # Strip None fields so update_stats doesn't overwrite real values
    # with nulls. Pydantic v2 ``model_dump(exclude_none=True)`` handles it.
    stats = payload.model_dump(exclude_none=True)
    try:
        store.update_stats(sid, stats)
    except Exception as e:
        logger.warning(f"TuiSessionStore.update_stats failed for {sid}: {e}")
        return {"ok": False}
    return {"ok": True}
