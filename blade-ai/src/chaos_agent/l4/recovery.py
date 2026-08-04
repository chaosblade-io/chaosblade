"""Explicit recovery flow for the L4 adapter."""

from __future__ import annotations

import logging
import uuid

from chaos_agent.l4.adapter import make_trajectory_id
from chaos_agent.l4.error_mapping import map_to_agent_error
from chaos_agent.l4.events import (
    _PHASE_STEP_MAP,
    _extract_aimessage,
    _normalize_langgraph_event,
)
from chaos_agent.l4.execution import _CancelRequested
from chaos_agent.l4.pool import _ChaosAgentPool
from chaos_agent.l4.schemas import L4AgentError, L4TaskResult

logger = logging.getLogger(__name__)


class _L4RecoveryMixin:
    async def _async_recover_explicit(
        self,
        pool: _ChaosAgentPool,
        runtime,
        task,
    ) -> L4TaskResult:
        """Explicit recover: read inject checkpoint → build recover state → run recover graph."""
        from chaos_agent.agent.state import infer_task_state

        # Attribute LLM token usage to this task
        try:
            from chaos_agent.observability.status_tracker import _tracing_callback
            if _tracing_callback is not None:
                _tracing_callback.set_task_id(task.task_id)
        except Exception:
            pass
        try:
            from chaos_agent.observability.tracer import get_trace
            await get_trace(task.task_id)
        except Exception:
            pass

        inject_task_id = (task.payload or {}).get("inject_task_id", "")
        if not inject_task_id:
            return L4TaskResult(
                task_id=task.task_id,
                status="failed",
                error=L4AgentError(
                    code="MISSING_INJECT_TASK_ID",
                    message="payload.inject_task_id is required for recover",
                    recoverable=False,
                ),
            )

        trajectory_id = make_trajectory_id(task.task_id)

        # Read inject graph final state as optional live context. Persistent
        # TaskSnapshot data remains the primary recovery source.
        inject_config = {
            "configurable": {"thread_id": inject_task_id},
            "recursion_limit": 150,
        }
        inject_state = await pool.inject_graph.aget_state(inject_config)
        checkpoint_values = inject_state.values if inject_state and inject_state.values else {}

        record_task_id = f"task-{uuid.uuid4()}"  # Same naming as CLI/HTTP recover
        from chaos_agent.agent.result.task_snapshot import resolve_recover_initial_state

        resolution = await resolve_recover_initial_state(
            inject_task_id,
            record_task_id=record_task_id,
            agents={"skill_registry": pool.skill_registry},
            checkpoint_values=checkpoint_values,
            kubeconfig_override=task.payload.get("kubeconfig") or None,
        )
        if resolution is None:
            return L4TaskResult(
                task_id=task.task_id,
                status="failed",
                trajectory_id=trajectory_id,
                error=L4AgentError(
                    code="INJECT_STATE_NOT_FOUND",
                    message=(
                        f"Cannot find recoverable inject state for task_id={inject_task_id}. "
                        "The inject execution may not exist, or both checkpoint and task snapshot are unavailable."
                    ),
                    recoverable=False,
                ),
            )

        recover_initial = resolution.initial_state
        source_values = resolution.source_values

        recover_config = {
            "configurable": {"thread_id": record_task_id},
            "recursion_limit": 150,
        }
        from chaos_agent.memory.session_finalizer import (
            RESULT_SUMMARY_RECOVER_PAYLOAD,
            finalize_recover_session,
        )

        # --- Bootstrap session_store for task file persistence ---
        _session_store = None
        try:
            from chaos_agent.memory.session_store import get_global_session_store
            _session_store = get_global_session_store()
            if _session_store:
                inject_messages = source_values.get("messages", [])
                _session_store.create_session(
                    record_task_id,
                    operation="recover",
                    tui_session_id=recover_initial.get("tui_session_id", "") or "",
                    parent_task_id=inject_task_id,
                    baseline_messages=inject_messages,
                )
        except Exception:
            logger.debug("Failed to bootstrap session_store for recover %s", record_task_id)

        # Run recover graph with streaming events (aligned with inject flow)
        # Uses astream_events so phase transitions, tool calls, and LLM
        # reasoning are emitted to the platform in real time — same as
        # _run_inject_with_runtime.
        recover_result = None
        current_step = None
        current_step_cm = None
        step_attrs_accumulator: dict = {}
        _pending_phase_completed_r: dict | None = None  # deferred phase_completed event

        def _emit_deferred_phase_completed_r() -> None:
            """Emit the buffered phase_completed (recover path)."""
            nonlocal _pending_phase_completed_r
            if _pending_phase_completed_r and runtime and hasattr(runtime, "emit_event"):
                runtime.emit_event("phase_completed", _pending_phase_completed_r)
            _pending_phase_completed_r = None

        async def _process_recover_event(event: dict) -> None:
            nonlocal current_step, current_step_cm, step_attrs_accumulator
            nonlocal _pending_phase_completed_r
            kind = event.get("event", "")

            if kind == "on_custom_event":
                name = event.get("name")
                data = event.get("data", {})
                if name == "phase_started" and runtime:
                    node = data.get("node", "")
                    phase = data.get("phase", "")
                    target_step = _PHASE_STEP_MAP.get(node, node)
                    current_step_name = (
                        getattr(current_step, "name", None)
                        if current_step
                        else None
                    )
                    if current_step_cm and current_step_name == target_step:
                        # Reuse same step container
                        pass
                    else:
                        if current_step_cm:
                            for k, v in step_attrs_accumulator.items():
                                current_step.attrs[k] = v
                            _emit_deferred_phase_completed_r()
                            current_step_cm.__exit__(None, None, None)
                        cm = runtime.step(
                            target_step,
                            attrs={
                                "phase": phase,
                                "trajectory_id": trajectory_id,
                            },
                        )
                        current_step = cm.__enter__()
                        current_step_cm = cm
                        step_attrs_accumulator = {}
                elif name == "phase_completed" and current_step_cm:
                    node = data.get("node", "")
                    phase_name = data.get("phase", "")
                    _pending_phase_completed_r = {
                        "kind": "phase_completed",
                        "node": node,
                        "phase": phase_name,
                        "message": f"Phase complete: {phase_name or node}",
                    }

            elif kind == "on_tool_start":
                tool_name = event.get("name", "")
                tool_input = event.get("data", {}).get("input", {})
                step_attrs_accumulator[f"tool.{tool_name}.input"] = str(tool_input)[
                    :500
                ]

            elif kind == "on_tool_end":
                tool_name = event.get("name", "")
                output = event.get("data", {}).get("output", "")
                step_attrs_accumulator[f"tool.{tool_name}.status"] = "ok"
                if (
                    tool_name in ("blade_destroy", "blade_status", "kubectl")
                    and runtime
                ):
                    try:
                        runtime.tool.execute(
                            "sls_write_logs",
                            {
                                "task_id": task.task_id,
                                "tool": tool_name,
                                "phase": "recover",
                                "output_preview": str(output)[:1000],
                            },
                        )
                    except Exception:
                        pass

            elif kind == "on_chat_model_end" and runtime and current_step:
                msg = _extract_aimessage(event.get("data", {}).get("output"))
                if msg and hasattr(msg, "additional_kwargs"):
                    rc = msg.additional_kwargs.get("reasoning_content", "")
                    if (
                        rc
                        and hasattr(runtime, "trajectory")
                        and runtime.trajectory
                        and hasattr(runtime.trajectory, "thought_trace")
                    ):
                        runtime.trajectory.thought_trace.append(
                            type(
                                "ThoughtStep",
                                (),
                                {
                                    "seq": len(runtime.trajectory.thought_trace) + 1,
                                    "thought": rc[:500],
                                    "action": "recover",
                                },
                            )()
                        )

            # Unified progress emission (same as inject)
            if runtime and hasattr(runtime, "emit_event"):
                for ev in _normalize_langgraph_event(event):
                    runtime.emit_event(ev["kind"], ev)

            if self._cancel_event.is_set():
                raise _CancelRequested()

        try:
            async for event in pool.recover_graph.astream_events(
                recover_initial, recover_config, version="v2"
            ):
                await _process_recover_event(event)

            # Close last step container
            if current_step_cm:
                for k, v in step_attrs_accumulator.items():
                    current_step.attrs[k] = v
                _emit_deferred_phase_completed_r()
                current_step_cm.__exit__(None, None, None)
                current_step_cm = None

            # Get final state after streaming completes
            recover_state = await pool.recover_graph.aget_state(recover_config)
            recover_result = (
                recover_state.values
                if recover_state and recover_state.values
                else {}
            )
        except Exception as e:
            # Close step container on error
            if current_step_cm:
                try:
                    _emit_deferred_phase_completed_r()
                    current_step_cm.__exit__(None, None, None)
                except Exception:
                    pass
            logger.exception("Explicit recover failed for inject_task_id=%s", inject_task_id)
            await finalize_recover_session(
                _session_store,
                pool.recover_graph,
                recover_config,
                record_task_id,
                inject_task_id,
                source_values,
                result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
                default_status="failed",
                error_log_level="debug",
            )
            return L4TaskResult(
                task_id=task.task_id,
                status="failed",
                trajectory_id=trajectory_id,
                error=map_to_agent_error(e),
            )

        # Interpret result
        if not recover_result:
            await finalize_recover_session(
                _session_store,
                pool.recover_graph,
                recover_config,
                record_task_id,
                inject_task_id,
                source_values,
                result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
                default_status="failed",
                error_log_level="debug",
            )
            return L4TaskResult(
                task_id=task.task_id,
                status="failed",
                trajectory_id=trajectory_id,
            )

        recover_task_state = infer_task_state(recover_result)
        status = "failed"
        if recover_task_state == "recovered":
            status = "passed"
        elif recover_task_state == "partial_recovered":
            status = "degraded"

        from chaos_agent.agent.result.operation_outcome import read_recover_verification

        # Finalize session_store with recover result
        await finalize_recover_session(
            _session_store,
            pool.recover_graph,
            recover_config,
            record_task_id,
            inject_task_id,
            source_values,
            result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
            default_status="completed" if status != "failed" else "failed",
            error_log_level="debug",
            precomputed_values=recover_result,
        )

        extras: dict = {
            "recovery_level": recover_task_state,
            "recover_verification": read_recover_verification(recover_result),
            "inject_task_id": inject_task_id,
            "blade_uid": recover_initial.get("blade_uid", ""),
        }

        # Token usage from recover graph
        token_usage = self._extract_token_usage_from_state(recover_result)
        if token_usage:
            extras["token_usage"] = token_usage

        return L4TaskResult(
            task_id=task.task_id,
            status=status,
            trajectory_id=trajectory_id,
            extras=extras,
        )

    @staticmethod
    def _extract_token_usage_from_state(state_values: dict) -> dict | None:
        """Best-effort extract token usage from graph state messages."""
        try:
            from langchain_core.messages import AIMessage
            total_prompt = 0
            total_completion = 0
            call_count = 0
            for msg in state_values.get("messages", []):
                if isinstance(msg, AIMessage):
                    usage = getattr(msg, "usage_metadata", None)
                    if usage:
                        total_prompt += usage.get("input_tokens", 0)
                        total_completion += usage.get("output_tokens", 0)
                        call_count += 1
            if call_count > 0:
                return {
                    "prompt_tokens": total_prompt,
                    "completion_tokens": total_completion,
                    "total_tokens": total_prompt + total_completion,
                    "call_count": call_count,
                }
        except Exception:
            pass
        return None
