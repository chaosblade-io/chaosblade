"""AgentRunner: Local execution wrapper that runs Agent Core directly without a server.

Provides the same interface as AgentClient but executes Agent Core locally.
Returns response dicts in the same format as server routes.
"""

import asyncio
import json
import logging
import signal
import sys
from collections import defaultdict
from typing import Callable, Optional

from chaos_agent import __version__
from chaos_agent.agent.factory import create_agent
from chaos_agent.agent.state import has_active_fault
from chaos_agent.agent.spec.fault_spec import DurationParamError, FaultSpec
from chaos_agent.agent.state_mgmt.state_builders import build_inject_initial_state
from chaos_agent.agent.streaming import StreamEvent, parse_stream_events
from chaos_agent.config.settings import settings
from chaos_agent.persistence.task_identity import (
    new_inject_task_id,
    new_recover_task_id,
)
from chaos_agent.models.schemas import JSONEnvelope, ResponseCode, build_inject_envelope
from chaos_agent.observability.status_tracker import (
    subscribe,
    unsubscribe,
    remove_tracker,
)
from chaos_agent.skills.catalog_generator import (
    generate_skill_catalog,
    infer_scope,
)
from chaos_agent.skills.loader import get_skills_dir
from chaos_agent.skills.models import SKILL_TYPE_FAULT_INJECTION
from chaos_agent.skills.prerequisites import PrerequisitesChecker
from chaos_agent.skills.registry import SkillRegistry
from chaos_agent.utils.fault_type import extract_fault_type
from chaos_agent.utils.time import now_iso
from chaos_agent.cli.result_builder import (
    _build_inject_result_events,
)
from chaos_agent.cli.session_finalize import (
    _finalize_inject_session,
    _format_error,
    auto_rollback,
)
from chaos_agent.cli.status_display import _status_printer

logger = logging.getLogger(__name__)


# Shared boundary decision for unattended auto-approve (single source:
# gates/_write_set_boundary). Kept importable here for the existing
# tests and call sites; every unattended channel — this runner's
# streaming and non-streaming paths, the HTTP SSE route, L4's legacy
# pre_approved branch — must decide through it, never a hard-coded
# "approved". AUTO delegation semantics: a widened write-set contract
# (case manifest beyond the victim coverage) auto-approves too, with
# an auditable ``auto_approved`` event carrying the entries — the
# manifest is the authority, the guard is the enforcement.
from chaos_agent.agent.nodes.gates._write_set_boundary import (  # noqa: E402
    unattended_resume_value as _unattended_resume_value,
    widened_auto_approval_payload as _widened_auto_approval_payload,
)


def _install_sigterm_cancel_guard() -> Optional[Callable[[], None]]:
    """Replace SIGTERM's default kill disposition with a driving-task cancel.

    The default disposition terminates the process with no Python-level
    chance to write the row's terminal word — the row stays at its last
    mid-graph upsert ("injecting"/"recovering"), a zombie no later
    writer can fix. The except branch around each CLI graph run catches
    the resulting CancelledError and writes the terminal word via
    write_aborted_task_row (skip_if_terminal keeps a finished run's
    verdict). SIGINT keeps its default KeyboardInterrupt disposition
    (the confirm prompts rely on it) and lands in the same except
    branch.

    Returns a cleanup callable, or None when the guard cannot be
    installed (non-main-thread / unsupported platform) — callers must
    degrade to the pre-guard behavior, not fail the run.
    """
    loop = asyncio.get_running_loop()
    # Capture the driving task at install time: signal-handler callbacks
    # run in loop-iteration context, not inside any task, so
    # current_task() inside the callback is always None.
    driving_task = asyncio.current_task()

    def _on_sigterm() -> None:
        if driving_task is not None and not driving_task.done():
            driving_task.cancel()

    try:
        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    except (NotImplementedError, RuntimeError):
        return None
    return lambda: loop.remove_signal_handler(signal.SIGTERM)


def _signal_interrupted_word() -> str:
    """The classified terminal word for a signal-interrupted CLI graph run.

    Single source for the interrupt case's word on BOTH CLI surfaces:
    the row write below and the recover session's ``default_status``
    resolve through this one helper, i.e. through the shared
    ``abort_row_word`` taxonomy the server's abort paths use. Round-64
    F1: the CLI recover paths spelled their session word from a boolean
    that could not tell an interrupt from a success, so the same abort
    event shipped "cancelled" to the row and "completed" to the session
    record — the word split rounds 55/56 legislated away, CLI edition.
    """

    from chaos_agent.server.routes.stream_abort import abort_row_word

    return abort_row_word("user_cancel")


async def _write_signal_interrupted_row(task_id: str) -> None:
    """Terminal row write for a signal-interrupted CLI graph run.

    "user_cancel" classifies to "cancelled" through _signal_interrupted_word
    — the same single source the server abort paths use. Fail-soft by
    design (an interrupt path must not raise past the exit that is
    already unwinding); write_aborted_task_row logs LOUD when the write
    is lost.
    """
    from chaos_agent.server.routes.stream_abort import write_aborted_task_row

    await write_aborted_task_row(task_id, _signal_interrupted_word())


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

        # TUI conversations live in exactly ONE place: the server's /turn
        # SSE route (server/routes/turn.py builds the intent initial_state,
        # turn_event_stream.py event_generator drives the dual-graph flow).
        # The local converse_stream twin this branch used to delegate to
        # was retired 2026-09-01 — zero callers and drifting from the
        # server implementation (channel-field fallbacks, interruption
        # records, checkpoint rollback). This method serves the CLI
        # streaming path only; interaction_mode="tui" is no longer a
        # supported entry point here.

        if kwargs.get("kubeconfig"):
            settings.kubeconfig_path = kwargs["kubeconfig"]
        if kwargs.get("context"):
            settings.kube_context = kwargs["context"]
        if kwargs.get("cluster_uuid"):
            settings.kubewiz_cluster_uuid = kwargs["cluster_uuid"]
        if kwargs.get("profile"):
            settings.kubewiz_profile = kwargs["profile"]

        task_id = new_inject_task_id()
        tui_session_id = kwargs.get("tui_session_id", "") or ""

        # Build initial state. FaultSpec is the single source of truth
        # for fault identity + tuning — entry points construct it and
        # no longer write the legacy scattered fields. Consumers read
        # via ``read_fault_spec(state)``.
        _ts = now_iso()
        _interaction_mode = kwargs.get("interaction_mode", "cli")
        _dry_run = bool(kwargs.get("dry_run", False))
        try:
            if kwargs.get("input"):
                spec = FaultSpec.from_cli_nl(input_text=kwargs["input"], kwargs=kwargs)
            else:
                spec = FaultSpec.from_cli_structured(kwargs)
        except DurationParamError as e:
            # Duration contract violation is a client input error — surface
            # the actionable guidance instead of letting the traceback leak.
            yield StreamEvent(type="error", content=str(e), task_id=task_id)
            return
        initial_state = build_inject_initial_state(
            task_id=task_id,
            tui_session_id=tui_session_id,
            fault_spec=spec,
            needs_confirmation=kwargs.get("confirm", False),
            kubeconfig=kwargs.get("kubeconfig", ""),
            kube_context=kwargs.get("context", ""),
            # Transport/channel fields are config-driven for the CLI surface
            # (AGENTS.md CLI syntax exposes no --ssh-host/--kube-connection-mode
            # flags), so these kwargs are normally absent and fall back to
            # settings via TransportTarget.from_state. They are read here only to
            # honor a programmatic caller that does supply them.
            kubewiz_cluster_uuid=kwargs.get("cluster_uuid", ""),
            kubewiz_profile=kwargs.get("profile", ""),
            kube_connection_mode=kwargs.get("kube_connection_mode", ""),
            host_name=kwargs.get("host_name", ""),
            ssh_host=kwargs.get("ssh_host", ""),
            ssh_user=kwargs.get("ssh_user", ""),
            ssh_key_path=kwargs.get("ssh_key_path", ""),
            ssh_port=kwargs.get("ssh_port"),
            created_at=_ts,
            interaction_mode=_interaction_mode,
            dry_run=_dry_run,
            planning_mode=kwargs.get("planning_mode", ""),
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
        status_queue = subscribe(task_id)
        done_event = asyncio.Event()
        printer_task = asyncio.create_task(_status_printer(status_queue, done_event))

        # Round-64 F2: an interrupt is the THIRD terminal case the boolean
        # exits cannot spell. The classified word lands here from the
        # signal arm below and rides into the session finalize as its
        # override — without it the row shipped "cancelled" while the
        # session's own inference spelled the same abort event "failed"
        # (one event, two words, two user-visible surfaces: the r55 F2
        # split, still open on the inject-CLI side).
        abort_word = ""

        # Orphan-row guard: SIGTERM's default kill disposition leaves the
        # row at its last mid-graph upsert (see _install_sigterm_cancel_guard).
        _sigterm_cleanup = _install_sigterm_cancel_guard()

        try:
            # Print a notice so the user knows the process is running
            if not settings.is_debug:
                sys.stderr.write("  ⏳ Fault injection in progress — the AI is analysing and planning, please wait...\n")
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

                # Determine interrupt content
                interrupt_content = ""
                if interrupt_info and isinstance(interrupt_info, dict):
                    interrupt_content = interrupt_info
                elif interrupt_info and isinstance(interrupt_info, str):
                    interrupt_content = {"type": "confirmation", "plan_summary": interrupt_info}
                else:
                    # Fallback: infer from graph state
                    plan_summary = current_state.values.get("plan_summary", "") if current_state.values else ""
                    interrupt_content = {"type": "confirmation", "plan_summary": plan_summary}

                # Resume the graph based on callback availability
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
                        resume_value = _unattended_resume_value(interrupt_info)
                        # AUTO delegation over a widened contract is an
                        # auditable event — the manifest entries ride the
                        # stream verbatim so the approval is on the record
                        # even with no human at the console (the guard
                        # still enforces the boundary per-name).
                        _widened = _widened_auto_approval_payload(interrupt_info)
                        if _widened is not None:
                            yield StreamEvent(
                                type="auto_approved",
                                content=(
                                    "[Auto-approved: confirmation_gate] "
                                    "widened write-set contract "
                                    "(case manifest mechanism_writes) — "
                                    "see payload for the entries"
                                ),
                                node="confirmation_gate",
                                task_id=task_id,
                                payload=_widened,
                            )
                        from langgraph.types import Command

                        async for event in graph.astream_events(
                            Command(resume=resume_value), config, version="v2"
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
                        # Legacy confirm_callback: only handles confirmation.
                        # Pass the FULL interrupt payload (dict) when available
                        # so the callback can render the widened-contract
                        # entries verbatim — a human approving a case manifest
                        # must see what the case legislated, not just the
                        # plan prose. Plan-text-only callbacks (old signature)
                        # still receive the summary string.
                        decision = await confirm_callback(
                            interrupt_info if isinstance(interrupt_info, dict)
                            else plan_summary
                        )
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
                snapshot=final_state,
            )
            for evt in result_events:
                yield evt
            if should_return:
                return

        except (KeyboardInterrupt, asyncio.CancelledError):
            # Signal-interrupted run (SIGTERM cancel / Ctrl-C): the graph
            # never finished, so the row must leave its mid-graph word —
            # and the session must hear the SAME classified word, or one
            # abort event ships two words (the r55 F2 split, CLI edition).
            abort_word = _signal_interrupted_word()
            try:
                await _write_signal_interrupted_row(task_id)
            except Exception:
                logger.warning(
                    "Failed to write interrupted terminal word for %s "
                    "(zombie-row risk)", task_id,
                )
            raise

        except Exception as e:
            code, msg = _format_error(e)
            logger.exception(f"Stream inject failed for task {task_id}")

            rollback_info = await auto_rollback(graph, config)

            yield StreamEvent(
                type="error",
                content=f"Inject failed: {msg}{rollback_info}",
                task_id=task_id,
            )
            from chaos_agent.agent.result.operation_result import build_inject_status_data_from_state

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
            if _sigterm_cleanup is not None:
                _sigterm_cleanup()
            # Finalize session: flush remaining messages from final graph state.
            # Conversations are the server /turn route's business (the local
            # converse_stream twin that carried the open-conversation
            # exception was retired 2026-09-01): a CLI-streaming inject is
            # always a blocking one-shot — always finalize.
            await _finalize_inject_session(
                self._session_store, graph, config, task_id,
                kwargs=kwargs,
                error_log_level="warning",
                # Round-64 F2: the interrupt path's classified word — the
                # session surface's only other writer is this finalize, so
                # without it the row's "cancelled" met the session's
                # inferred "failed" (the finalizer's own verdict gate
                # still yields to a reached verdict).
                status_override=abort_word or None,
            )
            done_event.set()
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

        task_id = new_inject_task_id()
        tui_session_id = kwargs.get("tui_session_id", "") or ""

        # Same single-source-of-truth pattern as inject_stream: FaultSpec
        # only, no legacy scattered fields.
        _ts2 = now_iso()
        try:
            if kwargs.get("input"):
                spec = FaultSpec.from_cli_nl(input_text=kwargs["input"], kwargs=kwargs)
            else:
                spec = FaultSpec.from_cli_structured(kwargs)
        except DurationParamError as e:
            # Duration contract violation is a client input error — return an
            # actionable envelope instead of letting the traceback leak.
            return JSONEnvelope.fail(
                code=ResponseCode.INVALID_PARAMS,
                message=str(e),
            )
        initial_state = build_inject_initial_state(
            task_id=task_id,
            tui_session_id=tui_session_id,
            fault_spec=spec,
            needs_confirmation=kwargs.get("confirm", False),
            kubeconfig=kwargs.get("kubeconfig", ""),
            kube_context=kwargs.get("context", ""),
            # Transport/channel fields are config-driven for the CLI surface
            # (AGENTS.md CLI syntax exposes no --ssh-host/--kube-connection-mode
            # flags), so these kwargs are normally absent and fall back to
            # settings via TransportTarget.from_state. They are read here only to
            # honor a programmatic caller that does supply them.
            kubewiz_cluster_uuid=kwargs.get("cluster_uuid", ""),
            kubewiz_profile=kwargs.get("profile", ""),
            kube_connection_mode=kwargs.get("kube_connection_mode", ""),
            host_name=kwargs.get("host_name", ""),
            ssh_host=kwargs.get("ssh_host", ""),
            ssh_user=kwargs.get("ssh_user", ""),
            ssh_key_path=kwargs.get("ssh_key_path", ""),
            ssh_port=kwargs.get("ssh_port"),
            created_at=_ts2,
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

        # Round-64 F2: an interrupt is the THIRD terminal case the boolean
        # exits cannot spell. The classified word lands here from the
        # signal arm below and rides into the session finalize as its
        # override (see inject_stream for the split it closes).
        abort_word = ""

        # Orphan-row guard (see _install_sigterm_cancel_guard).
        _sigterm_cleanup = _install_sigterm_cancel_guard()

        try:
            # Print a notice so the user knows the process is running
            if not settings.is_debug:
                sys.stderr.write("  ⏳ Fault injection in progress — the AI is analysing and planning, please wait...\n")
                sys.stderr.flush()

            # First invoke - will pause at confirmation_gate (or complete if chat)
            result = await self._agents["pipeline"].ainvoke(initial_state, config)

            # Round-64 F3: with confirm=True this invoke returns at the gate's
            # ``interrupt()`` — the run is PARKED, not over. Capture the fact
            # from the engine (the authority on pause) before anything
            # translates the values, or the fail-closed terminal projection
            # reads "no verdict yet" as "the injection failed".
            run_paused = False
            if kwargs.get("confirm", False):
                from chaos_agent.agent.state import resumable_pause

                run_paused = resumable_pause(
                    await self._agents["pipeline"].aget_state(config)
                )

            # If confirmation is NOT required, auto-approve and wait for completion
            # Only resume if the graph is actually paused at confirmation_gate
            if not kwargs.get("confirm", False):
                from langgraph.types import Command

                current_state = await self._agents["pipeline"].aget_state(config)
                # If graph is waiting for human input (at confirmation_gate), resume it
                if current_state and current_state.next:
                    # Unattended auto-approve decides through the shared
                    # boundary helper (AUTO delegation: always "approved" —
                    # the manifest is the authority, the guard enforces it).
                    # A widened write-set contract (the interrupt payload's
                    # ``write_set_widened`` marker) additionally lands in the
                    # audit log: no stream here to carry the event, so the
                    # delegation is recorded through the logger.
                    interrupt_info = None
                    for t in (current_state.tasks or []):
                        if getattr(t, "interrupts", None):
                            interrupt_info = t.interrupts[0].value
                            break
                    resume_value = _unattended_resume_value(interrupt_info)
                    if _widened_auto_approval_payload(interrupt_info) is not None:
                        logger.info(
                            "auto_approved: confirmation_gate delegated a "
                            "widened write-set contract (case manifest "
                            "mechanism_writes beyond victim coverage); "
                            "target_guard enforces the per-name boundary"
                        )
                    result = await self._agents["pipeline"].ainvoke(
                        Command(resume=resume_value), config
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

            from chaos_agent.agent.result.operation_result import build_inject_data_from_state
            inject_data = build_inject_data_from_state(
                result if isinstance(result, dict) else {}, task_id,
                paused=run_paused,
            )
            return build_inject_envelope(
                inject_data, inject_data["task_state"], inject_data.get("error", ""),
            )

        except (KeyboardInterrupt, asyncio.CancelledError):
            # Signal-interrupted run (SIGTERM cancel / Ctrl-C): the graph
            # never finished, so the row must leave its mid-graph word —
            # and the session must hear the SAME classified word, or one
            # abort event ships two words (the r55 F2 split, CLI edition).
            abort_word = _signal_interrupted_word()
            try:
                await _write_signal_interrupted_row(task_id)
            except Exception:
                logger.warning(
                    "Failed to write interrupted terminal word for %s "
                    "(zombie-row risk)", task_id,
                )
            raise

        except Exception as e:
            code, msg = _format_error(e)
            logger.exception(f"Local inject failed for task {task_id}")

            rollback_status = await auto_rollback(self._agents["pipeline"], config)

            from chaos_agent.agent.result.operation_result import build_inject_status_data_from_state

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
            if _sigterm_cleanup is not None:
                _sigterm_cleanup()
            # Finalize session: flush remaining messages from final graph state
            await _finalize_inject_session(
                self._session_store, self._agents["pipeline"], config, task_id,
                kwargs=kwargs,
                error_log_level="warning",
                # Round-64 F2: the interrupt path's classified word (see
                # inject_stream) — the finalizer's own verdict gate still
                # yields to a verdict the graph already reached.
                status_override=abort_word or None,
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

        # Orphan-row guard (see _install_sigterm_cancel_guard).
        _sigterm_cleanup = _install_sigterm_cancel_guard()

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

        except (KeyboardInterrupt, asyncio.CancelledError):
            # Signal-interrupted run (SIGTERM cancel / Ctrl-C): the graph
            # never finished, so the row must leave its mid-graph word.
            try:
                await _write_signal_interrupted_row(task_id)
            except Exception:
                logger.warning(
                    "Failed to write interrupted terminal word for %s "
                    "(zombie-row risk)", task_id,
                )
            raise

        except Exception as e:
            logger.exception(f"Resume stream failed for task {task_id}")
            yield StreamEvent(type="error", content=f"Resume failed: {e}", task_id=task_id)
        finally:
            if _sigterm_cleanup is not None:
                _sigterm_cleanup()
            done_event.set()
            unsubscribe(task_id, status_queue)
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
                content="This session is not in Dry-Run state, so it cannot be applied directly. Use /run <description> to start a new task.",
                task_id=thread_id,
            )
            return

        # Write-set boundary at the lift seam: ``aupdate_state`` writes the
        # lift values "as if" confirmation_gate had emitted them — the gate
        # node body NEVER runs, so its approval/clearance cannot happen
        # implicitly. A snapshot still pending its knowing human must be
        # cleared through an explicit decision BEFORE the stream continues:
        #   confirm mode  → knowledge card via interrupt_callback (the
        #                   manifest entries render verbatim; approval
        #                   rewrites the snapshot without the marker)
        #   auto mode     → /run is the operator's explicit command and the
        #                   standing delegation covers the widened contract
        #                   (D4) — clear with an audit log line
        from chaos_agent.agent.nodes.gates._write_set_boundary import (
            snapshot_widening_pending,
        )
        from chaos_agent.agent.target_guard.freeze import approved_from_dict
        from chaos_agent.agent.target_guard.mechanism_writes import (
            entries_beyond_victim,
            format_entries_for_payload,
        )
        _lift_clear_pending: dict = {}
        if snapshot_widening_pending(snapshot.values):
            _approved = approved_from_dict(snapshot.values.get("approved_target") or {})
            _widening = format_entries_for_payload(
                entries_beyond_victim(_approved) if _approved else (),
            )
            if interrupt_callback is not None:
                _card = {
                    "type": "confirmation",
                    "write_set_widened": {"mechanism_writes": _widening},
                    "mechanism_writes": _widening,
                    "plan_summary": snapshot.values.get("plan_summary", ""),
                    "safety_reason": (
                        "The dry-run plan's case contract includes mechanism "
                        "writes beyond the victim target. Approve to freeze "
                        "the extended write-set contract."
                    ),
                }
                _decision = await interrupt_callback(_card)
                if _decision != "approved":
                    yield StreamEvent(
                        type="error",
                        content="Dry-Run apply rejected: the widened write-set "
                                "contract (case manifest mechanism_writes) was "
                                "declined. Re-plan or approve the entries.",
                        task_id=thread_id,
                    )
                    return
            else:
                logger.info(
                    "lift_dry_run_and_run: auto-mode /run approved the widened "
                    "write-set contract (%d manifest entries beyond victim "
                    "coverage) under the operator's standing delegation",
                    len(_widening),
                )
            # Approval (or standing delegation): rewrite the snapshot with
            # the pending marker cleared, same as the gate's approved re-freeze.
            _existing = dict(snapshot.values.get("approved_target") or {})
            _existing.pop("widening_pending_approval", None)
            _lift_clear_pending = {"approved_target": _existing}

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
                # Widened-contract clearance decided above (knowledge card
                # or standing delegation) rides the same lift write.
                **_lift_clear_pending,
            },
            as_node="confirmation_gate",
        )

        status_queue = subscribe(thread_id)
        done_event = asyncio.Event()

        # Orphan-row guard (see _install_sigterm_cancel_guard): this entry
        # point drives the pipeline too, and without the guard its thread
        # row keeps its last mid-graph word on a SIGTERM.
        _sigterm_cleanup = _install_sigterm_cancel_guard()

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

            # Yield a structured result if the pipeline produced an active fault.
            final_state = await graph.aget_state(config)
            if final_state and final_state.values:
                values = final_state.values
                if has_active_fault(values):
                    from chaos_agent.models.schemas import build_inject_envelope
                    from chaos_agent.agent.result.operation_result import build_inject_data_from_state

                    _data = build_inject_data_from_state(values, thread_id)
                    yield StreamEvent(
                        type="result",
                        content=json.dumps(build_inject_envelope(
                            _data, _data["task_state"], _data.get("error", ""),
                        ), ensure_ascii=False),
                        task_id=thread_id,
                    )

        except (KeyboardInterrupt, asyncio.CancelledError):
            # Signal-interrupted run (SIGTERM cancel / Ctrl-C): the graph
            # never finished, so the row must leave its mid-graph word
            # (skip_if_terminal keeps any verdict the thread reached).
            try:
                await _write_signal_interrupted_row(thread_id)
            except Exception:
                logger.warning(
                    "Failed to write interrupted terminal word for %s "
                    "(zombie-row risk)", thread_id,
                )
            raise

        except Exception as e:
            logger.exception(f"lift_dry_run_and_run failed for {thread_id}")
            yield StreamEvent(
                type="error",
                content=f"Dry-Run apply failed: {e}",
                task_id=thread_id,
            )
        finally:
            if _sigterm_cleanup is not None:
                _sigterm_cleanup()
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
            # Connected defect 1 (round-64): ``query_active()`` keys off the
            # materialised liability column (round-32) — it returns rows with
            # a COMMITTED fault awaiting recovery. A run parked at the
            # confirmation gate has committed nothing, so it carries no
            # liability and was invisible here, blinding the TUI
            # crash-recovery to exactly the tasks its docstring promises to
            # find ("paused at interrupt points, waiting for user input").
            # The right discovery set for a pause is the ``waiting_input``
            # word the row itself carries (``select_tasks_by_state``), which
            # is "find resumable pauses", NOT the round-32 liability
            # predicate — a different question, so this does not resurrect
            # the retired "guess recoverability from the word" rule. Union
            # both so nothing the old query found is lost; the per-task
            # ``state.next`` check below is still the authority on whether a
            # candidate is really paused.
            from chaos_agent.agent.state import TaskStateOverlay
            candidate_tasks = await store.list_tasks(
                task_state=TaskStateOverlay.WAITING_INPUT.value,
            )
            try:
                candidate_tasks += await store.query_active()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Failed to query active tasks: {e}")
        except Exception as e:
            logger.warning(f"Failed to query interrupted tasks: {e}")
            return []

        graph = self._agents["pipeline"]
        interrupted = []
        seen_task_ids: set[str] = set()

        for task in candidate_tasks:
            task_id = task.get("task_id", "")
            if not task_id or task_id in seen_task_ids:
                continue
            seen_task_ids.add(task_id)

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

        # Reset module-level time_wait state for the new recover task.
        from chaos_agent.tools.wait import reset_wait_state
        reset_wait_state()

        inject_task_id = task_id
        # Recover gets its own task record file, cross-referenced back to inject
        # via parent_task_id. The langgraph thread_id stays = inject_task_id so
        # the recover graph can read inject's checkpoint.
        record_task_id = new_recover_task_id()
        config = {"configurable": {"thread_id": inject_task_id}, "recursion_limit": settings.recursion_limit}

        # Subscribe to status events emitted by recover nodes (keyed by state.task_id)
        status_queue = subscribe(record_task_id)
        done_event = asyncio.Event()
        printer_task = asyncio.create_task(_status_printer(status_queue, done_event))

        # Pre-declare in case fallback path is taken or an early exception fires.
        experiment_uid = ""
        state_values: dict = {}
        # Round-63 R63-1: the CLI envelope's session status consumes
        # default_status, so the failure exits must say WHICH exit ran —
        # otherwise a failed recovery records "completed" on its session
        # (the envelope's own data.result says failed/unverified).
        recover_failed = False
        # Round-64 F1: an interrupt is the THIRD terminal case the boolean
        # cannot spell. The classified word lands here from the signal arm
        # below and outranks both boolean exits — without it the same
        # abort event shipped "cancelled" to the row and the optimistic
        # "completed" to the session record.
        abort_word = ""

        # Orphan-row guard: SIGTERM's default kill disposition leaves the
        # inject row AND this run's recover session at their last mid-run
        # write (see _install_sigterm_cancel_guard).
        _sigterm_cleanup = _install_sigterm_cancel_guard()

        try:
            # Try to fetch LangGraph checkpoint as supplemental live context.
            current_state = await self._agents["pipeline"].aget_state(config)
            checkpoint_values = current_state.values if current_state and current_state.values else {}

            from chaos_agent.agent.result.task_snapshot import resolve_recover_initial_state

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
            experiment_uid = initial_state.get("experiment_uid") or ""
            inject_tui_session_id = initial_state.get("tui_session_id", "") or ""

            # Mark the inject task as "recovering" in TaskStore so that
            # query_active_experiments no longer returns it.  We use
            # update_task_state (direct column write) instead of upsert to
            # avoid overwriting the inject task's operation / result / verification.
            # finalize_recover_verification will later set it to "recovered".
            try:
                from chaos_agent.persistence.task_store import get_task_store
                store = await get_task_store()
                await store.update_task_state(inject_task_id, "recovering")
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
                sys.stderr.write("  ⏳ Fault recovery in progress — the AI is recovering and verifying, please wait...\n")
                sys.stderr.flush()
            result = await self._agents["recover"].ainvoke(initial_state, config)

            from chaos_agent.agent.result.operation_result import (
                build_recover_cli_data_from_state,
            )

            recover_data = build_recover_cli_data_from_state(
                result if isinstance(result, dict) else {},
                inject_task_id,
                state_values,
            )
            if recover_data.get("result") == "failed":
                recover_failed = True
                error_msg = recover_data.get("error") or "Recovery verification failed"
                recover_fail_data = {**recover_data, "error": error_msg}
                # RECOVERY_FAILED (4xxx operational), matching the remote
                # path in cli/client.py — the legacy raw 5000 surfaced an
                # operational failure under an internal-error code.
                return JSONEnvelope.fail(
                    code=ResponseCode.RECOVERY_FAILED,
                    message=error_msg,
                    data=recover_fail_data,
                )

            return JSONEnvelope.ok(data=recover_data)

        except (KeyboardInterrupt, asyncio.CancelledError):
            # Signal-interrupted run (SIGTERM cancel / Ctrl-C): the graph
            # never finished, so the row must leave its mid-graph word —
            # and the session must hear the SAME classified word, or one
            # abort event ships two words (the r55 F2 split, CLI edition).
            abort_word = _signal_interrupted_word()
            try:
                await _write_signal_interrupted_row(record_task_id)
            except Exception:
                logger.warning(
                    "Failed to write interrupted terminal word for %s "
                    "(zombie-row risk)", record_task_id,
                )
            raise

        except Exception as e:
            recover_failed = True
            code, msg = _format_error(e)
            logger.exception(f"Local recover failed for task {inject_task_id}")
            from chaos_agent.agent.result.operation_result import (
                build_recover_cli_failure_data_from_state,
            )

            return JSONEnvelope.fail(
                code=code,
                message=f"Recovery failed: {msg}",
                data=build_recover_cli_failure_data_from_state(
                    inject_task_id,
                    state_values,
                    experiment_uid=experiment_uid or "",
                    error=f"internal_error: Recovery failed: {msg}",
                ),
            )
        finally:
            if _sigterm_cleanup is not None:
                _sigterm_cleanup()
            # Finalize session: flush remaining messages from final graph state
            if self._session_store:
                from chaos_agent.memory.session_finalizer import (
                    RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
                    finalize_recover_session,
                )

                await finalize_recover_session(
                    self._session_store,
                    self._agents["recover"],
                    config,
                    record_task_id,
                    inject_task_id,
                    state_values,
                    result_summary_mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
                    # Round-64 F1: the interrupt path's classified word
                    # outranks the boolean exits — an interrupted run is
                    # not a failed one, and never the optimistic
                    # "completed" this expression used to spell for every
                    # BaseException exit (the runner's two arms above).
                    default_status=(
                        abort_word or ("failed" if recover_failed else "completed")
                    ),
                )
            done_event.set()
            await printer_task
            unsubscribe(record_task_id, status_queue)
            remove_tracker(record_task_id)

    # ---- recover_stream ----

    async def recover_stream(self, task_id: str, **kwargs):
        """Stream recover execution, yielding StreamEvent objects in real-time.

        Streaming twin of ``recover()``: same setup/finalization contract
        (TaskStore state transition, recover session record, ledger-aware
        session finalize), but the recover graph runs under
        ``astream_events`` so the CLI renders LLM tokens and tool results
        as they happen instead of a static "in progress" line.

        The recover graph has no confirmation interrupt, so unlike
        ``inject_stream`` there is no resume loop — one streaming pass,
        then the final envelope is rebuilt from the graph's final state
        and yielded as a ``result`` event (same JSONEnvelope shape that
        ``recover()`` returns, so ``--output`` formatting and exit codes
        are identical between streaming and non-streaming calls).

        Yields:
            StreamEvent: token, thinking, tool_start, tool_end,
            node_message, result, error
        """
        await self._ensure_initialized()

        # Reset module-level time_wait state for the new recover task.
        from chaos_agent.tools.wait import reset_wait_state
        reset_wait_state()

        inject_task_id = task_id
        record_task_id = new_recover_task_id()
        config = {"configurable": {"thread_id": inject_task_id}, "recursion_limit": settings.recursion_limit}

        status_queue = subscribe(record_task_id)
        done_event = asyncio.Event()
        printer_task = asyncio.create_task(_status_printer(status_queue, done_event))

        # Pre-declare in case fallback path is taken or an early exception fires.
        experiment_uid = ""
        state_values: dict = {}
        # Round-63 R63-1: the CLI envelope's session status consumes
        # default_status, so the failure exits must say WHICH exit ran —
        # otherwise a failed recovery records "completed" on its session
        # (the envelope's own data.result says failed/unverified).
        recover_failed = False
        # Round-64 F1: the streaming twin of recover()'s interrupt word —
        # an interrupt is the THIRD terminal case the boolean cannot
        # spell, and it outranks both boolean exits.
        abort_word = ""

        # Orphan-row guard: SIGTERM's default kill disposition leaves the
        # inject row AND this run's recover session at their last mid-run
        # write (see _install_sigterm_cancel_guard).
        _sigterm_cleanup = _install_sigterm_cancel_guard()

        try:
            current_state = await self._agents["pipeline"].aget_state(config)
            checkpoint_values = current_state.values if current_state and current_state.values else {}

            from chaos_agent.agent.result.task_snapshot import resolve_recover_initial_state

            resolution = await resolve_recover_initial_state(
                inject_task_id,
                record_task_id=record_task_id,
                agents=self._agents,
                checkpoint_values=checkpoint_values,
                kubeconfig_override=kwargs.get("kubeconfig") or None,
            )
            if resolution is None:
                yield StreamEvent(
                    type="error",
                    content=f"Task not recoverable: {inject_task_id}",
                    task_id=record_task_id,
                )
                yield StreamEvent(
                    type="result",
                    content=json.dumps(JSONEnvelope.fail(
                        code=ResponseCode.TASK_NOT_FOUND,
                        message=f"Task not recoverable: {inject_task_id}",
                    ), ensure_ascii=False),
                    task_id=record_task_id,
                )
                return

            initial_state = resolution.initial_state
            state_values = resolution.source_values
            experiment_uid = initial_state.get("experiment_uid") or ""
            inject_tui_session_id = initial_state.get("tui_session_id", "") or ""

            try:
                from chaos_agent.persistence.task_store import get_task_store
                store = await get_task_store()
                await store.update_task_state(inject_task_id, "recovering")
            except Exception:
                logger.warning(f"Failed to write recover state to TaskStore for {inject_task_id}")

            if self._session_store:
                inject_messages = state_values.get("messages", [])
                self._session_store.create_session(
                    record_task_id,
                    operation="recover",
                    tui_session_id=inject_tui_session_id,
                    parent_task_id=inject_task_id,
                    baseline_messages=inject_messages,
                )

            # Stream the recover graph (includes two-layer verification).
            async for event in self._agents["recover"].astream_events(
                initial_state, config, version="v2"
            ):
                for stream_evt in parse_stream_events(event):
                    stream_evt.task_id = record_task_id
                    yield stream_evt

            # astream_events does not return final values the way ainvoke
            # does — rebuild them from the checkpoint.
            final_snapshot = await self._agents["recover"].aget_state(config)
            result_values = (
                final_snapshot.values if final_snapshot and final_snapshot.values else {}
            )

            from chaos_agent.agent.result.operation_result import (
                build_recover_cli_data_from_state,
            )

            recover_data = build_recover_cli_data_from_state(
                result_values,
                inject_task_id,
                state_values,
            )
            if recover_data.get("result") == "failed":
                recover_failed = True
                error_msg = recover_data.get("error") or "Recovery verification failed"
                envelope = JSONEnvelope.fail(
                    code=ResponseCode.RECOVERY_FAILED,
                    message=error_msg,
                    data={**recover_data, "error": error_msg},
                )
            else:
                envelope = JSONEnvelope.ok(data=recover_data)

            yield StreamEvent(
                type="result",
                content=json.dumps(envelope, ensure_ascii=False),
                task_id=record_task_id,
            )

        except (KeyboardInterrupt, asyncio.CancelledError):
            # Signal-interrupted run (SIGTERM cancel / Ctrl-C): the graph
            # never finished, so the row must leave its mid-graph word —
            # and the session must hear the SAME classified word, or one
            # abort event ships two words (the r55 F2 split, CLI edition).
            abort_word = _signal_interrupted_word()
            try:
                await _write_signal_interrupted_row(record_task_id)
            except Exception:
                logger.warning(
                    "Failed to write interrupted terminal word for %s "
                    "(zombie-row risk)", record_task_id,
                )
            raise

        except Exception as e:
            recover_failed = True
            code, msg = _format_error(e)
            logger.exception(f"Local recover_stream failed for task {inject_task_id}")
            from chaos_agent.agent.result.operation_result import (
                build_recover_cli_failure_data_from_state,
            )

            yield StreamEvent(
                type="error",
                content=f"Recovery failed: {msg}",
                task_id=record_task_id,
            )
            yield StreamEvent(
                type="result",
                content=json.dumps(JSONEnvelope.fail(
                    code=code,
                    message=f"Recovery failed: {msg}",
                    data=build_recover_cli_failure_data_from_state(
                        inject_task_id,
                        state_values,
                        experiment_uid=experiment_uid or "",
                        error=f"internal_error: Recovery failed: {msg}",
                    ),
                ), ensure_ascii=False),
                task_id=record_task_id,
            )
        finally:
            if _sigterm_cleanup is not None:
                _sigterm_cleanup()
            if self._session_store:
                from chaos_agent.memory.session_finalizer import (
                    RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
                    finalize_recover_session,
                )

                await finalize_recover_session(
                    self._session_store,
                    self._agents["recover"],
                    config,
                    record_task_id,
                    inject_task_id,
                    state_values,
                    result_summary_mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
                    # Round-64 F1: the interrupt path's classified word
                    # outranks the boolean exits — an interrupted run is
                    # not a failed one, and never the optimistic
                    # "completed" this expression used to spell for every
                    # BaseException exit (the runner's two arms above).
                    default_status=(
                        abort_word or ("failed" if recover_failed else "completed")
                    ),
                )
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

        # Create a lightweight LLM instance for catalog generation.
        # enable_thinking=False: single-shot structured catalog output —
        # reasoning tokens only add latency (same rationale as
        # capabilities_cmd; bench_thinking.py) — but only for models
        # strong enough to absorb the disable (>= 1M window); weak
        # models keep thinking ON (factory.aux_calls_can_skip_thinking).
        from chaos_agent.agent.factory import (
            aux_calls_can_skip_thinking,
            make_llm,
        )
        llm = make_llm(
            temperature=0.3,
            max_retries=2,
            read_timeout=60,
            enable_thinking=False if aux_calls_can_skip_thinking() else None,
        )

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
                    categories_dict[uc_cat]["description"] = f"{uc_cat} fault-injection use cases"
                    categories_dict[uc_cat]["faults"].append({
                        "fault_type": extract_fault_type(uc_cat),
                        "use_case_name": uc["use_case_name"],
                        "fault_symptom": uc["fault_symptom"],
                        "resource_path": uc["resource_path"],
                        "example_cmd": uc["example_cmd"],
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
                categories_dict[cat]["faults"].append({
                    "fault_type": extract_fault_type(cat),
                    "name": name.replace("-", " ").title(),
                    "description": (
                        meta.description.split(".")[0] if meta.description else ""
                    ),
                    "example_cmd": nl_cmd,
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

        # Orphan-row guard (see _install_sigterm_cancel_guard): resuming a
        # paused graph is a graph run like any other — a SIGTERM here used
        # to kill the process with the row still at its last mid-graph word.
        _sigterm_cleanup = _install_sigterm_cancel_guard()

        try:
            from langgraph.types import Command

            resume_value = "approved" if action == "approve" else "rejected"
            final = await self._agents["pipeline"].ainvoke(Command(resume=resume_value), config)

            # Connected defect 2 (round-64): the docstring promises "the
            # returned task_state reflects the final state", but the shape
            # only carried {task_id, action, reason, confirmed_at} — so the
            # CLI's two-phase confirm (which replaces ``result`` with this
            # envelope) printed a bare "approved" with no verdict, and the
            # user could not tell an injected drill from a rejected one.
            # Read the resumed graph through the SAME single-source
            # projection every other terminal surface uses; the resume ran
            # the pipeline to its own verdict, so the snapshot is terminal
            # (next empty) and the word is real, not the paused placeholder.
            snapshot = await self._agents["pipeline"].aget_state(config)
            from chaos_agent.agent.result.operation_result import build_inject_data_from_state

            inject_data = build_inject_data_from_state(
                final if isinstance(final, dict) else {}, task_id, snapshot=snapshot,
            )
            task_state = inject_data.get("task_state")

            return JSONEnvelope.ok(
                data={
                    "task_id": task_id,
                    "action": action,
                    "reason": reason,
                    "confirmed_at": now_iso(),
                    "task_state": task_state,
                    "result": task_state,
                    "error": inject_data.get("error", ""),
                },

            )

        except (KeyboardInterrupt, asyncio.CancelledError):
            # Signal-interrupted run (SIGTERM cancel / Ctrl-C): the graph
            # never finished, so the row must leave its mid-graph word.
            try:
                await _write_signal_interrupted_row(task_id)
            except Exception:
                logger.warning(
                    "Failed to write interrupted terminal word for %s "
                    "(zombie-row risk)", task_id,
                )
            raise

        except Exception as e:
            code, msg = _format_error(e)
            logger.exception(f"Local confirm failed for task {task_id}")
            return JSONEnvelope.fail(code=code, message=f"Task not found or confirm failed: {msg}")
        finally:
            if _sigterm_cleanup is not None:
                _sigterm_cleanup()
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
        can count ``skill_count``. No LLM, no checkpointer,
        no prerequisites.
        """
        if self._registry is None:
            self._registry = SkillRegistry()
            self._registry.load_from_directory(get_skills_dir())
        return JSONEnvelope.ok(
            data={
                "version": __version__,
                "skill_count": len(self._registry),
            },
        )
