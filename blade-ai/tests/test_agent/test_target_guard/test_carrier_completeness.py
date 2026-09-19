"""载体完备性回归（tasks 2.5，立法 OQ2「内层命令级」判定粒度）。

阶段 1 守卫完备性审计的可执行立法（见 change `universal-cognitive-architecture`
的 guard-carrier-audit.md + design.md D2/D7）：

**核心不变量**——每个 *可解析* 危险命令载体，其 **内层 / 嵌套** 危险命令
（``blade create`` / ``rm -rf`` / escape 原语）MUST 被路由过共享分类器
（``infer_effective_target`` → registry → k8s_native）和/或只读判官，且裁决
**NEVER == ``SCOPE_READONLY``**。若能被判 READONLY，它就能穿过只读相位筛查
（task-ce9647931ce1 的 ``kubectl exec POD -- blade create`` 走私向量），测试失败。

**为什么是内层命令级**：task-ce9647931ce1 的外层 ``kubectl exec`` 合法、内层
``blade create`` 才是危险载荷。二进制级断言只查 ``kubectl`` 白名单、子命令级只查
``exec`` 是否允许——都漏掉内层走私。只有内层命令级断言能立法堵住该向量。

**MCP（不可解析载体）是唯一例外**：判 READONLY 直通、不走 ToolGuard（operator
保证）。这是 design.md D7 的 *可达性边界*、非缺口——安全 rests on ``attach_to``
（变更型 MCP 挂 phase2 记 WARNING）+ 失败成本（只读=低成本）。本文件把该边界
**钉为断言**，防止它被误当"未路由分类器"的缺口去"修复"（修复反会破坏只读面开放）。
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.target_guard.classifier import (
    SCOPE_ESCAPE,
    SCOPE_READONLY,
    SCOPE_UNKNOWN,
    infer_effective_target,
)
from chaos_agent.mcp.registry import McpToolRegistry
from chaos_agent.tools.host_cmd import host_inject
from chaos_agent.tools.readonly import (
    host_command_rejection_reason,
    kubectl_exec_rejection_reason,
)


@pytest.fixture(autouse=True)
def _clean_mcp_registry():
    """Only the MCP-boundary class registers tools; clear around every test so
    a registration never leaks into another carrier's classification."""
    McpToolRegistry.clear()
    yield
    McpToolRegistry.clear()


# ---------------------------------------------------------------------------
# 载体 #3：full kubectl（变更载体）——exec 内层命令级路由
# ---------------------------------------------------------------------------


class TestKubectlExecInnerNeverReadonly:
    """full ``kubectl`` 载体的 exec **内层**危险命令 MUST NOT 被判 READONLY。

    内层经 ``_classify_kubectl_exec`` 递归路由：``blade`` → chaosblade carrier
    的 inline CLI 解析器（registry.classify_inline_blade_command）；``kubectl``
    → 递归；escape 原语 → SCOPE_ESCAPE；plain ``rm``/``kill`` → pod 变更。
    """

    @pytest.mark.parametrize(
        "inner",
        [
            ["blade", "create", "k8s", "pod-network", "loss", "--percent", "100"],
            ["blade", "create", "k8s", "container-cpu", "fullload"],
            ["rm", "-rf", "/"],
            ["rm", "-rf", "/data"],
            ["kill", "-9", "1"],
            ["nsenter", "-t", "1", "-m", "--", "iptables", "-A", "INPUT", "-j", "DROP"],
        ],
    )
    def test_dangerous_inner_is_never_readonly(self, inner):
        et = infer_effective_target("kubectl", ["exec", "p1", "-n", "ns", "--", *inner])
        assert et.scope != SCOPE_READONLY, inner

    def test_inner_blade_routes_to_blade_carrier_as_mutation(self):
        """走私向量被堵死：内层 ``blade create`` 归 chaosblade carrier，判 pod 变更。"""
        et = infer_effective_target(
            "kubectl",
            ["exec", "p1", "-n", "ns", "--", "blade", "create", "k8s",
             "container-cpu", "fullload"],
        )
        assert et.scope == "pod"
        assert et.names == ("p1",)

    def test_inner_escape_primitive_is_scope_escape(self):
        """内层 ``nsenter`` + 变更载荷 → SCOPE_ESCAPE（host-escape，非 READONLY）。"""
        et = infer_effective_target(
            "kubectl",
            ["exec", "p1", "-n", "ns", "--", "nsenter", "-t", "1", "-m",
             "--", "iptables", "-A", "INPUT", "-j", "DROP"],
        )
        assert et.scope == SCOPE_ESCAPE

    def test_readonly_inner_is_readonly_control(self):
        """对照组：只读内层（``df -h``）确实判 READONLY——证明分类器是**区分**
        内层语义、不是一刀切拒绝（否则只读面开放无意义）。"""
        et = infer_effective_target("kubectl", ["exec", "p1", "-n", "ns", "--", "df", "-h"])
        assert et.scope == SCOPE_READONLY


# ---------------------------------------------------------------------------
# 载体 #4：kubectl_read（只读载体）——工具层 exec/debug 内层门
# ---------------------------------------------------------------------------


class TestKubectlReadOnlyCarrierInnerGate:
    """``kubectl_read`` 的 exec/debug 内层门（kubectl.py L1828-1841）用共享判官
    ``kubectl_exec_rejection_reason``：内层非只读即拒（返回具体 reason）。"""

    @pytest.mark.parametrize(
        "v_args",
        [
            "p1 -n ns -- blade create k8s pod-network loss --percent 100",
            "p1 -- rm -rf /",
            "p1 -- rm -rf /tmp/x",
        ],
    )
    def test_dangerous_inner_rejected(self, v_args):
        assert kubectl_exec_rejection_reason(v_args) is not None, v_args

    @pytest.mark.parametrize(
        "v_args",
        ["p1 -- df -h", "p1 -n ns -- cat /proc/loadavg"],
    )
    def test_readonly_inner_allowed_control(self, v_args):
        assert kubectl_exec_rejection_reason(v_args) is None, v_args


# ---------------------------------------------------------------------------
# 载体 #5/#6：host_inject / host_read（主机载体）
# ---------------------------------------------------------------------------


class _Recorder:
    """Capture the kwargs ``execute_via_transport`` is called with."""

    def __init__(self):
        self.calls = []

    async def __call__(self, cmd, target, **kwargs):
        from chaos_agent.models.command_result import CommandResult

        self.calls.append({"cmd": cmd, **kwargs})
        return CommandResult(exit_code=0, stdout="out", stderr="")

    @property
    def last(self):
        return self.calls[-1]


@pytest.fixture
def spy(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr("chaos_agent.tools.host_cmd.execute_via_transport", rec)
    return rec


class TestHostCarrierDangerousCommandGuarded:
    """host 载体：危险命令 MUST 落入 ToolGuard（``skip_guard=False``，rm 非白名单
    被拒）；只读判官 ``host_command_rejection_reason`` MUST 拒危险命令。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["rm -rf /data", "rm -rf /"])
    async def test_dangerous_host_inject_does_not_skip_guard(self, spy, command):
        await host_inject.ainvoke({"command": command})
        assert spy.last["skip_guard"] is False, command

    @pytest.mark.asyncio
    async def test_readonly_host_inject_skips_guard_control(self, spy):
        await host_inject.ainvoke({"command": "df -h"})
        assert spy.last["skip_guard"] is True

    @pytest.mark.parametrize("command", ["rm -rf /data", "rm -rf /"])
    def test_host_read_judge_rejects_dangerous(self, command):
        assert host_command_rejection_reason(command) is not None, command

    def test_host_read_judge_allows_readonly_control(self):
        assert host_command_rejection_reason("df -h") is None


# ---------------------------------------------------------------------------
# 载体 #8：MCP（不可解析载体）——边界记录，非缺口
# ---------------------------------------------------------------------------


class TestMcpBoundaryRecordedNotAGap:
    """MCP 判 READONLY 直通、不走 ToolGuard（operator 保证）——design.md D7 的
    可达性边界。此测试把边界钉住：发现型工具**只有经 operator install-time 注册**
    才 READONLY 放行；未注册 → 默认拒（SCOPE_UNKNOWN），不是 READONLY。"""

    def test_registered_mcp_is_readonly_passthrough(self):
        McpToolRegistry.register("coroot__query", ("phase1", "verifier"))
        et = infer_effective_target("coroot__query", {"q": "up"})
        assert et.scope == SCOPE_READONLY

    def test_unregistered_mcp_is_default_deny_not_readonly(self):
        et = infer_effective_target("coroot__query", {"q": "up"})
        assert et.scope == SCOPE_UNKNOWN
        assert et.scope != SCOPE_READONLY
