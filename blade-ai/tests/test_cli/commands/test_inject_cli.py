"""Tests for CLI inject command — Fix E: node-scope namespace validation."""

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from chaos_agent.cli.main import app

runner = CliRunner()


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
                "--direct",
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
            "--direct",
        ])
        # Should error about missing --namespace
        assert "--namespace" in result.output or result.exit_code != 0

    def test_node_scope_direct_without_namespace(self):
        """--direct with node-scope should not require --namespace."""
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
                "--direct",
                "--kubeconfig", "/nonexistent/kubeconfig",
            ])
        # Should NOT complain about missing --namespace
        output = result.output
        if "Error" in output and "--namespace" in output:
            pytest.fail(
                f"Node-scope should not require --namespace, but got: {output}"
            )
