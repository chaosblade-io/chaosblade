"""Tests for the ``{target_pod}`` declarative placeholder mechanism
(baseline-observation-contract change).

Covers three layers of the mechanism:

1. **Template resolution** (``_templates._resolve_one_baseline`` +
   ``_resolve_templates``): pure substitution of ``{target_pod}`` from a
   pre-resolved literal pod name, fail-open to ``_unresolved`` when the
   value is empty, and the unknown-var scan exemption list.

2. **Runtime resolution** (``baseline_capture._resolve_target_pod``): the
   async prewarm that produces the literal — pod scope is free
   (``spec.names[0]``), workload scope costs one ``kubectl get pods
   -l <sel> -o jsonpath`` query, node/host scope returns None.

3. **Regression anchor**: ``{pod_name}`` semantics under deployment scope
   MUST remain "workload name" (not pod name) — the two placeholders have
   distinct resolution sources and MUST NOT be conflated (memory e5aad315).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chaos_agent.agent.nodes.baseline.baseline_capture import (
    BaselineCommand,
    _resolve_target_pod,
    _resolve_templates,
)


# ---------------------------------------------------------------------------
# 1. Template resolution: pure substitution + fail-open + exemption list
# ---------------------------------------------------------------------------


class TestTargetPodTemplateResolution:
    """``{target_pod}`` substitution in ``_resolve_templates``."""

    def test_literal_pod_name_substituted(self):
        """Happy path: prewarm yielded a pod name → placeholder replaced."""
        state = {
            "fault_scope": "deployment",
            "target": {
                "namespace": "default",
                "names": ["drill-perms-target"],  # workload name, NOT pod name
                "labels": {"app": "drill-perms-target"},
            },
        }
        cmds = [BaselineCommand(
            "Container perm check",
            "kubectl exec {target_pod} -n {namespace} -- stat /app/config.yaml",
        )]
        result = _resolve_templates(
            cmds, state, profile="k8s",
            target_pod="drill-perms-target-6d4f8-x2k9p",
        )
        assert len(result) == 1
        assert result[0]["_unresolved"] is False
        # Substituted with the LITERAL pod name, not the workload name.
        assert "drill-perms-target-6d4f8-x2k9p" in result[0]["command"]
        assert "{target_pod}" not in result[0]["command"]
        # And crucially — NOT substituted with the workload name.
        assert "exec drill-perms-target " not in result[0]["command"]

    def test_empty_target_pod_marks_unresolved(self):
        """Fail-open: prewarm returned None → placeholder survives →
        ``_unresolved`` = True → viability gate skips the observation.
        MUST NOT execute a command with literal ``{target_pod}`` text."""
        state = {
            "fault_scope": "deployment",
            "target": {"namespace": "default", "names": ["d"], "labels": {}},
        }
        cmds = [BaselineCommand(
            "Container check", "kubectl exec {target_pod} -n default -- id",
        )]
        result = _resolve_templates(cmds, state, profile="k8s", target_pod=None)
        assert result[0]["_unresolved"] is True

    def test_target_pod_not_in_unknown_var_scan(self):
        """``{target_pod}`` MUST be exempted from the unknown-var scan
        (mechanism shared with ``{debug_pod}``, memory a98c6772). Otherwise
        a surviving placeholder would double-fault: once via the unresolved
        flag, once via the unknown-var warning path."""
        state = {
            "fault_scope": "deployment",
            "target": {"namespace": "default", "names": ["d"], "labels": {}},
        }
        cmds = [BaselineCommand(
            "Container check", "kubectl exec {target_pod} -n default -- id",
        )]
        # Even with target_pod=None, the exemption list keeps {target_pod}
        # out of the unknown_vars warning — the unresolved flag alone is
        # the correct signal.
        with patch(
            "chaos_agent.agent.nodes.baseline._templates.logger"
        ) as mock_logger:
            _resolve_templates(cmds, state, profile="k8s", target_pod=None)
            # No "Unknown template variable" warning for target_pod
            for call in mock_logger.warning.call_args_list:
                msg = call.args[0] if call.args else ""
                if "Unknown template variable" in msg:
                    assert "target_pod" not in str(call.args)

    def test_multi_command_reuses_same_literal(self):
        """Per-run cache contract: multiple ``{target_pod}`` commands in one
        batch all get the SAME literal pod name (no per-command re-resolution,
        isomorphic to _exec_in_debug_pod's per-node reuse)."""
        state = {
            "fault_scope": "deployment",
            "target": {
                "namespace": "default", "names": ["d"],
                "labels": {"app": "d"},
            },
        }
        cmds = [
            BaselineCommand("c1", "kubectl exec {target_pod} -n default -- id"),
            BaselineCommand("c2", "kubectl exec {target_pod} -n default -- mount"),
            BaselineCommand("c3", "kubectl exec {target_pod} -n default -- ps aux"),
        ]
        result = _resolve_templates(
            cmds, state, profile="k8s", target_pod="the-pod-abc",
        )
        assert all("the-pod-abc" in r["command"] for r in result)
        assert all(not r["_unresolved"] for r in result)


class TestTargetPodVsPodNameIndependence:
    """Regression anchor (memory e5aad315): ``{pod_name}`` and
    ``{target_pod}`` have DIFFERENT resolution sources and MUST NOT be
    conflated. Under deployment scope, ``{pod_name}`` = deployment name
    (from spec.names), ``{target_pod}`` = literal pod name (from
    prewarm query)."""

    def test_pod_name_is_workload_name_under_deployment_scope(self):
        state = {
            "fault_scope": "deployment",
            "target": {
                "namespace": "default",
                "names": ["my-deployment"],  # workload name
                "labels": {"app": "my-deployment"},
            },
        }
        cmds = [BaselineCommand(
            "API object query",
            "kubectl get deployment {pod_name} -n default",
        )]
        result = _resolve_templates(
            cmds, state, profile="k8s", target_pod="my-pod-xyz-123",
        )
        # {pod_name} resolves to the workload name (existing semantics,
        # unchanged by this change).
        assert "my-deployment" in result[0]["command"]
        # {target_pod} would resolve to the literal pod name if used, but
        # this command doesn't use it.
        assert result[0]["_unresolved"] is False

    def test_both_placeholders_coexist_independently(self):
        """A command using BOTH ``{pod_name}`` and ``{target_pod}`` gets
        two DIFFERENT substitutions from two different resolution sources."""
        state = {
            "fault_scope": "deployment",
            "target": {
                "namespace": "default",
                "names": ["my-deployment"],
                "labels": {"app": "my-deployment"},
            },
        }
        cmds = [BaselineCommand(
            "Cross-check",
            # Hypothetical: compare deployment metadata against pod state
            "kubectl get deployment {pod_name} -n default && echo {target_pod}",
        )]
        # Note: the ``&&`` makes this fail the shell-metachar screen at
        # validate_command time, but _resolve_templates doesn't validate —
        # it just substitutes. Testing the substitution logic in isolation.
        result = _resolve_templates(
            cmds, state, profile="k8s", target_pod="my-pod-xyz-123",
        )
        cmd = result[0]["command"]
        assert "my-deployment" in cmd   # {pod_name} → workload name
        assert "my-pod-xyz-123" in cmd  # {target_pod} → literal pod name


# ---------------------------------------------------------------------------
# 2. Runtime resolution: async prewarm function
# ---------------------------------------------------------------------------


def _mk_ctx(*, profile="k8s", scope="pod", names=("p1",), labels=None,
            namespace="default", pod_selector=None, kubeconfig=""):
    """Build a MagicMock ctx resembling _BaselineCtx for _resolve_target_pod."""
    ctx = MagicMock()
    ctx.profile = profile
    ctx.scope = scope
    ctx.kubeconfig = kubeconfig
    ctx.task_id = "test-task"
    ctx.pod_selector = pod_selector
    spec = MagicMock()
    spec.names = list(names)
    spec.labels = labels or {}
    spec.namespace = namespace
    ctx.spec = spec
    ctx.tracker = MagicMock()
    return ctx


class TestResolveTargetPodAsync:
    """Async prewarm logic in ``_resolve_target_pod``."""

    @pytest.mark.asyncio
    async def test_pod_scope_returns_names_zero_without_query(self):
        """pod scope: spec.names[0] IS the literal pod name (frozen by
        confirmation_gate). MUST NOT issue any kubectl query."""
        ctx = _mk_ctx(scope="pod", names=("my-pod-abc",))
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
            new_callable=AsyncMock,
        ) as mock_exec:
            result = await _resolve_target_pod(ctx)
        assert result == "my-pod-abc"
        mock_exec.assert_not_awaited()  # zero calls
        ctx.tracker.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_workload_scope_queries_once_via_pod_selector(self):
        """deployment scope with Phase 1.5 pod_selector: one get-pods query,
        returns the literal pod name from stdout."""
        ctx = _mk_ctx(
            scope="deployment", names=("my-deploy",),
            pod_selector={"app": "my-app"},
        )
        mock_result = MagicMock()
        mock_result.exit_code = 0
        mock_result.stdout = "my-pod-xyz-123\n"
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
            new_callable=AsyncMock, return_value=mock_result,
        ) as mock_exec:
            result = await _resolve_target_pod(ctx)
        assert result == "my-pod-xyz-123"
        assert mock_exec.await_count == 1
        # Verify the query shape: -l <selector> -o jsonpath=...items[0].metadata.name
        call_args = mock_exec.await_args.args[0]
        cmd_str = " ".join(call_args) if isinstance(call_args, list) else str(call_args)
        assert "-l" in cmd_str and "app=my-app" in cmd_str
        assert "jsonpath={.items[0].metadata.name}" in cmd_str

    @pytest.mark.asyncio
    async def test_workload_scope_falls_back_to_spec_labels(self):
        """When Phase 1.5 pod_selector is None (fail-open upstream), fall
        back to spec.labels for the selector."""
        ctx = _mk_ctx(
            scope="deployment", names=("d",),
            labels={"app": "from-spec"},
            pod_selector=None,
        )
        mock_result = MagicMock()
        mock_result.exit_code = 0
        mock_result.stdout = "fallback-pod"
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
            new_callable=AsyncMock, return_value=mock_result,
        ) as mock_exec:
            result = await _resolve_target_pod(ctx)
        assert result == "fallback-pod"
        cmd_str = str(mock_exec.await_args.args[0])
        assert "app=from-spec" in cmd_str

    @pytest.mark.asyncio
    async def test_query_failure_failopen_to_none(self):
        """Any exception during the query → None (fail-open, never crash)."""
        ctx = _mk_ctx(
            scope="deployment", names=("d",),
            pod_selector={"app": "x"},
        )
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
            new_callable=AsyncMock, side_effect=RuntimeError("boom"),
        ):
            result = await _resolve_target_pod(ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_nonzero_exit_failopen_to_none(self):
        ctx = _mk_ctx(scope="deployment", names=("d",), pod_selector={"a": "b"})
        mock_result = MagicMock()
        mock_result.exit_code = 1
        mock_result.stdout = ""
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
            new_callable=AsyncMock, return_value=mock_result,
        ):
            result = await _resolve_target_pod(ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_stdout_failopen_to_none(self):
        """Zero exit but empty output (no pods matched selector) → None.
        MUST NOT return an empty string that would pass through substitution
        as if it were a valid pod name."""
        ctx = _mk_ctx(scope="deployment", names=("d",), pod_selector={"a": "b"})
        mock_result = MagicMock()
        mock_result.exit_code = 0
        mock_result.stdout = "   \n"  # whitespace only
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
            new_callable=AsyncMock, return_value=mock_result,
        ):
            result = await _resolve_target_pod(ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_selector_available_failopen_to_none(self):
        """Workload scope but neither pod_selector nor spec.labels → None
        (nothing to query with)."""
        ctx = _mk_ctx(
            scope="deployment", names=("d",),
            pod_selector=None, labels={},
        )
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
            new_callable=AsyncMock,
        ) as mock_exec:
            result = await _resolve_target_pod(ctx)
        assert result is None
        mock_exec.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_node_scope_not_applicable(self):
        """node/host scope: {target_pod} doesn't apply — return None
        without any query."""
        ctx = _mk_ctx(scope="node", names=("node-1",))
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
            new_callable=AsyncMock,
        ) as mock_exec:
            result = await _resolve_target_pod(ctx)
        assert result is None
        mock_exec.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_host_profile_not_applicable(self):
        """host profile: no kubectl at all — return None."""
        ctx = _mk_ctx(profile="host", scope="pod", names=("p",))
        with patch(
            "chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
            new_callable=AsyncMock,
        ) as mock_exec:
            result = await _resolve_target_pod(ctx)
        assert result is None
        mock_exec.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pod_scope_empty_names_failopen(self):
        ctx = _mk_ctx(scope="pod", names=())
        result = await _resolve_target_pod(ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_tracker_written_on_success(self):
        """Verifier transparency: successful resolution MUST write to the
        tracker so the baseline receipt shows which pod was sampled."""
        ctx = _mk_ctx(scope="pod", names=("the-pod",))
        result = await _resolve_target_pod(ctx)
        assert result == "the-pod"
        # tracker.update called with the pod name in payload
        ctx.tracker.update.assert_called_once()
        payload = ctx.tracker.update.call_args.args[1]
        assert payload["step"] == "target_pod_resolution"
        assert payload["pod"] == "the-pod"
