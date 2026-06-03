"""Tests for injection step completeness check."""

from unittest.mock import MagicMock

import pytest

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.nodes._injection_detection import (
    _extract_drill_steps,
    _extract_kubectl_verbs_from_step,
    _get_executed_kubectl_verbs,
    check_injection_step_completeness,
)


DAEMONSET_SKILL_CASE = """**演练步骤**：
1. 选取一个运行 DaemonSet Pod 的节点
2. 使用 kubectl 将该节点标记为不可调度（cordon）：`kubectl cordon <node>`
3. 给该节点添加一个 DaemonSet 未配置容忍的自定义污点：`kubectl taint nodes <node> chaos-drill/unschedulable=true:NoSchedule`
4. 删除该节点上的 DaemonSet Pod，观察 Pod 是否被重建
5. 观察 DaemonSet 副本数变化

**注入验证**：
1. 执行 `kubectl get nodes`，确认目标节点标记为 SchedulingDisabled
"""

COREDNS_SKILL_CASE = """**演练步骤**：
1. 记录 CoreDNS Deployment 当前副本数
2. 将 CoreDNS Deployment 的副本数缩为 0，模拟 CoreDNS 完全不可用：
   ```bash
   kubectl scale deployment coredns -n kube-system --replicas=0
   ```
3. 在应用 A 的 Pod 内尝试进行 DNS 解析

**注入验证**：
1. 确认无 CoreDNS Pod 运行
"""

BLADE_ONLY_SKILL_CASE = """**演练步骤**：
1. 确认目标 Pod 存在
2. 使用 chaosblade 注入 CPU 满载
   blade create k8s pod-cpu fullload --cpu-percent 80

**注入验证**：
1. 查看 CPU 使用率
"""


class TestExtractDrillSteps:

    def test_daemonset_has_5_steps(self):
        steps = _extract_drill_steps(DAEMONSET_SKILL_CASE)
        assert len(steps) == 5

    def test_coredns_has_3_steps(self):
        steps = _extract_drill_steps(COREDNS_SKILL_CASE)
        assert len(steps) == 3

    def test_no_drill_section(self):
        assert _extract_drill_steps("no steps here") == []

    def test_empty_content(self):
        assert _extract_drill_steps("") == []


class TestExtractKubectlVerbs:

    def test_cordon_detected(self):
        step = "使用 kubectl 将该节点标记为不可调度（cordon）"
        assert "cordon" in _extract_kubectl_verbs_from_step(step)

    def test_taint_detected(self):
        step = "给该节点添加自定义污点：kubectl taint nodes"
        assert "taint" in _extract_kubectl_verbs_from_step(step)

    def test_delete_detected(self):
        step = "删除该节点上的 DaemonSet Pod"
        assert "delete" in _extract_kubectl_verbs_from_step(step)

    def test_scale_detected(self):
        step = "kubectl scale deployment coredns --replicas=0"
        assert "scale" in _extract_kubectl_verbs_from_step(step)

    def test_no_kubectl_verb(self):
        step = "观察 DaemonSet 副本数变化"
        assert _extract_kubectl_verbs_from_step(step) == set()

    def test_blade_step_no_kubectl(self):
        step = "使用 chaosblade 注入 CPU 满载"
        assert _extract_kubectl_verbs_from_step(step) == set()


class TestGetExecutedKubectlVerbs:

    def _make_messages(self, subcommands):
        """Build AIMessage + ToolMessage pairs for kubectl calls."""
        msgs = []
        for i, (sub, success) in enumerate(subcommands):
            tc_id = f"call_{i}"
            ai = AIMessage(content="", tool_calls=[{
                "name": "kubectl", "id": tc_id, "type": "tool_call",
                "args": {"subcommand": sub, "v_args": "..."},
            }])
            content = "success" if success else "Error: command failed"
            tool = ToolMessage(
                content=content, name="kubectl", tool_call_id=tc_id,
            )
            msgs.extend([ai, tool])
        return msgs

    def test_cordon_detected(self):
        msgs = self._make_messages([("cordon", True)])
        assert "cordon" in _get_executed_kubectl_verbs(msgs)

    def test_failed_command_not_counted(self):
        msgs = self._make_messages([("taint", False)])
        assert "taint" not in _get_executed_kubectl_verbs(msgs)

    def test_multiple_commands(self):
        msgs = self._make_messages([
            ("cordon", True), ("taint", True), ("delete", True),
        ])
        executed = _get_executed_kubectl_verbs(msgs)
        assert executed == {"cordon", "taint", "delete"}

    def test_readonly_not_counted(self):
        msgs = self._make_messages([("get", True), ("describe", True)])
        assert _get_executed_kubectl_verbs(msgs) == set()


class TestCheckInjectionStepCompleteness:

    def _make_messages(self, subcommands):
        msgs = []
        for i, sub in enumerate(subcommands):
            tc_id = f"call_{i}"
            ai = AIMessage(content="", tool_calls=[{
                "name": "kubectl", "id": tc_id, "type": "tool_call",
                "args": {"subcommand": sub, "v_args": "..."},
            }])
            tool = ToolMessage(
                content="success", name="kubectl", tool_call_id=tc_id,
            )
            msgs.extend([ai, tool])
        return msgs

    def test_daemonset_only_cordon_returns_nudge(self):
        """Only cordon executed → nudge for taint + delete."""
        msgs = self._make_messages(["cordon"])
        nudge = check_injection_step_completeness(DAEMONSET_SKILL_CASE, msgs)
        assert nudge is not None
        assert "INCOMPLETE" in nudge
        assert "taint" in nudge
        assert "delete" in nudge

    def test_daemonset_all_steps_returns_none(self):
        """All steps executed → no nudge."""
        msgs = self._make_messages(["cordon", "taint", "delete"])
        nudge = check_injection_step_completeness(DAEMONSET_SKILL_CASE, msgs)
        assert nudge is None

    def test_coredns_only_scale_returns_none(self):
        """CoreDNS only needs scale → no nudge."""
        msgs = self._make_messages(["scale"])
        nudge = check_injection_step_completeness(COREDNS_SKILL_CASE, msgs)
        assert nudge is None

    def test_blade_only_skill_returns_none(self):
        """Blade-only skill case has no kubectl verbs → no nudge."""
        msgs = self._make_messages(["get"])
        nudge = check_injection_step_completeness(BLADE_ONLY_SKILL_CASE, msgs)
        assert nudge is None

    def test_empty_skill_case_returns_none(self):
        nudge = check_injection_step_completeness("", [])
        assert nudge is None

    def test_no_messages_returns_nudge(self):
        """Skill case has steps but no commands executed → nudge."""
        nudge = check_injection_step_completeness(DAEMONSET_SKILL_CASE, [])
        assert nudge is not None
        assert "cordon" in nudge

    def test_fallback_no_drill_section_extracts_from_injection_content(self):
        """Skill case without 演练步骤 but with kubectl verbs → fallback extraction."""
        no_drill = (
            "**注入方式**：\n"
            "使用 kubectl cordon 标记节点不可调度，\n"
            "然后 kubectl taint 添加污点\n"
            "\n**注入恢复**：\n"
            "1. kubectl uncordon <node>\n"
        )
        msgs = self._make_messages(["cordon"])
        nudge = check_injection_step_completeness(no_drill, msgs)
        assert nudge is not None
        assert "taint" in nudge
        assert "uncordon" not in nudge

    def test_fallback_only_recovery_verbs_returns_none(self):
        """Skill case with kubectl verbs only in recovery section → no nudge."""
        only_recovery = (
            "**注入方式**：\n"
            "使用 kubectl cordon 标记节点不可调度\n"
            "\n**注入恢复**：\n"
            "1. kubectl uncordon <node>\n"
            "2. kubectl taint nodes <node> key-\n"
        )
        msgs = self._make_messages(["cordon"])
        nudge = check_injection_step_completeness(only_recovery, msgs)
        assert nudge is None

    def test_fallback_no_kubectl_verbs_returns_none(self):
        """Skill case without 演练步骤 and no kubectl verbs → no nudge."""
        no_verbs = "**故障现象**：应用 CPU 使用率过高\n"
        nudge = check_injection_step_completeness(no_verbs, [])
        assert nudge is None
