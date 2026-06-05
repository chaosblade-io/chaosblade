"""ConversationController — manages the conversation stream lifecycle.

Multi-invocation model:
  - First user message starts a new thread via inject_stream(interaction_mode="tui")
  - Subsequent messages continue the thread via converse_stream(thread_id, message)
  - The graph ends naturally after each turn (router → END)
  - The TUI REPL waits for next input
  - When agent_loop is triggered, the full pipeline runs in one invocation
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from chaos_agent.tui.bridge import EventBridge
from chaos_agent.tui.intent import IntentRouter, IntentType
from chaos_agent.tui.interrupt import InterruptHandler
from chaos_agent.tui.state import ConversationMode, SessionState
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


class ConversationController:
    """Manages the conversation stream lifecycle.

    Responsibilities:
    - Classify user intent (slash command / exit / agent input)
    - Manage multi-turn conversation threads (multi-invocation model)
    - Start inject streams and consume events through the renderer
    - Cancel active tasks and trigger recovery
    - Resume interrupted tasks (crash recovery)
    """

    def __init__(
        self,
        state: SessionState,
        runner,
        renderer,
        console=None,
    ) -> None:
        self._state = state
        self._runner = runner
        self._renderer = renderer
        self._bridge = EventBridge(renderer)
        self._interrupt_handler = InterruptHandler(
            console=console, renderer=renderer, state=state
        )
        self._intent_router = IntentRouter()
        self._interrupt_cb = self._make_interrupt_cb()
        # Persists past `_start_stream`'s finally so cancel/recover can read it.
        self._last_task_id: str = ""
        # Multi-invocation conversation thread tracking
        self._conversation_thread_id: str = ""
        self._in_conversation: bool = False
        # Did the last handle_input run the full inject pipeline (vs. ending
        # in intent_clarification as chat / Q&A / recover-bridge)? Read by
        # tui/app.py to gate the goodbye-panel injection counter.
        self._last_turn_was_injection: bool = False
        # Did the last injection turn fail (runner emitted an ``error`` event,
        # e.g. baseline failure or safety rejection)? Distinct from
        # injection_failed in app.py which only fires on raised exceptions.
        self._last_turn_failed: bool = False

    @property
    def interrupt_handler(self) -> InterruptHandler:
        return self._interrupt_handler

    @property
    def last_task_id(self) -> str:
        return self._last_task_id

    @property
    def in_conversation(self) -> bool:
        return self._in_conversation

    @property
    def last_turn_was_injection(self) -> bool:
        """True iff the most recent handle_input ran through the inject pipeline.

        False for chat / cluster Q&A / capability Q&A / recover-bridge turns
        (i.e. anything that ended in intent_clarification with a
        ``conversation_turn`` event).
        """
        return self._last_turn_was_injection

    @property
    def last_turn_failed(self) -> bool:
        """True when the last injection turn surfaced an ``error`` event from
        the runner (e.g. baseline failure or safety rejection). app.py reads
        this to classify the goodbye-panel counter as fail vs. success when
        no exception was raised."""
        return self._last_turn_failed

    async def handle_input(self, text: str, *, dry_run: bool = False) -> None:
        """Process user natural language input.

        Slash commands are handled by the caller (CommandDispatcher);
        EXIT raises asyncio.CancelledError-style by re-raising SystemExit;
        AGENT_INPUT starts or continues a conversation stream.

        When ``dry_run`` is True a *new* conversation is forced (Dry-Run
        threads cannot be silently appended onto an existing live thread).
        """
        intent = self._intent_router.classify(text)

        if intent == IntentType.SLASH_COMMAND:
            return  # caller dispatches

        if intent == IntentType.EXIT:
            raise SystemExit(0)

        # Reset the per-turn injection flags; populated by _start_stream /
        # _continue_conversation based on what the graph actually did.
        self._last_turn_was_injection = False
        self._last_turn_failed = False

        # Multi-invocation model: continue existing conversation or start new one.
        if dry_run:
            # /plan always starts a new dry-run thread, even if one is open.
            self._end_conversation()
            await self._start_stream(text, dry_run=True)
            return

        if self._in_conversation and self._conversation_thread_id:
            await self._continue_conversation(text)
        else:
            await self._start_stream(text)

    def classify_intent(self, text: str) -> IntentType:
        return self._intent_router.classify(text)

    def _make_interrupt_cb(self):
        """Wrap interrupt_handler to sidewrite the user's confirm answer."""
        raw_cb = self._interrupt_handler.handle_interrupt

        async def _cb(interrupt_info: dict) -> str:
            answer = await raw_cb(interrupt_info)
            _store = getattr(self._runner, "_tui_session_store", None)
            if _store and self._state.tui_session_id:
                _store.append_event(
                    self._state.tui_session_id, {
                        "ts": now_iso(),
                        "source": "user",
                        "task_id": "",
                        "event_type": "confirm_answer",
                        "data": {"content": answer},
                    },
                )
            return answer

        return _cb

    def _sidewrite_user_input(self, text: str) -> None:
        """Record user input to the Display Store (fire-and-forget)."""
        _store = getattr(self._runner, "_tui_session_store", None)
        if _store and self._state.tui_session_id:
            _store.append_event(
                self._state.tui_session_id, {
                    "ts": now_iso(),
                    "source": "user",
                    "task_id": "",
                    "event_type": "user_input",
                    "data": {"content": text},
                },
            )

    async def _start_stream(self, input_text: str, *, dry_run: bool = False, **kwargs) -> None:
        """Start a new inject stream (first message in a conversation)."""
        if not self._runner:
            self._renderer.error("AgentRunner not initialized")
            return

        intent = self._intent_router.classify(input_text)
        if intent == IntentType.AGENT_INPUT:
            self._state.conversation_mode = ConversationMode.DIRECT
        else:
            self._state.conversation_mode = ConversationMode.EXPLORATION

        from chaos_agent.tui.state import PermissionMode

        # In dry-run mode the gate never interrupts, so we don't need to ask
        # for a confirmation prompt — surface the preview directly.
        confirm = (not dry_run) and self._state.permission_mode == PermissionMode.CONFIRM
        interrupt_cb = self._interrupt_cb

        try:
            self._sidewrite_user_input(input_text)
            raw_stream = self._runner.inject_stream(
                input=input_text,
                confirm=confirm,
                interrupt_callback=interrupt_cb,
                interaction_mode="tui",
                tui_session_id=self._state.tui_session_id,
                dry_run=dry_run,
                **kwargs,
            )
            stream = self._runner._wrap_stream_with_sidewrite(
                raw_stream, self._state.tui_session_id,
            )

            self._state.set_streaming(True)

            task_id = ""
            entered_conversation = False
            turn_had_error = False
            try:
                async for event in stream:
                    if not task_id and event.task_id:
                        task_id = event.task_id
                        self._last_task_id = task_id
                        self._state.set_active_task(task_id)
                    if event.type == "result" and event.task_id:
                        self._last_task_id = event.task_id
                    # conversation_turn signals the graph ended normally
                    # in intent_clarification (multi-invocation model).
                    # For /plan (dry_run), the event carries the Pipeline
                    # task_id so lift_dry_run_and_run can find the checkpoint.
                    if event.type == "conversation_turn":
                        entered_conversation = True
                        if event.task_id:
                            task_id = event.task_id
                        continue
                    if event.type == "error":
                        # inject pipeline failed / safety-rejected: runner
                        # still emits conversation_turn so the TUI stays
                        # interactive, but for counter purposes this is an
                        # injection attempt, not a chat turn.
                        turn_had_error = True
                    await self._bridge.process_stream_event(event)
            except asyncio.CancelledError:
                logger.info(f"Stream cancelled for task {task_id}")
                raise
            except Exception as e:
                logger.error(f"Stream error: {e}")
                self._renderer.error(f"Stream error: {e}")
            finally:
                self._state.set_active_task("")
                self._state.set_streaming(False)

            # Conversation mode (multi-invocation): enter whenever the graph
            # ended in intent_clarification, regardless of success/failure —
            # this matches the runner's behavior and lets the user keep
            # talking after a failed inject.
            if entered_conversation and task_id:
                self._conversation_thread_id = task_id
                self._in_conversation = True
                if dry_run:
                    self._state.set_dry_run(True)
            else:
                self._state.set_dry_run(False)

            # Counter classification is independent of conversation mode:
            # a turn that produced an error event ran (and failed) the
            # inject pipeline; a turn without conversation_turn ran the
            # full pipeline successfully. Pure chat / Q&A turns emit
            # conversation_turn with no error.
            if turn_had_error or not entered_conversation:
                self._last_turn_was_injection = True
            if turn_had_error:
                self._last_turn_failed = True

        except Exception as e:
            logger.error(f"Failed to start inject stream: {e}")
            self._renderer.error(f"Failed to start: {e}")
            self._state.set_streaming(False)

    async def _continue_conversation(self, user_message: str) -> None:
        """Continue an existing conversation thread (subsequent messages)."""
        if not self._runner:
            self._renderer.error("AgentRunner not initialized")
            return

        thread_id = self._conversation_thread_id
        interrupt_cb = self._interrupt_cb

        try:
            self._sidewrite_user_input(user_message)
            raw_stream = self._runner.converse_stream(
                session_id=thread_id,
                user_message=user_message,
                interrupt_callback=interrupt_cb,
            )
            stream = self._runner._wrap_stream_with_sidewrite(
                raw_stream, self._state.tui_session_id, source="intent",
            )

            self._state.set_streaming(True)
            still_in_conversation = False
            turn_had_error = False

            try:
                async for event in stream:
                    if event.type == "conversation_turn":
                        still_in_conversation = True
                        continue
                    # If we get a "result" event, the full pipeline completed
                    # (agent_loop → execute → verify), conversation ends
                    if event.type == "result":
                        if event.task_id:
                            self._last_task_id = event.task_id
                        self._end_conversation()
                    if event.type == "error":
                        turn_had_error = True
                    await self._bridge.process_stream_event(event)
            except asyncio.CancelledError:
                logger.info(f"Conversation cancelled for thread {thread_id}")
                raise
            except Exception as e:
                logger.error(f"Conversation error: {e}")
                self._renderer.error(f"Conversation error: {e}")
            finally:
                self._state.set_streaming(False)

            # If no conversation_turn was received, the graph ran the full
            # pipeline (or errored) — exit conversation mode
            if not still_in_conversation:
                self._end_conversation()

            # Counter classification: an error event means the inject pipeline
            # ran and failed (runner emits both error and conversation_turn);
            # absence of conversation_turn means the full pipeline ran
            # successfully end-to-end.
            if turn_had_error or not still_in_conversation:
                self._last_turn_was_injection = True
            if turn_had_error:
                self._last_turn_failed = True

        except Exception as e:
            logger.error(f"Failed to continue conversation: {e}")
            self._renderer.error(f"Conversation failed: {e}")
            self._state.set_streaming(False)

    def _end_conversation(self) -> None:
        """Exit multi-invocation conversation mode."""
        self._in_conversation = False
        self._conversation_thread_id = ""
        self._state.set_dry_run(False)

    def end_conversation(self) -> None:
        """Public alias of :meth:`_end_conversation` for command handlers."""
        self._end_conversation()

    @property
    def conversation_thread_id(self) -> str:
        """Return the current conversation's thread_id (empty if none.)"""
        return self._conversation_thread_id

    async def is_dry_run_thread(self) -> bool:
        """True when the current conversation thread is in Dry-Run state.

        After /plan, _conversation_thread_id points to the Pipeline
        Graph checkpoint (task_id) where dry_run=True.
        """
        if not (self._in_conversation and self._conversation_thread_id and self._runner):
            return False
        try:
            agents = getattr(self._runner, "_agents", {})
            graph = agents.get("pipeline")
            if graph is None:
                return False
            snap = await graph.aget_state(
                {"configurable": {"thread_id": self._conversation_thread_id}}
            )
            return bool((snap.values or {}).get("dry_run"))
        except Exception:
            return False

    async def lift_and_run(self) -> None:
        """Lift the current Dry-Run thread and execute the planned injection."""
        if not self._runner:
            self._renderer.error("AgentRunner not initialized")
            return
        thread_id = self._conversation_thread_id
        if not thread_id:
            self._renderer.error("当前没有可落地的 Dry-Run 计划")
            return

        interrupt_cb = self._interrupt_cb
        try:
            raw_stream = self._runner.lift_dry_run_and_run(
                thread_id=thread_id,
                interrupt_callback=interrupt_cb,
            )
            stream = self._runner._wrap_stream_with_sidewrite(
                raw_stream, self._state.tui_session_id,
            )
            self._state.set_streaming(True)
            self._state.set_active_task(thread_id)
            try:
                async for event in stream:
                    if event.type == "result":
                        # Pipeline produced a real injection; conversation ends.
                        self._end_conversation()
                    await self._bridge.process_stream_event(event)
            except asyncio.CancelledError:
                logger.info(f"lift_and_run cancelled for {thread_id}")
                raise
            except Exception as e:
                logger.error(f"lift_and_run stream error: {e}")
                self._renderer.error(f"Dry-Run 落地失败: {e}")
            finally:
                self._state.set_active_task("")
                self._state.set_streaming(False)
        except Exception as e:
            logger.error(f"lift_and_run failed: {e}")
            self._renderer.error(f"Dry-Run 落地失败: {e}")
            self._state.set_streaming(False)

    async def cancel(self) -> None:
        """Trigger recovery for the most recently active task.

        In the synchronous REPL flow the stream is already torn down by
        `KeyboardInterrupt` propagating up; this method just kicks off
        the recovery side-effects on the runner.
        """
        task_id = self._last_task_id
        self._state.set_active_task("")
        self._state.set_streaming(False)
        self._end_conversation()

        if task_id and task_id != "pending" and self._runner:
            try:
                await self._runner.recover(task_id=task_id)
            except Exception as e:
                logger.error(f"Recovery after cancel failed: {e}")

    async def resume(self, task_id: str, interrupt_info: Optional[dict] = None) -> None:
        """Resume an interrupted task (crash recovery)."""
        if not self._runner:
            self._renderer.error("AgentRunner not initialized")
            return

        interrupt_cb = self._interrupt_cb

        resume_value = None
        if interrupt_info:
            resume_value = await self._interrupt_cb(interrupt_info)

        try:
            raw_stream = self._runner.resume_stream(
                task_id=task_id,
                resume_value=resume_value,
                interrupt_callback=interrupt_cb,
            )
            stream = self._runner._wrap_stream_with_sidewrite(
                raw_stream, self._state.tui_session_id,
            )
            async for event in stream:
                await self._bridge.process_stream_event(event)
        except Exception as e:
            logger.error(f"Resume failed: {e}")
            self._renderer.error(f"Resume failed: {e}")

    async def recover_task(self, task_id: str) -> None:
        """Explicitly recover a specific task."""
        if not self._runner:
            self._renderer.error("AgentRunner not initialized")
            return

        try:
            await self._runner.recover(task_id=task_id)
            self._renderer.system(f"Recovery completed for {task_id}")
        except Exception as e:
            logger.error(f"Recovery failed: {e}")
            self._renderer.error(f"Recovery failed: {e}")

    async def cleanup(self) -> None:
        """Clean up resources."""
        self._end_conversation()
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception as e:
                logger.warning(f"Error during runner cleanup: {e}")
