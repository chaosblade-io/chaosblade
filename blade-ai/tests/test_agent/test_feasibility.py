"""Tests for injection feasibility assessment (E18)."""

from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.feasibility import (
    FeasibilityReport,
    FeasibilitySeverity,
    assess_feasibility,
)
from chaos_agent.agent.fault_spec import FaultSpec


@pytest.fixture(autouse=True)
def _metrics_server_available():
    """Assume metrics-server is available for all checker tests."""
    import chaos_agent.agent._feasibility_checkers as _fc
    _fc._metrics_probe_cache = None
    with patch(
        "chaos_agent.agent._feasibility_checkers.is_metrics_server_available",
        new_callable=AsyncMock, return_value=True,
    ):
        yield


def _make_spec(**kwargs) -> FaultSpec:
    defaults = {
        "namespace": "cms-demo",
        "scope": "pod",
        "names": ("accounting-6fbdb464c7-qn2vr",),
        "blade_target": "mem",
        "blade_action": "load",
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
            "chaos_agent.agent._feasibility_checkers._fetch_memory_usage_mb",
            new_callable=AsyncMock, return_value=230,
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_memory_limit_mb",
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
            "chaos_agent.agent._feasibility_checkers._fetch_memory_usage_mb",
            new_callable=AsyncMock, return_value=185,
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_memory_limit_mb",
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
            "chaos_agent.agent._feasibility_checkers._fetch_memory_usage_mb",
            new_callable=AsyncMock, return_value=100,
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_memory_limit_mb",
            new_callable=AsyncMock, return_value=240,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.OK
        assert report.headroom > 0.20

    @pytest.mark.asyncio
    async def test_returns_none_when_no_limit(self):
        spec = _make_spec(params={"mem-percent": "98"})
        with patch(
            "chaos_agent.agent._feasibility_checkers._fetch_memory_usage_mb",
            new_callable=AsyncMock, return_value=222,
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_memory_limit_mb",
            new_callable=AsyncMock, return_value=None,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is None

    @pytest.mark.asyncio
    async def test_returns_none_when_kubectl_top_fails(self):
        spec = _make_spec(params={"mem-percent": "98"})
        with patch(
            "chaos_agent.agent._feasibility_checkers._fetch_memory_usage_mb",
            new_callable=AsyncMock, return_value=None,
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_memory_limit_mb",
            new_callable=AsyncMock, return_value=240,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is None

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
            "chaos_agent.agent._feasibility_checkers._fetch_memory_usage_mb",
            new_callable=AsyncMock, return_value=200,
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_memory_limit_mb",
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
            blade_target="cpu",
            blade_action="fullload",
            params={"cpu-percent": "100"},
            scope="pod",
        )
        with patch(
            "chaos_agent.agent._feasibility_checkers._fetch_cpu_usage_millicores",
            new_callable=AsyncMock, return_value=480,
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_cpu_limit_millicores",
            new_callable=AsyncMock, return_value=500,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.IMPOSSIBLE

    @pytest.mark.asyncio
    async def test_ok_when_headroom_sufficient(self):
        spec = _make_spec(
            blade_target="cpu",
            blade_action="fullload",
            params={"cpu-percent": "80"},
            scope="pod",
        )
        with patch(
            "chaos_agent.agent._feasibility_checkers._fetch_cpu_usage_millicores",
            new_callable=AsyncMock, return_value=200,
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_cpu_limit_millicores",
            new_callable=AsyncMock, return_value=500,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.OK
        assert report.headroom > 0.20

    @pytest.mark.asyncio
    async def test_node_scope_uses_capacity(self):
        spec = _make_spec(
            blade_target="cpu",
            blade_action="fullload",
            params={"cpu-percent": "90"},
            scope="node",
            names=("worker-01",),
            namespace="",
        )
        with patch(
            "chaos_agent.agent._feasibility_checkers._fetch_cpu_usage_millicores",
            new_callable=AsyncMock, return_value=7000,
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_node_cpu_capacity_millicores",
            new_callable=AsyncMock, return_value=8000,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        # target=7200, usage=7000, headroom=(7200-7000)/8000=2.5% → IMPOSSIBLE
        assert report.severity == FeasibilitySeverity.IMPOSSIBLE

    @pytest.mark.asyncio
    async def test_fullload_defaults_to_100_percent(self):
        spec = _make_spec(
            blade_target="cpu",
            blade_action="fullload",
            params={},
            scope="pod",
        )
        with patch(
            "chaos_agent.agent._feasibility_checkers._fetch_cpu_usage_millicores",
            new_callable=AsyncMock, return_value=100,
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_cpu_limit_millicores",
            new_callable=AsyncMock, return_value=500,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.OK

    @pytest.mark.asyncio
    async def test_returns_none_without_names(self):
        spec = _make_spec(blade_target="cpu", names=())
        report = await assess_feasibility(spec, "/fake/kubeconfig")
        assert report is None


class TestAssessFeasibility:
    """Entry point dispatch + error handling tests."""

    @pytest.mark.asyncio
    async def test_unknown_blade_target_returns_none(self):
        spec = _make_spec(blade_target="jvm")
        report = await assess_feasibility(spec, "/fake/kubeconfig")
        assert report is None

    @pytest.mark.asyncio
    async def test_checker_exception_returns_none(self):
        spec = _make_spec(params={"mem-percent": "98"})
        with patch(
            "chaos_agent.agent._feasibility_checkers._fetch_memory_usage_mb",
            new_callable=AsyncMock, side_effect=RuntimeError("boom"),
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is None

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
            "chaos_agent.agent._feasibility_checkers.is_metrics_server_available",
            new_callable=AsyncMock, return_value=False,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.SKIPPED
        assert "metrics-server" in report.message
        assert report.headroom == 0.0

    @pytest.mark.asyncio
    async def test_skipped_for_cpu_target_too(self):
        spec = _make_spec(blade_target="cpu", blade_action="fullload", params={"cpu-percent": "80"})
        with patch(
            "chaos_agent.agent._feasibility_checkers.is_metrics_server_available",
            new_callable=AsyncMock, return_value=False,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.SKIPPED

    @pytest.mark.asyncio
    async def test_no_skip_for_unknown_blade_target(self):
        spec = _make_spec(blade_target="jvm")
        with patch(
            "chaos_agent.agent._feasibility_checkers.is_metrics_server_available",
            new_callable=AsyncMock, return_value=False,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        # jvm has no checker → returns None (not SKIPPED)
        assert report is None

    @pytest.mark.asyncio
    async def test_probe_ttl_cache(self):
        import chaos_agent.agent._feasibility_checkers as _fc
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
            "chaos_agent.agent._feasibility_checkers._resolve_first_pod",
            new_callable=AsyncMock, return_value="accounting-6fbdb464c7-qn2vr",
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_memory_usage_mb",
            new_callable=AsyncMock, return_value=100,
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_memory_limit_mb",
            new_callable=AsyncMock, return_value=240,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.OK

    @pytest.mark.asyncio
    async def test_cpu_checker_resolves_pod_via_labels(self):
        spec = _make_spec(
            blade_target="cpu",
            blade_action="fullload",
            names=("accounting",),
            labels={"app": "accounting"},
            params={},
        )
        with patch(
            "chaos_agent.agent._feasibility_checkers._resolve_first_pod",
            new_callable=AsyncMock, return_value="accounting-6fbdb464c7-qn2vr",
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_cpu_usage_millicores",
            new_callable=AsyncMock, return_value=100,
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_cpu_limit_millicores",
            new_callable=AsyncMock, return_value=500,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.OK

    @pytest.mark.asyncio
    async def test_resolve_fails_returns_none(self):
        spec = _make_spec(
            names=("accounting",),
            labels={"app": "accounting"},
            params={"mem-percent": "80"},
        )
        with patch(
            "chaos_agent.agent._feasibility_checkers._resolve_first_pod",
            new_callable=AsyncMock, return_value=None,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is None


class TestNetworkFeasibilityChecker:
    """NetworkFeasibilityChecker label resolution tests."""

    @pytest.mark.asyncio
    async def test_network_checker_resolves_pod_via_labels(self):
        spec = _make_spec(
            blade_target="network",
            blade_action="delay",
            names=("accounting",),
            labels={"app": "accounting"},
            params={"time": "3000", "interface": "eth0"},
        )
        with patch(
            "chaos_agent.agent._feasibility_checkers._resolve_first_pod",
            new_callable=AsyncMock, return_value="accounting-6fbdb464c7-qn2vr",
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_pod_phase",
            new_callable=AsyncMock, return_value="Running",
        ), patch(
            "chaos_agent.agent._feasibility_checkers._check_interface_exists",
            new_callable=AsyncMock, return_value=(True, ""),
        ), patch(
            "chaos_agent.agent._feasibility_checkers._check_iptables_available",
            new_callable=AsyncMock, return_value=(True, ""),
        ), patch(
            "chaos_agent.agent._feasibility_checkers._check_active_network_experiment",
            new_callable=AsyncMock, return_value=False,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.OK

    @pytest.mark.asyncio
    async def test_network_impossible_when_iptables_missing(self):
        spec = _make_spec(
            blade_target="network",
            blade_action="drop",
            names=("accounting-6fbdb464c7-qn2vr",),
            params={},
        )
        with patch(
            "chaos_agent.agent._feasibility_checkers._resolve_first_pod",
            new_callable=AsyncMock, return_value="accounting-6fbdb464c7-qn2vr",
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_pod_phase",
            new_callable=AsyncMock, return_value="Running",
        ), patch(
            "chaos_agent.agent._feasibility_checkers._check_interface_exists",
            new_callable=AsyncMock, return_value=(True, ""),
        ), patch(
            "chaos_agent.agent._feasibility_checkers._check_iptables_available",
            new_callable=AsyncMock, return_value=(False, "command not found"),
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.IMPOSSIBLE
        assert "iptables" in report.message

    @pytest.mark.asyncio
    async def test_network_warns_when_iptables_indeterminate(self):
        spec = _make_spec(
            blade_target="network",
            blade_action="drop",
            names=("accounting-6fbdb464c7-qn2vr",),
            params={},
        )
        with patch(
            "chaos_agent.agent._feasibility_checkers._resolve_first_pod",
            new_callable=AsyncMock, return_value="accounting-6fbdb464c7-qn2vr",
        ), patch(
            "chaos_agent.agent._feasibility_checkers._fetch_pod_phase",
            new_callable=AsyncMock, return_value="Running",
        ), patch(
            "chaos_agent.agent._feasibility_checkers._check_interface_exists",
            new_callable=AsyncMock, return_value=(True, ""),
        ), patch(
            "chaos_agent.agent._feasibility_checkers._check_iptables_available",
            new_callable=AsyncMock, return_value=(None, "context deadline exceeded"),
        ), patch(
            "chaos_agent.agent._feasibility_checkers._check_active_network_experiment",
            new_callable=AsyncMock, return_value=False,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is not None
        assert report.severity == FeasibilitySeverity.TIGHT
        assert "context deadline exceeded" in report.current_value

    @pytest.mark.asyncio
    async def test_network_resolve_fails_returns_none(self):
        spec = _make_spec(
            blade_target="network",
            blade_action="delay",
            names=("accounting",),
            labels={"app": "accounting"},
            params={"time": "3000"},
        )
        with patch(
            "chaos_agent.agent._feasibility_checkers._resolve_first_pod",
            new_callable=AsyncMock, return_value=None,
        ):
            report = await assess_feasibility(spec, "/fake/kubeconfig")

        assert report is None
