"""Tests for the /plan dry-run injection plan generator.

Regression coverage for the baseline-preview section, which previously
referenced the removed ``BaselineCommand.v_args_template`` / ``.subcommand``
fields and called ``_lookup_baseline_commands`` with the old 3-arg signature.
Any (scope, target) that hits the registry used to hard-crash ``/plan`` with
``TypeError`` / ``AttributeError``; these tests lock the migrated behavior.
"""

import chaos_agent.transports as transports
from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.agent.spec.plan_generator import (
    _resolve_baseline_template,
    _section_baseline_preview,
)


class TestSectionBaselinePreviewK8s:
    """k8s profile: registry hit renders full kubectl command with templates."""

    def test_node_cpu_renders_resolved_full_command(self):
        spec = FaultSpec(scope="node", blade_target="cpu", names=("worker-1",))
        out = _section_baseline_preview(spec)
        # Full command string is rendered (no bare "kubectl {subcommand}" glue).
        assert "kubectl top node worker-1" in out
        assert "kubectl describe node worker-1" in out
        # Description column is preserved.
        assert "Node resource usage" in out
        # No unrendered template variables leak into the preview.
        assert "{node_name}" not in out

    def test_pod_process_kill_renders_namespace_and_label_selector(self):
        spec = FaultSpec(
            scope="pod",
            blade_target="process",
            blade_action="kill",
            namespace="prod",
            names=("api-0",),
            labels={"app": "api"},
        )
        out = _section_baseline_preview(spec)
        assert "-n prod" in out
        # {label_selector} must render as "-l app=api", not bare "app=api".
        assert "-l app=api" in out
        assert "{namespace}" not in out
        assert "{label_selector}" not in out

    def test_no_registry_match_returns_llm_fallback_note(self):
        spec = FaultSpec(scope="pod", blade_target="totally-unknown-target")
        out = _section_baseline_preview(spec)
        assert "LLM" in out

    def test_missing_scope_or_target_returns_empty(self):
        assert _section_baseline_preview(FaultSpec()) == ""
        assert _section_baseline_preview(FaultSpec(scope="node")) == ""

    def test_does_not_raise_on_registry_hit(self):
        # The original bug: 3-arg lookup + .v_args_template / .subcommand access
        # raised before this line could return. Assert it simply completes.
        spec = FaultSpec(scope="node", blade_target="cpu", names=("n1",))
        assert _section_baseline_preview(spec)  # non-empty, no exception


class TestSectionBaselinePreviewHost:
    """host profile: preview uses shell diagnostics, not kubectl."""

    def test_host_channel_uses_shell_commands(self, monkeypatch):
        monkeypatch.setattr(transports, "resolve_channel_name", lambda *a, **k: "ssh")
        spec = FaultSpec(scope="host", blade_target="cpu")
        out = _section_baseline_preview(spec)
        assert "top -bn1" in out
        assert "kubectl" not in out


class TestResolveBaselineTemplate:
    """Full-command template resolution used by the preview."""

    def test_resolves_known_variables(self):
        spec = FaultSpec(
            scope="pod",
            namespace="ns1",
            names=("pod-a",),
            labels={"app": "web", "tier": "fe"},
        )
        rendered = _resolve_baseline_template(
            "kubectl get pod {pod_name} -n {namespace} {label_selector}", spec,
        )
        assert rendered == "kubectl get pod pod-a -n ns1 -l app=web,tier=fe"

    def test_node_name_from_first_name_for_node_scope(self):
        spec = FaultSpec(scope="node", names=("node-7",))
        assert _resolve_baseline_template(
            "kubectl describe node {node_name}", spec,
        ) == "kubectl describe node node-7"

    def test_placeholders_when_data_missing(self):
        spec = FaultSpec(scope="node")
        assert "<node>" in _resolve_baseline_template("x {node_name}", spec)
        assert "<namespace>" in _resolve_baseline_template("y {namespace}", spec)
