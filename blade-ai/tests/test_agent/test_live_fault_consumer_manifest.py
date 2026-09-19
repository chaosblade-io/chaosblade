"""Round-28 live-consumer manifest — PARTIAL enforcement, honestly scoped.

``has_live_fault`` 语义立法无法像 UID 正则立法那样源级机器强制：一个
**新的** live 消费点写 ``has_active_fault`` 在 AST 层完全合法，没有任何
机器信号能识别「作者以为在读 live 侧契约」。本文件的锚定对象因此是
**已知清单**——round-28 替换的六个位点（keep/combo×2/zombie/graft/
rollback）与 emergency 门必须保持 live 谓词形态；任一位点退回 committed
谓词立即失败。清单之外的新位点靠 review 与 ``has_live_fault`` docstring
的指引（诚实性限定：partial——这是能力边界，不是遗漏）。

Round-30 升级：判据锚之外出现动作域错位形态（判据 plural、动作
singular——round-30 K1'），rollback 位点的锚从谓词形态升级为**同域**
形态（experiment-kind 动作 MUST 走 plural 清偿原语 sweep；singular
dispatch 仅在 native 分支合法）。

每个锚同时钉住**不应存在**的旧形态（committed 谓词或 UID 存在性当 live
证明），防止「回退式回归」——修复被悄悄还原而测试仍绿。
"""

from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "chaos_agent"


def _read(rel: str) -> str:
    return (_SRC_ROOT / rel).read_text(encoding="utf-8")


class TestLiveConsumerManifest:
    """The known live-semantics sites must gate on the live predicate."""

    def test_keep_across_seam_uses_live_predicate(self):
        src = _read("agent/nodes/planning/plan_change_confirm.py")
        # R1: the keep decision — a corpse releases the slot, a live fault
        # keeps its recovery handle.
        assert "keep_experiment_uid=has_live_fault(state)" in src
        assert "keep_experiment_uid=has_active_fault" not in src

    def test_combo_method_cleared_branch_uses_live_predicate(self):
        src = _read("agent/nodes/execute/execute_loop.py")
        # R2 (the replan-aftermath branch): a carried-over corpse slot must
        # not license a combo mark on the next native issue.
        assert "_experiment_live = has_live_fault(state)" in src
        assert "_experiment_live = has_active_fault(state)" not in src

    def test_combo_method_attributed_branch_uses_liability_oracle(self):
        src = _read("agent/nodes/execute/execute_loop.py")
        # R2 twin (round-28): UID PRESENCE on the state slot used to
        # license the combo; the liability oracle judges the slot instead.
        # The result-side UID keeps presence semantics (this iteration's
        # fresh birth, born-live by construction).
        assert (
            'result.get("experiment_uid")\n                    or live_liability_uids(state)'
            in src
        )
        assert (
            'state.get("experiment_uid")\n                    or result.get("experiment_uid")'
            not in src
        )

    def test_zombie_replan_guard_uses_live_predicate(self):
        src = _read("agent/nodes/execute/execute_loop.py")
        # R3: the guard must be able to fire on the post-destroy steady
        # state (the committed twin never let it).
        assert "and not has_live_fault(state)" in src
        assert "and not has_active_fault(state)" not in src

    def test_graft_fault_lane_uses_live_predicate(self):
        src = _read("server/routes/turn_event_stream.py")
        # R5: the fault lane grafts live fault context only — a corpse's
        # context is dead weight in the recover graph's input. The skill
        # lane and the recover-request elif keep their own semantics.
        assert "has_live_fault(_inj_state.values)" in src
        assert "has_active_fault(_inj_state.values)" not in src
        # The retracted-site control (round-27 R4, renumbered 28): the
        # recover-request elif keeps the COMMITTED predicate on purpose —
        # identity semantics; the user's recover targeting legitimately
        # names a dead experiment.
        assert "has_active_fault(_rv)" in src

    def test_auto_rollback_action_domain_matches_the_gate(self):
        src = _read("cli/session_finalize.py")
        # R6 (round-30 upgrade): the r29 gate judged the PLURAL liability
        # set while the action dispatched the SINGULAR slot — a composite
        # create's first-birth corpse rode the dispatch while the live
        # sibling left the failing task orphaned (round-30 K1'). The
        # experiment-kind branch now dispatches the liability SWEEP (the
        # live-set-driven destroy — gate and action are one); the explicit
        # has_live_fault call is gone because the sweep internalizes it.
        assert "sweep_live_liabilities" in src
        assert "is_experiment_handle(handle)" in src
        assert "if has_live_fault(values):" not in src
        assert "if has_active_fault(values):" not in src
        # The singular dispatch survives ONLY in the native branch —
        # native carriers have no plural domain (single handle, no death
        # oracle, round-25); re-introducing it for experiment kinds is the
        # exact retraction this anchor exists to catch.
        assert "FaultProviderRegistry.rollback_handle(" in src

    def test_emergency_gate_single_sourced_no_inline_ledger(self):
        src = _read("l4/execution.py")
        # Round-25's inline carrier-split gate was promoted to the
        # predicate (round-27/28): the site delegates, and the inline
        # ledger assembly is GONE from this file (drift between the two
        # copies was the whole rollout-gap root cause).
        assert "if has_live_fault(state.values):" in src
        assert "live_liability_uids" not in src

    def test_router_verifier_branch_keeps_committed_by_design(self):
        src = _read("agent/router.py")
        # The RETRACTED site (round-27 R4): the verifier branch is the
        # text-only EXIT gate — the router deliberately has no "end"
        # (task-ff057e7f), and swapping to the live predicate would
        # ping-pong text conclusions until MAX_EXECUTE_LOOP (the
        # task-51193464 family). Committed here is BY DESIGN; this anchor
        # guards against a future "consistency" pass re-introducing the
        # retraction.
        assert "if has_active_fault(state):" in src
        assert "if has_live_fault(state):" not in src
