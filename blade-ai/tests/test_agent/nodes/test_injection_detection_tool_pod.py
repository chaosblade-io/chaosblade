"""Tests for cluster-wide tool pod discovery helpers in _injection_detection.

Covers _parse_all_ns_pods_wide() and discover_tool_pod_on_node() which power
baseline_capture's per-node tool pod fallback.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.nodes._injection_detection import (
    _parse_all_ns_pods_wide,
    discover_tool_pod_on_node,
)


class TestParseAllNsPodsWide:
    """Tests for _parse_all_ns_pods_wide()."""

    def test_empty_input(self):
        assert _parse_all_ns_pods_wide("") == []
        assert _parse_all_ns_pods_wide(None) == []  # type: ignore[arg-type]

    def test_single_running_pod(self):
        output = (
            "default  chaosblade-tool-abc12  1/1  Running  0  3d  10.0.2.5  node-a  "
            "<none>  <none>"
        )
        assert _parse_all_ns_pods_wide(output) == [
            ("chaosblade-tool-abc12", "default", "node-a"),
        ]

    def test_multiple_namespaces_and_nodes(self):
        output = "\n".join([
            "default      chaosblade-tool-aaa  1/1  Running  0  1d  10.0.0.1  node-a  <none>  <none>",
            "chaosblade   otel-c-tool-bbb      1/1  Running  0  2d  10.0.0.2  node-b  <none>  <none>",
            "chaosblade   otel-c-tool-ccc      1/1  Running  0  2d  10.0.0.3  node-c  <none>  <none>",
        ])
        assert _parse_all_ns_pods_wide(output) == [
            ("chaosblade-tool-aaa", "default", "node-a"),
            ("otel-c-tool-bbb", "chaosblade", "node-b"),
            ("otel-c-tool-ccc", "chaosblade", "node-c"),
        ]

    def test_filters_non_running_pods(self):
        output = "\n".join([
            "default  chaosblade-tool-aaa  1/1  Running             0  1d  10.0.0.1  node-a  <none>  <none>",
            "default  chaosblade-tool-bbb  0/1  Pending             0  10s  <none>   node-b  <none>  <none>",
            "default  chaosblade-tool-ccc  0/1  CrashLoopBackOff    3   2m  10.0.0.3  node-c  <none>  <none>",
        ])
        assert _parse_all_ns_pods_wide(output) == [
            ("chaosblade-tool-aaa", "default", "node-a"),
        ]

    def test_skips_short_lines(self):
        output = "default  chaosblade-tool-aaa  1/1  Running  0"
        assert _parse_all_ns_pods_wide(output) == []

    def test_strips_blank_lines(self):
        output = (
            "\n"
            "default  chaosblade-tool-aaa  1/1  Running  0  1d  10.0.0.1  node-a\n"
            "\n"
        )
        assert _parse_all_ns_pods_wide(output) == [
            ("chaosblade-tool-aaa", "default", "node-a"),
        ]


@pytest.mark.asyncio
class TestDiscoverToolPodOnNode:
    """Tests for discover_tool_pod_on_node()."""

    async def test_finds_pod_on_target_node(self):
        # First label (chaosblade-tool) yields a match on node-b
        output = (
            "default  chaosblade-tool-aaa  1/1  Running  0  1d  10.0.0.1  node-a  <none>  <none>\n"
            "default  chaosblade-tool-bbb  1/1  Running  0  1d  10.0.0.2  node-b  <none>  <none>"
        )
        run_command_mock = AsyncMock(return_value=SimpleNamespace(
            stdout=output, stderr="", exit_code=0,
        ))
        with patch(
            "chaos_agent.tools.shell.run_command", run_command_mock,
        ), patch(
            "chaos_agent.tools.kubectl.build_kubectl_cmd",
            return_value=["kubectl", "get", "pods"],
        ):
            result = await discover_tool_pod_on_node(
                "node-b", "/tmp/kubeconfig", task_id="t1",
            )
        assert result == ("chaosblade-tool-bbb", "default")

    async def test_falls_back_to_second_label(self):
        # First label returns nothing, second label (otel-c-tool) matches
        empty = SimpleNamespace(stdout="", stderr="", exit_code=0)
        wide = SimpleNamespace(
            stdout=(
                "chaosblade  otel-c-tool-xyz  1/1  Running  0  1d  10.0.0.5  node-z  <none>  <none>"
            ),
            stderr="",
            exit_code=0,
        )
        run_command_mock = AsyncMock(side_effect=[empty, wide])
        with patch(
            "chaos_agent.tools.shell.run_command", run_command_mock,
        ), patch(
            "chaos_agent.tools.kubectl.build_kubectl_cmd",
            return_value=["kubectl", "get", "pods"],
        ):
            result = await discover_tool_pod_on_node(
                "node-z", "/tmp/kubeconfig", task_id="t2",
            )
        assert result == ("otel-c-tool-xyz", "chaosblade")
        assert run_command_mock.await_count == 2

    async def test_returns_none_when_no_pod_matches_node(self):
        output = (
            "default  chaosblade-tool-aaa  1/1  Running  0  1d  10.0.0.1  node-a  <none>  <none>"
        )
        run_command_mock = AsyncMock(return_value=SimpleNamespace(
            stdout=output, stderr="", exit_code=0,
        ))
        with patch(
            "chaos_agent.tools.shell.run_command", run_command_mock,
        ), patch(
            "chaos_agent.tools.kubectl.build_kubectl_cmd",
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
            "chaos_agent.tools.shell.run_command", run_command_mock,
        ), patch(
            "chaos_agent.tools.kubectl.build_kubectl_cmd",
            return_value=["kubectl", "get", "pods"],
        ):
            result = await discover_tool_pod_on_node(
                "node-a", "/tmp/kubeconfig", task_id="t4",
            )
        assert result is None
