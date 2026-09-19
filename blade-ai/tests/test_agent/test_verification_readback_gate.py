"""Read-side verification level closed-set gate (B76 round-15 D5).

The gate lives in ``read_inject_verification`` / ``read_recover_verification``
(operation_outcome.py). These tests pin the four contract clauses:

  1. Fossil-word clamp keeps every downstream verdict unchanged — the
     historical coincidence (persisted ``"failed"`` recover levels were
     treated like ``"unrecovered"`` by every consumer because neither is in
     the success set) is now an explicit contract instead of luck.
  2. In-set values pass through untouched.
  3. A missing ``level`` key passes through (legacy schema tolerance).
  4. The underlying state dict is never rewritten (read-side only).
"""

import logging

from chaos_agent.agent.result.operation_outcome import (
    read_inject_verification,
    read_recover_verification,
)
from chaos_agent.agent.result.operation_result import recover_task_state_from_values
from chaos_agent.agent.state import infer_task_state, recovery_task_state_from_level


def _recover_state(level: str | None, *, include_level: bool = True) -> dict:
    verification: dict = {"layer1": {"status": "failed"}, "layer2": {"status": "failed"}}
    if include_level:
        verification["level"] = level or ""
    return {
        "operation": "recover",
        "confirmed_intent": "inject",
        "result": {"recovered": False, "recovery_level": level or "failed"},
        "recover_verification": verification,
    }


def test_recover_fossil_failed_clamped_and_all_consumers_still_failed(caplog):
    """The legacy ``failed`` fossil clamps to ``unrecovered`` — and every
    verdict consumer (A/B/C mapping entries) still reads ``failed``.

    Pre-gate this worked by coincidence: ``"failed"`` is in no success set,
    so it accidentally behaved like ``"unrecovered"``. The gate makes that
    reading explicit; this test fails if either the clamp or the consumer
    verdict drifts.
    """
    state = _recover_state("failed")
    with caplog.at_level(logging.WARNING, logger="chaos_agent.agent.result.operation_outcome"):
        gated = read_recover_verification(state)
    assert gated["level"] == "unrecovered"
    assert any("outside closed set" in rec.message for rec in caplog.records)

    # All three verdict consumers, fed the GATED verification path.
    assert infer_task_state(state) == "failed"
    assert recover_task_state_from_values(state) == "failed"
    # Belt-and-braces: the raw fossil word also fails closed even without
    # the gate (this is the coincidence-clause pin).
    assert recovery_task_state_from_level("failed", recovered=False) == "failed"


def test_inject_fossil_word_clamped_to_unverified(caplog):
    """An inject level outside the closed set clamps to ``unverified`` —
    the same word the inject finalize write-clamp uses (mirror symmetry)."""
    state = {"verification": {"level": "strong", "layer1": {"status": "passed"}}}
    with caplog.at_level(logging.WARNING, logger="chaos_agent.agent.result.operation_outcome"):
        gated = read_inject_verification(state)
    assert gated["level"] == "unverified"
    assert any("outside closed set" in rec.message for rec in caplog.records)


def test_in_set_values_pass_through_untouched(caplog):
    """Closed-set members never clamp and never warn."""
    with caplog.at_level(logging.WARNING, logger="chaos_agent.agent.result.operation_outcome"):
        inject = read_inject_verification(
            {"verification": {"level": "verified", "layer1": {"status": "passed"}}}
        )
        recover = read_recover_verification(
            {"recover_verification": {"level": "partial", "layer1": {"status": "passed"}}}
        )
    assert inject["level"] == "verified"
    assert recover["level"] == "partial"
    assert not caplog.records


def test_missing_level_key_passes_through():
    """Schema tolerance: a verification dict without ``level`` is returned
    as-is (legacy states) — the gate only judges PRESENT values."""
    inject = read_inject_verification({"verification": {"layer1": {"status": "passed"}}})
    recover = read_recover_verification(
        {"recover_verification": {"layer1": {"status": "failed"}}}
    )
    assert inject is not None and "level" not in inject
    assert recover is not None and "level" not in recover


def test_underlying_state_never_rewritten(caplog):
    """The gate clamps its defensive copy only — the persisted/hydrated
    state keeps its original (fossil) word for audit trails."""
    state = _recover_state("failed")
    original = state["recover_verification"]["level"]
    with caplog.at_level(logging.WARNING, logger="chaos_agent.agent.result.operation_outcome"):
        read_recover_verification(state)
    assert state["recover_verification"]["level"] == original
    # The result mirror is equally untouched.
    assert state["result"]["recovery_level"] == "failed"


def test_gate_does_not_disturb_non_dict_verification():
    """Malformed verification values (non-dict) pass through as None —
    the gate adds no new failure mode."""
    assert read_inject_verification({"verification": "oops"}) is None
    assert read_recover_verification({"recover_verification": [1, 2]}) is None
