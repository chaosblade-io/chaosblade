"""Adversarial matrix for the durable-evidence verdict floor (a82984a3 root cure).

The a82984a3 bug class is a provenance inversion: the terminal word of a run
was sourced from the LEAST durable artifact (the LLM's ``submit_verification``
tool-call ARGS, which must survive the stream) instead of the MOST durable one
(the programmatic Layer-1 measurement already persisted in the checkpoint).
When an ``httpx.ReadTimeout`` killed the verifier loop mid-adjudication, the
crash arm hard-wrote ``result="failed"`` — labelling a successful injection /
recovery as failed, which the CLI reads as "cluster unaffected".

The cure legislates an invariant: **the terminal word must be derivable from
durable state by a deterministic synthesizer that always runs; the LLM verdict
only ever REFINES it.** These tests pin that invariant on BOTH arms (inject +
recover) and, on the recover side, on BOTH of its faces:

  * the VERDICT face — ``recover_task_state_from_values`` (Route B), which
    fail-closes to "failed" with no verdict;
  * the POSITION face — ``build_recover_session_summary``'s CLI envelope
    (Route A), which fail-closes to "recovering" with no verdict.

Round-64 F1 deliberately keeps the two recover faces DIFFERENT when there is
no evidence (session status "failed" vs envelope result "recovering" — "the
two faces do not contradict each other"). The cure does NOT unify them: it
lifts each face to the honest floor word ("unverified") only when durable
Layer-1 proof the destroy LANDED exists, and leaves each face's own fail-closed
word standing when it does not. So this matrix pins the LIFT and the
NO-EVIDENCE-NO-UPGRADE boundary on each face independently.

Polarity invariant (both arms): the floor is CONSERVATIVE — the inject floor
never claims "verified", the recover floor never claims "recovered". A floor
can only spell honest ignorance, never a success it cannot prove.

Persistence-surface closure (server arm): the recover verdict face (Route B)
is consumed by TWO persistence surfaces — the local CLI envelope AND the
server/l4 PAYLOAD card (``RESULT_SUMMARY_RECOVER_PAYLOAD``). The PAYLOAD arm
used to echo a caller-pre-built card and persist "" when none existed, which is
exactly every crash/abort path (the server only builds a card on normal
completion) — so a server-side recover crash AFTER the destroy landed persisted
"" + a generic "Recovery failed", bypassing the floor entirely while the local
CLI spelled "unverified". The cure derives the PAYLOAD card from durable state
through the SAME Route B renderer, so NO recover persistence surface — present
or future — can bypass the floor (mirrors ``finalize_inject_session``, which
always rebuilds via ``build_inject_data_from_state`` before mode dispatch).
Section 6 pins that surface; section 7 pins the cross-face convergence the
whole cure rests on.
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.result.operation_outcome import (
    synthesize_inject_verification_floor,
    synthesize_recover_verification_floor,
)
from chaos_agent.agent.result.operation_result import (
    build_recover_cli_failure_data_from_state,
    recover_task_state_from_values,
)
from chaos_agent.agent.state import terminal_task_state
from chaos_agent.memory.session_finalizer import (
    RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
    RESULT_SUMMARY_RECOVER_PAYLOAD,
    build_recover_session_summary,
)


# ---------------------------------------------------------------------------
# Durable-cache fixtures (plain dicts — the shape layer1_to_dict persists and
# Layer1Result.model_validate rehydrates; missing keys take model defaults).
# ---------------------------------------------------------------------------

_L1_PASSED = {"status": "passed", "affected_count": 1}
_L1_FAILED = {"status": "failed"}
_L1_SKIPPED = {"status": "skipped"}
_L1_IN_PROGRESS = {"status": "in_progress"}
_L1_ERROR = {"status": "error"}
_L1_EXPIRED_PASSED = {"status": "passed", "expired": True}


def _inject_state(cache=None, verification=None):
    values = {"experiment_uid": "uid-inject-1"}
    if cache is not None:
        values["inject_layer1_cache"] = cache
    if verification is not None:
        values["verification"] = verification
    return values


def _recover_state(cache=None, verification=None, result=None):
    values = {"operation": "recover", "confirmed_intent": "inject"}
    if cache is not None:
        values["recover_layer1_cache"] = cache
    if verification is not None:
        values["recover_verification"] = verification
    if result is not None:
        values["result"] = result
    return values


def _inject_values_for_summary():
    return {"experiment_uid": "uid-inject-1", "fault_spec": {}}


# ---------------------------------------------------------------------------
# 1. INJECT verdict floor via terminal_task_state (points 1-6 — already wired;
#    these lock the cure so a future edit cannot silently regress it).
# ---------------------------------------------------------------------------


class TestInjectCrashFloor:
    def test_passed_cache_no_verdict_reads_injected_not_failed(self):
        """THE a82984a3 cure (run⑥): injection landed, Layer 1 confirmed the
        experiment, but the LLM verdict never survived the stream. The durable
        evidence must read "injected", never the fail-closed "failed"."""
        assert terminal_task_state(_inject_state(cache=_L1_PASSED)) == "injected"

    @pytest.mark.parametrize(
        "cache",
        [_L1_FAILED, _L1_IN_PROGRESS, _L1_ERROR, _L1_EXPIRED_PASSED],
    )
    def test_non_passing_cache_stays_fail_closed(self, cache):
        """No positive durable proof ⇒ the fail-closed word stands. (An expired
        passed experiment with no side-effect confirmation is not "injected" —
        the fault record was gone before observation.) These are all Layer1
        shapes that were ATTEMPTED and measured counter-evidence / crashed /
        expired — distinct from _L1_SKIPPED below, which is skipped *by design*
        for a native fault that is presumptively still live."""
        assert terminal_task_state(_inject_state(cache=cache)) == "failed"

    def test_skipped_native_cache_no_verdict_reads_unverified_not_failed(self):
        """B88: the native/faultdrill-carrier twin of the run⑥ cure. Layer 1 is
        ``skipped`` BY DESIGN for a non-ChaosBlade fault ("the recipe landing is
        bookkeeping, not the fault landing — Layer-2 is the authority"), so the
        floor emits ``level="unverified"`` + ``layer2.status="skipped"``. When
        an ``httpx.ReadTimeout`` cuts the verifier AFTER the fault landed, the
        terminal word must be the floor's honest "unverified" — NOT "failed",
        which the CLI reads as "cluster unaffected" while the live carrier still
        owes recovery (the a82984a3 dangerous false-negative, native arm).
        "unverified" never claims the strongest word, so the polarity invariant
        holds; it only refuses to spell a counter-evidence the run does not
        have. Mirrors the ChaosBlade ``_L1_PASSED`` cure above via the same
        single-source floor."""
        assert terminal_task_state(_inject_state(cache=_L1_SKIPPED)) == "unverified"

    def test_absent_cache_stays_fail_closed(self):
        """Crash before ANY verifier iteration ⇒ no evidence, no upgrade."""
        assert terminal_task_state(_inject_state(cache=None)) == "failed"

    def test_real_verdict_is_never_overwritten_by_the_floor(self):
        """A genuine Layer-2 "failed" verdict must stay "failed" even though a
        passed Layer-1 cache would floor it up to "injected". The floor only
        fills an ABSENT verdict; it never second-guesses a real one."""
        values = _inject_state(
            cache=_L1_PASSED,
            verification={
                "level": "unverified",
                "layer1": {"status": "passed"},
                "layer2": {"status": "failed"},
            },
        )
        assert terminal_task_state(values) == "failed"


# ---------------------------------------------------------------------------
# 2. RECOVER verdict face (Route B) via recover_task_state_from_values.
#    RED until c4 wires the floor consult.
# ---------------------------------------------------------------------------


class TestRecoverVerdictFaceFloor:
    def test_passed_destroy_no_verdict_reads_unverified_not_failed(self):
        """Recover twin of the run⑥ cure: the destroy LANDED (Layer 1 confirms
        the experiment record is gone) but the Layer-2 return-to-baseline
        verdict never completed. Honest word = "unverified", not "failed"."""
        assert recover_task_state_from_values(
            _recover_state(cache=_L1_PASSED, result={"recovered": False})
        ) == "unverified"

    @pytest.mark.parametrize(
        "cache", [_L1_FAILED, _L1_SKIPPED, _L1_IN_PROGRESS, _L1_ERROR]
    )
    def test_non_passed_destroy_stays_failed(self, cache):
        """The recover floor is gated on layer1.is_passed(): a failed /
        in-progress / skipped destroy is a KNOWN non-landing, and
        recovery_task_state_from_level maps level=="unverified" → UNVERIFIED
        unconditionally — flooring those would under-report a known failure as
        mere ignorance. They must stay "failed"."""
        assert recover_task_state_from_values(
            _recover_state(cache=cache, result={"recovered": False})
        ) == "failed"

    def test_absent_cache_stays_failed(self):
        assert recover_task_state_from_values(
            _recover_state(cache=None, result={"recovered": False})
        ) == "failed"

    def test_real_recovered_verdict_is_not_downgraded(self):
        """A real "recovered" verdict on record must survive — the floor only
        ever fires on the fail-closed path, never over evidence."""
        assert recover_task_state_from_values(
            _recover_state(
                cache=_L1_PASSED,
                verification={
                    "level": "recovered",
                    "layer1": {"status": "passed"},
                    "layer2": {"status": "passed"},
                },
                result={"recovered": True, "recovery_level": "recovered"},
            )
        ) == "recovered"

    def test_real_unrecovered_counter_evidence_is_not_upgraded(self):
        """A real "unrecovered" (counter-evidence: fault persists) verdict
        resolves to "failed" and must NOT be lifted to "unverified" by the
        floor — the destroy being confirmed does not erase the LLM's finding
        that the fault is still present."""
        assert recover_task_state_from_values(
            _recover_state(
                cache=_L1_PASSED,
                verification={
                    "level": "unrecovered",
                    "layer1": {"status": "passed"},
                },
                result={"recovered": False},
            )
        ) == "failed"

    def test_deliberate_timeout_verdict_is_not_overridden(self):
        """W-56-8 boundary: a recover verifier CUT by its wall-clock / loop
        budget writes ``failure_reason`` (RECOVERY_VERIFICATION_TIMEOUT) — a
        deliberate terminal classification, not a crash. Even with a passed
        destroy cache, the floor must NOT lift it: the node has spoken
        "failure", and the floor only fills the SILENCE a crash leaves (no
        verdict, no reason). This is what separates the a82984a3 cure from the
        budget-cut contract."""
        state = _recover_state(cache=_L1_PASSED, result={"recovered": False})
        state["failure_reason"] = "recovery_verification_timeout"
        state["error"] = "recover verifier loop budget exhausted (60/60)"
        assert recover_task_state_from_values(state) == "failed"


# ---------------------------------------------------------------------------
# 3. RECOVER position face (Route A) via build_recover_session_summary's CLI
#    envelope. RED until c5 wires the lift; the no-evidence rows must STAY
#    "recovering" (round-64 F1 preserved).
# ---------------------------------------------------------------------------


def _recover_summary(recover_values):
    return build_recover_session_summary(
        recover_values,
        recover_task_id="task-recover",
        inject_task_id="task-inject",
        inject_state_values=_inject_values_for_summary(),
        mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
    )


class TestRecoverPositionFaceFloor:
    def test_passed_destroy_no_verdict_lifts_recovering_to_unverified(self):
        """The position face says "recovering" (where the run is). With durable
        proof the destroy landed, the honest position is "unverified" — the run
        ended mid-adjudication AFTER the recovery action, not mid-flight."""
        summary = _recover_summary(_recover_state(cache=_L1_PASSED))
        assert summary["data"]["result"] == "unverified"

    def test_no_cache_stays_recovering(self):
        """Round-64 F1 preserved verbatim: no durable evidence ⇒ the position
        face keeps its own fail-closed word "recovering" (the verdict face says
        "failed"; the two faces do not contradict — neither claims success)."""
        summary = _recover_summary(_recover_state(cache=None))
        assert summary["data"]["result"] == "recovering"

    @pytest.mark.parametrize(
        "cache", [_L1_FAILED, _L1_SKIPPED, _L1_IN_PROGRESS, _L1_ERROR]
    )
    def test_non_passed_destroy_stays_recovering(self, cache):
        """Same gate as the verdict face: no confirmed landing, no lift."""
        summary = _recover_summary(_recover_state(cache=cache))
        assert summary["data"]["result"] == "recovering"

    def test_real_verdict_word_is_used(self):
        summary = _recover_summary(
            _recover_state(
                verification={
                    "level": "recovered",
                    "layer1": {"status": "passed"},
                    "layer2": {"status": "passed"},
                },
                result={"recovered": True, "recovery_level": "recovered"},
            )
        )
        assert summary["data"]["result"] == "recovered"

    def test_deliberate_timeout_keeps_the_position_face(self):
        """Both faces honour a deliberate verdict: the budget-cut
        ``failure_reason`` (+ its ``error``) means the position face is NEVER
        lifted to "unverified". Here infer_task_state's error branch fires
        before its recover branch, so the position word is "failed" — the same
        face value Route B produces. Route A and Route B gate on the SAME
        signal and cannot diverge on a run the node classified."""
        state = _recover_state(cache=_L1_PASSED)
        state["failure_reason"] = "wall_clock_timeout"
        state["error"] = "recover verifier wall clock exhausted"
        summary = _recover_summary(state)
        assert summary["data"]["result"] != "unverified"
        assert summary["data"]["result"] == "failed"
        assert summary["status"] == "fail"


# ---------------------------------------------------------------------------
# 4. RECOVER crash arm builder via build_recover_cli_failure_data_from_state.
#    RED until c6 adds the recover-checkpoint parameter; the builder used to
#    hard-code {"result": {"recovered": False}} and never read the recover
#    graph's final checkpoint at all (worse than the pre-cure inject arm).
# ---------------------------------------------------------------------------


class TestRecoverCrashArmBuilder:
    def test_passed_destroy_checkpoint_reads_unverified(self):
        data = build_recover_cli_failure_data_from_state(
            "task-inject",
            _inject_values_for_summary(),
            experiment_uid="uid-inject-1",
            error="internal_error: Recovery failed: boom",
            recover_state_values=_recover_state(cache=_L1_PASSED),
        )
        assert data["result"] == "unverified"

    def test_no_checkpoint_stays_failed(self):
        """No recover checkpoint (aget_state failed / crashed before any Layer 1)
        ⇒ the fail-closed "failed" label stands — no evidence, no upgrade."""
        data = build_recover_cli_failure_data_from_state(
            "task-inject",
            _inject_values_for_summary(),
            experiment_uid="uid-inject-1",
            error="internal_error: Recovery failed: boom",
        )
        assert data["result"] == "failed"

    def test_non_passed_destroy_checkpoint_stays_failed(self):
        data = build_recover_cli_failure_data_from_state(
            "task-inject",
            _inject_values_for_summary(),
            experiment_uid="uid-inject-1",
            error="internal_error: Recovery failed: boom",
            recover_state_values=_recover_state(cache=_L1_FAILED),
        )
        assert data["result"] == "failed"


# ---------------------------------------------------------------------------
# 5. Polarity invariant — the floor NEVER over-claims a success word.
# ---------------------------------------------------------------------------


class TestFloorNeverOverClaims:
    def test_inject_floor_level_never_verified(self):
        for cache in (_L1_PASSED, _L1_FAILED, _L1_SKIPPED, _L1_EXPIRED_PASSED):
            floor = synthesize_inject_verification_floor(
                _inject_state(cache=cache)
            )
            assert floor is not None
            assert floor["level"] in {"partial", "unverified"}
            assert floor["level"] != "verified"

    def test_recover_floor_level_is_unverified_only_when_passed(self):
        floor = synthesize_recover_verification_floor(
            _recover_state(cache=_L1_PASSED)
        )
        assert floor is not None
        assert floor["level"] == "unverified"
        assert floor["level"] != "recovered"

    @pytest.mark.parametrize(
        "cache", [_L1_FAILED, _L1_SKIPPED, _L1_IN_PROGRESS, _L1_ERROR]
    )
    def test_recover_floor_is_none_for_non_passed_destroy(self, cache):
        assert synthesize_recover_verification_floor(
            _recover_state(cache=cache)
        ) is None

    def test_absent_cache_yields_no_floor_both_arms(self):
        assert synthesize_inject_verification_floor(_inject_state()) is None
        assert synthesize_recover_verification_floor(_recover_state()) is None


# ---------------------------------------------------------------------------
# 6. RECOVER server/l4 PAYLOAD persistence surface (Route B's second consumer).
#    RED until the PAYLOAD arm derived its card from durable state: it used to
#    persist "" on every crash/abort path (no caller-built payload), bypassing
#    the floor — the server twin of the a82984a3 bug the local CLI already cured.
# ---------------------------------------------------------------------------


def _recover_payload_summary(recover_values):
    return build_recover_session_summary(
        recover_values,
        recover_task_id="task-recover",
        inject_task_id="task-inject",
        inject_state_values=_inject_values_for_summary(),
        mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
    )


class TestRecoverServerPayloadFaceFloor:
    def test_passed_destroy_no_verdict_payload_reads_unverified(self):
        """THE server-arm cure: a server-side recover crash AFTER the destroy
        landed (durable Layer-1 proof, no surviving verdict) used to persist ""
        + a generic "Recovery failed". It must now spell "unverified" — the same
        word the local CLI gives for the identical checkpoint."""
        summary = _recover_payload_summary(_recover_state(cache=_L1_PASSED))
        assert summary["data"]["task_state"] == "unverified"
        assert summary["status"] == "success"

    def test_no_cache_payload_fail_closes_failed(self):
        """No durable evidence ⇒ the verdict face fail-closes to "failed" and
        the envelope polarity is fail — never a false success card."""
        summary = _recover_payload_summary(_recover_state(cache=None))
        assert summary["data"]["task_state"] == "failed"
        assert summary["status"] == "fail"

    @pytest.mark.parametrize(
        "cache", [_L1_FAILED, _L1_SKIPPED, _L1_IN_PROGRESS, _L1_ERROR]
    )
    def test_non_passed_destroy_payload_stays_failed(self, cache):
        """Same Route B gate as every other face: no confirmed landing, no lift."""
        summary = _recover_payload_summary(_recover_state(cache=cache))
        assert summary["data"]["task_state"] == "failed"

    def test_empty_state_payload_stays_honest_empty(self):
        """round-64 F1 boundary preserved on the server face too: empty values
        (aget_state failed) keep the honest "" — no evidence, no upgrade."""
        assert _recover_payload_summary({}) == ""

    def test_real_verdict_payload_is_used(self):
        """A live verdict is never downgraded by the floor."""
        summary = _recover_payload_summary(
            _recover_state(
                verification={
                    "level": "recovered",
                    "layer1": {"status": "passed"},
                    "layer2": {"status": "passed"},
                },
                result={"recovered": True, "recovery_level": "recovered"},
            )
        )
        assert summary["data"]["task_state"] == "recovered"

    def test_deliberate_timeout_payload_not_lifted(self):
        """W-56-8 honoured on the server face: a budget-cut recovery the node
        classified (failure_reason on record) is NEVER lifted to "unverified" —
        the floor fills only the silence a crash leaves."""
        state = _recover_state(cache=_L1_PASSED, result={"recovered": False})
        state["failure_reason"] = "recovery_verification_timeout"
        state["error"] = "recover verifier loop budget exhausted (60/60)"
        summary = _recover_payload_summary(state)
        assert summary["data"]["task_state"] == "failed"
        assert summary["status"] == "fail"


# ---------------------------------------------------------------------------
# 7. Cross-face convergence — the invariant the WHOLE cure rests on. The local
#    CLI (Route A position face) and the server/l4 PAYLOAD card (Route B verdict
#    face) are DIFFERENT routes to the terminal word; for a floor-eligible crash
#    they MUST converge on "unverified", or the same checkpoint would read
#    differently depending on which surface persisted it (the exact local/server
#    divergence the cure closes). For a no-evidence crash they deliberately keep
#    their own fail-closed words (round-64 F1) — but neither may claim success.
# ---------------------------------------------------------------------------


class TestRecoverCrossFaceConvergence:
    def test_passed_destroy_crash_all_faces_converge_unverified(self):
        state = _recover_state(cache=_L1_PASSED)
        route_a = _recover_summary(state)["data"]["result"]          # CLI envelope
        route_b = recover_task_state_from_values(state)              # verdict face
        payload = _recover_payload_summary(state)["data"]["task_state"]  # server card
        assert route_a == route_b == payload == "unverified"

    def test_no_cache_crash_faces_keep_own_word_never_success(self):
        """round-64 F1: with no evidence the two faces stay DIFFERENT (position
        "recovering" vs verdict "failed") — the cure does not unify them. The
        invariant here is weaker and load-bearing: NEITHER face, on EITHER
        persistence surface, may claim a success word it cannot prove."""
        state = _recover_state(cache=None)
        route_a = _recover_summary(state)["data"]["result"]
        payload = _recover_payload_summary(state)["data"]["task_state"]
        assert route_a == "recovering"
        assert payload == "failed"
        success_words = {"recovered", "partial_recovered", "unverified", "injected"}
        assert route_a not in success_words
        assert payload not in success_words
