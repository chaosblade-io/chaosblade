"""Tests for safety_check node."""

import pytest

from chaos_agent.agent.nodes.safety_check import safety_check
from chaos_agent.config.settings import settings


class TestSafetyCheck:
    """Tests for the safety_check node function."""

    @pytest.mark.asyncio
    async def test_all_checks_pass(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "kube_connection_mode", "kubeconfig")
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "default", "names": ["my-pod"]}

        result = await safety_check(state)
        # No kubeconfig + kubeconfig mode → conflict check skipped → warning
        assert result["safety_status"] == "warning"
        assert "集群连接" in result["safety_reason"]

    @pytest.mark.asyncio
    async def test_all_checks_pass_with_kubeconfig(self, sample_agent_state, monkeypatch):
        from unittest.mock import AsyncMock, patch
        monkeypatch.setattr(settings, "kubeconfig_path", "/fake/kubeconfig")
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "default", "names": ["my-pod"]}

        with patch(
            "chaos_agent.agent.nodes.safety_check.check_blade_conflicts",
            new_callable=AsyncMock,
            return_value=([], []),
        ):
            result = await safety_check(state)
        assert result["safety_status"] == "safe"
        assert result["safety_reason"] is None

    @pytest.mark.asyncio
    async def test_blacklisted_namespace(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "safety_blacklist_namespaces", "kube-system,kube-public")

        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "kube-system", "names": ["coredns"]}
        from tests._helpers import replace_fault_spec
        replace_fault_spec(state, namespace="kube-system", names=("coredns",))

        result = await safety_check(state)
        assert result["safety_status"] == "rejected"
        assert "kube-system" in result["safety_reason"]
        assert "blacklist" in result["safety_reason"].lower()

    @pytest.mark.asyncio
    async def test_another_blacklisted_namespace(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "safety_blacklist_namespaces", "kube-system,kube-public")

        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "kube-public", "names": ["some-res"]}
        from tests._helpers import replace_fault_spec
        replace_fault_spec(state, namespace="kube-public", names=("some-res",))

        result = await safety_check(state)
        assert result["safety_status"] == "rejected"
        assert "kube-public" in result["safety_reason"]

    @pytest.mark.asyncio
    async def test_no_skill_name(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = ""
        state["target"] = {"namespace": "default"}

        result = await safety_check(state)
        assert result["safety_status"] == "rejected"
        assert "no skill" in result["safety_reason"].lower()

    @pytest.mark.asyncio
    async def test_skill_name_none(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = None
        state["target"] = {"namespace": "default"}

        result = await safety_check(state)
        assert result["safety_status"] == "rejected"

    @pytest.mark.asyncio
    async def test_no_target(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        # Wipe the fault_spec entirely so safety_check's no-scope guard fires
        state["fault_spec"] = None

        result = await safety_check(state)
        assert result["safety_status"] == "rejected"
        assert "no target" in result["safety_reason"].lower()

    @pytest.mark.asyncio
    async def test_empty_target(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        # Empty spec (no scope/blade_target/blade_action) → rejected
        state["fault_spec"] = {}

        result = await safety_check(state)
        assert result["safety_status"] == "rejected"

    @pytest.mark.asyncio
    async def test_check_order_namespace_first(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "safety_blacklist_namespaces", "kube-system")

        state = sample_agent_state
        state["skill_name"] = ""
        from tests._helpers import replace_fault_spec
        replace_fault_spec(state, namespace="kube-system", names=())

        result = await safety_check(state)
        assert "blacklist" in result["safety_reason"].lower()

    @pytest.mark.asyncio
    async def test_check_order_skill_before_target(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = ""
        state["target"] = None

        result = await safety_check(state)
        assert "skill" in result["safety_reason"].lower()

    @pytest.mark.asyncio
    async def test_allowed_namespace(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "safety_blacklist_namespaces", "kube-system,kube-public")
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "kube_connection_mode", "kubeconfig")

        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "production", "names": ["my-app"]}

        result = await safety_check(state)
        # No kubeconfig + kubeconfig mode → conflict check skipped → warning
        assert result["safety_status"] == "warning"

    @pytest.mark.asyncio
    async def test_safety_score_attached_on_non_rejected(self, sample_agent_state, monkeypatch):
        """E10 — every safety_check return must carry safety_score."""
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "default", "names": ["my-pod"]}

        result = await safety_check(state)
        score = result.get("safety_score")
        assert score is not None
        assert "overall" in score
        assert "level" in score
        assert "blast_radius" in score
        assert "frequency" in score
        assert "time" in score
        assert "topology" in score

    @pytest.mark.asyncio
    async def test_safety_score_attached_on_rejected(self, sample_agent_state, monkeypatch):
        """E10 — score still attached even when status is rejected."""
        monkeypatch.setattr(settings, "safety_blacklist_namespaces", "kube-system")
        from tests._helpers import replace_fault_spec
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        replace_fault_spec(state, namespace="kube-system", names=("coredns",))

        result = await safety_check(state)
        assert result["safety_status"] == "rejected"
        assert result.get("safety_score") is not None
        assert result["safety_score"]["overall"] >= 0

    @pytest.mark.asyncio
    async def test_routing_escalation_safe_to_confirm(self, sample_agent_state, monkeypatch):
        """E10 — routing flag on + critical score upgrades safe→confirm_required."""
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "safety_score_routing_enabled", True)
        monkeypatch.setattr(settings, "safety_score_confirm_threshold", 50)
        monkeypatch.setattr(settings, "safety_score_warning_threshold", 30)
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        # production namespace + critical name → topology high
        # cluster scope = node → blast_radius high
        from tests._helpers import replace_fault_spec
        replace_fault_spec(
            state,
            namespace="production",
            scope="node",
            names=("api-gateway",),
            blade_target="cpu",
            blade_action="fullload",
            duration_seconds=0,  # permanent
        )

        result = await safety_check(state)
        # Without escalation this would be "safe" (no blacklist, no conflict).
        # With escalation enabled and a high score, expect confirm_required.
        assert result["safety_status"] == "confirm_required"
        assert result["safety_score"]["overall"] >= 50

    @pytest.mark.asyncio
    async def test_routing_escalation_default_off(self, sample_agent_state, monkeypatch):
        """E10 — default (routing flag off) doesn't escalate even with high score."""
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "kube_connection_mode", "kubeconfig")
        # routing flag NOT set → defaults to False
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        from tests._helpers import replace_fault_spec
        replace_fault_spec(
            state,
            namespace="production",
            scope="node",
            names=("api-gateway",),
            blade_target="cpu",
            blade_action="fullload",
            duration_seconds=0,
        )

        result = await safety_check(state)
        # No kubeconfig + kubeconfig mode → base status is "warning" (conflict check skipped).
        # High score but routing flag off → no score-based escalation beyond warning.
        assert result["safety_status"] == "warning"
        assert result["safety_score"]["overall"] >= 50

    @pytest.mark.asyncio
    async def test_feasibility_block_rejects_when_enabled(self, sample_agent_state, monkeypatch):
        """G1 — feasibility_check_block_on_impossible=True rejects the inject."""
        from unittest.mock import AsyncMock, patch
        from chaos_agent.agent.feasibility import FeasibilityReport, FeasibilitySeverity

        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "feasibility_check_enabled", True)
        monkeypatch.setattr(settings, "feasibility_check_block_on_impossible", True)
        monkeypatch.setattr(settings, "target_health_check_enabled", False)

        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        from tests._helpers import replace_fault_spec
        replace_fault_spec(state, namespace="default", names=("my-pod",),
                           blade_target="mem", blade_action="load")

        with patch(
            "chaos_agent.agent.feasibility.assess_feasibility",
            new_callable=AsyncMock,
            return_value=FeasibilityReport(
                severity=FeasibilitySeverity.IMPOSSIBLE,
                headroom=0.054,
                current_value="222Mi (92.5%)",
                limit_value="240Mi",
                target_value="235Mi (98%)",
                message="Memory at 92.5%, target 98% — only 13Mi headroom",
                recommendation="Pick a Pod with lower memory usage",
            ),
        ):
            result = await safety_check(state)

        assert result["safety_status"] == "rejected"
        assert "not feasible" in result["safety_reason"].lower()
        assert result.get("feasibility_report") is not None
        assert result["feasibility_report"]["severity"] == "impossible"

    @pytest.mark.asyncio
    async def test_health_and_feasibility_both_reject(self, sample_agent_state, monkeypatch):
        """G2 — both health blocker + feasibility impossible produce combined rejection."""
        from unittest.mock import AsyncMock, patch
        from chaos_agent.agent.target_health import HealthReport, HealthSeverity, HealthIssue
        from chaos_agent.agent.feasibility import FeasibilityReport, FeasibilitySeverity

        monkeypatch.setattr(settings, "kubeconfig_path", "/fake/kubeconfig")
        monkeypatch.setattr(settings, "target_health_check_enabled", True)
        monkeypatch.setattr(settings, "target_health_check_block_on_blocker", True)
        monkeypatch.setattr(settings, "feasibility_check_enabled", True)
        monkeypatch.setattr(settings, "feasibility_check_block_on_impossible", True)

        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        from tests._helpers import replace_fault_spec
        replace_fault_spec(state, namespace="default", names=("my-pod",),
                           blade_target="mem", blade_action="load")

        with patch(
            "chaos_agent.agent.nodes.safety_check.check_blade_conflicts",
            new_callable=AsyncMock,
            return_value=([], []),
        ), patch(
            "chaos_agent.agent.target_health.assess_target_health",
            new_callable=AsyncMock,
            return_value=HealthReport(
                target={"names": ["my-pod"]},
                overall=HealthSeverity.BLOCK,
                issues=[HealthIssue(
                    severity=HealthSeverity.BLOCK,
                    code="node.disk_pressure",
                    message="Node has DiskPressure=True for 103d",
                    duration_hint="103d",
                )],
            ),
        ), patch(
            "chaos_agent.agent.feasibility.assess_feasibility",
            new_callable=AsyncMock,
            return_value=FeasibilityReport(
                severity=FeasibilitySeverity.IMPOSSIBLE,
                headroom=0.02,
                current_value="230Mi (95.8%)",
                limit_value="240Mi",
                target_value="235Mi (98%)",
                message="Memory at 95.8%, only 5Mi headroom",
                recommendation="Pick a Pod with lower memory usage",
            ),
        ):
            result = await safety_check(state)

        assert result["safety_status"] == "rejected"
        # Both reasons present in combined message
        assert "health" in result["safety_reason"].lower()
        assert "feasible" in result["safety_reason"].lower()
        # Both reports attached
        assert result.get("target_health_report") is not None
        assert result.get("feasibility_report") is not None
        assert result["target_health_report"]["overall"] == "block"
        assert result["feasibility_report"]["severity"] == "impossible"

    @pytest.mark.asyncio
    async def test_conflict_does_not_hide_health_report(self, sample_agent_state, monkeypatch):
        """Improvement 1: conflicts no longer suppress health/feasibility reports."""
        from unittest.mock import AsyncMock, patch
        from chaos_agent.agent.target_health import HealthReport, HealthSeverity, HealthIssue

        monkeypatch.setattr(settings, "kubeconfig_path", "/fake/kubeconfig")
        monkeypatch.setattr(settings, "target_health_check_enabled", True)
        monkeypatch.setattr(settings, "feasibility_check_enabled", False)

        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        from tests._helpers import replace_fault_spec
        replace_fault_spec(state, namespace="default", names=("my-pod",))

        # Mock conflicts → returns uids
        with patch(
            "chaos_agent.agent.nodes.safety_check.check_blade_conflicts",
            new_callable=AsyncMock,
            return_value=(["uid-1"], []),
        ), patch(
            "chaos_agent.agent.target_health.assess_target_health",
            new_callable=AsyncMock,
            return_value=HealthReport(
                target={"names": ["my-pod"]},
                overall=HealthSeverity.BLOCK,
                issues=[HealthIssue(
                    severity=HealthSeverity.BLOCK,
                    code="node.disk_pressure",
                    message="Node has DiskPressure=True for 103d",
                    duration_hint="103d",
                )],
            ),
        ):
            result = await safety_check(state)

        # Conflict produces warning status
        assert result["safety_status"] == "warning"
        assert result["conflict_uids"] == ["uid-1"]
        # Health report is ALSO present (not hidden by conflict early-return)
        assert result.get("target_health_report") is not None
        assert result["target_health_report"]["overall"] == "block"
