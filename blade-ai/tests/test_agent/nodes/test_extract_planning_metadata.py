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
    _has_browsed_catalogue,
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

    def test_prefers_first_use_case_when_multiple_no_disambiguation(self):
        """When multiple use-case ToolMessages exist without plan/AI references, take the first one.

        The LLM typically reads the primary case first, then alternatives
        for comparison. Without disambiguation signals, first-read wins.
        """
        messages = [
            _make_tool_msg_read_skill(SAMPLE_SKILL_CASE, tool_call_id="tc_1"),
            _make_tool_msg_read_skill(SAMPLE_SKILL_CASE_NODE_CPU, tool_call_id="tc_2"),
        ]
        result = _extract_skill_case_from_messages(messages)
        assert result == SAMPLE_SKILL_CASE

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
        """NL mode: skill_case_content extracted from messages.

        Note: blade_scope/blade_target/blade_action derivation moved
        out of this node when FaultSpec became the single source of
        truth — those fields are now written by intent_clarification
        from submit_fault_intent's args.
        """
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
        # Note: scope derivation moved to intent_clarification — this
        # node now only extracts skill_case_content.

    @pytest.mark.asyncio
    async def test_no_messages_returns_empty(self):
        """No messages at all → empty result."""
        state = AgentState(task_id="test-task", messages=[])
        result = await extract_planning_metadata(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_guard_rejects_when_no_case_loaded(self):
        """Messages exist but no catalogue case → planning_rejected=True."""
        state = AgentState(
            task_id="test-task",
            messages=[
                HumanMessage(content="inject network loss"),
                AIMessage(content="I will inject network loss"),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result["planning_rejected"] is True
        assert len(result["messages"]) == 1
        assert "PLANNING REJECTED" in result["messages"][0].content

    @pytest.mark.asyncio
    async def test_guard_passes_when_case_in_state(self):
        """skill_case_content already in state → guard does not trigger."""
        state = AgentState(
            task_id="test-task",
            messages=[HumanMessage(content="inject")],
            skill_case_content=SAMPLE_SKILL_CASE,
        )
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True

    @pytest.mark.asyncio
    async def test_guard_passes_when_case_in_messages(self):
        """Case extracted from messages → guard does not trigger."""
        state = AgentState(
            task_id="test-task",
            messages=[
                HumanMessage(content="inject"),
                ToolMessage(
                    content=SAMPLE_SKILL_CASE,
                    tool_call_id="call_1",
                    name="read_skill_resource",
                ),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True
        assert result.get("skill_case_content") == SAMPLE_SKILL_CASE


# ---------------------------------------------------------------------------
# _has_browsed_catalogue — catalogue browse detection
# ---------------------------------------------------------------------------


class TestHasBrowsedCatalogue:

    def test_browsed(self):
        msgs = [
            AIMessage(content="", tool_calls=[{
                "name": "read_skill_resource", "id": "c1", "type": "tool_call",
                "args": {"skill_name": "k8s", "resource_path": "references/catalogue/"},
            }]),
        ]
        assert _has_browsed_catalogue(msgs) is True

    def test_browsed_subdir(self):
        msgs = [
            AIMessage(content="", tool_calls=[{
                "name": "read_skill_resource", "id": "c1", "type": "tool_call",
                "args": {"skill_name": "k8s", "resource_path": "references/catalogue/Pod_OOM内存异常/"},
            }]),
        ]
        assert _has_browsed_catalogue(msgs) is True

    def test_not_browsed(self):
        msgs = [
            AIMessage(content="", tool_calls=[{
                "name": "kubectl_ro", "id": "c1", "type": "tool_call",
                "args": {"subcommand": "get", "v_args": "pods"},
            }]),
        ]
        assert _has_browsed_catalogue(msgs) is False

    def test_empty_messages(self):
        assert _has_browsed_catalogue([]) is False


# ---------------------------------------------------------------------------
# Catalogue rejection guard
# ---------------------------------------------------------------------------


class TestCatalogueRejectionGuard:

    @pytest.mark.asyncio
    async def test_rejection_without_catalogue_browse_is_nudged(self):
        """LLM rejects without browsing catalogue → nudge, not reject."""
        state = AgentState(
            task_id="test-task",
            messages=[
                AIMessage(content="", tool_calls=[{
                    "name": "finish_planning", "id": "fp1", "type": "tool_call",
                    "args": {"summary": "not supported", "rejected": True,
                             "rejection_reason": "ChaosBlade cannot do this"},
                }]),
                ToolMessage(
                    content="Planning rejected. Reason: ChaosBlade cannot do this",
                    tool_call_id="fp1", name="finish_planning",
                ),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is True
        assert result.get("_catalogue_rejection_nudged") is True
        assert "error" not in result
        assert any("REJECTION NOT ACCEPTED" in m.content
                    for m in result.get("messages", []))

    @pytest.mark.asyncio
    async def test_rejection_after_catalogue_browse_is_accepted(self):
        """LLM browsed catalogue then rejects → accepted as real rejection."""
        state = AgentState(
            task_id="test-task",
            messages=[
                AIMessage(content="", tool_calls=[{
                    "name": "read_skill_resource", "id": "rs1", "type": "tool_call",
                    "args": {"skill_name": "k8s",
                             "resource_path": "references/catalogue/"},
                }]),
                ToolMessage(
                    content="Directory: ...", tool_call_id="rs1",
                    name="read_skill_resource",
                ),
                AIMessage(content="", tool_calls=[{
                    "name": "finish_planning", "id": "fp1", "type": "tool_call",
                    "args": {"summary": "no match", "rejected": True,
                             "rejection_reason": "No matching use case"},
                }]),
                ToolMessage(
                    content="Planning rejected. Reason: No matching use case",
                    tool_call_id="fp1", name="finish_planning",
                ),
            ],
        )
        result = await extract_planning_metadata(state)
        # LLM browsed catalogue then rejected → genuine rejection.
        # error is set so routing terminates at reject node (not agent_loop).
        assert result.get("planning_rejected") is True
        assert result.get("error") == "No matching use case"
        assert result.get("_planning_rejection_reason") == "No matching use case"
        assert result.get("_catalogue_rejection_nudged") is not True

    @pytest.mark.asyncio
    async def test_nudge_only_once(self):
        """Second rejection after nudge → accepted (no infinite loop)."""
        state = AgentState(
            task_id="test-task",
            _catalogue_rejection_nudged=True,
            messages=[
                AIMessage(content="", tool_calls=[{
                    "name": "finish_planning", "id": "fp2", "type": "tool_call",
                    "args": {"summary": "still not supported", "rejected": True,
                             "rejection_reason": "Really not supported"},
                }]),
                ToolMessage(
                    content="Planning rejected. Reason: Really not supported",
                    tool_call_id="fp2", name="finish_planning",
                ),
            ],
        )
        result = await extract_planning_metadata(state)
        # Second rejection after nudge → genuine rejection, terminate.
        # error is set so routing terminates at reject node (not agent_loop).
        assert result.get("planning_rejected") is True
        assert result.get("error") == "Really not supported"
        assert result.get("_planning_rejection_reason") == "Really not supported"