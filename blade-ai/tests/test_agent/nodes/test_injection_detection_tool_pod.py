"""Tests for cluster-wide tool pod discovery helpers in _injection_detection.

Covers _parse_tool_pod_rows() and discover_tool_pod_on_node() which power
baseline_capture's per-node tool pod fallback and the preplan probe's
fallback-path hint.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.tools.pod_discovery import (
    parse_tool_pod_rows as _parse_tool_pod_rows,
    discover_tool_pod_on_node,
)


class TestParseToolPodRows:
    """Tests for _parse_tool_pod_rows() (ns|name|phase|node jsonpath rows)."""

    def test_empty_input(self):
        assert _parse_tool_pod_rows("") == []
        assert _parse_tool_pod_rows(None) == []  # type: ignore[arg-type]

    def test_single_running_pod(self):
        output = "default|chaosblade-tool-abc12|Running|node-a"
        assert _parse_tool_pod_rows(output) == [
            ("chaosblade-tool-abc12", "default", "node-a"),
        ]

    def test_multiple_namespaces_and_nodes(self):
        output = "\n".join([
            "default|chaosblade-tool-aaa|Running|node-a",
            "chaosblade|otel-c-tool-bbb|Running|node-b",
            "chaosblade|otel-c-tool-ccc|Running|node-c",
        ])
        assert _parse_tool_pod_rows(output) == [
            ("chaosblade-tool-aaa", "default", "node-a"),
            ("otel-c-tool-bbb", "chaosblade", "node-b"),
            ("otel-c-tool-ccc", "chaosblade", "node-c"),
        ]

    def test_filters_non_running_pods(self):
        output = "\n".join([
            "default|chaosblade-tool-aaa|Running|node-a",
            "default|chaosblade-tool-bbb|Pending|node-b",
            "default|chaosblade-tool-ccc|Failed|node-c",
        ])
        assert _parse_tool_pod_rows(output) == [
            ("chaosblade-tool-aaa", "default", "node-a"),
        ]

    def test_skips_short_lines(self):
        assert _parse_tool_pod_rows("default|chaosblade-tool-aaa|Running") == []

    def test_strips_blank_lines(self):
        output = "\ndefault|chaosblade-tool-aaa|Running|node-a\n\n"
        assert _parse_tool_pod_rows(output) == [
            ("chaosblade-tool-aaa", "default", "node-a"),
        ]

    def test_restart_annotated_pods_keep_their_node(self):
        """Regression: the old ``-o wide`` positional parse broke on RESTARTS
        annotations like ``1 (14d ago)`` — the extra space shifted columns
        and the AGE token (``123d``) was read as the node name, so pods on
        restarted nodes went unattributed and the target-node carrier was
        missed by the preplan probe. Explicit fields are immune; this row
        set mirrors the live cluster output from that incident.
        """
        output = "\n".join([
            "chaosblade|chaosblade-tool-grzlt|Running|cn-shanghai-cloudspe.10.0.0.118",
            "chaosblade|chaosblade-tool-nw82s|Running|cn-shanghai-cloudspe.10.0.2.182",
            "chaosblade|chaosblade-tool-tt8q6|Running|cn-shanghai-cloudspe.10.0.2.183",
        ])
        pods = _parse_tool_pod_rows(output)
        nodes = {node for _, _, node in pods}
        assert "cn-shanghai-cloudspe.10.0.2.183" in nodes
        assert "123d" not in nodes


@pytest.mark.asyncio
class TestDiscoverToolPodOnNode:
    """Tests for discover_tool_pod_on_node()."""

    async def test_finds_pod_on_target_node(self):
        # First label (chaosblade-tool) yields a match on node-b
        output = (
            "default|chaosblade-tool-aaa|Running|node-a\n"
            "default|chaosblade-tool-bbb|Running|node-b"
        )
        run_command_mock = AsyncMock(return_value=SimpleNamespace(
            stdout=output, stderr="", exit_code=0,
        ))
        with patch(
            "chaos_agent.transports.execute_via_transport", run_command_mock,
        ), patch(
            "chaos_agent.tools.kubectl_cli.build_kubectl_cmd",
            return_value=["kubectl", "get", "pods"],
        ):
            result = await discover_tool_pod_on_node(
                "node-b", "/tmp/kubeconfig", task_id="t1",
            )
        assert result == ("chaosblade-tool-bbb", "default")

    async def test_falls_back_to_second_label(self):
        # First label returns nothing, second label (otel-c-tool) matches
        empty = SimpleNamespace(stdout="", stderr="", exit_code=0)
        rows = SimpleNamespace(
            stdout="chaosblade|otel-c-tool-xyz|Running|node-z",
            stderr="",
            exit_code=0,
        )
        run_command_mock = AsyncMock(side_effect=[empty, rows])
        with patch(
            "chaos_agent.transports.execute_via_transport", run_command_mock,
        ), patch(
            "chaos_agent.tools.kubectl_cli.build_kubectl_cmd",
            return_value=["kubectl", "get", "pods"],
        ):
            result = await discover_tool_pod_on_node(
                "node-z", "/tmp/kubeconfig", task_id="t2",
            )
        assert result == ("otel-c-tool-xyz", "chaosblade")
        assert run_command_mock.await_count == 2

    async def test_returns_none_when_no_pod_matches_node(self):
        output = "default|chaosblade-tool-aaa|Running|node-a"
        run_command_mock = AsyncMock(return_value=SimpleNamespace(
            stdout=output, stderr="", exit_code=0,
        ))
        with patch(
            "chaos_agent.transports.execute_via_transport", run_command_mock,
        ), patch(
            "chaos_agent.tools.kubectl_cli.build_kubectl_cmd",
            return_value=["kubectl", "get", "pods"],
        ):
            result = await discover_tool_pod_on_node(
                "node-missing", "/tmp/kubeconfig", task_id="t3",
            )
        assert result is None

    async def test_returns_none_when_all_labels_empty(self):
        empty = SimpleNamespace(stdout="", stderr="", exit_code=0)
        run_command_mock = AsyncMock(return_value=empty)
        with patch(
            "chaos_agent.transports.execute_via_transport", run_command_mock,
        ), patch(
            "chaos_agent.tools.kubectl_cli.build_kubectl_cmd",
            return_value=["kubectl", "get", "pods"],
        ):
            result = await discover_tool_pod_on_node(
                "node-a", "/tmp/kubeconfig", task_id="t4",
            )
        assert result is None
