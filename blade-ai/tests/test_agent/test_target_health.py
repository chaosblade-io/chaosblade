"""Tests for Patch D — pluggable target health pre-check."""

from __future__ import annotations

import pytest

from chaos_agent.agent.target_health import (
    HealthIssue,
    HealthReport,
    HealthSeverity,
    NodeHealthChecker,
    PodHealthChecker,
    _build_node_report,
    _build_pod_report,
    _format_condition_duration,
    assess_target_health,
    register_health_checker,
)


# ---------------------------------------------------------------------------
# _build_node_report (pure helper)
# ---------------------------------------------------------------------------


class TestBuildNodeReport:
    def test_no_conditions_is_ok(self):
        report = _build_node_report({"names": ["node-x"]}, [])
        assert report.overall == HealthSeverity.OK
        assert report.issues == []

    def test_disk_pressure_true_blocks(self):
        conditions = [
            {"type": "DiskPressure", "status": "True", "lastTransitionTime": ""},
            {"type": "Ready", "status": "True"},
        ]
        report = _build_node_report({"names": ["node-x"]}, conditions)
        assert report.overall == HealthSeverity.BLOCK
        assert len(report.issues) == 1
        assert report.issues[0].code == "node.disk_pressure"
        assert report.issues[0].severity == HealthSeverity.BLOCK

    def test_multiple_pressures(self):
        conditions = [
            {"type": "DiskPressure", "status": "True"},
            {"type": "MemoryPressure", "status": "True"},
            {"type": "PIDPressure", "status": "False"},  # not blocking
        ]
        report = _build_node_report({"names": ["n"]}, conditions)
        assert report.overall == HealthSeverity.BLOCK
        codes = {i.code for i in report.issues}
        assert codes == {"node.disk_pressure", "node.memory_pressure"}

    def test_status_false_is_ignored(self):
        # All "False" → no issues, OK
        conditions = [
            {"type": "DiskPressure", "status": "False"},
            {"type": "Ready", "status": "True"},
        ]
        report = _build_node_report({"names": ["n"]}, conditions)
        assert report.overall == HealthSeverity.OK

    def test_unknown_condition_type_ignored(self):
        # NotARealCondition → not in our blocking map, ignored
        conditions = [{"type": "NotARealCondition", "status": "True"}]
        report = _build_node_report({"names": ["n"]}, conditions)
        assert report.overall == HealthSeverity.OK

    def test_summary_format(self):
        conditions = [
            {"type": "DiskPressure", "status": "True"},
        ]
        report = _build_node_report({"names": ["n"]}, conditions)
        assert "node.disk_pressure" in report.summary()
        assert "block" in report.summary()


# ---------------------------------------------------------------------------
# _build_pod_report
# ---------------------------------------------------------------------------


class TestBuildPodReport:
    def test_running_pod_is_ok(self):
        report = _build_pod_report({"names": ["p"]}, {"phase": "Running"})
        assert report.overall == HealthSeverity.OK

    def test_evicted_pod_blocks(self):
        # The exact case the user log showed — otel-c-tool Evicted
        report = _build_pod_report(
            {"names": ["otel-c-tool-w2qv9"]},
            {"phase": "Failed", "reason": "Evicted"},
        )
        assert report.overall == HealthSeverity.BLOCK
        codes = {i.code for i in report.issues}
        assert "pod.reason.evicted" in codes

    def test_crashloop_blocks(self):
        report = _build_pod_report(
            {"names": ["p"]},
            {"phase": "Running", "reason": "CrashLoopBackOff"},
        )
        assert report.overall == HealthSeverity.BLOCK

    def test_pending_is_warn(self):
        report = _build_pod_report({"names": ["p"]}, {"phase": "Pending"})
        # Pending alone is WARN (might be transient scheduling delay)
        assert report.overall == HealthSeverity.WARN


# ---------------------------------------------------------------------------
# _format_condition_duration
# ---------------------------------------------------------------------------


class TestFormatDuration:
    def test_empty_returns_empty(self):
        assert _format_condition_duration("") == ""

    def test_invalid_returns_empty(self):
        assert _format_condition_duration("not-a-timestamp") == ""

    def test_days_format(self):
        # 2 days ago — should produce "2d"
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        result = _format_condition_duration(ts)
        # Allow ±1d slack for test-execution boundary
        assert result in {"1d", "2d", "3d"}


# ---------------------------------------------------------------------------
# assess_target_health (entry point)
# ---------------------------------------------------------------------------


class TestAssessTargetHealth:
    @pytest.mark.asyncio
    async def test_unknown_scope_returns_ok(self):
        report = await assess_target_health("future-scope", {"x": 1}, "")
        assert report.overall == HealthSeverity.OK
        assert report.issues == []

    @pytest.mark.asyncio
    async def test_node_with_disk_pressure(self, monkeypatch):
        from chaos_agent.agent import target_health

        async def fake_query(name, kc):
            return [
                {"type": "DiskPressure", "status": "True"},
            ]

        monkeypatch.setattr(target_health, "_query_node_conditions", fake_query)
        report = await assess_target_health(
            "node", {"names": ["n1"]}, ""
        )
        assert report.overall == HealthSeverity.BLOCK

    @pytest.mark.asyncio
    async def test_pod_evicted(self, monkeypatch):
        from chaos_agent.agent import target_health

        async def fake_query(name, ns, kc):
            return {"phase": "Failed", "reason": "Evicted"}

        monkeypatch.setattr(target_health, "_query_pod_status", fake_query)
        report = await assess_target_health(
            "pod", {"names": ["p1"], "namespace": "default"}, ""
        )
        assert report.overall == HealthSeverity.BLOCK

    @pytest.mark.asyncio
    async def test_checker_exception_degrades_gracefully(self, monkeypatch):
        from chaos_agent.agent import target_health

        async def boom(name, kc):
            raise RuntimeError("checker bug")

        monkeypatch.setattr(target_health, "_query_node_conditions", boom)
        # Must NOT propagate exception; report comes back empty/OK.
        report = await assess_target_health("node", {"names": ["n"]}, "")
        assert report.overall == HealthSeverity.OK

    @pytest.mark.asyncio
    async def test_empty_target_names_is_ok(self):
        report = await assess_target_health("node", {"names": []}, "")
        assert report.overall == HealthSeverity.OK


# ---------------------------------------------------------------------------
# _resolve_pod_names — label-based pod resolution
# ---------------------------------------------------------------------------


class TestResolvePodNames:
    @pytest.mark.asyncio
    async def test_labels_resolve_all_pods(self, monkeypatch):
        from chaos_agent.agent import target_health

        async def fake_resolve(t, kc):
            assert t.get("labels")
            return ["accounting-6fb-qn2vr", "accounting-6fb-xqhdd", "accounting-6fb-zf458"]

        monkeypatch.setattr(target_health, "_resolve_pod_names", fake_resolve)

        queried_pods = []

        async def fake_status(name, ns, kc):
            queried_pods.append(name)
            return {"phase": "Running"}

        monkeypatch.setattr(target_health, "_query_pod_status", fake_status)
        checker = PodHealthChecker()
        report = await checker.check(
            {"labels": {"app": "accounting"}, "names": ["accounting"], "namespace": "cms-demo"},
            "",
        )
        assert report.overall == HealthSeverity.OK
        assert len(queried_pods) == 3

    @pytest.mark.asyncio
    async def test_one_unhealthy_pod_among_healthy(self, monkeypatch):
        from chaos_agent.agent import target_health

        async def fake_resolve(t, kc):
            return ["pod-a", "pod-b", "pod-c"]

        monkeypatch.setattr(target_health, "_resolve_pod_names", fake_resolve)

        async def fake_status(name, ns, kc):
            if name == "pod-b":
                return {"phase": "Failed", "reason": "Evicted"}
            return {"phase": "Running"}

        monkeypatch.setattr(target_health, "_query_pod_status", fake_status)
        report = await assess_target_health(
            "pod",
            {"labels": {"app": "myapp"}, "namespace": "default"},
            "",
        )
        assert report.overall == HealthSeverity.BLOCK
        assert any("pod-b" in i.message for i in report.issues)

    @pytest.mark.asyncio
    async def test_no_labels_uses_names(self):
        from chaos_agent.agent import target_health

        target = {"names": ["exact-pod-123"], "namespace": "ns"}
        result = await target_health._resolve_pod_names(target, "")
        assert result == ["exact-pod-123"]

    @pytest.mark.asyncio
    async def test_no_labels_no_names_returns_empty(self):
        from chaos_agent.agent import target_health

        result = await target_health._resolve_pod_names({"namespace": "ns"}, "")
        assert result == []

    @pytest.mark.asyncio
    async def test_all_pods_healthy(self, monkeypatch):
        from chaos_agent.agent import target_health

        async def fake_resolve(t, kc):
            return ["accounting-6fb-qn2vr"]

        async def fake_status(name, ns, kc):
            return {"phase": "Running"}

        monkeypatch.setattr(target_health, "_resolve_pod_names", fake_resolve)
        monkeypatch.setattr(target_health, "_query_pod_status", fake_status)

        report = await assess_target_health(
            "pod",
            {"labels": {"app": "accounting"}, "names": ["accounting"], "namespace": "cms-demo"},
            "",
        )
        assert report.overall == HealthSeverity.OK


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


class TestRegisterHealthChecker:
    @pytest.mark.asyncio
    async def test_register_new_scope(self):
        class MyChecker:
            scope = "service"
            async def check(self, target, kubeconfig):
                return HealthReport(
                    target=target,
                    overall=HealthSeverity.WARN,
                    issues=[HealthIssue(
                        severity=HealthSeverity.WARN,
                        code="service.no_endpoints",
                        message="empty endpoints",
                    )],
                )

        register_health_checker(MyChecker())
        report = await assess_target_health("service", {"name": "svc-x"}, "")
        assert report.overall == HealthSeverity.WARN
        assert report.issues[0].code == "service.no_endpoints"


# ---------------------------------------------------------------------------
# HealthReport serialisation
# ---------------------------------------------------------------------------


class TestSafetyCheckIntegration:
    """Patch D — end-to-end: safety_check must attach the report to
    state, never break on health-check exceptions, and respect the
    ``block_on_blocker`` opt-in."""

    @pytest.mark.asyncio
    async def test_safety_check_attaches_warn_report_without_blocking(
        self, monkeypatch
    ):
        from chaos_agent.agent import target_health
        from chaos_agent.agent.nodes import safety_check as safety_check_module
        from chaos_agent.config.settings import settings

        # Make sure block_on_blocker is OFF (default) — even a BLOCK
        # report should NOT flip safety_status to rejected.
        monkeypatch.setattr(
            settings, "target_health_check_block_on_blocker", False
        )
        monkeypatch.setattr(settings, "target_health_check_enabled", True)

        async def fake_query(name, kc):
            return [{"type": "DiskPressure", "status": "True"}]

        monkeypatch.setattr(
            target_health, "_query_node_conditions", fake_query
        )

        # Stub _conflict_check.check_blade_conflicts so we don't need
        # real cluster access. The function is called inside
        # safety_check, so patch where it's used.
        async def no_conflicts(*_args, **_kwargs):
            # check_blade_conflicts(kubeconfig, task_id, *, namespace,
            # labels, target_names, request_scope_target_action) →
            # (uids, details). Return empty/zero so the inject is
            # eligible to reach our health-check insertion point.
            return [], []

        monkeypatch.setattr(
            safety_check_module, "check_blade_conflicts", no_conflicts
        )
        # safety_check skips conflict detection when kubeconfig is
        # empty, but our path through ``_resolve_kubeconfig`` may
        # return a non-empty default. Stub it to "" so the conflict
        # block is skipped entirely — the health-check stub above is
        # what we're actually verifying.
        monkeypatch.setattr(
            safety_check_module, "_resolve_kubeconfig", lambda _s: ""
        )
        # safety_check rejects when kubeconfig is empty (it skips
        # conflict detection) — but more importantly, it short-circuits
        # to "rejected" if ``skill_name`` is empty (step 2). Provide a
        # value so the flow reaches our health-check insertion point.
        state = {
            "task_id": "t-test",
            "blade_scope": "node",
            "skill_name": "k8s-chaos-skills",
            "target": {"namespace": "default", "names": ["n-bad"]},
            "fault_intent": {"namespace": "default"},
            "messages": [],
        }
        out = await safety_check_module.safety_check(state)
        # No kubeconfig → conflict check skipped → warning (not rejected)
        assert out.get("safety_status") in ("safe", "warning")
        assert "target_health_report" in out
        report = out["target_health_report"]
        assert report["overall"] == "block"
        assert any(
            i["code"] == "node.disk_pressure" for i in report["issues"]
        )

    @pytest.mark.asyncio
    async def test_safety_check_blocks_when_opted_in(self, monkeypatch):
        from chaos_agent.agent import target_health
        from chaos_agent.agent.nodes import safety_check as safety_check_module
        from chaos_agent.config.settings import settings

        monkeypatch.setattr(
            settings, "target_health_check_block_on_blocker", True
        )
        monkeypatch.setattr(settings, "target_health_check_enabled", True)

        async def fake_query(name, kc):
            return [{"type": "DiskPressure", "status": "True"}]

        monkeypatch.setattr(
            target_health, "_query_node_conditions", fake_query
        )

        async def no_conflicts(*_args, **_kwargs):
            # check_blade_conflicts(kubeconfig, task_id, *, namespace,
            # labels, target_names, request_scope_target_action) →
            # (uids, details). Return empty/zero so the inject is
            # eligible to reach our health-check insertion point.
            return [], []

        monkeypatch.setattr(
            safety_check_module, "check_blade_conflicts", no_conflicts
        )
        # safety_check skips conflict detection when kubeconfig is
        # empty, but our path through ``_resolve_kubeconfig`` may
        # return a non-empty default. Stub it to "" so the conflict
        # block is skipped entirely — the health-check stub above is
        # what we're actually verifying.
        monkeypatch.setattr(
            safety_check_module, "_resolve_kubeconfig", lambda _s: ""
        )

        state = {
            "task_id": "t-test",
            "blade_scope": "node",
            "skill_name": "k8s-chaos-skills",
            "target": {"namespace": "default", "names": ["n-bad"]},
            "fault_intent": {"namespace": "default"},
            "messages": [],
        }
        out = await safety_check_module.safety_check(state)
        # With opt-in, BLOCK report flips to rejected
        assert out.get("safety_status") == "rejected"
        assert "DiskPressure" in (out.get("safety_reason") or "") or \
            "node.disk_pressure" in (out.get("safety_reason") or "")
        assert "target_health_report" in out

    @pytest.mark.asyncio
    async def test_safety_check_swallows_health_check_exceptions(
        self, monkeypatch
    ):
        """A bug in the health checker must NOT take down inject."""
        from chaos_agent.agent import target_health
        from chaos_agent.agent.nodes import safety_check as safety_check_module
        from chaos_agent.config.settings import settings

        monkeypatch.setattr(settings, "target_health_check_enabled", True)

        async def boom(name, kc):
            raise RuntimeError("checker bug")

        monkeypatch.setattr(target_health, "_query_node_conditions", boom)

        async def no_conflicts(*_args, **_kwargs):
            # check_blade_conflicts(kubeconfig, task_id, *, namespace,
            # labels, target_names, request_scope_target_action) →
            # (uids, details). Return empty/zero so the inject is
            # eligible to reach our health-check insertion point.
            return [], []

        monkeypatch.setattr(
            safety_check_module, "check_blade_conflicts", no_conflicts
        )
        # safety_check skips conflict detection when kubeconfig is
        # empty, but our path through ``_resolve_kubeconfig`` may
        # return a non-empty default. Stub it to "" so the conflict
        # block is skipped entirely — the health-check stub above is
        # what we're actually verifying.
        monkeypatch.setattr(
            safety_check_module, "_resolve_kubeconfig", lambda _s: ""
        )

        state = {
            "task_id": "t-test",
            "blade_scope": "node",
            "skill_name": "k8s-chaos-skills",
            "target": {"namespace": "default", "names": ["n"]},
            "fault_intent": {"namespace": "default"},
            "messages": [],
        }
        out = await safety_check_module.safety_check(state)
        # Should still pass safety — health check failure is swallowed
        # via assess_target_health's try/except (returns OK on bug);
        # the report attachment may or may not be present, but
        # safety must not be rejected.
        assert out.get("safety_status") in ("safe", "warning")


class TestHealthReportToDict:
    def test_to_dict_round_trip(self):
        report = HealthReport(
            target={"names": ["n"]},
            overall=HealthSeverity.BLOCK,
            issues=[
                HealthIssue(
                    severity=HealthSeverity.BLOCK,
                    code="node.disk_pressure",
                    message="DiskPressure=True for 103d",
                    duration_hint="103d",
                ),
            ],
        )
        d = report.to_dict()
        assert d["overall"] == "block"
        assert d["issues"][0]["code"] == "node.disk_pressure"
        assert d["issues"][0]["duration_hint"] == "103d"
        assert "summary" in d

    def test_is_blocking(self):
        ok = HealthReport(target={}, overall=HealthSeverity.OK)
        warn = HealthReport(target={}, overall=HealthSeverity.WARN)
        block = HealthReport(target={}, overall=HealthSeverity.BLOCK)
        assert ok.is_blocking() is False
        assert warn.is_blocking() is False
        assert block.is_blocking() is True


# ---------------------------------------------------------------------------
# Node Ready condition (improvement 2)
# ---------------------------------------------------------------------------


class TestNodeReadyCondition:
    def test_not_ready_blocks(self):
        conditions = [
            {"type": "Ready", "status": "False", "lastTransitionTime": "2026-05-20T10:00:00Z"},
        ]
        report = _build_node_report({"names": ["node-1"]}, conditions)
        assert report.overall == HealthSeverity.BLOCK
        assert any(i.code == "node.not_ready" for i in report.issues)
        assert "NotReady" in report.issues[0].message

    def test_ready_unknown_blocks(self):
        conditions = [
            {"type": "Ready", "status": "Unknown", "lastTransitionTime": ""},
        ]
        report = _build_node_report({"names": ["node-1"]}, conditions)
        assert report.overall == HealthSeverity.BLOCK
        assert report.issues[0].code == "node.not_ready"

    def test_ready_true_is_ok(self):
        conditions = [
            {"type": "Ready", "status": "True"},
        ]
        report = _build_node_report({"names": ["node-1"]}, conditions)
        assert report.overall == HealthSeverity.OK

    def test_not_ready_plus_pressure(self):
        conditions = [
            {"type": "Ready", "status": "False", "lastTransitionTime": ""},
            {"type": "DiskPressure", "status": "True", "lastTransitionTime": ""},
        ]
        report = _build_node_report({"names": ["node-1"]}, conditions)
        assert report.overall == HealthSeverity.BLOCK
        codes = {i.code for i in report.issues}
        assert "node.not_ready" in codes
        assert "node.disk_pressure" in codes


# ---------------------------------------------------------------------------
# ChaosBlade agent existence (improvement 4)
# ---------------------------------------------------------------------------


class TestBladeAgentCheck:
    @pytest.mark.asyncio
    async def test_agent_missing_blocks(self, monkeypatch):
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "chaos_agent.agent.target_health._query_node_conditions",
            AsyncMock(return_value=[{"type": "Ready", "status": "True"}]),
        )
        monkeypatch.setattr(
            "chaos_agent.agent.target_health._query_blade_agent_on_node",
            AsyncMock(return_value=False),
        )
        report = await assess_target_health(
            "node",
            {"names": ["worker-01"], "namespace": "", "labels": {}, "resource_type": "node"},
            kubeconfig="/fake/kubeconfig",
        )
        assert report.overall == HealthSeverity.BLOCK
        assert any(i.code == "node.chaosblade_tool_missing" for i in report.issues)

    @pytest.mark.asyncio
    async def test_agent_present_is_ok(self, monkeypatch):
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "chaos_agent.agent.target_health._query_node_conditions",
            AsyncMock(return_value=[{"type": "Ready", "status": "True"}]),
        )
        monkeypatch.setattr(
            "chaos_agent.agent.target_health._query_blade_agent_on_node",
            AsyncMock(return_value=True),
        )
        report = await assess_target_health(
            "node",
            {"names": ["worker-01"], "namespace": "", "labels": {}, "resource_type": "node"},
            kubeconfig="/fake/kubeconfig",
        )
        assert report.overall == HealthSeverity.OK

    @pytest.mark.asyncio
    async def test_agent_check_disabled(self, monkeypatch):
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "chaos_agent.agent.target_health._query_node_conditions",
            AsyncMock(return_value=[{"type": "Ready", "status": "True"}]),
        )
        monkeypatch.setattr(
            "chaos_agent.config.settings.settings.blade_agent_check_enabled", False
        )
        # _query_blade_agent_on_node is NOT mocked — should never be called
        report = await assess_target_health(
            "node",
            {"names": ["worker-01"], "namespace": "", "labels": {}, "resource_type": "node"},
            kubeconfig="/fake/kubeconfig",
        )
        assert report.overall == HealthSeverity.OK
