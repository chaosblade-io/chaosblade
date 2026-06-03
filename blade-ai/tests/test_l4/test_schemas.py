"""Tests for chaos_agent.l4.schemas — L4 schema dataclasses."""

import dataclasses

from chaos_agent.l4.schemas import (
    FAULT_PAYLOAD_SCHEMA,
    L4AgentCard,
    L4AgentError,
    L4TaskResult,
    L4TestTask,
)


class TestL4TestTask:
    """Test L4TestTask dataclass."""

    def test_required_fields(self):
        task = L4TestTask(task_id="t-001", intent="inject cpu fault")
        assert task.task_id == "t-001"
        assert task.intent == "inject cpu fault"

    def test_defaults(self):
        task = L4TestTask(task_id="t-001", intent="test")
        assert task.target is None
        assert task.test_type is None
        assert task.payload == {}

    def test_payload_isolation(self):
        """Each instance gets its own payload dict."""
        t1 = L4TestTask(task_id="a", intent="x")
        t2 = L4TestTask(task_id="b", intent="y")
        t1.payload["key"] = "val"
        assert "key" not in t2.payload

    def test_custom_payload(self):
        task = L4TestTask(
            task_id="t-002",
            intent="inject",
            payload={"fault_scope": "pod", "namespace": "cms"},
        )
        assert task.payload["fault_scope"] == "pod"


class TestL4AgentError:
    """Test L4AgentError dataclass."""

    def test_minimal(self):
        err = L4AgentError(code="AGENT_TIMEOUT")
        assert err.code == "AGENT_TIMEOUT"
        assert err.message == ""
        assert err.recoverable is False
        assert err.details == {}

    def test_full(self):
        err = L4AgentError(
            code="TOOL_ERROR",
            message="blade failed",
            recoverable=True,
            details={"uid": "abc"},
        )
        assert err.recoverable is True
        assert err.details["uid"] == "abc"

    def test_serializable(self):
        err = L4AgentError(code="UNKNOWN", message="oops")
        d = dataclasses.asdict(err)
        assert d["code"] == "UNKNOWN"
        assert d["message"] == "oops"


class TestL4TaskResult:
    """Test L4TaskResult dataclass."""

    def test_defaults(self):
        result = L4TaskResult(task_id="t-001")
        assert result.status == "passed"
        assert result.trajectory_id is None
        assert result.summary == ""
        assert result.error is None
        assert result.extras == {}

    def test_with_error(self):
        err = L4AgentError(code="ASSERT_FAILED", message="verification failed")
        result = L4TaskResult(task_id="t-002", status="failed", error=err)
        assert result.status == "failed"
        assert result.error.code == "ASSERT_FAILED"

    def test_serializable_with_nested_error(self):
        err = L4AgentError(code="TOOL_ERROR")
        result = L4TaskResult(task_id="t-003", error=err, extras={"k": "v"})
        d = dataclasses.asdict(result)
        assert d["error"]["code"] == "TOOL_ERROR"
        assert d["extras"]["k"] == "v"


class TestL4AgentCard:
    """Test L4AgentCard dataclass."""

    def test_defaults(self):
        card = L4AgentCard(agent_id="resilience")
        assert card.agent_type == "resilience"
        assert card.version == "v1"
        assert card.weight == 1.0
        assert card.status == "RUNNING"
        assert card.protocol == "direct"
        assert card.health_endpoint == ""

    def test_full_card(self):
        card = L4AgentCard(
            agent_id="resilience",
            capabilities=["pod_cpu", "pod_mem"],
            sla={"p50_ms": 120000},
        )
        assert len(card.capabilities) == 2
        assert card.sla["p50_ms"] == 120000

    def test_serializable(self):
        card = L4AgentCard(agent_id="test", keywords=["chaos"])
        d = dataclasses.asdict(card)
        assert d["agent_id"] == "test"
        assert d["keywords"] == ["chaos"]


class TestFaultPayloadSchema:
    """Test FAULT_PAYLOAD_SCHEMA structure."""

    def test_is_object_type(self):
        assert FAULT_PAYLOAD_SCHEMA["type"] == "object"

    def test_required_fields(self):
        required = FAULT_PAYLOAD_SCHEMA["required"]
        assert "fault_scope" in required
        assert "fault_target" in required
        assert "fault_action" in required
        assert "namespace" in required

    def test_properties_present(self):
        props = FAULT_PAYLOAD_SCHEMA["properties"]
        assert "target_names" in props
        assert "target_labels" in props
        assert "params" in props
        assert "duration" in props
        assert "kubeconfig" in props
        assert "direct" in props
        assert "auto_recover" in props
