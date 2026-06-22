"""Tests for baseline_capture node."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chaos_agent.agent.nodes.baseline_capture import (
    BaselineCommand,
    BASELINE_COMMANDS,
    _BASELINE_SYSTEM_PROMPT,
    _LLM_BASELINE_MAX_RETRIES,
    _TOOL_POD_NAMESPACE,
    _build_scope_specific_examples,
    _llm_retry_failed_commands,
    _lookup_baseline_commands,
    _resolve_templates,
    _parse_debug_pod_name,
    _parse_llm_json_output,
    _validate_and_filter_commands,
    _normalize_debug_namespace,
    make_baseline_capture,
)


# ---------------------------------------------------------------------------
# Registry three-level lookup
# ---------------------------------------------------------------------------


class TestRegistryLookup:
    """Test _lookup_baseline_commands three-level fallback."""

    def test_exact_match(self):
        result = _lookup_baseline_commands("node", "disk", "fill")
        assert len(result) == 2
        assert result[0].description == "Node DiskPressure"
        assert result[1].mode == "debug_two_step"

    def test_target_fallback(self):
        result = _lookup_baseline_commands("node", "disk", "nonexistent_action")
        assert len(result) == 2
        assert result[0].description == "Node DiskPressure"

    def test_scope_fallback_returns_empty_for_unknown_target(self):
        """_lookup_baseline_commands only searches BASELINE_COMMANDS; scope-level
        fallback is handled by _SCOPE_FALLBACK in the node function."""
        result = _lookup_baseline_commands("node", "nonexistent", "action")
        assert result == []

    def test_no_match(self):
        result = _lookup_baseline_commands("container", "nonexistent", "action")
        assert result == []


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
        cmds = [BaselineCommand("Node DiskPressure", "describe", "node {node_name}")]
        result = _resolve_templates(cmds, state)
        assert len(result) == 1
        assert result[0]["v_args"] == "node cn-hongkong.10.0.2.69"
        assert result[0]["_unresolved"] is False

    def test_unresolved_namespace(self):
        state = {"target": {"names": ["my-pod"]}}
        cmds = [BaselineCommand("Pod info", "get", "pod {pod_name} -n {namespace}")]
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
        cmds = [BaselineCommand("Pod CPU", "top", "pod -n {namespace} {label_selector}")]
        result = _resolve_templates(cmds, state)
        assert len(result) == 1
        assert result[0]["v_args"] == "pod -n default -l app=nginx,tier=frontend"
        assert result[0]["_unresolved"] is False


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
        cmds = [BaselineCommand("Pod info", "exec", "{pod_name} -n {namespace} -- df -h")]
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
        cmds = [BaselineCommand("Pod info", "exec", "{pod_name} -n {namespace} -- df -h")]
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
        cmds = [BaselineCommand("Node info", "describe", "node {node_name}")]
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
        cmds = [BaselineCommand("Pod info", "exec", "{pod_name} -n {namespace} -- df -h")]
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
        raw = '[{"description":"test","subcommand":"get","v_args_template":"nodes","mode":"simple"}]'
        result = _parse_llm_json_output(raw)
        assert len(result) == 1
        assert result[0]["description"] == "test"

    def test_json_in_markdown_code_block(self):
        raw = '```json\n[{"description":"test","subcommand":"top","v_args_template":"nodes","mode":"simple"}]\n```'
        result = _parse_llm_json_output(raw)
        assert len(result) == 1

    def test_empty_input(self):
        assert _parse_llm_json_output("") == []
        assert _parse_llm_json_output(None) == []

    def test_invalid_json(self):
        assert _parse_llm_json_output("not json at all") == []

    def test_trailing_text(self):
        raw = '[{"description":"test","subcommand":"get","v_args_template":"nodes","mode":"simple"}] and some trailing text'
        result = _parse_llm_json_output(raw)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Command validation and filtering
# ---------------------------------------------------------------------------


class TestCommandValidation:
    """Test _validate_and_filter_commands whitelist enforcement."""

    def test_allowed_subcommands(self):
        cmds = [
            {"description": "test", "subcommand": "get", "v_args_template": "nodes", "mode": "simple"},
            {"description": "test2", "subcommand": "top", "v_args_template": "nodes", "mode": "simple"},
        ]
        result = _validate_and_filter_commands(cmds)
        assert len(result) == 2

    def test_rejected_subcommand(self):
        cmds = [
            {"description": "hack", "subcommand": "delete", "v_args_template": "pod x", "mode": "simple"},
        ]
        result = _validate_and_filter_commands(cmds)
        assert len(result) == 0

    def test_exec_with_allowed_command(self):
        cmds = [
            {"description": "disk", "subcommand": "exec", "v_args_template": "pod x -- df -h", "mode": "simple"},
        ]
        result = _validate_and_filter_commands(cmds)
        assert len(result) == 1

    def test_exec_with_disallowed_command(self):
        cmds = [
            {"description": "hack", "subcommand": "exec", "v_args_template": "pod x -- rm -rf /", "mode": "simple"},
        ]
        result = _validate_and_filter_commands(cmds)
        assert len(result) == 0

    def test_non_dict_input_skipped(self):
        cmds = ["not a dict", 42]
        result = _validate_and_filter_commands(cmds)
        assert len(result) == 0


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
        with patch("chaos_agent.agent.nodes.baseline_capture._execute_observations",
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
        with patch("chaos_agent.agent.nodes.baseline_capture._execute_observations",
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
        with patch("chaos_agent.agent.nodes.baseline_capture._execute_observations",
                    new_callable=AsyncMock, return_value=[]):
            result = await node(state)
        # No match in BASELINE_COMMANDS, _SCOPE_FALLBACK has no "container"
        assert result["baseline_data"]["source"] == "none"

    @pytest.mark.asyncio
    async def test_llm_derived_strategy(self):
        """When LLM returns valid commands, should use 'llm' source."""
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content=json.dumps([
            {"description": "Node disk", "subcommand": "top", "v_args_template": "node {node_name}", "mode": "simple"},
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
        with patch("chaos_agent.agent.nodes.baseline_capture._execute_observations",
                    new_callable=AsyncMock, return_value=[]), \
             patch("chaos_agent.agent.nodes.baseline_capture._lookup_baseline_commands",
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
        with patch("chaos_agent.agent.nodes.baseline_capture._lookup_baseline_commands",
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
        with patch("chaos_agent.agent.nodes.baseline_capture._execute_observations",
                    new_callable=AsyncMock, return_value=[
                        {"description": "test", "command": "kubectl top node test-node",
                         "exit_code": 0, "stdout": "OK", "stderr": ""},
                    ]), \
             patch("chaos_agent.agent.nodes.baseline_capture.sync_to_store",
                    new_callable=AsyncMock) as mock_sync, \
             patch("chaos_agent.agent.nodes.baseline_capture.sync_node_status_to_session") as mock_session, \
             patch("chaos_agent.agent.nodes.baseline_capture.get_tracker") as mock_tracker:
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

        # Verify result structure
        assert result["baseline_data"]["success_count"] == 1
        assert result["baseline_data"]["source"] == "registry"


# ---------------------------------------------------------------------------
# Bug fix tests: mode auto-correction, debug smart conversion,
# namespace normalization, parse_debug_pod_name new format
# ---------------------------------------------------------------------------


class TestModeAutoCorrection:
    """Test that {debug_pod} in v_args_template forces mode=debug_two_step."""

    def test_debug_pod_forces_debug_two_step_mode(self):
        """LLM generates {debug_pod} with mode=simple -> auto-corrected."""
        cmds = [
            {"description": "Node disk IO", "subcommand": "exec",
             "v_args_template": "{debug_pod} -n chaosblade -- iostat -xd 1 3",
             "mode": "simple"},
        ]
        result = _validate_and_filter_commands(cmds)
        assert len(result) == 1
        assert result[0].mode == "debug_two_step"

    def test_debug_pod_with_correct_mode_passes(self):
        """LLM generates {debug_pod} with mode=debug_two_step -> passes unchanged."""
        cmds = [
            {"description": "Node disk IO", "subcommand": "exec",
             "v_args_template": "{debug_pod} -n chaosblade -- iostat -xd 1 3",
             "mode": "debug_two_step"},
        ]
        result = _validate_and_filter_commands(cmds)
        assert len(result) == 1
        assert result[0].mode == "debug_two_step"


class TestDebugSmartConversion:
    """Test smart conversion of subcommand='debug' commands."""

    def test_debug_subcommand_with_exec_pod_dropped(self):
        """When exec {debug_pod} already exists, debug command is redundant."""
        cmds = [
            {"description": "Node disk IO", "subcommand": "exec",
             "v_args_template": "{debug_pod} -n chaosblade -- iostat -xd 1 3",
             "mode": "debug_two_step"},
            {"description": "Create debug pod", "subcommand": "debug",
             "v_args_template": "node/{node_name} --image=busybox -- sleep 3600",
             "mode": "simple"},
        ]
        result = _validate_and_filter_commands(cmds)
        assert len(result) == 1
        assert result[0].subcommand == "exec"
        assert result[0].mode == "debug_two_step"

    def test_debug_subcommand_converted_when_no_exec(self):
        """When no exec {debug_pod} exists, debug with diagnostic command is converted."""
        cmds = [
            {"description": "Debug and check disk", "subcommand": "debug",
             "v_args_template": "node/{node_name} --image=busybox -- df -h",
             "mode": "simple"},
        ]
        result = _validate_and_filter_commands(cmds)
        assert len(result) == 1
        assert result[0].subcommand == "exec"
        assert result[0].mode == "debug_two_step"
        assert "{debug_pod}" in result[0].v_args_template
        assert "df -h" in result[0].v_args_template
        assert f"-n {_TOOL_POD_NAMESPACE}" in result[0].v_args_template

    def test_debug_subcommand_dropped_when_sleep_only(self):
        """When debug only has 'sleep', it's dropped (no diagnostic intent)."""
        cmds = [
            {"description": "Create debug pod", "subcommand": "debug",
             "v_args_template": "node/{node_name} --image=busybox -- sleep 3600",
             "mode": "simple"},
        ]
        result = _validate_and_filter_commands(cmds)
        assert len(result) == 0


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
        cmds = [BaselineCommand("Node disk", "exec",
                                "{debug_pod} -n chaosblade -- df -h",
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
        cmds = [BaselineCommand("Node disk", "exec",
                                "{debug_pod} -n some-ns -- iostat -xd 1 3",
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
        assert f"-n {_TOOL_POD_NAMESPACE}" in debug_cmds[0].v_args_template
        assert "-n default" not in debug_cmds[0].v_args_template

    def test_node_disk_burn_uses_chaosblade_ns(self):
        cmds = BASELINE_COMMANDS[("node", "disk", "burn")]
        debug_cmds = [c for c in cmds if c.mode == "debug_two_step"]
        assert len(debug_cmds) == 1
        assert f"-n {_TOOL_POD_NAMESPACE}" in debug_cmds[0].v_args_template
        assert "-n default" not in debug_cmds[0].v_args_template

    def test_node_disk_fallback_uses_chaosblade_ns(self):
        cmds = BASELINE_COMMANDS[("node", "disk")]
        debug_cmds = [c for c in cmds if c.mode == "debug_two_step"]
        assert len(debug_cmds) == 1
        assert f"-n {_TOOL_POD_NAMESPACE}" in debug_cmds[0].v_args_template


# ---------------------------------------------------------------------------
# LLM prompt structure tests (U-shaped architecture validation)
# ---------------------------------------------------------------------------


class TestLLMDeriveBaselinePrompt:
    """Validate _BASELINE_SYSTEM_PROMPT structure and _llm_derive_baseline_commands prompt composition.

    These tests verify structural/semantic constraints, not exact text —
    prompt wording may evolve, but the architecture guarantees must hold.
    """

    # -- SystemMessage content tests --

    def test_system_prompt_contains_critical_rules(self):
        """Critical rules must appear in _BASELINE_SYSTEM_PROMPT."""
        assert "Scope→Variables mapping" in _BASELINE_SYSTEM_PROMPT
        assert "{debug_pod} → debug_two_step" in _BASELINE_SYSTEM_PROMPT
        assert "exec command whitelist" in _BASELINE_SYSTEM_PROMPT
        # Verify exec whitelist includes key diagnostic commands
        assert "df" in _BASELINE_SYSTEM_PROMPT
        assert "iostat" in _BASELINE_SYSTEM_PROMPT

    def test_system_prompt_u_shape_primacy(self):
        """Core Principle must appear in the primacy zone (first 600 chars).
        CRITICAL RULES is in the middle zone (syntax), not primacy."""
        primacy_zone = _BASELINE_SYSTEM_PROMPT[:750]
        assert "Core Principle" in primacy_zone
        assert "causation attribution" in primacy_zone
        assert "SAME metric" in primacy_zone

    def test_system_prompt_u_shape_recency(self):
        """REMINDER must appear in the recency zone (last 400 chars)."""
        recency_zone = _BASELINE_SYSTEM_PROMPT[-400:]
        assert "REMINDER" in recency_zone
        assert "SAME metric" in recency_zone
        assert "Scope→variables" in recency_zone

    def test_system_prompt_no_debug_prohibition_rule(self):
        """Rule 6 (subcommand='debug' prohibition) was removed from the prompt
        because _validate_and_filter_commands() Phase 2 provides full programmatic
        coverage. The prompt should not contain the old rule text."""
        assert "Avoid using subcommand 'debug'" not in _BASELINE_SYSTEM_PROMPT

    # -- Scope-specific examples tests --

    def test_node_scope_examples(self):
        """Node-scope examples must show debug_two_step mode and {debug_pod} variable."""
        examples = _build_scope_specific_examples("node")
        assert "debug_two_step" in examples
        assert "{debug_pod}" in examples
        assert "{node_name}" in examples

    def test_pod_scope_examples(self):
        """Pod-scope examples must show {pod_name} and {namespace} variables."""
        examples = _build_scope_specific_examples("pod")
        assert "{pod_name}" in examples
        assert "{namespace}" in examples
        assert "{label_selector}" in examples

    # -- LLM invocation pattern tests --

    @pytest.mark.asyncio
    async def test_llm_invoke_uses_system_and_human_messages(self):
        """_llm_derive_baseline_commands must invoke LLM with [SystemMessage, HumanMessage],
        not [HumanMessage] alone — aligning with project convention."""
        from langchain_core.messages import SystemMessage, HumanMessage

        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = json.dumps([
            {"description": "Pod CPU", "subcommand": "top",
             "v_args_template": "pod -n {namespace} {label_selector}",
             "mode": "simple"},
        ])
        mock_llm.ainvoke.return_value = mock_response

        from chaos_agent.agent.nodes.baseline_capture import _llm_derive_baseline_commands
        result = await _llm_derive_baseline_commands(
            mock_llm, "test skill content", "pod", "cpu", "fullload",
        )

        # Verify invocation pattern
        call_args = mock_llm.ainvoke.call_args
        messages = call_args[0][0]
        assert len(messages) == 2
        assert isinstance(messages[0], SystemMessage)
        assert isinstance(messages[1], HumanMessage)
        # SystemMessage uses the U-shaped prompt constant
        assert messages[0].content == _BASELINE_SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_human_prompt_focus_guidance(self):
        """HumanMessage should use fault-impact reasoning guidance."""
        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "[]"
        mock_llm.ainvoke.return_value = mock_response

        from chaos_agent.agent.nodes.baseline_capture import _llm_derive_baseline_commands
        await _llm_derive_baseline_commands(
            mock_llm, "test skill content", "pod", "cpu", "fullload",
        )

        call_args = mock_llm.ainvoke.call_args
        messages = call_args[0][0]
        human_content = messages[1].content
        # Should guide fault-impact reasoning, not old "Based on ALL content above"
        assert "reason about what states" in human_content
        assert "Based on ALL content above" not in human_content
        # Should contain fault type info
        assert "Fault type: pod-cpu-fullload" in human_content
        # Should contain skill-case tag
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
        cmd = BaselineCommand("desc", "top", "node {node_name}")
        assert cmd.extractors == []

    def test_baseline_command_extractors_round_trip(self):
        def _noop(_stdout, _state):
            return {}
        cmd = BaselineCommand(
            "desc", "top", "node {node_name}", extractors=[_noop],
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
                "Pod top", "top", "pod {pod_name} -n {namespace}",
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
            top_cmd = next(c for c in cmds if c.subcommand == "top")
            assert extract_pod_top_metrics in top_cmd.extractors

    @pytest.mark.asyncio
    async def test_extractors_run_and_merge_into_target_metadata(self):
        # End-to-end: build a baseline_capture node, mock the kubectl
        # execution to return a known ``top pod`` table, verify the
        # extractor parses it and the parsed fields land in the
        # returned state update's ``target_metadata``.
        from chaos_agent.agent.baseline_extractors import extract_pod_top_metrics

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
            "chaos_agent.agent.nodes.baseline_capture._execute_observations",
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
                "boom test", "top", "pod {pod_name} -n {namespace}",
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
                "chaos_agent.agent.nodes.baseline_capture._lookup_baseline_commands",
                return_value=crafted,
            ),
            patch(
                "chaos_agent.agent.nodes.baseline_capture._execute_observations",
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
                "spy cmd", "top", "pod {pod_name} -n {namespace}",
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
                "chaos_agent.agent.nodes.baseline_capture._lookup_baseline_commands",
                return_value=crafted,
            ),
            patch(
                "chaos_agent.agent.nodes.baseline_capture._execute_observations",
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
                "bad contract cmd", "top", "pod {pod_name} -n {namespace}",
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
                "chaos_agent.agent.nodes.baseline_capture._lookup_baseline_commands",
                return_value=crafted,
            ),
            patch(
                "chaos_agent.agent.nodes.baseline_capture._execute_observations",
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
        result = _lookup_baseline_commands("pod", "process", "kill")
        assert len(result) == 3
        descriptions = [c.description for c in result]
        assert "Service endpoints" in descriptions
        assert "Pod status/restarts" in descriptions
        assert "Pod events" in descriptions

    def test_endpoints_uses_label_selector(self):
        result = _lookup_baseline_commands("pod", "process", "kill")
        ep_cmd = next(c for c in result if c.description == "Service endpoints")
        assert "{label_selector}" in ep_cmd.v_args_template

    def test_pod_status_uses_wide_output(self):
        result = _lookup_baseline_commands("pod", "process", "kill")
        status_cmd = next(c for c in result if c.description == "Pod status/restarts")
        assert "-o wide" in status_cmd.v_args_template

    def test_target_fallback_still_works_for_other_actions(self):
        """(pod, process, <other>) should still fall back to the (pod, process) entry."""
        result = _lookup_baseline_commands("pod", "process", "stop")
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
            {"description": "Fixed endpoints", "subcommand": "get",
             "v_args_template": "endpoints -n {namespace} {label_selector}",
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
        assert _LLM_BASELINE_MAX_RETRIES == 2


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
                    {"description": "Endpoints", "subcommand": "get",
                     "v_args_template": "endpoints -n {namespace} {label_selector}",
                     "mode": "simple"},
                ]))
            else:
                return make_response(json.dumps([
                    {"description": "Fixed endpoints", "subcommand": "get",
                     "v_args_template": "endpoints -n {namespace} {label_selector}",
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
            "chaos_agent.agent.nodes.baseline_capture._execute_observations",
            new=fake_exec,
        ), patch(
            "chaos_agent.agent.nodes.baseline_capture._lookup_baseline_commands",
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
            {"description": "Pod status", "subcommand": "get",
             "v_args_template": "pods -n {namespace} {label_selector}",
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
            "chaos_agent.agent.nodes.baseline_capture._execute_observations",
            new=fake_exec,
        ), patch(
            "chaos_agent.agent.nodes.baseline_capture._lookup_baseline_commands",
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
                    {"description": "Pod status", "subcommand": "get",
                     "v_args_template": "pods -n {namespace} {label_selector}",
                     "mode": "simple"},
                    {"description": "Endpoints", "subcommand": "get",
                     "v_args_template": "endpoints -n {namespace} {label_selector}",
                     "mode": "simple"},
                ]))
            else:
                # Retry: corrected command for the failed one
                return make_response(json.dumps([
                    {"description": "Fixed endpoints", "subcommand": "get",
                     "v_args_template": "endpoints -n {namespace}",
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
            "chaos_agent.agent.nodes.baseline_capture._execute_observations",
            new=fake_exec,
        ), patch(
            "chaos_agent.agent.nodes.baseline_capture._lookup_baseline_commands",
            return_value=[],
        ):
            result = await node(state)

        # Original success (1) + retry success (1) = 2
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
            "chaos_agent.agent.nodes.baseline_capture._execute_observations",
            new=fake_exec,
        ):
            result = await node(state)

        # registry 跑挂 → 回落 scope_fallback（仍跑挂，但 source 应已切换）
        assert result["baseline_data"]["source"] == "scope_fallback"
        # 两次执行：1 次 registry + 1 次 scope_fallback（LLM 不可用，链路到此为止）
        assert exec_call_count["n"] == 2
