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
    Returns an **SSE stream** so the TS TUI can render the same
    real-time spinner/progress UX the auto-compact path gets via
    ``/turn``.

    Wire format (each frame is one JSON object inside ``data:``):

      - ``type=memory_compaction phase=started``  — hook entered the
        LLM summariser; client should show a spinner.
      - ``type=memory_compaction phase=completed`` — hook returned
        from the LLM call. Carries hook-side estimates.
      - ``type=memory_compaction phase=failed``    — hook raised.
      - ``type=result``                            — terminal event,
        carries the AUTHORITATIVE post-checkpoint
        ``{tokens_before, tokens_after, tokens_saved, compacted,
        layer, thread_id}`` in ``payload``. Sent AFTER the state has
        actually been written via ``aupdate_state``.
      - ``type=error``                             — request-level
        failure (state read/write, missing hook). Followed by
        ``done``.
      - ``type=done``                              — stream end. Client
        loop exits here.

    Mechanics:
      1. Subscribe to a private tracker keyed on a freshly minted
         ``compact-{uuid}`` task id. We OVERRIDE ``state.task_id``
         before passing state into the hook so its
         ``_emit_compaction_event`` lands on OUR tracker, not the
         long-running thread tracker (which would mix /compact events
         with stale auto-compaction events from prior turns).
      2. Run the hook in a background task while a consumer loop
         drains the tracker queue and serialises events as
         ``StreamEvent(type="memory_compaction", ...)`` — identical
         shape to what the /turn SSE relays, so the TS TUI can reuse
         its existing memory_compaction reducer code unchanged.
      3. After the hook returns, apply the LangGraph state update,
         re-read the checkpoint to compute the AUTHORITATIVE
         ``tokens_after`` (the hook's internal estimate is computed
         before the RemoveMessage tombstones are processed by the
         reducer; it isn't quite right at the checkpoint level).
      4. Emit a single ``result`` event with the route-level metrics,
         then ``done``.

    Resolves the thread id in this order:
      1. ``body.thread_id`` if the client supplied one.
      2. The last entry in ``TuiSessionStore.task_ids`` for ``sid``.
    Both must end up non-empty; otherwise we send a single ``error``
    + ``done`` pair so the TS handler can render "no active
    conversation to compact".
    """
    import time
    from fastapi.responses import StreamingResponse

    from chaos_agent.agent.streaming import StreamEvent
    from chaos_agent.config.settings import settings as s
    from chaos_agent.memory.context_manager import count_tokens_approx
    from chaos_agent.observability.status_tracker import (
        subscribe as _status_subscribe,
        unsubscribe as _status_unsubscribe,
    )
    from chaos_agent.server.routes.memory import _validate_session_id

    req_id = getattr(req.state, "request_id", "")

    # --- Pre-stream validation -------------------------------------------
    # We do all the cheap up-front checks BEFORE starting the SSE
    # response. Anything that fails here returns a normal JSON error
    # envelope (matching what older clients of the JSON-shaped
    # endpoint would see for these specific errors). Inside the
    # stream we use ``type=error`` + ``done`` frames instead.
    bad = _validate_session_id(sid, req_id)
    if bad is not None:
        return bad

    # Thread resolution. Three sources in priority order:
    #
    #   1. ``body.thread_id`` — caller-provided override (rare; mostly
    #      for recovery / debugging scripts).
    #   2. ``_GLOBAL_STORE[sid].conversation_thread_id`` — the stable
    #      LangGraph thread allocated at ``POST /sessions``. ONE
    #      session ↔ ONE thread, reused by every ``/turn``. This is
    #      what every chat / inject / recover in this session shares,
    #      so it's the right key for compaction (we want to compact
    #      "the user's conversation", not a specific inject task).
    #   3. Legacy fallback: ``TuiSessionStore.task_ids[-1]`` — only
    #      populated when an inject/recover task allocates its own
    #      task-id. A chat-only session has empty task_ids; relying on
    #      this list alone (the original /compact implementation)
    #      caused ``/compact`` to silently fail with TASK_NOT_FOUND
    #      whenever the user tried to compact a pure chat thread.
    thread_id = (body.thread_id or "").strip()
    if not thread_id:
        sess = _GLOBAL_STORE.get(sid)
        if sess is not None:
            thread_id = sess.get("conversation_thread_id") or ""
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
    hook = agents.get("pre_reason_hook")
    if hook is None:
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message="memory hook is not initialised on this server",
            request_id=req_id,
        )

    # Fresh per-call task id keeps our tracker isolated from the
    # thread's main task id (which carries ambient auto-compaction
    # events we don't want to mix in).
    compact_task_id = f"compact-{uuid4().hex[:12]}"

    def _convert(status_evt) -> StreamEvent | None:
        """Translate a hook-emitted ``StatusEvent`` (source=
        ``memory_compression``) into the wire-format
        ``StreamEvent(type="memory_compaction", ...)`` the TS TUI's
        /turn reducer already understands. Mirror of the helper in
        turn.py — keeping them shape-identical means the TS client
        can reuse one code path for both surfaces."""
        if getattr(status_evt, "source", "") != "memory_compression":
            return None
        detail = getattr(status_evt, "detail", None) or {}
        return StreamEvent(
            type="memory_compaction",
            content=getattr(status_evt, "message", ""),
            task_id=compact_task_id,
            compaction_phase=getattr(status_evt, "phase", ""),
            tokens_before=int(
                detail.get("total_tokens_before")
                or detail.get("tokens_before")
                or 0
            ),
            tokens_after=int(detail.get("tokens_after") or 0),
            messages_compacted=int(
                detail.get("messages_to_compact")
                or detail.get("messages_compacted")
                or 0
            ),
            duration_ms=float(getattr(status_evt, "duration_ms", 0.0) or 0.0),
            layer="llm_summary",
        )

    async def event_generator():
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": s.recursion_limit,
        }
        # Subscribe BEFORE reading state / starting the hook so we
        # don't miss the hook's "started" event (which fires very
        # close to its first await).
        queue = _status_subscribe(compact_task_id)
        # Hold a reference to the hook task at the function-scope
        # level so the ``finally`` block can cancel it if the
        # generator is torn down mid-stream (client disconnect /
        # asyncio.CancelledError). Without this, an abandoned
        # request would keep the LLM call running to completion,
        # wasting tokens AND eventually producing a result that
        # nobody applies to checkpoint state.
        hook_task: asyncio.Task | None = None
        try:
            # Read initial state.
            try:
                snapshot = await graph.aget_state(config)
            except Exception as e:
                yield StreamEvent(
                    type="error",
                    content=f"failed to read thread state: {e}",
                    task_id=compact_task_id,
                ).to_sse()
                yield StreamEvent(type="done", task_id=compact_task_id).to_sse()
                return

            state_values = snapshot.values or {}
            messages = list(state_values.get("messages") or [])
            before = count_tokens_approx(messages)
            if before == 0:
                # Nothing to compact at all — short-circuit with a
                # result frame so the client can render "noop".
                yield StreamEvent(
                    type="result",
                    task_id=compact_task_id,
                    payload={
                        "thread_id": thread_id,
                        "tokens_before": 0,
                        "tokens_after": 0,
                        "tokens_saved": 0,
                        "compacted": False,
                        "layer": "noop",
                        "message": "no messages to compact",
                    },
                ).to_sse()
                yield StreamEvent(type="done", task_id=compact_task_id).to_sse()
                return

            # Hand the hook a copy of state with our private task id
            # so its events flow to our tracker.
            state_for_hook = dict(state_values)
            state_for_hook["task_id"] = compact_task_id

            # Run the hook concurrently with the stream consumer.
            t0 = time.monotonic()
            hook_task = asyncio.create_task(hook(state_for_hook, force=True))

            # Pump tracker events while the hook is running.
            # ``wait_for`` with a 1s tick lets us interleave with the
            # ``hook_task.done()`` check without busy-looping. Every
            # 15s of idle queue (i.e. nothing emitted), we send an
            # SSE comment line as a keepalive — most LLM calls
            # finish faster than that, but proxies (nginx default
            # 60s) and undici (45s) cull silent connections.
            keepalive_idle = 0.0
            while not hook_task.done():
                try:
                    status_evt = await asyncio.wait_for(queue.get(), timeout=1.0)
                    keepalive_idle = 0.0
                    converted = _convert(status_evt)
                    if converted is not None:
                        yield converted.to_sse()
                except asyncio.TimeoutError:
                    keepalive_idle += 1.0
                    if keepalive_idle >= 15.0:
                        yield ": keepalive\n\n"
                        keepalive_idle = 0.0

            # Drain anything emitted between the last queue.get() and
            # the hook returning. ``get_nowait`` is the right
            # primitive — we know nothing else will be added now
            # that hook_task is done.
            while True:
                try:
                    status_evt = queue.get_nowait()
                    converted = _convert(status_evt)
                    if converted is not None:
                        yield converted.to_sse()
                except asyncio.QueueEmpty:
                    break

            # Surface hook exceptions as a structured error frame.
            try:
                updates = hook_task.result()
            except Exception as e:
                logger.exception("compact: hook failed")
                yield StreamEvent(
                    type="error",
                    content=f"compaction failed: {e}",
                    task_id=compact_task_id,
                ).to_sse()
                yield StreamEvent(type="done", task_id=compact_task_id).to_sse()
                return

            # Force-mode hook returns {} when there's literally
            # nothing to compact (everything fits in reserve_tokens).
            if not updates or "messages" not in updates:
                yield StreamEvent(
                    type="result",
                    task_id=compact_task_id,
                    payload={
                        "thread_id": thread_id,
                        "tokens_before": before,
                        "tokens_after": before,
                        "tokens_saved": 0,
                        "compacted": False,
                        "layer": "noop",
                        "message": "no historical messages to compact",
                    },
                ).to_sse()
                yield StreamEvent(type="done", task_id=compact_task_id).to_sse()
                return

            # Apply the LangGraph update.
            try:
                await graph.aupdate_state(config, updates)
            except Exception as e:
                logger.exception("compact: aupdate_state failed")
                yield StreamEvent(
                    type="error",
                    content=f"failed to apply compaction: {e}",
                    task_id=compact_task_id,
                ).to_sse()
                yield StreamEvent(type="done", task_id=compact_task_id).to_sse()
                return

            # Re-read so ``after`` reflects the actual checkpoint
            # post-reducer (RemoveMessage tombstones processed +
            # SystemMessage summary appended).
            try:
                snapshot_after = await graph.aget_state(config)
            except Exception as e:
                logger.exception("compact: aget_state(after) failed")
                yield StreamEvent(
                    type="error",
                    content=f"failed to verify compaction: {e}",
                    task_id=compact_task_id,
                ).to_sse()
                yield StreamEvent(type="done", task_id=compact_task_id).to_sse()
                return

            after = count_tokens_approx(
                (snapshot_after.values or {}).get("messages") or []
            )
            saved = max(0, before - after)
            compacted = saved > 0 and after < before
            duration_ms = (time.monotonic() - t0) * 1000.0

            yield StreamEvent(
                type="result",
                task_id=compact_task_id,
                duration_ms=duration_ms,
                payload={
                    "thread_id": thread_id,
                    "tokens_before": before,
                    "tokens_after": after,
                    "tokens_saved": saved,
                    "compacted": compacted,
                    "layer": "llm_summary" if compacted else "noop",
                },
            ).to_sse()
            yield StreamEvent(type="done", task_id=compact_task_id).to_sse()
        finally:
            # Reap the hook task if the generator was torn down
            # before it finished (client disconnect / asyncio cancel).
            # We DON'T await its result here — the caller is already
            # gone and there's no state update path that would apply
            # the LLM output anyway. Just unwind the task cleanly so
            # asyncio doesn't log a "Task was destroyed but it is
            # pending!" warning and so the LLM call gets a cancel
            # signal it can act on. The hook handles CancelledError
            # internally (it doesn't catch and swallow it).
            if hook_task is not None and not hook_task.done():
                hook_task.cancel()
                try:
                    await hook_task
                except (asyncio.CancelledError, Exception):
                    pass
            _status_unsubscribe(compact_task_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
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
