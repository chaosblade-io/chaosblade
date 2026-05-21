"""Pre-reasoning hook: unified memory management entry point.

Called before each LLM reasoning step in agent_loop and execute_loop.
Handles: tool output truncation → context check → async persistence → sync compaction.
"""

import asyncio
import logging
import time

from langchain_core.messages import SystemMessage, RemoveMessage

from chaos_agent.memory.compactor import compact_memory
from chaos_agent.memory.context_manager import (
    CompactTrackingState,
    ContextManager,
    TOKEN_ESTIMATE_SAFETY_MARGIN,
    count_tokens_approx,
    estimate_tokens,
    strip_large_outputs,
)
from chaos_agent.memory.session_store import SessionStore
from chaos_agent.memory.tool_compactor import ToolResultCompactor

logger = logging.getLogger(__name__)


class PreReasoningHook:
    """Unified memory management hook called before each LLM reasoning step."""

    def __init__(
        self,
        context_manager: ContextManager,
        tool_compactor: ToolResultCompactor,
        session_store: SessionStore,
        llm=None,
        tui_session_store=None,
    ):
        self.context_manager = context_manager
        self.tool_compactor = tool_compactor
        self.session_store = session_store
        self.llm = llm
        self.tui_session_store = tui_session_store
        # Per-task circuit breaker state. Keyed by task_id so two
        # concurrent tasks don't share each other's failure counters
        # (a single hook instance is shared across all tasks of a
        # running server). Without this, the
        # ``MAX_CONSECUTIVE_COMPACT_FAILURES`` guard in check_context
        # was dead code — every call constructed a fresh state with
        # zero failures, so the breaker could never trip.
        self._tracking: dict[str, CompactTrackingState] = {}

    def _get_tracking(self, task_id: str) -> CompactTrackingState:
        """Return (creating if needed) the per-task circuit breaker state."""
        state = self._tracking.get(task_id)
        if state is None:
            state = CompactTrackingState()
            self._tracking[task_id] = state
        return state

    def _emit_context_size_snapshot(
        self,
        task_id: str,
        current_tokens: int,
        messages_count: int,
    ) -> None:
        """Convenience wrapper used by the 3 emit points in
        ``__call__``. Pulls ``max_tokens`` / ``trigger_tokens`` from
        the bound ``context_manager`` so call sites only need to
        supply what's genuinely per-emit (the current measurement
        and the messages count). If we ever switch the trigger
        source (e.g. use ``calculate_token_warning_state``'s computed
        effective threshold instead of the legacy
        ``compact_threshold`` attribute), this is the single seam to
        change."""
        self._emit_context_size_event(
            task_id,
            current_tokens=current_tokens,
            max_tokens=self.context_manager.max_tokens,
            trigger_tokens=self.context_manager.compact_threshold,
            messages_count=messages_count,
        )

    def _emit_context_size_event(
        self,
        task_id: str,
        *,
        current_tokens: int,
        max_tokens: int,
        trigger_tokens: int,
        messages_count: int,
    ) -> None:
        """Fan-out a snapshot of the post-hook state size so the TS
        TUI Footer can render a live ``current/window`` indicator
        aligned with the actual compaction trigger.

        Uses the same dual fan-out as ``_emit_compaction_event`` —
        per-task tracker for the legacy CLI status stream + per-tui-
        session tracker (``tui-{sid}``) for the TS TUI's main /turn
        SSE relay. Best-effort; never raises.

        Emitted AFTER any compaction decision so ``current_tokens``
        reflects what the NEXT LLM call will actually see — useful
        because compaction shrinks the value and the Footer should
        visibly drop in real time when that happens."""
        try:
            from chaos_agent.observability.status_tracker import (
                get_tracker,
                StatusEvent,
            )
            event = StatusEvent(
                task_id=task_id,
                # Phase is not a lifecycle marker here — it's a
                # plain "this is the current measurement" frame.
                # "running" is the closest StatusPhase value; the
                # TS TUI dispatcher routes purely on ``source``,
                # not on phase, so the exact value doesn't matter.
                phase="running",
                category="node",
                source="context_size",
                message=(
                    f"context: {current_tokens}/{max_tokens} tokens "
                    f"({messages_count} msgs)"
                ),
                detail={
                    "current_tokens": current_tokens,
                    "max_tokens": max_tokens,
                    "trigger_tokens": trigger_tokens,
                    "messages_count": messages_count,
                },
                duration_ms=0.0,
            )
            if task_id:
                tracker = get_tracker(task_id)
                if tracker:
                    tracker.emit(event)
            tui_sid = self._state_tui_session_id or ""
            if tui_sid:
                tui_tracker = get_tracker(f"tui-{tui_sid}")
                if tui_tracker:
                    tui_tracker.emit(event)
        except Exception:
            pass  # best-effort — never let the indicator break the hook

    async def __call__(self, state: dict, *, force: bool = False) -> dict:
        """Execute memory management before LLM reasoning.

        Returns LangGraph-compatible state updates:
        - Tool truncation: returns updated messages (replaced by ID via add_messages reducer)
        - Compression: returns RemoveMessage entries + summary message

        Args:
            state: Agent state dict with 'messages' key
            force: When True (user-initiated /compact), bypass the
                auto-trigger threshold check, skip the circuit breaker,
                and ALWAYS run the LLM compaction path (no strip-only
                short-circuit). Default False — the auto path called
                before every LLM reasoning step retains its threshold
                gating and cheap-path optimisations.

        Returns:
            Updated state dict with possibly compacted messages
        """
        messages = state.get("messages", [])
        task_id = state.get("task_id", "")

        # Store state fields for _async_session_append routing
        self._state_confirmed_intent = state.get("confirmed_intent")
        self._state_tui_session_id = state.get("tui_session_id", "")

        # 1. Tool output truncation (modifies messages in-place)
        messages = self.tool_compactor.compact(messages, task_id=task_id)

        # 2. Context check — pass per-task tracking so the circuit
        # breaker inside check_context can observe past failures.
        # ``force`` is forwarded so a manual /compact invocation
        # bypasses both the threshold gate and the breaker.
        tracking = self._get_tracking(task_id)
        to_compact, to_keep, valid = self.context_manager.check_context(
            messages, tracking=tracking, force=force
        )

        # check_context returns valid=False when either:
        #   - the circuit breaker has tripped (too many consecutive
        #     failures), or
        #   - the context is at the BLOCKING level (already past the
        #     hard ceiling, no safe way to compact further).
        # Either way it has already written its own log line; we add
        # a hook-level warning so the operator sees the consequence
        # (we skipped compaction this turn) at the call site, not
        # buried in check_context's debug output. We still proceed
        # below — if to_compact is empty we'll bail out at the
        # ``if not to_compact:`` branch; if not, the caller asked us
        # to attempt aggressive stripping anyway.
        if not valid:
            logger.warning(
                f"check_context returned valid=False for task {task_id} "
                f"(circuit breaker tripped or at blocking level); "
                f"skipping LLM compaction this turn"
            )

        # 3. Append messages to SessionStore on EVERY call (not just compaction)
        if task_id and self.session_store:
            asyncio.create_task(
                self._async_session_append(task_id, messages)
            )

        if not to_compact:
            # Tool compaction modifies messages in-place (same IDs, changed content).
            # No state update needed — the message objects in state are already modified.
            # Write memory status to session file (always), but do NOT show in CLI.
            total_tokens = int(count_tokens_approx(messages) * TOKEN_ESTIMATE_SAFETY_MARGIN)
            self._persist_to_session(
                task_id,
                f"Memory OK: {len(messages)} messages, ~{total_tokens} tokens",
            )
            self._emit_context_size_snapshot(task_id, total_tokens, len(messages))
            return {}

        # --- Intermediate route: try aggressive tool output truncation first ---
        # If we can get below the compaction threshold by just truncating tool
        # outputs more aggressively, skip the expensive LLM-based compression.
        stripped = strip_large_outputs(to_compact, threshold=1000)
        # Force mode (manual /compact) ALWAYS runs the LLM path —
        # strip-only just truncates tool output content, it doesn't
        # produce a summary. A user pressing /compact wants the
        # structural collapse a summary gives, not a cosmetic trim.
        # We still run the strip above as preprocessing (smaller LLM
        # input = cheaper call), then fall through to compact_memory.
        if not force:
            combined = stripped + to_keep
            combined_tokens = int(count_tokens_approx(combined) * TOKEN_ESTIMATE_SAFETY_MARGIN)
            if combined_tokens < self.context_manager.compact_threshold:
                logger.info(
                    f"Aggressive tool truncation sufficient: {combined_tokens} tokens "
                    f"(threshold {self.context_manager.compact_threshold}), "
                    f"skipping LLM compression"
                )
                # Hand the stripped messages back to LangGraph so the state
                # actually reflects the truncation. ``strip_large_outputs``
                # produces NEW message objects via ``model_copy`` (LangChain
                # BaseModels are immutable), preserving each message's id.
                # The ``add_messages`` reducer dedupes by id and REPLACES
                # the originals with the stripped copies — exactly what we
                # want here.
                #
                # PREVIOUS BUG: this returned ``{}``, so the stripped
                # objects were discarded and the next reasoning step still
                # saw the un-stripped tool output. The whole "intermediate
                # route" was therefore silently a no-op: token usage never
                # dropped, and we'd hit the same threshold again next turn,
                # paying the strip cost over and over with zero effect.
                self._emit_context_size_snapshot(task_id, combined_tokens, len(combined))
                return {"messages": stripped}

        # Calculate total tokens before compression for observability
        total_tokens_before = sum(
            estimate_tokens(getattr(msg, "content", ""))
            for msg in to_compact
            if isinstance(getattr(msg, "content", ""), str)
        )

        logger.info(
            f"Compacting {len(to_compact)} messages for task {task_id}"
        )

        # Emit STARTED event (NODE category → debug-only in CLI, always persisted to session)
        self._emit_compaction_event(
            task_id,
            "started",
            f"Compressing {len(to_compact)} messages ({len(to_keep)} kept)",
            category="node",
            detail={
                "messages_to_compact": len(to_compact),
                "messages_to_keep": len(to_keep),
                "total_tokens_before": total_tokens_before,
            },
        )

        # 4. Sync compaction (generate structured summary).
        # Pass ``state`` so compact_memory's extract_critical_context
        # pulls structured fields (active blade_uid, skill, target,
        # plan_path) out of state and prepends them as a recovery
        # header on the summary. Without this, those fields rely
        # entirely on the LLM remembering to copy them — fine on a
        # good day, lossy on a bad one. The auto path used to skip
        # ``state``; passing it here is the same fix the unification
        # applied to the manual /compact path.
        start_time = time.monotonic()
        try:
            previous_summary = state.get("compressed_summary", "")
            summary = await compact_memory(
                to_compact,
                previous_summary=previous_summary,
                llm=self.llm,
                state=state,
            )

            duration_ms = (time.monotonic() - start_time) * 1000
            tokens_after = int(count_tokens_approx(to_keep) * TOKEN_ESTIMATE_SAFETY_MARGIN)
            # Circuit breaker bookkeeping: a successful compaction
            # resets the consecutive-failure counter (so a transient
            # LLM outage doesn't permanently trip the breaker once the
            # provider recovers) and marks this turn as compacted.
            tracking.consecutive_failures = 0
            tracking.compacted = True
            tracking.turn_count += 1
            self._emit_compaction_event(
                task_id,
                "completed",
                f"Compression done: {len(to_compact)} messages compressed ({duration_ms:.0f}ms)",
                category="node",
                duration_ms=duration_ms,
                detail={
                    "messages_compacted": len(to_compact),
                    "tokens_before": total_tokens_before,
                    "tokens_after": tokens_after,
                },
            )
        except Exception as e:
            duration_ms = (time.monotonic() - start_time) * 1000
            # Circuit breaker: count this failure. After
            # MAX_CONSECUTIVE_COMPACT_FAILURES in a row, the next
            # check_context call will short-circuit with valid=False
            # and we'll stop hammering the LLM with a request it
            # clearly can't handle (e.g. provider 5xx, malformed
            # response, OOM in the summariser).
            tracking.consecutive_failures += 1
            self._emit_compaction_event(
                task_id,
                "failed",
                f"Compression failed: {e}",
                category="node",
                duration_ms=duration_ms,
            )
            raise

        # 5. Build LangGraph-compatible state update:
        #    Remove compacted messages + add incremental summary message
        #    Incremental: each summary is a separate [Compressed History] entry,
        #    never re-compresses previous summaries (they are in to_keep).
        summary_message = SystemMessage(content=f"[Compressed History]\n{summary}")

        remove_messages = []
        for msg in to_compact:
            msg_id = getattr(msg, "id", None)
            if msg_id:
                remove_messages.append(RemoveMessage(id=msg_id))

        # Append new summary (don't replace previous summaries)
        # The previous [Compressed History] messages are in to_keep and untouched.
        # Emit the post-compaction state size so the Footer indicator
        # visibly drops in real time when the summary lands.
        post_compaction_messages = list(to_keep) + [summary_message]
        post_compaction_tokens = int(
            count_tokens_approx(post_compaction_messages) * TOKEN_ESTIMATE_SAFETY_MARGIN
        )
        self._emit_context_size_snapshot(
            task_id, post_compaction_tokens, len(post_compaction_messages),
        )
        return {
            "messages": remove_messages + [summary_message],
            "compressed_summary": summary,
        }

    async def _async_session_append(self, task_id: str, messages: list) -> None:
        """Append messages to appropriate store based on execution phase.

        Intent clarification phase (confirmed_intent=None/"unset"):
        → NO session file write. Intent dialogue persistence is handled
          exclusively by intent_clarification._persist_dialogue, which
          writes the user-visible filtered version (internal tools removed,
          fallback content injected). This eliminates the double-write bug
          where the hook wrote raw LLM output and _persist_dialogue wrote
          the filtered version, causing dedup failures when content differed
          (e.g. empty content vs fallback text).

        Execution phase (confirmed_intent=inject/recover/chat):
        → write to task file (execution messages).
        """
        try:
            # Determine routing based on confirmed_intent in state
            confirmed_intent = self._state_confirmed_intent

            if confirmed_intent in (None, "unset"):
                # Intent clarification: single-source persistence via
                # _persist_dialogue. Hook does NOT write session file.
                return

            # Inject/recover execution → task JSONL (existing behavior)
            self.session_store.append_messages(task_id, messages)
        except Exception as e:
            logger.warning(f"Failed to append messages for task {task_id}: {e}")

    def _persist_to_session(self, task_id: str, message: str, detail: dict = None) -> None:
        """Persist a memory event to the task file (always, regardless of mode)."""
        if task_id and self.session_store:
            try:
                self.session_store.append_raw_message(task_id, {
                    "type": "system",
                    "content": f"[Memory Compression] {message}",
                    "detail": detail or {},
                })
            except Exception:
                pass  # Session persistence is best-effort

    def _emit_compaction_event(
        self,
        task_id: str,
        phase: str,
        message: str,
        *,
        category: str = "node",
        source: str = "memory_compression",
        detail: dict = None,
        duration_ms: float = 0.0,
    ) -> None:
        """Emit a status event for memory compression activity.

        Uses tracker.emit() directly to avoid disrupting the calling node's
        tracker state (_current_source, _start_time).

        CLI visibility: NODE category → debug mode only.
        Session persistence: always written regardless of mode.
        """
        event_detail = detail or {}

        # 1. Emit to status tracker (real-time CLI observability)
        try:
            from chaos_agent.observability.status_tracker import (
                get_tracker,
                StatusEvent,
            )
            event = StatusEvent(
                task_id=task_id,
                phase=phase,
                category=category,
                source=source,
                message=message,
                detail=event_detail,
                duration_ms=duration_ms,
            )
            # Primary fan-out target: the per-task tracker the legacy
            # CLI ``/api/v1/status-stream/{task_id}`` endpoint
            # subscribes to. Existing CLI consumers continue to
            # receive every memory_compression event unchanged.
            tracker = get_tracker(task_id)
            if tracker:
                tracker.emit(event)
            # Phase 4 fan-out target: the TS TUI's main turn SSE
            # (``POST /sessions/{sid}/turn``) subscribes to a tracker
            # keyed by ``f"tui-{tui_session_id}"`` so it can merge
            # memory_compaction events into the same stream that
            # carries token/tool/phase events. Without this fan-out,
            # the TS TUI sees a multi-second silent stall while
            # ``compact_memory()`` runs and assumes the connection
            # has hung. ``tui_session_id`` is captured at the top
            # of ``__call__`` (line ~47) so it's available even when
            # state.task_id has been mutated to an op-task-id by
            # intent_clarification.
            tui_sid = self._state_tui_session_id or ""
            if tui_sid:
                tui_tracker = get_tracker(f"tui-{tui_sid}")
                if tui_tracker:
                    tui_tracker.emit(event)
        except Exception:
            pass  # Status tracking is best-effort

        # 2. Persist to session store (always, regardless of CLI visibility)
        if task_id:
            self._persist_to_session(
                task_id,
                message,
                detail={**event_detail, "duration_ms": duration_ms} if duration_ms else event_detail,
            )


def merge_hook_updates(result: dict, hook_updates: dict) -> dict:
    """Merge hook state updates into node result, concatenating messages lists.

    The add_messages reducer processes items sequentially: RemoveMessages
    delete old entries, then new messages are appended. Hook's messages
    (RemoveMessages + summary) must precede node's messages (LLM response)
    so deletions are processed before additions.

    Without this, ``result.update(hook_updates)`` overwrites
    ``result["messages"]`` when both contain entries, causing the LLM
    response (and any injection messages) to be silently lost.
    """
    if not hook_updates:
        return result
    for key, value in hook_updates.items():
        if key == "messages" and key in result:
            result["messages"] = value + result["messages"]
        else:
            result[key] = value
    return result
