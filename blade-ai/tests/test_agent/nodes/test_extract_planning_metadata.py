"""Tests for extract_planning_metadata node.

Validates that the node correctly extracts skill_case_content and
derives blade_scope/target/action from agent_loop message history,
filling the State gap that causes baseline_capture to produce
source="none" in NL mode.
"""

import logging

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
    _extract_skill_case_from_messages,
    _derive_scope_target_action,
    _extract_plan_verification_slices,
    _find_saved_plan,
    _has_browsed_catalogue,
    _user_stated_duration_hint,
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

    def test_skips_skill_md_intent_table(self):
        """A SKILL.md read must not be taken for a use case.

        ``read_skill_resource`` only rejects paths escaping the skill dir, so
        "SKILL.md" is returnable. Both k8s-chaos-skills and host-chaos-skills
        SKILL.md carry the intent-extraction table below, whose 故障现象 row
        used to satisfy the bare-substring marker test and made a capability
        table the source of truth for every layer-2 extractor.
        """
        skill_md = """从用户输入中提取四个**语义维度**（只关心「注入什么故障」）：

| 维度 | 说明 | 示例 |
|------|------|------|
| **故障层级** | 作用在哪个层面 | Pod / Workload / Service / Node |
| **故障现象** | 期望模拟的表现 | Pending、OOM、CPU 高 |
| **故障原因** | 具体根因 | 资源不足、配置错误 |
"""
        messages = [
            ToolMessage(
                content=skill_md,
                name="read_skill_resource",
                tool_call_id="tc_1",
            ),
        ]
        result = _extract_skill_case_from_messages(messages)
        assert result == ""

    def test_keeps_real_case_alongside_skill_md(self):
        """A SKILL.md read must not shadow the use case read in the same batch."""
        messages = [
            ToolMessage(
                content="| **故障现象** | 期望模拟的表现 | Pending、OOM |\n",
                name="read_skill_resource",
                tool_call_id="tc_md",
            ),
            _make_tool_msg_read_skill(SAMPLE_SKILL_CASE, tool_call_id="tc_1"),
        ]
        result = _extract_skill_case_from_messages(messages)
        assert result == SAMPLE_SKILL_CASE


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


# Retired with B83: the resource-path directory-prefix scope derivation
# (_derive_scope_from_resource_path + _DIR_PREFIX_SCOPE_MAP) was deleted —
# symptom-level directory names are exactly the "unaware source" the
# declaration contract replaced (see TestFinishPlanningFaultIdentityDeclaration).


# ── extract_planning_metadata (full node) ──


class TestExtractPlanningMetadataNode:

    @pytest.fixture(autouse=True)
    def _isolate_experiment_timeout(self):
        # Pin the operator default to the code floor so the B14 backfill
        # floor assertion does not depend on the host machine's
        # ~/.blade-ai/config.json (a stale experiment_timeout there wins
        # the unspecified-duration path via max(configured, floor)).
        from chaos_agent.config.settings import blade_ai_context
        from chaos_agent.utils.fault_type import _DEFAULT_MIN_DURATION

        with blade_ai_context(experiment_timeout=_DEFAULT_MIN_DURATION):
            yield

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
        """Direct mode: State already has values → no re-derivation.

        The one write the node still performs is the B14 duration
        contract backfill: a spec materialised from legacy scattered
        fields carries no duration, so it lands on the safety floor.
        Everything the State already had stays untouched. Modern direct
        entries (from_cli_structured / from_intent_args) pre-fill the
        duration at construction, so for them the node stays a no-op.
        """
        state = AgentState(
            task_id="test-task",
            skill_case_content="already loaded",
            fault_scope="pod",
            fault_target="cpu",
            fault_action="fullload",
            messages=[],
        )
        result = await extract_planning_metadata(state)
        # No re-extraction of what the State already has.
        assert "skill_case_content" not in result
        # Duration contract backfilled onto the floor (B14); the
        # already-populated identity fields pass through untouched.
        from chaos_agent.agent.spec.fault_spec import read_fault_spec
        spec = read_fault_spec({**state, **result})
        assert spec is not None
        assert spec.duration_seconds == 300
        assert (spec.scope, spec.fault_target, spec.fault_action) == (
            "pod", "cpu", "fullload",
        )

    @pytest.mark.asyncio
    async def test_partial_state_skill_case_only(self):
        """State has blade_scope but not skill_case_content → only skill_case extracted."""
        state = AgentState(
            task_id="test-task",
            fault_scope="pod",
            fault_target="disk",
            fault_action="burn",
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
# User-stated duration hint — vocabulary behind the B14 fallback warning
# ---------------------------------------------------------------------------

_NODE_LOGGER = "chaos_agent.agent.nodes.planning.extract_planning_metadata"


class TestUserStatedDurationHint:
    """``_user_stated_duration_hint`` gates a WARNING, never a write. Narrow
    by design: a miss keeps today's behaviour, while a false alarm would nag
    on well-formed descriptions and erode trust in the warning."""

    def test_chinese_prefix_form(self):
        hint = _user_stated_duration_hint("注入 CPU 满载故障，持续 120 秒，到期自动恢复")
        assert hint == "持续 120 秒"

    def test_chinese_suffix_form(self):
        hint = _user_stated_duration_hint("注入磁盘 IO 故障，300 秒后自动恢复")
        assert hint == "300 秒后"

    def test_chinese_minutes(self):
        hint = _user_stated_duration_hint("对节点注入 CPU 满载，时长：10 分钟")
        assert hint == "时长：10 分钟"

    def test_english_prefix_form(self):
        hint = _user_stated_duration_hint("inject cpu fullload for 5 minutes")
        assert hint == "for 5 minutes"

    def test_english_suffix_form(self):
        hint = _user_stated_duration_hint(
            "blade create k8s pod-cpu fullload, 600 seconds later auto-recover"
        )
        assert hint == "600 seconds later auto-recover"

    # ── Carriers the CLI-NL corpus actually uses (the first version missed
    # these: 窗口/倒计时 as keyword, and an English keyword with a Chinese
    # unit) ──

    def test_chinese_window_keyword(self):
        hint = _user_stated_duration_hint(
            "注入「Sidecar 网络中断 → 网络丢包」故障，窗口 600 秒。"
        )
        assert hint == "窗口 600 秒"

    def test_chinese_countdown_keyword(self):
        hint = _user_stated_duration_hint(
            "返还 PV 返回 200 后先武装定时恢复再注入——倒计时 900 秒，与注入紧邻"
        )
        assert hint == "倒计时 900 秒"

    def test_mixed_english_keyword_chinese_unit(self):
        """Real descriptions mix the axes: English keyword, Chinese unit."""
        hint = _user_stated_duration_hint(
            "注入「资源饱和导致 HPA 副本达到上限」故障（kubectl-native 路径），"
            "duration 600 秒，到期容器内定时器自动恢复。"
        )
        assert hint == "duration 600 秒"

    def test_duration_contract_header_form(self):
        hint = _user_stated_duration_hint("duration 契约：600 秒（系统地板）")
        assert hint == "duration 契约：600 秒"

    def test_approximation_modifier_between_keyword_and_number(self):
        hint = _user_stated_duration_hint("duration 契约：实际故障窗口约 300 秒（注入→观察）")
        assert hint == "窗口约 300 秒"

    def test_fault_parameters_do_not_trip_it(self):
        """A number inside a fault parameter is not a duration statement."""
        assert _user_stated_duration_hint("注入网络延迟故障：delay 3000ms，比例 30%") == ""

    def test_port_and_percent_do_not_trip_it(self):
        assert _user_stated_duration_hint("Pod 监听端口 3306，cpu-percent=80") == ""

    def test_beyond_a_day_is_not_a_window(self):
        assert _user_stated_duration_hint("持续 99999 秒") == ""

    def test_keyword_inside_a_longer_word_is_not_a_keyword(self):
        """``\\b`` keeps the English keyword axis honest: the "for" inside
        "before" is not a duration keyword."""
        assert _user_stated_duration_hint("观察 Pod 状态 before 300 秒, then stop") == ""

    def test_discipline_interval_is_not_a_window(self):
        """Documented boundary: the arm/inject gap is a discipline, not the
        window, and it carries a symbol no keyword form allows."""
        assert _user_stated_duration_hint("武装与注入间隔 ≤60 秒，注入序列不插入其他操作") == ""

    def test_observation_cost_is_not_a_window(self):
        """Documented boundary: a cost of waiting is not a stated window."""
        assert _user_stated_duration_hint("STS 停在 1/2 需人工删 Pod 才收敛（45 秒）") == ""

    def test_empty_text(self):
        assert _user_stated_duration_hint("") == ""


class TestStatedDurationFallbackWarning:
    """B14's silent case made audible: nothing declared a window while the
    description reads like it stated one. The warning reports the fallback,
    it never invents a value."""

    @pytest.fixture(autouse=True)
    def _isolate_experiment_timeout(self):
        from chaos_agent.config.settings import blade_ai_context

        with blade_ai_context(experiment_timeout=300):
            yield

    @staticmethod
    def _state(description: str, messages: list | None = None, **spec_overrides):
        from chaos_agent.agent.spec.fault_spec import FaultSpec

        spec = FaultSpec(
            scope="pod",
            fault_target="cpu",
            fault_action="fullload",
            source="cli_nl",
            user_description=description,
            **spec_overrides,
        )
        return AgentState(
            task_id="test-task",
            skill_case_content="already loaded",
            fault_spec=spec.to_dict(),
            messages=messages or [],
        )

    @pytest.mark.asyncio
    async def test_warns_when_description_states_a_window_nobody_declared(self, caplog):
        from chaos_agent.agent.spec.fault_spec import read_fault_spec

        state = self._state("对 Pod 注入 CPU 满载，持续 120 秒，到期自动恢复")
        with caplog.at_level(logging.WARNING, logger=_NODE_LOGGER):
            result = await extract_planning_metadata(state)
        assert "appears to state a duration" in caplog.text
        assert "持续 120 秒" in caplog.text
        # The contract still lands on the operator default — the warning
        # reports the mismatch instead of amending the value.
        assert read_fault_spec({**state, **result}).duration_seconds == 300

    @pytest.mark.asyncio
    async def test_silent_when_the_planner_declares(self, caplog):
        from chaos_agent.agent.spec.fault_spec import read_fault_spec

        declared = AIMessage(content="", tool_calls=[
            {
                "name": "finish_planning",
                "args": {"summary": "plan", "duration_seconds": 120},
                "id": "tc_fp",
            },
        ])
        state = self._state("对 Pod 注入 CPU 满载，持续 120 秒", messages=[declared])
        with caplog.at_level(logging.WARNING, logger=_NODE_LOGGER):
            result = await extract_planning_metadata(state)
        assert "appears to state a duration" not in caplog.text
        assert read_fault_spec({**state, **result}).duration_seconds == 120

    @pytest.mark.asyncio
    async def test_silent_when_description_states_nothing(self, caplog):
        state = self._state("对 Pod 注入 CPU 满载故障")
        with caplog.at_level(logging.WARNING, logger=_NODE_LOGGER):
            await extract_planning_metadata(state)
        assert "appears to state a duration" not in caplog.text

    @pytest.mark.asyncio
    async def test_silent_when_the_entry_value_is_pinned(self, caplog):
        """A hard-pinned entry value owns the contract: the statement in the
        description cannot overrule it, so there is nothing to warn about."""
        from chaos_agent.agent.spec.fault_spec import read_fault_spec

        state = self._state(
            "对 Pod 注入 CPU 满载，持续 120 秒", duration_seconds=600
        )
        with caplog.at_level(logging.WARNING, logger=_NODE_LOGGER):
            result = await extract_planning_metadata(state)
        assert "appears to state a duration" not in caplog.text
        assert read_fault_spec({**state, **result}).duration_seconds == 600

# ---------------------------------------------------------------------------
# _has_browsed_catalogue — catalogue browse detection
# ---------------------------------------------------------------------------


# ── B76 ② (rewritten for the B83 declaration contract): a scope change
# from the planner's declaration clears BOTH old-scope fields ──


@pytest.mark.asyncio
async def test_declaration_scope_change_clears_stale_labels_along_with_names():
    """B76 regression, carried into the declaration contract: a label
    selector locked under the OLD scope is exactly as kind-inconsistent as
    a name. The planner's declaration rewrites an INCOMPLETE identity
    (the lazy probe-derived scope below), and a scope change clears names
    AND labels with it — the r4 run froze scope=node + a Pod label into an
    unexecutable contract the guard rejected in every addressing form."""
    from chaos_agent.agent.spec.fault_spec import FaultSpec, read_fault_spec

    spec = FaultSpec(
        scope="pod",
        namespace="default",
        labels={"app": "drill-nodedown-target"},
        names=("stale-pod",),
        source="cli_nl",
        user_description="模拟节点宕机（lazy derivation 中间态重现）",
    )
    state = AgentState(
        task_id="t-b76",
        messages=[
            AIMessage(content="", tool_calls=[{
                "name": "finish_planning", "id": "fp1", "type": "tool_call",
                "args": {
                    "summary": "node network loss",
                    "fault_scope": "node",
                    "fault_target": "network",
                    "fault_action": "loss",
                },
            }]),
            ToolMessage(
                content="Planning finalized. Summary: node network loss",
                tool_call_id="fp1", name="finish_planning",
            ),
        ],
        skill_case_content=SAMPLE_SKILL_CASE,
        fault_spec=spec.to_dict(),
    )
    result = await extract_planning_metadata(state)
    new_spec = read_fault_spec({**state, **result})
    assert new_spec is not None
    assert new_spec.scope == "node"
    assert new_spec.fault_target == "network"
    assert new_spec.fault_action == "loss"
    assert new_spec.names == ()
    assert new_spec.labels == {}


@pytest.mark.asyncio
async def test_declaration_same_scope_keeps_labels():
    """Same-scope declaration is a fill-in for the missing fields, not an
    override: labels survive."""
    from chaos_agent.agent.spec.fault_spec import FaultSpec, read_fault_spec

    spec = FaultSpec(
        scope="pod",
        namespace="default",
        labels={"app": "keep-me"},
        source="cli_nl",
        user_description="pod CPU 满载",
    )
    state = AgentState(
        task_id="t-b76b",
        messages=[
            AIMessage(content="", tool_calls=[{
                "name": "finish_planning", "id": "fp1", "type": "tool_call",
                "args": {
                    "summary": "pod cpu fullload",
                    "fault_scope": "pod",
                    "fault_target": "cpu",
                    "fault_action": "fullload",
                },
            }]),
            ToolMessage(
                content="Planning finalized. Summary: pod cpu fullload",
                tool_call_id="fp1", name="finish_planning",
            ),
        ],
        skill_case_content=SAMPLE_SKILL_CASE,
        fault_spec=spec.to_dict(),
    )
    result = await extract_planning_metadata(state)
    new_spec = read_fault_spec({**state, **result})
    assert new_spec is not None
    assert new_spec.scope == "pod"
    assert new_spec.fault_target == "cpu"
    assert new_spec.fault_action == "fullload"
    assert new_spec.labels == {"app": "keep-me"}


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
                "name": "kubectl_read", "id": "c1", "type": "tool_call",
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
            # A stale gate note from earlier in the attempt: the reject node
            # renders ``safety_reason`` first, so this edge owes it a fresh
            # value (W-56-6 in-edge matrix).
            safety_reason=(
                "Cluster-wide blast radius: execution will mutate resources "
                "beyond the target scope."
            ),
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
        # The stale blast-radius note above must be REPLACED, not left to be
        # rendered as this termination's cause.
        assert result.get("safety_reason") == "No matching use case"

    @pytest.mark.asyncio
    async def test_nudge_only_once(self):
        """Second rejection after nudge → accepted (no infinite loop)."""
        state = AgentState(
            task_id="test-task",
            _catalogue_rejection_nudged=True,
            safety_reason="No skill activated — returned to planner for activation",
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
        # Same freshness duty as above: the "No skill activated" note must be
        # replaced by this edge's own reason.
        assert result.get("safety_reason") == "Really not supported"


class TestSavedPlanHydration:
    """state["plan"] must carry the FULL saved plan — the Phase 2 system
    prompt carrier — instead of the LLM-compressed finish_planning summary,
    which may drop exact commands, vehicle names, or preconditions."""

    FULL_PLAN = (
        "## Task Summary\ncomplex multi-step\n\n"
        "## Execution Steps\n1. kubectl exec tool-pod -- blade create mem load\n\n"
        "## Verification Methods\nmulti-round sampling"
    )

    def _plan_messages(self, echo_body: bool = True) -> list:
        result_content = (
            f"Plan saved to /tmp/plan/task-1.md\n\n{self.FULL_PLAN}"
            if echo_body else "Plan saved to /tmp/plan/task-1.md"
        )
        return [
            HumanMessage(content="inject memory fault"),
            AIMessage(content="", tool_calls=[{
                "name": "save_fault_plan", "id": "sp1", "type": "tool_call",
                "args": {"task_id": "task-1", "plan_content": self.FULL_PLAN},
            }]),
            ToolMessage(content=result_content, tool_call_id="sp1",
                        name="save_fault_plan"),
            AIMessage(content="", tool_calls=[{
                "name": "finish_planning", "id": "fp1", "type": "tool_call",
                "args": {"summary": "short summary only"},
            }]),
            ToolMessage(content="Planning finalized. Summary: short summary only",
                        tool_call_id="fp1", name="finish_planning"),
        ]

    @pytest.mark.asyncio
    async def test_full_plan_beats_summary(self):
        """Normal flow (save_fault_plan then finish_planning): the full
        plan wins over the summary; is_complex/plan_path come along."""
        state = AgentState(task_id="t", skill_case_content="case",
                           messages=self._plan_messages())
        result = await extract_planning_metadata(state)
        assert result["plan"] == self.FULL_PLAN
        assert result.get("is_complex") is True
        assert result.get("plan_path") == "/tmp/plan/task-1.md"

    @pytest.mark.asyncio
    async def test_hydration_independent_of_echo(self):
        """Slim tool result (no body echo) → content still hydrates from
        the save_fault_plan tool-call args."""
        state = AgentState(task_id="t", skill_case_content="case",
                           messages=self._plan_messages(echo_body=False))
        result = await extract_planning_metadata(state)
        assert result["plan"] == self.FULL_PLAN

    @pytest.mark.asyncio
    async def test_summary_used_without_saved_plan(self):
        """Simple task (no save_fault_plan) → summary remains the plan."""
        msgs = [
            HumanMessage(content="inject cpu fault"),
            AIMessage(content="", tool_calls=[{
                "name": "finish_planning", "id": "fp1", "type": "tool_call",
                "args": {"summary": "simple one-shot plan"},
            }]),
            ToolMessage(content="Planning finalized. Summary: simple one-shot plan",
                        tool_call_id="fp1", name="finish_planning"),
        ]
        state = AgentState(task_id="t", skill_case_content="case", messages=msgs)
        result = await extract_planning_metadata(state)
        assert result["plan"] == "simple one-shot plan"

    @pytest.mark.asyncio
    async def test_existing_state_plan_not_overwritten(self):
        """Direct mode (plan already in state) → node leaves it alone."""
        state = AgentState(task_id="t", skill_case_content="case",
                           plan="direct mode plan",
                           messages=self._plan_messages())
        result = await extract_planning_metadata(state)
        assert result.get("plan") in (None, "direct mode plan")

    def test_find_saved_plan_empty_history(self):
        content, path = _find_saved_plan([])
        assert content == ""
        assert path == ""

    @pytest.mark.asyncio
    async def test_plan_summary_stored_from_finish_planning(self):
        """finish_planning's summary lands in state['plan_summary'] on the
        complex track too — state['plan'] carries the FULL plan there, so
        the summary is the confirm card / CLI prompt's only compact
        rendering of intent."""
        state = AgentState(task_id="t", skill_case_content="case",
                           messages=self._plan_messages())
        result = await extract_planning_metadata(state)
        assert result["plan"] == self.FULL_PLAN
        assert result.get("plan_summary") == "short summary only"

    @pytest.mark.asyncio
    async def test_plan_summary_not_overwriting_state(self):
        """plan_summary already in state (direct mode) → untouched."""
        state = AgentState(task_id="t", skill_case_content="case",
                           plan_summary="direct summary",
                           messages=self._plan_messages())
        result = await extract_planning_metadata(state)
        assert result.get("plan_summary") is None

    @pytest.mark.asyncio
    async def test_plan_verification_sliced_from_full_plan(self):
        """The verifier-facing slice carries Verification Methods (and
        Expected Impact when present) but never Execution Steps."""
        state = AgentState(task_id="t", skill_case_content="case",
                           messages=self._plan_messages())
        result = await extract_planning_metadata(state)
        pv = result.get("plan_verification", "")
        assert "## Verification Methods" in pv
        assert "multi-round sampling" in pv
        assert "Execution Steps" not in pv
        assert "blade create mem load" not in pv

    def test_extract_plan_verification_slices(self):
        plan = (
            "## Task Summary\ns\n\n"
            "## Verification Methods\nvm-body\n\n"
            "## Expected Impact\nei-body\n"
        )
        pv = _extract_plan_verification_slices(plan)
        assert pv.startswith("## Verification Methods")
        assert "vm-body" in pv and "ei-body" in pv
        assert "Task Summary" not in pv
        # No verification sections → empty (simple prose plan).
        assert _extract_plan_verification_slices("just a summary") == ""
        assert _extract_plan_verification_slices("") == ""


# ---------------------------------------------------------------------------
# B83/B84 (#49 post-mortem): finish_planning fault-identity declaration
#
# The planner is the only actor that knows which mechanism the plan chose.
# The declaration (fault_scope / fault_target / fault_action on
# finish_planning) hands that knowledge to the spec-resolution node.
# The skill-case document is a MENU (main path + backup means); its first
# blade command is not the chosen mechanism — #49's case doc carried a
# kubectl-native main path plus a ``node-disk burn`` backup, and the old
# AUTHORITATIVE regex hijacked the spec from pod to node.
# ---------------------------------------------------------------------------

# A #49-shaped menu: kubectl-native main path + blade backup means.
_HIJACK_STYLE_CASE = """**用例名称** Volume卸载失败 导致 Pod_Terminating

**故障现象**：
1. Pod 删除后卡在 Terminating

**演练步骤**（主路径）：
1. kubectl exec 进入目标 Pod，执行 timeout 300 tail -f 占用挂载卷文件句柄
2. kubectl delete pod 触发 Terminating

**备选手段**：
1. blade create k8s node-disk burn --names <节点名> --path <volume挂载路径> --read --write --timeout 600

**注入验证**：
1. kubectl get pod 确认 Terminating

**恢复验证**：
1. 确认句柄释放后 Pod 完成 Termination
"""

# A case with NO blade command anywhere (kubectl-native only).
_WEAK_CASE_NO_BLADE = """**故障现象**：Pod Pending

**注入验证**：
1. kubectl get pods 查看

**恢复验证**：
1. 确认 Pod Running
"""


def _make_finish_call(declared: dict | None = None, **extra_args) -> AIMessage:
    """AI message carrying a finish_planning tool call (optional identity)."""
    args: dict = {"summary": "Plan finalized", "duration_seconds": 600}
    if declared:
        args.update(declared)
    args.update(extra_args)
    return AIMessage(content="", tool_calls=[{
        "name": "finish_planning", "id": "fp1", "type": "tool_call", "args": args,
    }])


def _make_finish_tm(content: str = "Planning finalized. Summary: Plan finalized") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id="fp1", name="finish_planning")


def _cli_nl_spec(**kwargs):
    from chaos_agent.agent.spec.fault_spec import FaultSpec

    return FaultSpec(
        source="cli_nl",
        user_description="注入 Volume卸载失败导致 Pod Terminating",
        **kwargs,
    )


class TestFinishPlanningFaultIdentityDeclaration:
    """Node-level behaviour of the declaration-driven identity resolution."""

    @pytest.fixture(autouse=True)
    def _isolate_experiment_timeout(self):
        # Same isolation as TestExtractPlanningMetadataNode: the duration
        # backfill floor must not depend on the host's config.json.
        from chaos_agent.config.settings import blade_ai_context
        from chaos_agent.utils.fault_type import _DEFAULT_MIN_DURATION

        with blade_ai_context(experiment_timeout=_DEFAULT_MIN_DURATION):
            yield

    @pytest.mark.asyncio
    async def test_declaration_fills_empty_triple_cli_nl(self):
        """B83 primary path: the declaration is the identity source in CLI NL.

        The case menu carries a ``node-disk burn`` backup; the declared
        triple (pod/process — the fd-hold mechanism family) must win and
        the backup must land nowhere in the spec.
        """
        from chaos_agent.agent.spec.fault_spec import read_fault_spec

        state = AgentState(
            task_id="t-b83a",
            skill_case_content=_HIJACK_STYLE_CASE,
            fault_spec=_cli_nl_spec().to_dict(),
            messages=[
                _make_finish_call({
                    "fault_scope": "pod",
                    "fault_target": "process",
                    "fault_action": "hold",
                }),
                _make_finish_tm(),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True
        new_spec = read_fault_spec({**state, **result})
        assert new_spec is not None
        assert new_spec.scope == "pod"
        assert new_spec.fault_target == "process"
        assert new_spec.fault_action == "hold"

    @pytest.mark.asyncio
    async def test_case_doc_blade_command_no_longer_derives_identity(self):
        """B83 hijack killed: no declaration → the case-menu blade command
        must NOT be copied into the spec; the planner is nudged once to
        declare instead of the system silently inventing an identity."""
        from chaos_agent.agent.spec.fault_spec import read_fault_spec

        state = AgentState(
            task_id="t-b83b",
            skill_case_content=_HIJACK_STYLE_CASE,
            fault_spec=_cli_nl_spec().to_dict(),
            messages=[
                _make_finish_call(),
                _make_finish_tm(),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is True
        assert result.get("_identity_declaration_nudged") is True
        assert any("fault_scope" in m.content for m in result.get("messages", []))
        # The hijack shape: node/disk/burn must land nowhere.
        assert "fault_spec" not in result
        merged = read_fault_spec({**state, **{k: v for k, v in result.items() if k != "messages"}})
        assert merged is not None
        assert merged.fault_target != "disk"
        assert merged.scope != "node"

    @pytest.mark.asyncio
    async def test_plan_execution_steps_blade_command_is_derivation_source(self):
        """Fallback tier: no declaration → the blade pattern in the plan's
        Execution Steps (the LLM's actual plan, not the case menu) derives
        the identity."""
        from chaos_agent.agent.spec.fault_spec import read_fault_spec

        state = AgentState(
            task_id="t-b83c",
            skill_case_content=_WEAK_CASE_NO_BLADE,
            fault_spec=_cli_nl_spec().to_dict(),
            plan=(
                "## Task Summary\nCPU fullload drill\n\n"
                "## Execution Steps\n"
                "1. blade create k8s pod-cpu fullload --names target --timeout 600\n\n"
                "## Verification Methods\n- kubectl top pod\n"
            ),
            messages=[
                _make_finish_call(),
                _make_finish_tm(),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True
        new_spec = read_fault_spec({**state, **result})
        assert new_spec is not None
        assert new_spec.scope == "pod"
        assert new_spec.fault_target == "cpu"
        assert new_spec.fault_action == "fullload"

    @pytest.mark.asyncio
    async def test_declaration_conflict_with_reviewed_identity_is_split_nudge(self):
        """B84: the declaration never rewrites a reviewed identity — a
        conflict is routed back once as a split (align or propose_plan_change)."""
        from chaos_agent.agent.spec.fault_spec import FaultSpec, read_fault_spec

        reviewed = FaultSpec(
            scope="node",
            fault_target="disk",
            fault_action="burn",
            names=("node-1",),
            namespace="default",
            source="tui",
            user_description="node disk burn",
            duration_seconds=600,
        )
        state = AgentState(
            task_id="t-b84a",
            skill_case_content=_HIJACK_STYLE_CASE,
            fault_spec=reviewed.to_dict(),
            messages=[
                _make_finish_call({
                    "fault_scope": "pod",
                    "fault_target": "process",
                    "fault_action": "hold",
                }),
                _make_finish_tm(),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is True
        assert result.get("_identity_split_nudged") is True
        msg_text = " ".join(m.content for m in result.get("messages", []))
        assert "propose_plan_change" in msg_text
        assert "conflicts" in msg_text
        # Reviewed identity is untouched — no frozen franken-contract.
        assert "fault_spec" not in result
        merged = read_fault_spec({**state, **{k: v for k, v in result.items() if k != "messages"}})
        assert merged is not None
        assert (merged.scope, merged.fault_target, merged.fault_action) == (
            "node", "disk", "burn",
        )

    @pytest.mark.asyncio
    async def test_split_nudge_fires_once_then_proceeds(self):
        """After one nudge the reviewed identity stands; the conflicting
        declaration is discarded (the guard stays the final arbiter)."""
        from chaos_agent.agent.spec.fault_spec import FaultSpec, read_fault_spec

        reviewed = FaultSpec(
            scope="node",
            fault_target="disk",
            fault_action="burn",
            names=("node-1",),
            namespace="default",
            source="tui",
            user_description="node disk burn",
            duration_seconds=600,
        )
        state = AgentState(
            task_id="t-b84b",
            skill_case_content=_HIJACK_STYLE_CASE,
            fault_spec=reviewed.to_dict(),
            _identity_split_nudged=True,
            messages=[
                _make_finish_call({
                    "fault_scope": "pod",
                    "fault_target": "process",
                    "fault_action": "hold",
                }),
                _make_finish_tm(),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True
        merged = read_fault_spec({**state, **{k: v for k, v in result.items() if k != "messages"}})
        assert merged is not None
        assert (merged.scope, merged.fault_target, merged.fault_action) == (
            "node", "disk", "burn",
        )

    @pytest.mark.asyncio
    async def test_declaration_matching_reviewed_identity_proceeds_silently(self):
        """A declaration consistent with the reviewed identity is a no-op
        cross-check — no nudge, no rewrite."""
        from chaos_agent.agent.spec.fault_spec import FaultSpec, read_fault_spec

        reviewed = FaultSpec(
            scope="pod",
            fault_target="disk",
            fault_action="fill",
            names=("target",),
            namespace="default",
            source="cli_structured",
            user_description="pod disk fill",
            duration_seconds=600,
        )
        state = AgentState(
            task_id="t-b84c",
            skill_case_content=SAMPLE_SKILL_CASE,
            fault_spec=reviewed.to_dict(),
            messages=[
                _make_finish_call({
                    "fault_scope": "pod",
                    "fault_target": "disk",
                    "fault_action": "fill",
                }),
                _make_finish_tm(),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True
        merged = read_fault_spec({**state, **{k: v for k, v in result.items() if k != "messages"}})
        assert merged is not None
        assert (merged.scope, merged.fault_target, merged.fault_action) == (
            "pod", "disk", "fill",
        )

    @pytest.mark.asyncio
    async def test_declaration_fills_only_missing_fields(self):
        """Fill-vacuum per field: a lazy scope (with names/labels) survives
        a same-scope declaration; only the missing target/action are filled."""
        from chaos_agent.agent.spec.fault_spec import read_fault_spec

        state = AgentState(
            task_id="t-b83d",
            skill_case_content=_HIJACK_STYLE_CASE,
            fault_spec=_cli_nl_spec(
                scope="pod",
                namespace="default",
                names=("drill-sts-pvc-target",),
                labels={"app": "keep-me"},
            ).to_dict(),
            messages=[
                _make_finish_call({
                    "fault_scope": "pod",
                    "fault_target": "process",
                    "fault_action": "hold",
                }),
                _make_finish_tm(),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True
        new_spec = read_fault_spec({**state, **result})
        assert new_spec is not None
        assert new_spec.fault_target == "process"
        assert new_spec.fault_action == "hold"
        assert new_spec.scope == "pod"
        assert new_spec.names == ("drill-sts-pvc-target",)
        assert new_spec.labels == {"app": "keep-me"}

    @pytest.mark.asyncio
    async def test_declaration_nudge_fires_once_then_proceeds(self):
        """Still-unresolved identity after one declaration nudge → proceed
        without inventing one (safety_check gates an empty scope honestly)."""
        from chaos_agent.agent.spec.fault_spec import read_fault_spec

        state = AgentState(
            task_id="t-b83e",
            skill_case_content=_HIJACK_STYLE_CASE,
            fault_spec=_cli_nl_spec().to_dict(),
            _identity_declaration_nudged=True,
            messages=[
                _make_finish_call(),
                _make_finish_tm(),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True
        merged = read_fault_spec({**state, **{k: v for k, v in result.items() if k != "messages"}})
        assert merged is not None
        # Nothing was invented from the case menu.
        assert merged.fault_target == ""
        assert merged.scope == ""

    @pytest.mark.asyncio
    async def test_split_nudge_resets_plan_family_for_round_two(self):
        """F1 (cascade review of the B83/B84 fix): both identity nudges
        fire AFTER a finalised round wrote the plan family into State —
        the first nudge family with that property (the catalogue nudge
        fires on the rejection round, before any write). Round 2 must
        land its OWN plan: the round-1 plan attacked the identity the
        nudge is rejecting. The nudge result therefore resets the plan
        family so the write-once guards re-open (same seam as
        plan_change_confirm's approved branch)."""
        from chaos_agent.agent.spec.fault_spec import FaultSpec

        reviewed = FaultSpec(
            scope="node",
            fault_target="disk",
            fault_action="burn",
            names=("node-1",),
            namespace="default",
            source="tui",
            user_description="node disk burn",
            duration_seconds=600,
        )
        state = AgentState(
            task_id="t-f1a",
            skill_case_content=_HIJACK_STYLE_CASE,
            fault_spec=reviewed.to_dict(),
            plan="## Task Summary\nR1 old plan (attacked the wrong identity)",
            plan_summary="R1 old summary",
            plan_verification="## Verification Methods\nR1 old",
            messages=[
                _make_finish_call({
                    "fault_scope": "pod",
                    "fault_target": "process",
                    "fault_action": "hold",
                }),
                _make_finish_tm(),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is True
        assert result.get("_identity_split_nudged") is True
        # Plan-family reset: round 2's write-once guards must re-open.
        for key in ("plan", "plan_summary", "plan_verification",
                    "plan_path", "skill_case_content"):
            assert result.get(key, "MISSING") is None, key
        assert result.get("is_complex", "MISSING") is False

    @pytest.mark.asyncio
    async def test_declaration_nudge_resets_plan_family_for_round_two(self):
        """F1: the declaration nudge carries the same plan-family reset —
        its round 2 must also land its own plan (a re-finalised
        kubectl-native plan carrying an explicit declaration)."""
        state = AgentState(
            task_id="t-f1b",
            skill_case_content=_WEAK_CASE_NO_BLADE,
            fault_spec=_cli_nl_spec().to_dict(),
            plan="## Execution Steps\n1. kubectl delete pod target",
            plan_summary="R1 old summary",
            messages=[
                _make_finish_call(),
                _make_finish_tm(),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is True
        assert result.get("_identity_declaration_nudged") is True
        for key in ("plan", "plan_summary", "plan_verification",
                    "plan_path", "skill_case_content"):
            assert result.get(key, "MISSING") is None, key
        assert result.get("is_complex", "MISSING") is False

    @pytest.mark.asyncio
    async def test_split_nudge_round_two_lands_new_plan(self):
        """F1 end-to-end over the nudge seam: after the split nudge routes
        back and the LLM re-finalises with a matching declaration, the
        round-2 plan must REPLACE the round-1 plan in State. Without the
        reset the write-once guards keep the round-1 plan — the one that
        attacked the wrong identity — feeding Phase 2."""
        from chaos_agent.agent.spec.fault_spec import FaultSpec, read_fault_spec

        reviewed = FaultSpec(
            scope="node",
            fault_target="disk",
            fault_action="burn",
            names=("node-1",),
            namespace="default",
            source="tui",
            user_description="node disk burn",
            duration_seconds=600,
        )
        read_call = AIMessage(content="", tool_calls=[{
            "name": "read_skill_resource", "id": "rs1", "type": "tool_call",
            "args": {"resource_path": "references/catalogue/Pod_Terminating/x.md"},
        }])
        read_tm = ToolMessage(content=_HIJACK_STYLE_CASE, tool_call_id="rs1",
                              name="read_skill_resource")
        state = AgentState(
            task_id="t-f1c",
            fault_spec=reviewed.to_dict(),
            plan="## Task Summary\nR1 old plan",
            plan_summary="R1 old plan",
            plan_verification="## Verification Methods\nR1 old",
            messages=[
                read_call,
                read_tm,
                _make_finish_call({
                    "fault_scope": "pod",
                    "fault_target": "process",
                    "fault_action": "hold",
                }),
                _make_finish_tm("Planning finalized. Summary: R1 old plan"),
            ],
        )
        result1 = await extract_planning_metadata(state)
        assert result1.get("planning_rejected") is True

        # LangGraph merge of the nudged round + round-2 re-entry: the LLM
        # re-finalises with a declaration matching the reviewed identity.
        merged_state = {
            **state,
            **{k: v for k, v in result1.items() if k != "messages"},
            "messages": [
                *state["messages"],
                *result1["messages"],
                _make_finish_call({
                    "fault_scope": "node",
                    "fault_target": "disk",
                    "fault_action": "burn",
                }),
                _make_finish_tm("Planning finalized. Summary: R2 corrected plan"),
            ],
        }
        result2 = await extract_planning_metadata(merged_state)
        assert result2.get("planning_rejected") is not True
        assert result2.get("plan", "MISSING") == "R2 corrected plan"
        assert result2.get("plan_summary", "MISSING") == "R2 corrected plan"
        merged_spec = read_fault_spec({**merged_state, **{
            k: v for k, v in result2.items() if k != "messages"
        }})
        assert merged_spec is not None
        assert (merged_spec.scope, merged_spec.fault_target,
                merged_spec.fault_action) == ("node", "disk", "burn")

    @pytest.mark.asyncio
    async def test_no_nudge_round_keeps_write_once_semantics(self):
        """Guard rail: the plan-family reset is nudge-only — a plain
        re-entry with a matching declaration keeps write-once semantics
        (an already-written plan is NOT cleared or rewritten)."""
        from chaos_agent.agent.spec.fault_spec import FaultSpec

        reviewed = FaultSpec(
            scope="pod",
            fault_target="disk",
            fault_action="fill",
            names=("target",),
            namespace="default",
            source="cli_structured",
            user_description="pod disk fill",
            duration_seconds=600,
        )
        state = AgentState(
            task_id="t-f1d",
            skill_case_content=SAMPLE_SKILL_CASE,
            fault_spec=reviewed.to_dict(),
            plan="## Task Summary\nsettled plan",
            plan_summary="settled",
            plan_verification="## Verification Methods\nsettled",
            messages=[
                _make_finish_call({
                    "fault_scope": "pod",
                    "fault_target": "disk",
                    "fault_action": "fill",
                }),
                _make_finish_tm(),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True
        assert "plan" not in result
        assert "plan_summary" not in result
        assert "plan_verification" not in result


class TestExtractPlanningFaultIdentityHelper:
    """Unit behaviour of the declaration reader."""

    def test_reads_last_finish_planning_declaration(self):
        from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
            _extract_planning_fault_identity,
        )

        msgs = [
            _make_finish_call({"fault_scope": "node", "fault_target": "disk",
                               "fault_action": "burn"}),
            _make_finish_call({"fault_scope": "pod", "fault_target": "process",
                               "fault_action": "hold"}),
        ]
        assert _extract_planning_fault_identity(msgs) == ("pod", "process", "hold")

    def test_absent_declaration_returns_empty(self):
        from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
            _extract_planning_fault_identity,
        )

        assert _extract_planning_fault_identity([]) == ("", "", "")
        assert _extract_planning_fault_identity(
            [_make_finish_call()]
        ) == ("", "", "")

    def test_normalises_whitespace_and_case(self):
        from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
            _extract_planning_fault_identity,
        )

        msgs = [_make_finish_call({
            "fault_scope": " Pod ",
            "fault_target": "Process",
            "fault_action": "Hold",
        })]
        assert _extract_planning_fault_identity(msgs) == ("pod", "process", "hold")

    def test_ignores_save_fault_plan_carrier(self):
        """The declaration carrier is finish_planning only — a saved draft
        is not a final declaration."""
        from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
            _extract_planning_fault_identity,
        )

        msgs = [AIMessage(content="", tool_calls=[{
            "name": "save_fault_plan", "id": "sp1", "type": "tool_call",
            "args": {
                "task_id": "t", "plan_content": "# plan",
                "fault_scope": "pod", "fault_target": "process",
                "fault_action": "hold",
            },
        }])]
        assert _extract_planning_fault_identity(msgs) == ("", "", "")


class TestDurationBackfillPrecedence:
    """Pin the precedence at the B14 duration backfill site.

    A hard-pinned contract value (CLI ``--duration``) wins; the planner's
    ``finish_planning`` declaration only FILLS an empty contract. The former
    ``max(declared_duration, spec.duration_seconds)`` lifted a pinned 60 to a
    declared 300 — amending a contract-stated duration, the one thing
    ``ensure_min_duration`` forbids in either direction
    (l4-contract-faithfulness) — which silently widened the approved exposure
    window and pushed the recovery timer riding on ``duration_seconds`` out
    with it.
    """

    @pytest.fixture(autouse=True)
    def _isolate_experiment_timeout(self):
        from chaos_agent.config.settings import blade_ai_context
        from chaos_agent.utils.fault_type import _DEFAULT_MIN_DURATION

        with blade_ai_context(experiment_timeout=_DEFAULT_MIN_DURATION):
            yield

    @staticmethod
    async def _resolve(pinned: int, declared: int) -> int:
        """Run the node and return the duration it lands on."""
        from chaos_agent.agent.spec.fault_spec import read_fault_spec

        state = AgentState(
            task_id="t-dur-prec",
            skill_case_content=_WEAK_CASE_NO_BLADE,
            fault_spec=_cli_nl_spec(
                duration_seconds=pinned,
                scope="pod",
                fault_target="cpu",
                fault_action="fullload",
            ).to_dict(),
            messages=[
                _make_finish_call(
                    {
                        "fault_scope": "pod",
                        "fault_target": "cpu",
                        "fault_action": "fullload",
                    },
                    duration_seconds=declared,
                ),
                _make_finish_tm(),
            ],
        )
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True
        spec = read_fault_spec({**state, **result})
        assert spec is not None
        return spec.duration_seconds

    @pytest.mark.asyncio
    async def test_unpinned_spec_takes_declared_value(self):
        """B14 primary purpose: an empty contract adopts the declaration."""
        assert await self._resolve(pinned=0, declared=300) == 300

    @pytest.mark.asyncio
    async def test_declared_shorter_than_pinned_never_lowers_it(self):
        """A pinned value is never shrunk by a shorter declaration."""
        assert await self._resolve(pinned=600, declared=300) == 600

    @pytest.mark.asyncio
    async def test_pinned_value_wins_over_longer_declaration(self):
        """A pinned 60 stays 60 even when the planner declares 300.

        The regression this guards: ``max(declared, pinned)`` used to lift
        the pinned value, silently widening the approved exposure window
        from 60s to 300s and deferring the recovery timer with it.
        """
        assert await self._resolve(pinned=60, declared=300) == 60

    @pytest.mark.asyncio
    async def test_pinned_below_floor_not_raised_by_backfill(self):
        """Neither a declaration nor the empirical floor lifts a pin."""
        assert await self._resolve(pinned=60, declared=0) == 60


# ---------------------------------------------------------------------------
# Guard 1c (#59): plan-prescribed banned verbs are caught at PLANNING time
# ---------------------------------------------------------------------------


class TestPlanBannedVerbGuard:
    """Case #59 (Pod_被驱逐重建_DiskPressure): the case doc prescribed a
    timer payload built from ``rm`` and a recovery through ``systemctl`` —
    both banned verbs. The execute-phase carrier gate rejected them
    word-level, but only AFTER confirmation, burning ~15 minutes of
    mid-execution rewrites. Guard 1c runs the SAME static legislation over
    the plan's host-command lines before confirmation, so the rewrite
    happens while the plan is still open.
    """

    # The #59 shape: a disk fill whose timer payload used ``rm`` — the
    # exact prescription the guard would reject in execute.
    PLAN_WITH_RM_PAYLOAD = (
        "## Execution Steps\n"
        "1. Create debug pod and fill node disk:\n"
        "```\n"
        "kubectl exec node-debugger-x -- chroot /host sh -c "
        "'dd if=/dev/zero of=/var/tmp/fill.bin bs=1M count=4096 && "
        "systemd-run --on-active=600s --unit=drill-reclaim-x "
        "rm -f /var/tmp/fill.bin'\n"
        "```\n"
    )

    # The compliant #59 rewrite: timer payload is the family inverse.
    PLAN_WITH_TRUNCATE_PAYLOAD = (
        "## Execution Steps\n"
        "1. Create debug pod and fill node disk:\n"
        "```\n"
        "kubectl exec node-debugger-x -- chroot /host sh -c "
        "'dd if=/dev/zero of=/var/tmp/fill.bin bs=1M count=4096 && "
        "systemd-run --on-active=600s --unit=drill-reclaim-x "
        "truncate -s 0 /var/tmp/fill.bin'\n"
        "```\n"
    )

    @staticmethod
    def _state_with_plan(plan: str, **overrides):
        return AgentState(
            task_id="t-g1c",
            skill_case_content=SAMPLE_SKILL_CASE,
            plan=plan,
            messages=[
                AIMessage(content="", tool_calls=[{
                    "name": "finish_planning", "id": "fp1", "type": "tool_call",
                    "args": {"summary": "node disk fill"},
                }]),
                ToolMessage(
                    content="Planning finalized. Summary: node disk fill",
                    tool_call_id="fp1", name="finish_planning",
                ),
            ],
            **overrides,
        )

    @pytest.mark.asyncio
    async def test_rm_payload_plan_is_rejected_with_rewrite_nudge(self):
        from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
            _plan_banned_verb_findings,
        )

        state = self._state_with_plan(self.PLAN_WITH_RM_PAYLOAD)
        # The scan itself: the fenced chroot /host line carries 'rm'.
        findings = _plan_banned_verb_findings(self.PLAN_WITH_RM_PAYLOAD)
        assert findings, "rm payload line must be flagged"
        assert "rm" in findings[0][1]

        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is True
        assert result.get("_plan_banned_verb_nudged") is True
        # The plan family re-opens so round 2 can land its OWN plan
        # (finish_planning's writes are write-once).
        assert result.get("plan") is None
        assert result.get("plan_summary") is None
        assert result.get("skill_case_content") is None
        # The nudge names the verb and carries the compliant-form guidance.
        nudge = str(result["messages"][0].content)
        assert "PLAN/GUARD INCOMPATIBILITY" in nudge
        assert "'rm'" in nudge
        assert "truncate" in nudge
        # Case-doc context: the cited line is the offending command.
        assert "rm -f" in nudge

    @pytest.mark.asyncio
    async def test_systemctl_recovery_step_is_flagged_too(self):
        from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
            _plan_banned_verb_findings,
        )

        plan = (
            "## Execution Steps\n"
            "2. Recover the timer:\n"
            "```\n"
            "kubectl exec node-debugger-x -- chroot /host systemctl "
            "stop drill-reclaim-x\n"
            "```\n"
        )
        findings = _plan_banned_verb_findings(plan)
        assert findings
        assert "systemctl" in findings[0][1]

        state = self._state_with_plan(plan)
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is True
        nudge = str(result["messages"][0].content)
        assert "'systemctl'" in nudge

    @pytest.mark.asyncio
    async def test_compliant_truncate_payload_plan_passes(self):
        state = self._state_with_plan(self.PLAN_WITH_TRUNCATE_PAYLOAD)
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True
        assert "_plan_banned_verb_nudged" not in result
        assert "messages" not in result or not [
            m for m in result.get("messages", [])
            if "PLAN/GUARD INCOMPATIBILITY" in str(getattr(m, "content", ""))
        ]

    @pytest.mark.asyncio
    async def test_nudge_fires_only_once(self):
        # After the nudge the plan proceeds: the execute guard remains the
        # final arbiter, and a model that insists still faces it there.
        state = self._state_with_plan(
            self.PLAN_WITH_RM_PAYLOAD, _plan_banned_verb_nudged=True,
        )
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True
        assert "messages" not in result or not [
            m for m in result.get("messages", [])
            if "PLAN/GUARD INCOMPATIBILITY" in str(getattr(m, "content", ""))
        ]

    @pytest.mark.asyncio
    async def test_readonly_probe_with_argument_position_verb_is_exempt(self):
        # The readonly face is honoured exactly as the execute gate honours
        # it: ``systemctl`` in a genuine probe's ARGUMENT position
        # (``which systemctl``) passes the readonly judge, so the word-level
        # verb hit is exempted. The BINARY position stays banned on host
        # entry (``systemctl status`` included) — checked next.
        plan = (
            "## Execution Steps\n"
            "3. Probe for the timer binary:\n"
            "```\n"
            "kubectl exec node-debugger-x -- chroot /host sh -c "
            "'which systemctl'\n"
            "```\n"
        )
        state = self._state_with_plan(plan)
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True

    @pytest.mark.asyncio
    async def test_systemctl_binary_position_stays_banned_even_readonly(self):
        # Host entry bans the systemctl BINARY at word level — benign and
        # hostile forms are indistinguishable there, so even ``systemctl
        # is-active`` (a guarded read-only form) is flagged. This is the
        # execute gate's own rule (the probe face's overlay); Guard 1c
        # must not be wider than it.
        plan = (
            "## Execution Steps\n"
            "3. Probe the timer:\n"
            "```\n"
            "kubectl exec node-debugger-x -- chroot /host systemctl "
            "is-active drill-reclaim-x\n"
            "```\n"
        )
        from chaos_agent.agent.target_guard.carriers import (
            find_banned_host_verbs,
            is_readonly_host_probe,
        )
        from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
            _plan_banned_verb_findings,
        )

        segment = plan.split("```\n")[1].strip()
        assert find_banned_host_verbs(segment) == ("systemctl",)
        assert is_readonly_host_probe(segment) is False
        assert _plan_banned_verb_findings(plan)

        state = self._state_with_plan(plan)
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is True

    @pytest.mark.asyncio
    async def test_prose_mention_outside_a_fence_is_exempt(self):
        # Prose ("never use rm to clean up") is not a prescription; only
        # fenced host-entering command lines are judged.
        plan = (
            "## Execution Steps\n"
            "1. Fill the node disk via dd. (Do NOT use rm; prefer truncate.)\n"
        )
        state = self._state_with_plan(plan)
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True

    @pytest.mark.asyncio
    async def test_unfenced_command_line_is_exempt(self):
        # Same rule from the other side: a bare (unfenced) line mentioning
        # chroot /host is prose-shaped to this guard — the execute gate
        # still judges the real exec when it is issued.
        plan = (
            "## Execution Steps\n"
            "1. Run `chroot /host rm -f /var/tmp/fill.bin` on the node.\n"
        )
        from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
            _plan_banned_verb_findings,
        )

        assert _plan_banned_verb_findings(plan) == []
        state = self._state_with_plan(plan)
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True

    @pytest.mark.asyncio
    async def test_nsenter_entry_form_is_flagged(self):
        # The entry vocabulary is the execute gate's, not just the
        # ``chroot /host`` spelling: a plan spelled ``nsenter -t 1 ...``
        # with a banned verb sailed through the chroot-only scan and
        # rediscovered the ban mid-execution — #59's cost pattern through
        # a side door (adversarial self-review of the #59 fix).
        plan = (
            "## Execution Steps\n"
            "2. Stop the timer:\n"
            "```\n"
            "kubectl exec node-debugger-x -- nsenter -t 1 -m -- "
            "systemctl stop drill-reclaim-x\n"
            "```\n"
        )
        from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
            _plan_banned_verb_findings,
        )

        findings = _plan_banned_verb_findings(plan)
        assert findings
        assert "systemctl" in findings[0][1]

        state = self._state_with_plan(plan)
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is True

    @pytest.mark.asyncio
    async def test_host_binary_absolute_form_is_flagged(self):
        # The ``/host/<binary>`` absolute form: the banned verb rides the
        # binary path itself — the execute gate's word-level face matches
        # it (the leading ``/`` is in the verb regex's boundary class), so
        # the plan pre-check must see it too.
        plan = (
            "## Execution Steps\n"
            "2. Check the timer:\n"
            "```\n"
            "kubectl exec node-debugger-x -- /host/systemctl "
            "is-active drill-reclaim-x\n"
            "```\n"
        )
        from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
            _plan_banned_verb_findings,
        )

        findings = _plan_banned_verb_findings(plan)
        assert findings
        assert "systemctl" in findings[0][1]

    @pytest.mark.asyncio
    async def test_nsenter_argument_position_verb_is_exempt(self):
        # Parity with the readonly face behind the widened entry forms:
        # ``which systemctl`` behind nsenter is a genuine probe's ARGUMENT
        # position — exempt, the same way it is behind chroot (the readonly
        # judge handles the nsenter wrapper natively; verified by probe).
        plan = (
            "## Execution Steps\n"
            "3. Probe for the binary:\n"
            "```\n"
            "kubectl exec node-debugger-x -- nsenter -t 1 -m -- "
            "which systemctl\n"
            "```\n"
        )
        state = self._state_with_plan(plan)
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True

    @pytest.mark.asyncio
    async def test_host_entry_line_without_banned_verb_is_not_flagged(self):
        # The widened entry match must not become a blanket flag on every
        # host command: a probe with no banned verb (df behind chroot,
        # df behind nsenter) produces no finding.
        plan = (
            "## Execution Steps\n"
            "3. Read host disk usage:\n"
            "```\n"
            "kubectl exec node-debugger-x -- chroot /host df -h /var\n"
            "kubectl exec node-debugger-x -- nsenter -t 1 -m -- df -h /var\n"
            "```\n"
        )
        from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
            _plan_banned_verb_findings,
        )

        assert _plan_banned_verb_findings(plan) == []
        state = self._state_with_plan(plan)
        result = await extract_planning_metadata(state)
        assert result.get("planning_rejected") is not True
