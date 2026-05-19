"""Tests for extract_planning_metadata node.

Validates that the node correctly extracts skill_case_content and
derives blade_scope/target/action from agent_loop message history,
filling the State gap that causes baseline_capture to produce
source="none" in NL mode.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes.extract_planning_metadata import (
    _extract_skill_case_from_messages,
    _derive_scope_target_action,
    _derive_scope_from_resource_path,
    extract_planning_metadata,
)
from chaos_agent.agent.state import AgentState

# ── Fixtures ──

SAMPLE_SKILL_CASE = """**用例名称** 异常IO占用 导致 Pod_磁盘IO过高

**故障现象**：
1. Pod 容器内磁盘 IO 使用率持续过高

**演练步骤**：
1. 使用 ChaosBlade 对目标 Pod 注入磁盘 IO 负载：
   - 命令示例：`blade create k8s pod-disk burn --names <pod> --namespace <ns> --path /tmp --size 100 --read --write --timeout 600 --kubeconfig <path>`

**注入验证**：
1. df -h 查看磁盘使用率

**恢复验证**：
1. 确认恢复到 baseline 水平
"""

SAMPLE_SKILL_CASE_NODE_CPU = """**用例名称** CPU使用率过高

**故障现象**：
1. Pod CPU 使用率持续过高

**演练步骤**：
1. blade create k8s pod-cpu fullload --names <pod> --namespace <ns> --cpu-percent 80 --timeout 600

**注入验证**：
1. kubectl top pod 查看 CPU 使用率

**恢复验证**：
1. 确认 CPU 恢复正常
"""

SAMPLE_SKILL_CASE_NODE_DISK = """**用例名称** 磁盘使用率过高

**故障现象**：
1. Node 磁盘使用率持续过高

**演练步骤**：
1. blade create k8s node-disk fill --path /var/lib/docker --percent 95 --timeout 600

**注入验证**：
1. df -h 查看磁盘使用率

**恢复验证**：
1. 确认磁盘使用率恢复
"""


def _make_tool_msg_read_skill(content: str, tool_call_id: str = "tc_1") -> ToolMessage:
    return ToolMessage(
        content=content,
        name="read_skill_resource",
        tool_call_id=tool_call_id,
    )


def _make_ai_msg_with_read_skill(resource_path: str, tool_call_id: str = "tc_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_skill_resource",
                "args": {
                    "skill_name": "k8s-chaos-skills",
                    "resource_path": resource_path,
                },
                "id": tool_call_id,
            },
        ],
    )


def _make_tool_msg_directory_listing(tool_call_id: str = "tc_dir") -> ToolMessage:
    return ToolMessage(
        content="Directory: references/catalogue/\nContents:\n  - Pod_磁盘IO过高/\n  - Node_CPU使用率过高/\n",
        name="read_skill_resource",
        tool_call_id=tool_call_id,
    )


# ── _extract_skill_case_from_messages ──


class TestExtractSkillCaseFromMessages:

    def test_basic_extraction(self):
        """Extract skill_case_content from a use-case ToolMessage."""
        messages = [
            HumanMessage(content="inject disk fault"),
            _make_ai_msg_with_read_skill(
                "references/catalogue/Pod_磁盘IO过高/Pod_磁盘IO过高_异常IO占用.md",
            ),
            _make_tool_msg_read_skill(SAMPLE_SKILL_CASE),
        ]
        result = _extract_skill_case_from_messages(messages)
        assert result == SAMPLE_SKILL_CASE

    def test_skips_directory_listing(self):
        """Directory listing ToolMessages should be skipped."""
        messages = [
            _make_tool_msg_directory_listing(),
        ]
        result = _extract_skill_case_from_messages(messages)
        assert result == ""

    def test_skips_empty_content(self):
        """ToolMessage with empty content should be skipped."""
        messages = [
            ToolMessage(content="", name="read_skill_resource", tool_call_id="tc_1"),
        ]
        result = _extract_skill_case_from_messages(messages)
        assert result == ""

    def test_returns_empty_when_no_tool_messages(self):
        """No ToolMessages → empty result."""
        messages = [
            HumanMessage(content="inject disk fault"),
            AIMessage(content="I will activate the skill."),
        ]
        result = _extract_skill_case_from_messages(messages)
        assert result == ""

    def test_skips_non_read_skill_resource_tool(self):
        """ToolMessages from other tools (e.g., kubectl) should be skipped."""
        messages = [
            ToolMessage(
                content="NAME   READY   STATUS\npod1   1/1    Running",
                name="kubectl",
                tool_call_id="tc_k1",
            ),
        ]
        result = _extract_skill_case_from_messages(messages)
        assert result == ""

    def test_prefers_last_use_case_when_multiple(self):
        """When multiple use-case ToolMessages exist, take the last one."""
        messages = [
            _make_tool_msg_read_skill(SAMPLE_SKILL_CASE, tool_call_id="tc_1"),
            _make_tool_msg_read_skill(SAMPLE_SKILL_CASE_NODE_CPU, tool_call_id="tc_2"),
        ]
        result = _extract_skill_case_from_messages(messages)
        assert result == SAMPLE_SKILL_CASE_NODE_CPU

    def test_skips_use_case_without_markers(self):
        """ToolMessage without use-case markers (故障现象/注入验证/恢复验证) is skipped."""
        messages = [
            ToolMessage(
                content="Some random text without any markers",
                name="read_skill_resource",
                tool_call_id="tc_1",
            ),
        ]
        result = _extract_skill_case_from_messages(messages)
        assert result == ""


# ── _derive_scope_target_action ──


class TestDeriveScopeTargetAction:

    def test_pod_disk_burn(self):
        """Parse pod-disk burn → (pod, disk, burn)."""
        scope, target, action = _derive_scope_target_action(SAMPLE_SKILL_CASE)
        assert scope == "pod"
        assert target == "disk"
        assert action == "burn"

    def test_pod_cpu_fullload(self):
        """Parse pod-cpu fullload → (pod, cpu, fullload)."""
        scope, target, action = _derive_scope_target_action(SAMPLE_SKILL_CASE_NODE_CPU)
        assert scope == "pod"
        assert target == "cpu"
        assert action == "fullload"

    def test_node_disk_fill(self):
        """Parse node-disk fill → (node, disk, fill)."""
        scope, target, action = _derive_scope_target_action(SAMPLE_SKILL_CASE_NODE_DISK)
        assert scope == "node"
        assert target == "disk"
        assert action == "fill"

    def test_empty_skill_case(self):
        """Empty skill case → all empty."""
        scope, target, action = _derive_scope_target_action("")
        assert scope == ""
        assert target == ""
        assert action == ""

    def test_multiple_commands_takes_first(self):
        """Skill case with multiple ChaosBlade commands → first match."""
        content = (
            "blade create k8s pod-disk burn --path /tmp\n"
            "blade create k8s pod-disk fill --path /data\n"
        )
        scope, target, action = _derive_scope_target_action(content)
        assert scope == "pod"
        assert target == "disk"
        assert action == "burn"


# ── _derive_scope_from_resource_path ──


class TestDeriveScopeFromResourcePath:

    def test_pod_directory(self):
        """Pod_磁盘IO过高 path → scope=pod."""
        messages = [
            _make_ai_msg_with_read_skill(
                "references/catalogue/Pod_磁盘IO过高/Pod_磁盘IO过高_异常IO占用.md",
            ),
        ]
        scope = _derive_scope_from_resource_path(messages)
        assert scope == "pod"

    def test_node_directory(self):
        """Node_CPU使用率过高 path → scope=node."""
        messages = [
            _make_ai_msg_with_read_skill(
                "references/catalogue/Node_CPU使用率过高/Node_CPU使用率过高_异常占用.md",
            ),
        ]
        scope = _derive_scope_from_resource_path(messages)
        assert scope == "node"

    def test_daemonset_directory(self):
        """DaemonSet_未完全调度 path → scope=pod (mapped)."""
        messages = [
            _make_ai_msg_with_read_skill(
                "references/catalogue/DaemonSet_未完全调度/DaemonSet_未完全调度_调度受限.md",
            ),
        ]
        scope = _derive_scope_from_resource_path(messages)
        assert scope == "pod"

    def test_node_container_runtime_disk(self):
        """节点容器运行时 disk path → scope=node."""
        messages = [
            _make_ai_msg_with_read_skill(
                "references/catalogue/节点容器运行时磁盘使用率过高/节点容器运行时磁盘使用率过高_日志堆积.md",
            ),
        ]
        scope = _derive_scope_from_resource_path(messages)
        assert scope == "node"

    def test_no_read_skill_resource_call(self):
        """No read_skill_resource call → empty scope."""
        messages = [
            AIMessage(content="I will proceed", tool_calls=[]),
        ]
        scope = _derive_scope_from_resource_path(messages)
        assert scope == ""

    def test_prefers_last_call(self):
        """Multiple read_skill_resource calls → last one wins."""
        messages = [
            _make_ai_msg_with_read_skill(
                "references/catalogue/Pod_磁盘IO过高/Pod_磁盘IO过高_异常IO占用.md",
                tool_call_id="tc_1",
            ),
            _make_ai_msg_with_read_skill(
                "references/catalogue/Node_CPU使用率过高/Node_CPU使用率过高_异常占用.md",
                tool_call_id="tc_2",
            ),
        ]
        scope = _derive_scope_from_resource_path(messages)
        assert scope == "node"


# ── extract_planning_metadata (full node) ──


class TestExtractPlanningMetadataNode:

    @pytest.mark.asyncio
    async def test_nl_mode_full_extraction(self):
        """NL mode: all fields extracted from messages."""
        state = AgentState(
            task_id="test-task",
            messages=[
                HumanMessage(content="inject disk IO fault on pod"),
                AIMessage(content="", tool_calls=[
                    {"name": "activate_skill", "args": {"skill_name": "k8s-chaos-skills"}, "id": "tc_act"},
                ]),
                _make_ai_msg_with_read_skill(
                    "references/catalogue/Pod_磁盘IO过高/Pod_磁盘IO过高_异常IO占用.md",
                    tool_call_id="tc_read",
                ),
                _make_tool_msg_read_skill(SAMPLE_SKILL_CASE, tool_call_id="tc_read"),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result["skill_case_content"] == SAMPLE_SKILL_CASE
        assert result["blade_scope"] == "pod"
        assert result["blade_target"] == "disk"
        assert result["blade_action"] == "burn"

    @pytest.mark.asyncio
    async def test_direct_mode_not_affected(self):
        """Direct mode: State already has values → node returns empty dict."""
        state = AgentState(
            task_id="test-task",
            skill_case_content="already loaded",
            blade_scope="pod",
            blade_target="cpu",
            blade_action="fullload",
            messages=[],
        )
        result = await extract_planning_metadata(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_partial_state_skill_case_only(self):
        """State has blade_scope but not skill_case_content → only skill_case extracted."""
        state = AgentState(
            task_id="test-task",
            blade_scope="pod",
            blade_target="disk",
            blade_action="burn",
            messages=[
                _make_tool_msg_read_skill(SAMPLE_SKILL_CASE),
            ],
        )
        result = await extract_planning_metadata(state)
        assert "skill_case_content" in result
        # blade_scope/target/action already exist → not overwritten
        assert "blade_scope" not in result
        assert "blade_target" not in result
        assert "blade_action" not in result

    @pytest.mark.asyncio
    async def test_scope_fallback_from_resource_path(self):
        """No ChaosBlade command in skill_case → scope derived from resource_path."""
        # Skill case without blade command pattern
        weak_skill_case = """**故障现象**：Pod Pending

**注入验证**：
1. kubectl get pods 查看

**恢复验证**：
1. 确认 Pod Running
"""
        state = AgentState(
            task_id="test-task",
            messages=[
                _make_ai_msg_with_read_skill(
                    "references/catalogue/Pod_Pending/Pod_Pending_节点资源不足.md",
                    tool_call_id="tc_read",
                ),
                _make_tool_msg_read_skill(weak_skill_case, tool_call_id="tc_read"),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result["skill_case_content"] == weak_skill_case
        assert result["blade_scope"] == "pod"
        # target/action may be empty (no ChaosBlade command in skill_case)
        # This is expected — registry fallback uses (scope, target) level

    @pytest.mark.asyncio
    async def test_no_messages_returns_empty(self):
        """No messages at all → empty result."""
        state = AgentState(task_id="test-task", messages=[])
        result = await extract_planning_metadata(state)
        assert result == {}