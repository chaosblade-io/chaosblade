"""AgentRunner: Local execution wrapper that runs Agent Core directly without a server.

Provides the same interface as AgentClient but executes Agent Core locally.
Returns response dicts in the same format as server routes.
"""

import asyncio
import json
import logging
import sys
import time
import uuid
from collections import defaultdict
from typing import Optional

from chaos_agent import __version__
from chaos_agent.agent.factory import create_agent
from chaos_agent.agent.fault_spec import FaultSpec
from chaos_agent.agent.operation_summary import build_task_summary_text
from chaos_agent.agent.skill_identity import read_active_skill_name
from chaos_agent.agent.state_builders import build_inject_initial_state
from chaos_agent.agent.streaming import StreamEvent, parse_stream_events
from chaos_agent.config.settings import settings
from chaos_agent.memory.operation_summary_writer import write_operation_summary
from chaos_agent.models.schemas import JSONEnvelope, ResponseCode, build_inject_envelope
from chaos_agent.observability.status_tracker import (
    subscribe,
    unsubscribe,
    remove_tracker,
)
from chaos_agent.skills.catalog_generator import (
    generate_skill_catalog,
    infer_scope,
    infer_blade_params,
    build_direct_cmd,
)
from chaos_agent.skills.loader import get_skills_dir
from chaos_agent.skills.models import SKILL_TYPE_FAULT_INJECTION
from chaos_agent.skills.prerequisites import PrerequisitesChecker
from chaos_agent.skills.registry import SkillRegistry
from chaos_agent.utils.fault_type import extract_fault_type
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


from chaos_agent.cli.result_builder import (
    _build_inject_result_events,
    _extract_visible_reply,
)
from chaos_agent.cli.session_finalize import (
    _finalize_inject_session,
    _format_error,
    auto_rollback,
)
from chaos_agent.cli.status_display import _status_printer


class AgentRunner:
    """Local execution wrapper - runs Agent Core directly without a server.

    Mirrors the AgentClient interface but invokes the LangGraph agents
    in-process. Returns the same JSON envelope format as server routes.

    Usage:
        runner = AgentRunner()
        result = await runner.inject(fault_type="pod-kill", ...)
        await runner.cleanup()  # close resources when done
    """

    def __init__(self):
        self._registry: Optional[SkillRegistry] = None
        self._agents: Optional[dict] = None
        self._initialized = False
        self._checkpointer_conn = None  # hold ref for cleanup
        self._tui_session_store = None  # set by TUI app

    def _sidewrite_event(
        self,
        session_id: str,
        evt: "StreamEvent",
        source: str = "pipeline",
    ) -> None:
        """Fire-and-forget: persist a StreamEvent to the Display Store."""
        if not self._tui_session_store or not session_id:
            return
        try:
            self._tui_session_store.append_event(session_id, {
                "ts": evt.timestamp,
                "source": source,
                "task_id": evt.task_id or "",
                "event_type": evt.type,
                "data": evt.to_dict(),
            })
        except Exception:
            pass

    async def _wrap_stream_with_sidewrite(self, stream, session_id: str, source: str = "pipeline"):
        """Wrap an async generator to sidewrite all StreamEvents."""
        async for evt in stream:
            self._sidewrite_event(session_id, evt, source)
            yield evt

    async def initialize(self):
        """Explicitly initialize Agent Core components.

        Call this during startup to avoid lazy-init delay on first message.
        Safe to call multiple times (idempotent).
        """
        await self._ensure_initialized()

    async def _ensure_initialized(self):
        """Lazy initialization of Agent Core components."""
        if self._initialized:
            return

        # Initialize skill registry
        self._registry = SkillRegistry()
        self._registry.load_from_directory(get_skills_dir())

        # Check skills loaded
        self._check_skills_loaded(self._registry)

        # Check prerequisites
        prereq_checker = PrerequisitesChecker()
        await prereq_checker.check_startup_prerequisites(self._registry)

        # E9 — CLI MCP client init. Same lifecycle as server lifespan
        # (connect_all with per-server timeout), but persisted on the
        # runner instance and torn down in close().
        from chaos_agent.config.settings import settings as _settings
        self._mcp_manager = None
        if _settings.mcp_enabled:
            from chaos_agent.mcp.manager import McpManager
            self._mcp_manager = McpManager()
            try:
                await self._mcp_manager.connect_all(
                    connect_timeout_seconds=_settings.mcp_connect_timeout_seconds,
                )
            except Exception as e:
                logger.warning(f"MCP startup failed (continuing): {e}")
                self._mcp_manager = None

        # Create agents with checkpointer
        self._agents = await create_agent(self._registry, mcp_manager=self._mcp_manager)
        self._checkpointer_conn = self._agents.get("checkpointer_conn")
        self._session_store = self._agents.get("session_store")
        self._initialized = True
        logger.info(f"AgentRunner initialized - {len(self._registry)} skills loaded")

    @staticmethod
    def _check_skills_loaded(registry: SkillRegistry):
        """Check that at least one skill is loaded.

        Warns if no skills found, as the agent cannot perform fault injection without skills.
        """
        if len(registry) == 0:
            skills_dir = get_skills_dir()
            logger.warning(
                f"No skills loaded from {skills_dir}. "
                f"The agent will not be able to perform fault injection. "
                f"Please copy skill directories (each containing SKILL.md) to {skills_dir}/"
            )

    # ---- inject_stream ----

    async def inject_stream(self, confirm_callback=None, interrupt_callback=None, **kwargs):
        """Stream inject execution, yielding StreamEvent objects in real-time.

        Uses LangGraph astream_events to stream LLM tokens and tool results.
        Handles interrupts (confirmation_gate, ask_human) by yielding a confirm
        event, then resuming via the appropriate callback.

        Args:
            confirm_callback: Optional async callable that returns "approved" or "rejected".
                Kept for backward compatibility with CLI confirm command.
                If None and confirm=True, the graph stays paused after yielding the
                confirm event (caller should use confirm() to resume).
            interrupt_callback: Optional async callable(interrupt_info: dict) -> str.
                Generalized callback that handles both confirmation and question interrupts.
                If provided, takes precedence over confirm_callback for confirmation interrupts.
                interrupt_info format:
                  - confirmation: {"type": "confirmation", "plan_summary": ..., "safety_status": ...}
                  - question:    {"type": "question", "content": "..."}

        Yields:
            StreamEvent: token, tool_start, tool_end, confirm, result, error
        """
        await self._ensure_initialized()

        # TUI mode: delegate to dual-graph converse_stream
        _interaction_mode = kwargs.get("interaction_mode", "cli")
        if _interaction_mode == "tui":
            session_id = kwargs.get("tui_session_id", "") or ""
            input_text = kwargs.get("input", "")
            async for evt in self.converse_stream(
                session_id, input_text,
                interrupt_callback=interrupt_callback,
                tui_session_id=session_id,
                interaction_mode="tui",
                kubeconfig=kwargs.get("kubeconfig", ""),
                kube_context=kwargs.get("context", ""),
                needs_confirmation=kwargs.get("confirm", False),
                dry_run=kwargs.get("dry_run", False),
            ):
                yield evt
            return

        if kwargs.get("kubeconfig"):
            settings.kubeconfig_path = kwargs["kubeconfig"]
        if kwargs.get("context"):
            settings.kube_context = kwargs["context"]
        if kwargs.get("cluster_uuid"):
            settings.kubewiz_cluster_uuid = kwargs["cluster_uuid"]
        if kwargs.get("profile"):
            settings.kubewiz_profile = kwargs["profile"]

        task_id = f"task-{uuid.uuid4()}"
        tui_session_id = kwargs.get("tui_session_id", "") or ""

        # Build initial state. FaultSpec is the single source of truth
        # for fault identity + tuning — entry points construct it and
        # no longer write the legacy scattered fields. Consumers read
        # via ``read_fault_spec(state)``.
        _ts = now_iso()
        _interaction_mode = kwargs.get("interaction_mode", "cli")
        _dry_run = bool(kwargs.get("dry_run", False))
        if kwargs.get("input"):
            spec = FaultSpec.from_cli_nl(input_text=kwargs["input"], kwargs=kwargs)
        else:
            spec = FaultSpec.from_cli_structured(kwargs)
        initial_state = build_inject_initial_state(
            task_id=task_id,
            tui_session_id=tui_session_id,
            fault_spec=spec,
            needs_confirmation=kwargs.get("confirm", False),
            kubeconfig=kwargs.get("kubeconfig", ""),
            kube_context=kwargs.get("context", ""),
            kubewiz_cluster_uuid=kwargs.get("cluster_uuid", ""),
            kubewiz_profile=kwargs.get("profile", ""),
            created_at=_ts,
            direct=kwargs.get("direct", False) if not kwargs.get("input") else False,
            interaction_mode=_interaction_mode,
            dry_run=_dry_run,
        )

        config = {"configurable": {"thread_id": task_id}, "recursion_limit": settings.recursion_limit}
        graph = self._agents["pipeline"]

        # Write initial task state to TaskStore before graph starts
        try:
            from chaos_agent.persistence.task_store import get_task_store
            store = await get_task_store()
            state_for_store = {k: v for k, v in initial_state.items() if k != "task_id"}
            await store.upsert(task_id, **state_for_store)
        except Exception as e:
            logger.warning(f"Failed to write initial state to TaskStore for {task_id}: {e}")

        # Create session for recording. P0-7-6: extract IntentClarificationSummary
        # from initial_state messages (present when intent was converged via
        # dialogue) and pass as initial_messages so the task file starts with
        # the handoff message, not an empty messages list.
        handoff_msg = None
        try:
            from langchain_core.messages import SystemMessage
            for msg in initial_state.get("messages", []):
                content = getattr(msg, "content", "") or ""
                if isinstance(msg, SystemMessage) and content.startswith("[Intent Clarification Summary]"):
                    handoff_msg = msg
                    break
        except Exception:
            pass
        if self._session_store:
            self._session_store.create_session(
                task_id,
                operation="inject",
                tui_session_id=tui_session_id,
                initial_messages=[handoff_msg] if handoff_msg else None,
            )

        # Subscribe to status events for the background printer.
        # In TUI mode the renderer drives its own phase visualization,
        # so the stderr printer would just leak noise alongside the UI.
        _suppress_stderr = _interaction_mode == "tui"
        status_queue = subscribe(task_id)
        done_event = asyncio.Event()
        printer_task = (
            None if _suppress_stderr
            else asyncio.create_task(_status_printer(status_queue, done_event))
        )

        try:
            # Print a notice so the user knows the process is running
            if not _suppress_stderr and not settings.is_debug:
                sys.stderr.write("  ⏳ 故障注入进行中，AI 正在分析并规划，请耐心等待...\n")
                sys.stderr.flush()

            # Phase 1: Stream the first invoke (runs until confirmation_gate or completion)
            final_state = None
            # Track whether any visible token streamed this turn; if not, we
            # synthesize one from the final AIMessage so backends that put
            # the answer into reasoning_content (e.g. qwen enable_thinking)
            # don't leave the user with thinking-only output.
            turn_tokens_seen = False
            async for event in graph.astream_events(initial_state, config, version="v2"):
                for stream_evt in parse_stream_events(event):
                    stream_evt.task_id = task_id
                    if stream_evt.type == "token":
                        turn_tokens_seen = True
                    yield stream_evt

            # Check if graph paused at an interrupt point (confirmation_gate, ask_human, etc.)
            # Loop to handle multiple interrupts (e.g., ask_human then confirmation_gate)
            resume_event_count = 0
            while True:
                current_state = await graph.aget_state(config)

                if not (current_state and current_state.next):
                    break  # Graph completed, no more interrupts

                next_nodes = list(current_state.next)

                # Extract interrupt info from the paused state
                interrupt_info = None
                for task in (current_state.tasks or []):
                    if hasattr(task, 'interrupts') and task.interrupts:
                        interrupt_info = task.interrupts[0].value
                        break

                # Determine interrupt type
                interrupt_type = "confirmation"
                interrupt_content = ""
                if interrupt_info and isinstance(interrupt_info, dict):
                    interrupt_type = interrupt_info.get("type", "confirmation")
                    interrupt_content = interrupt_info
                elif interrupt_info and isinstance(interrupt_info, str):
                    interrupt_content = {"type": "confirmation", "plan_summary": interrupt_info}
                else:
                    # Fallback: infer from graph state
                    plan_summary = current_state.values.get("plan_summary", "") if current_state.values else ""
                    interrupt_content = {"type": "confirmation", "plan_summary": plan_summary}

                # Resume the graph based on callback availability
                resume_value = None
                if interrupt_callback:
                    # Self-contained callback: renders UI and returns answer directly.
                    # No need to yield "confirm" event — the callback handles everything.
                    response = await interrupt_callback(interrupt_content)
                    from langgraph.types import Command

                    async for event in graph.astream_events(
                        Command(resume=response), config, version="v2"
                    ):
                        resume_event_count += 1
                        for stream_evt in parse_stream_events(event):
                            stream_evt.task_id = task_id
                            if stream_evt.type == "token":
                                turn_tokens_seen = True
                            yield stream_evt
                    logger.info(
                        "Resume (interrupt_callback) yielded %d events (task_id=%s)",
                        resume_event_count, task_id,
                    )
                    # Continue loop to check for subsequent interrupts

                elif "confirmation_gate" in next_nodes:
                    # CLI mode: only confirmation type expected (CLI skips intent_clarification)
                    plan_summary = current_state.values.get("plan_summary", "") if current_state.values else ""

                    if not kwargs.get("confirm", False):
                        # Auto-approve: resume with "approved"
                        from langgraph.types import Command

                        async for event in graph.astream_events(
                            Command(resume="approved"), config, version="v2"
                        ):
                            resume_event_count += 1
                            for stream_evt in parse_stream_events(event):
                                stream_evt.task_id = task_id
                                if stream_evt.type == "token":
                                    turn_tokens_seen = True
                                yield stream_evt
                        logger.info(
                            "Resume (auto-approve) yielded %d events (task_id=%s)",
                            resume_event_count, task_id,
                        )
                        # Continue loop to check for subsequent interrupts
                    elif confirm_callback:
                        # Legacy confirm_callback: only handles confirmation
                        decision = await confirm_callback(plan_summary)
                        from langgraph.types import Command

                        async for event in graph.astream_events(
                            Command(resume=decision), config, version="v2"
                        ):
                            resume_event_count += 1
                            for stream_evt in parse_stream_events(event):
                                stream_evt.task_id = task_id
                                if stream_evt.type == "token":
                                    turn_tokens_seen = True
                                yield stream_evt
                        logger.info(
                            "Resume (confirm_callback) yielded %d events (task_id=%s)",
                            resume_event_count, task_id,
                        )
                        # Continue loop to check for subsequent interrupts
                    else:
                        # confirm=True but no callback → graph stays paused,
                        # caller should call runner.confirm(task_id, "approve") later
                        break
                else:
                    # Unknown interrupt without callback — cannot handle
                    logger.warning(f"Unhandled interrupt at {next_nodes}, no callback provided")
                    break

            # Extract final result
            final_state = await graph.aget_state(config)
            _values = final_state.values if final_state and final_state.values else None
            result_events, should_return = _build_inject_result_events(
                _values, task_id, turn_tokens_seen, _interaction_mode,
            )
            for evt in result_events:
                yield evt
            if should_return:
                return

        except Exception as e:
            code, msg = _format_error(e)
            logger.exception(f"Stream inject failed for task {task_id}")

            rollback_info = await auto_rollback(graph, config)

            yield StreamEvent(
                type="error",
                content=f"Inject failed: {msg}{rollback_info}",
                task_id=task_id,
            )
            from chaos_agent.agent.operation_result import build_inject_status_data_from_state

            yield StreamEvent(
                type="result",
                content=json.dumps(JSONEnvelope.fail(
                    code=code,
                    message=f"Inject failed: {msg}{rollback_info}",
                    data=build_inject_status_data_from_state(
                        initial_state,
                        task_id,
                        result="failed",
                        error=f"internal_error: Inject failed: {msg}{rollback_info}",
                    ),
                ), ensure_ascii=False),
                task_id=task_id,
            )
        finally:
            # Finalize session: flush remaining messages from final graph state.
            # Skip finalize when the TUI conversation is still ongoing — i.e.,
            # the graph yielded a conversation_turn and is expected to receive
            # more messages via converse_stream on the same thread_id. Finalizing
            # here would remove the task from _active_sessions and cause the
            # subsequent turn's hook appends to log "Task not found".
            # Compute is_open_conversation here (needs local _interaction_mode)
            # rather than inside _finalize_inject_session.
            _is_open = False
            _vals = {}
            if _interaction_mode == "tui":
                try:
                    _fgs = await graph.aget_state(config)
                    _vals = _fgs.values if _fgs and _fgs.values else {}
                    _is_open = (
                        not _vals.get("blade_uid", "")
                        and not (_vals.get("failure_reason") or _vals.get("error"))
                        and _vals.get("safety_status") != "rejected"
                        and _vals.get("confirmed_intent") not in ("chat",)
                    )
                except Exception:
                    pass
            await _finalize_inject_session(
                self._session_store, graph, config, task_id,
                kwargs=kwargs,
                is_open_conversation=_is_open if _interaction_mode == "tui" else None,
                error_log_level="warning",
                precomputed_values=_vals if _interaction_mode == "tui" else None,
                tui_session_store=self._tui_session_store,
            )
            done_event.set()
            if printer_task is not None:
                await printer_task
            unsubscribe(task_id, status_queue)
            remove_tracker(task_id)

    # ---- inject ----

    async def inject(self, **kwargs) -> dict:
        """Inject a fault locally. Equivalent to POST /api/v1/inject.

        If confirm=False, auto-approves the confirmation gate and waits
        for the graph to complete, returning the final result.

        If confirm=True, runs until the confirmation gate pauses and
        returns the intermediate state with needs_confirm=True.
        The caller should then call confirm() to resume.
        """
        await self._ensure_initialized()

        # Runtime override: kubeconfig/context from CLI args
        if kwargs.get("kubeconfig"):
            settings.kubeconfig_path = kwargs["kubeconfig"]
        if kwargs.get("context"):
            settings.kube_context = kwargs["context"]
        if kwargs.get("cluster_uuid"):
            settings.kubewiz_cluster_uuid = kwargs["cluster_uuid"]
        if kwargs.get("profile"):
            settings.kubewiz_profile = kwargs["profile"]

        task_id = f"task-{uuid.uuid4()}"
        tui_session_id = kwargs.get("tui_session_id", "") or ""

        # Same single-source-of-truth pattern as inject_stream: FaultSpec
        # only, no legacy scattered fields.
        _ts2 = now_iso()
        if kwargs.get("input"):
            spec = FaultSpec.from_cli_nl(input_text=kwargs["input"], kwargs=kwargs)
        else:
            spec = FaultSpec.from_cli_structured(kwargs)
        initial_state = build_inject_initial_state(
            task_id=task_id,
            tui_session_id=tui_session_id,
            fault_spec=spec,
            needs_confirmation=kwargs.get("confirm", False),
            kubeconfig=kwargs.get("kubeconfig", ""),
            kube_context=kwargs.get("context", ""),
            kubewiz_cluster_uuid=kwargs.get("cluster_uuid", ""),
            kubewiz_profile=kwargs.get("profile", ""),
            created_at=_ts2,
            direct=kwargs.get("direct", False) if not kwargs.get("input") else False,
            interaction_mode="cli",
        )

        config = {"configurable": {"thread_id": task_id}, "recursion_limit": settings.recursion_limit}

        # Write initial task state to TaskStore before graph starts
        try:
            from chaos_agent.persistence.task_store import get_task_store
            store = await get_task_store()
            state_for_store = {k: v for k, v in initial_state.items() if k != "task_id"}
            await store.upsert(task_id, **state_for_store)
        except Exception as e:
            logger.warning(f"Failed to write initial state to TaskStore for {task_id}: {e}")

        # Create session for recording (same P0-7-6 handoff logic)
        _handoff2 = None
        try:
            from langchain_core.messages import SystemMessage
            for msg in initial_state.get("messages", []):
                content = getattr(msg, "content", "") or ""
                if isinstance(msg, SystemMessage) and content.startswith("[Intent Clarification Summary]"):
                    _handoff2 = msg
                    break
        except Exception:
            pass
        if self._session_store:
            self._session_store.create_session(
                task_id,
                operation="inject",
                tui_session_id=tui_session_id,
                initial_messages=[_handoff2] if _handoff2 else None,
            )

        # Subscribe to status events and start printer
        status_queue = subscribe(task_id)
        done_event = asyncio.Event()
        printer_task = asyncio.create_task(_status_printer(status_queue, done_event))

        try:
            # Print a notice so the user knows the process is running
            if not settings.is_debug:
                sys.stderr.write("  ⏳ 故障注入进行中，AI 正在分析并规划，请耐心等待...\n")
                sys.stderr.flush()

            # First invoke - will pause at confirmation_gate (or complete if chat)
            result = await self._agents["pipeline"].ainvoke(initial_state, config)

            # If confirmation is NOT required, auto-approve and wait for completion
            # Only resume if the graph is actually paused at confirmation_gate
            if not kwargs.get("confirm", False):
                from langgraph.types import Command

                current_state = await self._agents["pipeline"].aget_state(config)
                # If graph is waiting for human input (at confirmation_gate), resume it
                if current_state and current_state.next:
                    result = await self._agents["pipeline"].ainvoke(
                        Command(resume="approved"), config
                    )

            # Non-injection intent completed via intent_clarification (TUI mode)
            confirmed_intent = result.get("confirmed_intent") if isinstance(result, dict) else ""
            if confirmed_intent in ("chat", "recover"):
                return JSONEnvelope.ok(
                    data={
                        "task_id": task_id,
                        "result": "completed",
                        "confirmed_intent": confirmed_intent,
                    },
                )

            from chaos_agent.agent.operation_result import build_inject_data_from_state
            inject_data = build_inject_data_from_state(
                result if isinstance(result, dict) else {}, task_id,
            )
            return build_inject_envelope(
                inject_data, inject_data["task_state"], inject_data.get("error", ""),
            )

        except Exception as e:
            code, msg = _format_error(e)
            logger.exception(f"Local inject failed for task {task_id}")

            rollback_status = await auto_rollback(self._agents["pipeline"], config)

            from chaos_agent.agent.operation_result import build_inject_status_data_from_state

            return JSONEnvelope.fail(
                code=code,
                message=f"Inject failed: {msg}{rollback_status}",
                data=build_inject_status_data_from_state(
                    initial_state,
                    task_id,
                    result="failed",
                    error=f"internal_error: Inject failed: {msg}{rollback_status}",
                ),
            )
        finally:
            # Finalize session: flush remaining messages from final graph state
            await _finalize_inject_session(
                self._session_store, self._agents["pipeline"], config, task_id,
                kwargs=kwargs,
                is_open_conversation=None,  # blocking inject always finalizes
                error_log_level="warning",
                tui_session_store=self._tui_session_store,
            )
            done_event.set()
            await printer_task
            unsubscribe(task_id, status_queue)
            remove_tracker(task_id)

    async def cleanup(self):
        """Close resources (checkpointer DB connection, TaskStore) to allow clean shutdown."""
        if self._checkpointer_conn is not None:
            try:
                await self._checkpointer_conn.close()
                logger.info("Checkpointer connection closed")
            except Exception as e:
                logger.warning(f"Failed to close checkpointer connection: {e}")
            finally:
                self._checkpointer_conn = None

        # E9 — MCP client disconnect (reap stdio children, close HTTP sessions)
        _mcp = getattr(self, "_mcp_manager", None)
        if _mcp is not None:
            try:
                await _mcp.disconnect_all()
            except Exception as e:
                logger.warning(f"MCP disconnect failed: {e}")
            finally:
                self._mcp_manager = None

        # Close TaskStore backend
        try:
            from chaos_agent.persistence.task_store import reset_task_store
            await reset_task_store()
        except Exception as e:
            logger.warning(f"Failed to reset TaskStore: {e}")

    # ---- resume_stream ----

    async def converse_stream(self, session_id: str, user_message: str, interrupt_callback=None, **kwargs):
        """Dual-graph TUI conversation: Intent Graph → Pipeline Graph.

        Phase 1 (Intent Graph, thread_id=session_id):
          Stream intent_clarification dialogue. On inject intent,
          intent_confirm fires interrupt(). After approval, handoff_summary
          and fault_spec are extracted.

        Phase 2 (Pipeline Graph, thread_id=task_id):
          Only runs when confirmed_intent == "inject". Streams the full
          injection pipeline (agent_loop → safety → execute → verify).

        Chat / recover / unresolved intents end at Phase 1 with
        a conversation_turn event.

        Yields:
            StreamEvent: token, tool_start, tool_end, confirm, result, error, conversation_turn
        """
        await self._ensure_initialized()

        from langchain_core.messages import HumanMessage, SystemMessage
        from langgraph.types import Command

        intent_graph = self._agents["intent"]
        pipeline_graph = self._agents["pipeline"]
        intent_config = {"configurable": {"thread_id": session_id}, "recursion_limit": settings.recursion_limit}

        # Session-level fields for Intent Graph (needed on first turn;
        # subsequent turns carry them via checkpoint merge).
        intent_input = {
            "messages": [HumanMessage(content=user_message)],
            "confirmed_intent": "unset",
        }
        # Merge session-level kwargs (tui_session_id, kubeconfig,
        # needs_confirmation, dry_run, interaction_mode, etc.)
        _session_keys = (
            "tui_session_id", "interaction_mode", "kubeconfig",
            "kube_context", "kubewiz_cluster_uuid", "kubewiz_profile",
            "needs_confirmation", "dry_run",
        )
        for k in _session_keys:
            if k in kwargs:
                intent_input[k] = kwargs[k]

        turn_tokens_seen = False
        pipeline_started = False
        pipeline_task_id = ""

        try:
            # ── Phase 1: Intent Graph ──────────────────────────────
            async for event in intent_graph.astream_events(intent_input, intent_config, version="v2"):
                for stream_evt in parse_stream_events(event):
                    stream_evt.task_id = session_id
                    if stream_evt.type == "token":
                        turn_tokens_seen = True
                    yield stream_evt

            # Handle Intent Graph interrupts (intent_confirm)
            while True:
                cur = await intent_graph.aget_state(intent_config)
                if not (cur and cur.next):
                    break

                interrupt_info = None
                for t in (cur.tasks or []):
                    if hasattr(t, "interrupts") and t.interrupts:
                        interrupt_info = t.interrupts[0].value
                        break
                if not interrupt_info:
                    break

                if interrupt_callback:
                    response = await interrupt_callback(interrupt_info)
                    async for event in intent_graph.astream_events(
                        Command(resume=response), intent_config, version="v2"
                    ):
                        for stream_evt in parse_stream_events(event):
                            stream_evt.task_id = session_id
                            if stream_evt.type == "token":
                                turn_tokens_seen = True
                            yield stream_evt
                else:
                    break

            # Read Intent Graph result
            intent_final = await intent_graph.aget_state(intent_config)
            iv = intent_final.values if intent_final else {}
            confirmed = iv.get("confirmed_intent")

            # ── Phase 2: Pipeline Graph (inject only) ──────────────
            if confirmed == "inject" and iv.get("fault_spec"):
                pipeline_started = True
                task_id = iv.get("task_id", f"task-{uuid.uuid4()}")
                pipeline_task_id = task_id
                handoff = iv.get("handoff_summary", "")
                tui_sid = iv.get("tui_session_id", "") or session_id

                # Bootstrap task session (moved from intent_confirm)
                from chaos_agent.agent.nodes.intent_clarification import bootstrap_task_session
                if task_id:
                    handoff_msg = SystemMessage(content=handoff) if handoff else None
                    bootstrap_task_session(
                        task_id, operation="inject",
                        tui_session_id=tui_sid,
                        handoff_message=handoff_msg,
                    )

                pipeline_config = {"configurable": {"thread_id": task_id}, "recursion_limit": settings.recursion_limit}
                pipeline_input = {
                    "task_id": task_id,
                    "tui_session_id": tui_sid,
                    "operation": "inject",
                    "confirmed_intent": "inject",
                    "fault_spec": iv.get("fault_spec"),
                    "needs_confirmation": iv.get("needs_confirmation", True),
                    "interaction_mode": "tui",
                    "kubeconfig": iv.get("kubeconfig", ""),
                    "kube_context": iv.get("kube_context", ""),
                    "messages": [SystemMessage(content=handoff)] if handoff else [],
                    "safety_status": "pending",
                    "created_at": now_iso(),
                }

                # Stream Pipeline Graph
                async for event in pipeline_graph.astream_events(pipeline_input, pipeline_config, version="v2"):
                    for stream_evt in parse_stream_events(event):
                        stream_evt.task_id = task_id
                        if stream_evt.type == "token":
                            turn_tokens_seen = True
                        yield stream_evt

                # Handle Pipeline interrupts (confirmation_gate)
                while True:
                    cur = await pipeline_graph.aget_state(pipeline_config)
                    if not (cur and cur.next):
                        break
                    interrupt_info = None
                    _interrupt_node = ""
                    for t in (cur.tasks or []):
                        if hasattr(t, "interrupts") and t.interrupts:
                            interrupt_info = t.interrupts[0].value
                            _interrupt_node = getattr(t, "name", "")
                            break
                    if not interrupt_info:
                        break
                    # Auto mode: skip confirmation_gate without user interaction
                    _auto_mode = not iv.get("needs_confirmation", True)
                    if _auto_mode and _interrupt_node == "confirmation_gate":
                        response = "approved"
                    elif interrupt_callback:
                        response = await interrupt_callback(interrupt_info)
                    else:
                        break
                    async for event in pipeline_graph.astream_events(
                        Command(resume=response), pipeline_config, version="v2"
                    ):
                        for stream_evt in parse_stream_events(event):
                            stream_evt.task_id = task_id
                            if stream_evt.type == "token":
                                turn_tokens_seen = True
                            yield stream_evt

                # Build and yield result from Pipeline Graph
                pfinal = await pipeline_graph.aget_state(pipeline_config)
                pv = pfinal.values if pfinal else {}

                # Dry-run (plan_builder path): no result card.
                # Emit conversation_turn with pipeline task_id so the TUI's
                # _conversation_thread_id points to the Pipeline checkpoint
                # (needed by lift_dry_run_and_run / is_dry_run_thread).
                if pv.get("dry_run"):
                    yield StreamEvent(type="conversation_turn", content="", task_id=task_id)
                    return

                blade_uid = pv.get("blade_uid", "")

                if blade_uid:
                    from chaos_agent.models.schemas import build_inject_envelope
                    from chaos_agent.agent.operation_result import build_inject_data_from_state

                    _data = build_inject_data_from_state(pv, task_id)
                    yield StreamEvent(
                        type="result",
                        content=json.dumps(build_inject_envelope(
                            _data, _data["task_state"], _data.get("error", ""),
                        ), ensure_ascii=False),
                        task_id=task_id,
                    )
                else:
                    # Pipeline ran but no blade_uid (error / rejection)
                    from chaos_agent.agent.operation_outcome import read_operation_outcome
                    error_msg = read_operation_outcome(pv).error
                    if error_msg or pv.get("safety_status") == "rejected":
                        yield StreamEvent(
                            type="error",
                            content=error_msg or pv.get("safety_reason") or "Request rejected",
                            task_id=task_id,
                        )
                    # Use session_id (not pipeline task_id) so the TUI's
                    # _conversation_thread_id stays = session_id for the
                    # next converse_stream call.
                    yield StreamEvent(type="conversation_turn", content="", task_id=session_id)

                # Write task summary back to Intent Graph + session file
                try:
                    summary = build_task_summary_text(pv, task_id)
                    await write_operation_summary(
                        summary,
                        intent_graph=intent_graph,
                        thread_id=session_id,
                        state_update={"pipeline_task_id": task_id},
                        tui_session_id=session_id,
                        tui_session_store=self._tui_session_store,
                        recursion_limit=settings.recursion_limit,
                    )
                except Exception:
                    logger.debug("Failed to write task summary to Intent Graph", exc_info=True)

            else:
                # Non-inject: chat / recover / unresolved
                if not turn_tokens_seen:
                    synthetic = _extract_visible_reply(iv)
                    if synthetic:
                        yield StreamEvent(type="token", content=synthetic, task_id=session_id)
                yield StreamEvent(type="conversation_turn", content="", task_id=session_id)

        except Exception as e:
            logger.exception(f"converse_stream failed for session {session_id}")
            yield StreamEvent(type="error", content=f"Conversation failed: {e}", task_id=session_id)
        finally:
            if pipeline_started and pipeline_task_id:
                try:
                    _pfinal = await pipeline_graph.aget_state(
                        {"configurable": {"thread_id": pipeline_task_id}}
                    )
                    _pvals = _pfinal.values if _pfinal else {}
                    await _finalize_inject_session(
                        self._session_store, pipeline_graph,
                        {"configurable": {"thread_id": pipeline_task_id}},
                        pipeline_task_id,
                        is_open_conversation=False,
                        error_log_level="debug",
                        precomputed_values=_pvals,
                        tui_session_store=self._tui_session_store,
                    )
                except Exception:
                    logger.debug("Pipeline session finalize failed", exc_info=True)

    # ---- resume_stream ----

    async def resume_stream(self, task_id: str, resume_value=None, interrupt_callback=None):
        """Resume a paused graph from its checkpoint.

        Used when TUI crashes while waiting for user input.
        The checkpoint is preserved in SQLite, so this method
        restores execution from where it left off.

        Args:
            task_id: The task ID to resume.
            resume_value: Value to pass to Command(resume=...).
                         If None, resumes without a value (continues execution).
            interrupt_callback: Optional async callback for handling subsequent interrupts.

        Yields:
            StreamEvent: Same event types as inject_stream.
        """
        await self._ensure_initialized()

        config = {"configurable": {"thread_id": task_id}, "recursion_limit": settings.recursion_limit}
        graph = self._agents["pipeline"]

        current_state = await graph.aget_state(config)
        if not current_state or not current_state.next:
            yield StreamEvent(type="error", content=f"Task {task_id} has no paused state", task_id=task_id)
            return

        # Subscribe to status events
        status_queue = subscribe(task_id)
        done_event = asyncio.Event()
        printer_task = asyncio.create_task(_status_printer(status_queue, done_event))

        try:
            # Initial resume from the provided resume_value
            if resume_value is not None:
                from langgraph.types import Command

                async for event in graph.astream_events(
                    Command(resume=resume_value), config, version="v2"
                ):
                    for stream_evt in parse_stream_events(event):
                        stream_evt.task_id = task_id
                        yield stream_evt
            else:
                async for event in graph.astream_events(None, config, version="v2"):
                    for stream_evt in parse_stream_events(event):
                        stream_evt.task_id = task_id
                        yield stream_evt

            # Loop to handle subsequent interrupts after initial resume
            while interrupt_callback:
                current_state = await graph.aget_state(config)
                if not (current_state and current_state.next):
                    break  # Graph completed

                interrupt_info = None
                for task in (current_state.tasks or []):
                    if hasattr(task, 'interrupts') and task.interrupts:
                        interrupt_info = task.interrupts[0].value
                        break

                if not interrupt_info:
                    break  # Paused but no interrupt info — unexpected state

                response = await interrupt_callback(interrupt_info)
                from langgraph.types import Command

                async for event in graph.astream_events(
                    Command(resume=response), config, version="v2"
                ):
                    for stream_evt in parse_stream_events(event):
                        stream_evt.task_id = task_id
                        yield stream_evt

        except Exception as e:
            logger.exception(f"Resume stream failed for task {task_id}")
            yield StreamEvent(type="error", content=f"Resume failed: {e}", task_id=task_id)
        finally:
            done_event.set()
            unsubscribe(task_id)
            try:
                printer_task.cancel()
            except Exception:
                pass
            remove_tracker(task_id)

    # ---- lift_dry_run_and_run ----

    async def lift_dry_run_and_run(
        self,
        thread_id: str,
        interrupt_callback=None,
    ):
        """Lift the dry_run flag on a Dry-Run thread and continue the pipeline.

        Used by TUI `/run` (no args) after one or more `/plan` turns. The
        thread's checkpoint already holds the planning artifacts (target,
        params, plan_summary, intent), and the previous Dry-Run invocation
        terminated cleanly at confirmation_gate → END.

        Rather than replaying intent_clarification → agent_loop → safety_check
        from scratch, we use ``aupdate_state(values, as_node="confirmation_gate")``
        to write the lift values as if confirmation_gate had just emitted them.
        Streaming with no input then continues from confirmation_gate's outgoing
        conditional edge (route_after_confirmation), which now sees dry_run=False
        and routes to baseline_capture → execute → verify.
        """
        await self._ensure_initialized()

        from langgraph.types import Command

        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": settings.recursion_limit}
        graph = self._agents["pipeline"]

        snapshot = await graph.aget_state(config)
        if not snapshot or not snapshot.values:
            yield StreamEvent(
                type="error",
                content=f"Thread {thread_id} not found",
                task_id=thread_id,
            )
            return

        if not snapshot.values.get("dry_run"):
            yield StreamEvent(
                type="error",
                content="当前会话不在 Dry-Run 状态，无法直接落地。请使用 /run <描述> 起新任务。",
                task_id=thread_id,
            )
            return

        # Re-enter from confirmation_gate's outgoing edge: write the lift
        # values "as if" confirmation_gate had just produced them. The /run
        # invocation itself counts as the user confirmation, so we also clear
        # any safety_status=confirm_required overlay state.
        await graph.aupdate_state(
            config,
            {
                "dry_run": False,
                "needs_confirmation": False,
                "safety_status": "safe",
                "error": None,
                "failure_reason": None,
                "replan_requested": False,
                "replan_count": 0,
                "replan_context": None,
            },
            as_node="confirmation_gate",
        )

        status_queue = subscribe(thread_id)
        done_event = asyncio.Event()

        try:
            async for event in graph.astream_events(None, config, version="v2"):
                for stream_evt in parse_stream_events(event):
                    stream_evt.task_id = thread_id
                    yield stream_evt

            # Drive any remaining interrupts (e.g., confirmation_gate when the
            # user is in CONFIRM permission mode).
            while True:
                cur = await graph.aget_state(config)
                if not (cur and cur.next):
                    break

                info = None
                for t in (cur.tasks or []):
                    if hasattr(t, "interrupts") and t.interrupts:
                        info = t.interrupts[0].value
                        break

                if not info or not interrupt_callback:
                    break

                response = await interrupt_callback(info)
                async for event in graph.astream_events(
                    Command(resume=response), config, version="v2"
                ):
                    for stream_evt in parse_stream_events(event):
                        stream_evt.task_id = thread_id
                        yield stream_evt

            # Yield a structured result if the pipeline produced a blade_uid.
            final_state = await graph.aget_state(config)
            if final_state and final_state.values:
                values = final_state.values
                blade_uid = values.get("blade_uid", "")
                if blade_uid:
                    from chaos_agent.models.schemas import build_inject_envelope
                    from chaos_agent.agent.operation_result import build_inject_data_from_state

                    _data = build_inject_data_from_state(values, thread_id)
                    yield StreamEvent(
                        type="result",
                        content=json.dumps(build_inject_envelope(
                            _data, _data["task_state"], _data.get("error", ""),
                        ), ensure_ascii=False),
                        task_id=thread_id,
                    )

        except Exception as e:
            logger.exception(f"lift_dry_run_and_run failed for {thread_id}")
            yield StreamEvent(
                type="error",
                content=f"Dry-Run 落地失败: {e}",
                task_id=thread_id,
            )
        finally:
            done_event.set()
            unsubscribe(thread_id, status_queue)

    # ---- list_interrupted_tasks ----

    async def list_interrupted_tasks(self) -> list[dict]:
        """Find all tasks paused at interrupt points (waiting for user input).

        Used by TUI on startup to discover tasks that were interrupted
        in previous sessions (crash recovery).

        Returns:
            List of dicts with task_id, next_nodes, and interrupt_info.
        """
        await self._ensure_initialized()

        try:
            from chaos_agent.persistence.task_store import get_task_store
            store = await get_task_store()
            active_tasks = await store.query_active()
        except Exception as e:
            logger.warning(f"Failed to query active tasks: {e}")
            return []

        graph = self._agents["pipeline"]
        interrupted = []

        for task in active_tasks:
            task_id = task.get("task_id", "")
            if not task_id:
                continue

            try:
                config = {"configurable": {"thread_id": task_id}, "recursion_limit": settings.recursion_limit}
                state = await graph.aget_state(config)
                if not state or not state.next:
                    continue

                interrupt_info = None
                for t in (state.tasks or []):
                    if hasattr(t, 'interrupts') and t.interrupts:
                        interrupt_info = t.interrupts[0].value
                        break

                interrupted.append({
                    "task_id": task_id,
                    "next_nodes": list(state.next),
                    "interrupt_info": interrupt_info,
                })
            except Exception as e:
                logger.debug(f"Failed to check state for task {task_id}: {e}")
                continue

        return interrupted

    # ---- recover ----

    async def recover(self, task_id: str, **kwargs) -> dict:
        """Recover a fault locally.

        Uses the recover graph (which includes two-layer verification)
        instead of calling blade_destroy directly.

        Args:
            task_id: The inject task_id (== langgraph thread_id used to locate
                the inject checkpoint). This is the drill-level identifier
                returned to callers in the response envelope.
        """
        await self._ensure_initialized()

        inject_task_id = task_id
        # Recover gets its own task record file, cross-referenced back to inject
        # via parent_task_id. The langgraph thread_id stays = inject_task_id so
        # the recover graph can read inject's checkpoint.
        record_task_id = f"task-{uuid.uuid4()}"
        config = {"configurable": {"thread_id": inject_task_id}, "recursion_limit": settings.recursion_limit}

        # Subscribe to status events emitted by recover nodes (keyed by state.task_id)
        status_queue = subscribe(record_task_id)
        done_event = asyncio.Event()
        printer_task = asyncio.create_task(_status_printer(status_queue, done_event))

        # Pre-declare in case fallback path is taken or an early exception fires.
        blade_uid = ""
        target: dict = {}
        state_values: dict = {}

        try:
            # Try to fetch LangGraph checkpoint as supplemental live context.
            current_state = await self._agents["pipeline"].aget_state(config)
            checkpoint_values = current_state.values if current_state and current_state.values else {}

            from chaos_agent.agent.task_snapshot import resolve_recover_initial_state

            resolution = await resolve_recover_initial_state(
                inject_task_id,
                record_task_id=record_task_id,
                agents=self._agents,
                checkpoint_values=checkpoint_values,
                kubeconfig_override=kwargs.get("kubeconfig") or None,
            )
            if resolution is None:
                return JSONEnvelope.fail(
                    code=ResponseCode.TASK_NOT_FOUND,
                    message=f"Task not recoverable: {inject_task_id}",
                )

            initial_state = resolution.initial_state
            state_values = resolution.source_values
            blade_uid = initial_state.get("blade_uid", "") or ""
            skill_name = read_active_skill_name(initial_state)
            inject_tui_session_id = initial_state.get("tui_session_id", "") or ""

            # Write recover initial state to TaskStore (keyed by inject_task_id —
            # the drill-level identifier; recover outcome updates the same row).
            try:
                from chaos_agent.persistence.task_store import get_task_store
                store = await get_task_store()
                await store.upsert(inject_task_id, operation="recover", blade_uid=blade_uid, skill_name=skill_name)
            except Exception:
                logger.warning(f"Failed to write recover state to TaskStore for {inject_task_id}")

            # Create a separate session file for the recover record. Inject
            # messages are passed as baseline so that messages inherited from
            # the inject checkpoint are not re-persisted in the recover file.
            if self._session_store:
                inject_messages = state_values.get("messages", [])
                self._session_store.create_session(
                    record_task_id,
                    operation="recover",
                    tui_session_id=inject_tui_session_id,
                    parent_task_id=inject_task_id,
                    baseline_messages=inject_messages,
                )

            # Execute recover graph (includes two-layer verification)
            if not settings.is_debug:
                sys.stderr.write("  ⏳ 故障恢复进行中，AI 正在执行恢复并验证，请耐心等待...\n")
                sys.stderr.flush()
            result = await self._agents["recover"].ainvoke(initial_state, config)

            from chaos_agent.agent.operation_result import (
                build_recover_cli_data_from_state,
            )

            recover_data = build_recover_cli_data_from_state(
                result if isinstance(result, dict) else {},
                inject_task_id,
                state_values,
            )
            if recover_data.get("result") == "failed":
                error_msg = recover_data.get("error") or "Recovery verification failed"
                recover_fail_data = {**recover_data, "error": error_msg}
                return JSONEnvelope.fail(
                    code=ResponseCode.NO_BLADE_UID,
                    message=error_msg,
                    data=recover_fail_data,
                )

            return JSONEnvelope.ok(data=recover_data)

        except Exception as e:
            code, msg = _format_error(e)
            logger.exception(f"Local recover failed for task {inject_task_id}")
            from chaos_agent.agent.operation_result import (
                build_recover_cli_failure_data_from_state,
            )

            return JSONEnvelope.fail(
                code=code,
                message=f"Recovery failed: {msg}",
                data=build_recover_cli_failure_data_from_state(
                    inject_task_id,
                    state_values,
                    blade_uid=blade_uid or "",
                    error=f"internal_error: Recovery failed: {msg}",
                ),
            )
        finally:
            # Finalize session: flush remaining messages from final graph state
            if self._session_store:
                try:
                    remaining = []
                    merged_error_fin = ""
                    values_fin = {}
                    try:
                        final_graph_state = await self._agents["recover"].aget_state(config)
                        if final_graph_state and final_graph_state.values:
                            from chaos_agent.agent.operation_outcome import read_operation_outcome

                            values_fin = final_graph_state.values
                            remaining = values_fin.get("messages", [])
                            merged_error_fin = read_operation_outcome(values_fin).error
                    except Exception:
                        pass
                    from chaos_agent.agent.state import infer_task_state

                    inferred_state = infer_task_state(values_fin) if values_fin else "recovered"
                    from chaos_agent.agent.operation_result import (
                        build_recover_cli_data_from_state,
                    )

                    result_data = build_recover_cli_data_from_state(
                        values_fin,
                        inject_task_id,
                        state_values,
                    )
                    result_data["result"] = inferred_state
                    result_data["error"] = merged_error_fin
                    self._session_store.finalize_session(
                        record_task_id,
                        remaining_messages=remaining,
                        result_summary=build_inject_envelope(
                            result_data,
                            inferred_state,
                            merged_error_fin,
                        ),
                        status="completed",
                    )
                except Exception:
                    logger.warning(f"Failed to finalize recover session {record_task_id}")
            done_event.set()
            await printer_task
            unsubscribe(record_task_id, status_queue)
            remove_tracker(record_task_id)

    # ---- metric ----

    async def metric(self, task_id: str = "") -> dict:
        """Query task metrics and status from TaskStore. No agent initialization needed."""
        from chaos_agent.persistence.task_store import get_task_store

        store = await get_task_store()

        if task_id:
            data = await store.get_metric(task_id)
            if data:
                return JSONEnvelope.ok(data=data)
            return JSONEnvelope.fail(code=ResponseCode.TASK_NOT_FOUND, message=f"Task not found: {task_id}")

        # No task_id → return ALL tasks from TaskStore
        all_metrics = await store.get_all_metrics(limit=200)

        return JSONEnvelope.ok(data=all_metrics)

    # ---- list_skills ----

    async def list_skills(self, **params) -> dict:
        """List supported fault capabilities with use-case examples.

        Equivalent to GET /api/v1/skills. Uses LLM to analyze each skill's
        content and generate injectable fault scenarios with example commands.
        Results are cached to disk; use no_cache=True to force regeneration.
        """
        await self._ensure_initialized()

        no_cache = params.get("no_cache", False)
        categories_dict = defaultdict(
            lambda: {"category": "", "description": "", "faults": []}
        )

        # Create a lightweight LLM instance for catalog generation
        from chaos_agent.agent.factory import make_llm
        llm = make_llm(temperature=0.3, max_retries=2, read_timeout=60)

        for name, meta in self._registry.metadata.items():
            if params.get("category") and meta.category != params["category"]:
                continue
            if params.get("target_type") and meta.target != params["target_type"]:
                continue
            if meta.skill_type != SKILL_TYPE_FAULT_INJECTION:
                continue  # 非故障注入类 skill 不参与 list 用例生成

            cat = meta.category or "other"

            # Read skill content (SKILL.md body)
            try:
                skill_content = self._registry.activate(name)
            except Exception:
                skill_content = meta.description or ""

            # Get skill directory for fingerprint computation
            skill_dir = self._registry.get_skill_dir(name)

            # Generate use-case catalog via LLM (cached)
            use_cases = await generate_skill_catalog(
                skill_name=name,
                skill_content=skill_content,
                skill_dir=skill_dir,
                llm=llm,
                work_dir=settings.working_dir,
                no_cache=no_cache,
            )

            if use_cases:
                for uc in use_cases:
                    uc_cat = uc.get("category") or cat
                    categories_dict[uc_cat]["category"] = uc_cat
                    categories_dict[uc_cat]["description"] = f"{uc_cat} 故障注入用例"
                    categories_dict[uc_cat]["faults"].append({
                        "fault_type": extract_fault_type(uc_cat),
                        "use_case_name": uc["use_case_name"],
                        "fault_symptom": uc["fault_symptom"],
                        "resource_path": uc["resource_path"],
                        "example_cmd": uc["example_cmd"],
                        "example_cmd_direct": uc.get("example_cmd_direct", ""),
                    })
            else:
                # Fallback — skill has no extractable scenarios
                categories_dict[cat]["category"] = cat
                categories_dict[cat]["description"] = f"{cat} related faults"
                scope = infer_scope(cat)
                desc = meta.description.split(chr(46))[0] if meta.description else name
                if scope == "node":
                    nl_cmd = (
                        f'blade-ai inject -i "帮我注入{desc}故障，'
                        f'目标为<node-name>，'
                        f'kubeconfig路径为<kubeconfig>"'
                    )
                else:
                    nl_cmd = (
                        f'blade-ai inject -i "帮我注入{desc}故障，'
                        f'命名空间为<namespace>，目标为<name>，'
                        f'kubeconfig路径为<kubeconfig>"'
                    )
                blade_params = infer_blade_params(cat, scope=scope)
                categories_dict[cat]["faults"].append({
                    "fault_type": extract_fault_type(cat),
                    "name": name.replace("-", " ").title(),
                    "description": (
                        meta.description.split(".")[0] if meta.description else ""
                    ),
                    "example_cmd": nl_cmd,
                    "example_cmd_direct": build_direct_cmd(blade_params) if blade_params else "",
                })

        categories = list(categories_dict.values())
        total_use_cases = sum(len(c["faults"]) for c in categories)

        return JSONEnvelope.ok(
            data={
                "total": total_use_cases,
                "categories": categories,
            },
            
        )

    # ---- confirm ----

    async def confirm(self, task_id: str, action: str, reason: str = "") -> dict:
        """Confirm or reject a pending task. Equivalent to POST /api/v1/confirm/{task_id}.

        In local mode, this also waits for the graph to complete after resuming,
        so the returned task_state reflects the final state.
        """
        await self._ensure_initialized()

        if action not in ("approve", "reject"):
            return JSONEnvelope.fail(code=ResponseCode.INVALID_ACTION, message="Invalid action, must be 'approve' or 'reject'")

        config = {"configurable": {"thread_id": task_id}, "recursion_limit": settings.recursion_limit}

        # Subscribe for status during confirm flow
        status_queue = subscribe(task_id)
        done_event = asyncio.Event()
        printer_task = asyncio.create_task(_status_printer(status_queue, done_event))

        try:
            from langgraph.types import Command

            resume_value = "approved" if action == "approve" else "rejected"
            await self._agents["pipeline"].ainvoke(Command(resume=resume_value), config)

            new_state = "injecting" if action == "approve" else "cancelled"

            return JSONEnvelope.ok(
                data={
                    "task_id": task_id,
                    "action": action,
                    "reason": reason,
                    "confirmed_at": now_iso(),
                },

            )

        except Exception as e:
            code, msg = _format_error(e)
            logger.exception(f"Local confirm failed for task {task_id}")
            return JSONEnvelope.fail(code=code, message=f"Task not found or confirm failed: {msg}")
        finally:
            done_event.set()
            await printer_task
            unsubscribe(task_id, status_queue)
            remove_tracker(task_id)

    # ---- version ----

    async def version(self) -> dict:
        """Show version information. Equivalent to GET /api/v1/version.

        ``blade-ai version`` is a metadata-only command — it shouldn't
        require LLM credentials, a reachable model endpoint, or the
        full graph wiring. The previous implementation called
        ``_ensure_initialized()``, which transitively constructs the
        LLM client (via ``create_agent`` → ``make_llm``); on a fresh
        machine without ``OPENAI_API_KEY`` / ``DASHSCOPE_API_KEY`` set,
        the OpenAI SDK raises ``OpenAIError: Missing credentials`` and
        the user can't even check what version they have installed.
        Worse, partial init left aiosqlite worker threads dangling so
        the process hung after the traceback.
        Init only what the response needs: the SkillRegistry, so we
        can count ``supported_fault_count``. No LLM, no checkpointer,
        no prerequisites.
        """
        if self._registry is None:
            self._registry = SkillRegistry()
            self._registry.load_from_directory(get_skills_dir())
        return JSONEnvelope.ok(
            data={
                "version": __version__,
                "supported_fault_count": len(self._registry),
            },
        )
