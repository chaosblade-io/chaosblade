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
from chaos_agent.agent.streaming import StreamEvent, parse_stream_event
from chaos_agent.config.settings import settings
from chaos_agent.models.schemas import JSONEnvelope, ResponseCode, build_inject_envelope
from chaos_agent.observability.status_tracker import (
    subscribe,
    unsubscribe,
    remove_tracker,
    StatusEvent,
    StatusPhase,
    StatusCategory,
)
from chaos_agent.skills.catalog_generator import (
    generate_skill_catalog,
    infer_scope,
    infer_blade_params,
    build_direct_cmd,
)
from chaos_agent.skills.loader import get_skills_dir
from chaos_agent.skills.prerequisites import PrerequisitesChecker
from chaos_agent.skills.registry import SkillRegistry
from chaos_agent.utils.fault_type import extract_fault_type
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


def _extract_visible_reply(values: dict) -> str:
    """Pick a user-visible reply from the latest AIMessage in graph state.

    Used to recover from LLM backends that emit the answer only into
    reasoning_content during streaming (e.g. qwen enable_thinking),
    leaving the user without any token events for this turn.
    """
    if not isinstance(values, dict):
        return ""
    messages = values.get("messages") or []
    for msg in reversed(messages):
        msg_type = getattr(msg, "type", "")
        if msg_type != "ai":
            continue
        content = getattr(msg, "content", "") or ""
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text", ""))
            content = "".join(parts)
        if isinstance(content, str) and content.strip():
            return content
    return ""


def _format_error(e: Exception) -> tuple[int, str]:
    """Format an exception into (error_code, message) with type info.

    - ChaosAgentError subclasses: use their built-in error_code
    - Other exceptions: code 4001 with type name prefix for debuggability
    """
    from chaos_agent.errors import ChaosAgentError

    if isinstance(e, ChaosAgentError):
        return e.error_code, f"{type(e).__name__}: {e}"
    return 4001, f"{type(e).__name__}: {e}"


async def _finalize_inject_session(
    session_store,
    graph_or_agent,
    config,
    session_id: str,
    kwargs: dict | None = None,
    is_open_conversation: bool | None = None,
    error_log_level: str = "warning",
    precomputed_values: dict | None = None,
    tui_session_store=None,
) -> None:
    """Finalize an inject-type session by reading final graph state and
    persisting the result envelope.

    Shared across ``inject_stream``, ``inject``, and ``converse_stream``
    to eliminate ~70 lines of duplicated finalization logic.

    Parameters
    ----------
    session_store : SessionStore
        The active session store (``self._session_store``).
    graph_or_agent : CompiledGraph | dict
        Object with ``aget_state(config)`` method — either the compiled
        graph passed to streaming methods, or ``self._agents["pipeline"]``
        for the blocking ``inject()`` method.  Ignored when
        ``precomputed_values`` is provided.
    config : RunnableConfig
        LangGraph config dict containing ``thread_id`` and ``configurable``.
        Ignored when ``precomputed_values`` is provided.
    session_id : str
        Task/thread identifier used as the session key.
    kwargs : dict | None
        Original inject kwargs — used for fault_type priority-1 lookup
        (``scope/target/action`` keys).  ``None`` skips this priority level.
    is_open_conversation : bool | None
        If True: mid-conversation turn — append messages but keep session
        alive.  If False: final turn — call ``finalize_session``.
        If None: always finalize (used by blocking ``inject()``).
    error_log_level : str
        Log level for the outer catch-all exception: ``"warning"`` for
        inject/inject_stream, ``"debug"`` for converse_stream.
    precomputed_values : dict | None
        Final graph-state ``values`` dict, if already fetched by the
        caller (e.g. for ``is_open_conversation`` computation).
        When provided, the internal ``aget_state`` call is skipped,
        avoiding a redundant checkpoint read.
    tui_session_store : TuiSessionStore | None
        Caller's ``self._tui_session_store``. Needed only on the
        ``is_open_conversation=True`` path so mid-conversation intent
        dialogue is routed to the TUI session file instead of the task
        session file. ``None`` (default) falls back to ``session_store``.
        This is a parameter rather than a free ``self`` reference
        because ``_finalize_inject_session`` is a module-level function,
        not a method.
    """
    if not session_store:
        return

    from chaos_agent.agent.state import infer_task_state
    from chaos_agent.memory.session_store import build_verification_simple

    try:
        remaining = []
        verification = None
        blade_uid_fin = ""
        target_fin = None
        skill_name_fin = ""
        error_fin = ""
        failure_reason_fin = ""
        blade_params_fin = {}
        values_fin = {}

        try:
            if precomputed_values:
                values_fin = precomputed_values
                remaining = values_fin.get("messages", [])
                verification = values_fin.get("verification")
                blade_uid_fin = values_fin.get("blade_uid", "")
                target_fin = values_fin.get("target")
                skill_name_fin = values_fin.get("skill_name", "")
                error_fin = values_fin.get("error") or ""
                failure_reason_fin = values_fin.get("failure_reason") or ""
                blade_params_fin = values_fin.get("params") or {}
            else:
                final_graph_state = await graph_or_agent.aget_state(config)
                if final_graph_state and final_graph_state.values:
                    values_fin = final_graph_state.values
                    remaining = values_fin.get("messages", [])
                    verification = values_fin.get("verification")
                    blade_uid_fin = values_fin.get("blade_uid", "")
                    target_fin = values_fin.get("target")
                    skill_name_fin = values_fin.get("skill_name", "")
                    error_fin = values_fin.get("error") or ""
                    failure_reason_fin = values_fin.get("failure_reason") or ""
                    blade_params_fin = values_fin.get("params") or {}
        except Exception:
            pass

        # Open-conversation: append dialogue messages to session file
        # (intent clarification phase), not task file.
        if is_open_conversation is True:
            try:
                # Route intent dialogue to TUI session store. Note this
                # is a module-level function, so the caller must pass
                # ``tui_session_store`` explicitly — using ``self``
                # here would NameError at runtime.
                tui_ses_id = values_fin.get("tui_session_id", "") if values_fin else ""
                if tui_session_store and tui_ses_id:
                    tui_session_store.append_dialogue(tui_ses_id, remaining)
                else:
                    # Fallback: no tui_session_store available (non-TUI mode)
                    session_store.append_messages(session_id, remaining)
            except Exception:
                logger.debug(
                    f"Mid-conversation append failed for {session_id}",
                    exc_info=True,
                )
            return

        # Final turn: infer state, build envelope, finalize session.
        inferred_state = infer_task_state(values_fin) if values_fin else "unknown"
        if inferred_state == "injecting":
            inferred_state = "injected" if blade_uid_fin else "failed"

        # Compute fault_type (3-priority chain).
        fault_type_fin = ""
        # Priority 1: explicit kwargs (scope/target/action)
        if kwargs and kwargs.get("scope") and kwargs.get("target") and kwargs.get("action"):
            fault_type_fin = f"{kwargs['scope']}-{kwargs['target']}-{kwargs['action']}"
        # Priority 2: blade_params (scope/target/action from ChaosBlade command)
        if not fault_type_fin and blade_params_fin:
            _s = blade_params_fin.get("scope", "")
            _a = blade_params_fin.get("action", "")
            _t = blade_params_fin.get("target", "")
            if _s and _t and _a:
                fault_type_fin = f"{_s}-{_t}-{_a}"
        # Priority 3: skill_name fallback
        if not fault_type_fin:
            fault_type_fin = skill_name_fin

        merged_error_fin = failure_reason_fin or error_fin or ""
        names_fin = target_fin.get("names", []) if target_fin else []
        ns_fin = target_fin.get("namespace", "") if target_fin else ""
        ns_fin = ns_fin or (kwargs.get("namespace", "") if kwargs else "")
        ns_fin = ns_fin or blade_params_fin.get("namespace", "")

        session_store.finalize_session(
            session_id,
            remaining_messages=remaining,
            result_summary=build_inject_envelope({
                "task_id": session_id,
                "result": inferred_state,
                "fault_type": fault_type_fin,
                "blade_uid": blade_uid_fin,
                "targets": [{"name": n, "namespace": ns_fin} for n in names_fin],
                "verification": build_verification_simple(verification),
                "error": merged_error_fin,
            }, inferred_state, merged_error_fin),
            status="completed",
        )
    except Exception:
        _log = logger.warning if error_log_level == "warning" else logger.debug
        _log(f"Failed to finalize session for {session_id}", exc_info=True)

# ANSI color codes for CLI status output
_PHASE_COLORS = {
    StatusPhase.STARTED: "\033[36m",     # cyan
    StatusPhase.RUNNING: "\033[33m",     # yellow
    StatusPhase.COMPLETED: "\033[32m",   # green
    StatusPhase.FAILED: "\033[31m",      # red
}
_PHASE_ICONS = {
    StatusPhase.STARTED: "►",
    StatusPhase.RUNNING: "●",
    StatusPhase.COMPLETED: "✓",
    StatusPhase.FAILED: "✗",
}
_RESET = "\033[0m"


def format_status_event(event: StatusEvent) -> str:
    """Format a status event for CLI display.

    Visibility rules:
    - Non-debug mode: only show SYSTEM events (e.g., final results).
    - Debug mode: show all events (NODE, TOOL, LLM, SYSTEM) including
      tool output previews and LLM reasoning summaries.
    """
    # Filter: suppress debug events in non-debug mode
    if event.detail.get("debug") and not settings.is_debug:
        return ""

    # Filter: suppress NODE and TOOL category events in non-debug mode
    if not settings.is_debug and event.category in (StatusCategory.NODE, StatusCategory.TOOL):
        return ""

    color = _PHASE_COLORS.get(event.phase, "")
    icon = _PHASE_ICONS.get(event.phase, "\u00b7")
    duration = f" ({event.duration_ms:.0f}ms)" if event.duration_ms > 0 else ""

    # Multi-line messages (e.g., LLM debug summaries) get indented continuation lines
    if "\n" in event.message:
        header, rest = event.message.split("\n", 1)
        # Indent continuation lines to align with the header
        indented_rest = rest.replace("\n", "\n      ")
        line = f"  {color}{icon} [{event.source}] {header}{duration}{_RESET}\n      {indented_rest}"
    else:
        line = f"  {color}{icon} [{event.source}] {event.message}{duration}{_RESET}"

    # Show tool output preview (stdout_preview) when present (debug mode only, already filtered above)
    stdout_preview = event.detail.get("stdout_preview", "")
    if stdout_preview:
        # Truncate to 200 chars for display, indent each line
        preview_text = stdout_preview[:200]
        if len(stdout_preview) > 200:
            preview_text += "..."
        indented_preview = preview_text.replace("\n", "\n      ")
        line += f"\n      → output: {indented_preview}"

    # In debug mode, show detail dict for debug events (indented)
    if settings.is_debug and event.detail.get("debug") and event.detail:
        import json
        # Skip redundant fields already shown in the message
        detail = {k: v for k, v in event.detail.items() if k not in ("debug", "tool_calls", "stdout_preview")}
        if detail:
            detail_str = json.dumps(detail, ensure_ascii=False)
            line += f"\n    → detail: {detail_str}"

    return line


async def _status_printer(queue: asyncio.Queue[StatusEvent], done_event: asyncio.Event):
    """Background task that reads status events and prints them to stderr."""
    while not done_event.is_set():
        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
            import sys
            formatted = format_status_event(event)
            if formatted:  # skip empty (filtered debug events)
                sys.stderr.write(formatted + "\n")
                sys.stderr.flush()
        except asyncio.TimeoutError:
            continue
        except Exception:
            break

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
        target_names = list(spec.names)
        initial_state = {
            "task_id": task_id,
            "tui_session_id": tui_session_id,
            "operation": "inject",
            "fault_spec": spec.to_dict(),
            "needs_confirmation": kwargs.get("confirm", False),
            "safety_status": "pending",
            "kubeconfig": kwargs.get("kubeconfig", ""),
            "kube_context": kwargs.get("context", ""),
            "created_at": _ts,
            "direct": kwargs.get("direct", False) if not kwargs.get("input") else False,
            "interaction_mode": _interaction_mode,
            "dry_run": _dry_run,
        }

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
                stream_evt = parse_stream_event(event)
                if stream_evt is not None:
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
                        stream_evt = parse_stream_event(event)
                        if stream_evt is not None:
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
                            stream_evt = parse_stream_event(event)
                            if stream_evt is not None:
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
                            stream_evt = parse_stream_event(event)
                            if stream_evt is not None:
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
            if final_state and final_state.values:
                values = final_state.values
                skill_name = values.get("skill_name", "")
                blade_uid = values.get("blade_uid", "")

                # Multi-invocation model: if TUI mode and no blade_uid,
                # the LLM response was already streamed as tokens. Signal
                # conversation_turn so the TUI enters conversation mode.
                # EXCEPT when there's an error or rejection — those still
                # need to be reported to the user via result/error events.
                if _interaction_mode == "tui" and not blade_uid:
                    error_msg = values.get("failure_reason") or values.get("error") or ""
                    safety_rejected = values.get("safety_status") == "rejected"

                    if error_msg or safety_rejected:
                        # Pipeline ran but failed/rejected — report to user
                        yield StreamEvent(
                            type="error",
                            content=error_msg or values.get("safety_reason") or "Request rejected",
                            task_id=task_id,
                        )
                    # Synthesize a token from the latest AIMessage if the
                    # streaming layer never emitted one (e.g. qwen put the
                    # answer into reasoning_content only).
                    if not turn_tokens_seen:
                        synthetic = _extract_visible_reply(values)
                        if synthetic:
                            yield StreamEvent(
                                type="token",
                                content=synthetic,
                                task_id=task_id,
                            )
                    yield StreamEvent(
                        type="conversation_turn",
                        content="",
                        task_id=task_id,
                    )
                    return

                # Fault injection result
                from chaos_agent.agent.fault_spec import (
                    legacy_params_dict, legacy_target_dict, read_fault_spec,
                )
                safety_status = values.get("safety_status", "unknown")
                result_target = legacy_target_dict(values)
                blade_params = legacy_params_dict(values)
                ns = result_target.get("namespace") or kwargs.get("namespace") or ""
                names = result_target.get("names") or target_names or [kwargs.get("target_name", "")]
                _spec = read_fault_spec(values)
                fault_type = (
                    _spec.fault_type if (_spec and _spec.fault_type)
                    else skill_name or ""
                )

                from chaos_agent.memory.session_store import build_verification_simple
                verification = values.get("verification")
                from chaos_agent.agent.state import extract_ui_diagnostics, infer_task_state
                task_state = infer_task_state(values)
                if task_state == "injecting":
                    task_state = "injected"

                # Fallback for targets: when labels-based targeting produces empty names,
                # extract from verification resource_statuses or use labels as placeholder.
                if not names and verification:
                    resource_statuses = verification.get("layer1", {}).get("resource_statuses", [])
                    for rs in resource_statuses:
                        name = rs.get("name", "")
                        if name and isinstance(name, str) and name not in names:
                            names.append(name)
                if not names:
                    labels_info = result_target.get("labels") or kwargs.get("labels")
                    if labels_info:
                        if isinstance(labels_info, dict):
                            labels_str = ",".join(f"{k}={v}" for k, v in labels_info.items())
                        else:
                            labels_str = str(labels_info)
                        names = [f"<label:{labels_str}>"]

                # Build response data
                # Merge failure_reason into error
                merged_error = values.get("failure_reason") or values.get("error") or ""
                result_data = {
                    "task_id": task_id,
                    "result": task_state,
                    "fault_type": fault_type,
                    "blade_uid": blade_uid,
                    "targets": [{"name": name, "namespace": ns} for name in names],
                    "verification": build_verification_simple(verification),
                    "error": merged_error,
                    **extract_ui_diagnostics(values),
                }

                yield StreamEvent(
                    type="result",
                    content=json.dumps(build_inject_envelope(
                        result_data, task_state, merged_error,
                    ), ensure_ascii=False),
                    task_id=task_id,
                )
            else:
                yield StreamEvent(
                    type="error",
                    content="Graph completed but no state available",
                    task_id=task_id,
                )

        except Exception as e:
            code, msg = _format_error(e)
            logger.exception(f"Stream inject failed for task {task_id}")

            # Auto-rollback
            rollback_info = ""
            try:
                current_state = await graph.aget_state(config)
                if current_state and current_state.values:
                    blade_uid = current_state.values.get("blade_uid", "")
                    kubeconfig = current_state.values.get("kubeconfig", "")
                    if blade_uid:
                        from chaos_agent.tools.blade import blade_destroy
                        await blade_destroy.ainvoke(
                            {"uid": blade_uid, "kubeconfig": kubeconfig}
                        )
                        rollback_info = f" (auto-rolled back blade_uid={blade_uid})"
            except Exception as rb_err:
                rollback_info = f" (rollback FAILED: {rb_err})"
                logger.error(f"Auto-rollback failed: {rb_err}")

            yield StreamEvent(
                type="error",
                content=f"Inject failed: {msg}{rollback_info}",
                task_id=task_id,
            )
            # Build fault_type from CLI params for error response
            err_fault_type = ""
            if kwargs.get("scope") and kwargs.get("target") and kwargs.get("action"):
                err_fault_type = f"{kwargs['scope']}-{kwargs['target']}-{kwargs['action']}"
            yield StreamEvent(
                type="result",
                content=json.dumps(JSONEnvelope.fail(
                    code=code,
                    message=f"Inject failed: {msg}{rollback_info}",
                    data={
                        "task_id": task_id,
                        "result": "failed",
                        "fault_type": err_fault_type,
                        "blade_uid": "",
                        "targets": [],
                        "error": f"internal_error: Inject failed: {msg}{rollback_info}",
                    },
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

        task_id = f"task-{uuid.uuid4()}"
        tui_session_id = kwargs.get("tui_session_id", "") or ""

        # Same single-source-of-truth pattern as inject_stream: FaultSpec
        # only, no legacy scattered fields.
        _ts2 = now_iso()
        if kwargs.get("input"):
            spec = FaultSpec.from_cli_nl(input_text=kwargs["input"], kwargs=kwargs)
        else:
            spec = FaultSpec.from_cli_structured(kwargs)
        target_names = list(spec.names)
        initial_state = {
            "task_id": task_id,
            "tui_session_id": tui_session_id,
            "operation": "inject",
            "fault_spec": spec.to_dict(),
            "needs_confirmation": kwargs.get("confirm", False),
            "safety_status": "pending",
            "kubeconfig": kwargs.get("kubeconfig", ""),
            "kube_context": kwargs.get("context", ""),
            "created_at": _ts2,
            "direct": kwargs.get("direct", False) if not kwargs.get("input") else False,
            "interaction_mode": "cli",
        }

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

            # Build response from graph result
            from chaos_agent.agent.fault_spec import legacy_target_dict
            safety_status = "approved"
            plan_summary = ""
            blade_uid = ""
            result_target = {}
            skill_name = ""
            verification = None
            if isinstance(result, dict):
                safety_status = result.get("safety_status", "approved")
                plan_summary = result.get("plan_summary", "")
                blade_uid = result.get("blade_uid", "")
                result_target = legacy_target_dict(result)
                skill_name = result.get("skill_name", "")
                verification = result.get("verification")

            # Infer the correct task_state from the full graph state
            from chaos_agent.agent.state import extract_ui_diagnostics, infer_task_state
            task_state = infer_task_state(result if isinstance(result, dict) else {})
            # If infer_task_state returns 'injecting', the graph completed.
            # Only upgrade to 'injected' if blade_uid exists (injection succeeded).
            # Without blade_uid, the graph ended without successful injection → "failed".
            if task_state == "injecting":
                task_state = "injected" if blade_uid else "failed"

            # Resolve target info: prefer graph result, fall back to CLI kwargs, then blade_params
            blade_params = result.get("params") or {}
            ns = result_target.get("namespace") or kwargs.get("namespace") or blade_params.get("namespace") or ""
            names = result_target.get("names") or target_names or [kwargs.get("target_name", "")]
            fault_type = ""
            # Priority 1: from CLI structured params
            if kwargs.get("scope") and kwargs.get("target") and kwargs.get("action"):
                fault_type = f"{kwargs['scope']}-{kwargs['target']}-{kwargs['action']}"
            blade_params = result.get("params") or {}

            # Priority 2: from blade_params (LLM mode)
            if not fault_type and blade_params:
                scope = blade_params.get("scope", "")
                action = blade_params.get("action", "")
                target_action = blade_params.get("target", "")
                if scope and target_action and action:
                    fault_type = f"{scope}-{target_action}-{action}"
            # Priority 3: skill_name fallback
            if not fault_type:
                fault_type = skill_name

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

            from chaos_agent.memory.session_store import build_verification_simple
            # Build response data
            # Merge failure_reason into error
            merged_error = ""
            if isinstance(result, dict):
                failure_reason = result.get("failure_reason")
                if failure_reason:
                    merged_error = failure_reason
                elif result.get("error"):
                    merged_error = result["error"]
            inject_data = {
                "task_id": task_id,
                "result": task_state,
                "fault_type": fault_type,
                "blade_uid": blade_uid,
                "targets": [{"name": name, "namespace": ns} for name in names],
                "verification": build_verification_simple(verification),
                "error": merged_error,
                **extract_ui_diagnostics(result if isinstance(result, dict) else {}),
            }
            return build_inject_envelope(inject_data, task_state, merged_error)

        except Exception as e:
            code, msg = _format_error(e)
            logger.exception(f"Local inject failed for task {task_id}")

            # Auto-rollback: if blade_create succeeded but graph crashed later,
            # we must destroy the experiment to avoid orphaned faults.
            rollback_status = ""
            try:
                current_state = await self._agents["pipeline"].aget_state(config)
                if current_state and current_state.values:
                    blade_uid = current_state.values.get("blade_uid", "")
                    kubeconfig = current_state.values.get("kubeconfig", "")
                    if blade_uid:
                        logger.warning(
                            f"Auto-rollback: destroying blade experiment {blade_uid} "
                            f"after inject failure"
                        )
                        from chaos_agent.tools.blade import blade_destroy
                        destroy_result = await blade_destroy.ainvoke(
                            {"uid": blade_uid, "kubeconfig": kubeconfig}
                        )
                        rollback_status = f" (auto-rolled back blade_uid={blade_uid})"
                        logger.info(f"Auto-rollback result: {destroy_result}")
            except Exception as rb_err:
                rollback_status = f" (rollback FAILED: {rb_err})"
                logger.error(f"Auto-rollback failed: {rb_err}")

            # Build fault_type from CLI params for error response
            err_fault_type = ""
            if kwargs.get("scope") and kwargs.get("target") and kwargs.get("action"):
                err_fault_type = f"{kwargs['scope']}-{kwargs['target']}-{kwargs['action']}"

            return JSONEnvelope.fail(code=code, message=f"Inject failed: {msg}{rollback_status}", data={
                "task_id": task_id,
                "result": "failed",
                "fault_type": err_fault_type,
                "blade_uid": blade_uid or "",
                "targets": [],
                "error": f"internal_error: Inject failed: {msg}{rollback_status}",
            })
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
            "kube_context", "needs_confirmation", "dry_run",
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
                stream_evt = parse_stream_event(event)
                if stream_evt is not None:
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
                        stream_evt = parse_stream_event(event)
                        if stream_evt is not None:
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
                    stream_evt = parse_stream_event(event)
                    if stream_evt is not None:
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
                        stream_evt = parse_stream_event(event)
                        if stream_evt is not None:
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
                    from chaos_agent.memory.session_store import build_verification_simple
                    from chaos_agent.agent.state import extract_ui_diagnostics, infer_task_state
                    from chaos_agent.models.schemas import build_inject_envelope
                    from chaos_agent.agent.fault_spec import legacy_params_dict, legacy_target_dict, read_fault_spec

                    verification = pv.get("verification")
                    task_state = infer_task_state(pv)
                    if task_state == "injecting":
                        task_state = "injected"
                    result_target = legacy_target_dict(pv)
                    ns = result_target.get("namespace") or ""
                    names = result_target.get("names") or []
                    skill_name = pv.get("skill_name", "")
                    _spec = read_fault_spec(pv)
                    fault_type = (_spec.fault_type if (_spec and _spec.fault_type) else skill_name or "")
                    merged_error = pv.get("failure_reason") or pv.get("error") or ""

                    yield StreamEvent(
                        type="result",
                        content=json.dumps(build_inject_envelope(
                            {
                                "task_id": task_id,
                                "result": task_state,
                                "fault_type": fault_type,
                                "blade_uid": blade_uid,
                                "targets": [{"name": n, "namespace": ns} for n in names],
                                "verification": build_verification_simple(verification),
                                "error": merged_error,
                                **extract_ui_diagnostics(pv),
                            }, task_state, merged_error,
                        ), ensure_ascii=False),
                        task_id=task_id,
                    )
                else:
                    # Pipeline ran but no blade_uid (error / rejection)
                    error_msg = pv.get("failure_reason") or pv.get("error") or ""
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
                    from chaos_agent.agent.state import infer_task_state
                    from chaos_agent.agent.fault_spec import read_fault_spec as _read_spec
                    from chaos_agent.agent.state import extract_ui_diagnostics as _extract_diag
                    from chaos_agent.memory.session_store import build_verification_simple as _build_verif
                    ts = infer_task_state(pv) if pv else "unknown"
                    _spec = _read_spec(pv) if pv else None
                    _ft = (_spec.fault_type if _spec and _spec.fault_type else pv.get("skill_name", ""))
                    _ns = _spec.namespace if _spec else ""
                    _names = ", ".join(_spec.names) if _spec and _spec.names else ""
                    _uid = pv.get("blade_uid", "")
                    _verif = pv.get("verification")
                    _verif_simple = _build_verif(_verif) if _verif else None
                    _diag = _extract_diag(pv)
                    _se_summary = _diag.get("side_effects_summary", "")

                    _verif_line = ""
                    if _verif_simple:
                        _v_level = _verif_simple.get("level", "unknown")
                        _v_l1 = _verif_simple.get("layer1", {}).get("status", "?")
                        _v_l2 = _verif_simple.get("layer2", {}).get("status", "?")
                        _verif_line = f"验证: {_v_level} (L1={_v_l1}, L2={_v_l2})"

                    summary_parts = [
                        f"[Task Summary] task_id={task_id}",
                        f"类型: {_ft} | 目标: {_ns}/{_names}",
                        f"结果: {ts} | blade_uid: {_uid}",
                    ]
                    if _verif_line:
                        summary_parts.append(_verif_line)
                    if _se_summary:
                        summary_parts.append(f"副作用: {_se_summary}")
                    if _diag.get("failure_reason"):
                        summary_parts.append(f"失败原因: {_diag['failure_reason']}")
                    summary = "\n".join(summary_parts)
                    summary_msg = SystemMessage(content=summary)
                    await intent_graph.aupdate_state(intent_config, {
                        "messages": [summary_msg],
                        "pipeline_task_id": task_id,
                    }, as_node="save_dialogue")
                    # Also persist to session file for audit trail
                    if self._tui_session_store and session_id:
                        self._tui_session_store.append_dialogue(session_id, [summary_msg])
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
                    stream_evt = parse_stream_event(event)
                    if stream_evt is not None:
                        stream_evt.task_id = task_id
                        yield stream_evt
            else:
                async for event in graph.astream_events(None, config, version="v2"):
                    stream_evt = parse_stream_event(event)
                    if stream_evt is not None:
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
                    stream_evt = parse_stream_event(event)
                    if stream_evt is not None:
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
                stream_evt = parse_stream_event(event)
                if stream_evt is not None:
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
                    stream_evt = parse_stream_event(event)
                    if stream_evt is not None:
                        stream_evt.task_id = thread_id
                        yield stream_evt

            # Yield a structured result if the pipeline produced a blade_uid.
            final_state = await graph.aget_state(config)
            if final_state and final_state.values:
                values = final_state.values
                blade_uid = values.get("blade_uid", "")
                if blade_uid:
                    from chaos_agent.memory.session_store import build_verification_simple
                    from chaos_agent.agent.state import extract_ui_diagnostics, infer_task_state
                    from chaos_agent.models.schemas import build_inject_envelope

                    task_state = infer_task_state(values)
                    if task_state == "injecting":
                        task_state = "injected"
                    from chaos_agent.agent.fault_spec import (
                        legacy_params_dict, legacy_target_dict, read_fault_spec,
                    )
                    result_target = legacy_target_dict(values)
                    blade_params = legacy_params_dict(values)
                    ns = result_target.get("namespace") or ""
                    names = result_target.get("names") or []
                    skill_name = values.get("skill_name", "")
                    _spec_for_ft = read_fault_spec(values)
                    fault_type = (
                        _spec_for_ft.fault_type if (_spec_for_ft and _spec_for_ft.fault_type)
                        else skill_name or ""
                    )
                    merged_error = values.get("failure_reason") or values.get("error") or ""
                    yield StreamEvent(
                        type="result",
                        content=json.dumps(
                            build_inject_envelope(
                                {
                                    "task_id": thread_id,
                                    "result": task_state,
                                    "fault_type": fault_type,
                                    "blade_uid": blade_uid,
                                    "targets": [{"name": n, "namespace": ns} for n in names],
                                    "verification": build_verification_simple(values.get("verification")),
                                    "error": merged_error,
                                    **extract_ui_diagnostics(values),
                                },
                                task_state,
                                merged_error,
                            ),
                            ensure_ascii=False,
                        ),
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
        """Recover a fault locally. Equivalent to POST /api/v1/recover.

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

        try:
            # Get current state from inject graph checkpoint
            current_state = await self._agents["pipeline"].aget_state(config)
            if not current_state or not current_state.values:
                return JSONEnvelope.fail(code=ResponseCode.TASK_NOT_FOUND, message=f"Task not found: {inject_task_id}")

            state_values = current_state.values
            blade_uid = state_values.get("blade_uid", "")
            target = state_values.get("target", {}) or {}
            skill_name = state_values.get("skill_name", "")
            kubeconfig = kwargs.get("kubeconfig", "") or state_values.get("kubeconfig", "")
            inject_tui_session_id = state_values.get("tui_session_id", "") or ""

            # Build inject context from inject-phase messages for recover LLM
            # Reformatted: raw kubectl outputs are abstracted to prevent
            # "causal chain illusion" (LLM reusing stale inject-phase data as
            # current post-recovery evidence instead of calling kubectl).
            # See utils/inject_context.py for rationale and implementation.
            from chaos_agent.utils.inject_context import build_inject_context
            inject_msgs = state_values.get("messages", [])
            inject_context = build_inject_context(inject_msgs)

            # Build initial state for recover graph
            # Explicitly clear verification/messages to prevent inject graph
            # checkpoint state from leaking into the recover verifier loop.
            initial_state = {
                "task_id": record_task_id,
                "tui_session_id": inject_tui_session_id,
                "parent_task_id": inject_task_id,
                "operation": "recover",
                "blade_uid": blade_uid,
                "skill_name": skill_name,
                "skill_case_content": state_values.get("skill_case_content", ""),
                "inject_verification_summary": state_values.get("inject_verification_summary", ""),
                "inject_context": inject_context,
                "target": target,
                "kubeconfig": kubeconfig,
                "injection_method": state_values.get("injection_method"),
                "kubectl_exec_pod_name": state_values.get("kubectl_exec_pod_name"),
                "created_at": state_values.get("created_at", ""),  # Preserve inject's created_at
                "verifier_loop_count": 0,  # Reset for fresh recover attempt
                "verification": None,       # Clear inject graph's verification
                "recover_verification": None,  # Clear stale recover verification
                "messages": [],             # Clear inject graph's conversation
                "inject_layer1_cache": None,   # Clear inject layer1 cache
                "recover_layer1_cache": None,  # Clear stale recover layer1 cache
                "recover_phase": "layer1_recovery",  # Reset to Layer 1 for fresh recover
                "layer1_iteration_count": 0,   # Reset Layer 1 iteration counter
                "layer2_context_added": False,  # Reset Layer 2 context flag
                "error": None,              # Clear stale error from previous attempt
                "failure_reason": None,     # Clear stale failure_reason
                "failure_detail": None,     # Clear stale failure_detail
            }

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
            recover_start = time.monotonic()
            result = await self._agents["recover"].ainvoke(initial_state, config)
            recover_duration_ms = int((time.monotonic() - recover_start) * 1000)

            # Extract verification results from graph output
            is_recovered = False
            recovery_level = "recovered"
            verification = None
            if isinstance(result, dict):
                is_recovered = result.get("result", {}).get("recovered", False)
                recovery_level = result.get("result", {}).get("recovery_level", "recovered")
                verification = result.get("recover_verification")

            # Build targets info (fallback to inject graph params for namespace)
            names = target.get("names", []) if target else []
            ns = target.get("namespace", "") if target else ""
            if not ns:
                inject_params = state_values.get("params") or {}
                ns = inject_params.get("namespace", "")

            from chaos_agent.memory.session_store import build_verification_simple

            if not is_recovered:
                # Merge failure_reason into error
                failure_reason = result.get("failure_reason") if isinstance(result, dict) else None
                recover_fail_data = {
                    "task_id": inject_task_id,
                    "result": "failed",
                    "blade_uid": blade_uid,
                    "targets": [{"name": name, "namespace": ns} for name in names],
                    "error": failure_reason or "Recovery verification failed",
                }
                return JSONEnvelope.fail(
                    code=ResponseCode.NO_BLADE_UID,
                    message="Recovery verification failed",
                    data=recover_fail_data,
                )

            return JSONEnvelope.ok(
                data={
                    "task_id": inject_task_id,
                    "result": recovery_level,
                    "blade_uid": blade_uid,
                    "targets": [{"name": name, "namespace": ns} for name in names],
                    "verification": build_verification_simple(verification),
                },
            )

        except Exception as e:
            code, msg = _format_error(e)
            logger.exception(f"Local recover failed for task {inject_task_id}")
            return JSONEnvelope.fail(code=code, message=f"Recovery failed: {msg}", data={
                "task_id": inject_task_id,
                "result": "failed",
                "blade_uid": blade_uid or "",
                "targets": [],
                "error": f"internal_error: Recovery failed: {msg}",
            })
        finally:
            # Finalize session: flush remaining messages from final graph state
            if self._session_store:
                try:
                    remaining = []
                    recover_verification = None
                    error_fin = ""
                    failure_reason_fin = ""
                    values_fin = {}
                    try:
                        final_graph_state = await self._agents["recover"].aget_state(config)
                        if final_graph_state and final_graph_state.values:
                            values_fin = final_graph_state.values
                            remaining = values_fin.get("messages", [])
                            recover_verification = values_fin.get("recover_verification")
                            error_fin = values_fin.get("error") or ""
                            failure_reason_fin = values_fin.get("failure_reason") or ""
                    except Exception:
                        pass
                    from chaos_agent.memory.session_store import build_verification_simple
                    from chaos_agent.agent.state import infer_task_state

                    inferred_state = infer_task_state(values_fin) if values_fin else "recovered"

                    merged_error_fin = failure_reason_fin or error_fin or ""
                    names_fin = target.get("names", []) if target else []
                    ns_fin = target.get("namespace", "") if target else ""
                    self._session_store.finalize_session(
                        record_task_id,
                        remaining_messages=remaining,
                        result_summary=build_inject_envelope({
                            "task_id": inject_task_id,
                            "result": inferred_state,
                            "blade_uid": blade_uid,
                            "targets": [{"name": n, "namespace": ns_fin} for n in names_fin],
                            "verification": build_verification_simple(recover_verification),
                            "error": merged_error_fin,
                        }, inferred_state, merged_error_fin),
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
