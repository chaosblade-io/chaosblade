"""L4 Agent SDK schema definitions.

Zero-dependency dataclass mirrors of ai-testing-platform types.
blade-ai does NOT import from ai-testing-platform; these definitions
are duck-type-compatible with the platform's TestTask / TaskResult / AgentCard.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class L4TestTask:
    """Input task from ai-testing-platform."""

    task_id: str
    intent: str
    target: str | None = None
    test_type: str | None = None
    payload: dict = field(default_factory=dict)


@dataclass
class L4AgentError:
    """Structured error attached to a failed TaskResult."""

    code: str  # AGENT_TIMEOUT | TARGET_UNREACHABLE | PERMISSION_DENIED | TOOL_ERROR | ASSERT_FAILED | UNKNOWN
    message: str = ""
    recoverable: bool = False
    details: dict = field(default_factory=dict)


@dataclass
class L4TaskResult:
    """Output result returned to ai-testing-platform."""

    task_id: str
    status: str = "passed"  # passed | failed | cancelled | degraded
    trajectory_id: str | None = None
    summary: str = ""
    error: L4AgentError | None = None
    extras: dict = field(default_factory=dict)


@dataclass
class L4AgentCard:
    """Agent metadata for platform registration."""

    agent_id: str
    agent_type: str = "resilience"
    description: str = ""
    version: str = "v1"
    weight: float = 1.0
    status: str = "RUNNING"  # RUNNING | DEGRADED | OFFLINE
    capabilities: list[str] = field(default_factory=list)
    capability_groups: list[dict] = field(default_factory=list)
    """Structured capability groups [{name, summary, examples}], rendered by
    the platform coordinator into its system prompt so the main agent knows
    what user-visible scenarios should be routed here."""
    keywords: list[str] = field(default_factory=list)
    test_types: list[str] = field(default_factory=list)
    input_schema: dict = field(default_factory=dict)
    output_schema: dict = field(default_factory=dict)
    sla: dict = field(default_factory=dict)
    cost_profile: dict = field(default_factory=dict)
    health_endpoint: str = ""
    protocol: str = "direct"


# --- Human-in-the-loop card protocol (v0.5.0) ---
#
# 主图节点通过 LangGraph ``interrupt(payload)`` 暂停。v0.5.0 起 SDK 把 4 类
# interrupt payload 统一适配成 ``PendingCard``，由上层（ai-testing-platform /
# TUI / Server）通过 ``runtime.present_card`` 协议消费。
#
# decision 二元：``approved`` | ``rejected``。SDK 不感知 ``request_modify``——
# 平台层把「修改」拆解为「先 step rejected 再 clarify(user_feedback)」两步，
# 主图节点零改动（reject 分支已为「保留 messages + fault_spec、下一轮 NL refine」
# 设计，参考 TUI Composer YesNoFeedbackSelect 同构 pattern）。


@dataclass
class PendingCard:
    """Human-in-the-loop card surfaced from a graph interrupt.

    card_type:
      - ``intent_confirm``    — 意图确认（fault_intent 收敛后）
      - ``plan_confirm``      — 执行前安全确认（confirmation_gate）
      - ``plan_change``       — 计划变更确认（plan_change_confirm）
      - ``tool_drift``        — 工具调用偏移确认（tool_screener target_change）

    decision_options 仅 ``intent_confirm`` 卡可包含 ``request_modify``，
    其余卡片仅 ``approved`` / ``rejected``。
    """

    card_type: str
    card_id: str
    title: str
    summary: str
    details: dict = field(default_factory=dict)
    decision_options: list[str] = field(default_factory=lambda: ["approved", "rejected"])
    thread_id: str = ""


@dataclass
class ClarifyResult:
    """Result of one ``clarify(thread_id, user_message)`` round.

    last_ai_message:
        最后一条 AIMessage 文本，用于 chat 渲染。
    fault_intent:
        当前累计的故障意图字典（可能尚未完全收敛）。
    confirmed_intent:
        ``"inject"`` 表示用户已批准开打；``"discuss"`` 表示对话继续；
        ``None`` 表示尚未到 intent_confirm 节点（仍在 clarification 多轮）。
    pending_card:
        若图驱动到 ``intent_confirm`` interrupt，会带回 ``PendingCard``；
        否则为 ``None``（继续多轮澄清）。
    token_usage:
        本轮 clarify 中所有 LLM 调用的 token 消耗汇总。
        格式: ``{"prompt_tokens": int, "completion_tokens": int,
        "total_tokens": int}``。无数据时为 ``None``。
    """

    thread_id: str
    last_ai_message: str = ""
    fault_intent: dict | None = None
    confirmed_intent: str | None = None
    pending_card: "PendingCard | None" = None
    token_usage: dict | None = None


@dataclass
class StepResult:
    """Result of one ``step(thread_id, command)`` resume.

    status:
      - ``completed``    — graph 已跑到 END，``task_result`` 非空
      - ``interrupted``  — 又遇到下一个 interrupt，``pending_card`` 非空
      - ``failed``       — 执行抛异常，``task_result.error`` 非空
    """

    thread_id: str
    status: str = "interrupted"
    pending_card: "PendingCard | None" = None
    task_result: "L4TaskResult | None" = None


# JSON Schema for TestTask.payload (AgentCard.input_schema).
#
# Single canonical shape: payload.fault_intent, matching
# FaultSpec.to_intent_dict() produced by the platform's
# run_chaos_inject tool.
_FAULT_INTENT_SCHEMA: dict = {
    "type": "object",
    "description": (
        "Structured fault intent produced by clarify_chaos_intent. "
        "Required fields: scope, target, action, namespace."
    ),
    "required": ["scope", "target", "action", "namespace"],
    "properties": {
        "scope": {"type": "string", "enum": ["pod", "node", "container"]},
        "target": {
            "type": "string",
            "description": "cpu|mem|network|disk|process",
        },
        "action": {
            "type": "string",
            "description": "fullload|load|delay|loss|fill|kill|burn",
        },
        "namespace": {"type": "string"},
        "names": {"type": "array", "items": {"type": "string"}},
        "labels": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "params": {"type": "object", "additionalProperties": {"type": "string"}},
        "duration": {"type": "integer", "description": "seconds"},
        "user_description": {"type": "string"},
    },
}

FAULT_PAYLOAD_SCHEMA: dict = {
    "type": "object",
    "description": "L4 fault injection payload. fault_intent is required.",
    "required": ["fault_intent"],
    "properties": {
        "fault_intent": _FAULT_INTENT_SCHEMA,
        "kubeconfig": {"type": "string"},
        "kube_context": {"type": "string"},
        # Connection mode for K8s — kubeconfig text OR KubeWiz HTTP gateway.
        # Not in `required`: blade-ai falls back to settings defaults when
        # absent. The platform layer (ai-testing-platform) gates completeness
        # before dispatch.
        "kube_connection_mode": {
            "type": "string",
            "enum": ["kubeconfig", "kubewiz"],
            "description": "K8s 连接模式：kubeconfig 文本 或 KubeWiz HTTP 网关。",
        },
        "kubewiz_url": {
            "type": "string",
            "description": "KubeWiz 网关地址（kube_connection_mode=kubewiz 时使用）。",
        },
        "kubewiz_cluster_uuid": {
            "type": "string",
            "description": "KubeWiz 目标集群 UUID。",
        },
        "kubewiz_profile": {
            "type": "string",
            "description": "KubeWiz wiz task exec --profile 登录工号。",
        },
        "kubewiz_token": {
            "type": "string",
            "description": "KubeWiz 永久 token。",
        },
        "direct": {"type": "boolean", "default": True},
        "auto_recover": {"type": "boolean", "default": True},
    },
}
