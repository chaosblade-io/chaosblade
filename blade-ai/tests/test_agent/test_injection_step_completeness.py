"""Tests for the multi-step injection step self-check (high-tolerance, soft)."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes.execute._injection_detection import (
    _extract_drill_steps,
    _injection_intent_steps,
    _required_kubectl_verbs,
    build_injection_step_selfcheck,
)
from chaos_agent.agent.nodes.execute.execute_loop import _detect_terminal_conclusion
from chaos_agent.agent.providers import FaultProviderRegistry


# --- Skill case fixtures ---

# k8s multi-step: steps mention cordon / taint / delete (via 添加污点 / 删除).
DAEMONSET_SKILL_CASE = """**演练步骤**：
1. 选取一个运行 DaemonSet Pod 的节点
2. 使用 kubectl 将该节点标记为不可调度：`kubectl cordon <node>`
3. 给该节点添加污点：`kubectl taint nodes <node> chaos=true:NoSchedule`
4. 删除该节点上的 DaemonSet Pod，触发重建

**注入验证**：
1. 执行 `kubectl get nodes`
"""

# host multi-step: step mentions the `iptables` binary.
HOST_MULTISTEP_SKILL_CASE = """**演练步骤**：
1. 记录当前连接基线：`ss -s`
2. 使用 iptables 丢弃目标端口入向流量
3. 武装定时恢复

**注入验证**：
1. 确认连接超时
"""

SINGLE_STEP_SKILL_CASE = """**演练步骤**：
1. 使用 chaosblade 注入 CPU 满载

**注入验证**：
1. 查看 CPU 使用率
"""

# Real-world case (task-8b87d1e2): the step spells out `kubectl label`, but the
# agent achieves it with `kubectl patch node ... metadata.labels`.
LABEL_SKILL_CASE = """**演练步骤**：
1. 记录目标节点的当前 taint 信息
2. 给目标节点添加标签：`kubectl label node <node> chaos-target=<app-name>`
3. 给目标节点添加污点：`kubectl taint node <node> chaos-test=true:NoSchedule`

**注入验证**：
1. 观察新 Pod 的调度状态
"""


def _kubectl_msgs(pairs):
    """pairs: list of (subcommand, content) or (subcommand, content, v_args).

    Builds AIMessage+ToolMessage pairs; ``v_args`` matters for the ``patch``
    semantic-equivalence credit.
    """
    msgs = []
    for i, pair in enumerate(pairs):
        sub, content = pair[0], pair[1]
        v_args = pair[2] if len(pair) > 2 else "..."
        tc_id = f"call_{i}"
        msgs.append(AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": tc_id, "type": "tool_call",
            "args": {"subcommand": sub, "v_args": v_args},
        }]))
        msgs.append(ToolMessage(content=content, name="kubectl", tool_call_id=tc_id))
    return msgs


def _host_msgs(pairs):
    """pairs: list of (command, content). Build host_inject AIMessage+ToolMessage."""
    msgs = []
    for i, (cmd, content) in enumerate(pairs):
        tc_id = f"h_{i}"
        msgs.append(AIMessage(content="", tool_calls=[{
            "name": "host_inject", "id": tc_id, "type": "tool_call",
            "args": {"command": cmd},
        }]))
        msgs.append(ToolMessage(content=content, name="host_inject", tool_call_id=tc_id))
    return msgs


class TestExtractDrillSteps:
    def test_daemonset_has_4_steps(self):
        assert len(_extract_drill_steps(DAEMONSET_SKILL_CASE)) == 4

    def test_no_drill_section(self):
        assert _extract_drill_steps("no steps here") == []


class TestChineseVerbVocabulary:
    """The Chinese step phrases must be specific to the ACTION, not the effect.

    Unlike the English verbs (word-boundary matched), a Chinese phrase is
    matched as a SUBSTRING of the whole step, and ``_injection_intent_steps``
    only drops steps that START with an observation prefix. So a phrase that
    also appears while describing an OUTCOME yields a REQUIRED verb the case
    never asked for — and the self-check then nudges the model to perform it.
    That direction is harmful, unlike the harmless under-reporting this
    vocabulary otherwise biases toward, so the negative half below is as
    load-bearing as the positive half.
    """

    @staticmethod
    def _required(step: str) -> set[str]:
        return set(_required_kubectl_verbs(_injection_intent_steps([step])))

    @pytest.mark.parametrize("step,verb", [
        ("驱逐节点上的所有 Pod：`kubectl drain <node> --ignore-daemonsets`", "drain"),
        ("驱逐节点 <node> 上的业务 Pod", "drain"),
        ("排空节点，模拟节点维护场景", "drain"),
        ("给目标节点打标签 `chaos-target=<app>`", "label"),
        ("添加标签 ops-maintenance=scheduled 到节点", "label"),
        ("移除标签 ops-maintenance", "label"),
        ("添加注解 chaos.io/drill=true", "annotate"),
    ])
    def test_action_phrase_recognised(self, step, verb):
        assert verb in self._required(step), step

    @pytest.mark.parametrize("step,forbidden", [
        # "驱逐" alone is the everyday word for an Evicted pod, so pressure
        # cases state it as an expected EFFECT mid-sentence. Mapping the bare
        # word to ``drain`` would tell the model to drain the node.
        ("注入内存压力后，Pod 可能被驱逐或 OOMKilled", "drain"),
        ("节点出现 MemoryPressure，kubelet 会驱逐低优先级 Pod", "drain"),
        ("Pod 被驱逐后，检查是否在其它节点重建", "drain"),
        # Label SELECTORS appear in almost every step; only an attached verb
        # ("打标签" / "添加标签") means the step writes a label.
        ("使用标签选择器 -l app=<name> 定位目标 Pod", "label"),
        ("确认标签匹配的 Pod 数量", "label"),
    ])
    def test_effect_or_selector_phrase_not_required(self, step, forbidden):
        assert forbidden not in self._required(step), step

    @pytest.mark.parametrize("step,verb", [
        ("删除 Pod：`kubectl delete pod x`", "delete"),
        ("缩容 Deployment 到 0", "scale"),
        ("添加污点 key=v:NoSchedule", "taint"),
        ("标记为不可调度", "cordon"),
    ])
    def test_existing_phrases_unchanged(self, step, verb):
        assert verb in self._required(step), step

    def test_every_mapped_verb_is_a_step_verb(self):
        """A phrase mapping to a verb outside ``step_kubectl_verbs`` is dead
        weight: the self-check only ever reports verbs from that set."""
        from chaos_agent.agent.providers.k8s_native import K8sNativeProvider

        mapped = set(K8sNativeProvider.chinese_verb_map.values())
        assert mapped <= set(K8sNativeProvider.step_kubectl_verbs)


class TestBuildInjectionStepSelfcheckK8s:
    def test_nothing_executed_flags_missing(self):
        msg = build_injection_step_selfcheck(DAEMONSET_SKILL_CASE, [], "kubectl_native")
        assert msg is not None
        assert "self-check" in msg.lower()
        # missing verbs surfaced
        assert "cordon" in msg and "taint" in msg and "delete" in msg
        # soft, not the old hard wording
        assert "MUST" not in msg
        assert "INCOMPLETE" not in msg
        assert "Reconsider" in msg

    def test_all_executed_returns_none(self):
        msgs = _kubectl_msgs([
            ("cordon", "node/x cordoned"),
            ("taint", "node/x tainted"),
            ("delete", "pod deleted"),
        ])
        assert build_injection_step_selfcheck(
            DAEMONSET_SKILL_CASE, msgs, "kubectl_native") is None

    def test_timed_out_mutation_counts_as_executed(self):
        """High tolerance: a timed-out delete reached the cluster → NOT missing.

        This is the original false-INCOMPLETE bug (task-57f2a15c)."""
        msgs = _kubectl_msgs([
            ("cordon", "node/x cordoned"),
            ("taint", "node/x tainted"),
            ("delete", "Error: kubectl delete (exit 1): Error: task timed out after 30s"),
        ])
        assert build_injection_step_selfcheck(
            DAEMONSET_SKILL_CASE, msgs, "kubectl_native") is None

    def test_guard_rejected_verb_stays_missing(self):
        """A pre-execution guard reject means the command never ran → still missing."""
        msgs = _kubectl_msgs([
            ("cordon", "node/x cordoned"),
            ("taint", "node/x tainted"),
            ("delete", "[target_guard] REJECT_BANNED: not allowed"),
        ])
        msg = build_injection_step_selfcheck(
            DAEMONSET_SKILL_CASE, msgs, "kubectl_native")
        assert msg is not None
        assert "delete" in msg

    def test_single_step_returns_none(self):
        assert build_injection_step_selfcheck(
            SINGLE_STEP_SKILL_CASE, [], "kubectl_native") is None


class TestPatchSemanticEquivalence:
    """``kubectl patch`` credits the dedicated verb it is equivalent to.

    Regression for task-8b87d1e2: the step said ``kubectl label node ...`` but
    the agent used ``kubectl patch node -p '{"metadata":{"labels":...}}'``, so
    the verb-name comparison wrongly reported ``label`` as un-performed.
    """

    def test_patch_labels_credits_label(self):
        msgs = _kubectl_msgs([
            ("patch", "node/x patched",
             '''node x -p '{"metadata":{"labels":{"chaos-target":"ksm"}}}' '''),
            ("taint", "node/x tainted"),
        ])
        assert build_injection_step_selfcheck(
            LABEL_SKILL_CASE, msgs, "kubectl_native") is None

    def test_patch_without_labels_keeps_label_missing(self):
        """No blanket credit: a patch touching something else leaves it missing."""
        msgs = _kubectl_msgs([
            ("patch", "deployment patched",
             '''deployment d -p '{"spec":{"template":{"spec":{"nodeSelector":{}}}}}' '''),
            ("taint", "node/x tainted"),
        ])
        msg = build_injection_step_selfcheck(
            LABEL_SKILL_CASE, msgs, "kubectl_native")
        assert msg is not None
        assert "label" in msg

    def test_patch_taints_credits_taint(self):
        msgs = _kubectl_msgs([
            ("patch", "node/x patched",
             '''node x -p '{"metadata":{"labels":{"a":"b"}}}' '''),
            ("patch", "node/x patched",
             '''node x -p '{"spec":{"taints":[{"key":"chaos-test"}]}}' '''),
        ])
        assert build_injection_step_selfcheck(
            LABEL_SKILL_CASE, msgs, "kubectl_native") is None

    def test_patch_replicas_credits_scale(self):
        from chaos_agent.agent.nodes.execute._injection_detection import (
            _patch_equivalent_verbs,
        )

        assert _patch_equivalent_verbs(
            '''deploy d -p '{"spec":{"replicas":0}}' ''') == {"scale"}
        assert _patch_equivalent_verbs(
            '''node x -p '{"spec":{"unschedulable":true}}' ''') == {"cordon", "uncordon"}
        assert _patch_equivalent_verbs("deploy d -p '{\"spec\":{}}'") == set()

    def test_dedicated_verb_credits_patch_reverse(self):
        """Mirror direction: a step spelled ``kubectl patch`` is satisfied when the
        agent used the dedicated verb, since each such verb IS a field patch."""
        case = """**演练步骤**：
1. 使用 kubectl patch 为节点添加标签
2. 给节点添加污点：`kubectl taint node <n> k=v:NoSchedule`

**注入验证**：
1. 观察调度状态
"""
        msgs = _kubectl_msgs([
            ("label", "node/x labeled", "node x chaos-target=ksm"),
            ("taint", "node/x tainted", "node x k=v:NoSchedule"),
        ])
        assert build_injection_step_selfcheck(case, msgs, "kubectl_native") is None


class TestReadonlyStepsExcluded:
    """Baseline / observation steps must not become REQUIRED injection actions.

    Host-side analogue of the label-vs-patch false positive: the host vocabulary
    is binary names that read and write under the same name, so a read-only
    ``systemctl status`` baseline step used to demand a ``systemctl`` injection.
    """

    def test_host_readonly_baseline_not_required(self):
        case = """**演练步骤**：
1. 确认目标服务当前状态：`systemctl status nginx`
2. 杀死目标进程：`kill -9 <pid>`
3. 观察服务是否自动重启

**注入验证**：
1. 确认进程已消失
"""
        msgs = _host_msgs([("kill -9 1234", "ok")])
        # Only `kill` is a real injection action; `systemctl status` is baseline.
        assert build_injection_step_selfcheck(case, msgs, "host_native") is None

    def test_host_write_step_still_required(self):
        """Filtering must not swallow a genuine injection step."""
        case = """**演练步骤**：
1. 确认目标服务当前状态：`systemctl status nginx`
2. 停止服务：`systemctl stop nginx`
3. 观察影响

**注入验证**：
1. 确认服务已停
"""
        msg = build_injection_step_selfcheck(case, [], "host_native")
        assert msg is not None
        assert "systemctl" in msg

    def test_readonly_filter_never_grows_steps(self):
        from chaos_agent.agent.nodes.execute._injection_detection import (
            _injection_intent_steps,
        )

        steps = ["记录当前状态", "杀死进程", "观察恢复"]
        out = _injection_intent_steps(steps)
        assert out == ["杀死进程"]
        assert len(out) <= len(steps)

    def test_action_step_mentioning_observation_is_kept(self):
        """Only the LEADING intent decides: an action step that also says 观察 must
        keep contributing its verb, or a genuinely skipped delete goes unnoticed.

        Regression for DaemonSet_未完全调度_节点不可调度.md, whose injection step
        reads "删除该节点上的 DaemonSet Pod，观察 Pod 是否被重建".
        """
        from chaos_agent.agent.nodes.execute._injection_detection import (
            _injection_intent_steps,
            _required_kubectl_verbs,
        )

        steps = [
            "删除该节点上的 DaemonSet Pod，观察 Pod 是否被重建",
            "观察 DaemonSet 副本数变化",
        ]
        kept = _injection_intent_steps(steps)
        assert kept == [steps[0]]
        assert "delete" in _required_kubectl_verbs(kept)


class TestBuildInjectionStepSelfcheckHost:
    def test_host_nothing_executed_flags_missing(self):
        msg = build_injection_step_selfcheck(HOST_MULTISTEP_SKILL_CASE, [], "host_native")
        assert msg is not None
        assert "iptables" in msg

    def test_host_binary_executed_returns_none(self):
        msgs = _host_msgs([("iptables -A INPUT -p tcp --dport 80 -j DROP", "ok")])
        assert build_injection_step_selfcheck(
            HOST_MULTISTEP_SKILL_CASE, msgs, "host_native") is None


class TestDetectTerminalConclusionWiring:
    @pytest.fixture(autouse=True)
    def _register(self):
        FaultProviderRegistry.register_builtins()

    def _text(self):
        return AIMessage(content="Injection complete.", tool_calls=[])

    def _last_human(self, result):
        for m in result.get("messages", []):
            if isinstance(m, HumanMessage):
                return m.content
        return ""

    def test_kubectl_native_missing_emits_selfcheck(self):
        state = {
            "injection_method": "kubectl_native",
            "skill_case_content": DAEMONSET_SKILL_CASE,
            "messages": [],  # nothing executed → missing
        }
        result: dict = {}
        _detect_terminal_conclusion(self._text(), state, result)
        assert result.get("_injection_selfcheck_nudged") is True
        assert result.get("injection_method") is None
        assert "self-check" in self._last_human(result).lower()

    def test_kubectl_native_complete_no_selfcheck(self):
        """All required verbs attempted → no missing → clean exit (no nudge)."""
        state = {
            "injection_method": "kubectl_native",
            "skill_case_content": DAEMONSET_SKILL_CASE,
            "messages": _kubectl_msgs([
                ("cordon", "ok"), ("taint", "ok"), ("delete", "ok"),
            ]),
        }
        result: dict = {}
        _detect_terminal_conclusion(self._text(), state, result)
        assert "_injection_selfcheck_nudged" not in result
        assert not result.get("messages")

    def test_host_native_missing_emits_selfcheck(self):
        state = {
            "injection_method": "host_native",
            "skill_case_content": HOST_MULTISTEP_SKILL_CASE,
            "messages": [],
        }
        result: dict = {}
        _detect_terminal_conclusion(self._text(), state, result)
        assert result.get("_injection_selfcheck_nudged") is True

    def test_blade_uid_exits_without_selfcheck(self):
        state = {
            "blade_uid": "abc123",
            "injection_method": "kubectl_native",
            "skill_case_content": DAEMONSET_SKILL_CASE,
            "messages": [],
        }
        result: dict = {}
        _detect_terminal_conclusion(self._text(), state, result)
        assert "_injection_selfcheck_nudged" not in result
        assert not result.get("messages")

    def test_selfcheck_is_one_shot(self):
        state = {
            "injection_method": "kubectl_native",
            "skill_case_content": DAEMONSET_SKILL_CASE,
            "messages": [],
            "_injection_selfcheck_nudged": True,
        }
        result: dict = {}
        _detect_terminal_conclusion(self._text(), state, result)
        assert "_injection_selfcheck_nudged" not in result
