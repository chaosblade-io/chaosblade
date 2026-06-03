"""L4ResilienceAgent — the main L4 adapter for blade-ai.

Implements the L4 lifecycle (prepare/execute/cleanup/cancel) by
wrapping blade-ai's LangGraph inject/recover graphs and driving
runtime.step() via astream_events phase event interception.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from logging.handlers import RotatingFileHandler

from chaos_agent.l4.adapter import (
    build_recover_initial_state,
    make_trajectory_id,
    state_to_task_result,
    test_task_to_initial_state,
)
from chaos_agent.l4.error_mapping import (
    _build_step_result_from_error,
    map_error_class,
    map_to_agent_error,
)
from chaos_agent.l4.schemas import L4TaskResult

# phase_started node → runtime.step() name mapping.
# Only nodes wrapped with with_phase_events() emit events.
# direct_setup, load_memory, save_memory do NOT emit phase events.
_PHASE_STEP_MAP: dict[str, str] = {
    "intent_clarification": "planning",
    "plan_builder": "planning",
    "agent_loop": "planning",
    "safety_check": "safety_check",
    "confirmation_gate": "approval_gate",
    "intent_confirm": "approval_gate",
    "baseline_capture": "baseline_capture",
    "execute_loop": "fault_injection",
    "direct_execute": "fault_injection",
    "verifier_loop": "verification",
    "finalize_verification": "verification",
}


_logging_configured = False


def _setup_logging() -> None:
    """Configure file-based logging for L4 SDK mode (idempotent)."""
    global _logging_configured
    if _logging_configured:
        return
    _logging_configured = True

    from chaos_agent.config.settings import settings

    log_dir = settings.resolved_memory_dir / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    log_path = log_dir / "l4.log"
    try:
        handler = RotatingFileHandler(
            log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
    except Exception:
        return

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.addHandler(handler)
    level_name = (settings.log_level or "INFO").upper()
    root.setLevel(getattr(logging, level_name, logging.INFO))

    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class _CancelRequested(Exception):
    """Internal: break out of event loop into try/finally cleanup."""


class _ChaosAgentPool:
    """Holds compiled inject/recover graphs.

    Uses MemorySaver instead of AsyncSqliteSaver because L4 execute()
    creates a new event loop per call via asyncio.run(). AsyncSqliteSaver's
    aiosqlite connection binds to the init loop and fails in a new one.
    MemorySaver is pure-dict, no IO binding.
    """

    inject_graph = None
    recover_graph = None
    skill_registry = None
    _initialized = False
    _init_lock = threading.Lock()

    def ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            from langgraph.checkpoint.memory import MemorySaver

            from chaos_agent.agent.factory import create_agent
            from chaos_agent.skills.loader import get_skills_dir
            from chaos_agent.skills.registry import SkillRegistry

            registry = SkillRegistry()
            skills_dir = get_skills_dir()
            if skills_dir.exists():
                registry.load_from_directory(skills_dir)

            checkpointer = MemorySaver()
            agents = asyncio.run(create_agent(registry, checkpointer=checkpointer))
            self.inject_graph = agents["inject"]
            self.recover_graph = agents["recover"]
            self.skill_registry = registry
            self._initialized = True


class L4ResilienceAgent:
    """blade-ai L4 adapter layer.

    Does NOT inherit BaseTestAgent (avoids circular dependency).
    Implements the same method signatures; ai-testing-platform's
    ResilienceAgent delegates to this object via composition.
    """

    def __init__(self) -> None:
        self._pool: _ChaosAgentPool | None = None
        self._cancel_event = threading.Event()
        self._completed: dict[str, L4TaskResult] = {}
        self._state_transitions_buffer: list[dict] = []

    # --- Lifecycle ---

    def prepare(self, runtime, task) -> None:
        """Pre-check: initialize graph pool, validate K8s/ChaosBlade."""
        self._ensure_pool()

    def execute(self, runtime, task) -> L4TaskResult:
        """Main entry: TestTask → inject graph → TaskResult.

        B3 idempotent: same task_id returns cached result.
        """
        if task.task_id in self._completed:
            return self._completed[task.task_id]
        self._state_transitions_buffer = []
        # _ensure_pool() MUST be called in sync context (before asyncio.run)
        # because ensure_initialized() internally uses asyncio.run(create_agent(...))
        pool = self._ensure_pool()
        result = asyncio.run(self._async_execute(pool, runtime, task))
        if result.status in ("passed", "failed", "cancelled", "degraded"):
            self._completed[task.task_id] = result
            # FIFO eviction: drop oldest-inserted entry when over capacity.
            # B3 idempotent cache; repeated task_id is rare in production.
            if len(self._completed) > 100:
                oldest_inserted = next(iter(self._completed))
                del self._completed[oldest_inserted]
        return result

    def cleanup(self, runtime, task) -> None:
        """Clean up per-task state."""
        self._cancel_event.clear()

    def request_cancel(self) -> None:
        """Cooperative cancel: set event, 3s guarantee."""
        self._cancel_event.set()

    def is_cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    # --- Internal ---

    def _ensure_pool(self) -> _ChaosAgentPool:
        """Lazy-init graph pool. Compiles inject/recover graphs on first call."""
        if self._pool is None:
            _setup_logging()
            self._pool = _ChaosAgentPool()
        self._pool.ensure_initialized()
        return self._pool

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

        current_step = None
        current_step_cm = None
        step_attrs_accumulator: dict = {}
        self._state_transitions_buffer = []

        async def _process_event(event: dict) -> None:
            nonlocal current_step, current_step_cm, step_attrs_accumulator
            kind = event.get("event", "")

            if kind == "on_custom_event":
                name = event.get("name")
                data = event.get("data", {})
                if name == "phase_started" and runtime:
                    node = data.get("node", "")
                    phase = data.get("phase", "")
                    if current_step_cm:
                        for k, v in step_attrs_accumulator.items():
                            current_step.attrs[k] = v
                        current_step_cm.__exit__(None, None, None)
                    cm = runtime.step(
                        _PHASE_STEP_MAP.get(node, node),
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
                    for k, v in step_attrs_accumulator.items():
                        current_step.attrs[k] = v
                    current_step_cm.__exit__(None, None, None)
                    current_step = None
                    current_step_cm = None
                    step_attrs_accumulator = {}
                    self._state_transitions_buffer.append(
                        {
                            "from_phase": data.get("phase", ""),
                            "event": "completed",
                            "node": node,
                            "timestamp": time.time(),
                        }
                    )

            elif kind == "on_tool_start" and runtime:
                tool_name = event.get("name", "")
                tool_input = event.get("data", {}).get("input", {})
                step_attrs_accumulator[f"tool.{tool_name}.input"] = str(tool_input)[
                    :500
                ]
                if hasattr(runtime, "emit_event"):
                    runtime.emit_event("tool_start", {
                        "message": f"调用工具: {tool_name}",
                        "tool": tool_name,
                        "input": str(tool_input)[:500],
                    })

            elif kind == "on_tool_end" and runtime:
                tool_name = event.get("name", "")
                output = event.get("data", {}).get("output", "")
                step_attrs_accumulator[f"tool.{tool_name}.status"] = "ok"
                if hasattr(runtime, "emit_event"):
                    runtime.emit_event("tool_end", {
                        "message": f"工具返回: {tool_name}",
                        "level": "ok",
                        "tool": tool_name,
                        "summary": str(output)[:2000],
                    })
                if tool_name in ("blade_create", "blade_status", "kubectl"):
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
                output = event.get("data", {}).get("output")
                if output is None:
                    return
                msg = None
                if hasattr(output, "generations"):
                    gens = output.generations
                    if gens and gens[0]:
                        first = gens[0][0]
                        msg = first.message if hasattr(first, "message") else None
                elif hasattr(output, "additional_kwargs"):
                    msg = output
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
                if msg and hasattr(runtime, "emit_event"):
                    content = ""
                    if hasattr(msg, "content"):
                        content = msg.content if isinstance(msg.content, str) else str(msg.content)
                    # Thinking models (Qwen enable_thinking) put the chain-of-
                    # thought in reasoning_content with empty content on tool-call
                    # turns. Fall back to reasoning so the live stream still shows
                    # the model's thinking on those turns, not just the final answer.
                    rc = ""
                    if hasattr(msg, "additional_kwargs"):
                        rc = msg.additional_kwargs.get("reasoning_content", "") or ""
                    if content:
                        runtime.emit_event("llm_thought", {
                            "message": content[:500],
                            "content": content[:3000],
                        })
                    elif rc:
                        runtime.emit_event("llm_thought", {
                            "message": rc[:500],
                            "content": rc[:3000],
                        })

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

        # --- StatusTracker → runtime bridge ---
        # Subscribe to blade-ai's internal tracker events and forward
        # them as user-facing progress messages to the platform runtime.
        # Only COMPLETED/FAILED events are forwarded (skip high-frequency
        # STARTED/RUNNING updates and debug-only events).
        _tracker_task: asyncio.Task | None = None
        _tracker_queue = None
        if runtime and hasattr(runtime, "emit_event"):
            try:
                from chaos_agent.observability.status_tracker import subscribe

                _tracker_queue = subscribe(task.task_id)

                async def _drain_tracker():
                    while True:
                        try:
                            ev = await _tracker_queue.get()
                        except asyncio.CancelledError:
                            return
                        if ev is None:
                            return
                        phase = ev.phase
                        msg = ev.message or ""
                        source = ev.source or ""
                        detail = ev.detail or {}
                        # Skip: no message, debug-only events, high-freq updates
                        if not msg:
                            continue
                        if detail.get("debug"):
                            continue
                        if phase not in ("completed", "failed"):
                            continue
                        level = "ok" if phase == "completed" else "error"
                        try:
                            runtime.emit_event("agent_progress", {
                                "message": f"[{source}] {msg}",
                                "source": source,
                                "phase": phase,
                                "level": level,
                                "duration_ms": ev.duration_ms,
                            })
                        except Exception:
                            pass

                _tracker_task = asyncio.create_task(_drain_tracker())
            except Exception:
                _tracker_queue = None

        # --- Main execution flow ---
        try:
            async for event in pool.inject_graph.astream_events(
                initial_state, config, version="v2"
            ):
                await _process_event(event)

            # Handle GraphInterrupt from confirmation_gate
            state = await pool.inject_graph.aget_state(config)
            while state.tasks and any(t.interrupts for t in state.tasks):
                payload = task.payload or {}
                if payload.get("pre_approved"):
                    resume_value = "approved"
                elif runtime and hasattr(runtime, "require_approval"):
                    approval = runtime.require_approval(risk_level="high")
                    resume_value = "approved" if approval else "rejected"
                else:
                    resume_value = "approved"

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
            if _tracker_task and not _tracker_task.done():
                _tracker_task.cancel()
                try:
                    await _tracker_task
                except asyncio.CancelledError:
                    pass
            if _tracker_queue is not None:
                try:
                    from chaos_agent.observability.status_tracker import unsubscribe
                    unsubscribe(task.task_id, _tracker_queue)
                except Exception:
                    pass
            if current_step_cm:
                current_step_cm.__exit__(None, None, None)
                current_step = None
                current_step_cm = None

        # Populate trajectory
        state = await pool.inject_graph.aget_state(config)
        if runtime and hasattr(runtime, "trajectory") and runtime.trajectory:
            self._populate_trajectory(runtime, state.values, trajectory_id, task)

        # Build final TaskResult
        result = state_to_task_result(state.values, task.task_id, trajectory_id)

        # Budget check (C5)
        if runtime:
            result = self._check_budget(runtime, result, state.values)

        # Emit structured conclusion event so the platform UI shows a
        # clear result card (not buried in "模型思考").
        if runtime and hasattr(runtime, "emit_event"):
            verification = state.values.get("verification") or {}
            level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
            blade_uid = state.values.get("blade_uid", "")
            runtime.emit_event("conclusion", {
                "message": (
                    f"故障注入{'成功' if result.status == 'passed' else '失败'}"
                    f" | 验证级别: {level}"
                    f"{f' | blade_uid: {blade_uid}' if blade_uid else ''}"
                ),
                "status": result.status,
                "level": level,
                "blade_uid": blade_uid,
                "trajectory_id": trajectory_id,
                "summary": result.summary or "",
            })

        # Note: runtime.finish() is intentionally NOT called here.
        # It is invoked once by the outer _async_execute() finally block with
        # the FINAL status (post-recovery), to keep trajectory.status in sync
        # with the platform's TaskResult.status.

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
        inject_state = await pool.inject_graph.aget_state(config)
        if not inject_state or not inject_state.values:
            return inject_result

        recover_initial = build_recover_initial_state(inject_state.values, task.task_id)
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
            from chaos_agent.agent.state import infer_task_state

            recover_task_state = infer_task_state(recover_result)
            if recover_task_state in ("recovered", "partial_recovered"):
                inject_result.status = (
                    "passed" if recover_task_state == "recovered" else "degraded"
                )
                inject_result.extras["recovery_level"] = recover_task_state
                inject_result.extras["recover_verification"] = recover_result.get(
                    "recover_verification"
                )

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
        from chaos_agent.agent.state import infer_task_state

        task_state = infer_task_state(values)
        verification = values.get("verification", {})
        replan_count = values.get("replan_count", 0)

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
            "coverage": 1.0 if values.get("skill_name") else 0.5,
            "flake_score": min(1.0, replan_count / 3.0),
            "assert_confidence": level_confidence.get(ver_level, 0.3),
            "tool_success_rate": (1.0 if not values.get("error") else 0.5),
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
                    recover_initial = build_recover_initial_state(state.values, task_id)
                    recover_config = {
                        "configurable": {"thread_id": f"recover-{task_id}"},
                        "recursion_limit": 150,
                    }
                    await pool.recover_graph.ainvoke(recover_initial, recover_config)
        except Exception:
            pass
