"""Tests for inject_context utility — causal chain illusion prevention.

Verifies that build_inject_context() produces abstracts that:
  1. Do NOT contain reusable raw kubectl metric values
  2. Contain EXPIRED markers and MUST-re-execute instructions
  3. Preserve structural information (tool type, success/fail)
  4. Handle various kubectl output formats correctly
"""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.utils.inject_context import (
    build_inject_context,
    _abstract_kubectl_result,
    _strip_metric_values,
    _count_filesystems_in_df,
    _extract_pod_name_from_describe,
)


# ---------------------------------------------------------------------------
# Fixtures: realistic inject-phase messages
# ---------------------------------------------------------------------------

def _make_df_h_tool_result():
    """Realistic df -h output from pod-disk-burn inject verification."""
    return ToolMessage(
        content=(
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "overlay         116G   13G   97G  12% /\n"
            "tmpfs            64M     0   64M   0% /dev\n"
            "/dev/vdb        116G   13G   97G  12% /etc/hosts\n"
            "shm              64M     0   64M   0% /dev/shm\n"
        ),
        tool_call_id="call_df_h_123",
        name="kubectl",
    )


def _make_describe_pod_tool_result():
    """Realistic kubectl describe pod output."""
    return ToolMessage(
        content=(
            "Name:             accounting-6fbdb464c7-qn2vr\n"
            "Namespace:        cms-demo\n"
            "Priority:         0\n"
            "Service Account:  otel-demo\n"
            "Node:             cn-hongkong\n"
            "Start Time:       Tue, 28 Apr 2026 11:40:08 +0800\n"
            "Status:           Running\n"
            "Restart Count:    7\n"
            "Last State:       Terminated (OOMKilled)\n"
        ),
        tool_call_id="call_describe_456",
        name="kubectl",
    )


def _make_get_pods_tool_result():
    """Realistic kubectl get pods output."""
    return ToolMessage(
        content=(
            "NAME                READY   STATUS    RESTARTS      AGE\n"
            "otel-c-tool-5pmkc   1/1     Running   0             33d\n"
            "otel-c-tool-7t526   1/1     Running   0             33d\n"
            "otel-c-tool-4hvvf   0/1     Evicted   0             11m\n"
        ),
        tool_call_id="call_get_pods_789",
        name="kubectl",
    )


def _make_blade_destroy_tool_result():
    """Realistic blade destroy output (structured JSON)."""
    return ToolMessage(
        content=(
            '{"code":200,"success":true,"result":'
            '"command: k8s pod-disk burn --path=/tmp --namespace=cms-demo '
            '--read=true --write=true, destroy time: 2026-05-13T05:52:32Z"}'
        ),
        tool_call_id="call_blade_destroy_101",
        name="blade_destroy",
    )


def _make_ai_message():
    """AI reasoning from inject verifier."""
    return AIMessage(
        content=(
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer2: passed — disk burn effect confirmed\n"
            "- BaselineUsed: true\n"
            "- Overall: verified"
        ),
    )


def _make_exec_ls_tool_result():
    """kubectl exec ls output."""
    return ToolMessage(
        content="total 8.0K\ndrwxrwxrwx 1 root root 4096 May 12 01:15 .\n-rwxrwx 1 root root    0 May 12 01:15 .burn_test",
        tool_call_id="call_exec_ls_202",
        name="kubectl",
    )


def _make_du_tool_result():
    """kubectl exec du -sh output."""
    return ToolMessage(
        content="4.0K\t/tmp",
        tool_call_id="call_du_303",
        name="kubectl",
    )


# ---------------------------------------------------------------------------
# Core function tests
# ---------------------------------------------------------------------------

class TestBuildInjectContext:
    """Test build_inject_context with realistic inject-phase messages."""

    def test_empty_messages_returns_empty(self):
        result = build_inject_context([])
        assert result == ""

    def test_no_tool_messages_only_ai(self):
        msgs = [_make_ai_message()]
        result = build_inject_context(msgs)
        assert "EXPIRED" in result
        assert "RECOVERY_VERIFICATION_RESULT" in result
        # AI content should be present (it's LLM's own conclusions)
        assert "Layer2: passed" in result

    def test_df_h_output_no_reusable_values(self):
        """df -h raw output must NOT contain specific percentages/sizes."""
        msgs = [_make_df_h_tool_result()]
        result = build_inject_context(msgs)
        # EXPIRED marker must be present
        assert "EXPIRED" in result
        # MUST-re-execute instruction must be present
        assert "re-execute" in result
        # Specific metric values (12%, 13G, 97G) must NOT appear
        assert "12%" not in result
        assert "13G" not in result
        assert "97G" not in result
        # Abstract description should mention "filesystem(s)"
        assert "filesystem" in result
        # "re-execute df -h now" must appear
        assert "re-execute df -h now" in result

    def test_describe_pod_no_reusable_values(self):
        """kubectl describe must NOT contain pod status/restart details."""
        msgs = [_make_describe_pod_tool_result()]
        result = build_inject_context(msgs)
        assert "EXPIRED" in result
        # Pod name can appear in abstract (it's structural, not a metric value)
        assert "accounting" in result
        # Namespace can appear
        assert "cms-demo" in result
        # But Restart Count value must NOT appear
        assert "Restart Count" not in result
        # "Restart Count: 7" must not appear; pod name may contain "7" which is OK
        # "re-execute now" must appear
        assert "re-execute" in result

    def test_get_pods_no_reusable_values(self):
        """kubectl get pods must NOT contain pod status details."""
        msgs = [_make_get_pods_tool_result()]
        result = build_inject_context(msgs)
        assert "EXPIRED" in result
        # Specific pod statuses must NOT appear
        assert "Running" not in result
        assert "1/1" not in result
        # But row count can appear (structural info)
        assert "3 entries" in result

    def test_blade_destroy_preserved(self):
        """blade_destroy output is structured JSON — safe to include."""
        msgs = [_make_blade_destroy_tool_result()]
        result = build_inject_context(msgs)
        assert "EXPIRED" in result
        # blade destroy success info is preserved (it's not kubectl data)
        assert "blade_destroy" in result
        assert "code" in result or "200" in result
        assert "success" in result

    def test_mixed_messages_all_abstracted(self):
        """All kubectl outputs must be abstracted; AI content preserved."""
        msgs = [
            _make_ai_message(),
            _make_df_h_tool_result(),
            _make_describe_pod_tool_result(),
            _make_get_pods_tool_result(),
            _make_blade_destroy_tool_result(),
        ]
        result = build_inject_context(msgs)
        assert "EXPIRED" in result
        # AI content preserved
        assert "Layer2: passed" in result
        # df -h values NOT present
        assert "12%" not in result
        assert "13G" not in result
        # describe pod structural info preserved but metrics abstracted
        assert "accounting" in result
        # get pods values NOT present
        assert "Running" not in result
        # blade destroy preserved
        assert "blade_destroy" in result

    def test_max_length_constraint(self):
        """Result should not exceed max length."""
        # Create many messages to stress the length limit
        msgs = []
        for i in range(20):
            msgs.append(AIMessage(content=f"Verification step {i}: passed — some long evidence here " * 10))
            msgs.append(ToolMessage(content=f"Filesystem 116G 13G 97G 12% overlay / {i}", name="kubectl", tool_call_id=f"tc_{i}"))
        result = build_inject_context(msgs)
        # Should be truncated but still contain EXPIRED prefix
        assert "EXPIRED" in result
        # Total length should be reasonable (prefix + truncated context)
        assert len(result) < 5000

    def test_exec_ls_output_abstracted(self):
        """kubectl exec ls output must be abstracted."""
        msgs = [_make_exec_ls_tool_result()]
        result = build_inject_context(msgs)
        assert "EXPIRED" in result
        # "ls/dir listing" hint should appear
        assert "ls" in result or "dir listing" in result
        # Specific file names/sizes should NOT appear
        assert "8.0K" not in result
        assert ".burn_test" not in result

    def test_du_output_abstracted(self):
        """kubectl exec du -sh output must be abstracted."""
        msgs = [_make_du_tool_result()]
        result = build_inject_context(msgs)
        assert "EXPIRED" in result
        # "du/size check" hint should appear
        assert "du" in result or "size" in result
        # Specific size values should NOT appear
        assert "4.0K" not in result


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestAbstractKubectlResult:
    """Test _abstract_kubectl_result with various output formats."""

    def test_df_output_abstract(self):
        content = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "overlay         116G   13G   97G  12% /\n"
        )
        abstract = _abstract_kubectl_result(content)
        assert "filesystem" in abstract
        assert "re-execute df -h now" in abstract
        assert "12%" not in abstract
        assert "13G" not in abstract

    def test_describe_pod_abstract(self):
        content = (
            "Name:             my-pod-123\n"
            "Namespace:        test-ns\n"
            "Status:           Running\n"
        )
        abstract = _abstract_kubectl_result(content)
        assert "my-pod-123" in abstract
        assert "test-ns" in abstract
        assert "re-execute now" in abstract

    def test_get_pods_abstract(self):
        content = (
            "NAME                READY   STATUS    RESTARTS      AGE\n"
            "pod-1               1/1     Running   0             5d\n"
            "pod-2               1/1     Running   0             5d\n"
        )
        abstract = _abstract_kubectl_result(content)
        assert "2 entries" in abstract
        assert "re-execute now" in abstract

    def test_fallback_abstract(self):
        """Unknown kubectl output format gets generic abstract."""
        content = "some random output that doesn't match any pattern"
        abstract = _abstract_kubectl_result(content)
        assert "kubectl" in abstract
        assert "expired" in abstract


class TestStripMetricValues:
    """Test _strip_metric_values removes specific metric patterns."""

    def test_percentage_values(self):
        result = _strip_metric_values("overlay 12% /dev/vdb 84%")
        assert "12%" not in result
        assert "84%" not in result
        assert "[expired]" in result

    def test_size_values(self):
        result = _strip_metric_values("13G used 100M avail 4.0K total")
        assert "13G" not in result
        assert "100M" not in result
        assert "4.0K" not in result

    def test_restart_count(self):
        result = _strip_metric_values("Restart Count: 7")
        assert "Restart Count: 7" not in result
        assert "[expired]" in result

    def test_preserves_non_metric_text(self):
        result = _strip_metric_values("Pod is Running with no events")
        assert "Running" in result
        assert "no events" in result


class TestCountFilesystemsInDf:
    def test_count(self):
        content = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "overlay         116G   13G   97G  12% /\n"
            "tmpfs            64M     0   64M   0% /dev\n"
        )
        assert _count_filesystems_in_df(content) == 2

    def test_empty_data(self):
        content = "Filesystem      Size  Used Avail Use% Mounted on\n"
        assert _count_filesystems_in_df(content) == 0


class TestExtractPodNameFromDescribe:
    def test_extract(self):
        content = "Name:             accounting-6fbdb464c7-qn2vr\nNamespace:        cms-demo\n"
        assert _extract_pod_name_from_describe(content) == "accounting-6fbdb464c7-qn2vr"

    def test_no_name(self):
        content = "Some other content without Name field"
        assert _extract_pod_name_from_describe(content) == ""


class TestBladeJsonDetection:
    """Test detection and abstraction of blade JSON wrapped in kubectl exec."""

    def test_blade_json_in_kubectl_abstracted(self):
        """Blade destroy JSON wrapped in kubectl exec should preserve code/success."""
        blade_json = '{"code":200,"success":true,"result":"command: k8s pod-disk burn --path=/tmp, destroy time: 2026-05-13Z"}'
        abstract = _abstract_kubectl_result(blade_json)
        assert "blade operation result" in abstract
        assert "code=200" in abstract
        assert "success=True" in abstract
        # Should not be classified as generic exec output
        assert "exec command output" not in abstract

    def test_blade_json_in_full_context(self):
        """Blade destroy via kubectl exec preserved in full inject context."""
        msgs = [
            ToolMessage(
                content='{"code":200,"success":true,"result":"command: k8s pod-disk burn"}',
                tool_call_id="tc_blade",
                name="kubectl",
            ),
        ]
        result = build_inject_context(msgs)
        assert "blade operation result" in result
        assert "code=200" in result
        assert "EXPIRED" in result

    def test_non_json_kubectl_not_detected_as_blade(self):
        """Regular kubectl output (not JSON) should not trigger blade detection."""
        df_output = "Filesystem      Size  Used Avail Use% Mounted on\noverlay  116G  13G  97G  12% /"
        from chaos_agent.utils.inject_context import _looks_like_blade_json
        assert not _looks_like_blade_json(df_output)

    def test_blade_create_json_preserved(self):
        """blade create JSON (also wrapped in kubectl exec) preserved."""
        blade_create = '{"code":200,"success":true,"result":"command: k8s pod-cpu fullload --cpu-percent 80"}'
        abstract = _abstract_kubectl_result(blade_create)
        assert "blade operation result" in abstract
        assert "code=200" in abstract