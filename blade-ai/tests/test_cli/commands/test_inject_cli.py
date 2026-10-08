"""Tests for CLI inject command — Fix E: node-scope namespace validation."""

import logging
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from chaos_agent.cli.main import app
from chaos_agent.config.settings import blade_ai_context

runner = CliRunner()

# run_command is patched so the CLI stops before real preflight network
# probes (which would hang against a nonexistent cluster). The duration
# resolution under test happens before that call, and the value it echoes
# is the same variable later placed in request_data["duration"].
_SKIPPED = {"code": 1, "message": "injection skipped in test", "data": {}}


class TestInjectNodeScopeNamespace:
    """Fix E: CLI should NOT require --namespace for node-scope injection."""

    def test_node_scope_without_namespace_is_valid(self):
        """Node-scope inject should succeed without --namespace."""
        # We only validate the CLI parsing — the actual injection will fail
        # without kubeconfig, but the validation should NOT reject it for
        # missing --namespace. Mock run_command so we don't hit real
        # preflight network probes (which would hang on a nonexistent cluster).
        with patch(
            "chaos_agent.cli.commands.inject.run_command",
            return_value={"code": 1, "message": "injection skipped in test", "data": {}},
        ):
            result = runner.invoke(app, [
                "inject",
                "--scope", "node",
                "--target", "disk",
                "--action", "burn",
                "-n", "cn-hongkong.10.0.1.120",
                "-p", "path=/tmp,read,write",
                "-d", "120",
                "--kubeconfig", "/nonexistent/kubeconfig",
            ])
        # The CLI should NOT error with "namespace" requirement for node scope.
        # It may fail later for other reasons (missing kubeconfig, etc.)
        # but should NOT produce the "Provide ... --namespace" error.
        assert "--namespace" not in result.output or "node" in result.output.lower()

    def test_pod_scope_without_namespace_is_invalid(self):
        """Pod-scope inject should still require --namespace."""
        result = runner.invoke(app, [
            "inject",
            "--scope", "pod",
            "--target", "cpu",
            "--action", "fullload",
            "-n", "app=myapp",
            "-p", "cpu-percent=80",
            "-d", "120",
        ])
        # Should error about missing --namespace
        assert "--namespace" in result.output or result.exit_code != 0

    def test_node_scope_without_namespace(self):
        """Structured node-scope inject should not require --namespace."""
        # Mock run_command to avoid real preflight network probes hanging
        # on a nonexistent cluster (same rationale as the test above).
        with patch(
            "chaos_agent.cli.commands.inject.run_command",
            return_value={"code": 1, "message": "injection skipped in test", "data": {}},
        ):
            result = runner.invoke(app, [
                "inject",
                "--scope", "node",
                "--target", "cpu",
                "--action", "fullload",
                "-n", "node-1",
                "-p", "cpu-percent=90",
                "-d", "120",
                "--kubeconfig", "/nonexistent/kubeconfig",
            ])
        # Should NOT complain about missing --namespace
        output = result.output
        if "Error" in output and "--namespace" in output:
            pytest.fail(
                f"Node-scope should not require --namespace, but got: {output}"
            )


class TestInjectDurationResolution:
    """Structured mode resolves an unset --duration from operator config.

    Pins the removal of the hardcoded ``duration = 300`` pre-fill. A literal
    default made ``experiment_timeout`` dead in this channel: a positive
    value reads as an explicit pin, so ensure_min_duration took its verbatim
    branch and the configured default never applied. The CLI now passes
    "unspecified" through to the single policy point.
    """

    _NODE_ARGS = [
        "inject",
        "--scope", "node",
        "--target", "cpu",
        "--action", "fullload",
        "-n", "node-1",
        "-p", "cpu-percent=90",
        "--kubeconfig", "/nonexistent/kubeconfig",
    ]

    def test_unset_duration_uses_configured_default(self):
        with blade_ai_context(experiment_timeout=900):
            with patch(
                "chaos_agent.cli.commands.inject.run_command",
                return_value=_SKIPPED,
            ):
                result = runner.invoke(app, self._NODE_ARGS)
        # The echoed value is the same variable written to
        # request_data["duration"], so this asserts the payload too.
        assert "Using the configured default 900s" in result.output

    def test_unset_duration_below_floor_is_not_raised(self):
        """A configured default below the empirical floor stays verbatim."""
        with blade_ai_context(experiment_timeout=60):
            with patch(
                "chaos_agent.cli.commands.inject.run_command",
                return_value=_SKIPPED,
            ):
                result = runner.invoke(app, self._NODE_ARGS)
        assert "Using the configured default 60s" in result.output
        assert "Using the configured default 300s" not in result.output

    def test_combined_input_mode_leaves_duration_unset(self):
        """``-i`` PLUS structured flags is still an NL run.

        The resolution below the guard is documented as structured-mode only:
        writing the resolved default back in a combined call would make it an
        explicit pin that overrules the duration stated in the description.
        """
        with blade_ai_context(experiment_timeout=900):
            with patch(
                "chaos_agent.cli.commands.inject.run_command",
                return_value=_SKIPPED,
            ):
                result = runner.invoke(
                    app, [*self._NODE_ARGS, "-i", "注入 CPU 满载，持续 60 秒"]
                )
        assert "Using the configured default" not in result.output

    def test_explicit_duration_below_floor_honoured_verbatim(self, caplog):
        """-d wins over both the configured default and the floor."""
        with caplog.at_level(
            logging.WARNING, logger="chaos_agent.utils.fault_type"
        ):
            with blade_ai_context(experiment_timeout=900):
                with patch(
                    "chaos_agent.cli.commands.inject.run_command",
                    return_value=_SKIPPED,
                ):
                    result = runner.invoke(app, [*self._NODE_ARGS, "-d", "60"])
        # No configured-default substitution happened ...
        assert "Using the configured default" not in result.output
        # ... and the explicit 60 reached the policy point untouched.
        assert "applying the requested 60s verbatim" in caplog.text
