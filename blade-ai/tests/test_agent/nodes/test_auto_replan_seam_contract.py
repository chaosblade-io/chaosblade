"""Contract tests for the system auto-trigger replan seam (S1 fix).

The router's error branch auto-routes REPLAN-classified errors to
``agent_loop``, but that path used to write NO replan bookkeeping: no
``replan_context``, no ``replan_count`` increment, and the terminal error
stayed set — so ``should_continue_agent_loop`` rejected on its very next
evaluation. The "replan" was a one-iteration detour straight to failure.

The fix converges the system auto-trigger with the two LLM channels on the
SAME structural review (:func:`_review_replan_request`) and the SAME seam
writer (:func:`_fire_replan_seam`). These tests pin:

1. The trigger criterion is the canonical classifier — exactly the errors
   the router's auto-detect consults, so the two can never disagree.
2. Every gate (switch, budget, sticky flag, same-turn dedup) is honoured.
3. The reviewed-rejection branch keeps executing: clears the whole
   terminal-error triple and appends a LIFECYCLE REVIEW note.
4. The seam clears the WHOLE terminal-error triple (``error``,
   ``failure_detail``, ``failure_reason``) — ``read_merged_error`` falls
   back to ``failure_detail``, so clearing only ``error`` would leave
   ``outcome.error`` non-empty and agent_loop would still reject.
5. End-to-end routing: fired seam -> execute router "replan" AND
   agent_loop router not "reject".
"""

from chaos_agent.agent.nodes.execute.execute_loop import (
    _fire_replan_seam,
    _maybe_auto_trigger_replan,
)
from chaos_agent.agent.replan import ReplanRequest
from chaos_agent.agent.result.operation_outcome import read_operation_outcome
from chaos_agent.agent.result.verdict import FailureCategory
from chaos_agent.agent.router import (
    should_continue_agent_loop,
    should_continue_execute_loop,
)
from chaos_agent.agent.state_mgmt.state_helpers import fail_state
from chaos_agent.config.settings import settings
from langchain_core.messages import AIMessage, ToolMessage

REPLAN_ERROR = "Error: target pod accounting-xyz not found in namespace cms-demo"
TRANSIENT_ERROR = "Error: connection timeout"


def _state(**kw) -> dict:
    s = {"messages": [], "replan_count": 0, "task_id": "task-s1"}
    s.update(kw)
    return s


def _attempted_state(**kw) -> dict:
    """A state where the current contract DID attempt an injection."""
    return _state(injection_method="blade", experiment_uid="abc123", **kw)


def _fail_result(error: str = REPLAN_ERROR) -> dict:
    """Real terminal-error payload as stamped by fail_state."""
    return dict(fail_state(FailureCategory.EXECUTION_FAILED, error, []))


# ---------------------------------------------------------------------------
# Trigger criterion == canonical classifier
# ---------------------------------------------------------------------------

class TestTriggerCriterion:
    def test_replan_classified_error_fires_the_seam(self):
        result = _fail_result()
        _maybe_auto_trigger_replan(_attempted_state(), result)
        assert result.get("replan_requested") is True

    def test_transient_error_never_fires(self):
        result = {"error": TRANSIENT_ERROR}
        _maybe_auto_trigger_replan(_attempted_state(), result)
        assert not result.get("replan_requested")
        assert result["error"] == TRANSIENT_ERROR

    def test_empty_error_is_inert(self):
        result = {}
        _maybe_auto_trigger_replan(_attempted_state(), result)
        assert not result.get("replan_requested")


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

class TestGates:
    def test_switch_off_is_inert(self):
        result = _fail_result()
        original = settings.replan_auto_trigger
        settings.replan_auto_trigger = False
        try:
            _maybe_auto_trigger_replan(_attempted_state(), result)
        finally:
            settings.replan_auto_trigger = original
        assert not result.get("replan_requested")

    def test_exhausted_budget_is_inert(self):
        result = _fail_result()
        _maybe_auto_trigger_replan(
            _attempted_state(replan_count=int(settings.max_replan_count)),
            result,
        )
        assert not result.get("replan_requested")

    def test_same_turn_llm_seam_is_not_doubled(self):
        result = _fail_result()
        result["replan_requested"] = True
        _maybe_auto_trigger_replan(_attempted_state(), result)
        assert result.get("replan_count") is None  # seam writer never ran

    def test_sticky_state_flag_is_inert(self):
        result = _fail_result()
        _maybe_auto_trigger_replan(_attempted_state(replan_requested=True), result)
        assert not result.get("replan_count")


# ---------------------------------------------------------------------------
# Fired seam — bookkeeping via the shared writer
# ---------------------------------------------------------------------------

class TestFiredSeam:
    def test_seam_bookkeeping_matches_the_llm_channel(self):
        result = _fail_result()
        _maybe_auto_trigger_replan(_attempted_state(), result)
        assert result["replan_requested"] is True
        assert result["replan_count"] == 1
        assert result["replan_context"]
        assert len(result.get("replan_history") or []) == 1
        assert result.get("approved_target") is None

    def test_seam_clears_the_whole_terminal_error_triple(self):
        result = _fail_result()
        assert result["failure_detail"]  # fail_state always stamps it
        _maybe_auto_trigger_replan(_attempted_state(), result)
        assert result["error"] is None
        assert result["failure_detail"] is None
        assert result.get("failure_reason") is None


# ---------------------------------------------------------------------------
# Reviewed rejection — keep executing with a clear ledger
# ---------------------------------------------------------------------------

class TestReviewedRejection:
    def test_no_injection_attempt_rejects_and_continues(self):
        result = _fail_result()
        _maybe_auto_trigger_replan(_state(), result)  # no attribution
        assert not result.get("replan_requested")
        assert result["error"] is None
        assert result["failure_detail"] is None
        msgs = result.get("messages") or []
        assert msgs and "LIFECYCLE REVIEW" in msgs[-1].content


# ---------------------------------------------------------------------------
# Seam writer invariants (channel-independent)
# ---------------------------------------------------------------------------

class TestFireReplanSeam:
    def test_writer_clears_terminal_error_triple_for_any_channel(self):
        state = _attempted_state()
        result = dict(fail_state(
            FailureCategory.EXECUTION_FAILED, REPLAN_ERROR, []
        ))
        request = ReplanRequest(
            kind="feasibility",
            decision="plan_invalid",
            invalidated_assumption="target exists",
            affected_step="inject",
        )
        _fire_replan_seam(state, result, request, {"error_summary": REPLAN_ERROR})
        assert result["replan_requested"] is True
        assert result["error"] is None
        assert result["failure_detail"] is None
        assert result.get("failure_reason") is None


# ---------------------------------------------------------------------------
# Seam experiment_uid retention (task-349ccf5d)
# ---------------------------------------------------------------------------

def _create_msg(uid: str, call_id: str = "tc-create") -> ToolMessage:
    """Successful blade_create result carrying the experiment uid."""
    return ToolMessage(
        content=f'{{"code":200,"success":true,"result":"{uid}"}}',
        tool_call_id=call_id, name="blade_create",
    )


def _fail_msg(i: int) -> ToolMessage:
    return ToolMessage(
        content="Error: kubectl exec timed out",
        tool_call_id=f"tc-f{i}", name="kubectl", status="error",
    )


def _destroy_msg(uid: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[
        {"name": "blade_destroy", "args": {"uid": uid}, "id": "tc-destroy"},
    ])


def _request() -> ReplanRequest:
    return ReplanRequest(
        kind="feasibility",
        decision="plan_invalid",
        invalidated_assumption="target exists",
        affected_step="inject",
    )


class TestSeamBladeUidRetention:
    """task-349ccf5d: the seam's keep decision used to trust the raw
    scan inside replan_context, which stops after 5 failed messages —
    a successful create buried deeper in history was invisible and the
    live experiment (uid ``5aaa51dbcb78a25d``) was orphaned. The keep
    decision now runs the canonical extractor over the FULL message
    history (destroyed/retired filtered), with ``state.experiment_uid`` as
    the memory-compression fallback."""

    def test_live_uid_survives_five_failure_truncation(self):
        """Successful create followed by >=5 failed tool messages — the
        exact truncation shape that lost the uid."""
        messages = [_create_msg("1eafbeef00000002")] + [_fail_msg(i) for i in range(6)]
        state = _state(messages=messages)  # NO state.experiment_uid: found in history
        result = _fail_result()
        result["experiment_uid"] = "1eafbeef00000002"
        _fire_replan_seam(state, result, _request(), {"error_summary": REPLAN_ERROR})
        assert result["experiment_uid"] == "1eafbeef00000002"  # kept, not cleared
        assert result["replan_context"]["existing_experiment_uids"] == ["1eafbeef00000002"]

    def test_destroyed_uid_is_not_resurrected(self):
        """An experiment already sent to blade_destroy must stay dead —
        keeping its uid would re-arm recover on a gone experiment."""
        messages = [_create_msg("deadbeef00000001"), _destroy_msg("deadbeef00000001"), _fail_msg(0)]
        state = _state(messages=messages)
        result = _fail_result()
        _fire_replan_seam(state, result, _request(), {"error_summary": REPLAN_ERROR})
        assert result["experiment_uid"] is None
        assert result["replan_context"]["existing_experiment_uids"] == []

    def test_destroyed_uid_in_state_fallback_is_not_resurrected(self):
        """The state.experiment_uid fallback must pass the SAME death filters:
        nothing clears state.experiment_uid when the LLM issues blade_destroy,
        so the raw persisted uid would otherwise resurrect the dead
        experiment into existing_experiment_uids (the Phase-1 replan prompt)
        and the keep decision."""
        messages = [_create_msg("deadbeef00000001"), _destroy_msg("deadbeef00000001"), _fail_msg(0)]
        state = _state(messages=messages, experiment_uid="deadbeef00000001")
        result = _fail_result()
        result["experiment_uid"] = "deadbeef00000001"
        _fire_replan_seam(state, result, _request(), {"error_summary": REPLAN_ERROR})
        assert result["experiment_uid"] is None
        assert result["replan_context"]["existing_experiment_uids"] == []
        assert result["replan_history"][-1]["experiment_uid_at_seam"] is None

    def test_retired_uid_is_not_resurrected(self):
        """Framework-side cleanup leaves no destroy ToolMessage; the
        retired list is the seam's only evidence."""
        messages = [_create_msg("5e71a1b2c3d4e5f6"), _fail_msg(0)]
        state = _state(messages=messages, retired_experiment_uids=["5e71a1b2c3d4e5f6"])
        result = _fail_result()
        _fire_replan_seam(state, result, _request(), {"error_summary": REPLAN_ERROR})
        assert result["experiment_uid"] is None
        assert result["replan_context"]["existing_experiment_uids"] == []

    def test_state_fallback_when_create_message_compressed(self):
        """Memory compression may summarize away the create ToolMessage;
        the persisted ``state.experiment_uid`` is the fallback evidence."""
        state = _state(messages=[_fail_msg(0)], experiment_uid="uid-fallback")
        result = _fail_result()
        result["experiment_uid"] = "uid-fallback"
        _fire_replan_seam(state, result, _request(), {"error_summary": REPLAN_ERROR})
        assert result["experiment_uid"] == "uid-fallback"
        assert result["replan_context"]["existing_experiment_uids"] == ["uid-fallback"]

    def test_experiment_uid_at_seam_recorded_in_history(self):
        """Audit trail: the uid observed at the seam lands in
        replan_history whether it was kept or dropped."""
        state = _state(messages=[_create_msg("1eafbeef00000002")])
        result = _fail_result()
        result["experiment_uid"] = "1eafbeef00000002"
        _fire_replan_seam(state, result, _request(), {"error_summary": REPLAN_ERROR})
        assert result["replan_history"][-1]["experiment_uid_at_seam"] == "1eafbeef00000002"

        # Nothing alive at the seam -> still recorded (as None).
        result2 = _fail_result()
        _fire_replan_seam(_state(), result2, _request(), {"error_summary": REPLAN_ERROR})
        assert result2["replan_history"][-1]["experiment_uid_at_seam"] is None

    # -----------------------------------------------------------------
    # Phase-14 G4: the legacy uid spelling is EOL (fresh-database ruling).
    # Pre-rename checkpoints that carry a top-level ``blade_uid`` key are
    # no longer hydrated — the seam derives the uid from the modern key
    # and message evidence only. These tests pin the retired behaviour:
    # a legacy-only state key is invisible to every gate and filter.
    # -----------------------------------------------------------------

    def test_legacy_state_uid_key_hydrates_into_keep_decision(self):
        """[已翻转] G4 EOL 后：旧键拼写不再被 hydration 救活——seam 从
        state（现代键）+ 消息证据重算 uid，两者皆空时覆盖预置值为
        None（mid-flight result 的预置 uid 不是证据）。"""
        state = _state(messages=[_fail_msg(0)], blade_uid="uid-legacy")
        result = _fail_result()
        result["experiment_uid"] = "uid-legacy"  # preset, NOT evidence
        _fire_replan_seam(state, result, _request(), {"error_summary": REPLAN_ERROR})
        assert result["experiment_uid"] is None
        assert result["replan_context"]["existing_experiment_uids"] == []

    def test_state_destroyed_uid_is_not_resurrected(self):
        """The durable state uid passes the SAME death filters as the
        message scan: a uid whose experiment was destroyed stays dead."""
        messages = [_create_msg("deadbeef00000001"), _destroy_msg("deadbeef00000001"), _fail_msg(0)]
        state = _state(messages=messages, experiment_uid="deadbeef00000001")
        result = _fail_result()
        _fire_replan_seam(state, result, _request(), {"error_summary": REPLAN_ERROR})
        assert result["experiment_uid"] is None
        assert result["replan_context"]["existing_experiment_uids"] == []

    def test_legacy_state_uid_counts_as_attempted_injection(self):
        """[已翻转] G4 EOL 后：attribution-presence gate（auto-trigger）不
        再读旧键拼写——仅旧键的 checkpoint 不算 attempted injection，
        seam 不触发，落入 reviewed-rejection 分支。"""
        result = _fail_result()
        _maybe_auto_trigger_replan(_state(blade_uid="abc123"), result)
        assert "replan_requested" not in result


# ---------------------------------------------------------------------------
# End-to-end routing on the merged state
# ---------------------------------------------------------------------------

def _merge(state: dict, result: dict) -> dict:
    merged = {**state, **{k: v for k, v in result.items() if k != "messages"}}
    merged["messages"] = state["messages"] + result.get("messages", [])
    return merged


class TestEndToEndRouting:
    def test_fired_seam_routes_replan_and_survives_agent_loop(self):
        state = _attempted_state(execute_loop_count=3)
        result = _fail_result()
        _maybe_auto_trigger_replan(state, result)
        merged = _merge(state, result)
        assert not read_operation_outcome(merged).error
        assert should_continue_execute_loop(merged) == "replan"
        assert should_continue_agent_loop(merged) != "reject"

    def test_rejected_seam_keeps_executing_without_terminal_error(self):
        state = _state(execute_loop_count=3)
        result = _fail_result()
        _maybe_auto_trigger_replan(state, result)
        merged = _merge(state, result)
        assert not read_operation_outcome(merged).error
        assert should_continue_execute_loop(merged) != "replan"
        assert should_continue_agent_loop(merged) != "reject"

    def test_untouched_terminal_error_still_reaches_verdict(self):
        """Budget-exhausted errors must keep their normal terminal path."""
        state = _attempted_state(
            execute_loop_count=3,
            replan_count=int(settings.max_replan_count),
        )
        result = _fail_result()
        _maybe_auto_trigger_replan(state, result)
        merged = _merge(state, result)
        assert read_operation_outcome(merged).error
        assert should_continue_execute_loop(merged) != "replan"
