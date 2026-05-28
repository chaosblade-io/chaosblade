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
    keywords: list[str] = field(default_factory=list)
    test_types: list[str] = field(default_factory=list)
    input_schema: dict = field(default_factory=dict)
    output_schema: dict = field(default_factory=dict)
    sla: dict = field(default_factory=dict)
    cost_profile: dict = field(default_factory=dict)
    health_endpoint: str = ""
    protocol: str = "direct"


# JSON Schema for TestTask.payload (AgentCard.input_schema)
FAULT_PAYLOAD_SCHEMA: dict = {
    "type": "object",
    "required": ["fault_scope", "fault_target", "fault_action", "namespace"],
    "properties": {
        "fault_scope": {"type": "string", "enum": ["pod", "node", "container"]},
        "fault_target": {
            "type": "string",
            "description": "cpu|mem|network|disk|process",
        },
        "fault_action": {
            "type": "string",
            "description": "fullload|load|delay|loss|fill|kill|burn",
        },
        "namespace": {"type": "string"},
        "target_names": {"type": "array", "items": {"type": "string"}},
        "target_labels": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "params": {"type": "object", "additionalProperties": {"type": "string"}},
        "duration": {"type": "integer", "description": "seconds"},
        "kubeconfig": {"type": "string"},
        "kube_context": {"type": "string"},
        "direct": {"type": "boolean", "default": True},
        "auto_recover": {"type": "boolean", "default": True},
    },
}
