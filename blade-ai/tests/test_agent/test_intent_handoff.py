from chaos_agent.agent.intent_handoff import (
    build_pipeline_handoff_from_intent_state,
    clear_dispatched_operation_payload_update,
    detect_dispatchable_operation,
)
from chaos_agent.agent.state_mgmt.state_builders import build_inject_initial_state


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
        # Cross-graph bridge payload (tier1-speedup): dispatched once, then stale.
        "progress_ledger": None,
        "probe_snapshot": None,
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
    snapshot = {"facts": [{"fact": "pod-a Running", "source_tool": "kubectl_read"}]}
    ledger = {"state": {"established_facts": ["pod-a Running"]}}
    intent_state = {
        "tui_session_id": "sid-1",
        "handoff_summary": "summary",
        "fault_spec": fault_spec,
        "progress_ledger": ledger,
        "probe_snapshot": snapshot,
    }

    handoff = build_pipeline_handoff_from_intent_state(
        intent_state,
        operation="inject",
        task_id="task-1",
        default_tui_session_id="fallback",
    )

    fault_spec["names"].append("pod-b")
    snapshot["facts"].append({"fact": "mutated"})
    ledger["state"]["established_facts"].append("mutated")

    assert handoff.operation == "inject"
    assert handoff.task_id == "task-1"
    assert handoff.tui_session_id == "sid-1"
    assert handoff.handoff_summary == "summary"
    assert handoff.fault_spec == {"scope": "pod", "names": ["pod-a"]}
    assert handoff.batch_submit_args is None
    # Bridge payload is deep-copied: later intent-graph mutation cannot leak in.
    assert handoff.probe_snapshot == {"facts": [{"fact": "pod-a Running", "source_tool": "kubectl_read"}]}
    assert handoff.progress_ledger == {"state": {"established_facts": ["pod-a Running"]}}


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
    # No intent-time evidence recorded → bridge fields stay None (pre-change
    # rendering baseline on the pipeline side).
    assert handoff.progress_ledger is None
    assert handoff.probe_snapshot is None


def test_cross_graph_bridge_reaches_pipeline_initial_state():
    """tier1-speedup regression anchor: intent-time evidence must survive the
    full Intent Graph → Pipeline Graph handoff.

    The original implementation shipped with this chain silently broken at
    every hop (IntentState had no channels for these fields, so langgraph
    dropped the writes without error; the handoff dataclass and the pipeline
    initial-state builder never carried them either) while 29 function-level
    tests stayed green — they constructed state dicts directly and never
    exercised the real graph boundary. This test pins the bridge end to end.
    """
    snapshot = {
        "facts": [
            {"fact": "pod-a Running", "source_tool": "update_progress", "probed_at": "2026-08-26T10:00:00+08:00"}
        ]
    }
    ledger = {"state": {"established_facts": ["pod-a Running"]}, "log": ["probed target"]}
    intent_state = {
        "tui_session_id": "sid-1",
        "handoff_summary": "summary",
        "fault_spec": {"scope": "pod", "names": ["pod-a"]},
        "progress_ledger": ledger,
        "probe_snapshot": snapshot,
    }

    handoff = build_pipeline_handoff_from_intent_state(
        intent_state, operation="inject", task_id="task-1",
    )
    pipeline_input = build_inject_initial_state(
        task_id=handoff.task_id,
        fault_spec=handoff.fault_spec,
        progress_ledger=handoff.progress_ledger,
        probe_snapshot=handoff.probe_snapshot,
    )

    assert pipeline_input["probe_snapshot"] == snapshot
    assert pipeline_input["progress_ledger"] == ledger

    # Direct entry (no intent dialogue) keeps the pre-change baseline.
    direct = build_inject_initial_state(task_id="t2", fault_spec={"scope": "pod"})
    assert "probe_snapshot" not in direct
    assert "progress_ledger" not in direct
