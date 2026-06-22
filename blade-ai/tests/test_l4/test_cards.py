"""Tests for chaos_agent.l4.cards — interrupt → PendingCard adapter (v0.5.0)."""

from chaos_agent.l4.cards import interrupt_to_card
from chaos_agent.l4.schemas import PendingCard


class TestIntentConfirmAdapter:
    def test_intent_confirm_payload(self):
        payload = {
            "type": "intent_confirm",
            "fault_intent": {
                "fault_type": "cpu-fullload",
                "namespace": "cms-demo",
                "names": ["pod-1"],
            },
            "summary": "cpu满载注入",
            "intent_confidence": 0.92,
            "clarification_round": 1,
            "intent_reasoning": "user explicit",
            "batch_faults": None,
        }
        card = interrupt_to_card(payload, "thread-1")

        assert isinstance(card, PendingCard)
        assert card.card_type == "intent_confirm"
        assert card.thread_id == "thread-1"
        assert card.card_id.startswith("intent_confirm-thread-1-")
        # request_modify is allowed only for intent_confirm
        assert "request_modify" in card.decision_options
        assert "approved" in card.decision_options
        assert "rejected" in card.decision_options
        assert "cpu-fullload" in card.title
        assert "cms-demo" in card.title
        assert card.details["intent_confidence"] == 0.92
        assert card.details["clarification_round"] == 1
        assert card.details["fault_intent"]["fault_type"] == "cpu-fullload"

    def test_intent_confirm_minimal(self):
        # Missing optional fields — must not crash
        card = interrupt_to_card({"type": "intent_confirm"}, "t")
        assert card.card_type == "intent_confirm"
        assert card.details["fault_intent"] == {}


class TestPlanConfirmAdapter:
    def test_confirmation_gate_no_type_field(self):
        # confirmation_gate payload has NO ``type`` field; recognised by
        # safety_status + plan_summary co-presence.
        payload = {
            "skill_name": "cpu-fullload-by-pod",
            "fault_intent": {"fault_type": "cpu-fullload"},
            "target": "pod/foo",
            "plan_summary": "step 1: ...",
            "safety_status": "safe",
            "safety_reason": None,
            "params": {"percent": "80"},
        }
        card = interrupt_to_card(payload, "t1")
        assert card.card_type == "plan_confirm"
        # plan_confirm CANNOT request_modify
        assert "request_modify" not in card.decision_options
        assert card.decision_options == ["approved", "rejected"]
        assert card.details["skill_name"] == "cpu-fullload-by-pod"
        assert card.details["safety_status"] == "safe"


class TestPlanChangeAdapter:
    def test_plan_change(self):
        payload = {
            "type": "plan_change",
            "reason": "target has no cpu",
            "original": {"fault_type": "cpu", "scope": "pod"},
            "proposed": {"fault_type": "mem", "scope": "pod"},
        }
        card = interrupt_to_card(payload, "t")
        assert card.card_type == "plan_change"
        assert "request_modify" not in card.decision_options
        assert "cpu" in card.title
        assert "mem" in card.title
        assert card.details["reason"] == "target has no cpu"


class TestToolDriftAdapter:
    def test_target_change(self):
        payload = {
            "type": "target_change",
            "summary": "drift detected",
            "reason": "wrong namespace",
            "agent_reason": "i thought it was default",
            "original": {"namespace": "cms"},
            "proposed": {"namespace": "default"},
            "tool_calls": [{"name": "kubectl", "reason": "ns switch"}],
        }
        card = interrupt_to_card(payload, "t")
        assert card.card_type == "tool_drift"
        assert "request_modify" not in card.decision_options
        assert card.details["agent_reason"] == "i thought it was default"
        assert card.details["tool_calls"][0]["name"] == "kubectl"


class TestUnknownAdapter:
    def test_unrecognised_dict(self):
        card = interrupt_to_card({"foo": "bar"}, "t")
        assert card.card_type == "unknown"
        assert card.details["raw_payload"] == {"foo": "bar"}

    def test_non_dict_payload(self):
        card = interrupt_to_card("plain string", "t")
        assert card.card_type == "unknown"

    def test_none_payload(self):
        card = interrupt_to_card(None, "t")
        assert card.card_type == "unknown"


class TestCardIdUnique:
    def test_card_ids_unique(self):
        payload = {"type": "intent_confirm", "fault_intent": {}}
        ids = {interrupt_to_card(payload, "t").card_id for _ in range(20)}
        assert len(ids) == 20  # uuid suffix guarantees uniqueness
