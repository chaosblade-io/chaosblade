"""L4ResilienceAgent — the main L4 adapter for blade-ai.

Implements the L4 lifecycle (prepare/execute/cleanup/cancel) by
wrapping blade-ai's LangGraph inject/recover graphs and driving
runtime.step() via astream_events phase event interception.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
import uuid
import warnings
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

from chaos_agent.l4.adapter import (
    make_trajectory_id,
    state_to_task_result,
    test_task_to_initial_state,
)
from chaos_agent.l4.cards import interrupt_to_card
from chaos_agent.l4.error_mapping import (
    _build_step_result_from_error,
    map_error_class,
    map_to_agent_error,
)
from chaos_agent.l4.schemas import (
    ClarifyResult,
    L4AgentError,
    L4TaskResult,
    PendingCard,
    StepResult,
)


# Default timeout for ``runtime.present_card()`` callback. Upper layers
# typically resolve cards within seconds; 600s gives ample buffer for
# slow human reviewers before SDK falls back to ``rejected``.
DEFAULT_CARD_DECISION_TIMEOUT_S: float = 600.0


def _extract_pending_interrupt_payload(graph_state) -> object | None:
    """Return the first unresolved interrupt's payload, or None."""
    if not graph_state or not graph_state.tasks:
        return None
    for task in graph_state.tasks:
        interrupts = getattr(task, "interrupts", None) or ()
        for it in interrupts:
            value = getattr(it, "value", None)
            if value is not None:
                return value
    return None


# ---------------------------------------------------------------------------
# Progress event forwarding — astream_events v2 → on_event callback
# ---------------------------------------------------------------------------

# Tool names to surface as progress events (user-facing).
_TOOL_DISPLAY_NAMES: dict[str, str] = {
    "kubectl_ro": "查询集群资源",
    "read_skill_resource": "读取故障场景",
    "activate_skill": "激活故障技能",
}


def _extract_aimessage(output: Any) -> Any | None:
    """Pull the AIMessage from a LangGraph ``on_chat_model_end`` ``data.output``.

    The output may be:
      * an ``LLMResult`` with ``.generations[0][0].message``
      * an ``AIMessage`` directly
      * ``None`` / something else
    """
    if output is None:
        return None
    if hasattr(output, "generations"):
        gens = output.generations
        if gens and gens[0]:
            first = gens[0][0]
            return getattr(first, "message", None)
    if hasattr(output, "additional_kwargs"):
        return output
    return None


# 与 TUI streaming.py 的 _SILENT_TOKEN_NODES 保持一致：
# 这些节点的 LLM 输出以专用卡片展示（如 postmortem），流式 token 会重复。
_SILENT_TOKEN_NODES: frozenset = frozenset({"save_memory"})


def _is_silent_node(event: dict) -> bool:
    """Check if the event originates from a node whose tokens should not stream."""
    tags = event.get("tags", [])
    for tag in tags:
        if tag.startswith("langsmith:nodes:"):
            return tag.split(":")[-1] in _SILENT_TOKEN_NODES
    metadata = event.get("metadata", {})
    node = metadata.get("langgraph_node", "")
    return node in _SILENT_TOKEN_NODES


def _normalize_langgraph_event(event: dict) -> list[dict]:
    """Single source of truth for parsing LangGraph ``astream_events(v2)``
    events into normalized progress dicts.

    Shared by both progress channels:
      * clarify path → ``_forward_progress_event`` → ``on_event(dict)`` callback
      * inject/recover path → ``_process_event`` → ``runtime.emit_event(kind, dict)``

    Adding a new event surface (or changing field shape) only requires
    touching this function — both channels stay in lockstep.

    Returns a list because some upstream events expand into multiple
    progress events (notably ``on_chat_model_end`` → ``llm_thought`` +
    ``llm_end``). Returns ``[]`` when the event is intentionally dropped
    (e.g. ``on_chain_start`` outside the small whitelist, ``on_chain_end``,
    retriever / parser / prompt events, unknown custom events).
    """
    kind = event.get("event", "")
    name = event.get("name", "")
    data = event.get("data") or {}

    if kind == "on_tool_start":
        display = _TOOL_DISPLAY_NAMES.get(name, name)
        ev: dict = {
            "kind": "tool_start",
            "tool_name": name,
            "message": f"正在调用: {display}",
        }
        tool_input = data.get("input")
        if tool_input is not None:
            ev["input"] = tool_input
        return [ev]

    if kind == "on_tool_end":
        display = _TOOL_DISPLAY_NAMES.get(name, name)
        ev = {
            "kind": "tool_end",
            "tool_name": name,
            "message": f"完成: {display}",
            "level": "ok",
        }
        output = data.get("output")
        if output is not None:
            content = getattr(output, "content", None)
            if content is None and isinstance(output, dict):
                content = output.get("content")
            if content is None:
                content = str(output)
            if isinstance(content, str) and len(content) > 2000:
                content = content[:2000] + "...(truncated)"
            ev["output"] = content
        return [ev]

    if kind == "on_chain_start" and name in (
        "load_memory", "intent_confirm", "save_memory",
    ):
        return [{
            "kind": "node_start",
            "node_name": name,
            "message": f"进入节点: {name}",
        }]

    if kind == "on_chat_model_start":
        # save_memory 节点的输出以 postmortem 卡片展示，流式 token 会重复。
        if _is_silent_node(event):
            return []
        # 发送 llm_start 让前端创建 thinking 节点，后续 llm_token 会累积到该节点。
        return [{"kind": "llm_start", "message": ""}]

    if kind == "on_chat_model_stream":
        # save_memory 节点静默（见上）
        if _is_silent_node(event):
            return []
        # 流式 token chunk：前端已实现 token 聚合（按 llm_start → llm_thought
        # 聚到同一 thinking 卡片），恢复逐 token 下发以支持流式展示。
        chunk = data.get("chunk")
        if chunk is None:
            return []

        # enable_thinking 模式（Qwen 等）：思考内容在 additional_kwargs.reasoning_content
        additional_kwargs = getattr(chunk, "additional_kwargs", {}) or {}
        reasoning_content = additional_kwargs.get("reasoning_content", "")
        if reasoning_content:
            return [{"kind": "llm_token", "message": reasoning_content}]

        content = getattr(chunk, "content", "") or ""
        if isinstance(content, list):
            text_parts = [
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            content = "".join(text_parts)
        if not content:
            return []
        return [{"kind": "llm_token", "message": content}]

    if kind == "on_chat_model_end":
        msg = _extract_aimessage(data.get("output"))
        if msg is None:
            return []
        content = ""
        if hasattr(msg, "content"):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
        rc = ""
        if hasattr(msg, "additional_kwargs"):
            rc = msg.additional_kwargs.get("reasoning_content") or ""
        usage = getattr(msg, "usage_metadata", None)

        events: list[dict] = []
        # ``llm_thought``: surfaces final answer or chain-of-thought. Thinking
        # models (Qwen enable_thinking, etc.) put CoT in ``reasoning_content``
        # with empty ``content`` on tool-call turns — fall back to ``rc`` so
        # the live stream still shows reasoning on those turns.
        # 同时把 token usage / reasoning 原文挂到 llm_thought 上，避免再
        # 单独发一帧 ``llm_end``（"AI 思考完成"卡片相对 llm_thought 整段
        # 是冗余收尾，对用户无信息增量）。
        # message 统一前缀 ``💭 模型思考：``，让前端 timeline 一眼分辨
        # 这是模型推理而非工具调用 / 节点提示。完整内容仍在 ``content``
        # 字段（截 3000 字），前端可基于 content 做展开式展示。
        _THOUGHT_PREFIX = "💭 模型思考："
        if content:
            ev: dict = {
                "kind": "llm_thought",
                "message": f"{_THOUGHT_PREFIX}{content[:500]}",
                "content": content[:3000],
            }
            if isinstance(usage, dict):
                ev["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
            if isinstance(rc, str) and rc:
                ev["reasoning"] = rc[:3000]
            events.append(ev)
        elif rc:
            ev = {
                "kind": "llm_thought",
                "message": f"{_THOUGHT_PREFIX}{rc[:500]}",
                "content": rc[:5000],
                "reasoning": rc[:5000],
            }
            if isinstance(usage, dict):
                ev["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
            events.append(ev)
        # 不再发独立的 ``llm_end`` 卡片；如未来前端需要"思考完成"语义
        # 标记，可改为读 ``llm_thought`` 的存在与否做边界推断。
        return events

    if kind == "on_custom_event":
        # phase_started / phase_completed 仅在 ReAct 循环的 intent 阶段
        # 出现重复噪声（每次工具调用回到 intent node 都会再发一次）。
        # inject / recover 状态机里 baseline_capture / inject / verify /
        # recover / cleanup / postmortem 等 phase 每阶段只发 1 次，是
        # 真实的状态切换提示，必须保留以便前端展示节奏。
        if name in ("phase_started", "phase_completed"):
            phase_name = data.get("phase", "") or ""
            if phase_name == "intent":
                return []
            label = "阶段开始" if name == "phase_started" else "阶段完成"
            return [{
                "kind": name,
                "node": data.get("node", ""),
                "phase": phase_name,
                "message": f"{label}: {phase_name or data.get('node', '')}",
            }]
        if name == "node_message":
            content = data.get("content", "")
            if isinstance(content, str) and content:
                return [{
                    "kind": "node_message",
                    "node": data.get("node", ""),
                    "message": content,
                }]
            return []
        if name == "batch_fault_result":
            return [{
                "kind": "batch_fault_result",
                "message": "批量故障结果",
                "detail": data,
            }]
        return []

    return []


def _forward_progress_event(
    event: dict,
    on_event: "Callable[[dict], None]",
) -> None:
    """Thin wrapper used by the clarify path (lightweight ``on_event``
    callback). All parsing lives in :func:`_normalize_langgraph_event` so
    this channel stays byte-for-byte aligned with the inject/recover
    ``runtime.emit_event`` channel.
    """
    for ev in _normalize_langgraph_event(event):
        on_event(ev)


def _last_ai_message_text(values: dict) -> str:
    """Pull the last AIMessage content from graph state.messages."""
    msgs = values.get("messages") or []
    for msg in reversed(msgs):
        cls_name = type(msg).__name__
        if cls_name == "AIMessage":
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                return content
            return str(content) if content else ""
    return ""


def _conn_to_state_patch(conn: dict) -> dict:
    """Translate a ``conn`` dict (from platform) into LangGraph state patch.

    Only includes keys present in ``conn`` to avoid clobbering existing
    state values with empty strings. This is the wire-format used by both
    ``clarify(conn=...)`` (initial inject) and ``update_connection``
    (mid-conversation env switch).
    """
    patch: dict = {}
    for key in ("kubeconfig", "kube_context",
                "kubewiz_cluster_uuid", "kubewiz_profile"):
        if key in conn:
            patch[key] = conn.get(key) or ""
    return patch

# phase_started node → runtime.step() name mapping.
# Only nodes wrapped with with_phase_events() emit events.
# direct_setup, load_memory do NOT emit phase events.
# save_memory IS wrapped (phase="postmortem") — see graph.py.
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
    "save_memory": "postmortem",
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

    Uses MemorySaver: pure-dict, no IO/loop binding. Works for both
    sync entry (CLI — each ``asyncio.run()`` creates a fresh loop) and
    async entry (platform — stays in main loop across calls).
    """

    inject_graph = None
    intent_graph = None  # Intent Graph (dialogue layer: intent_clarification + intent_confirm)
    recover_graph = None
    skill_registry = None
    _initialized = False
    _init_lock = threading.Lock()
    # asyncio.Lock is created lazily on first async init call (cannot be
    # created at class-definition time — needs a running event loop).
    _async_init_lock: "asyncio.Lock | None" = None

    def _build_graphs_sync(self) -> dict:
        """Build skill registry + return create_agent kwargs (loop-agnostic).

        Extracted so both sync and async init paths share the heavy
        registry-loading logic without duplication.
        """
        from chaos_agent.skills.loader import get_skills_dir
        from chaos_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        skills_dir = get_skills_dir()
        if skills_dir.exists():
            registry.load_from_directory(skills_dir)
        return {"registry": registry}

    def _commit(self, agents: dict, registry) -> None:
        """Atomic commit of compiled graphs onto the class."""
        cls = type(self)
        cls.inject_graph = agents["pipeline"]
        cls.intent_graph = agents["intent"]
        cls.recover_graph = agents["recover"]
        cls.skill_registry = registry
        cls._initialized = True

    def ensure_initialized(self) -> None:
        """Sync init path — used by CLI and any sync caller.

        Uses ``asyncio.run()`` to drive the async ``create_agent``. Safe
        only when the calling thread has NO running event loop.
        """
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            from langgraph.checkpoint.memory import MemorySaver

            from chaos_agent.agent.factory import create_agent

            ctx = self._build_graphs_sync()
            registry = ctx["registry"]
            checkpointer = MemorySaver()
            agents = asyncio.run(create_agent(registry, checkpointer=checkpointer))
            self._commit(agents, registry)

    async def async_ensure_initialized(self) -> None:
        """Async init path — used by platform (stays in main loop).

        Uses ``asyncio.Lock`` for coroutine-safe single init. Sync
        ``threading.Lock`` would block the loop if another coroutine
        also tries to init.
        """
        if self._initialized:
            return
        cls = type(self)
        if cls._async_init_lock is None:
            cls._async_init_lock = asyncio.Lock()
        async with cls._async_init_lock:
            if self._initialized:
                return
            from langgraph.checkpoint.memory import MemorySaver

            from chaos_agent.agent.factory import create_agent

            ctx = self._build_graphs_sync()
            registry = ctx["registry"]
            checkpointer = MemorySaver()
            agents = await create_agent(registry, checkpointer=checkpointer)
            self._commit(agents, registry)


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

    async def async_prepare(self, runtime, task) -> None:
        """Async pre-check: initialize graph pool (stays in caller's loop)."""
        await self._async_ensure_pool()

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

    def recover(self, runtime, task) -> L4TaskResult:
        """Public entry for explicit fault recovery.

        ``task.payload`` must contain ``inject_task_id`` — the task_id of
        the inject execution whose fault we want to recover.

        Returns L4TaskResult with status in (recovered, partial_recovered, failed).
        """
        pool = self._ensure_pool()
        result = asyncio.run(self._async_recover_explicit(pool, runtime, task))
        return result

    async def async_execute(self, runtime, task) -> L4TaskResult:
        """Async main entry (stays in caller's loop).

        Same semantics as ``execute()`` but without ``asyncio.run()``.
        """
        if task.task_id in self._completed:
            return self._completed[task.task_id]
        self._state_transitions_buffer = []
        pool = await self._async_ensure_pool()
        result = await self._async_execute(pool, runtime, task)
        if result.status in ("passed", "failed", "cancelled", "degraded"):
            self._completed[task.task_id] = result
            if len(self._completed) > 100:
                oldest_inserted = next(iter(self._completed))
                del self._completed[oldest_inserted]
        return result

    async def async_recover(self, runtime, task) -> L4TaskResult:
        """Async public entry for explicit fault recovery (stays in caller's loop)."""
        pool = await self._async_ensure_pool()
        return await self._async_recover_explicit(pool, runtime, task)

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
        from chaos_agent.agent.task_snapshot import resolve_recover_initial_state

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

        # Run recover graph
        recover_result = None
        try:
            if runtime:
                with runtime.step(
                    "explicit_recover", attrs={"trajectory_id": trajectory_id}
                ) as sr:
                    recover_result = await pool.recover_graph.ainvoke(
                        recover_initial, recover_config
                    )
                    sr.attrs["recovery_status"] = "completed"
            else:
                recover_result = await pool.recover_graph.ainvoke(
                    recover_initial, recover_config
                )
        except Exception as e:
            logger.exception("Explicit recover failed for inject_task_id=%s", inject_task_id)
            self._finalize_recover_session(_session_store, record_task_id, None, "failed")
            return L4TaskResult(
                task_id=task.task_id,
                status="failed",
                trajectory_id=trajectory_id,
                error=map_to_agent_error(e),
            )

        # Interpret result
        if not recover_result:
            self._finalize_recover_session(_session_store, record_task_id, None, "failed")
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

        # Finalize session_store with recover result
        self._finalize_recover_session(
            _session_store, record_task_id, recover_result,
            "completed" if status != "failed" else "failed",
        )

        extras: dict = {
            "recovery_level": recover_task_state,
            "recover_verification": recover_result.get("recover_verification"),
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

    @staticmethod
    def _finalize_recover_session(
        session_store, record_task_id: str, result: dict | None, status: str,
    ) -> None:
        """Finalize session_store for a recover task (best-effort)."""
        if session_store is None:
            return
        try:
            remaining_messages = []
            if isinstance(result, dict):
                remaining_messages = result.get("messages", [])
            session_store.finalize_session(
                record_task_id,
                remaining_messages=remaining_messages,
                status=status,
            )
        except Exception:
            logger.debug("Failed to finalize recover session %s", record_task_id)

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

    async def _async_ensure_pool(self) -> _ChaosAgentPool:
        """Async lazy-init. For callers already in a running event loop."""
        if self._pool is None:
            _setup_logging()
            self._pool = _ChaosAgentPool()
        await self._pool.async_ensure_initialized()
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

        async def _process_event(event: dict) -> None:
            nonlocal current_step, current_step_cm, step_attrs_accumulator
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
                    # 不同 step 名时再统一关闭，或在主循环 finally 兜底收尾。
                    # 这样同一 LangGraph node 多次进出（如 agent_loop 的 N 次
                    # ReAct 迭代）以及紧随其后的 ToolNode 工具调用都属于同一
                    # 容器,前端 timeline 不会出现"planning 完成 (Nms)"卡片
                    # 之间夹着裸工具卡的现象。
                    self._state_transitions_buffer.append(
                        {
                            "from_phase": data.get("phase", ""),
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
                        if phase not in ("completed", "failed", "started"):
                            continue
                        # started 仅允许 postmortem 源穿透（避免高频 noise）
                        if phase == "started" and source != "postmortem":
                            continue
                        level = {"completed": "ok", "failed": "error", "started": "info"}.get(phase, "info")
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

        # Budget guard removed per user decision — token overspend
        # should not downgrade a successful injection result.

        # Emit inject conclusion event.
        if runtime and hasattr(runtime, "emit_event"):
            from chaos_agent.agent.operation_outcome import read_inject_verification

            verification = read_inject_verification(state.values) or {}
            level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
            blade_uid = state.values.get("blade_uid", "")
            _status_text_map = {
                "passed": "成功",
                "degraded": "成功（降级）",
                "failed": "失败",
            }
            status_text = _status_text_map.get(result.status, "失败")
            runtime.emit_event("conclusion", {
                "message": (
                    f"故障注入{status_text}"
                    f" | 验证级别: {level}"
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

        from chaos_agent.agent.task_snapshot import resolve_recover_initial_state

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

            # Emit recover conclusion event.
            if runtime and hasattr(runtime, "emit_event"):
                _recover_status_map = {
                    "recovered": "成功",
                    "partial_recovered": "成功（部分恢复）",
                    "failed": "失败",
                }
                recover_text = _recover_status_map.get(recover_task_state, "完成")
                recover_level = "ok" if recover_task_state == "recovered" else (
                    "warn" if recover_task_state == "partial_recovered" else "error"
                )
                blade_uid = inject_result.extras.get("blade_uid", "")
                runtime.emit_event("conclusion", {
                    "message": (
                        f"故障恢复{recover_text}"
                        f" | 恢复级别: {recover_task_state}"
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
        from chaos_agent.agent.fault_spec import fault_type_from_state
        from chaos_agent.agent.operation_outcome import (
            read_inject_verification,
            read_operation_outcome,
        )
        from chaos_agent.agent.state import infer_task_state

        task_state = infer_task_state(values)
        verification = read_inject_verification(values) or {}
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
            "coverage": 1.0 if fault_type_from_state(values) else 0.5,
            "flake_score": min(1.0, replan_count / 3.0),
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
                    from chaos_agent.agent.task_snapshot import resolve_recover_initial_state

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

    def clarify(
        self,
        thread_id: str,
        user_message: str,
        *,
        tui_session_id: str | None = None,
        conn: dict | None = None,
        on_event: "Callable[[dict], None] | None" = None,
    ) -> ClarifyResult:
        """Run one round of intent clarification.

        Drives the inject graph through ``intent_clarification`` (and
        possibly ``intent_confirm``) until either:
          - graph stops at ``intent_confirm`` interrupt → ``pending_card``
            is populated and ``confirmed_intent`` is None
          - LLM produces a follow-up question → ``last_ai_message`` is
            populated, ``pending_card`` is None
          - graph reaches END (rejected branch) → all fields None except
            ``last_ai_message``

        First call uses ``confirmed_intent=None`` initial_state; subsequent
        calls re-invoke from scratch with accumulated messages + new one
        (resetting confirmed_intent/fault_spec to None so the graph always
        enters intent_clarification).

        Args:
            thread_id: LangGraph checkpointer key (process-memory scope).
            user_message: latest human turn.
            tui_session_id: filename of the on-disk session JSON used by
                TuiSessionStore for cross-process replay. The platform
                typically passes the chat session UUID so ``inject``/
                ``recover`` task files share the same session folder.
                When None/empty, falls back to ``""`` (legacy behaviour,
                no on-disk persistence).
            conn: optional connection params injected into the graph state
                on first turn or refreshed on subsequent turns (for env
                switching). Recognised keys::

                    {
                      "kubeconfig": str,           # raw kubeconfig YAML
                      "kube_context": str,         # optional context name
                      "kubewiz_cluster_uuid": str, # for kubewiz mode
                      "kubewiz_profile": str,      # for kubewiz mode
                    }

                Note: ``settings`` (kubewiz_url/token, model creds) are
                injected separately by the caller via
                ``with blade_ai_context(...):``. ``conn`` only carries
                the per-state fields consumed by pipeline nodes
                (baseline_capture / verifier / debug_pod) via
                ``state.get("kubeconfig")``.
            on_event: optional callback for intermediate progress events.
                Invoked synchronously with a dict containing event info
                (tool calls, node transitions). Used by the platform to
                relay progress to the frontend via SSE.
        """
        pool = self._ensure_pool()
        return asyncio.run(
            self._async_clarify(
                pool, thread_id, user_message,
                tui_session_id=tui_session_id or "",
                conn=conn or {},
                on_event=on_event,
            )
        )

    async def async_clarify(
        self,
        thread_id: str,
        user_message: str,
        *,
        tui_session_id: str | None = None,
        conn: dict | None = None,
        on_event: "Callable[[dict], None] | None" = None,
    ) -> ClarifyResult:
        """Async public entry for intent clarification (stays in caller's loop)."""
        pool = await self._async_ensure_pool()
        return await self._async_clarify(
            pool, thread_id, user_message,
            tui_session_id=tui_session_id or "",
            conn=conn or {},
            on_event=on_event,
        )

    async def _async_clarify(
        self,
        pool: _ChaosAgentPool,
        thread_id: str,
        user_message: str,
        *,
        tui_session_id: str = "",
        conn: dict | None = None,
        on_event: "Callable[[dict], None] | None" = None,
    ) -> ClarifyResult:
        from langchain_core.messages import HumanMessage

        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 150,
        }
        conn = conn or {}

        # If thread already has state, push a new HumanMessage and let
        # the graph re-route from intent_clarification. Otherwise build
        # a minimal initial state.
        #
        # CRITICAL: use pool.intent_graph (Intent Graph = dialogue layer:
        # load_memory → intent_clarification → intent_confirm → END).
        # The previous code mistakenly used pool.inject_graph (Pipeline
        # Graph) which has NO intent_clarification node — causing the
        # graph to route straight into agent_loop/finish_planning.
        intent_graph = pool.intent_graph
        try:
            existing = await intent_graph.aget_state(config)
        except Exception:
            existing = None

        has_existing = bool(existing and existing.values)

        if has_existing and existing.next:
            # Graph is at an interrupt (e.g. intent_confirm).
            #
            # IMPORTANT: Command(update=...) on an interrupted graph does NOT
            # re-execute the interrupted node in a consistent way across
            # LangGraph versions. In some versions it skips the node entirely
            # and continues to the downstream router — which, with
            # confirmed_intent="inject" still in state, routes to agent_loop
            # (causing finish_planning to run inside a clarify() call).
            #
            # Safe approach: treat the new user message as an implicit
            # rejection of the pending intent confirm. Reset the graph state
            # (clear confirmed_intent/fault_spec) and re-invoke from scratch
            # so the graph enters intent_clarification cleanly.
            prev_messages = list(existing.values.get("messages") or [])
            prev_messages.append(HumanMessage(content=user_message))
            graph_input = {
                "task_id": thread_id,
                "tui_session_id": tui_session_id,
                "confirmed_intent": None,
                "fault_spec": None,
                "dry_run": False,
                "interaction_mode": "tui",
                "kubeconfig": conn.get("kubeconfig", "") or "",
                "kube_context": conn.get("kube_context", "") or "",
                "kubewiz_cluster_uuid": conn.get("kubewiz_cluster_uuid", "") or "",
                "kubewiz_profile": conn.get("kubewiz_profile", "") or "",
                "messages": prev_messages,
            }
        elif has_existing and not existing.next:
            # Graph reached END on previous turn (pure-text follow-up).
            # Re-invoke from scratch with accumulated messages + new one.
            # This is the standard LangGraph pattern for multi-turn where
            # each turn ends the graph (should_continue returns END).
            prev_messages = list(existing.values.get("messages") or [])
            prev_messages.append(HumanMessage(content=user_message))
            graph_input = {
                "task_id": thread_id,
                "tui_session_id": tui_session_id,
                "confirmed_intent": None,
                "fault_spec": None,
                "dry_run": False,
                "interaction_mode": "tui",
                "kubeconfig": conn.get("kubeconfig", "") or "",
                "kube_context": conn.get("kube_context", "") or "",
                "kubewiz_cluster_uuid": conn.get("kubewiz_cluster_uuid", "") or "",
                "kubewiz_profile": conn.get("kubewiz_profile", "") or "",
                "messages": prev_messages,
            }
        else:
            graph_input = {
                "task_id": thread_id,
                "tui_session_id": tui_session_id,
                # Crucial: None → intent_clarification will run from scratch
                "confirmed_intent": None,
                "fault_spec": None,
                "dry_run": False,
                "interaction_mode": "tui",
                "kubeconfig": conn.get("kubeconfig", "") or "",
                "kube_context": conn.get("kube_context", "") or "",
                "kubewiz_cluster_uuid": conn.get("kubewiz_cluster_uuid", "") or "",
                "kubewiz_profile": conn.get("kubewiz_profile", "") or "",
                "messages": [HumanMessage(content=user_message)],
            }

        values, pending_card, token_usage = await self._drive_until_interrupt(
            intent_graph, graph_input, config, on_event=on_event
        )

        # Extract fault_intent from IntentState's fault_spec (dict form of
        # FaultSpec). IntentState stores the converged intent as fault_spec,
        # not as a separate fault_intent field.
        fault_intent = None
        _spec_dict = values.get("fault_spec")
        if _spec_dict and isinstance(_spec_dict, dict):
            try:
                from chaos_agent.agent.fault_spec import FaultSpec
                fault_intent = FaultSpec.from_dict(_spec_dict).to_intent_dict()
            except Exception:
                fault_intent = _spec_dict

        return ClarifyResult(
            thread_id=thread_id,
            last_ai_message=_last_ai_message_text(values),
            fault_intent=fault_intent,
            confirmed_intent=values.get("confirmed_intent"),
            pending_card=pending_card,
            token_usage=token_usage,
        )

    def update_connection(
        self,
        thread_id: str,
        conn: dict,
    ) -> None:
        """Refresh the kubeconfig / kubewiz fields on an existing thread.

        Used when the platform wants to switch the bound drill environment
        mid-conversation while preserving messages/fault_spec/confirmed_intent.

        ``conn`` shape is identical to ``clarify(conn=...)``:

            {"kubeconfig": ..., "kube_context": ...,
             "kubewiz_cluster_uuid": ..., "kubewiz_profile": ...}

        If the thread has no existing state yet, this is a no-op (the
        next ``clarify`` call will pick up the same conn via its own
        parameter).
        """
        pool = self._ensure_pool()
        asyncio.run(self._async_update_connection(pool, thread_id, conn or {}))

    async def async_update_connection(
        self,
        thread_id: str,
        conn: dict,
    ) -> None:
        """Async public entry for connection refresh (stays in caller's loop)."""
        pool = await self._async_ensure_pool()
        await self._async_update_connection(pool, thread_id, conn or {})

    async def _async_update_connection(
        self,
        pool: _ChaosAgentPool,
        thread_id: str,
        conn: dict,
    ) -> None:
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 150,
        }
        # Clarify threads live on the Intent Graph
        graph = pool.intent_graph
        try:
            existing = await graph.aget_state(config)
        except Exception:
            existing = None
        if not (existing and existing.values):
            # No checkpoint yet — the connection will be set on the next
            # clarify(conn=...) call. Silently skip.
            return
        await graph.aupdate_state(
            config,
            values=_conn_to_state_patch(conn),
        )

    def step(
        self,
        thread_id: str,
        command: dict,
    ) -> StepResult:
        """Resume a paused graph with a card decision.

        Args:
            thread_id: same thread_id passed to clarify / execute
            command: ``{"card_id": str, "decision": "approved"|"rejected",
                "answer": str | None}``. ``answer`` is reserved for the
                upper layer's ``request_modify`` path; SDK ignores it
                and treats ``decision`` as the only authoritative input.

        ``decision`` MUST be ``"approved"`` or ``"rejected"``. SDK does
        not accept ``request_modify``: the platform layer translates
        ``request_modify`` into ``step(rejected)`` followed by
        ``clarify(user_feedback)``, so the main graph nodes need zero
        changes.
        """
        decision = (command or {}).get("decision")
        if decision not in ("approved", "rejected"):
            raise ValueError(
                f"step.decision must be 'approved' or 'rejected', got {decision!r}. "
                "request_modify is handled by the platform layer, not the SDK."
            )
        pool = self._ensure_pool()
        return asyncio.run(self._async_step(pool, thread_id, decision))

    async def async_step(
        self,
        thread_id: str,
        command: dict,
    ) -> StepResult:
        """Async public entry for resuming a paused graph (stays in caller's loop)."""
        decision = (command or {}).get("decision")
        if decision not in ("approved", "rejected"):
            raise ValueError(
                f"step.decision must be 'approved' or 'rejected', got {decision!r}. "
                "request_modify is handled by the platform layer, not the SDK."
            )
        pool = await self._async_ensure_pool()
        return await self._async_step(pool, thread_id, decision)

    async def _async_step(
        self,
        pool: _ChaosAgentPool,
        thread_id: str,
        decision: str,
    ) -> StepResult:
        from langgraph.types import Command

        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": 150,
        }

        # Determine which graph owns this thread.
        # Clarify threads (chaos-<session_id>) live on intent_graph;
        # execution threads (task-<hex>) live on inject_graph (pipeline).
        graph = pool.intent_graph if thread_id.startswith("chaos-") else pool.inject_graph

        try:
            values, pending_card, _token_usage = await self._drive_until_interrupt(
                graph,
                Command(resume=decision),
                config,
            )
        except Exception as exc:
            return StepResult(
                thread_id=thread_id,
                status="failed",
                pending_card=None,
                task_result=L4TaskResult(
                    task_id=thread_id,
                    status="failed",
                    trajectory_id="",
                    error=map_to_agent_error(exc),
                ),
            )

        if pending_card is not None:
            return StepResult(
                thread_id=thread_id,
                status="interrupted",
                pending_card=pending_card,
                task_result=None,
            )

        # Graph ended — synthesise a TaskResult from final state
        result = state_to_task_result(values, thread_id, trajectory_id="")
        return StepResult(
            thread_id=thread_id,
            status="completed",
            pending_card=None,
            task_result=result,
        )
