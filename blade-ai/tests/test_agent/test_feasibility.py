"""Tests for injection feasibility assessment (E18)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.spec.feasibility import (
    FeasibilityReport,
    FeasibilitySeverity,
    assess_feasibility,
)
from chaos_agent.agent.spec.fault_spec import FaultSpec


@pytest.fixture(autouse=True)
def _metrics_server_available():
    """Assume metrics-server is available for all checker tests."""
    import chaos_agent.agent.spec._feasibility_checkers as _fc
    _fc._metrics_probe_cache = None
    with patch(
        "chaos_agent.agent.spec._feasibility_checkers.is_metrics_server_available",
        new_callable=AsyncMock, return_value=True,
    ):
        yield


def _make_spec(**kwargs) -> FaultSpec:
    defaults = {
        "namespace": "cms-demo",
        "scope": "pod",
        "names": ("accounting-6fbdb464c7-qn2vr",),
        "fault_target": "mem",
        "fault_action": "load",
        "params": {"mem-percent": "98"},
    }
    defaults.update(kwargs)
    return FaultSpec(**defaults)


class TestMemoryFeasibilityChecker:
    """MemoryFeasibilityChecker pure logic tests."""

    @pytest.mark.asyncio
    async def test_impossible_when_headroom_under_5_percent(self):
        # usage=230, limit=240, target=98% → target_mb=235, headroom=(235-230)/240=2.1%
        spec = _make_spec(params={"mem-percent": "98"})
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_memory_usage_mb",
            new_callable=AsyncMock, return_value=230,
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_memory_limit_mb",
            new_callable=AsyncMock, return_value=240,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.IMPOSSIBLE
        assert report.headroom < 0.05
        assert "230" in report.message
        assert "240" in report.message

    @pytest.mark.asyncio
    async def test_tight_when_headroom_10_to_20_percent(self):
        spec = _make_spec(params={"mem-percent": "95"})
        # usage=180, limit=240, target=228 → headroom=(228-180)/240=20% → TIGHT
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_memory_usage_mb",
            new_callable=AsyncMock, return_value=185,
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_memory_limit_mb",
            new_callable=AsyncMock, return_value=240,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.TIGHT
        assert 0.05 < report.headroom <= 0.20

    @pytest.mark.asyncio
    async def test_ok_when_headroom_over_20_percent(self):
        spec = _make_spec(params={"mem-percent": "80"})
        # usage=100, limit=240, target=192 → headroom=(192-100)/240=38% → OK
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_memory_usage_mb",
            new_callable=AsyncMock, return_value=100,
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_memory_limit_mb",
            new_callable=AsyncMock, return_value=240,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.OK
        assert report.headroom > 0.20

    @pytest.mark.asyncio
    async def test_returns_none_when_no_limit(self):
        """Essence-B boundary (LEGITIMATE no-ceiling): usage read succeeded but
        the pod has no resources.limits.memory → unbounded → mem-load is
        trivially feasible. This is NOT a capability loss, so it stays ``None``
        (advisory OK) — contrast test_skipped_when_kubectl_top_fails below where
        the READ itself failed."""
        spec = _make_spec(params={"mem-percent": "98"})
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_memory_usage_mb",
            new_callable=AsyncMock, return_value=222,
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_memory_limit_mb",
            new_callable=AsyncMock, return_value=None,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is None

    @pytest.mark.asyncio
    async def test_skipped_when_kubectl_top_fails(self):
        """Essence-B (capability loss): a FAILED usage read (metrics-server
        returned nothing for THIS target, slipping past the global metrics-server
        precheck) used to launder into ``None`` (≡OK, a silent green on the
        confirm card). It must now surface as a first-class SKIPPED report."""
        spec = _make_spec(params={"mem-percent": "98"})
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_memory_usage_mb",
            new_callable=AsyncMock, return_value=None,
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_memory_limit_mb",
            new_callable=AsyncMock, return_value=240,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.SKIPPED

    @pytest.mark.asyncio
    async def test_returns_none_when_no_mem_percent_param(self):
        spec = _make_spec(params={})
        report = await assess_feasibility(spec, "/fake/kubeconfig")
        assert report is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_names(self):
        spec = _make_spec(names=())
        report = await assess_feasibility(spec, "/fake/kubeconfig")
        assert report is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_namespace(self):
        spec = _make_spec(namespace="")
        report = await assess_feasibility(spec, "/fake/kubeconfig")
        assert report is None

    @pytest.mark.asyncio
    async def test_headroom_zero_when_usage_exceeds_target(self):
        spec = _make_spec(params={"mem-percent": "80"})
        # usage=200, limit=240, target=192 → headroom=(192-200)/240 < 0 → IMPOSSIBLE
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_memory_usage_mb",
            new_callable=AsyncMock, return_value=200,
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_memory_limit_mb",
            new_callable=AsyncMock, return_value=240,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.IMPOSSIBLE
        assert report.headroom == 0.0


class TestCpuFeasibilityChecker:
    """CpuFeasibilityChecker pure logic tests."""

    @pytest.mark.asyncio
    async def test_impossible_when_cpu_at_capacity(self):
        spec = _make_spec(
            fault_target="cpu",
            fault_action="fullload",
            params={"cpu-percent": "100"},
            scope="pod",
        )
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_cpu_usage_millicores",
            new_callable=AsyncMock, return_value=480,
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_cpu_limit_millicores",
            new_callable=AsyncMock, return_value=500,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.IMPOSSIBLE

    @pytest.mark.asyncio
    async def test_ok_when_headroom_sufficient(self):
        spec = _make_spec(
            fault_target="cpu",
            fault_action="fullload",
            params={"cpu-percent": "80"},
            scope="pod",
        )
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_cpu_usage_millicores",
            new_callable=AsyncMock, return_value=200,
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_cpu_limit_millicores",
            new_callable=AsyncMock, return_value=500,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.OK
        assert report.headroom > 0.20

    @pytest.mark.asyncio
    async def test_node_scope_uses_capacity(self):
        spec = _make_spec(
            fault_target="cpu",
            fault_action="fullload",
            params={"cpu-percent": "90"},
            scope="node",
            names=("worker-01",),
            namespace="",
        )
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_cpu_usage_millicores",
            new_callable=AsyncMock, return_value=7000,
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_node_cpu_capacity_millicores",
            new_callable=AsyncMock, return_value=8000,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        # target=7200, usage=7000, headroom=(7200-7000)/8000=2.5% → IMPOSSIBLE
        assert report.severity == FeasibilitySeverity.IMPOSSIBLE

    @pytest.mark.asyncio
    async def test_fullload_defaults_to_100_percent(self):
        spec = _make_spec(
            fault_target="cpu",
            fault_action="fullload",
            params={},
            scope="pod",
        )
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_cpu_usage_millicores",
            new_callable=AsyncMock, return_value=100,
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_cpu_limit_millicores",
            new_callable=AsyncMock, return_value=500,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.OK

    @pytest.mark.asyncio
    async def test_returns_none_without_names(self):
        spec = _make_spec(fault_target="cpu", names=())
        report = await assess_feasibility(spec, "/fake/kubeconfig")
        assert report is None


class TestAssessFeasibility:
    """Entry point dispatch + error handling tests."""

    @pytest.mark.asyncio
    async def test_unknown_fault_target_returns_none(self):
        spec = _make_spec(fault_target="jvm")
        report = await assess_feasibility(spec, "/fake/kubeconfig")
        assert report is None

    @pytest.mark.asyncio
    async def test_checker_exception_returns_skipped(self):
        """Essence-B (capability loss): a REGISTERED checker that raised could
        not determine feasibility. ``assess_feasibility``'s outer handler used to
        launder the exception into ``None`` (≡OK); it must now surface SKIPPED.
        (``checker is None`` — no checker at all — still returns None: that is
        genuine not-applicable, tested by test_unknown_fault_target_returns_none.)
        """
        spec = _make_spec(params={"mem-percent": "98"})
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_memory_usage_mb",
            new_callable=AsyncMock, side_effect=RuntimeError("boom"),
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.SKIPPED

    @pytest.mark.asyncio
    async def test_report_to_dict_structure(self):
        report = FeasibilityReport(
            severity=FeasibilitySeverity.IMPOSSIBLE,
            headroom=0.054,
            current_value="222Mi (92.5%)",
            limit_value="240Mi",
            target_value="235Mi (98%)",
            message="Memory at 92.5%",
            recommendation="Pick a Pod with lower memory usage",
        )
        d = report.to_dict()
        assert d["severity"] == "impossible"
        assert d["headroom"] == 0.054
        assert d["message"] == "Memory at 92.5%"
        assert d["recommendation"] == "Pick a Pod with lower memory usage"


class TestMetricsServerProbe:
    """Metrics-server availability probe (improvement 3)."""

    @pytest.mark.asyncio
    async def test_skipped_when_metrics_server_unavailable(self):
        spec = _make_spec(params={"mem-percent": "98"})
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers.is_metrics_server_available",
            new_callable=AsyncMock, return_value=False,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.SKIPPED
        assert "metrics-server" in report.message
        assert report.headroom == 0.0

    @pytest.mark.asyncio
    async def test_skipped_for_cpu_target_too(self):
        spec = _make_spec(fault_target="cpu", fault_action="fullload", params={"cpu-percent": "80"})
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers.is_metrics_server_available",
            new_callable=AsyncMock, return_value=False,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.SKIPPED

    @pytest.mark.asyncio
    async def test_no_skip_for_unknown_fault_target(self):
        spec = _make_spec(fault_target="jvm")
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers.is_metrics_server_available",
            new_callable=AsyncMock, return_value=False,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        # jvm has no checker → returns None (not SKIPPED)
        assert report is None

    @pytest.mark.asyncio
    async def test_probe_ttl_cache(self):
        import chaos_agent.agent.spec._feasibility_checkers as _fc
        _fc._metrics_probe_cache = (True, _fc._time.monotonic())
        # Should use cache, not call _run_kubectl
        result = await _fc.is_metrics_server_available("/fake")
        assert result is True


class TestLabelBasedPodResolution:
    """Feasibility checkers resolve real pod name via labels."""

    @pytest.mark.asyncio
    async def test_memory_checker_resolves_pod_via_labels(self):
        spec = _make_spec(
            names=("accounting",),
            labels={"app": "accounting"},
            params={"mem-percent": "80"},
        )
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._resolve_first_pod",
            new_callable=AsyncMock, return_value="accounting-6fbdb464c7-qn2vr",
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_memory_usage_mb",
            new_callable=AsyncMock, return_value=100,
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_memory_limit_mb",
            new_callable=AsyncMock, return_value=240,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.OK

    @pytest.mark.asyncio
    async def test_cpu_checker_resolves_pod_via_labels(self):
        spec = _make_spec(
            fault_target="cpu",
            fault_action="fullload",
            names=("accounting",),
            labels={"app": "accounting"},
            params={},
        )
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._resolve_first_pod",
            new_callable=AsyncMock, return_value="accounting-6fbdb464c7-qn2vr",
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_cpu_usage_millicores",
            new_callable=AsyncMock, return_value=100,
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_cpu_limit_millicores",
            new_callable=AsyncMock, return_value=500,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.OK

    @pytest.mark.asyncio
    async def test_resolve_fails_returns_skipped(self):
        """Essence-B (capability loss): the spec is COMPLETE (names+labels+
        namespace) but no Running pod is resolvable in the cluster, so the mem
        check could not run. This mirrors NetworkFeasibilityChecker's
        unresolvable-target SKIPPED — it used to launder into ``None`` (≡OK).
        Contrast test_returns_none_when_no_names / _no_namespace, where the spec
        is INCOMPLETE and the check legitimately does not apply (→ None)."""
        spec = _make_spec(
            names=("accounting",),
            labels={"app": "accounting"},
            params={"mem-percent": "80"},
        )
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._resolve_first_pod",
            new_callable=AsyncMock, return_value=None,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.SKIPPED


class TestNetworkFeasibilityChecker:
    """NetworkFeasibilityChecker label resolution tests."""

    @pytest.mark.asyncio
    async def test_network_checker_resolves_pod_via_labels(self):
        spec = _make_spec(
            fault_target="network",
            fault_action="delay",
            names=("accounting",),
            labels={"app": "accounting"},
            params={"time": "3000", "interface": "eth0"},
        )
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._resolve_first_pod",
            new_callable=AsyncMock, return_value="accounting-6fbdb464c7-qn2vr",
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_pod_phase",
            new_callable=AsyncMock, return_value="Running",
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_pod_node",
            new_callable=AsyncMock, return_value="node-a",
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_blade_tool_pod_ready",
            new_callable=AsyncMock, return_value=(True, (), ""),
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_interface_exists",
            new_callable=AsyncMock, return_value=(True, ""),
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_iptables_available",
            new_callable=AsyncMock, return_value=(True, ""),
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_active_network_experiment",
            new_callable=AsyncMock, return_value=False,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.OK

    @pytest.mark.asyncio
    async def test_network_tight_when_in_container_iptables_missing(self):
        """Missing in-container iptables only closes the direct-exec
        fallback — blade/debug-container paths carry their own binaries,
        so this is advisory (TIGHT), not a blocker (task-e9bae269)."""
        spec = _make_spec(
            fault_target="network",
            fault_action="drop",
            names=("accounting-6fbdb464c7-qn2vr",),
            params={},
        )
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._resolve_first_pod",
            new_callable=AsyncMock, return_value="accounting-6fbdb464c7-qn2vr",
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_pod_phase",
            new_callable=AsyncMock, return_value="Running",
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_pod_node",
            new_callable=AsyncMock, return_value="node-a",
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_blade_tool_pod_ready",
            new_callable=AsyncMock, return_value=(True, (), ""),
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_interface_exists",
            new_callable=AsyncMock, return_value=(True, ""),
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_iptables_available",
            new_callable=AsyncMock, return_value=(False, "command not found"),
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_active_network_experiment",
            new_callable=AsyncMock, return_value=False,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.TIGHT
        assert "iptables" in report.message
        assert "direct-exec" in report.message

    @pytest.mark.asyncio
    async def test_network_warns_when_iptables_indeterminate(self):
        spec = _make_spec(
            fault_target="network",
            fault_action="drop",
            names=("accounting-6fbdb464c7-qn2vr",),
            params={},
        )
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._resolve_first_pod",
            new_callable=AsyncMock, return_value="accounting-6fbdb464c7-qn2vr",
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_pod_phase",
            new_callable=AsyncMock, return_value="Running",
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_pod_node",
            new_callable=AsyncMock, return_value="node-a",
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_blade_tool_pod_ready",
            new_callable=AsyncMock, return_value=(True, (), ""),
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_interface_exists",
            new_callable=AsyncMock, return_value=(True, ""),
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_iptables_available",
            new_callable=AsyncMock, return_value=(None, "context deadline exceeded"),
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_active_network_experiment",
            new_callable=AsyncMock, return_value=False,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.TIGHT
        assert "context deadline exceeded" in report.current_value

    @pytest.mark.asyncio
    async def test_network_unresolvable_target_returns_skipped(self):
        """W-83-5 essence-B: an unresolvable target must be a VISIBLE SKIPPED,
        not a silent ``None``. ``assess_feasibility`` treats ``None`` as
        "equivalent to OK — fail-open", so the pre-fix ``return None`` made a
        network check that NEVER RAN render identically to one that PASSED.
        The network checker now returns a first-class SKIPPED report (the same
        state the metrics-server-unavailable path uses)."""
        spec = _make_spec(
            fault_target="network",
            fault_action="delay",
            names=("accounting",),
            labels={"app": "accounting"},
            params={"time": "3000"},
        )
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._resolve_first_pod",
            new_callable=AsyncMock, return_value=None,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.SKIPPED
        assert "resolve" in report.message.lower()
        assert report.headroom == 0.0


class TestNetworkMechanismGate:
    """The netfilter-only probes (in-container iptables + interface on pod
    scope; host iptables on node scope) must be gated on the case-declared
    ``network_mechanism`` — NOT on ``fault_action`` — so a userspace-bind /
    resolver fault never receives a factually wrong iptables advisory, while
    an undeclared ("") or netfilter fault fails safe and runs every probe
    (byte-identical to the pre-declaration behaviour)."""

    @staticmethod
    def _pod_scope_patches():
        """Shared pod-scope plumbing up to the mechanism gate. Returns a dict
        of patch-target → AsyncMock so tests can assert call/no-call on the
        gated probes individually."""
        base = "chaos_agent.agent.spec._feasibility_checkers."
        return {
            "_resolve_first_pod": AsyncMock(return_value="accounting-6fbdb464c7-qn2vr"),
            "_fetch_pod_phase": AsyncMock(return_value="Running"),
            "_fetch_pod_node": AsyncMock(return_value="node-a"),
            "_check_blade_tool_pod_ready": AsyncMock(return_value=(True, (), "")),
            "_check_interface_exists": AsyncMock(return_value=(True, "")),
            "_check_iptables_available": AsyncMock(return_value=(False, "command not found")),
            "_check_active_network_experiment": AsyncMock(return_value=False),
        }, base

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mechanism", ["userspace-bind", "resolver"])
    async def test_non_netfilter_mechanism_skips_iptables_and_interface(self, mechanism):
        """A declared non-netfilter mechanism reaches OK WITHOUT the
        iptables/interface probes running — even though iptables is missing
        (which would emit a TIGHT advisory under the netfilter path)."""
        mocks, base = self._pod_scope_patches()
        spec = _make_spec(
            fault_target="network", fault_action="occupy",
            names=("accounting-6fbdb464c7-qn2vr",), params={},
        )
        with patch(base + "_resolve_first_pod", mocks["_resolve_first_pod"]), \
             patch(base + "_fetch_pod_phase", mocks["_fetch_pod_phase"]), \
             patch(base + "_fetch_pod_node", mocks["_fetch_pod_node"]), \
             patch(base + "_check_blade_tool_pod_ready", mocks["_check_blade_tool_pod_ready"]), \
             patch(base + "_check_interface_exists", mocks["_check_interface_exists"]), \
             patch(base + "_check_iptables_available", mocks["_check_iptables_available"]), \
             patch(base + "_check_active_network_experiment", mocks["_check_active_network_experiment"]):
            report = await assess_feasibility(
                spec, "/fake/kubeconfig", network_mechanism=mechanism,
            )

        assert report is not None
        assert report.severity == FeasibilitySeverity.OK
        # The netfilter-only probes NEVER RAN; the OK report must not claim
        # iptables/interface facts it did not collect.
        mocks["_check_interface_exists"].assert_not_called()
        mocks["_check_iptables_available"].assert_not_called()
        assert "iptables" not in report.current_value
        assert "interface" not in report.current_value

    @pytest.mark.asyncio
    async def test_undeclared_mechanism_fails_safe_to_netfilter(self):
        """No declaration ("") → the iptables probe RUNS and its missing
        binary surfaces the TIGHT advisory, exactly as before the gate."""
        mocks, base = self._pod_scope_patches()
        spec = _make_spec(
            fault_target="network", fault_action="occupy",
            names=("accounting-6fbdb464c7-qn2vr",), params={},
        )
        with patch(base + "_resolve_first_pod", mocks["_resolve_first_pod"]), \
             patch(base + "_fetch_pod_phase", mocks["_fetch_pod_phase"]), \
             patch(base + "_fetch_pod_node", mocks["_fetch_pod_node"]), \
             patch(base + "_check_blade_tool_pod_ready", mocks["_check_blade_tool_pod_ready"]), \
             patch(base + "_check_interface_exists", mocks["_check_interface_exists"]), \
             patch(base + "_check_iptables_available", mocks["_check_iptables_available"]), \
             patch(base + "_check_active_network_experiment", mocks["_check_active_network_experiment"]):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.TIGHT
        mocks["_check_iptables_available"].assert_called_once()

    @pytest.mark.asyncio
    async def test_gate_keys_on_mechanism_not_fault_action(self):
        """Mechanism-invariance: the SAME netfilter-typical action verb
        (``delay``) is skipped when the case DECLARES userspace-bind, proving
        the gate reads the declared mechanism, not the action string. Without
        a hard-coded action branch, an author-declared mechanism is the only
        input that suppresses the probes."""
        mocks, base = self._pod_scope_patches()
        spec = _make_spec(
            fault_target="network", fault_action="delay",
            names=("accounting-6fbdb464c7-qn2vr",), params={"time": "3000"},
        )
        with patch(base + "_resolve_first_pod", mocks["_resolve_first_pod"]), \
             patch(base + "_fetch_pod_phase", mocks["_fetch_pod_phase"]), \
             patch(base + "_fetch_pod_node", mocks["_fetch_pod_node"]), \
             patch(base + "_check_blade_tool_pod_ready", mocks["_check_blade_tool_pod_ready"]), \
             patch(base + "_check_interface_exists", mocks["_check_interface_exists"]), \
             patch(base + "_check_iptables_available", mocks["_check_iptables_available"]), \
             patch(base + "_check_active_network_experiment", mocks["_check_active_network_experiment"]):
            report = await assess_feasibility(
                spec, "/fake/kubeconfig", network_mechanism="userspace-bind",
            )

        assert report.severity == FeasibilitySeverity.OK
        mocks["_check_iptables_available"].assert_not_called()

    @pytest.mark.asyncio
    async def test_node_non_netfilter_mechanism_skips_host_iptables(self):
        """Node scope: a declared non-netfilter mechanism returns None (check
        not applicable) instead of probing host iptables — a missing host
        iptables would otherwise emit an IMPOSSIBLE that could BLOCK a
        feasible host-port-occupy injection."""
        spec = _make_spec(
            fault_target="network", fault_action="occupy",
            scope="node", names=("node-a",), namespace="", params={},
        )
        node_probe = AsyncMock(return_value=(False, "chroot missing"))
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_node_iptables_available",
            node_probe,
        ):
            report = await assess_feasibility(
                spec, "/fake/kubeconfig", network_mechanism="userspace-bind",
            )

        assert report is None
        node_probe.assert_not_called()

    @pytest.mark.asyncio
    async def test_node_netfilter_mechanism_still_checks_host_iptables(self):
        """Control for the node axis: an undeclared ("") node network fault
        STILL probes host iptables and returns IMPOSSIBLE when missing — the
        P0 host-iptables guard (task-4d5069fe) is preserved for netfilter."""
        spec = _make_spec(
            fault_target="network", fault_action="delay",
            scope="node", names=("node-a",), namespace="", params={},
        )
        node_probe = AsyncMock(return_value=(False, "chroot missing"))
        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_node_iptables_available",
            node_probe,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.IMPOSSIBLE
        node_probe.assert_called_once()

    def test_gate_has_no_hardcoded_fault_action_branch(self):
        """Negative source guard: the mechanism gate must be a single
        membership test against the declared mechanism set, never a growing
        ``fault_action in {...}`` allowlist. Scanning the checker source keeps
        the '槽位与内容分离' invariant honest — a future author adding an
        action-string branch here turns this test red."""
        import inspect
        from chaos_agent.agent.spec._feasibility_checkers import (
            NetworkFeasibilityChecker,
        )

        src = inspect.getsource(NetworkFeasibilityChecker.assess)
        assert "needs_netfilter" in src
        assert "NON_NETFILTER_NETWORK_MECHANISMS" in src
        # The gate predicate reads the declared mechanism, not the action verb.
        assert "network_mechanism not in NON_NETFILTER_NETWORK_MECHANISMS" in src
        # No hard-coded per-action allowlist/denylist driving the netfilter gate.
        assert 'fault_action ==' not in src
        assert 'fault_action in' not in src


class TestBladeToolPodFeasibility:
    """Blade operator-path precondition: the chaosblade-tool pod on the
    target pod's NODE must be Running and carry the action's binaries.

    Regression for task-e9bae269: cluster-level "some tool pod is
    healthy" probing let two terminal blade_create attempts burn before
    the ImagePullBackOff on the target node's tool pod was diagnosed.
    """

    @staticmethod
    def _spec():
        return _make_spec(
            fault_target="network",
            fault_action="drop",
            names=("accounting-6fbdb464c7-qn2vr",),
            params={},
        )

    def _base_patches(self):
        return (
            patch(
                "chaos_agent.agent.spec._feasibility_checkers._resolve_first_pod",
                new_callable=AsyncMock,
                return_value="accounting-6fbdb464c7-qn2vr",
            ),
            patch(
                "chaos_agent.agent.spec._feasibility_checkers._fetch_pod_phase",
                new_callable=AsyncMock, return_value="Running",
            ),
            patch(
                "chaos_agent.agent.spec._feasibility_checkers._fetch_pod_node",
                new_callable=AsyncMock, return_value="node-a",
            ),
        )

    @pytest.mark.asyncio
    async def test_impossible_when_tool_pod_missing_on_target_node(self):
        resolve, phase, node = self._base_patches()
        with resolve, phase, node, patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_blade_tool_pod_ready",
            new_callable=AsyncMock,
            return_value=(False, (),
                          "no Running chaosblade-tool pod found on node node-a"),
        ):
            report = await assess_feasibility(self._spec(), "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.IMPOSSIBLE
        assert "chaosblade-tool" in report.message
        assert "node-a" in report.message
        # Recommendation must point at the documented degradation path.
        assert "netadmin" in report.recommendation

    @pytest.mark.asyncio
    async def test_impossible_when_tool_pod_lacks_required_binary(self):
        resolve, phase, node = self._base_patches()
        with resolve, phase, node, patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_blade_tool_pod_ready",
            new_callable=AsyncMock,
            return_value=(False, ("iptables",),
                          "tool pod chaosblade-tool-x missing binary: not found"),
        ):
            report = await assess_feasibility(self._spec(), "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.IMPOSSIBLE
        assert "iptables" in report.message

    @pytest.mark.asyncio
    async def test_tight_when_tool_pod_check_indeterminate(self):
        resolve, phase, node = self._base_patches()
        with resolve, phase, node, patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_blade_tool_pod_ready",
            new_callable=AsyncMock,
            return_value=(None, (), "kubectl timeout"),
        ):
            report = await assess_feasibility(self._spec(), "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.TIGHT
        assert "kubectl timeout" in report.current_value

    @pytest.mark.asyncio
    async def test_skipped_when_pod_node_unresolvable(self):
        """Node lookup failure must not block — the remaining checks run."""
        resolve, phase, _unused = self._base_patches()
        with resolve, phase, patch(
            "chaos_agent.agent.spec._feasibility_checkers._fetch_pod_node",
            new_callable=AsyncMock, return_value=None,
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_blade_tool_pod_ready",
            new_callable=AsyncMock,
        ) as tool_check, patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_interface_exists",
            new_callable=AsyncMock, return_value=(True, ""),
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_iptables_available",
            new_callable=AsyncMock, return_value=(True, ""),
        ), patch(
            "chaos_agent.agent.spec._feasibility_checkers._check_active_network_experiment",
            new_callable=AsyncMock, return_value=False,
        ):
            report = await assess_feasibility(self._spec(), "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.OK
        tool_check.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_find_tool_pod_parses_namespace_and_name(self):
        """Discovery is namespace-agnostic: real clusters deploy ChaosBlade
        outside the chart-default namespace (task-e9bae269: default)."""
        from chaos_agent.agent.spec._feasibility_checkers import (
            _find_tool_pod_on_node,
        )

        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._run_kubectl",
            new_callable=AsyncMock,
            return_value="default chaosblade-tool-26xlq",
        ) as run:
            result = await _find_tool_pod_on_node("node-a", "/fake/kubeconfig")

        assert result == ("chaosblade-tool-26xlq", "default")
        # Must query all namespaces with the tool label, not -n chaosblade.
        args = run.await_args.args[0]
        assert "-A" in args
        assert "app=chaosblade-tool" in args
        assert "-n" not in args

    @pytest.mark.asyncio
    async def test_find_tool_pod_returns_none_when_absent(self):
        from chaos_agent.agent.spec._feasibility_checkers import (
            _find_tool_pod_on_node,
        )

        with patch(
            "chaos_agent.agent.spec._feasibility_checkers._run_kubectl",
            new_callable=AsyncMock, return_value=None,
        ):
            assert await _find_tool_pod_on_node("node-a", "/fake") is None

    def test_required_binaries_by_action(self):
        from chaos_agent.agent.spec._feasibility_checkers import (
            _required_tool_binaries,
        )

        assert _required_tool_binaries("drop") == ("iptables",)
        for action in ("delay", "loss", "corrupt", "duplicate"):
            assert _required_tool_binaries(action) == ("tc",)


class TestHostFeasibilityProbes:
    """Host-profile (profile, target) probes + dispatch via profile_for_spec."""

    @pytest.mark.asyncio
    async def test_host_mem_probe_parses_free(self):
        from chaos_agent.agent.spec._host_feasibility_checkers import HostMemProbe

        free_out = (
            "              total        used        free      shared  buff/cache   available\n"
            "Mem:           2000         500        1000          10         490        1400\n"
            "Swap:             0           0           0\n"
        )
        with patch(
            "chaos_agent.agent.spec._host_feasibility_checkers._run_host",
            new_callable=AsyncMock, return_value=free_out,
        ):
            m = await HostMemProbe().measure(_make_spec(scope="host"), "")
        assert m is not None
        assert m.current == 500
        assert m.limit == 2000

    @pytest.mark.asyncio
    async def test_host_mem_probe_degrades_to_meminfo(self):
        from chaos_agent.agent.spec._host_feasibility_checkers import HostMemProbe

        meminfo = "MemTotal:       2097152 kB\nMemAvailable:   1048576 kB\n"

        async def fake_run(cmd, timeout=8, *, target=None):
            if cmd[:2] == ["free", "-m"]:
                return None  # primary binary missing → degrade
            if cmd[:1] == ["cat"]:
                return meminfo
            return None

        with patch(
            "chaos_agent.agent.spec._host_feasibility_checkers._run_host",
            side_effect=fake_run,
        ):
            m = await HostMemProbe().measure(_make_spec(scope="host"), "")
        assert m is not None
        # used = (2097152 - 1048576)//1024 = 1024 MiB; total = 2097152//1024 = 2048
        assert m.current == 1024
        assert m.limit == 2048

    @pytest.mark.asyncio
    async def test_host_cpu_probe_load_over_cores(self):
        from chaos_agent.agent.spec._host_feasibility_checkers import HostCpuProbe

        async def fake_run(cmd, timeout=8, *, target=None):
            if cmd[:1] == ["uptime"]:
                return " 10:00:00 up 1 day,  load average: 1.50, 1.20, 1.00"
            if cmd[:1] == ["nproc"]:
                return "4"
            return None

        with patch(
            "chaos_agent.agent.spec._host_feasibility_checkers._run_host",
            side_effect=fake_run,
        ):
            m = await HostCpuProbe().measure(_make_spec(scope="host", fault_target="cpu"), "")
        assert m is not None
        assert m.current == 1500  # int(1.5 * 1000)
        assert m.limit == 4000    # 4 cores * 1000

    @pytest.mark.asyncio
    async def test_host_disk_probe_uses_df(self):
        from chaos_agent.agent.spec._host_feasibility_checkers import HostDiskProbe

        spec = _make_spec(scope="host", fault_target="disk", params={"path": "/data"})
        with patch(
            "chaos_agent.agent.spec._host_feasibility_checkers.fetch_host_disk_usage",
            new_callable=AsyncMock, return_value=(72, 100),
        ):
            m = await HostDiskProbe().measure(spec, "")
        assert m is not None
        assert m.current == 72
        assert m.limit == 100

    @pytest.mark.asyncio
    async def test_host_disk_probe_resolves_mount_from_names_when_params_empty(self):
        # D4/B2 real shape: the NL host disk-fill carries the mount in ``names``
        # with params EMPTY. The probe must read the FaultSpec authority (which
        # falls through params["path"] → names absolute paths), NOT params.get,
        # else it measures the root "/" instead of the declared /drill-disk.
        from chaos_agent.agent.spec._host_feasibility_checkers import HostDiskProbe

        spec = _make_spec(
            scope="host", fault_target="disk", fault_action="fill",
            names=("/drill-disk",), params={},
        )
        with patch(
            "chaos_agent.agent.spec._host_feasibility_checkers.fetch_host_disk_usage",
            new_callable=AsyncMock, return_value=(1, 2),
        ) as fetch:
            m = await HostDiskProbe().measure(spec, "")
        assert m is not None
        assert fetch.call_args.args[0] == "/drill-disk"

    @pytest.mark.asyncio
    async def test_dispatch_selects_host_probe_for_host_scope(self):
        # scope="host" → profile_for_spec → "host"; mem checker resolves the
        # host mem probe and skips the metrics-server gate.
        free_out = (
            "              total        used        free\n"
            "Mem:           2000         500        1000\n"
        )
        spec = _make_spec(scope="host", params={"mem-percent": "98"})
        with patch(
            "chaos_agent.agent.spec._host_feasibility_checkers._run_host",
            new_callable=AsyncMock, return_value=free_out,
        ):
            report = await assess_feasibility(spec, "")
        assert report is not None
        # target=1960 MiB, usage=500 → headroom huge → OK
        assert report.severity == FeasibilitySeverity.OK


class TestSeamDelegation:
    """``_resolve_first_pod`` / ``_fetch_pod_node`` are thin wrappers over the
    shared ``target_identity`` seam (#83 audit root-fix). The checker tests above
    patch these wrappers wholesale, so NOTHING there exercises the delegation
    GLUE — the exact params the wrapper must hand the seam. These lock it: a
    workload scope must resolve via selector DISCOVERY (never fall back to the
    bare workload name — the pre-fix bug), under a Running-phase filter, through
    an executor that preserves this lane's transport contract.
    """

    @pytest.mark.asyncio
    async def test_resolve_first_pod_delegates_with_discovery_and_running_filter(self):
        from chaos_agent.agent.spec import _feasibility_checkers as fc

        spec = _make_spec(
            scope="deployment", names=("web",), namespace="ns",
            fault_target="cpu", fault_action="fullload", params={},
        )
        captured: dict = {}

        async def _fake_resolve(**kwargs):
            captured.update(kwargs)
            return "web-7d9-abc"

        with patch(
            "chaos_agent.agent.target_identity.resolve_target_pod_name",
            side_effect=_fake_resolve,
        ):
            out = await fc._resolve_first_pod(spec, "/kc")

        assert out == "web-7d9-abc"
        assert captured["scope"] == "deployment"
        assert captured["names"] == ("web",) or list(captured["names"]) == ["web"]
        assert captured["namespace"] == "ns"
        assert captured["kubeconfig"] == "/kc"
        # THE fix: a workload scope asks the workload for its own selector
        # instead of returning the bare workload name.
        assert captured["discover_selector"] is True
        assert captured["field_selector"] == "status.phase=Running"

    @pytest.mark.asyncio
    async def test_feasibility_executor_preserves_transport_contract(self):
        """The injected executor must keep ``skip_guard=True`` +
        ``source='feasibility-check'`` — delegating the query SHAPE to the seam
        must not drop this lane's transport semantics."""
        from chaos_agent.agent.spec import _feasibility_checkers as fc

        seen: dict = {}

        async def _fake_transport(cmd, target, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(exit_code=0, stdout="pod-x")

        with patch(
            "chaos_agent.transports.execute_via_transport",
            side_effect=_fake_transport,
        ):
            await fc._feasibility_executor(["kubectl", "get"], None, timeout=5)

        assert seen["skip_guard"] is True
        assert seen["source"] == "feasibility-check"

    @pytest.mark.asyncio
    async def test_fetch_pod_node_delegates_to_seam_primitive(self):
        from chaos_agent.agent.spec import _feasibility_checkers as fc

        captured: dict = {}

        async def _fake_node(**kwargs):
            captured.update(kwargs)
            return "node-a"

        with patch(
            "chaos_agent.agent.target_identity.resolve_node_of_pod",
            side_effect=_fake_node,
        ):
            out = await fc._fetch_pod_node("pod-1", "ns", "/kc")

        assert out == "node-a"
        assert captured["pod_name"] == "pod-1"
        assert captured["namespace"] == "ns"
        assert captured["kubeconfig"] == "/kc"
