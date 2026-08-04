"""Contract tests for the replan-review structural rule (task-71fa78b6).

A contract that never attempted its injection cannot be declared
infeasible: the review must key on STATE FACTS (attribution / issued
injection calls within the current epoch), never on the replan request's
free text — the model hallucinated its evidence wholesale in the incident
task. The rule is phase- and fault-agnostic by design.
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes.execute.execute_loop import (
    _handle_replan,
    _injection_attempted_this_contract,
    _review_replan_request,
)
from chaos_agent.agent.replan import ReplanRequest


def _request(decision: str = "plan_invalid") -> ReplanRequest:
    return ReplanRequest(
        kind="feasibility",
        decision=decision,
        invalidated_assumption="the injection channel works",
        affected_step="inject",
        observed_evidence=["phase1_readonly_violation x39"],
    )


def _blade_create_call(tc_id: str = "bc-1") -> dict:
    return {
        "name": "blade_create",
        "id": tc_id,
        "args": {"target": "cpu", "action": "fullload"},
    }


# ---------------------------------------------------------------------------
# _injection_attempted_this_contract — the structural proofs
# ---------------------------------------------------------------------------

class TestInjectionAttemptedThisContract:
    def test_no_attempt_in_empty_contract(self):
        state = {"messages": [HumanMessage(content="go")]}
        assert _injection_attempted_this_contract(state) is False

    def test_attribution_is_proof(self):
        assert _injection_attempted_this_contract(
            {"messages": [], "injection_method": "host_blade"}
        ) is True
        assert _injection_attempted_this_contract(
            {"messages": [], "blade_uid": "abc123"}
        ) is True

    def test_issued_blade_create_is_an_attempt_even_without_result(self):
        state = {
            "messages": [
                HumanMessage(content="go"),
                AIMessage(content="", tool_calls=[_blade_create_call()]),
            ],
        }
        assert _injection_attempted_this_contract(state) is True

    def test_attempt_before_epoch_boundary_does_not_count(self):
        """A failed attempt under the OLD contract must not license a replan
        under the NEW one — the epoch boundary is the contract boundary."""
        pre_seam = [
            HumanMessage(content="old contract"),
            AIMessage(content="", tool_calls=[_blade_create_call()]),
            ToolMessage(content="Error: failed", name="blade_create", tool_call_id="bc-1"),
        ]
        state = {
            "messages": pre_seam + [HumanMessage(content="[PLAN CHANGE APPROVED]")],
            "attribution_epoch_index": len(pre_seam),
        }
        assert _injection_attempted_this_contract(state) is False

    def test_kubectl_mutation_attempt_is_proof(self):
        state = {
            "messages": [
                AIMessage(content="", tool_calls=[{
                    "name": "kubectl",
                    "id": "kc-1",
                    "args": {"subcommand": "patch", "v_args": "patch deployment x"},
                }]),
            ],
        }
        assert _injection_attempted_this_contract(state) is True

    def test_read_only_kubectl_is_not_an_attempt(self):
        state = {
            "messages": [
                AIMessage(content="", tool_calls=[{
                    "name": "kubectl_read",
                    "id": "kr-1",
                    "args": {"subcommand": "get"},
                }]),
            ],
        }
        assert _injection_attempted_this_contract(state) is False

    def test_host_native_shell_attempt_is_proof(self):
        """The host-native carrier (raw-shell faults) must count too — the
        attempt vocabulary is the attribution classifier's, so no carrier can
        silently fall out of the review rule."""
        state = {
            "kube_connection_mode": "ssh",
            "ssh_host": "10.0.0.5",
            "messages": [
                AIMessage(content="", tool_calls=[{
                    "name": "host_inject",
                    "id": "hi-1",
                    "args": {"command": "blade create cpu fullload"},
                }]),
            ],
        }
        assert _injection_attempted_this_contract(state) is True


# ---------------------------------------------------------------------------
# _review_replan_request — the review rule
# ---------------------------------------------------------------------------

class TestReviewReplanRequest:
    def test_needs_investigation_stays_in_react(self):
        reason = _review_replan_request({"messages": []}, _request("needs_investigation"))
        assert reason is not None
        assert "investigation" in reason

    def test_plan_invalid_without_attempt_is_rejected(self):
        """The incident case: Phase 2 active, baseline captured, zero injection
        attempts — the hallucinated replan must not consume budget."""
        state = {
            "messages": [HumanMessage(content="baseline captured")],
            "baseline_data": {"ok": True},
        }
        reason = _review_replan_request(state, _request("plan_invalid"))
        assert reason is not None
        assert "No injection attempt" in reason

    def test_plan_invalid_after_attempt_is_allowed(self):
        state = {
            "messages": [
                AIMessage(content="", tool_calls=[_blade_create_call()]),
                ToolMessage(content="Error: failed", name="blade_create", tool_call_id="bc-1"),
            ],
        }
        assert _review_replan_request(state, _request("plan_invalid")) is None

    def test_free_text_cannot_talk_around_the_rule(self):
        """Rich-sounding evidence in the request changes nothing — only state
        facts decide."""
        request = _request("plan_invalid")
        request.observed_evidence = [
            "blade_create rejected 39 times",
            "kernel lacks netem",
            "CR never reached terminal phase",
        ]
        reason = _review_replan_request({"messages": []}, request)
        assert reason is not None


# ---------------------------------------------------------------------------
# _handle_replan — deferred rejection flag on the tool channel
# ---------------------------------------------------------------------------

class TestHandleReplanDeferredRejection:
    def test_rejected_plan_invalid_sets_deferred_flag_without_firing(self):
        response = AIMessage(content="", tool_calls=[{
            "name": "request_replan",
            "id": "rr-1",
            "args": {
                "kind": "feasibility",
                "decision": "plan_invalid",
                "invalidated_assumption": "channel works",
                "affected_step": "inject",
            },
        }])
        state = {"messages": [HumanMessage(content="go")]}
        result: dict = {}
        _handle_replan(response, state, result)
        assert result.get("_replan_review_rejection")
        assert "No injection attempt" in result["_replan_review_rejection"]
        # Nothing fired: no budget spent, no routing flag.
        assert "replan_count" not in result
        assert result.get("replan_requested") is not True

    def test_needs_investigation_tool_call_keeps_legacy_silent_path(self):
        response = AIMessage(content="", tool_calls=[{
            "name": "request_replan",
            "id": "rr-2",
            "args": {
                "kind": "feasibility",
                "decision": "needs_investigation",
                "invalidated_assumption": "unclear effect",
                "affected_step": "verify",
            },
        }])
        state = {"messages": [HumanMessage(content="go")]}
        result: dict = {}
        _handle_replan(response, state, result)
        assert "_replan_review_rejection" not in result
        assert "replan_count" not in result
