"""L4 execution and recovery lifecycle implementation."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
import warnings
from typing import TYPE_CHECKING

from chaos_agent.l4.adapter import (
    make_trajectory_id,
    state_to_task_result,
    test_task_to_initial_state,
)
from chaos_agent.l4.cards import interrupt_to_card
from chaos_agent.l4.constants import DEFAULT_CARD_DECISION_TIMEOUT_S
from chaos_agent.l4.error_mapping import (
    _build_step_result_from_error,
    map_error_class,
    map_to_agent_error,
)
from chaos_agent.l4.events import (
    _PHASE_STEP_MAP,
    _extract_aimessage,
    _extract_pending_interrupt_payload,
    _forward_progress_event,
    _normalize_langgraph_event,
)
from chaos_agent.l4.pool import _ChaosAgentPool
from chaos_agent.l4.schemas import L4AgentError, L4TaskResult, PendingCard

if TYPE_CHECKING:
    from collections.abc import Callable


logger = logging.getLogger(__name__)


class _CancelRequested(Exception):
    """Internal signal used to leave the event loop for emergency recovery."""


class _L4ExecutionMixin:
    async def _async_execute(
        self,
        pool: _ChaosAgentPool,
        runtime,
        task,
        healed: bool = False,
    ) -> L4TaskResult:
        """Full execution: inject → [interrupt] → [auto recover] → result.

        pool is passed from execute() to avoid nested asyncio.run().

        runtime.finish() is called exactly once in the finally block of the
        outermost call (healed=False), with the FINAL status (post-recovery).
        This prevents the trajectory from being persisted with stale inject-phase
        status when recovery downgrades the result to "degraded".

        healed: marks self-heal recursion to avoid double finish and double heal.
        """
        trajectory_id = make_trajectory_id(task.task_id)
        initial_state = test_task_to_initial_state(task)
        config = {
            "configurable": {"thread_id": task.task_id},
            "recursion_limit": 150,
        }
        final_result: L4TaskResult | None = None

        try:
            inject_result = await self._run_inject_with_runtime(
                pool, runtime, initial_state, config, task, trajectory_id
            )
            if inject_result.status in ("failed", "cancelled"):
                final_result = inject_result
                return final_result

            payload = task.payload or {}
            if payload.get("auto_recover", True):
                final_result = await self._run_recover_with_runtime(
                    pool, runtime, config, task, trajectory_id, inject_result
                )
                return final_result

            final_result = inject_result
            return final_result

        except Exception as e:
            # C3 self-heal: single retry only
            if runtime and hasattr(runtime, "heal") and not healed:
                step_result = _build_step_result_from_error(e)
                heal_result = runtime.heal(step_result, error_class=map_error_class(e))
                if heal_result and getattr(heal_result, "healed", False):
                    final_result = await self._async_execute(
                        pool, runtime, task, healed=True
                    )
                    return final_result
            final_result = L4TaskResult(
                task_id=task.task_id,
                status="failed",
                trajectory_id=trajectory_id,
                error=map_to_agent_error(e),
            )
            return final_result

        finally:
            # Only the outermost call (healed=False) is responsible for finish().
            # Recursive heal calls return their result up; the outer finally
            # then persists the trajectory with the final status.
            if (
                not healed
                and runtime is not None
                and hasattr(runtime, "finish")
                and final_result is not None
            ):
                try:
                    runtime.finish(status=final_result.status)
                except Exception:
                    pass

    async def _run_inject_with_runtime(
        self,
        pool: _ChaosAgentPool,
        runtime,
        initial_state: dict,
        config: dict,
        task,
        trajectory_id: str,
    ) -> L4TaskResult:
        """Run inject graph, intercept phase events for runtime.step(),
        and handle confirmation_gate GraphInterrupt."""
        from langgraph.types import Command

        # Ensure the TracingCallback attributes LLM token usage to this task.
        # Graph nodes use get_tracker() (not track_status context manager), so
        # _tracing_callback.set_task_id() is never called within nodes — we must
        # do it here before the graph starts executing.
        try:
            from chaos_agent.observability.status_tracker import _tracing_callback
            if _tracing_callback is not None:
                _tracing_callback.set_task_id(task.task_id)
        except Exception:
            pass

        # Also ensure the trace object exists in _traces so on_llm_end records
        # don't go to a throwaway TaskTrace.
        try:
            from chaos_agent.observability.tracer import get_trace
            await get_trace(task.task_id)
        except Exception:
            pass

        current_step = None
        current_step_cm = None
        step_attrs_accumulator: dict = {}
        self._state_transitions_buffer = []
        _pending_phase_completed: dict | None = None  # deferred phase_completed event

        def _emit_deferred_phase_completed() -> None:
            """Emit the buffered phase_completed to the platform.

            Called when the step container is truly being closed (transition
            to a different phase or final cleanup).
            """
            nonlocal _pending_phase_completed
            if _pending_phase_completed and runtime and hasattr(runtime, "emit_event"):
                runtime.emit_event("phase_completed", _pending_phase_completed)
            _pending_phase_completed = None

        async def _process_event(event: dict) -> None:
            nonlocal current_step, current_step_cm, step_attrs_accumulator
            nonlocal _pending_phase_completed
            kind = event.get("event", "")

            # ----- Side effects (trajectory step / accumulators / log mirror) -----
            # Channel-specific bookkeeping that does NOT belong in the shared
            # ``_normalize_langgraph_event`` parser. Each branch is self-contained:
            # progress emission below is unified.
            if kind == "on_custom_event":
                name = event.get("name")
                data = event.get("data", {})
                if name == "phase_started" and runtime:
                    node = data.get("node", "")
                    phase = data.get("phase", "")
                    target_step = _PHASE_STEP_MAP.get(node, node)
                    # Step 容器复用策略：
                    # 同一 step 名（如 ``agent_loop`` → ``planning``）多次进入
                    # 时复用现有 ``runtime.step`` 容器,不关闭也不新建。这样
                    # LangGraph 在两次 ``with_phase_events`` 之间路由到无 phase
                    # 包装的 ``ToolNode``(``phase1_tools`` / ``phase2_tools`` /
                    # ``clarification_tools``) 时，工具事件能挂入仍然 running
                    # 的 step 容器，而不是被切碎到顶层。仅当切换到不同 step
                    # 名（如 planning → baseline_capture）时才关闭旧容器、
                    # 新建新容器。
                    current_step_name = getattr(current_step, "name", None) if current_step else None
                    if current_step_cm and current_step_name == target_step:
                        # 同名 step 已在跑，复用容器。仅记录 transition 用于审计。
                        self._state_transitions_buffer.append(
                            {
                                "from_phase": phase,
                                "event": "started",
                                "node": node,
                                "timestamp": time.time(),
                                "reused": True,
                            }
                        )
                    else:
                        if current_step_cm:
                            for k, v in step_attrs_accumulator.items():
                                current_step.attrs[k] = v
                            _emit_deferred_phase_completed()
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
                        self._state_transitions_buffer.append(
                            {
                                "from_phase": phase,
                                "event": "started",
                                "node": node,
                                "timestamp": time.time(),
                            }
                        )
                elif name == "phase_completed" and current_step_cm:
                    node = data.get("node", "")
                    # 不立即关闭 step 容器：等下一次 ``phase_started`` 切换到
                    # 不同 step 名时再统一关闭，或在主循环 finally 兆底收尾。
                    # 这样同一 LangGraph node 多次进出（如 agent_loop 的 N 次
                    # ReAct 迭代）以及紧随其后的 ToolNode 工具调用都属于同一
                    # 容器,前端 timeline 不会出现"planning 完成 (Nms)"卡片
                    # 之间夹着裸工具卡的现象。
                    # 缓存 phase_completed 事件，在容器真正关闭时补发给平台。
                    phase_name = data.get("phase", "")
                    _pending_phase_completed = {
                        "kind": "phase_completed",
                        "node": node,
                        "phase": phase_name,
                        "message": f"Phase complete: {phase_name or node}",
                    }
                    self._state_transitions_buffer.append(
                        {
                            "from_phase": phase_name,
                            "event": "completed",
                            "node": node,
                            "timestamp": time.time(),
                        }
                    )

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
                if tool_name in ("blade_create", "blade_status", "kubectl") and runtime:
                    try:
                        runtime.tool.execute(
                            "sls_write_logs",
                            {
                                "task_id": task.task_id,
                                "tool": tool_name,
                                "phase": "inject",
                                "output_preview": str(output)[:1000],
                            },
                        )
                    except Exception:
                        pass

            elif kind == "on_chat_model_end" and runtime and current_step:
                # Persist reasoning_content into the trajectory thought_trace
                # so the postmortem includes the model's chain-of-thought.
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
                                    "action": "decide",
                                },
                            )()
                        )

            # ----- Unified progress emission via shared normalizer -----
            # Same parser as the clarify path's ``_forward_progress_event``.
            # Adding/changing events only requires touching one function.
            if runtime and hasattr(runtime, "emit_event"):
                for ev in _normalize_langgraph_event(event):
                    runtime.emit_event(ev["kind"], ev)

            if self._cancel_event.is_set():
                raise _CancelRequested()

        # --- Bootstrap session for task file persistence ---
        try:
            from chaos_agent.memory.session_store import get_global_session_store
            _store = get_global_session_store()
            if _store and not _store.has_active(task.task_id):
                _store.create_session(task.task_id, operation="inject")
        except Exception:
            pass

        # --- Main execution flow ---
        try:
            async for event in pool.inject_graph.astream_events(
                initial_state, config, version="v2"
            ):
                await _process_event(event)

            # Handle GraphInterrupt from confirmation_gate / intent_confirm /
            # plan_change_confirm / tool_screener.
            #
            # Resolution order (v0.5.0):
            #   1. ``runtime.present_card(card)`` — preferred; upper layer
            #      surfaces a structured card to the user and returns
            #      ``{"decision": ..., "answer": ...}``.
            #   2. ``payload.get("pre_approved")`` — legacy auto-approve;
            #      DeprecationWarning emitted on use.
            #   3. ``runtime.require_approval`` — legacy boolean callback.
            #   4. None of the above — fail-closed ``rejected`` (was
            #      ``approved`` in <=0.4.x; flipped for safety).
            state = await pool.inject_graph.aget_state(config)
            while state.tasks and any(t.interrupts for t in state.tasks):
                interrupt_payload = _extract_pending_interrupt_payload(state)
                resume_value = await self._resolve_interrupt_decision(
                    runtime=runtime,
                    interrupt_payload=interrupt_payload,
                    payload=task.payload or {},
                    thread_id=task.task_id,
                )

                async for event in pool.inject_graph.astream_events(
                    Command(resume=resume_value), config, version="v2"
                ):
                    await _process_event(event)

                state = await pool.inject_graph.aget_state(config)

        except _CancelRequested:
            await self._emergency_recover(pool, task.task_id, config)
            return L4TaskResult(
                task_id=task.task_id,
                status="cancelled",
                trajectory_id=trajectory_id,
            )
        finally:
            if current_step_cm:
                _emit_deferred_phase_completed()
                current_step_cm.__exit__(None, None, None)
                current_step = None
                current_step_cm = None

        # Populate trajectory
        state = await pool.inject_graph.aget_state(config)
        if runtime and hasattr(runtime, "trajectory") and runtime.trajectory:
            self._populate_trajectory(runtime, state.values, trajectory_id, task)

        # Build final TaskResult
        result = state_to_task_result(state.values, task.task_id, trajectory_id)

        # Budget guard removed per user decision — token overspend
        # should not downgrade a successful injection result.

        # Emit inject conclusion event.
        if runtime and hasattr(runtime, "emit_event"):
            from chaos_agent.agent.result.operation_outcome import read_inject_verification

            verification = read_inject_verification(state.values) or {}
            level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
            blade_uid = state.values.get("blade_uid", "")
            _status_text_map = {
                "passed": "succeeded",
                "degraded": "succeeded (degraded)",
                "failed": "failed",
            }
            status_text = _status_text_map.get(result.status, "failed")
            runtime.emit_event("conclusion", {
                "message": (
                    f"Fault injection {status_text}"
                    f" | verification level: {level}"
                    f"{f' | blade_uid: {blade_uid}' if blade_uid else ''}"
                ),
                "status": result.status,
                "level": level,
                "blade_uid": blade_uid,
                "trajectory_id": trajectory_id,
                "summary": result.summary or "",
                "postmortem": (result.extras or {}).get("postmortem"),
            })

        return result

    async def _run_recover_with_runtime(
        self,
        pool: _ChaosAgentPool,
        runtime,
        config: dict,
        task,
        trajectory_id: str,
        inject_result: L4TaskResult,
    ) -> L4TaskResult:
        """Auto recover: read inject final state → build recover state → run."""
        # Attribute LLM token usage to this task during recover
        try:
            from chaos_agent.observability.status_tracker import _tracing_callback
            if _tracing_callback is not None:
                _tracing_callback.set_task_id(task.task_id)
        except Exception:
            pass

        inject_state = await pool.inject_graph.aget_state(config)
        if not inject_state or not inject_state.values:
            return inject_result

        from chaos_agent.agent.result.task_snapshot import resolve_recover_initial_state

        resolution = await resolve_recover_initial_state(
            task.task_id,
            record_task_id=f"recover-{task.task_id}",
            agents={"skill_registry": pool.skill_registry},
            checkpoint_values=inject_state.values,
        )
        if resolution is None:
            return inject_result
        recover_initial = resolution.initial_state
        recover_config = {
            "configurable": {"thread_id": f"recover-{task.task_id}"},
            "recursion_limit": 150,
        }

        recover_result = None
        if runtime:
            with runtime.step(
                "auto_recover", attrs={"trajectory_id": trajectory_id}
            ) as sr:
                try:
                    recover_result = await pool.recover_graph.ainvoke(
                        recover_initial, recover_config
                    )
                    sr.attrs["recovery_status"] = "completed"
                except Exception as e:
                    sr.attrs["recovery_error"] = str(e)
        else:
            recover_result = await pool.recover_graph.ainvoke(
                recover_initial, recover_config
            )

        if recover_result:
            from chaos_agent.agent.result.operation_outcome import read_recover_verification
            from chaos_agent.agent.state import infer_task_state

            recover_task_state = infer_task_state(recover_result)
            if recover_task_state in ("recovered", "partial_recovered"):
                inject_result.status = (
                    "passed" if recover_task_state == "recovered" else "degraded"
                )
                inject_result.extras["recovery_level"] = recover_task_state
                inject_result.extras["recover_verification"] = (
                    read_recover_verification(recover_result)
                )

            # Emit recover conclusion event.
            if runtime and hasattr(runtime, "emit_event"):
                _recover_status_map = {
                    "recovered": "succeeded",
                    "partial_recovered": "succeeded (partial recovery)",
                    "failed": "failed",
                }
                recover_text = _recover_status_map.get(recover_task_state, "completed")
                recover_level = "ok" if recover_task_state == "recovered" else (
                    "warn" if recover_task_state == "partial_recovered" else "error"
                )
                blade_uid = inject_result.extras.get("blade_uid", "")
                runtime.emit_event("conclusion", {
                    "message": (
                        f"Fault recovery {recover_text}"
                        f" | recovery level: {recover_task_state}"
                        f"{f' | blade_uid: {blade_uid}' if blade_uid else ''}"
                    ),
                    "status": inject_result.status,
                    "level": recover_level,
                    "recovery_level": recover_task_state,
                    "trajectory_id": trajectory_id,
                    "summary": inject_result.summary or "",
                })

        return inject_result

    def _populate_trajectory(
        self, runtime, values: dict, trajectory_id: str, task
    ) -> None:
        """Fill runtime.trajectory agent-specific fields (D2)."""
        traj = runtime.trajectory

        if hasattr(traj, "state_transitions"):
            for t in self._state_transitions_buffer:
                traj.state_transitions.append(t)

        if hasattr(traj, "tool_call_chain"):
            from chaos_agent.observability.tracer import _traces

            trace = _traces.get(task.task_id)
            if trace:
                for i, span in enumerate(trace.spans):
                    for tc in span.tool_calls:
                        traj.tool_call_chain.append(
                            type(
                                "ToolCall",
                                (),
                                {
                                    "seq": i,
                                    "tool_name": tc,
                                    "elapsed_ms": span.duration_ms,
                                    "status": ("ok" if not span.error else "failed"),
                                },
                            )()
                        )

        if hasattr(traj, "context_window"):
            from chaos_agent.observability.tracer import _traces

            trace = _traces.get(task.task_id)
            if trace:
                traj.context_window = {
                    "total_input": trace.total_token_input,
                    "total_output": trace.total_token_output,
                    "llm_calls": trace.total_llm_calls,
                }

        if hasattr(traj, "eval_report"):
            metrics = self._derive_metrics(values)
            for k, v in metrics.items():
                if hasattr(traj.eval_report, k):
                    setattr(traj.eval_report, k, v)

        traj.agent_id = "resilience"
        traj.agent_type = "resilience"
        traj.trajectory_id = trajectory_id

    def _derive_metrics(self, values: dict) -> dict:
        """Derive 9+1 metrics (D4)."""
        from chaos_agent.agent.spec.fault_spec import fault_type_from_state
        from chaos_agent.agent.result.operation_outcome import (
            read_inject_verification,
            read_operation_outcome,
        )
        from chaos_agent.agent.state import infer_task_state

        task_state = infer_task_state(values)
        verification = read_inject_verification(values) or {}
        replan_count = values.get("replan_count", 0)
        verify_replan_count = values.get("verify_replan_count", 0)

        ver_level = (
            verification.get("level", "unknown")
            if isinstance(verification, dict)
            else "unknown"
        )
        level_confidence = {
            "verified": 1.0,
            "partial": 0.7,
            "unverified": 0.0,
            "unknown": 0.3,
        }

        duration_ms = 0
        created_at = values.get("created_at", "")
        finished_at = values.get("finished_at", "")
        if created_at and finished_at:
            try:
                from chaos_agent.utils.time import parse_iso_timestamp

                ct = parse_iso_timestamp(created_at)
                ft = parse_iso_timestamp(finished_at)
                duration_ms = int((ft - ct).total_seconds() * 1000)
            except Exception:
                pass

        return {
            "success_rate": (1.0 if task_state in ("injected", "recovered") else 0.0),
            "coverage": 1.0 if fault_type_from_state(values) else 0.5,
            "flake_score": min(1.0, (replan_count + verify_replan_count) / 3.0),
            "assert_confidence": level_confidence.get(ver_level, 0.3),
            "tool_success_rate": (1.0 if not read_operation_outcome(values).error else 0.5),
            "avg_duration_ms": duration_ms,
            "token_efficiency": 0,
            "recovery_rate": (
                1.0
                if task_state == "recovered"
                else (0.5 if task_state == "partial_recovered" else 0.0)
            ),
            "blast_radius_score": 0.5,
        }

    def _check_budget(
        self, runtime, result: L4TaskResult, values: dict
    ) -> L4TaskResult:
        """Budget check (C5): downgrade on overspend."""
        from chaos_agent.observability.tracer import _traces

        trace = _traces.get(result.task_id)
        if trace:
            max_tokens = 50000
            if trace.total_token_input + trace.total_token_output > max_tokens:
                result.extras["budget_exceeded"] = "tokens"
                result.status = (
                    "degraded" if result.status == "passed" else result.status
                )
        return result

    async def _emergency_recover(
        self, pool: _ChaosAgentPool, task_id: str, config: dict
    ) -> None:
        """Emergency recover: best-effort destroy lingering blade experiment."""
        try:
            state = await pool.inject_graph.aget_state(config)
            if state and state.values:
                blade_uid = state.values.get("blade_uid", "")
                if blade_uid:
                    from chaos_agent.agent.result.task_snapshot import resolve_recover_initial_state

                    resolution = await resolve_recover_initial_state(
                        task_id,
                        record_task_id=f"recover-{task_id}",
                        agents={"skill_registry": pool.skill_registry},
                        checkpoint_values=state.values,
                    )
                    if resolution is None:
                        return
                    recover_initial = resolution.initial_state
                    recover_config = {
                        "configurable": {"thread_id": f"recover-{task_id}"},
                        "recursion_limit": 150,
                    }
                    await pool.recover_graph.ainvoke(recover_initial, recover_config)
        except Exception:
            pass

    # --- Human-in-the-loop card protocol (v0.5.0) ---

    async def _resolve_interrupt_decision(
        self,
        runtime,
        interrupt_payload,
        payload: dict,
        thread_id: str,
    ) -> str:
        """Pick a resume value for one interrupt.

        Returns ``"approved"`` or ``"rejected"``.

        Resolution order:
          1. ``runtime.present_card(card)``
          2. ``payload.pre_approved`` (legacy, DeprecationWarning)
          3. ``runtime.require_approval`` (legacy)
          4. fail-closed ``rejected``
        """
        # 1) Preferred: structured card callback
        if runtime is not None and hasattr(runtime, "present_card"):
            card = interrupt_to_card(interrupt_payload, thread_id)
            timeout_s = float(
                payload.get("card_decision_timeout") or DEFAULT_CARD_DECISION_TIMEOUT_S
            )
            try:
                decision = await self._invoke_present_card(
                    runtime, card, timeout_s=timeout_s
                )
            except asyncio.TimeoutError:
                logging.getLogger(__name__).warning(
                    "present_card timeout (%.1fs) on card %s; fail-closed rejected.",
                    timeout_s, card.card_id,
                )
                return "rejected"
            except Exception:
                logging.getLogger(__name__).exception(
                    "present_card raised on card %s; fail-closed rejected.",
                    card.card_id,
                )
                return "rejected"
            if decision is not None:
                return "approved" if decision == "approved" else "rejected"
            # decision is None → callback not registered, fall through

        # 2) Legacy auto-approve
        if payload.get("pre_approved"):
            warnings.warn(
                "TestTask.payload.pre_approved is deprecated; upper layers "
                "should implement runtime.present_card(card) for human-in-"
                "the-loop confirmation. Auto-approve will be removed in 0.6.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            return "approved"

        # 3) Legacy require_approval boolean
        if runtime is not None and hasattr(runtime, "require_approval"):
            try:
                approval = runtime.require_approval(risk_level="high")
            except Exception:
                approval = False
            return "approved" if approval else "rejected"

        # 4) Fail-closed
        return "rejected"

    async def _invoke_present_card(
        self,
        runtime,
        card: PendingCard,
        timeout_s: float,
    ) -> str | None:
        """Call ``runtime.present_card(card)`` with timeout.

        Supports both sync and async ``present_card`` implementations.
        Returns the decision string (``"approved"`` / ``"rejected"``) or
        ``None`` when the runtime returned ``None`` (no callback wired).
        """
        fn = getattr(runtime, "present_card", None)
        if fn is None:
            return None

        if inspect.iscoroutinefunction(fn):
            result = await asyncio.wait_for(fn(card), timeout=timeout_s)
        else:
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, fn, card),
                timeout=timeout_s,
            )

        if result is None:
            return None
        if isinstance(result, dict):
            decision = result.get("decision")
            if decision in ("approved", "rejected"):
                return decision
            # SDK contract: decision must be approved/rejected. Anything
            # else (including ``request_modify``) is treated as rejected
            # at the SDK boundary; upper layer is responsible for the
            # rejected → clarify(user_feedback) chaining.
            return "rejected"
        if isinstance(result, str) and result in ("approved", "rejected"):
            return result
        return "rejected"

    async def _drive_until_interrupt(
        self,
        graph,
        graph_input,
        config: dict,
        *,
        on_event: "Callable[[dict], None] | None" = None,
    ) -> tuple[dict, PendingCard | None, dict | None]:
        """Drive ``graph`` to the next interrupt or END.

        Args:
            graph: a compiled LangGraph
            graph_input: ``initial_state`` dict OR ``Command(resume=...)`` /
                ``Command(update=...)``
            config: ``{"configurable": {"thread_id": ...}}``
            on_event: optional callback invoked with progress dicts for
                tool_start / tool_end / node transitions. The platform
                uses this to relay intermediate progress to the SSE bus.

        Returns:
            ``(state.values, pending_card_or_None, token_usage_or_None)``
            — pending_card is non-None when an interrupt is reached.
            — token_usage aggregates all LLM calls during this drive.
        """
        prompt_tokens = 0
        completion_tokens = 0

        async for event in graph.astream_events(graph_input, config, version="v2"):
            if on_event is not None:
                _forward_progress_event(event, on_event)

            # Accumulate token usage from LLM responses
            if event.get("event") == "on_chat_model_end":
                output = event.get("data", {}).get("output")
                if output is not None:
                    # LangChain AIMessage carries usage_metadata
                    um = getattr(output, "usage_metadata", None)
                    if um and isinstance(um, dict):
                        prompt_tokens += um.get("input_tokens", 0)
                        completion_tokens += um.get("output_tokens", 0)
                    # Fallback: response_metadata.token_usage
                    elif hasattr(output, "response_metadata"):
                        tu = (output.response_metadata or {}).get("token_usage")
                        if tu and isinstance(tu, dict):
                            prompt_tokens += tu.get("prompt_tokens", 0)
                            completion_tokens += tu.get("completion_tokens", 0)
                        else:
                            logger.warning(
                                "_drive_until_interrupt: on_chat_model_end fired but "
                                "no usage found. usage_metadata=%r, response_metadata=%r",
                                um, getattr(output, "response_metadata", None),
                            )
                    else:
                        logger.warning(
                            "_drive_until_interrupt: on_chat_model_end output has "
                            "no usage_metadata and no response_metadata. type=%s",
                            type(output).__name__,
                        )

        state = await graph.aget_state(config)
        values = state.values or {}

        total = prompt_tokens + completion_tokens

        # Fallback: if astream_events did not capture usage (e.g. DashScope
        # streaming or certain LangGraph versions that don't reliably fire
        # on_chat_model_end with usage_metadata), extract from the last AI
        # message(s) produced during this drive.
        if total == 0:
            from langchain_core.messages import AIMessage as _AIMsg

            # Count how many messages were in the input so we only look at
            # NEW messages generated by this drive.
            if isinstance(graph_input, dict):
                prev_count = len(graph_input.get("messages") or [])
            else:
                # Command(update=...) — we appended 1 message to existing state
                # so count all but the last as "previous".
                prev_count = max(0, len(values.get("messages", [])) - 2)

            new_messages = (values.get("messages") or [])[prev_count:]
            for msg in new_messages:
                if isinstance(msg, _AIMsg):
                    um = getattr(msg, "usage_metadata", None)
                    if um and isinstance(um, dict):
                        prompt_tokens += um.get("input_tokens", 0)
                        completion_tokens += um.get("output_tokens", 0)
                    elif hasattr(msg, "response_metadata"):
                        tu = (msg.response_metadata or {}).get("token_usage")
                        if tu and isinstance(tu, dict):
                            prompt_tokens += tu.get("prompt_tokens", 0)
                            completion_tokens += tu.get("completion_tokens", 0)
            total = prompt_tokens + completion_tokens
            if total == 0:
                # Log the first AI message's metadata for debugging
                for msg in new_messages:
                    if isinstance(msg, _AIMsg):
                        logger.warning(
                            "_drive_until_interrupt: AI message has no token info. "
                            "usage_metadata=%r, response_metadata keys=%r",
                            getattr(msg, "usage_metadata", None),
                            list((getattr(msg, "response_metadata", None) or {}).keys()),
                        )
                        break

        token_usage = (
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total,
            }
            if total > 0
            else None
        )

        if state.tasks and any(t.interrupts for t in state.tasks):
            payload = _extract_pending_interrupt_payload(state)
            thread_id = (config.get("configurable") or {}).get("thread_id", "")
            card = interrupt_to_card(payload, thread_id)
            return values, card, token_usage

        return values, None, token_usage
