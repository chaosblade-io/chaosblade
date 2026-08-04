"""Interruption records — what the intent graph is told when a turn does NOT
complete.

The intent graph is context-isolated from execution, so without a record written
at the interruption point it cannot tell the next dialogue turn that anything
happened, nor that a fault may still be live. The progress ledger is what makes
this possible from ANY interruption point: it is always current in state, so the
exit handler just reads and renders it.
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.progress_ledger import freeze_anchor, merge_progress_ledger
from chaos_agent.agent.result.operation_summary import (
    INTERRUPT_CAUSES,
    build_interrupted_record,
)

_SPEC = {"scope": "pod", "blade_target": "network", "blade_action": "loss",
         "namespace": "ns", "names": ["p0"]}


def _ledger_mid_execution():
    return merge_progress_ledger(
        freeze_anchor(_SPEC, goal="inject 30% loss"),
        state_update={"phase": "executing", "established_facts": ["pod p0 Running"]},
        log_append=[
            {"event": "target confirmed", "status": "verified"},
            {"event": "loss may not have taken effect yet", "status": "assumed"},
        ],
    )


# ── The ledger IS the interruption record ──────────────────────────────

@pytest.mark.parametrize("cause", sorted(INTERRUPT_CAUSES))
def test_record_names_the_cause_and_carries_ledger_progress(cause):
    record = build_interrupted_record(
        {"progress_ledger": _ledger_mid_execution()}, "task-x", cause=cause,
    )
    assert "[Task Interrupted]" in record
    assert INTERRUPT_CAUSES[cause] in record
    # The point of the whole design: the intent graph learns HOW FAR it got.
    assert "pod p0 Running" in record
    assert "target confirmed" in record


def test_record_preserves_unverified_status_markers():
    # A finding the executor never verified must not reach the next dialogue turn
    # dressed up as established fact.
    record = build_interrupted_record(
        {"progress_ledger": _ledger_mid_execution()}, "task-x", cause="user_cancel",
    )
    assert "[assumed] loss may not have taken effect yet" in record
    assert "[verified] target confirmed" in record


def test_record_is_explicit_when_nothing_was_recorded():
    record = build_interrupted_record({}, "task-x", cause="user_cancel")
    assert "left no progress record" in record


def test_record_identifies_the_target_even_when_the_ledger_is_empty():
    # Planning has no anchor by design (the FaultSpec is still converging) and the
    # model may not have recorded anything yet. Without reading the spec from
    # state, the dialogue would learn only that "something was cancelled" — and a
    # follow-up like "try again" would have no referent.
    record = build_interrupted_record(
        {"fault_spec": {**_SPEC, "fault_type": "pod-network-loss"}},
        "task-x", cause="user_cancel",
    )
    assert "pod-network-loss" in record
    assert "ns/p0" in record


def test_record_omits_the_target_line_when_nothing_is_known_yet():
    # Interrupted before any spec existed: do not emit an empty "Type: | Target:" line.
    record = build_interrupted_record({}, "task-x", cause="disconnected")
    assert "Type:" not in record


def test_target_line_is_not_duplicated_when_the_ledger_also_has_it():
    led = merge_progress_ledger(
        freeze_anchor({**_SPEC, "fault_type": "pod-network-loss"}, goal="g"),
        log_append=[{"event": "injected", "status": "verified"}],
    )
    record = build_interrupted_record(
        {"progress_ledger": led, "fault_spec": {**_SPEC, "fault_type": "pod-network-loss"}},
        "task-x", cause="user_cancel",
    )
    assert record.count("Type:") == 1


def test_error_detail_is_included_and_bounded():
    record = build_interrupted_record(
        {}, "task-x", cause="internal_error", error_detail="E" * 500,
    )
    assert "Error detail" in record
    assert len(record) < 1200  # detail truncated, record stays compact


# ── Wording: advisory, never imperative ────────────────────────────────

def test_live_fault_warning_suggests_a_check_and_does_not_order_recovery():
    record = build_interrupted_record(
        {"progress_ledger": _ledger_mid_execution(), "blade_uid": "uid-1"},
        "task-x", cause="user_cancel",
    )
    assert "advisable" in record               # advisory
    assert "a recovery can be run" in record   # offered as an option
    # Phrased as a suggestion, not an instruction to go recover.
    assert "must run a recovery" not in record
    assert "you must" not in record.lower()


def test_no_live_fault_warning_when_nothing_was_executed():
    record = build_interrupted_record(
        {"progress_ledger": freeze_anchor(_SPEC, goal="g")}, "task-x",
        cause="confirm_timeout",
    )
    # A card that timed out before execution touched nothing — do not imply a
    # live fault the user must go chase.
    assert "may still be in a faulted state" not in record


# ── Timing: the record must be written BEFORE the terminating event ────

@pytest.mark.asyncio
async def test_writing_after_the_terminating_yield_would_silently_never_run():
    # Documents WHY every exit handler writes before its final yield: a consumer
    # stops iterating at ``done``, the generator is closed, and code after the
    # last yield never executes. (Regression guard for a real bug.)
    ran: list[str] = []

    async def after_final_yield():
        try:
            raise RuntimeError("boom")
        except Exception:
            yield "error"
            yield "done"
            ran.append("write")      # unreachable once the consumer breaks

    async def before_final_yield():
        try:
            raise RuntimeError("boom")
        except Exception:
            ran.append("write")      # what the production handlers do
            yield "error"
            yield "done"

    async def consume(gen):
        async for evt in gen:
            if evt == "done":
                break

    await consume(after_final_yield())
    assert ran == []                 # silently lost
    await consume(before_final_yield())
    assert ran == ["write"]          # survives


# ── Which operation the record is attributed to ────────────────────────

@pytest.mark.asyncio
async def test_interrupted_recovery_is_read_from_the_recover_thread():
    # Recovery runs on its own graph and thread. Reading the pipeline
    # coordinates instead would attribute the interruption to the inject task and
    # miss the recover ledger completely — so recover coordinates take precedence.
    import asyncio as _asyncio
    from unittest.mock import AsyncMock, MagicMock

    from chaos_agent.server.routes.turn_event_stream import (
        TurnContext,
        _write_interrupted_record,
    )

    def _graph_with(ledger, marker):
        snap = MagicMock()
        snap.values = {"progress_ledger": ledger, "marker": marker}
        graph = MagicMock()
        graph.aget_state = AsyncMock(return_value=snap)
        return graph

    recover_ledger = merge_progress_ledger(
        None, log_append=[{"event": "destroy issued", "status": "observed"}],
    )
    recover_graph = _graph_with(recover_ledger, "recover")
    pipeline_graph = _graph_with(_ledger_mid_execution(), "pipeline")

    written: list[str] = []
    ctx = TurnContext(
        sid="s", turn_id="turn-1", thread_id="th", input_text="", permission_mode="default",
        dry_run=False, req=None, store=None,
        agents={"recover": recover_graph}, task_tracker=None,
        intent_graph=MagicMock(), pipeline_graph=pipeline_graph,
        graph_config={}, initial_state={}, tracker_key="k",
        tracker_queue=_asyncio.Queue(),
    )
    # Both were dispatched this turn; recovery is the one in flight.
    ctx.pipeline_task_id = "task-inject"
    ctx.pipeline_config = {"configurable": {"thread_id": "task-inject"}}
    ctx.recover_task_id = "task-recover"
    ctx.recover_config = {"configurable": {"thread_id": "task-recover"}}

    import chaos_agent.server.routes.turn_event_stream as tes

    async def _capture(text, **kwargs):
        written.append(text)

    original = tes.write_operation_summary
    tes.write_operation_summary = _capture
    try:
        await _write_interrupted_record(ctx, cause="user_cancel")
    finally:
        tes.write_operation_summary = original

    assert len(written) == 1
    assert "task-recover" in written[0]
    assert "destroy issued" in written[0]      # the recover ledger
    assert "pod p0 Running" not in written[0]  # not the inject ledger
    recover_graph.aget_state.assert_awaited_once()
    pipeline_graph.aget_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_second_record_once_the_turn_already_wrote_one():
    # A completed operation must never be contradicted by an interruption note
    # appended on top of it.
    import asyncio as _asyncio
    from unittest.mock import MagicMock

    from chaos_agent.server.routes.turn_event_stream import (
        TurnContext,
        _write_interrupted_record,
    )
    import chaos_agent.server.routes.turn_event_stream as tes

    ctx = TurnContext(
        sid="s", turn_id="turn-1", thread_id="th", input_text="", permission_mode="default",
        dry_run=False, req=None, store=None, agents={}, task_tracker=None,
        intent_graph=MagicMock(), pipeline_graph=MagicMock(), graph_config={},
        initial_state={}, tracker_key="k", tracker_queue=_asyncio.Queue(),
    )
    ctx.operation_record_written = True

    written: list[str] = []

    async def _capture(text, **kwargs):
        written.append(text)

    original = tes.write_operation_summary
    tes.write_operation_summary = _capture
    try:
        await _write_interrupted_record(ctx, cause="internal_error")
    finally:
        tes.write_operation_summary = original

    assert written == []


# ── The record must survive the dialogue's own trimming ────────────────

def test_interruption_record_is_preserved_when_dialogue_is_trimmed():
    # The intent graph trims old dialogue but preserves operation records, since
    # the LLM needs them to answer "what happened last time?". An interruption
    # record needs it MORE than a completed one: it is the only place the dialogue
    # learns a fault may still be live. Its prefix must be on that allowlist.
    from langchain_core.messages import HumanMessage, SystemMessage

    from chaos_agent.agent.nodes.planning.intent_confirm import (
        _TRIM_TAIL_KEEP,
        _build_trim_remove_list,
    )

    record = SystemMessage(
        content=build_interrupted_record({"blade_uid": "x"}, "task-1", cause="user_cancel"),
        id="rec",
    )
    messages = [record] + [
        HumanMessage(content=f"chat{i}", id=f"h{i}") for i in range(_TRIM_TAIL_KEEP + 3)
    ]
    removed = {r.id for r in _build_trim_remove_list(messages)}
    assert "rec" not in removed
    # Sanity: ordinary dialogue in the same position IS trimmed.
    assert "h0" in removed

