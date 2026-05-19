"""Tests for intent_confirm node — verifies the interrupt payload and the
post-decision state transitions.

Why payload-shape matters: the TUI renderer (tui/renderers/intent_confirm.py)
reads ``intent_confidence`` out of this dict to decide whether to draw the
low-confidence warning row. If the node forgets to forward it, the warning
silently never fires.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from chaos_agent.agent.nodes.intent_confirm import intent_confirm


def _state(**overrides):
    base = {
        "task_id": "t-confirm-1",
        "fault_intent": {
            "fault_type": "cpu-fullload",
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "namespace": "cms-demo",
        },
        "intent_confidence": 0.92,
    }
    base.update(overrides)
    return base


class TestIntentConfirmInterruptPayload:
    """interrupt() is monkey-patched to a sentinel-raising lambda that
    captures its argument; we read the dict the node tried to send."""

    @pytest.mark.asyncio
    async def test_payload_carries_intent_confidence(self):
        captured: dict = {}

        def fake_interrupt(info):
            captured.update(info)
            raise RuntimeError("interrupt-stub")

        with patch("chaos_agent.agent.nodes.intent_confirm.interrupt", fake_interrupt):
            with pytest.raises(RuntimeError, match="interrupt-stub"):
                await intent_confirm(_state())

        assert captured["type"] == "intent_confirm"
        assert captured["intent_confidence"] == pytest.approx(0.92)
        assert captured["fault_intent"]["fault_type"] == "cpu-fullload"

    @pytest.mark.asyncio
    async def test_missing_confidence_defaults_to_zero(self):
        """When upstream did not set intent_confidence (e.g. legacy paths),
        the node must coerce to 0.0 rather than propagate None — the
        renderer's ``> 0`` gate relies on a real float."""
        captured: dict = {}

        def fake_interrupt(info):
            captured.update(info)
            raise RuntimeError("interrupt-stub")

        state = _state()
        state.pop("intent_confidence")
        with patch("chaos_agent.agent.nodes.intent_confirm.interrupt", fake_interrupt):
            with pytest.raises(RuntimeError, match="interrupt-stub"):
                await intent_confirm(state)

        assert captured["intent_confidence"] == 0.0


class TestIntentConfirmDecisionRouting:

    @pytest.mark.asyncio
    async def test_approved_returns_empty_dict(self):
        with patch(
            "chaos_agent.agent.nodes.intent_confirm.interrupt",
            return_value="approved",
        ):
            result = await intent_confirm(_state())
        assert result == {}

    @pytest.mark.asyncio
    async def test_rejected_clears_intent_and_fault_intent(self):
        with patch(
            "chaos_agent.agent.nodes.intent_confirm.interrupt",
            return_value="rejected",
        ):
            result = await intent_confirm(_state())
        assert result == {"confirmed_intent": None, "fault_intent": None}
