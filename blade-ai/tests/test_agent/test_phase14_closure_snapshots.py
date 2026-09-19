"""Phase-14 closure baseline snapshots (tasks 1.1-1.4).

钉扎**改前**行为基线，覆盖四条 Phase-14 改动将触碰的判定路径：

1. 横向判定路径——issue_time_method 的 kubectl-exec 内嵌 blade 归因、
   verify 侧 was_blade_create_attempted 的 native-takeover 判定、
   k8s_native classifier 对 inline blade 命令的递归委托；
2. hydration 消费点的新键语义（G4 EOL 后：新键 record 直通 / 旧键
   record 经原消费点读取为空——旧键视作不存在）；
3. handle kind 协议值现状（blade_uid）与 compactor 渲染去重行为；
4. compactor 的 UID 提取双键正则（新键命中 / 旧键命中）。

标注约定：
- ``[行为等价]``：Phase-14 改动后快照必须逐值相等（词汇/地址只换不改行为）；
- ``[改后翻转]``：快照钉的是 EOL 前的现状，对应任务组完成后断言按
  tasks.md 标注翻转（届时同步改写本文件断言与 fixture，翻转本身即验收）。
"""

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.providers.chaosblade.provider import (
    ChaosbladeProvider,
    classify_inline_blade,
)
from chaos_agent.agent.providers.chaosblade.python_provider import (
    ChaosbladePythonProvider,
)
from chaos_agent.agent.providers.chaosblade.verify import (
    was_blade_create_attempted,
)
from chaos_agent.agent.providers.k8s_native.provider import K8sNativeProvider
from chaos_agent.agent.state import build_status_data
from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.agent.target_guard.freeze import approved_from_dict
from chaos_agent.agent.target_guard.types import SCOPE_UNKNOWN
from chaos_agent.memory.compactor import (
    build_post_compact_context_message,
    extract_critical_context,
)


class _Msg:
    """Minimal message stub — extract_critical_context only reads .content."""

    def __init__(self, content: str) -> None:
        self.content = content


def _ai_kubectl_call(subcommand: str, v_args: str, tc_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "kubectl",
                "args": {"subcommand": subcommand, "v_args": v_args},
                "id": tc_id,
                "type": "tool_call",
            }
        ],
    )


# ---------------------------------------------------------------------------
# 1.1 横向判定路径：issue_time_method（kubectl-exec 内嵌 blade 归因）
# ---------------------------------------------------------------------------


class TestIssueTimeMethodSnapshot:
    """[行为等价] G2 改道中立词汇后，归因结果逐值相等。"""

    def test_direct_blade_create_maps_to_host_blade(self):
        assert (
            ChaosbladeProvider().issue_time_method("blade_create", {}) == "host_blade"
        )

    def test_kubectl_exec_embedded_blade_maps_to_kubectl_exec(self):
        # 判定词汇住 kubectl 工具域（message_scanning.KUBECTL_COMMAND_SUBCOMMANDS，
        # phase-14 G2；exec ∈ {exec, debug}）
        result = ChaosbladeProvider().issue_time_method(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": "nginx-1 -n demo -- blade create k8s pod-cpu fullload",
            },
        )
        assert result == "kubectl_exec"

    def test_kubectl_debug_embedded_blade_maps_to_kubectl_exec(self):
        result = ChaosbladeProvider().issue_time_method(
            "kubectl",
            {
                "subcommand": "debug",
                "v_args": "node-1 -- blade create k8s node-cpu fullload",
            },
        )
        assert result == "kubectl_exec"

    def test_kubectl_exec_without_blade_create_is_not_claimed(self):
        assert (
            ChaosbladeProvider().issue_time_method(
                "kubectl", {"subcommand": "exec", "v_args": "nginx-1 -- ls /data"}
            )
            is None
        )

    def test_kubectl_object_verb_is_not_claimed(self):
        assert (
            ChaosbladeProvider().issue_time_method(
                "kubectl", {"subcommand": "get", "v_args": "pods"}
            )
            is None
        )

    def test_non_kubectl_tool_is_not_claimed(self):
        assert ChaosbladeProvider().issue_time_method("host_read", {}) is None


# ---------------------------------------------------------------------------
# 1.1 横向判定路径：verify 的 native takeover 判定
# ---------------------------------------------------------------------------


class TestNativeTakeoverSnapshot:
    """[行为等价] G2 改道后，was_blade_create_attempted 分支结果逐值相等。"""

    def test_durable_method_record_short_circuits(self):
        # 任何已提交的 method 归因都证明有注入成功 → 不是 "attempted and failed"
        msgs = [ToolMessage(name="blade_create", content="Error: create failed", tool_call_id="tc0")]
        assert was_blade_create_attempted(msgs, "kubectl_native") is False

    def test_kubectl_exec_delivery_is_not_attempt_failure(self):
        # kubectl exec 成功投递 blade（content-only 检测，无 tool_call_id）
        msgs = [
            ToolMessage(
                name="kubectl",
                content='{"code":200,"success":true,"result":"abc123def"}',
                tool_call_id="tc1",
            )
        ]
        assert was_blade_create_attempted(msgs, None) is False

    def test_native_takeover_after_failed_blade(self):
        # blade 失败后 kubectl-native 接管（scale 落在 blade_create 之后）
        # —— 判定词汇住 kubectl 工具域（message_scanning 的 write 子命令集
        # / command 子命令集 / exec_inner_command_mutates，phase-14 G2）
        msgs = [
            ToolMessage(name="blade_create", content="Error: create failed", tool_call_id="tc0"),
            _ai_kubectl_call(
                "scale", "deployment/nginx --replicas=0 -n demo", "call_2"
            ),
            ToolMessage(
                name="kubectl",
                content="deployment.apps/nginx scaled",
                tool_call_id="call_2",
            ),
        ]
        assert was_blade_create_attempted(msgs, None) is False

    def test_failed_blade_without_alternative_is_attempted(self):
        msgs = [ToolMessage(name="blade_create", content="Error: create failed", tool_call_id="tc0")]
        assert was_blade_create_attempted(msgs, None) is True

    def test_successful_blade_create_is_still_attempted_flag_true(self):
        # 无 UID 提取、无 method 归因时，blade_create 存在即 attempted
        msgs = [ToolMessage(name="blade_create", content="created experiment", tool_call_id="tc0")]
        assert was_blade_create_attempted(msgs, None) is True

    def test_no_blade_evidence(self):
        assert was_blade_create_attempted([], None) is False


# ---------------------------------------------------------------------------
# 1.1 横向判定路径：k8s_native classifier 对 inline blade 的递归委托
# ---------------------------------------------------------------------------


class TestInlineBladeDelegationSnapshot:
    """[行为等价] G3 接缝化后，委托分类结果逐值相等。"""

    def test_direct_pod_fixture(self):
        # 被委托函数本体：blade create k8s pod-cpu fullload --names X -n ns
        et = classify_inline_blade(
            [
                "blade", "create", "k8s", "pod-cpu", "fullload",
                "--names", "nginx-1", "-n", "demo",
            ],
            "kubectl exec nginx-1 -n demo -- blade create k8s pod-cpu fullload --names nginx-1",
            fallback_ns="demo",
            fallback_pod="nginx-1",
        )
        assert et.scope == "pod"
        assert et.namespace == "demo"
        assert et.names == ("nginx-1",)
        assert et.fault_target == "cpu"
        assert et.fault_action == "fullload"

    def test_inline_destroy_routes_to_unknown_with_uid(self):
        # 第十二轮翻案：inline destroy 不再判 readonly（E3 溯源旁路）——
        # 路由 SCOPE_UNKNOWN 携带 blade_destroy_uid，由 screener 溯源门验证。
        # Round-16 形态翻案：hex16 是实验 UID 形态（非形态 token 按设计
        # 走 form issue，见 TestInlineBladeDestroyProvenance 面）。
        et = classify_inline_blade(
            ["blade", "destroy", "aa11bb22cc33dd44"],
            "kubectl exec p -- blade destroy aa11bb22cc33dd44",
            fallback_ns="demo",
            fallback_pod="p",
        )
        assert et.scope == SCOPE_UNKNOWN
        assert et.blade_destroy_uid == "aa11bb22cc33dd44"

    def test_node_fixture(self):
        et = classify_inline_blade(
            [
                "blade", "create", "k8s", "node-cpu", "fullload",
                "--node", "node-1",
            ],
            "kubectl debug node/node-1 -- blade create k8s node-cpu fullload --node node-1",
            fallback_ns="default",
            fallback_pod="",
        )
        assert et.scope == "node"
        assert et.namespace == ""
        assert et.names == ("node-1",)
        assert et.fault_target == "cpu"
        assert et.fault_action == "fullload"

    def test_delegation_via_k8s_native_classifier(self):
        # 委托点本身：K8sNativeProvider.classify_tool_target 对 exec+blade
        # 的分类 == classify_inline_blade 的直接结果（横向接缝行为等价）
        et = K8sNativeProvider().classify_tool_target(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": "nginx-1 -n demo -- blade create k8s pod-cpu fullload --names nginx-1",
            },
            "kubectl exec nginx-1 -n demo -- blade create k8s pod-cpu fullload --names nginx-1",
        )
        assert et is not None
        assert et.scope == "pod"
        # 现状语义：外层 kubectl 的 ``-n demo`` 不透传给内层 blade
        # （inner 无 ``-n`` 时 effective namespace 判为 default）
        assert et.namespace == "default"
        assert et.names == ("nginx-1",)
        assert et.fault_target == "cpu"
        assert et.fault_action == "fullload"

    def test_delegation_via_k8s_native_classifier_node(self):
        et = K8sNativeProvider().classify_tool_target(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": "nginx-1 -n demo -- blade create k8s node-cpu fullload --node node-1",
            },
            "kubectl exec nginx-1 -n demo -- blade create k8s node-cpu fullload --node node-1",
        )
        assert et is not None
        assert et.scope == "node"
        assert et.namespace == ""
        assert et.names == ("node-1",)


# ---------------------------------------------------------------------------
# 1.2 hydration 消费点的新键语义
# ---------------------------------------------------------------------------


class TestHydrationNewKeySnapshot:
    """[行为等价 + EOL 翻转] G4 拆除转译层后：新键路径语义零变化，
    旧键 record 经原消费点读取为空（旧键视作不存在）。"""

    def test_new_key_records_pass_through(self):
        # 原消费点（state.build_status_data 的 hydration 入口已拆）：
        # 新键 record 直通——experiment_uid 原样读出
        data = build_status_data("t1", {"experiment_uid": "u1"})
        assert data["experiment_uid"] == "u1"

    def test_new_key_wins_over_legacy(self):
        # EOL 语义：双键 record 只读新键（phase-9 前的「新键胜」升格
        # 为「旧键根本不读」——fresh-database 裁决下旧键不存在）
        data = build_status_data(
            "t1", {"experiment_uid": "new", "blade_uid": "old"}
        )
        assert data["experiment_uid"] == "new"

    def test_legacy_key_translated_current(self):
        # [已翻转] 原快照：hydrate({"blade_uid": "u1"}) ==
        # {"experiment_uid": "u1"}。G4 EOL 后：旧键 record 经原消费点
        # 读取为空（旧键视作不存在）。
        data = build_status_data("t1", {"blade_uid": "u1"})
        assert data["experiment_uid"] == ""

    def test_fault_spec_from_dict_new_keys(self):
        # 消费点（fault_spec.from_dict 的 hydration 入口）：新键 spec dict
        spec = FaultSpec.from_dict(
            {
                "scope": "pod",
                "namespace": "demo",
                "names": ["nginx-1"],
                "fault_target": "cpu",
                "fault_action": "fullload",
            }
        )
        assert spec is not None
        assert spec.namespace == "demo"
        assert spec.scope == "pod"
        assert list(spec.names) == ["nginx-1"]
        assert spec.fault_target == "cpu"
        assert spec.fault_action == "fullload"

    def test_approved_from_dict_new_keys(self):
        # 消费点（freeze.approved_from_dict 的 hydration 入口）：新键 approved dict
        at = approved_from_dict(
            {
                "scope": "pod",
                "namespace": "demo",
                "names": ["nginx-1"],
                "fault_target": "cpu",
                "fault_action": "fullload",
            }
        )
        assert at is not None
        assert at.namespace == "demo"
        assert at.names == ("nginx-1",)
        assert at.fault_target == "cpu"
        assert at.fault_action == "fullload"

    def test_approved_from_dict_legacy_translated_current(self):
        # [已翻转] 原快照：blade_target/blade_action 被翻译读取。
        # G4 EOL 后：旧键 record 经原消费点读取为空（默认 ""）。
        at = approved_from_dict(
            {"scope": "pod", "blade_target": "cpu", "blade_action": "fullload"}
        )
        assert at is not None
        assert at.fault_target == ""
        assert at.fault_action == ""


# ---------------------------------------------------------------------------
# 1.3 handle kind 值现状与 compactor 渲染去重
# ---------------------------------------------------------------------------


class TestHandleKindSnapshot:
    """kind 协议值现状钉扎（G7 改名后断言翻转，渲染行为不变）。"""

    def test_chaosblade_handle_kind_value(self):
        # [已翻转] phase-14 G7："blade_uid" → "experiment_uid"
        assert ChaosbladeProvider.handle_kind == "experiment_uid"

    def test_python_provider_handle_kind_value(self):
        # [已翻转] phase-14 G7："blade_uid" → "experiment_uid"
        assert ChaosbladePythonProvider.handle_kind == "experiment_uid"

    def test_build_fault_handle_dict(self):
        h = ChaosbladeProvider().build_fault_handle(
            {"experiment_uid": "u1", "injection_method": "host_blade"}
        )
        # [已翻转] phase-14 G7：kind 值变 "experiment_uid"，其余逐值相等
        assert h == {"kind": "experiment_uid", "value": "u1", "method": "host_blade"}

    def test_python_build_fault_handle_dict(self):
        h = ChaosbladePythonProvider().build_fault_handle(
            {"experiment_uid": "u2", "injection_method": "python_agent"}
        )
        # [已翻转] phase-14 G7：kind 值变 "experiment_uid"
        assert h == {"kind": "experiment_uid", "value": "u2", "method": "python_agent"}

    def test_build_fault_handle_none_without_uid(self):
        assert (
            ChaosbladeProvider().build_fault_handle({"injection_method": "host_blade"})
            is None
        )

    def test_compactor_renders_uidless_handle(self):
        # [行为等价] UID-less 载体 handle 显示
        msg = build_post_compact_context_message(
            {"active_fault_handle": {"kind": "native", "method": "host_native"}}
        )
        assert "Active fault handle" in msg

    def test_compactor_dedupes_experiment_kind_handle(self):
        # [行为等价] 实验载体 handle 走 UID 行不重复（kind 判据去重）。
        # phase-14 G7 改名后 fixture 的 kind 值同步换 "experiment_uid"，
        # 去重行为本身必须不变。
        msg = build_post_compact_context_message(
            {
                "active_experiment_uid": "u1",
                "active_fault_handle": {
                    "kind": "experiment_uid",
                    "value": "u1",
                    "method": "host_blade",
                },
            }
        )
        assert "Active fault handle" not in msg
        assert "Active experiment_uid: u1" in msg


# ---------------------------------------------------------------------------
# 1.4 compactor 双键正则现状
# ---------------------------------------------------------------------------


class TestCompactorDualKeyRegexSnapshot:
    """UID 提取双键正则现状钉扎（G5 降单键后旧键翻转）。"""

    def test_new_key_text_extracted(self):
        # [已翻转] round-22 Q4：存续上下文消息面委托 registry 接缝——
        # 非工具载体（prose/_Msg 存根）的 experiment_uid 拼写不再回流
        # （洗入通道废除：HumanMessage 提及/任意消息文本不能许可 UID）。
        # 合法载体是真 blade_create ToolMessage（test_uid_shape_legislation
        # .test_survival_context_delegates_uid_lifecycle_to_registry 钉扎）。
        ctx = extract_critical_context(
            [_Msg('{"experiment_uid": "abc123def4560789"}')], {}
        )
        assert ctx.get("active_experiment_uid") is None

    def test_legacy_key_text_not_extracted(self):
        # [已翻转] G5 单键化后：旧键拼写文本不再命中（fresh-database
        # 裁决，旧会话消息不回流）
        ctx = extract_critical_context(
            [_Msg('{"blade_uid": "abc123def4560789"}')], {}
        )
        assert ctx.get("active_experiment_uid") is None

    def test_plain_text_new_key_extracted(self):
        # [已翻转] round-22 Q4 同上：prose 面废除，非工具载体不回流
        ctx = extract_critical_context(
            [_Msg("experiment_uid: ffedcba432107891")], {}
        )
        assert ctx.get("active_experiment_uid") is None
