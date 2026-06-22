from chaos_agent.agent.intent_handoff import (
    build_pipeline_handoff_from_intent_state,
    clear_dispatched_operation_payload_update,
    detect_dispatchable_operation,
)


def test_clear_dispatched_operation_payload_update_shape():
    update = clear_dispatched_operation_payload_update()

    assert update == {
        "confirmed_intent": None,
        "batch_submit_args": None,
        "fault_spec": None,
        "handoff_summary": None,
        "intent_reasoning": None,
        "intent_confidence": 0.0,
        "clarification_round": 0,
    }


def test_detect_dispatchable_operation_respects_interrupts():
    assert detect_dispatchable_operation(
        {"confirmed_intent": "inject", "fault_spec": {"scope": "pod"}},
    ) == "inject"
    assert detect_dispatchable_operation(
        {"confirmed_intent": "batch_inject", "batch_submit_args": {"faults": []}},
    ) == "batch_inject"
    assert detect_dispatchable_operation(
        {"confirmed_intent": "inject", "fault_spec": {"scope": "pod"}},
        has_pending_interrupt=True,
    ) is None
    assert detect_dispatchable_operation({"confirmed_intent": "inject"}) is None


def test_build_pipeline_handoff_copies_single_inject_payload():
    fault_spec = {"scope": "pod", "names": ["pod-a"]}
    intent_state = {
        "tui_session_id": "sid-1",
        "handoff_summary": "summary",
        "fault_spec": fault_spec,
    }

    handoff = build_pipeline_handoff_from_intent_state(
        intent_state,
        operation="inject",
        task_id="task-1",
        default_tui_session_id="fallback",
    )

    fault_spec["names"].append("pod-b")

    assert handoff.operation == "inject"
    assert handoff.task_id == "task-1"
    assert handoff.tui_session_id == "sid-1"
    assert handoff.handoff_summary == "summary"
    assert handoff.fault_spec == {"scope": "pod", "names": ["pod-a"]}
    assert handoff.batch_submit_args is None


def test_build_pipeline_handoff_copies_batch_payload_and_falls_back_sid():
    batch_args = {"faults": [{"scope": "pod"}]}
    intent_state = {
        "tui_session_id": "",
        "fault_spec": {"scope": "pod"},
        "batch_submit_args": batch_args,
    }

    handoff = build_pipeline_handoff_from_intent_state(
        intent_state,
        operation="batch_inject",
        task_id="task-batch",
        default_tui_session_id="sid-fallback",
    )

    batch_args["faults"][0]["scope"] = "node"

    assert handoff.operation == "batch_inject"
    assert handoff.tui_session_id == "sid-fallback"
    assert handoff.fault_spec == {"scope": "pod"}
    assert handoff.batch_submit_args == {"faults": [{"scope": "pod"}]}
