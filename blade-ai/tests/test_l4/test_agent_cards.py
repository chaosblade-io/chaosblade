"""Tests for L4ResilienceAgent v0.5.0 human-in-the-loop hooks.

These tests focus on the new APIs (clarify / step / _resolve_interrupt_decision /
_invoke_present_card) without spinning up the full LangGraph pool.
"""

import asyncio
import time
import warnings

import pytest

from chaos_agent.l4.agent import (
    DEFAULT_CARD_DECISION_TIMEOUT_S,
    L4ResilienceAgent,
)
from chaos_agent.l4.schemas import PendingCard


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ---------- _invoke_present_card ----------

class TestInvokePresentCard:
    def test_callback_returns_approved(self):
        agent = L4ResilienceAgent()

        class R:
            def present_card(self, card):
                return {"decision": "approved", "answer": None}

        card = PendingCard(card_type="intent_confirm", card_id="x", title="t", summary="s")
        decision = _run(agent._invoke_present_card(R(), card, timeout_s=5.0))
        assert decision == "approved"

    def test_callback_returns_rejected(self):
        agent = L4ResilienceAgent()

        class R:
            def present_card(self, card):
                return {"decision": "rejected", "answer": "no"}

        card = PendingCard(card_type="plan_confirm", card_id="x", title="t", summary="s")
        decision = _run(agent._invoke_present_card(R(), card, timeout_s=5.0))
        assert decision == "rejected"

    def test_callback_returns_none_means_no_callback(self):
        agent = L4ResilienceAgent()

        class R:
            def present_card(self, card):
                return None

        card = PendingCard(card_type="intent_confirm", card_id="x", title="t", summary="s")
        decision = _run(agent._invoke_present_card(R(), card, timeout_s=5.0))
        assert decision is None

    def test_callback_returns_unknown_decision_treated_as_rejected(self):
        # SDK contract: decision MUST be approved/rejected. Anything
        # else (including request_modify) maps to rejected.
        agent = L4ResilienceAgent()

        class R:
            def present_card(self, card):
                return {"decision": "request_modify", "answer": "modify"}

        card = PendingCard(card_type="intent_confirm", card_id="x", title="t", summary="s")
        decision = _run(agent._invoke_present_card(R(), card, timeout_s=5.0))
        assert decision == "rejected"

    def test_callback_async_supported(self):
        agent = L4ResilienceAgent()

        class R:
            async def present_card(self, card):
                await asyncio.sleep(0)
                return {"decision": "approved"}

        card = PendingCard(card_type="intent_confirm", card_id="x", title="t", summary="s")
        decision = _run(agent._invoke_present_card(R(), card, timeout_s=5.0))
        assert decision == "approved"

    def test_callback_timeout(self):
        agent = L4ResilienceAgent()

        class R:
            def present_card(self, card):
                time.sleep(2.0)
                return {"decision": "approved"}

        card = PendingCard(card_type="intent_confirm", card_id="x", title="t", summary="s")
        with pytest.raises(asyncio.TimeoutError):
            _run(agent._invoke_present_card(R(), card, timeout_s=0.1))


# ---------- _resolve_interrupt_decision ----------

class TestResolveInterruptDecision:
    def test_present_card_preferred_over_pre_approved(self):
        # Even with pre_approved=True, present_card decision wins.
        agent = L4ResilienceAgent()

        class R:
            def present_card(self, card):
                return {"decision": "rejected"}

        decision = _run(agent._resolve_interrupt_decision(
            runtime=R(),
            interrupt_payload={"type": "intent_confirm"},
            payload={"pre_approved": True},
            thread_id="t",
        ))
        assert decision == "rejected"

    def test_pre_approved_legacy_emits_deprecation_warning(self):
        # No present_card → fall back to pre_approved with warning.
        agent = L4ResilienceAgent()

        class R:
            pass  # no present_card, no require_approval

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            decision = _run(agent._resolve_interrupt_decision(
                runtime=R(),
                interrupt_payload={"type": "intent_confirm"},
                payload={"pre_approved": True},
                thread_id="t",
            ))
            assert decision == "approved"
            dep_warnings = [w for w in captured if issubclass(w.category, DeprecationWarning)]
            assert len(dep_warnings) >= 1
            assert "pre_approved" in str(dep_warnings[0].message)

    def test_require_approval_legacy_fallback(self):
        agent = L4ResilienceAgent()

        class R:
            def require_approval(self, risk_level="high"):
                return False

        decision = _run(agent._resolve_interrupt_decision(
            runtime=R(),
            interrupt_payload={"type": "intent_confirm"},
            payload={},
            thread_id="t",
        ))
        assert decision == "rejected"

    def test_no_callback_no_pre_approved_fail_closed(self):
        # Nothing → fail-closed rejected (was approved in <=0.4.x).
        agent = L4ResilienceAgent()
        decision = _run(agent._resolve_interrupt_decision(
            runtime=None,
            interrupt_payload={"type": "intent_confirm"},
            payload={},
            thread_id="t",
        ))
        assert decision == "rejected"

    def test_present_card_returning_none_falls_through_to_legacy(self):
        # present_card returns None → not registered → fall through to
        # pre_approved / require_approval.
        agent = L4ResilienceAgent()

        class R:
            def present_card(self, card):
                return None

            def require_approval(self, risk_level="high"):
                return True

        decision = _run(agent._resolve_interrupt_decision(
            runtime=R(),
            interrupt_payload={"type": "intent_confirm"},
            payload={},
            thread_id="t",
        ))
        assert decision == "approved"

    def test_present_card_timeout_fail_closed(self):
        agent = L4ResilienceAgent()

        class R:
            def present_card(self, card):
                time.sleep(0.5)
                return {"decision": "approved"}

        decision = _run(agent._resolve_interrupt_decision(
            runtime=R(),
            interrupt_payload={"type": "intent_confirm"},
            payload={"card_decision_timeout": 0.05},
            thread_id="t",
        ))
        # Timeout → fail-closed rejected
        assert decision == "rejected"


# ---------- step() input validation ----------

class TestStepValidation:
    def test_step_rejects_request_modify(self):
        agent = L4ResilienceAgent()
        with pytest.raises(ValueError) as exc:
            agent.step("thread-1", {"decision": "request_modify"})
        assert "approved" in str(exc.value)
        assert "rejected" in str(exc.value)
        assert "platform layer" in str(exc.value)

    def test_step_rejects_empty_decision(self):
        agent = L4ResilienceAgent()
        with pytest.raises(ValueError):
            agent.step("thread-1", {})

    def test_step_rejects_unknown_decision(self):
        agent = L4ResilienceAgent()
        with pytest.raises(ValueError):
            agent.step("thread-1", {"decision": "maybe"})


# ---------- defaults ----------

class TestDefaults:
    def test_default_timeout(self):
        assert DEFAULT_CARD_DECISION_TIMEOUT_S == 600.0
