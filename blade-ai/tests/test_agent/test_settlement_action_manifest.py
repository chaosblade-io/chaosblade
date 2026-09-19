"""Round-31 settlement-action manifest — PARTIAL enforcement, honestly scoped.

Round-30 立法「live 语义消费点的判据与动作 MUST 同域」但只落地了
auto_rollback 一个 seam——r25→r29 的立法-推广分离在动作侧复发（元模式
第十一次）。本文件是清偿**动作面**的清单锚，与判据面 manifest
（test_live_fault_consumer_manifest.py）对偶：库里每一个负有清偿义务的
seam（以清理/回滚义务 dispatch destroy 的路径）MUST 走 plural 清偿原语
``sweep_live_liabilities``——任一已登记 seam 退回 singular dispatch
（绕过 sweep 直接 ``layer1_raw_destroy`` 单 UID / 实验分支走
``rollback_handle``）立即失败。

登记的豁免（每条带理由；无理由的 singular dispatch 即违约）：

- recover finalize 的 identity retry（``layer1_raw_destroy`` 裸重试）：
  committed 恢复目标的定向重试——同一 finalize 的前置 sweep 已清掉
  全部残余负债，retry 时序在后（时序豁免，非域豁免）。
- auto_rollback 的 native 分支（``rollback_handle``）：native 载体无
  plural 域（单 handle、无死亡预言机，r25 裁决）——判据域与动作域
  本就同域自洽。

清单之外的新 seam 靠 review 与 ``sweep_live_liabilities`` docstring 的
指引（诚实性限定：partial——语义立法无法机器强制新 seam；这是能力
边界，不是遗漏）。

每个锚同时钉住**不应存在**的旧形态（singular 直派），防止「回退式
回归」——修复被悄悄还原而测试仍绿。

Round-32 增补（配置域锚）：r30/r31 钉住了「打谁」（判据域）与
「谁来打」（动作面），本清单再把「用什么配置打」钉进同一张网
——destroy 的 kubeconfig MUST 经 resolve_kubeconfig 三级回退（与
注入链 create 单源），解析长在 sweep 原语内部（裸传 state 是正确
形态）；显式签名派发位（rollback_handle 调用点）MUST 在调用点传
解析值。任一退回裸读即失败。
"""

from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "chaos_agent"


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


class TestSettlementActionSeams:
    """The four seams that owe a settlement obligation MUST dispatch the
    plural primitive."""

    def test_plan_change_seam_sweeps(self):
        src = _read("agent/nodes/planning/plan_change_confirm.py")
        # The contract boundary (r26 legislation): the old contract's
        # experiments must not run under the new one.
        assert "sweep_live_liabilities" in src

    def test_recover_finale_sweeps(self):
        src = _read("agent/nodes/recover/_recover_finalize.py")
        # B76 review G: the last point where the framework still holds
        # the full ownership record.
        assert "sweep_live_liabilities" in src

    def test_auto_rollback_sweeps(self):
        src = _read("cli/session_finalize.py")
        # Round-30: the failure-termination path (CLI + both server
        # routes ride this shared seam).
        assert "sweep_live_liabilities" in src
        assert "is_experiment_handle(handle)" in src
        # The singular dispatch survives ONLY in the native branch.
        assert "FaultProviderRegistry.rollback_handle(" in src

    def test_verify_replan_cleanup_sweeps(self):
        src = _read("agent/nodes/verify/_verifier_finalize.py")
        # Round-31 (R6''): the replan seam's own pollution rationale
        # ("a live residual pollutes the fresh verification") is the
        # plan-change seam's rationale verbatim — same obligation, same
        # primitive.
        assert "sweep_live_liabilities" in src
        # The retired singular dispatch must not come back.
        assert "layer1_raw_destroy(" not in src

    def test_sweep_primitive_lives_at_registry(self):
        src = _read("agent/providers/registry.py")
        # The single plural settlement primitive every seam rides.
        assert "async def sweep_live_liabilities" in src


class TestRegisteredExemptions:
    """Singular dispatches that are LEGAL, each with its legislated
    reason pinned — an exemption without a reason is a violation."""

    def test_recover_identity_retry_is_exempt_by_ordering(self):
        src = _read("agent/nodes/recover/_recover_finalize.py")
        # The retry targets the committed recovery identity AFTER the
        # same finalize's sweep already cleared every residual liability
        # (the sweep runs above the retry block) — a timing exemption,
        # not a domain one: the retry re-owns an identity whose destroy
        # the main Layer-1 flow failed, it does not settle the set.
        assert "layer1_raw_destroy(" in src
        assert src.index("sweep_live_liabilities") < src.index(
            "layer1_raw_destroy("
        )


class TestSettlementConfigDomain:
    """Round-32 K1 — the settlement CONFIG domain: every destroy the
    framework dispatches MUST run under the same resolution contract the
    injection chain's create used (state > spec > settings). The
    resolution lives INSIDE the sweep primitive; an explicit-signature
    dispatch site passes the resolved value at the call site."""

    def test_sweep_resolves_kubeconfig_internally(self):
        src = _read("agent/providers/registry.py")
        # The bare state read ("" fallback → blade's own default cluster
        # whenever the CLI entry seeded the key empty) must not return.
        assert "kubeconfig = resolve_kubeconfig(values)" in src
        assert 'values.get("kubeconfig") or ""' not in src

    def test_native_rollback_dispatch_passes_resolved_value(self):
        src = _read("cli/session_finalize.py")
        # The rollback_handle kwarg is an explicit signature — the call
        # site carries the resolution duty (today's native carriers are
        # no-op rollbacks, so this pins the defensive contract a future
        # real undo inherits).
        assert "kubeconfig=resolve_kubeconfig(values)" in src
        assert 'values.get("kubeconfig", "")' not in src

    def test_neutral_resolver_home_exists(self):
        # The canonical resolver must live at the neutral layer (importable
        # from providers without a layering inversion) — moving it back
        # into the execute-private module would re-create the structural
        # pressure that produced the split.
        src = _read("agent/kubeconfig.py")
        assert "def resolve_kubeconfig" in src
        assert "return settings.kubeconfig_path" in src

    def test_execute_alias_delegates_to_canonical(self):
        src = _read("agent/nodes/execute/_kubeconfig_inject.py")
        # The execute-side private name must delegate (not re-implement)
        # — a second implementation would be a second truth source.
        assert "from chaos_agent.agent.kubeconfig import resolve_kubeconfig" in src
        # The old inline body (its own three-tier reads) is gone.
        assert "return settings.kubeconfig_path" not in src
