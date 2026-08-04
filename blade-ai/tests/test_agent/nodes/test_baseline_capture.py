"""Tests for baseline_capture node."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chaos_agent.agent.nodes.baseline.baseline_capture import (
    BaselineCommand,
    BASELINE_COMMANDS,
    _LLM_BASELINE_MAX_RETRIES,
    _TOOL_POD_NAMESPACE,
    _llm_retry_failed_commands,
    _lookup_baseline_commands,
    _resolve_templates,
    _parse_debug_pod_name,
    _parse_llm_json_output,
    _target_coverage,
    _validate_and_filter_commands,
    _normalize_debug_namespace,
    _evidence_supplement_commands,
    make_baseline_capture,
)
from chaos_agent.agent.nodes.baseline._baseline_profiles import (
    build_baseline_system_prompt,
)
from chaos_agent.agent.spec.fault_spec import FaultSpec


# ---------------------------------------------------------------------------
# Registry three-level lookup
# ---------------------------------------------------------------------------


class TestRegistryLookup:
    """Test _lookup_baseline_commands three-level fallback."""

    def test_exact_match(self):
        result = _lookup_baseline_commands("k8s", "node", "disk", "fill")
        assert len(result) == 2
        assert result[0].description == "Node DiskPressure"
        assert result[1].mode == "debug_two_step"

    def test_target_fallback(self):
        result = _lookup_baseline_commands("k8s", "node", "disk", "nonexistent_action")
        assert len(result) == 2
        assert result[0].description == "Node DiskPressure"

    def test_scope_fallback_returns_empty_for_unknown_target(self):
        """_lookup_baseline_commands only searches BASELINE_COMMANDS; scope-level
        fallback is handled by _SCOPE_FALLBACK in the node function."""
        result = _lookup_baseline_commands("k8s", "node", "nonexistent", "action")
        assert result == []

    def test_no_match(self):
        result = _lookup_baseline_commands("k8s", "container", "nonexistent", "action")
        assert result == []

    def test_host_lookup_by_target(self):
        """host profile keys the registry by blade target only."""
        result = _lookup_baseline_commands("host", "node", "cpu", "fullload")
        assert [c.command for c in result] == ["top -bn1"]
        disk = _lookup_baseline_commands("host", "node", "disk", "burn")
        assert [c.command for c in disk] == ["df -h", "iostat -xd 1 2"]

    def test_host_lookup_unknown_target_empty(self):
        assert _lookup_baseline_commands("host", "node", "nonexistent", "x") == []


# ---------------------------------------------------------------------------
# Template resolution
# ---------------------------------------------------------------------------


class TestTemplateResolution:
    """Test _resolve_templates variable substitution."""

    def test_simple_resolution(self):
        state = {
            "target": {
                "namespace": "cms-demo",
                "names": ["cn-hongkong.10.0.2.69"],
                "labels": {"app": "accounting"},
            },
        }
        cmds = [BaselineCommand("Node DiskPressure", "kubectl describe node {node_name}")]
        result = _resolve_templates(cmds, state)
        assert len(result) == 1
        assert result[0]["v_args"] == "node cn-hongkong.10.0.2.69"
        assert result[0]["_unresolved"] is False

    def test_unresolved_namespace(self):
        state = {"target": {"names": ["my-pod"]}}
        cmds = [BaselineCommand("Pod info", "kubectl get pod {pod_name} -n {namespace}")]
        result = _resolve_templates(cmds, state)
        assert len(result) == 1
        assert result[0]["_unresolved"] is True

    def test_label_selector_resolution(self):
        state = {
            "target": {
                "namespace": "default",
                "names": [],
                "labels": {"app": "nginx", "tier": "frontend"},
            },
        }
        # ``{label_selector}`` 渲染时已含 ``-l `` 前缀，模板里不再叠 ``-l``。
        cmds = [BaselineCommand("Pod CPU", "kubectl top pod -n {namespace} {label_selector}")]
        result = _resolve_templates(cmds, state)
        assert len(result) == 1
        assert result[0]["v_args"] == "pod -n default -l app=nginx,tier=frontend"
        assert result[0]["_unresolved"] is False

    def test_multi_target_expansion_marks_sampled_target_entries(self):
        state = {
            "blade_scope": "node",
            "target": {"names": [f"node-{index}" for index in range(12)]},
        }
        result = _resolve_templates(
            [BaselineCommand("Node status", "kubectl describe node {node_name}")],
            state,
        )

        assert len(result) == 10
        assert all(entry["_target_sampled"] for entry in result)
        assert {entry["_target_name"] for entry in result} <= {
            f"node-{index}" for index in range(12)
        }


class TestTargetCoverage:
    def test_aggregate_baseline_reports_partial_coverage_for_az_wide_target(self):
        spec = FaultSpec(
            scope="node",
            names=tuple(f"node-{index}" for index in range(4)),
            blade_target="network",
        )
        coverage = _target_coverage(
            spec,
            [{"description": "Nodes", "command": "kubectl get nodes node-0 node-2"}],
            [{"command": "kubectl get nodes node-0 node-2", "stdout": "node-0\nnode-2"}],
        )

        assert coverage["collection_mode"] == "aggregate_or_llm"
        assert coverage["observed_names"] == ["node-0", "node-2"]
        assert coverage["missing_count"] == 2
        assert coverage["complete"] is False

    def test_target_name_is_not_matched_as_a_prefix_of_another_name(self):
        spec = FaultSpec(
            scope="node", names=("node-1", "node-10"), blade_target="network",
        )
        coverage = _target_coverage(
            spec, [], [{"stdout": "node-10 Ready"}],
        )

        assert coverage["observed_names"] == ["node-10"]
        assert coverage["missing_names"] == ["node-1"]


class TestEvidenceSupplements:
    def test_host_identity_and_cross_metric_are_added_for_incomplete_baseline(self):
        supplements = _evidence_supplement_commands(
            "host",
            FaultSpec(scope="node", blade_target="mem"),
            [{"description": "Host memory", "command": "free -m"}],
        )

        # A single ``free`` observation cannot count as both primary and
        # independent cross evidence.
        assert [command.command for command in supplements] == [
            "hostname",
            "vmstat -s",
        ]

    def test_k8s_cross_metric_is_added_without_guessing_a_new_target(self):
        supplements = _evidence_supplement_commands(
            "k8s",
            FaultSpec(scope="node", names=("node-a",), blade_target="cpu"),
            [{"description": "Node CPU", "command": "kubectl top node node-a"}],
        )

        assert [command.command for command in supplements] == [
            "kubectl describe node {node_name}",
        ]

    def test_container_scope_collects_identity_from_its_owning_pod(self):
        supplements = _evidence_supplement_commands(
            "k8s",
            FaultSpec(
                scope="container",
                namespace="prod",
                names=("api-0",),
                blade_target="cpu",
            ),
            [],
        )

        assert [command.command for command in supplements] == [
            "kubectl get pod {pod_name} -n {namespace}",
            "kubectl describe pod {pod_name} -n {namespace}",
        ]


class TestTemplateResolutionNodeScope:
    """Fix C: _resolve_templates must not set pod_name for node-scope.

    For node-scope, names contains node names — using them as pod_name
    produces incorrect baseline commands (e.g. kubectl exec into a "pod"
    that is actually a node name).
    """

    def test_node_scope_pod_name_unresolved(self):
        """When blade_scope=node, {pod_name} should remain unresolved
        even though names is non-empty."""
        state = {
            "blade_scope": "node",
            "target": {
                "namespace": "",
                "names": ["cn-hongkong.10.0.1.120"],
            },
        }
        cmds = [BaselineCommand("Pod info", "kubectl exec {pod_name} -n {namespace} -- df -h")]
        result = _resolve_templates(cmds, state)
        assert len(result) == 1
        # pod_name should NOT be resolved (node name is not a pod name)
        assert result[0]["_unresolved"] is True
        assert "cn-hongkong" not in result[0]["v_args"]

    def test_pod_scope_pod_name_resolved(self):
        """When blade_scope=pod, {pod_name} should still be resolved normally."""
        state = {
            "blade_scope": "pod",
            "target": {
                "namespace": "cms-demo",
                "names": ["accounting-abc"],
            },
        }
        cmds = [BaselineCommand("Pod info", "kubectl exec {pod_name} -n {namespace} -- df -h")]
        result = _resolve_templates(cmds, state)
        assert len(result) == 1
        assert result[0]["_unresolved"] is False
        assert "accounting-abc" in result[0]["v_args"]

    def test_node_scope_node_name_still_resolved(self):
        """When blade_scope=node, {node_name} should still resolve correctly."""
        state = {
            "blade_scope": "node",
            "target": {
                "namespace": "",
                "names": ["cn-hongkong.10.0.1.120"],
            },
        }
        cmds = [BaselineCommand("Node info", "kubectl describe node {node_name}")]
        result = _resolve_templates(cmds, state)
        assert len(result) == 1
        assert result[0]["_unresolved"] is False
        assert "cn-hongkong.10.0.1.120" in result[0]["v_args"]

    def test_no_scope_pod_name_resolved(self):
        """When blade_scope is not set, fall back to legacy behavior
        (pod_name = names[0]) for backwards compatibility."""
        state = {
            "target": {
                "namespace": "default",
                "names": ["my-pod"],
            },
        }
        cmds = [BaselineCommand("Pod info", "kubectl exec {pod_name} -n {namespace} -- df -h")]
        result = _resolve_templates(cmds, state)
        assert len(result) == 1
        assert result[0]["_unresolved"] is False
        assert "my-pod" in result[0]["v_args"]


# ---------------------------------------------------------------------------
# LLM JSON output parsing
# ---------------------------------------------------------------------------


class TestLLMJsonParsing:
    """Test _parse_llm_json_output robustness."""

    def test_pure_json(self):
        raw = '[{"description":"test","command":"kubectl get nodes","mode":"simple"}]'
        result = _parse_llm_json_output(raw)
        assert len(result) == 1
        assert result[0]["description"] == "test"

    def test_json_in_markdown_code_block(self):
        raw = '```json\n[{"description":"test","command":"kubectl top nodes","mode":"simple"}]\n```'
        result = _parse_llm_json_output(raw)
        assert len(result) == 1

    def test_empty_input(self):
        assert _parse_llm_json_output("") == []
        assert _parse_llm_json_output(None) == []

    def test_invalid_json(self):
        assert _parse_llm_json_output("not json at all") == []

    def test_trailing_text(self):
        raw = '[{"description":"test","command":"kubectl get nodes","mode":"simple"}] and some trailing text'
        result = _parse_llm_json_output(raw)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Command validation and filtering
# ---------------------------------------------------------------------------


class TestCommandValidation:
    """Test _validate_and_filter_commands whitelist enforcement (k8s + host)."""

    def test_allowed_commands(self):
        cmds = [
            {"description": "test", "command": "kubectl get nodes", "mode": "simple"},
            {"description": "test2", "command": "kubectl top nodes", "mode": "simple"},
        ]
        result = _validate_and_filter_commands(cmds, "k8s")
        assert len(result) == 2

    def test_rejected_subcommand(self):
        cmds = [
            {"description": "hack", "command": "kubectl delete pod x", "mode": "simple"},
        ]
        result = _validate_and_filter_commands(cmds, "k8s")
        assert len(result) == 0

    def test_exec_with_allowed_command(self):
        cmds = [
            {"description": "disk", "command": "kubectl exec pod-x -n ns -- df -h", "mode": "simple"},
        ]
        result = _validate_and_filter_commands(cmds, "k8s")
        assert len(result) == 1

    def test_exec_with_disallowed_command(self):
        cmds = [
            {"description": "hack", "command": "kubectl exec pod-x -n ns -- rm -rf /", "mode": "simple"},
        ]
        result = _validate_and_filter_commands(cmds, "k8s")
        assert len(result) == 0

    def test_non_dict_input_skipped(self):
        cmds = ["not a dict", 42]
        result = _validate_and_filter_commands(cmds, "k8s")
        assert len(result) == 0

    def test_host_allowed_commands(self):
        cmds = [
            {"description": "cpu", "command": "top -bn1", "mode": "simple"},
            {"description": "mem", "command": "free -m", "mode": "simple"},
        ]
        result = _validate_and_filter_commands(cmds, "host")
        assert len(result) == 2
        assert all(c.mode == "simple" for c in result)

    def test_host_rejects_kubectl(self):
        cmds = [{"description": "x", "command": "kubectl get pods", "mode": "simple"}]
        assert _validate_and_filter_commands(cmds, "host") == []

    def test_host_rejects_pipe(self):
        cmds = [{"description": "x", "command": "ps aux | grep java", "mode": "simple"}]
        assert _validate_and_filter_commands(cmds, "host") == []

    def test_host_mode_forced_simple(self):
        cmds = [{"description": "x", "command": "top -bn1", "mode": "debug_two_step"}]
        result = _validate_and_filter_commands(cmds, "host")
        assert len(result) == 1
        assert result[0].mode == "simple"


# ---------------------------------------------------------------------------
# Debug pod name parsing
# ---------------------------------------------------------------------------


class TestDebugPodParsing:
    """Test _parse_debug_pod_name from kubectl debug output."""

    def test_pod_created_format(self):
        output = "pod/cn-hongkong-debug-abcde created"
        assert _parse_debug_pod_name(output) == "cn-hongkong-debug-abcde"

    def test_starting_format(self):
        output = "Starting debugging pod cn-hongkong-debug-xyz12 created"
        assert _parse_debug_pod_name(output) == "cn-hongkong-debug-xyz12"

    def test_empty_output(self):
        assert _parse_debug_pod_name("") == ""

    def test_no_debug_pod(self):
        assert _parse_debug_pod_name("some random output") == ""


# ---------------------------------------------------------------------------
# Fallback chain: LLM -> Registry -> Scope
# ---------------------------------------------------------------------------


class TestFallbackChain:
    """Test strategy fallback in make_baseline_capture."""

    @pytest.mark.asyncio
    async def test_registry_fallback_when_no_llm(self):
        """When no LLM, should use Registry."""
        node = make_baseline_capture(llm=None, registry=None)
        state = {
            "task_id": "test-1",
            "blade_scope": "node",
            "blade_target": "disk",
            "blade_action": "fill",
            "target": {
                "namespace": "default",
                "names": ["test-node"],
                "labels": {},
            },
            "kubeconfig": "/path/to/kubeconfig",
        }
        with patch("chaos_agent.agent.nodes.baseline.baseline_capture._execute_observations",
                    new_callable=AsyncMock, return_value=[]):
            result = await node(state)
        assert result["baseline_data"]["source"] == "registry"

    @pytest.mark.asyncio
    async def test_scope_fallback_when_no_target_match(self):
        """When no (scope,target) match, falls to _SCOPE_FALLBACK."""
        node = make_baseline_capture(llm=None, registry=None)
        state = {
            "task_id": "test-2",
            "blade_scope": "node",
            "blade_target": "nonexistent",
            "blade_action": "nonexistent",
            "target": {
                "namespace": "default",
                "names": ["test-node"],
                "labels": {},
            },
            "kubeconfig": "/path/to/kubeconfig",
        }
        with patch("chaos_agent.agent.nodes.baseline.baseline_capture._execute_observations",
                    new_callable=AsyncMock, return_value=[]):
            result = await node(state)
        # ("node","nonexistent","nonexistent") → no exact, ("node","nonexistent") → no match
        # → _lookup_baseline_commands returns [] → _SCOPE_FALLBACK["node"] used
        assert result["baseline_data"]["source"] == "scope_fallback"

    @pytest.mark.asyncio
    async def test_no_match_at_all(self):
        """When scope is completely unknown, source is 'none'."""
        node = make_baseline_capture(llm=None, registry=None)
        state = {
            "task_id": "test-2b",
            "blade_scope": "container",
            "blade_target": "cpu",
            "blade_action": "fullload",
            "target": {
                "namespace": "default",
                "names": ["test-container"],
                "labels": {},
            },
            "kubeconfig": "/path/to/kubeconfig",
        }
        with patch("chaos_agent.agent.nodes.baseline.baseline_capture._execute_observations",
                    new_callable=AsyncMock, return_value=[]):
            result = await node(state)
        # No match in BASELINE_COMMANDS, _SCOPE_FALLBACK has no "container"
        assert result["baseline_data"]["source"] == "none"

    @pytest.mark.asyncio
    async def test_llm_derived_strategy(self):
        """When LLM returns valid commands, should use 'llm' source."""
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content=json.dumps([
            {"description": "Node disk", "command": "kubectl top node {node_name}", "mode": "simple"},
        ])))
        node = make_baseline_capture(llm=mock_llm, registry=None)
        state = {
            "task_id": "test-3",
            "blade_scope": "node",
            "blade_target": "disk",
            "blade_action": "fill",
            "skill_case_content": "some skill content",
            "target": {
                "namespace": "default",
                "names": ["test-node"],
                "labels": {},
            },
            "kubeconfig": "/path/to/kubeconfig",
        }
        with patch("chaos_agent.agent.nodes.baseline.baseline_capture._execute_observations",
                    new_callable=AsyncMock, return_value=[]), \
             patch("chaos_agent.agent.nodes.baseline.baseline_capture._lookup_baseline_commands",
                   return_value=[]):
            result = await node(state)
        assert result["baseline_data"]["source"] == "llm"


# ---------------------------------------------------------------------------
# Exception safety
# ---------------------------------------------------------------------------


class TestExceptionSafety:
    """Test that baseline_capture never blocks injection on error."""

    @pytest.mark.asyncio
    async def test_exception_returns_error_baseline(self):
        """Node should gracefully handle strategy exceptions and still return a result.

        With the Viability Gate strategy chain, individual strategy exceptions
        are caught internally (falling through to the next strategy) rather than
        bubbling up to the outer try/except. This means the result source will
        reflect which strategy ultimately won (or "none" if all failed), not
        "error". The outer try/except still catches truly unexpected errors
        (e.g., during execution, not strategy selection).
        """
        node = make_baseline_capture(llm=None, registry=None)
        state = {
            "task_id": "test-err",
            "blade_scope": "node",
            "blade_target": "disk",
            "blade_action": "fill",
        }
        # Force an exception in the registry strategy via mock.
        # The strategy chain should catch it and try scope_fallback next.
        # scope_fallback returns `kubectl top node {node_name}` but with no
        # node_name it's 0 viable, so source becomes "none" (not "error").
        with patch("chaos_agent.agent.nodes.baseline.baseline_capture._lookup_baseline_commands",
                    side_effect=RuntimeError("unexpected")):
            result = await node(state)
        # Source is "none" because all strategies either failed or produced
        # 0 viable commands — NOT "error" (which only happens on truly
        # unexpected exceptions outside the strategy chain).
        assert result["baseline_data"]["source"] == "none"
        assert result["baseline_data"]["success_count"] == 0


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


class TestObservability:
    """Test that baseline_capture emits tracker/store/session events."""

    @pytest.mark.asyncio
    async def test_tracker_and_store_called(self):
        """Verify tracker, sync_to_store, and session_store are called."""
        node = make_baseline_capture(llm=None, registry=None)
        state = {
            "task_id": "test-obs",
            "blade_scope": "node",
            "blade_target": "disk",
            "blade_action": "fill",
            "target": {
                "namespace": "default",
                "names": ["test-node"],
                "labels": {},
            },
            "kubeconfig": "/path/to/kubeconfig",
        }
        with patch("chaos_agent.agent.nodes.baseline.baseline_capture._execute_observations",
                    new_callable=AsyncMock, return_value=[
                        {"description": "test", "command": "kubectl top node test-node",
                         "exit_code": 0, "stdout": "OK", "stderr": ""},
                    ]), \
             patch("chaos_agent.agent.nodes.baseline.baseline_capture.sync_to_store",
                    new_callable=AsyncMock) as mock_sync, \
             patch("chaos_agent.agent.nodes.baseline.baseline_capture.sync_node_status_to_session") as mock_session, \
             patch("chaos_agent.agent.nodes.baseline.baseline_capture.get_tracker") as mock_tracker:
            mock_tracker_instance = MagicMock()
            mock_tracker.return_value = mock_tracker_instance
            result = await node(state)

        # Verify tracker was used
        mock_tracker_instance.start.assert_called_once()
        mock_tracker_instance.complete.assert_called_once()

        # Verify sync_to_store was called
        mock_sync.assert_called_once()

        # Verify session status was recorded
        mock_session.assert_called_once()

        # The mocked executor deliberately returns one observation regardless
        # of the resolved command count.
        assert result["baseline_data"]["success_count"] == 1
        assert result["baseline_data"]["source"] == "registry"


# ---------------------------------------------------------------------------
# Bug fix tests: mode auto-correction, debug smart conversion,
# namespace normalization, parse_debug_pod_name new format
# ---------------------------------------------------------------------------


class TestModeAutoCorrection:
    """Test that {debug_pod} in a k8s command forces mode=debug_two_step."""

    def test_debug_pod_forces_debug_two_step_mode(self):
        """LLM generates {debug_pod} with mode=simple -> auto-corrected."""
        cmds = [
            {"description": "Node disk IO",
             "command": "kubectl exec {debug_pod} -n chaosblade -- iostat -xd 1 3",
             "mode": "simple"},
        ]
        result = _validate_and_filter_commands(cmds, "k8s")
        assert len(result) == 1
        assert result[0].mode == "debug_two_step"

    def test_debug_pod_with_correct_mode_passes(self):
        """LLM generates {debug_pod} with mode=debug_two_step -> passes unchanged."""
        cmds = [
            {"description": "Node disk IO",
             "command": "kubectl exec {debug_pod} -n chaosblade -- iostat -xd 1 3",
             "mode": "debug_two_step"},
        ]
        result = _validate_and_filter_commands(cmds, "k8s")
        assert len(result) == 1
        assert result[0].mode == "debug_two_step"


class TestNamespaceNormalization:
    """Test _normalize_debug_namespace ensures chaosblade namespace."""

    def test_namespace_replaced_to_chaosblade(self):
        v_args = "{debug_pod} -n default -- iostat -xd 1 3"
        result = _normalize_debug_namespace(v_args)
        assert f"-n {_TOOL_POD_NAMESPACE}" in result
        assert "-n default" not in result

    def test_namespace_added_when_missing(self):
        v_args = "{debug_pod} -- iostat -xd 1 3"
        result = _normalize_debug_namespace(v_args)
        assert f"-n {_TOOL_POD_NAMESPACE}" in result

    def test_chaosblade_namespace_unchanged(self):
        v_args = f"{{debug_pod}} -n {_TOOL_POD_NAMESPACE} -- iostat -xd 1 3"
        result = _normalize_debug_namespace(v_args)
        assert f"-n {_TOOL_POD_NAMESPACE}" in result

    def test_custom_namespace_replaced(self):
        v_args = "{debug_pod} -n custom-ns -- df -h"
        result = _normalize_debug_namespace(v_args)
        assert f"-n {_TOOL_POD_NAMESPACE}" in result
        assert "-n custom-ns" not in result

    def test_long_namespace_flag_replaced(self):
        v_args = "{debug_pod} --namespace custom-ns -- df -h"
        result = _normalize_debug_namespace(v_args)
        assert f"-n {_TOOL_POD_NAMESPACE}" in result
        assert "--namespace" not in result


class TestResolveTemplatesNamespaceAndMode:
    """Test _resolve_templates deep defense: mode + namespace normalization."""

    def test_mode_auto_correction_in_resolve(self):
        """If {debug_pod} present but mode is simple, resolve corrects it."""
        state = {
            "blade_scope": "node",
            "target": {"namespace": "", "names": ["test-node"], "labels": {}},
        }
        cmds = [BaselineCommand("Node disk",
                                "kubectl exec {debug_pod} -n chaosblade -- df -h",
                                mode="simple")]
        result = _resolve_templates(cmds, state)
        assert len(result) == 1
        assert result[0]["mode"] == "debug_two_step"

    def test_namespace_normalized_for_debug_two_step(self):
        """debug_two_step commands get namespace normalized to chaosblade."""
        state = {
            "blade_scope": "node",
            "target": {"namespace": "", "names": ["test-node"], "labels": {}},
        }
        cmds = [BaselineCommand("Node disk",
                                "kubectl exec {debug_pod} -n some-ns -- iostat -xd 1 3",
                                mode="debug_two_step")]
        result = _resolve_templates(cmds, state)
        assert len(result) == 1
        assert f"-n {_TOOL_POD_NAMESPACE}" in result[0]["v_args"]
        assert "-n some-ns" not in result[0]["v_args"]


class TestDebugPodParsingNewFormat:
    """Test _parse_debug_pod_name with K8s 1.25+ output format."""

    def test_new_kubectl_debug_format(self):
        output = ("Creating debugging pod node-debugger-cn-hongkong.10.0.2.69-z24x7 "
                  "with container debugger on node cn-hongkong.10.0.2.69.")
        assert _parse_debug_pod_name(output) == "node-debugger-cn-hongkong.10.0.2.69-z24x7"

    def test_new_format_without_trailing_period(self):
        output = ("Creating debugging pod node-debugger-test-node-abc12 "
                  "with container debugger on node test-node")
        assert _parse_debug_pod_name(output) == "node-debugger-test-node-abc12"

    def test_old_kubectl_debug_format_still_works(self):
        output = "pod/node-name-debug-abc123 created"
        assert _parse_debug_pod_name(output) == "node-name-debug-abc123"

    def test_starting_format_still_works(self):
        output = "Starting debugging pod cn-hongkong-debug-xyz12 created"
        assert _parse_debug_pod_name(output) == "cn-hongkong-debug-xyz12"


class TestRegistryUsesChaosbladeNamespace:
    """Verify Registry and FCAT commands use chaosblade namespace, not default."""

    def test_node_disk_fill_uses_chaosblade_ns(self):
        cmds = BASELINE_COMMANDS[("node", "disk", "fill")]
        debug_cmds = [c for c in cmds if c.mode == "debug_two_step"]
        assert len(debug_cmds) == 1
        assert f"-n {_TOOL_POD_NAMESPACE}" in debug_cmds[0].command
        assert "-n default" not in debug_cmds[0].command

    def test_node_disk_burn_uses_chaosblade_ns(self):
        cmds = BASELINE_COMMANDS[("node", "disk", "burn")]
        debug_cmds = [c for c in cmds if c.mode == "debug_two_step"]
        assert len(debug_cmds) == 1
        assert f"-n {_TOOL_POD_NAMESPACE}" in debug_cmds[0].command
        assert "-n default" not in debug_cmds[0].command

    def test_node_disk_fallback_uses_chaosblade_ns(self):
        cmds = BASELINE_COMMANDS[("node", "disk")]
        debug_cmds = [c for c in cmds if c.mode == "debug_two_step"]
        assert len(debug_cmds) == 1
        assert f"-n {_TOOL_POD_NAMESPACE}" in debug_cmds[0].command


# ---------------------------------------------------------------------------
# LLM prompt structure tests (U-shaped architecture validation)
# ---------------------------------------------------------------------------


class TestLLMDeriveBaselinePrompt:
    """Validate build_baseline_system_prompt structure and
    _llm_derive_baseline_commands prompt composition.

    These tests verify structural/semantic constraints, not exact text —
    prompt wording may evolve, but the architecture guarantees must hold.
    """

    # -- SystemMessage content tests --

    def test_k8s_prompt_contains_core_and_kubectl_fragment(self):
        """k8s channel prompt = universal core + kubectl capability fragment."""
        prompt = build_baseline_system_prompt("kubeconfig")
        # Universal core mission (channel-agnostic).
        assert "Core Principle" in prompt
        assert "causation attribution" in prompt
        assert "SAME metric" in prompt
        # k8s capability fragment.
        assert "kubectl" in prompt
        assert "debug_two_step" in prompt

    def test_host_prompt_contains_core_and_host_fragment(self):
        """host channel prompt = same core + host-shell fragment, no k8s fragment."""
        prompt = build_baseline_system_prompt("ssh")
        assert "Core Principle" in prompt
        assert "causation attribution" in prompt
        # host capability fragment present; k8s fragment absent.
        assert "Capability: Host shell diagnostics" in prompt
        assert "Capability: Kubernetes" not in prompt

    def test_kubewiz_channels_map_to_expected_profiles(self):
        """kubewiz_k8s -> k8s fragment; kubewiz_host -> host fragment."""
        assert "Capability: Kubernetes" in build_baseline_system_prompt("kubewiz_k8s")
        assert (
            "Capability: Host shell diagnostics"
            in build_baseline_system_prompt("kubewiz_host")
        )

    # -- LLM invocation pattern tests --

    @pytest.mark.asyncio
    async def test_llm_invoke_uses_system_and_human_messages(self):
        """_llm_derive_baseline_commands must invoke LLM with
        [SystemMessage, HumanMessage], the SystemMessage being the
        channel-assembled prompt."""
        from langchain_core.messages import SystemMessage, HumanMessage

        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps([
            {"description": "Pod CPU",
             "command": "kubectl top pod -n prod -l app=x", "mode": "simple"},
        ])
        mock_llm.ainvoke.return_value = mock_response

        from chaos_agent.agent.nodes.baseline.baseline_capture import _llm_derive_baseline_commands
        await _llm_derive_baseline_commands(
            mock_llm, "test skill content", "pod", "cpu", "fullload",
        )

        # Verify invocation pattern
        call_args = mock_llm.ainvoke.call_args
        messages = call_args[0][0]
        assert len(messages) == 2
        assert isinstance(messages[0], SystemMessage)
        assert isinstance(messages[1], HumanMessage)
        # SystemMessage uses the channel-assembled prompt (default kubeconfig).
        assert messages[0].content == build_baseline_system_prompt("kubeconfig")

    @pytest.mark.asyncio
    async def test_human_prompt_focus_guidance(self):
        """HumanMessage should use fault-impact reasoning guidance."""
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "[]"
        mock_llm.ainvoke.return_value = mock_response

        from chaos_agent.agent.nodes.baseline.baseline_capture import _llm_derive_baseline_commands
        await _llm_derive_baseline_commands(
            mock_llm, "test skill content", "pod", "cpu", "fullload",
        )

        call_args = mock_llm.ainvoke.call_args
        messages = call_args[0][0]
        human_content = messages[1].content
        # Should guide fault-impact reasoning.
        assert "reason about what states" in human_content
        # Should contain fault type info.
        assert "Fault type: pod-cpu-fullload" in human_content
        # Should contain skill-case tag.
        assert "<skill-case>" in human_content


class TestExtractorFramework:
    """Extractor integration: BaselineCommand.extractors must run after
    each command completes, the resulting fields must merge into
    target_metadata, and extractor failures must NOT break baseline
    capture. Locked here as a regression guard — if the runner stops
    invoking extractors, the FCAT P0 path silently goes back to issuing
    a duplicate ``kubectl top pod``."""

    def test_baseline_command_extractors_default_empty(self):
        # Backward-compat: existing call sites that don't pass
        # ``extractors=`` must still produce a valid BaselineCommand
        # with no extractors attached.
        cmd = BaselineCommand("desc", "kubectl top node {node_name}")
        assert cmd.extractors == []

    def test_baseline_command_extractors_round_trip(self):
        def _noop(_stdout, _state):
            return {}
        cmd = BaselineCommand(
            "desc", "kubectl top node {node_name}", extractors=[_noop],
        )
        assert cmd.extractors == [_noop]

    def test_resolve_templates_preserves_extractors(self):
        # Regression: if _resolve_templates drops the extractors
        # field, the runner can't reach them after execution.
        def _extr(_s, _st):
            return {"k": "v"}
        state = {
            "target": {"namespace": "ns", "names": ["p"], "labels": {}},
        }
        cmds = [
            BaselineCommand(
                "Pod top", "kubectl top pod {pod_name} -n {namespace}",
                extractors=[_extr],
            ),
        ]
        resolved = _resolve_templates(cmds, state)
        assert resolved[0]["_extractors"] == [_extr]

    def test_pod_cpu_and_mem_commands_carry_extractor(self):
        # Lock down that the production registry has the extractor
        # wired up. If someone deletes it, the next ``pod cpu`` /
        # ``pod mem`` drill silently goes back to two ``kubectl top``
        # roundtrips (one in baseline, one in direct_execute).
        from chaos_agent.agent.baseline_extractors import extract_pod_top_metrics

        for key in (("pod", "cpu"), ("pod", "mem")):
            cmds = BASELINE_COMMANDS[key]
            top_cmd = next(c for c in cmds if c.command.startswith("kubectl top"))
            assert extract_pod_top_metrics in top_cmd.extractors

    @pytest.mark.asyncio
    async def test_extractors_run_and_merge_into_target_metadata(self):
        # End-to-end: build a baseline_capture node, mock the kubectl
        # execution to return a known ``top pod`` table, verify the
        # extractor parses it and the parsed fields land in the
        # returned state update's ``target_metadata``.
        fake_top_output = (
            "NAME                              CPU(cores)   MEMORY(bytes)\n"
            "target-pod-xyz                    50m          120Mi\n"
        )

        async def fake_exec(commands, kubeconfig, task_id):
            # Return one observation per command. The first matches
            # the ``top`` baseline command and carries the table we
            # want the extractor to parse.
            results = []
            for cmd in commands:
                if cmd.get("subcommand") == "top":
                    results.append({
                        "description": cmd["description"],
                        "command": "kubectl top pod ...",
                        "exit_code": 0,
                        "stdout": fake_top_output,
                        "stderr": "",
                    })
                else:
                    results.append({
                        "description": cmd["description"],
                        "command": "kubectl describe pod ...",
                        "exit_code": 0,
                        "stdout": "",
                        "stderr": "",
                    })
            return results

        node = make_baseline_capture(llm=None, registry=None)
        state = {
            "blade_scope": "pod",
            "blade_target": "mem",
            "blade_action": "burn",
            "kubeconfig": "/path/to/kube",
            "target": {
                "namespace": "ns",
                "names": ["target-pod-xyz"],
                "labels": {"app": "demo"},
            },
            # direct_setup ran first → existing metadata must be
            # PRESERVED across the extractor merge.
            "target_metadata": {"pod_memory_limit_mb": 240},
            "task_id": "t-extractor",
            "skill_case_content": "",
        }
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture._execute_observations",
            new=fake_exec,
        ):
            result = await node(state)

        md = result.get("target_metadata") or {}
        # Pre-existing field preserved (merge, not replace)
        assert md.get("pod_memory_limit_mb") == 240
        # Newly extracted fields present
        assert md.get("pod_memory_usage_mb") == 120
        assert md.get("pod_cpu_usage_mc") == 50

    @pytest.mark.asyncio
    async def test_extractor_exception_does_not_break_baseline(self):
        # An extractor raising must be logged debug and skipped;
        # baseline must still complete and return observations.
        def _boom(_stdout, _state):
            raise RuntimeError("parser broke")

        # Inject a custom command list via a stub strategy. Easier
        # than patching BASELINE_COMMANDS in place because we need
        # the runtime to use OUR command (with the booming extractor).
        async def fake_exec(commands, kubeconfig, task_id):
            return [
                {
                    "description": commands[0]["description"],
                    "command": "x",
                    "exit_code": 0,
                    "stdout": "anything",
                    "stderr": "",
                }
            ]

        # Replace _lookup_baseline_commands so the registry path
        # returns our crafted command with the booming extractor.
        crafted = [
            BaselineCommand(
                "boom test", "kubectl top pod {pod_name} -n {namespace}",
                extractors=[_boom],
            ),
        ]
        node = make_baseline_capture(llm=None, registry=None)
        state = {
            "blade_scope": "pod",
            "blade_target": "mem",
            "blade_action": "burn",
            "kubeconfig": "/k",
            "target": {"namespace": "ns", "names": ["p"], "labels": {}},
            "task_id": "t-boom",
            "skill_case_content": "",
        }
        with (
            patch(
                "chaos_agent.agent.nodes.baseline.baseline_capture._lookup_baseline_commands",
                return_value=crafted,
            ),
            patch(
                "chaos_agent.agent.nodes.baseline.baseline_capture._execute_observations",
                new=fake_exec,
            ),
        ):
            result = await node(state)

        # baseline_data still produced, no exception bubbled up
        assert "baseline_data" in result
        assert result["baseline_data"]["success_count"] == 1
        # No target_metadata field updates from the failed extractor
        assert "target_metadata" not in result or "pod_memory_usage_mb" not in (
            result.get("target_metadata") or {}
        )

    @pytest.mark.asyncio
    async def test_extractor_skipped_when_command_failed(self):
        # Regression: extractor must NOT run on commands with
        # exit_code != 0. Their stdout is empty/garbage and parsing
        # it would either produce nonsense (silent corruption) or
        # raise (logged debug but still wasteful).
        extractor_called = {"n": 0}

        def _spy(stdout, state):
            extractor_called["n"] += 1
            return {"spy_called": True}

        async def fake_exec(commands, kubeconfig, task_id):
            return [
                {
                    "description": commands[0]["description"],
                    "command": "x",
                    "exit_code": 1,  # FAILURE — extractor must not see this
                    "stdout": "",
                    "stderr": "kubectl error",
                }
            ]

        crafted = [
            BaselineCommand(
                "spy cmd", "kubectl top pod {pod_name} -n {namespace}",
                extractors=[_spy],
            ),
        ]
        node = make_baseline_capture(llm=None, registry=None)
        state = {
            "blade_scope": "pod", "blade_target": "mem", "blade_action": "burn",
            "kubeconfig": "/k",
            "target": {"namespace": "ns", "names": ["p"], "labels": {}},
            "task_id": "t-skip",
            "skill_case_content": "",
        }
        with (
            patch(
                "chaos_agent.agent.nodes.baseline.baseline_capture._lookup_baseline_commands",
                return_value=crafted,
            ),
            patch(
                "chaos_agent.agent.nodes.baseline.baseline_capture._execute_observations",
                new=fake_exec,
            ),
        ):
            await node(state)
        assert extractor_called["n"] == 0

    @pytest.mark.asyncio
    async def test_extractor_returning_non_dict_does_not_crash(self):
        # Contract says extractors return dict. A buggy author
        # returning a list / None / int must NOT take down the
        # whole baseline pipeline.
        def _bad_contract(stdout, state):
            return ["not", "a", "dict"]

        async def fake_exec(commands, kubeconfig, task_id):
            return [
                {
                    "description": commands[0]["description"],
                    "command": "x",
                    "exit_code": 0,
                    "stdout": "ok",
                    "stderr": "",
                }
            ]

        crafted = [
            BaselineCommand(
                "bad contract cmd", "kubectl top pod {pod_name} -n {namespace}",
                extractors=[_bad_contract],
            ),
        ]
        node = make_baseline_capture(llm=None, registry=None)
        state = {
            "blade_scope": "pod", "blade_target": "mem", "blade_action": "burn",
            "kubeconfig": "/k",
            "target": {"namespace": "ns", "names": ["p"], "labels": {}},
            "task_id": "t-bad-contract",
            "skill_case_content": "",
        }
        with (
            patch(
                "chaos_agent.agent.nodes.baseline.baseline_capture._lookup_baseline_commands",
                return_value=crafted,
            ),
            patch(
                "chaos_agent.agent.nodes.baseline.baseline_capture._execute_observations",
                new=fake_exec,
            ),
        ):
            result = await node(state)
        # Must complete without crash, with no contamination of
        # target_metadata from the non-dict return.
        assert "baseline_data" in result
        md = result.get("target_metadata") or {}
        assert "not" not in md and 0 not in md


# ---------------------------------------------------------------------------
# Fix: pod-process-kill registry entry
# ---------------------------------------------------------------------------


class TestPodProcessKillRegistryEntry:
    """Verify (pod, process, kill) exact match returns endpoints + pod status."""

    def test_exact_match_exists(self):
        result = _lookup_baseline_commands("k8s", "pod", "process", "kill")
        assert len(result) == 3
        descriptions = [c.description for c in result]
        assert "Service endpoints" in descriptions
        assert "Pod status/restarts" in descriptions
        assert "Pod events" in descriptions

    def test_endpoints_uses_label_selector(self):
        result = _lookup_baseline_commands("k8s", "pod", "process", "kill")
        ep_cmd = next(c for c in result if c.description == "Service endpoints")
        assert "{label_selector}" in ep_cmd.command

    def test_pod_status_uses_wide_output(self):
        result = _lookup_baseline_commands("k8s", "pod", "process", "kill")
        status_cmd = next(c for c in result if c.description == "Pod status/restarts")
        assert "-o wide" in status_cmd.command

    def test_target_fallback_still_works_for_other_actions(self):
        """(pod, process, <other>) should still fall back to the (pod, process) entry."""
        result = _lookup_baseline_commands("k8s", "pod", "process", "stop")
        assert len(result) == 2
        descriptions = [c.description for c in result]
        assert "Pod status" in descriptions
        assert "Pod events" in descriptions


# ---------------------------------------------------------------------------
# Fix: LLM retry on execution failure
# ---------------------------------------------------------------------------


class TestLLMRetryFailedCommands:
    """Verify _llm_retry_failed_commands feeds errors back to LLM."""

    @pytest.mark.asyncio
    async def test_retry_sends_error_feedback(self):
        """LLM retry prompt must contain the failed command and its error."""
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content=json.dumps([
            {"description": "Fixed endpoints",
             "command": "kubectl get endpoints -n {namespace} {label_selector}",
             "mode": "simple"},
        ])))

        failed_obs = [{
            "command": "kubectl get endpoints -n cms-demo -l -l opentelemetry.io/name=rec",
            "exit_code": 1,
            "stderr": "error: there is no need to specify a resource type",
        }]

        result = await _llm_retry_failed_commands(
            mock_llm, "skill content", "pod", "process", "kill", failed_obs,
        )
        assert len(result) == 1
        assert result[0].description == "Fixed endpoints"

        # Verify error feedback was included in the prompt
        call_args = mock_llm.ainvoke.call_args
        messages = call_args[0][0]
        human_content = messages[1].content
        assert "FAILED" in human_content
        assert "-l -l" in human_content
        assert "error: there is no need" in human_content

    @pytest.mark.asyncio
    async def test_retry_returns_empty_on_llm_failure(self):
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("API error"))
        result = await _llm_retry_failed_commands(
            mock_llm, "skill", "pod", "process", "kill",
            [{"command": "bad", "exit_code": 1, "stderr": "err"}],
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_retry_returns_empty_when_no_llm(self):
        result = await _llm_retry_failed_commands(
            None, "skill", "pod", "process", "kill",
            [{"command": "bad", "exit_code": 1, "stderr": "err"}],
        )
        assert result == []

    def test_max_retries_constant(self):
        assert _LLM_BASELINE_MAX_RETRIES == 3


class TestBaselineCaptureRetryIntegration:
    """End-to-end: LLM commands fail → retry with error feedback → succeed."""

    @pytest.mark.asyncio
    async def test_retry_replaces_failed_with_corrected(self):
        """When LLM commands fail execution, retry produces working commands."""
        call_count = {"n": 0}

        mock_llm = AsyncMock()

        def make_response(content):
            r = MagicMock()
            r.content = content
            return r

        # First call: initial derivation (returns command with -l -l bug)
        # Second call: retry derivation (returns corrected command)
        def ainvoke_side_effect(messages):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return make_response(json.dumps([
                    {"description": "Endpoints",
                     "command": "kubectl get endpoints -n {namespace} {label_selector}",
                     "mode": "simple"},
                ]))
            else:
                return make_response(json.dumps([
                    {"description": "Fixed endpoints",
                     "command": "kubectl get endpoints -n {namespace} {label_selector}",
                     "mode": "simple"},
                ]))

        mock_llm.ainvoke = AsyncMock(side_effect=ainvoke_side_effect)

        exec_call_count = {"n": 0}

        async def fake_exec(commands, kubeconfig, task_id):
            exec_call_count["n"] += 1
            results = []
            for cmd in commands:
                if exec_call_count["n"] == 1:
                    # First execution: fail
                    results.append({
                        "description": cmd["description"],
                        "command": "kubectl get endpoints -n cms-demo -l -l ...",
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": "error: there is no need to specify a resource type",
                    })
                else:
                    # Retry execution: succeed
                    results.append({
                        "description": cmd["description"],
                        "command": "kubectl get endpoints -n cms-demo -l ...",
                        "exit_code": 0,
                        "stdout": "NAME       ENDPOINTS\nrec-svc    10.0.1.1:8080",
                        "stderr": "",
                    })
            return results

        node = make_baseline_capture(llm=mock_llm, registry=None)
        state = {
            "task_id": "test-retry",
            "blade_scope": "pod",
            "blade_target": "process",
            "blade_action": "kill",
            "skill_case_content": "some skill case content",
            "target": {
                "namespace": "cms-demo",
                "names": ["rec-pod"],
                "labels": {"opentelemetry.io/name": "recommendation"},
            },
            "kubeconfig": "/path/to/kubeconfig",
        }
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture._execute_observations",
            new=fake_exec,
        ), patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture._lookup_baseline_commands",
            return_value=[],
        ):
            result = await node(state)

        assert result["baseline_data"]["source"] == "llm"
        assert result["baseline_data"]["success_count"] == 1
        # LLM was called twice: initial + retry
        assert call_count["n"] == 2
        # Execution was called twice: initial + retry
        assert exec_call_count["n"] == 2

    @pytest.mark.asyncio
    async def test_no_retry_when_all_succeed(self):
        """When all LLM commands succeed, no retry is attempted."""
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content=json.dumps([
            {"description": "Pod status",
             "command": "kubectl get pods -n {namespace} {label_selector}",
             "mode": "simple"},
        ])))

        async def fake_exec(commands, kubeconfig, task_id):
            return [{
                "description": commands[0]["description"],
                "command": "kubectl get pods ...",
                "exit_code": 0,
                "stdout": "NAME   READY   STATUS\nrec-pod   1/1   Running",
                "stderr": "",
            }]

        node = make_baseline_capture(llm=mock_llm, registry=None)
        state = {
            "task_id": "test-no-retry",
            "blade_scope": "pod",
            "blade_target": "process",
            "blade_action": "kill",
            "skill_case_content": "skill case",
            "target": {
                "namespace": "cms-demo",
                "names": ["rec-pod"],
                "labels": {"app": "rec"},
            },
            "kubeconfig": "/path/to/kubeconfig",
        }
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture._execute_observations",
            new=fake_exec,
        ), patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture._lookup_baseline_commands",
            return_value=[],
        ):
            result = await node(state)

        assert result["baseline_data"]["success_count"] == 1
        # LLM called only once (no retry)
        assert mock_llm.ainvoke.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_preserves_original_successes(self):
        """Original successful commands are kept across retries."""
        call_count = {"n": 0}

        mock_llm = AsyncMock()

        def make_response(content):
            r = MagicMock()
            r.content = content
            return r

        def ainvoke_side_effect(messages):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Initial: 2 commands
                return make_response(json.dumps([
                    {"description": "Pod status",
                     "command": "kubectl get pods -n {namespace} {label_selector}",
                     "mode": "simple"},
                    {"description": "Endpoints",
                     "command": "kubectl get endpoints -n {namespace} {label_selector}",
                     "mode": "simple"},
                ]))
            else:
                # Retry: corrected command for the failed one
                return make_response(json.dumps([
                    {"description": "Fixed endpoints",
                     "command": "kubectl get endpoints -n {namespace}",
                     "mode": "simple"},
                ]))

        mock_llm.ainvoke = AsyncMock(side_effect=ainvoke_side_effect)

        exec_call_count = {"n": 0}

        async def fake_exec(commands, kubeconfig, task_id):
            exec_call_count["n"] += 1
            results = []
            for cmd in commands:
                if exec_call_count["n"] == 1:
                    # First execution: first succeeds, second fails
                    if "pods" in cmd.get("v_args", ""):
                        results.append({
                            "description": cmd["description"],
                            "command": "kubectl get pods ...",
                            "exit_code": 0,
                            "stdout": "OK",
                            "stderr": "",
                        })
                    else:
                        results.append({
                            "description": cmd["description"],
                            "command": "kubectl get endpoints ...",
                            "exit_code": 1,
                            "stdout": "",
                            "stderr": "error",
                        })
                else:
                    # Retry: succeed
                    results.append({
                        "description": cmd["description"],
                        "command": "kubectl get endpoints -n cms-demo",
                        "exit_code": 0,
                        "stdout": "ENDPOINTS OK",
                        "stderr": "",
                    })
            return results

        node = make_baseline_capture(llm=mock_llm, registry=None)
        state = {
            "task_id": "test-partial",
            "blade_scope": "pod",
            "blade_target": "process",
            "blade_action": "kill",
            "skill_case_content": "skill case",
            "target": {
                "namespace": "cms-demo",
                "names": ["rec-pod"],
                "labels": {"app": "rec"},
            },
            "kubeconfig": "/path/to/kubeconfig",
        }
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture._execute_observations",
            new=fake_exec,
        ), patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture._lookup_baseline_commands",
            return_value=[],
        ):
            result = await node(state)

        # Original success (1) + retry success (1) = 2.
        assert result["baseline_data"]["success_count"] == 2

    @pytest.mark.asyncio
    async def test_registry_falls_back_to_scope_when_all_fail(self):
        """Execution-level fallback：registry 全部失败应回落到 scope_fallback。

        旧行为是 "registry 不 retry，原地 0/N 收摊"；新行为是 "当前 strategy
        执行 0/N succeeded 时回落到 strategy_chain 中下一个 viable strategy"
        （source != 'llm'，LLM 走自己的 4.1 retry 路径）。
        """
        exec_call_count = {"n": 0}

        async def fake_exec(commands, kubeconfig, task_id):
            exec_call_count["n"] += 1
            return [{
                "description": commands[0]["description"],
                "command": "kubectl ...",
                "exit_code": 1,
                "stdout": "",
                "stderr": "error",
            }]

        node = make_baseline_capture(llm=None, registry=None)
        state = {
            "task_id": "test-no-retry-registry",
            "blade_scope": "pod",
            "blade_target": "process",
            "blade_action": "kill",
            "target": {
                "namespace": "cms-demo",
                "names": ["rec-pod"],
                "labels": {"app": "rec"},
            },
            "kubeconfig": "/path/to/kubeconfig",
        }
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture._execute_observations",
            new=fake_exec,
        ):
            result = await node(state)

        # registry 跑挂 → 回落 scope_fallback（仍跑挂，但 source 应已切换）
        assert result["baseline_data"]["source"] == "scope_fallback"
        # 两次执行：1 次 registry + 1 次 scope_fallback（LLM 不可用，链路到此为止）
        assert exec_call_count["n"] == 2


# ---------------------------------------------------------------------------
# Host profile: same strategy chain, host-shell execution (no debug pod)
# ---------------------------------------------------------------------------


def _fake_result(exit_code=0, stdout="", stderr=""):
    r = MagicMock()
    r.exit_code = exit_code
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestHostProfileBaseline:
    """Host baseline (ssh / kubewiz_host) runs the SAME LLM→registry→fallback
    chain as k8s, but executes plain shell diagnostics via the transport with
    ``skip_guard=True`` and NEVER creates a debug pod / discovers a tool pod.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", ["ssh", "kubewiz_host"])
    async def test_host_registry_runs_shell_diagnostic(self, channel):
        calls = []

        async def fake_transport(cmd, target, **kwargs):
            calls.append((cmd, kwargs))
            return _fake_result(0, "load average: 0.1", "")

        node = make_baseline_capture(llm=None, registry=None)
        state = {
            "task_id": "host-1",
            "blade_scope": "node",
            "blade_target": "cpu",
            "blade_action": "fullload",
            "target": {"namespace": "", "names": ["10.0.0.9"], "labels": {}},
        }
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture.resolve_channel_name",
            return_value=channel,
        ), patch(
            "chaos_agent.agent.nodes.baseline._executors.execute_via_transport",
            new=fake_transport,
        ), patch(
            "chaos_agent.agent.nodes.baseline._executors._create_and_wait_debug_pod",
            new_callable=AsyncMock,
        ) as mock_debug, patch(
            "chaos_agent.agent.nodes.baseline._executors.discover_tool_pod_on_node",
            new_callable=AsyncMock,
        ) as mock_tool:
            result = await node(state)

        assert result["baseline_data"]["source"] == "registry"
        # CPU primary evidence plus hostname and an independent uptime check.
        assert result["baseline_data"]["success_count"] == 3
        # A host shell diagnostic was executed (top -bn1), not kubectl.
        assert calls, "execute_via_transport was not called"
        argv, kwargs = calls[0]
        assert argv == ["top", "-bn1"]
        assert kwargs.get("skip_guard") is True
        assert kwargs.get("source") == "baseline-host"
        # Host path never touches the k8s debug-pod machinery.
        mock_debug.assert_not_called()
        mock_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_host_iostat_degrades_to_proc(self):
        """When a host binary is missing, degrade to a /proc read."""
        seq = []

        async def fake_transport(cmd, target, **kwargs):
            seq.append(cmd)
            if cmd == ["iostat", "-xd", "1", "2"]:
                return _fake_result(127, "", "iostat: command not found")
            return _fake_result(0, "ok", "")

        node = make_baseline_capture(llm=None, registry=None)
        state = {
            "task_id": "host-io",
            "blade_scope": "node",
            "blade_target": "disk",
            "blade_action": "burn",
            "target": {"namespace": "", "names": ["h"], "labels": {}},
        }
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture.resolve_channel_name",
            return_value="ssh",
        ), patch(
            "chaos_agent.agent.nodes.baseline._executors.execute_via_transport",
            new=fake_transport,
        ):
            result = await node(state)

        # disk registry = [df -h, iostat -xd 1 2]; iostat degrades to /proc.
        assert ["df", "-h"] in seq
        assert ["iostat", "-xd", "1", "2"] in seq
        assert ["cat", "/proc/diskstats"] in seq
        # Both dimensions plus deterministic host identity succeed.
        assert result["baseline_data"]["success_count"] == 3

    @pytest.mark.asyncio
    async def test_host_llm_strategy_with_validation(self):
        """Host LLM strategy: only whitelisted host diagnostics survive
        validation; a kubectl command from the LLM is rejected."""
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content=json.dumps([
            {"description": "Host CPU", "command": "top -bn1", "mode": "simple"},
            {"description": "Bad", "command": "kubectl get pods", "mode": "simple"},
        ])))

        async def fake_transport(cmd, target, **kwargs):
            return _fake_result(0, "ok", "")

        node = make_baseline_capture(llm=mock_llm, registry=None)
        state = {
            "task_id": "host-llm",
            "blade_scope": "node",
            "blade_target": "cpu",
            "blade_action": "fullload",
            "skill_case_content": "some skill content",
            "target": {"namespace": "", "names": ["h"], "labels": {}},
        }
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture.resolve_channel_name",
            return_value="ssh",
        ), patch(
            "chaos_agent.agent.nodes.baseline._executors.execute_via_transport",
            new=fake_transport,
        ):
            result = await node(state)

        assert result["baseline_data"]["source"] == "llm"
        # The whitelisted diagnostic ran; evidence supplementation adds only
        # host identity and an independent load cross-check.
        assert result["baseline_data"]["success_count"] == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "blade_target,expected_argv",
        [
            ("mem", ["free", "-m"]),
            ("network", ["ss", "-s"]),
            ("process", ["ps", "aux"]),
        ],
    )
    async def test_host_registry_table_wiring(self, blade_target, expected_argv):
        """Each host blade_target maps to its shell diagnostic table entry and
        is executed verbatim via the transport (no kubectl, no debug pod)."""
        calls = []

        async def fake_transport(cmd, target, **kwargs):
            calls.append(cmd)
            return _fake_result(0, "ok", "")

        node = make_baseline_capture(llm=None, registry=None)
        state = {
            "task_id": f"host-{blade_target}",
            "blade_scope": "node",
            "blade_target": blade_target,
            "blade_action": "fullload",
            "target": {"namespace": "", "names": ["h"], "labels": {}},
        }
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture.resolve_channel_name",
            return_value="ssh",
        ), patch(
            "chaos_agent.agent.nodes.baseline._executors.execute_via_transport",
            new=fake_transport,
        ):
            result = await node(state)

        assert result["baseline_data"]["source"] == "registry"
        assert expected_argv in calls
        # Nothing kubectl-flavored leaked into the host path.
        assert all(argv and argv[0] != "kubectl" for argv in calls)

    @pytest.mark.asyncio
    async def test_host_scope_fallback_when_registry_empty(self):
        """An unknown host target has no registry entry, so the chain falls
        through to the host scope fallback (_HOST_FALLBACK: uptime, top)."""
        calls = []

        async def fake_transport(cmd, target, **kwargs):
            calls.append(cmd)
            return _fake_result(0, "ok", "")

        node = make_baseline_capture(llm=None, registry=None)
        state = {
            "task_id": "host-fb",
            "blade_scope": "node",
            "blade_target": "unknown-target",  # not in _HOST_BASELINE_COMMANDS
            "blade_action": "fullload",
            "target": {"namespace": "", "names": ["h"], "labels": {}},
        }
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture.resolve_channel_name",
            return_value="ssh",
        ), patch(
            "chaos_agent.agent.nodes.baseline._executors.execute_via_transport",
            new=fake_transport,
        ):
            result = await node(state)

        assert result["baseline_data"]["source"] == "scope_fallback"
        # _HOST_FALLBACK runs uptime + top -bn1.
        assert ["uptime"] in calls
        assert ["top", "-bn1"] in calls
        assert result["baseline_data"]["success_count"] == 3

    @pytest.mark.asyncio
    async def test_host_all_commands_fail_best_effort(self):
        """When every host diagnostic (and its /proc degrade) fails, baseline
        still completes with success_count=0 rather than raising."""

        async def fake_transport(cmd, target, **kwargs):
            return _fake_result(1, "", "boom")

        node = make_baseline_capture(llm=None, registry=None)
        state = {
            "task_id": "host-fail",
            "blade_scope": "node",
            "blade_target": "process",  # ps aux + uptime, no /proc fallback
            "blade_action": "kill",
            "target": {"namespace": "", "names": ["h"], "labels": {}},
        }
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture.resolve_channel_name",
            return_value="ssh",
        ), patch(
            "chaos_agent.agent.nodes.baseline._executors.execute_via_transport",
            new=fake_transport,
        ):
            result = await node(state)

        assert "baseline_data" in result
        assert result["baseline_data"]["success_count"] == 0


class TestK8sProfileBaseline:
    """k8s baseline still assembles the kubectl prompt, injects global kubectl
    parameters, and builds a debug pod for {debug_pod} + debug_two_step."""

    @pytest.mark.asyncio
    async def test_debug_two_step_creates_debug_pod(self):
        async def fake_transport(cmd, target, **kwargs):
            return _fake_result(0, "Filesystem  Size", "")

        node = make_baseline_capture(llm=None, registry=None)
        state = {
            "task_id": "k8s-dbg",
            "blade_scope": "node",
            "blade_target": "disk",
            "blade_action": "fill",
            "target": {"namespace": "", "names": ["cn-node-1"], "labels": {}},
            "kubeconfig": "/kc",
        }
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture.resolve_channel_name",
            return_value="kubeconfig",
        ), patch(
            "chaos_agent.agent.nodes.baseline._executors.execute_via_transport",
            new=fake_transport,
        ), patch(
            "chaos_agent.agent.nodes.baseline._executors._create_and_wait_debug_pod",
            new_callable=AsyncMock,
            return_value=("dbg-pod", _TOOL_POD_NAMESPACE),
        ) as mock_create, patch(
            "chaos_agent.agent.nodes.baseline._executors._delete_debug_pod",
            new_callable=AsyncMock,
        ) as mock_delete:
            result = await node(state)

        # node/disk/fill has a debug_two_step command → pod created + cleaned.
        mock_create.assert_awaited_once()
        mock_delete.assert_awaited_once()
        assert result["baseline_data"]["source"] == "registry"

# ---------------------------------------------------------------------------
# Retry memory
# ---------------------------------------------------------------------------


class TestRetryRemembersWhatItTried:
    """Each retry is a fresh LLM call with no memory of the previous one.

    Without the tried-command list the prompt is byte-identical every round, so
    the model can only resample. task-fc64c982: the node had no debug pod, and
    two of the three retries both emitted ``kubectl exec {debug_pod} -- pidof
    containerd`` — 71s spent before the third happened to change approach.
    """

    FAILED = [{
        "description": "containerd process status",
        "command": "kubectl exec {debug_pod} -n chaosblade -- pgrep containerd",
        "exit_code": -1,
        "stdout": "",
        "stderr": "No debug pod or tool pod available for node node-1",
    }]

    def _llm(self, captured: list):
        class _Resp:
            content = ('[{"description": "containerd process status", '
                       '"command": "kubectl describe node node-1", "mode": "simple"}]')
            additional_kwargs: dict = {}

        class _LLM:
            async def ainvoke(_self, messages):
                captured.append(messages[-1].content)
                return _Resp()

        return _LLM()

    async def _retry(self, captured, already_tried):
        return await _llm_retry_failed_commands(
            self._llm(captured), "skill case body", "node", "process", "stop",
            self.FAILED, names=("node-1",), already_tried=already_tried,
        )

    async def test_history_changes_the_prompt(self):
        captured: list[str] = []
        await self._retry(captured, ())
        await self._retry(captured, ("kubectl exec {debug_pod} -- pidof containerd",))
        assert captured[0] != captured[1]

    async def test_the_first_retry_has_no_history_section(self):
        captured: list[str] = []
        await self._retry(captured, ())
        assert "Already attempted" not in captured[0]

    async def test_tried_commands_are_listed_verbatim(self):
        """The LLM's own text, template placeholder intact — that is what it
        must recognise as "mine"."""
        captured: list[str] = []
        tried = (
            "kubectl exec {debug_pod} -n chaosblade -- pidof containerd",
            "kubectl exec {debug_pod} -n chaosblade -- ps aux",
        )
        await self._retry(captured, tried)
        for cmd in tried:
            assert cmd in captured[0]

    async def test_the_instruction_rules_out_equivalent_variants(self):
        """Re-emitting ``pgrep`` after ``pidof`` failed is the actual failure
        mode, so forbidding the exact string is not enough."""
        captured: list[str] = []
        await self._retry(captured, ("kubectl exec {debug_pod} -- pidof containerd",))
        assert "variant that would fail the same way" in captured[0]
