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

    async def __call__(self, state: dict) -> dict:
        """Execute memory management before LLM reasoning.

        Returns LangGraph-compatible state updates:
        - Tool truncation: returns updated messages (replaced by ID via add_messages reducer)
        - Compression: returns RemoveMessage entries + summary message

        Args:
            state: Agent state dict with 'messages' key

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

        # 2. Context check
        to_compact, to_keep, valid = self.context_manager.check_context(messages)

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
            return {}

        # --- Intermediate route: try aggressive tool output truncation first ---
        # If we can get below the compaction threshold by just truncating tool
        # outputs more aggressively, skip the expensive LLM-based compression.
        stripped = strip_large_outputs(to_compact, threshold=1000)
        combined = stripped + to_keep
        combined_tokens = int(count_tokens_approx(combined) * TOKEN_ESTIMATE_SAFETY_MARGIN)
        if combined_tokens < self.context_manager.compact_threshold:
            logger.info(
                f"Aggressive tool truncation sufficient: {combined_tokens} tokens "
                f"(threshold {self.context_manager.compact_threshold}), "
                f"skipping LLM compression"
            )
            # Update the in-place tool messages that were stripped
            return {}

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

        # 4. Sync compaction (generate structured summary)
        start_time = time.monotonic()
        try:
            previous_summary = state.get("compressed_summary", "")
            summary = await compact_memory(
                to_compact,
                previous_summary=previous_summary,
                llm=self.llm,
            )

            duration_ms = (time.monotonic() - start_time) * 1000
            tokens_after = int(count_tokens_approx(to_keep) * TOKEN_ESTIMATE_SAFETY_MARGIN)
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
