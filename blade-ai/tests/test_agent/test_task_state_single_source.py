"""TaskState vocabulary single-source pinning (B76 round-15 root-cause fix).

Round-15 found the task_state domain (10 words) existing only as prose in
the infer_task_state docstring while ~10 files hand-copied subsets of it,
plus THREE parallel implementations of the recover verdict → task_state
mapping (infer_task_state's recover branch, recover_task_state_from_values,
_recover_finalize's ternary) disagreeing on exactly one input combination.

The fix legislated TaskState in state.py and single-sourced the mapping in
``recovery_task_state_from_level`` (D3; D4 ruling: verification
authoritative). These tests pin:

  1. legislation  — the 10-word closed set and the terminal subset (8)
  2. mapping      — the full consistency matrix: 4 levels × 2 recovered ×
                    3 layer1 statuses, with the D4 divergence combo as an
                    explicit row
  3. agreement    — all three historical entry points (A/B) return the
                    same word for every matrix combination — the round-15
                    acceptance criterion
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.result.operation_result import recover_task_state_from_values
from chaos_agent.agent.result.verdict import RECOVER_VERDICT_VALUES
from chaos_agent.agent.state import (
    TASK_STATE_TERMINAL_VALUES,
    TASK_STATE_VALUES,
    infer_phase,
    infer_task_state,
    layer1_expired_override_applies,
    recovery_task_state_from_level,
)


# ---------------------------------------------------------------------------
# 1. Legislation: closed set + terminal subset
# ---------------------------------------------------------------------------


class TestTaskStateLegislation:
    def test_closed_set_is_the_ten_word_de_facto_set(self):
        # The de-facto vocabulary infer_task_state always produced — the
        # enum legislated it verbatim (no word added, none renamed).
        assert TASK_STATE_VALUES == {
            "injecting", "injected", "recovering", "recovered",
            "partial_recovered", "unverified", "failed", "rejected",
            "completed", "cancelled",
        }

    def test_terminal_subset_excludes_only_the_transient_pair(self):
        assert TASK_STATE_TERMINAL_VALUES == TASK_STATE_VALUES - {
            "injecting", "recovering",
        }

    def test_terminal_subset_matches_the_historical_task_store_copy(self):
        # The frozenset task_store hand-copied before round-15 — pinned so
        # the derived constant can never silently drift from the de-facto
        # guard set it replaced.
        assert TASK_STATE_TERMINAL_VALUES == frozenset({
            "injected", "recovered", "partial_recovered",
            "failed", "rejected", "completed", "cancelled", "unverified",
        })


# ---------------------------------------------------------------------------
# 2. Mapping: the full consistency matrix (D4 ruling explicit)
# ---------------------------------------------------------------------------

LEVELS = sorted(RECOVER_VERDICT_VALUES)  # recovered, partial, unrecovered, unverified
RECOVERED = [True, False]
LAYER1 = ["passed", "skipped", "failed"]


def _expected(level: str, recovered: bool, layer1_status: str) -> str:
    """The D4-A-semantics truth table (verification authoritative)."""
    if recovered:
        return "partial_recovered" if level == "partial" else "recovered"
    if level == "unverified":
        return "unverified"
    if layer1_status == "skipped" and level in ("recovered", "partial"):
        return "partial_recovered" if level == "partial" else "recovered"
    return "failed"


class TestRecoveryMappingMatrix:
    @pytest.mark.parametrize("level", LEVELS)
    @pytest.mark.parametrize("recovered", RECOVERED)
    @pytest.mark.parametrize("layer1_status", LAYER1)
    def test_matrix_cell(self, level, recovered, layer1_status):
        assert (
            recovery_task_state_from_level(
                level, recovered=recovered, layer1_status=layer1_status
            )
            == _expected(level, recovered, layer1_status)
        )

    def test_d4_divergence_combo_is_verification_authoritative(self):
        """THE round-15 finding: recovered=False + level="recovered" +
        layer1="skipped" resolved differently across the three copies
        (A→recovered, B/C→failed). D4 ruled A semantics — the verification
        verdict outranks the result boolean mirror."""
        assert (
            recovery_task_state_from_level(
                "recovered", recovered=False, layer1_status="skipped"
            )
            == "recovered"
        )
        assert (
            recovery_task_state_from_level(
                "partial", recovered=False, layer1_status="skipped"
            )
            == "partial_recovered"
        )

    def test_write_path_invariant_cells(self):
        """On the write path recover finalize derives both inputs from the
        same verification dict (recovered = level ∈ success set), so only
        the diagonal cells are reachable — they must stay stable."""
        for level in ("recovered", "partial"):
            assert recovery_task_state_from_level(
                level, recovered=True, layer1_status="passed"
            ) == ("partial_recovered" if level == "partial" else "recovered")
        for level in ("unverified", "unrecovered"):
            assert recovery_task_state_from_level(
                level, recovered=False, layer1_status="failed"
            ) == ("unverified" if level == "unverified" else "failed")


# ---------------------------------------------------------------------------
# 3. Agreement: the historical A/B entry points return the same word
# ---------------------------------------------------------------------------


def _recover_state(level: str, recovered: bool, layer1_status: str) -> dict:
    return {
        "operation": "recover",
        "confirmed_intent": "inject",
        "result": {"recovered": recovered, "recovery_level": level},
        "recover_verification": {
            "level": level,
            "layer1": {"status": layer1_status},
        },
    }


class TestEntryPointsAgree:
    @pytest.mark.parametrize("level", LEVELS)
    @pytest.mark.parametrize("recovered", RECOVERED)
    @pytest.mark.parametrize("layer1_status", LAYER1)
    def test_a_and_b_agree_on_every_cell(self, level, recovered, layer1_status):
        state = _recover_state(level, recovered, layer1_status)
        a = infer_task_state(state)                       # implementation A
        b = recover_task_state_from_values(state)         # implementation B
        assert a == b == _expected(level, recovered, layer1_status), (
            f"A={a} B={b} expected={_expected(level, recovered, layer1_status)} "
            f"for level={level} recovered={recovered} layer1={layer1_status}"
        )

    def test_b_result_mirror_fallback_agrees(self):
        """Legacy states without a verification dict: B falls back to the
        result mirror and must still produce the same words."""
        for recovered, level, expected in (
            (True, "recovered", "recovered"),
            (True, "partial", "partial_recovered"),
            (False, "unverified", "unverified"),
            (False, "failed", "failed"),
        ):
            values = {
                "operation": "recover",
                "confirmed_intent": "inject",
                "result": {"recovered": recovered, "recovery_level": level},
            }
            assert recover_task_state_from_values(values) == expected


# ---------------------------------------------------------------------------
# 4. Inject-side expired-Layer1 override: predicate single source
# ---------------------------------------------------------------------------
#
# task inject-e47de3e8 / procload_d17fix (inject-76414f92): a Layer1 record
# that ends Destroyed (timeout elapsed OR the agent cleaned up a false-positive
# carrier) yields layer1={status:failed, expired:True}. The invariant "an
# expired Layer1 must not veto a Layer2-verified fault" is consumed by THREE
# terminal faces. Before it was single-sourced only ``_infer_failure_detail``
# carried it, so a verified drill whose carrier was destroyed exited failed
# (envelope 4002) with an EMPTY failure reason — the faces disagreeing on
# identical input. These tests pin the shared predicate AND the cross-face
# equality (memory 02177659: parallel routes must have an a==b assertion, not
# just per-face value pins).


def _inject_state(l1_status, l1_expired, l2_status, level) -> dict:
    layer1 = {"status": l1_status}
    if l1_expired is not None:
        layer1["expired"] = l1_expired
    return {
        "operation": "inject",
        "experiment_uid": "abc123",
        "skill_name": "cpu-stress",
        "verification": {
            "level": level,
            "layer1": layer1,
            "layer2": {"status": l2_status},
        },
    }


class TestLayer1ExpiredOverridePredicate:
    def test_fires_only_on_the_strict_combination(self):
        assert layer1_expired_override_applies(
            {"level": "verified",
             "layer1": {"status": "failed", "expired": True},
             "layer2": {"status": "passed"}}
        ) is True

    def test_each_condition_is_necessary(self):
        base = {"level": "verified",
                "layer1": {"status": "failed", "expired": True},
                "layer2": {"status": "passed"}}
        # missing expired
        assert layer1_expired_override_applies(
            {**base, "layer1": {"status": "failed"}}) is False
        # layer2 not passed
        assert layer1_expired_override_applies(
            {**base, "layer2": {"status": "partial"}}) is False
        # level not verified
        assert layer1_expired_override_applies({**base, "level": "partial"}) is False
        # layer1 not failed
        assert layer1_expired_override_applies(
            {**base, "layer1": {"status": "warning", "expired": True}}) is False

    def test_malformed_verification_is_false(self):
        assert layer1_expired_override_applies(None) is False
        assert layer1_expired_override_applies({}) is False
        assert layer1_expired_override_applies(
            {"level": "verified", "layer1": "failed", "layer2": {"status": "passed"}}
        ) is False


# ---------------------------------------------------------------------------
# 5. Inject-side cross-face equality: task_state == phase == failure_detail
# ---------------------------------------------------------------------------

L1_STATUSES = ["failed"]
EXPIRED = [True, False, None]
L2_STATUSES = ["passed", "partial", "unknown", "failed"]
LEVELS_INJECT = ["verified", "partial", "unverified"]


class TestInjectExpiredOverrideFacesAgree:
    @pytest.mark.parametrize("level", LEVELS_INJECT)
    @pytest.mark.parametrize("l2_status", L2_STATUSES)
    @pytest.mark.parametrize("l1_expired", EXPIRED)
    @pytest.mark.parametrize("l1_status", L1_STATUSES)
    def test_three_faces_agree_on_the_failure_question(
        self, l1_status, l1_expired, l2_status, level
    ):
        """The crux assertion 02177659 demands: ONE fixture fed to all three
        terminal faces, asserting they agree on whether this is a failure. For
        an l1_status=='failed' input every face is binary on the same axis
        (task: injected|failed, phase: verification_passed|verification_failed,
        detail: {}|fail_state), so disagreement is a real divergence — exactly
        the silent split that shipped the exit-1-on-verified bug."""
        from chaos_agent.agent.nodes.store.memory_nodes import _infer_failure_detail

        state = _inject_state(l1_status, l1_expired, l2_status, level)

        override_task = infer_task_state(state) == "injected"
        override_phase = infer_phase(state) == "verification_passed"
        # {} (exempted) == override fired; a fail_state == veto stood.
        override_detail = not _infer_failure_detail(state)

        expected = (
            l1_status == "failed"
            and bool(l1_expired)
            and l2_status == "passed"
            and level == "verified"
        )
        assert override_task == override_phase == override_detail == expected, (
            f"faces diverged: task={override_task} phase={override_phase} "
            f"detail={override_detail} expected={expected} for "
            f"l1={l1_status}/expired={l1_expired} l2={l2_status} level={level}"
        )

