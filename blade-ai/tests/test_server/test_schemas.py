"""Tests for server schemas (Pydantic models)."""

import pytest

from chaos_agent.models.schemas import JSONEnvelope
from chaos_agent.server.schemas import (
    InjectRequest,
    RecoverRequest,
    ConfirmRequest,
    TargetInfo,
    InjectResponse,
    RecoverResponse,
    ConfirmResponse,
    SkillParameterInfo,
    FaultTypeInfo,
    CategoryInfo,
    SkillsListResponse,
    VersionResponse,
)


class TestInjectRequest:
    def test_required_fields(self):
        req = InjectRequest(
            scope="pod",
            target="pod",
            action="delete",
            target_name="my-pod",
            namespace="default",
        )
        assert req.scope == "pod"
        assert req.target == "pod"
        assert req.action == "delete"
        assert req.target_name == "my-pod"
        assert req.namespace == "default"

    def test_default_values(self):
        req = InjectRequest(
            scope="pod",
            target="pod",
            action="delete",
            target_name="my-pod",
            namespace="default",
        )
        assert req.duration == 600
        assert req.params is None
        assert req.params_flags is None
        assert req.confirm is False
        assert req.labels is None
        assert req.input is None
        assert req.direct is False

    def test_custom_values(self):
        req = InjectRequest(
            scope="pod",
            target="network",
            action="delay",
            target_name="app1,app2",
            namespace="production",
            duration=120,
            params={"time": 3000},
            params_flags=["read", "write"],
            confirm=True,
            direct=True,
        )
        assert req.duration == 120
        assert req.params == {"time": 3000}
        assert req.params_flags == ["read", "write"]
        assert req.confirm is True
        assert req.direct is True

    def test_missing_required_field_raises(self):
        with pytest.raises(Exception):
            InjectRequest(scope="pod")

    def test_input_only_mode(self):
        """NL mode: only input field, no structured params required."""
        req = InjectRequest(input="给 default 命名空间的 my-pod 注入 pod-kill 故障")
        assert req.input is not None
        assert req.scope is None
        assert req.target is None
        assert req.action is None

    def test_input_mode_with_partial_structured(self):
        """NL mode with some structured params still works."""
        req = InjectRequest(
            input="kill the pod my-app",
            duration=120,
            confirm=True,
        )
        assert req.input == "kill the pod my-app"
        assert req.duration == 120
        assert req.confirm is True

    def test_neither_nl_nor_structured_raises(self):
        """Neither nl nor full structured params should raise validation error."""
        with pytest.raises(Exception):
            InjectRequest(scope="pod")

    def test_partial_structured_without_nl_raises(self):
        """Partial structured params without nl should raise validation error."""
        with pytest.raises(Exception):
            InjectRequest(scope="pod", target="cpu")

    def test_direct_with_input_raises(self):
        """direct mode is not compatible with input."""
        with pytest.raises(Exception):
            InjectRequest(input="kill the pod", direct=True)

    def test_direct_without_full_structured_raises(self):
        """direct mode requires all structured params."""
        with pytest.raises(Exception):
            InjectRequest(scope="pod", target="cpu", action="fullload", direct=True)

    def test_invalid_scope_raises(self):
        """Invalid scope value should raise validation error."""
        with pytest.raises(Exception):
            InjectRequest(
                scope="invalid",
                target="cpu",
                action="fullload",
                target_name="my-pod",
                namespace="default",
            )


class TestRecoverRequest:
    def test_required_fields(self):
        req = RecoverRequest(task_id="task-123")
        assert req.task_id == "task-123"

    def test_defaults(self):
        req = RecoverRequest(task_id="task-123")
        assert req.target_name is None
        assert req.force is False

    def test_custom_values(self):
        req = RecoverRequest(task_id="task-123", target_name="my-pod", force=True)
        assert req.target_name == "my-pod"
        assert req.force is True


class TestConfirmRequest:
    def test_required_fields(self):
        req = ConfirmRequest(action="approve")
        assert req.action == "approve"

    def test_with_reason(self):
        req = ConfirmRequest(action="reject", reason="Too risky")
        assert req.reason == "Too risky"

    def test_default_reason(self):
        req = ConfirmRequest(action="approve")
        assert req.reason is None


class TestTargetInfo:
    def test_defaults(self):
        info = TargetInfo()
        assert info.name == ""
        assert info.namespace == ""

    def test_with_values(self):
        info = TargetInfo(name="pod-1", namespace="default")
        assert info.name == "pod-1"
        assert info.namespace == "default"


class TestInjectResponse:
    def test_required_fields(self):
        resp = InjectResponse(task_id="task-1")
        assert resp.task_id == "task-1"

    def test_defaults(self):
        resp = InjectResponse(task_id="task-1")
        assert resp.result == "pending"
        assert resp.fault_type == ""
        assert resp.blade_uid == ""
        assert resp.targets == []
        assert resp.verification is None
        assert resp.error == ""

    def test_with_targets_and_verification(self):
        resp = InjectResponse(
            task_id="task-1",
            result="injected",
            fault_type="pod-cpu-fullload",
            blade_uid="uid-123",
            targets=[TargetInfo(name="my-pod", namespace="default")],
            verification={"level": "verified", "layer1": "passed", "layer2": "passed"},
        )
        assert resp.result == "injected"
        assert len(resp.targets) == 1
        assert resp.targets[0].name == "my-pod"
        assert resp.blade_uid == "uid-123"
        assert resp.verification["level"] == "verified"

class TestRecoverResponse:
    def test_required_fields(self):
        resp = RecoverResponse(task_id="task-1")
        assert resp.task_id == "task-1"

    def test_defaults(self):
        resp = RecoverResponse(task_id="task-1")
        assert resp.result == "pending"
        assert resp.blade_uid == ""
        assert resp.targets == []
        assert resp.verification is None
        assert resp.error == ""

    def test_with_targets(self):
        resp = RecoverResponse(
            task_id="task-1",
            result="recovered",
            blade_uid="uid-123",
            targets=[TargetInfo(name="pod-1", namespace="default")],
        )
        assert resp.result == "recovered"
        assert resp.targets[0].name == "pod-1"


class TestConfirmResponse:
    def test_fields(self):
        resp = ConfirmResponse(task_id="task-1", action="approve")
        assert resp.task_id == "task-1"
        assert resp.action == "approve"
        assert resp.reason is None


class TestSkillParameterInfo:
    def test_defaults(self):
        info = SkillParameterInfo(key="duration")
        assert info.key == "duration"
        assert info.type == "string"
        assert info.required is False


class TestFaultTypeInfo:
    def test_defaults(self):
        info = FaultTypeInfo(fault_type="pod-delete")
        assert info.fault_type == "pod-delete"
        assert info.target_types == []
        assert info.params == []


class TestCategoryInfo:
    def test_defaults(self):
        info = CategoryInfo(category="network")
        assert info.category == "network"
        assert info.faults == []


class TestSkillsListResponse:
    def test_defaults(self):
        resp = SkillsListResponse()
        assert resp.total == 0
        assert resp.categories == []


class TestVersionResponse:
    def test_defaults(self):
        resp = VersionResponse()
        # Bumped in lockstep with pyproject / package.json / utils/version.ts.
        # If you bump the package version, this assertion has to move too.
        assert resp.version == "0.1.0"
        assert resp.build_time == ""
        assert resp.supported_fault_count == 0


class TestJSONEnvelope:
    def test_defaults(self):
        env = JSONEnvelope()
        assert env.code == 0
        assert env.message == "success"
        assert env.data is None
        assert env.request_id == ""

    def test_custom_values(self):
        env = JSONEnvelope(
            code=1,
            message="error",
            data={"key": "value"},
            request_id="req-123",
        )
        assert env.code == 1
        assert env.message == "error"
        assert env.data == {"key": "value"}

    def test_timestamp_auto_generated(self):
        env = JSONEnvelope()
        assert env.timestamp != ""
        # Should be ISO format
        assert "T" in env.timestamp or "-" in env.timestamp
