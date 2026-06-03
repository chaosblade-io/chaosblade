"""Tests covering the two-layer interrupt fix in /turn (intent_confirm + confirmation_gate).

Background. The original turn.py only watched for ``confirmation_gate``
in ``state.next``, which silently skipped the ``intent_confirm`` (Layer 1)
pause and let ``_build_result_payload`` serialise mid-flow state as a
phantom ``Injection failed`` result. Fix: generic interrupt detection
via ``state.tasks[*].interrupts[*].value`` plus a ``while`` loop that
drains every pause until the graph reaches END.

These tests pin the four behaviours that together prove the fix:

  1. ``_extract_pending_interrupt`` reads from the right LangGraph
     state shape and is node-name agnostic.
  2. ``_content_from_interrupt_payload`` falls through summary →
     plan_summary → JSON dump in that order.
  3. ``_normalise_answer`` accepts the documented Y/yes/y/ok aliases
     and rejects everything else.
  4. ``_build_result_payload`` refuses to serialise while the graph is
     still paused (``state.next`` non-empty), so a missed pending
     interrupt (future regression) cannot reproduce the original bug.

Tests 1-3 are pure-function units. Test 4 mocks just enough of
``aget_state``'s return shape to exercise the guard.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chaos_agent.server.routes.turn import (
    _build_result_payload,
    _content_from_interrupt_payload,
    _extract_pending_interrupt,
    _normalise_answer,
)


# ---------------------------------------------------------------------------
# _extract_pending_interrupt
# ---------------------------------------------------------------------------


def _make_state(tasks):
    """Build a minimal mock with the attribute shape LangGraph returns.

    ``aget_state`` returns a ``StateSnapshot`` with ``.tasks`` and ``.next``;
    each task carries ``.name`` and ``.interrupts``; each interrupt has
    ``.value``. Real LangGraph types would also have many other fields,
    but ``_extract_pending_interrupt`` only touches those three.
    """
    return SimpleNamespace(tasks=tasks, next=("placeholder",), values={})


def _make_task(name, interrupt_values):
    return SimpleNamespace(
        name=name,
        interrupts=tuple(SimpleNamespace(value=v) for v in interrupt_values),
    )


class TestExtractPendingInterrupt:
    def test_none_state_returns_none(self):
        assert _extract_pending_interrupt(None) is None

    def test_empty_tasks_returns_none(self):
        assert _extract_pending_interrupt(_make_state([])) is None

    def test_task_without_interrupts_returns_none(self):
        # Pause point that isn't an interrupt — defense-in-depth path.
        state = _make_state([_make_task("baseline_capture", [])])
        assert _extract_pending_interrupt(state) is None

    def test_intent_confirm_payload_dict(self):
        payload = {
            "type": "intent_confirm",
            "fault_intent": {"fault_type": "node-cpu-fullload"},
            "summary": "故障类型: node-cpu-fullload\n范围: node\n...",
            "intent_confidence": 0.92,
        }
        state = _make_state([_make_task("intent_confirm", [payload])])

        result = _extract_pending_interrupt(state)
        assert result == ("intent_confirm", payload)

    def test_confirmation_gate_payload_dict(self):
        payload = {
            "skill_name": "node-cpu-fullload",
            "target": {"namespace": "cms-demo", "names": ["node-1"]},
            "plan_summary": "blade create node cpu fullload --names node-1",
            "safety_status": "safe",
            "safety_reason": None,
        }
        state = _make_state([_make_task("confirmation_gate", [payload])])

        result = _extract_pending_interrupt(state)
        assert result == ("confirmation_gate", payload)

    def test_first_pending_wins_when_multiple_tasks(self):
        # If somehow two tasks both had interrupts, return the first
        # (declaration order). The current graph never produces this,
        # but the contract should be deterministic.
        first_payload = {"summary": "first"}
        second_payload = {"summary": "second"}
        state = _make_state(
            [
                _make_task("intent_confirm", [first_payload]),
                _make_task("confirmation_gate", [second_payload]),
            ]
        )

        result = _extract_pending_interrupt(state)
        assert result == ("intent_confirm", first_payload)

    def test_non_dict_payload_is_wrapped(self):
        # Every interrupt() call site in this codebase uses a dict, but
        # the helper should not crash on a stray string/int and should
        # surface it as ``{"value": payload}`` so callers stay uniform.
        state = _make_state([_make_task("future_node", ["raw string"])])

        result = _extract_pending_interrupt(state)
        assert result == ("future_node", {"value": "raw string"})

    def test_skips_none_value_entries(self):
        # An interrupt entry with value=None means "already resolved";
        # walk past it to find the next pending one.
        live_payload = {"summary": "live"}
        state = _make_state(
            [
                _make_task("noop", [None]),
                _make_task("intent_confirm", [live_payload]),
            ]
        )

        result = _extract_pending_interrupt(state)
        assert result == ("intent_confirm", live_payload)


# ---------------------------------------------------------------------------
# _content_from_interrupt_payload
# ---------------------------------------------------------------------------


class TestContentFromInterruptPayload:
    def test_intent_confirm_uses_summary(self):
        payload = {
            "summary": "故障类型: node-cpu-fullload",
            "fault_intent": {"some": "data"},
        }
        assert _content_from_interrupt_payload(payload) == "故障类型: node-cpu-fullload"

    def test_confirmation_gate_uses_plan_summary(self):
        payload = {
            "plan_summary": "blade create node cpu fullload",
            "safety_status": "safe",
        }
        assert (
            _content_from_interrupt_payload(payload)
            == "blade create node cpu fullload"
        )

    def test_summary_wins_over_plan_summary_when_both_present(self):
        # Defensive: a future combined node could carry both. Pick the
        # narrower (intent) summary since it's the more user-facing one
        # in the current call sites.
        payload = {"summary": "S", "plan_summary": "P"}
        assert _content_from_interrupt_payload(payload) == "S"

    def test_falls_back_to_json_dump_when_keys_absent(self):
        payload = {"unexpected_field": "value"}
        result = _content_from_interrupt_payload(payload)
        # Round-trip parse to assert it's valid JSON without pinning
        # whitespace.
        parsed = json.loads(result)
        assert parsed == {"unexpected_field": "value"}


# ---------------------------------------------------------------------------
# _normalise_answer
# ---------------------------------------------------------------------------


class TestNormaliseAnswer:
    @pytest.mark.parametrize(
        "raw",
        ["approved", "yes", "y", "ok", "Y", "Yes", " approved ", "OK"],
    )
    def test_approval_aliases(self, raw):
        assert _normalise_answer(raw) == "approved"

    @pytest.mark.parametrize(
        "raw",
        ["rejected", "no", "n", "", "maybe", "Y\nbutwait", "approve_later"],
    )
    def test_rejection_default(self, raw):
        # Anything that's not in the approval whitelist → rejected.
        # Includes the empty string (timed-out client send) and odd
        # multi-line answers.
        assert _normalise_answer(raw) == "rejected"


# ---------------------------------------------------------------------------
# _build_result_payload — pause guard
# ---------------------------------------------------------------------------


class TestBuildResultPayloadPauseGuard:
    """The new ``if final_state.next: return None`` line is the second
    layer of defence. If a future change causes the interrupt-loop to
    miss a pending pause, this still prevents the original phantom
    ``Injection failed`` ResultCard from leaking out.
    """

    @pytest.mark.asyncio
    async def test_returns_none_when_graph_still_paused(self):
        graph = AsyncMock()
        graph.aget_state.return_value = SimpleNamespace(
            values={
                "confirmed_intent": "inject",
                "blade_uid": "",  # mid-flow, no real injection yet
                "fault_intent": {"fault_type": "node-cpu-fullload"},
            },
            next=("intent_confirm",),  # still paused
            tasks=[],
        )
        result = await _build_result_payload(graph, {}, "task-abc", 0.0)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_payload_when_graph_finished_and_intent_inject(self):
        graph = AsyncMock()
        graph.aget_state.return_value = SimpleNamespace(
            values={
                "confirmed_intent": "inject",
                "blade_uid": "blade-uid-xyz",
                "task_id": "task-abc",
                "params": {
                    "scope": "node",
                    "target": "cpu",
                    "action": "fullload",
                    "namespace": "cms-demo",
                },
                "skill_name": "node-cpu-fullload",
            },
            next=(),  # graph finished
            tasks=[],
        )
        result = await _build_result_payload(graph, {}, "task-abc", 0.0)
        assert result is not None
        assert result["status"] == "success"
        assert result["data"]["task_id"] == "task-abc"
        assert result["data"]["fault_type"] == "node-cpu-fullload"
        assert result["data"]["blade_uid"] == "blade-uid-xyz"

    @pytest.mark.asyncio
    async def test_returns_none_when_intent_not_inject_or_recover(self):
        # Pure-chat turn — no result envelope even if graph is finished.
        # This is the existing behaviour; the test pins it to make sure
        # the new pause guard above didn't accidentally widen the
        # condition.
        graph = AsyncMock()
        graph.aget_state.return_value = SimpleNamespace(
            values={"confirmed_intent": "chat"},
            next=(),
            tasks=[],
        )
        result = await _build_result_payload(graph, {}, "task-abc", 0.0)
        assert result is None
