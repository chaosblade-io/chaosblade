"""Tests for safety_check node."""

import pytest

from chaos_agent.agent.nodes.gates.safety_check import safety_check
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
        assert "cluster access" in result["safety_reason"]

    @pytest.mark.asyncio
    async def test_all_checks_pass_with_kubeconfig(self, sample_agent_state, monkeypatch):
        from unittest.mock import AsyncMock, patch
        monkeypatch.setattr(settings, "kubeconfig_path", "/fake/kubeconfig")
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "default", "names": ["my-pod"]}

        with patch(
            "chaos_agent.agent.nodes.gates.safety_check.check_blade_conflicts",
            new_callable=AsyncMock,
            return_value=([], []),
        ):
            result = await safety_check(state)
        assert result["safety_status"] == "safe"
        assert result["safety_reason"] is None

    @pytest.mark.asyncio
    async def test_host_injection_skips_conflict_check(self, sample_agent_state, monkeypatch):
        # Host-scope injection targets a bare host (no cluster CRD), so the
        # cluster conflict check must NOT run and the result must be "safe"
        # (not a "no cluster access" warning). Regression for kubewiz_host
        # being mis-treated as a k8s channel (positive PROFILE_K8S whitelist).
        from unittest.mock import AsyncMock, patch
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "kube_connection_mode", "kubewiz_host")
        monkeypatch.setattr(settings, "host_name", "10.0.2.8")
        state = sample_agent_state
        state["skill_name"] = "host-cpu-fullload"
        from tests._helpers import replace_fault_spec
        replace_fault_spec(
            state, scope="host", fault_target="cpu", fault_action="fullload",
            namespace="", names=(),
        )

        with patch(
            "chaos_agent.agent.nodes.gates.safety_check.check_blade_conflicts",
            new_callable=AsyncMock,
            return_value=([], []),
        ) as mock_conflicts:
            result = await safety_check(state)
        mock_conflicts.assert_not_called()
        assert result["safety_status"] == "safe"

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
        assert result["safety_status"] == "retry"
        assert "skill" in result["safety_reason"].lower()
        # Verify a HumanMessage was appended for LLM feedback
        msgs = result.get("messages", [])
        assert msgs
        last_msg = msgs[-1]
        assert hasattr(last_msg, "content")
        assert "activate_skill" in last_msg.content

    @pytest.mark.asyncio
    async def test_skill_name_none(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = None
        state["target"] = {"namespace": "default"}

        result = await safety_check(state)
        assert result["safety_status"] == "retry"

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
        # no_skill is now recoverable (retry), checked before no_target
        assert result["safety_status"] == "retry"
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
            fault_target="cpu",
            fault_action="fullload",
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
            fault_target="cpu",
            fault_action="fullload",
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
        from chaos_agent.agent.spec.feasibility import FeasibilityReport, FeasibilitySeverity

        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "feasibility_check_enabled", True)
        monkeypatch.setattr(settings, "feasibility_check_block_on_impossible", True)
        monkeypatch.setattr(settings, "target_health_check_enabled", False)

        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        from tests._helpers import replace_fault_spec
        replace_fault_spec(state, namespace="default", names=("my-pod",),
                           fault_target="mem", fault_action="load")

        with patch(
            "chaos_agent.agent.spec.feasibility.assess_feasibility",
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
        from chaos_agent.agent.spec.feasibility import FeasibilityReport, FeasibilitySeverity

        monkeypatch.setattr(settings, "kubeconfig_path", "/fake/kubeconfig")
        monkeypatch.setattr(settings, "target_health_check_enabled", True)
        monkeypatch.setattr(settings, "target_health_check_block_on_blocker", True)
        monkeypatch.setattr(settings, "feasibility_check_enabled", True)
        monkeypatch.setattr(settings, "feasibility_check_block_on_impossible", True)

        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        from tests._helpers import replace_fault_spec
        replace_fault_spec(state, namespace="default", names=("my-pod",),
                           fault_target="mem", fault_action="load")

        with patch(
            "chaos_agent.agent.nodes.gates.safety_check.check_blade_conflicts",
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
            "chaos_agent.agent.spec.feasibility.assess_feasibility",
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
            "chaos_agent.agent.nodes.gates.safety_check.check_blade_conflicts",
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


class TestAnchorKindIntegrity:
    """B76 review F (probe_b76_round6.py): the frozen identity must still
    name the resource KIND the user anchored. The skill-selection channel
    (extract scope override + lazy derivation rebuild) can silently swap a
    node-anchored intent for a pod fault with no user touchpoint in CLI."""

    NODE_INTENT = (
        "模拟节点宕机：在节点 cn-shanghai-cloudspe.25.209.71.189 上切断该节点与 "
        "API Server 的网络通信"
    )

    @pytest.mark.asyncio
    async def test_anchored_node_intent_with_pod_spec_forces_confirmation(
        self, sample_agent_state, monkeypatch,
    ):
        """F1-F3 shape: user anchors node X, the plan (after scope override
        + lazy derivation) delivers pod-cpu-fullload on an unrelated pod —
        the freeze must not pass silently."""
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "kube_connection_mode", "kubeconfig")
        state = sample_agent_state
        state["skill_name"] = "pod-cpu-fullload"
        from tests._helpers import replace_fault_spec
        replace_fault_spec(
            state, scope="pod", fault_target="cpu", fault_action="fullload",
            namespace="cms-demo", names=("my-app-pod-0",),
            user_description=self.NODE_INTENT,
        )

        result = await safety_check(state)
        assert result["safety_status"] == "confirm_required"
        assert result["needs_confirmation"] is True
        # The receipt names BOTH sides of the mismatch so the TUI card (or
        # the CLI gate rejection) shows exactly what drifted.
        assert "cn-shanghai-cloudspe.25.209.71.189" in result["safety_reason"]
        assert "pod" in result["safety_reason"]
        assert "my-app-pod-0" in result["safety_reason"]

    @pytest.mark.asyncio
    async def test_anchored_node_intent_with_node_spec_stays_unblocked(
        self, sample_agent_state, monkeypatch,
    ):
        """The happy path (LLM picks a node-scope skill) must not pay for
        the guard: same anchor, node scope → status unchanged."""
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "kube_connection_mode", "kubeconfig")
        state = sample_agent_state
        state["skill_name"] = "node-network-loss"
        from tests._helpers import replace_fault_spec
        replace_fault_spec(
            state, scope="node", fault_target="network", fault_action="loss",
            namespace="", names=("cn-shanghai-cloudspe.25.209.71.189",),
            user_description=self.NODE_INTENT,
        )

        result = await safety_check(state)
        # No kubeconfig → conflict check skipped → warning; the anchor
        # check must NOT escalate it.
        assert result["safety_status"] == "warning"
        assert result.get("needs_confirmation") is not True

    @pytest.mark.asyncio
    async def test_anchored_node_intent_with_host_spec_stays_unblocked(
        self, sample_agent_state, monkeypatch,
    ):
        """node and host are the same machine from two angles: a node-level
        drill routinely lands on host scope (Node_CPU cases run systemd-run
        payloads on the host). Anchored-node intent under host scope is a
        correct domain mapping, not a retarget — must NOT force the gate
        (this was a near-miss in the fix: canonicalise_kind("host") is
        "host", a naive != "node" check would have broken every host-path
        node drill)."""
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "kube_connection_mode", "kubewiz_host")
        monkeypatch.setattr(settings, "host_name", "10.0.2.8")
        state = sample_agent_state
        state["skill_name"] = "host-cpu-fullload"
        from tests._helpers import replace_fault_spec
        replace_fault_spec(
            state, scope="host", fault_target="cpu", fault_action="fullload",
            namespace="", names=(),
            user_description="在节点 cn-shanghai-cloudspe.25.209.71.189 上打满 CPU",
        )

        result = await safety_check(state)
        assert result["safety_status"] == "safe"

    @pytest.mark.asyncio
    async def test_unanchored_intent_with_pod_spec_stays_unblocked(
        self, sample_agent_state, monkeypatch,
    ):
        """Anchorless text (the legacy shape) never triggers — the check is
        fail-open on the anchor vocabulary, same as ①'s prefill."""
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "kube_connection_mode", "kubeconfig")
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        from tests._helpers import replace_fault_spec
        replace_fault_spec(
            state, scope="pod", fault_target="kill", fault_action="delete",
            namespace="default", names=("my-pod",),
            user_description="delete pod my-pod",
        )

        result = await safety_check(state)
        assert result["safety_status"] == "warning"

    @pytest.mark.asyncio
    async def test_rejected_status_is_not_softened_by_anchor_check(
        self, sample_agent_state, monkeypatch,
    ):
        """A blacklisted namespace is already terminal-rejected; the anchor
        check must not rewrite the reason (the blacklist diagnosis is the
        more specific one) nor soften the status."""
        monkeypatch.setattr(settings, "safety_blacklist_namespaces", "kube-system,kube-public")
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "kube_connection_mode", "kubeconfig")
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        from tests._helpers import replace_fault_spec
        replace_fault_spec(
            state, scope="pod", namespace="kube-system", names=("coredns",),
            user_description=self.NODE_INTENT,
        )

        result = await safety_check(state)
        assert result["safety_status"] == "rejected"
        assert "kube-system" in result["safety_reason"]


# ══ inject-b6b02ebd — fail-closed on an incoherent victim (维度2 + fail-closed) ══
#
# A ``name_from: victim_node`` mechanism needs a REAL victim pod to derive its
# host node. When the victim name was mis-derived from a mechanism target's
# probe (kube-proxy frozen under the drill-lb victim ns), discover_victim_nodes
# resolves nothing, the bridge collapses, and every node/host write REJECT_DRIFTs
# into a ~28-minute slow death. The fix fails FAST: if the named victim is
# DEFINITIVELY absent (NotFound) from its declared namespace, route back to the
# planner (retry) with an actionable re-declare nudge instead of freezing.
#
# The discriminator is fail-closed on a POSITIVE absence proof only: a
# transient failure (timeout, transport down) is NOT absence, so a flaky
# cluster never triggers a spurious replan.

import contextlib  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

from chaos_agent.agent.nodes.gates.safety_check import (  # noqa: E402
    _confirm_victim_pods_absent,
)
from chaos_agent.tools.kubectl_cli import QueryOutcome  # noqa: E402

_NOT_FOUND = QueryOutcome(
    ok=False,
    error='exit=1: Error from server (NotFound): pods "kube-proxy-worker-r5mg9" not found',
)
_EXISTS = QueryOutcome(ok=True, stdout="pod/kube-proxy-worker-r5mg9")
_TRANSIENT = QueryOutcome(ok=False, error="exception: connection timed out")


class TestConfirmVictimPodsAbsent:
    """Unit tests for the fail-closed/fail-open absence discriminator."""

    @pytest.mark.asyncio
    async def test_definitive_notfound_is_reported_absent(self):
        with patch(
            "chaos_agent.tools.kubectl_cli.query_kubectl",
            new=AsyncMock(return_value=_NOT_FOUND),
        ):
            absent = await _confirm_victim_pods_absent(
                "drill-lb", ("kube-proxy-worker-r5mg9",), "/fake",
            )
        assert absent == ("kube-proxy-worker-r5mg9",)

    @pytest.mark.asyncio
    async def test_existing_pod_is_not_absent(self):
        with patch(
            "chaos_agent.tools.kubectl_cli.query_kubectl",
            new=AsyncMock(return_value=_EXISTS),
        ):
            absent = await _confirm_victim_pods_absent(
                "drill-lb", ("drill-lb-target",), "/fake",
            )
        assert absent == ()

    @pytest.mark.asyncio
    async def test_transient_failure_fails_open_not_absent(self):
        # A timeout / transport-down is NOT proof of absence — reporting it as
        # absent would fire a spurious replan on a flaky cluster.
        with patch(
            "chaos_agent.tools.kubectl_cli.query_kubectl",
            new=AsyncMock(return_value=_TRANSIENT),
        ):
            absent = await _confirm_victim_pods_absent(
                "drill-lb", ("some-pod",), "/fake",
            )
        assert absent == ()

    @pytest.mark.asyncio
    async def test_mixed_only_definitive_notfound_returned(self):
        # One victim definitively absent, one merely unreachable: only the
        # proven-absent one is reported, so the "EVERY named victim absent"
        # caller condition is not met and no replan fires on partial evidence.
        answers = {"gone-pod": _NOT_FOUND, "flaky-pod": _TRANSIENT}

        async def fake_query(args, kubeconfig="", *, log_name=""):
            return answers[args[1]]

        with patch(
            "chaos_agent.tools.kubectl_cli.query_kubectl", new=fake_query,
        ):
            absent = await _confirm_victim_pods_absent(
                "drill-lb", ("gone-pod", "flaky-pod"), "/fake",
            )
        assert absent == ("gone-pod",)


class TestVictimAbsentFailClosed:
    """Node-level: safety_check must fail-fast REJECT an incoherent victim."""

    @staticmethod
    def _victim_node_entry():
        # A mechanism manifest legislating ``name_from: victim_node`` — the
        # derived node entry that collapses when the victim cannot be resolved.
        from chaos_agent.agent.target_guard.mechanism_writes import (
            MechanismWriteEntry,
        )
        return (MechanismWriteEntry(
            scope="node", namespace="", name_from="victim_node",
        ),)

    def _franken_state(self, sample_agent_state):
        from tests._helpers import replace_fault_spec
        state = sample_agent_state
        state["skill_name"] = "k8s-chaos-skills"
        # The franken target: a mechanism pod's NAME frozen under the victim ns.
        replace_fault_spec(
            state, scope="pod", fault_target="process", fault_action="kill",
            namespace="drill-lb", names=("kube-proxy-worker-r5mg9",),
            labels={}, case_resource_path="cases/kube-proxy.md",
        )
        return state

    def _patches(self, existence_outcome):
        """Deterministic seam set: no cluster, disabled health/feasibility, a
        victim_node manifest, empty node discovery, and a controlled victim
        existence query."""
        return [
            patch("chaos_agent.agent.nodes.gates.safety_check.sync_to_store",
                  new=AsyncMock()),
            patch("chaos_agent.agent.nodes.gates.safety_check.load_case_mechanism_writes",
                  new=lambda *a, **k: self._victim_node_entry()),
            patch("chaos_agent.agent.nodes.gates.safety_check.discover_owner_names",
                  new=AsyncMock(return_value=())),
            patch("chaos_agent.agent.nodes.gates.safety_check.discover_names_by_labels",
                  new=AsyncMock(return_value=())),
            patch("chaos_agent.agent.nodes.gates.safety_check.discover_pod_pvc_claims",
                  new=AsyncMock(return_value=())),
            patch("chaos_agent.agent.nodes.gates.safety_check.discover_victim_nodes",
                  new=AsyncMock(return_value=())),
            patch("chaos_agent.tools.kubectl_cli.query_kubectl",
                  new=AsyncMock(return_value=existence_outcome)),
        ]

    async def _run(self, state, monkeypatch, existence_outcome):
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "kube_connection_mode", "kubeconfig")
        monkeypatch.setattr(settings, "safety_blacklist_namespaces", "")
        monkeypatch.setattr(settings, "target_health_check_enabled", False)
        monkeypatch.setattr(settings, "feasibility_check_enabled", False)
        with contextlib.ExitStack() as stack:
            for p in self._patches(existence_outcome):
                stack.enter_context(p)
            return await safety_check(state)

    @pytest.mark.asyncio
    async def test_absent_victim_rejects_fail_fast(self, sample_agent_state, monkeypatch):
        # F1 (inject-b6b02ebd cascade fix): an incoherent victim is now a
        # TERMINAL reject, not a retry. The retry was mechanically futile —
        # ``names`` is write-once locked, so routing back to the planner spun
        # to MAX_AGENT_LOOP without ever correcting the name (proven by
        # _probe_futile_loop.py). The reason must carry an operator-facing
        # diagnostic AND deliberately avoid the "user"/"reject" substrings so
        # reject.py maps it to SAFETY_REJECTED (not USER_REJECTED).
        state = self._franken_state(sample_agent_state)
        result = await self._run(state, monkeypatch, _NOT_FOUND)
        assert result["safety_status"] == "rejected"
        reason = result["safety_reason"]
        assert "do not exist" in reason.lower()
        assert "drill-lb" in reason
        assert "user" not in reason.lower()    # -> SAFETY_REJECTED, not USER_*
        assert "reject" not in reason.lower()
        # No LLM nudge is appended anymore (there is no further planner turn).
        assert all(
            "VICTIM IDENTITY UNRESOLVED" not in getattr(m, "content", "")
            for m in result["messages"]
        )

    @pytest.mark.asyncio
    async def test_rejected_lands_on_safety_rejected_terminal(
        self, sample_agent_state, monkeypatch,
    ):
        # F1 end-to-end: the fail-fast disposition must actually TERMINATE on
        # SAFETY_REJECTED. Verified against the REAL consumer decision
        # functions (router + reject._infer_failure_detail), not assumed:
        #   - route_after_safety must send "rejected" to REJECT, never back to
        #     agent_loop (looping back is what made the old retry futile);
        #   - reject.py must attribute it to SAFETY_REJECTED and — because the
        #     rejected branch is checked BEFORE agent_loop_count — must NOT
        #     downgrade to PLANNING_TIMEOUT even after planning turns ran.
        from chaos_agent.agent.router import route_after_safety, REJECT
        from chaos_agent.agent.nodes.gates.reject import _infer_failure_detail
        from chaos_agent.agent.result.verdict import FailureCategory

        state = self._franken_state(sample_agent_state)
        result = await self._run(state, monkeypatch, _NOT_FOUND)
        assert result["safety_status"] == "rejected"

        # Router terminates (never loops back to the planner).
        assert route_after_safety(result) == REJECT

        # Reject node attributes to SAFETY_REJECTED, the diagnostic survives,
        # and agent_loop_count>0 does NOT downgrade it to PLANNING_TIMEOUT.
        terminal_state = {**state, **result, "agent_loop_count": 3}
        detail = _infer_failure_detail(terminal_state)
        assert (
            detail["failure_detail"]["category"]
            == FailureCategory.SAFETY_REJECTED.value
        )
        assert "drill-lb" in detail["failure_detail"]["context"]

    @pytest.mark.asyncio
    async def test_transient_failure_does_not_reject(self, sample_agent_state, monkeypatch):
        # Fail-open on infra: an unreachable cluster must NOT be read as
        # "victim absent" — that would spuriously reject a valid drill.
        state = self._franken_state(sample_agent_state)
        result = await self._run(state, monkeypatch, _TRANSIENT)
        assert result["safety_status"] != "rejected"

    @pytest.mark.asyncio
    async def test_existing_victim_does_not_reject(self, sample_agent_state, monkeypatch):
        # A victim that DOES exist (node discovery merely returned empty here)
        # is not the franken shape — no fail-fast reject.
        state = self._franken_state(sample_agent_state)
        result = await self._run(state, monkeypatch, _EXISTS)
        assert result["safety_status"] != "rejected"
