"""Tests for _conflict_check module: _extract_param_from_flag, _analyze_overlap, check_blade_conflicts."""

import json

import pytest

from chaos_agent.agent.nodes._conflict_check import (
    ConflictInfo,
    _extract_param_from_flag,
    _analyze_overlap,
    check_blade_conflicts,
)


class TestExtractParamFromFlag:
    """Tests for _extract_param_from_flag()."""

    def test_equals_format(self):
        flag = "--namespace=cms-demo --labels=app=accounting"
        assert _extract_param_from_flag(flag, "--namespace") == "cms-demo"
        assert _extract_param_from_flag(flag, "--labels") == "app=accounting"

    def test_space_format(self):
        flag = "--namespace cms-demo --labels app=accounting"
        assert _extract_param_from_flag(flag, "--namespace") == "cms-demo"
        assert _extract_param_from_flag(flag, "--labels") == "app=accounting"

    def test_mixed_formats(self):
        flag = "--namespace=cms-demo --labels app=accounting --timeout 180s"
        assert _extract_param_from_flag(flag, "--namespace") == "cms-demo"
        assert _extract_param_from_flag(flag, "--labels") == "app=accounting"
        assert _extract_param_from_flag(flag, "--timeout") == "180s"

    def test_param_not_present(self):
        flag = "--namespace cms-demo --cpu-percent 80"
        assert _extract_param_from_flag(flag, "--labels") == ""

    def test_empty_flag(self):
        assert _extract_param_from_flag("", "--namespace") == ""

    def test_param_name_with_dashes(self):
        flag = "--cpu-percent=80 --mem-percent=90"
        assert _extract_param_from_flag(flag, "--cpu-percent") == "80"
        assert _extract_param_from_flag(flag, "--mem-percent") == "90"

    def test_value_with_equals_sign(self):
        # Labels value may contain = in key=value format
        flag = "--labels app=accounting,version=v1"
        assert _extract_param_from_flag(flag, "--labels") == "app=accounting,version=v1"

    def test_param_at_end_of_flag(self):
        flag = "--namespace cms-demo --timeout 180s --names pod1"
        assert _extract_param_from_flag(flag, "--names") == "pod1"

    def test_comma_separated_names(self):
        flag = "--namespace cms-demo --names pod1,pod2,pod3"
        assert _extract_param_from_flag(flag, "--names") == "pod1,pod2,pod3"

    def test_single_dash_param_name(self):
        # Function strips leading dashes, so "-" prefix works too
        flag = "--namespace cms-demo"
        assert _extract_param_from_flag(flag, "namespace") == "cms-demo"


class TestAnalyzeOverlap:
    """Tests for _analyze_overlap()."""

    def _make_conflict(self, **kwargs):
        defaults = {
            "uid": "abc123def4567890",
            "flag": "",
            "namespace": "",
            "names": "",
            "labels": "",
        }
        defaults.update(kwargs)
        return ConflictInfo(**defaults)

    def test_same_namespace_same_name_overlaps(self):
        ci = self._make_conflict(namespace="cms-demo", names="pod-1")
        _analyze_overlap(ci, "cms-demo", "pod-1", "")
        assert ci.overlaps_target is True
        assert "same target" in ci.overlap_reason
        assert "pod-1" in ci.overlap_reason

    def test_same_namespace_different_name_no_overlap(self):
        ci = self._make_conflict(namespace="cms-demo", names="pod-1")
        _analyze_overlap(ci, "cms-demo", "pod-2", "")
        assert ci.overlaps_target is False
        assert ci.overlap_reason == ""

    def test_different_namespace_same_name_no_overlap(self):
        ci = self._make_conflict(namespace="other-ns", names="pod-1")
        _analyze_overlap(ci, "cms-demo", "pod-1", "")
        assert ci.overlaps_target is False

    def test_comma_separated_names_partial_overlap(self):
        ci = self._make_conflict(namespace="cms-demo", names="pod-1,pod-2")
        _analyze_overlap(ci, "cms-demo", "pod-2,pod-3", "")
        assert ci.overlaps_target is True
        assert "pod-2" in ci.overlap_reason

    def test_same_namespace_same_labels_overlaps(self):
        ci = self._make_conflict(namespace="cms-demo", labels="app=accounting")
        _analyze_overlap(ci, "cms-demo", "", "app=accounting")
        assert ci.overlaps_target is True
        assert "same labels" in ci.overlap_reason

    def test_same_namespace_partial_labels_overlap(self):
        ci = self._make_conflict(
            namespace="cms-demo", labels="app=accounting,version=v1"
        )
        _analyze_overlap(ci, "cms-demo", "", "app=accounting,version=v2")
        assert ci.overlaps_target is True
        assert "app=accounting" in ci.overlap_reason

    def test_same_namespace_different_labels_no_overlap(self):
        ci = self._make_conflict(namespace="cms-demo", labels="app=billing")
        _analyze_overlap(ci, "cms-demo", "", "app=accounting")
        assert ci.overlaps_target is False

    def test_both_name_and_labels_overlap(self):
        ci = self._make_conflict(
            namespace="cms-demo", names="pod-1", labels="app=accounting"
        )
        _analyze_overlap(ci, "cms-demo", "pod-1", "app=accounting")
        assert ci.overlaps_target is True
        assert "same target" in ci.overlap_reason
        assert "same labels" in ci.overlap_reason

    def test_no_namespace_info_no_overlap(self):
        ci = self._make_conflict(names="pod-1")
        _analyze_overlap(ci, "cms-demo", "pod-1", "")
        assert ci.overlaps_target is False

    def test_empty_target_no_overlap(self):
        ci = self._make_conflict(namespace="cms-demo", names="pod-1")
        _analyze_overlap(ci, "cms-demo", "", "")
        assert ci.overlaps_target is False

    def test_conflict_no_names_no_labels_no_overlap(self):
        ci = self._make_conflict(namespace="cms-demo")
        _analyze_overlap(ci, "cms-demo", "pod-1", "app=accounting")
        assert ci.overlaps_target is False


class TestDestroyedStatusFilter:
    """Tests that Destroyed/Revoked experiments are excluded from conflict detection."""

    def _make_blade_status_mock(self, monkeypatch, blade_output: str):
        """Patch run_command inside the function's local import."""
        async def fake_run_command(cmd, **kwargs):
            from chaos_agent.tools.shell import CommandResult
            if "get" in cmd and "pods" in cmd:
                return CommandResult(
                    stdout="NAME                READY   STATUS    RESTARTS   AGE\notel-c-tool-abc12   1/1     Running   0          32d\n",
                    stderr="", exit_code=0, duration_ms=100.0,
                )
            return CommandResult(
                stdout=blade_output,
                stderr="", exit_code=0, duration_ms=200.0,
            )
        # run_command is imported dynamically inside the function,
        # so we must patch it at the source module.
        monkeypatch.setattr("chaos_agent.tools.shell.run_command", fake_run_command)

    @pytest.mark.asyncio
    async def test_destroyed_experiments_excluded(self, monkeypatch):
        """Destroyed experiments should not appear in conflict list."""
        self._make_blade_status_mock(monkeypatch, json.dumps({
            "code": 200, "success": True,
            "result": [
                {"Uid": "aaaa0000bbbb1111", "Flag": "k8s pod-disk burn --namespace=cms-demo --names=pod-1 --timeout 600", "Status": "Destroyed"},
                {"Uid": "cccc2222dddd3333", "Flag": "k8s pod-disk burn --namespace=cms-demo --names=pod-1 --timeout 600", "Status": "Running"},
            ]
        }))

        uids, details = await check_blade_conflicts(
            kubeconfig="/tmp/config", task_id="",
            namespace="cms-demo", target_names="pod-1",
        )
        assert len(uids) == 1
        assert "cccc2222dddd3333" in uids
        assert "aaaa0000bbbb1111" not in uids

    @pytest.mark.asyncio
    async def test_revoked_experiments_excluded(self, monkeypatch):
        """Revoked experiments should not appear in conflict list."""
        self._make_blade_status_mock(monkeypatch, json.dumps({
            "code": 200, "success": True,
            "result": [
                {"Uid": "eeee4444ffff5555", "Flag": "k8s pod-cpu fullload --namespace=test --labels=app=myapp", "Status": "Revoked"},
                {"Uid": "gggg6666hhhh7777", "Flag": "k8s pod-cpu fullload --namespace=test --labels=app=myapp", "Status": "Success"},
            ]
        }))

        uids, details = await check_blade_conflicts(
            kubeconfig="/tmp/config", task_id="",
            namespace="test", labels="app=myapp",
        )
        assert len(uids) == 1
        assert "gggg6666hhhh7777" in uids
        assert "eeee4444ffff5555" not in uids

    @pytest.mark.asyncio
    async def test_all_destroyed_returns_empty(self, monkeypatch):
        """When all experiments are Destroyed, conflict list should be empty."""
        self._make_blade_status_mock(monkeypatch, json.dumps({
            "code": 200, "success": True,
            "result": [
                {"Uid": "aaaa0000bbbb1111", "Flag": "k8s pod-disk burn --namespace=cms-demo --names=pod-1", "Status": "Destroyed"},
                {"Uid": "cccc2222dddd3333", "Flag": "k8s pod-disk burn --namespace=cms-demo --names=pod-2", "Status": "Destroyed"},
            ]
        }))

        uids, details = await check_blade_conflicts(
            kubeconfig="/tmp/config", task_id="",
            namespace="cms-demo", target_names="pod-1",
        )
        assert uids == []
        assert details == []

    @pytest.mark.asyncio
    async def test_running_experiments_counted_as_overlap(self, monkeypatch):
        """Only Running/Success experiments should trigger target overlap."""
        self._make_blade_status_mock(monkeypatch, json.dumps({
            "code": 200, "success": True,
            "result": [
                {"Uid": "overlap1112223334", "Flag": "k8s pod-disk burn --namespace=cms-demo --names=accounting-6fbdb464c7-zf458 --timeout 600", "Status": "Running"},
                {"Uid": "destroyed5556667778", "Flag": "k8s pod-disk burn --namespace=cms-demo --names=accounting-6fbdb464c7-zf458 --timeout 600", "Status": "Destroyed"},
            ]
        }))

        uids, details = await check_blade_conflicts(
            kubeconfig="/tmp/config", task_id="",
            namespace="cms-demo", target_names="accounting-6fbdb464c7-zf458",
            request_scope_target_action="pod-disk-burn",
        )
        assert len(uids) == 1
        assert len(details) == 1
        assert details[0].overlaps_target is True
