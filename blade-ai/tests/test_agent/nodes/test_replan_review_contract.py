"""Contract tests for the replan-review structural rule (task-71fa78b6).

A contract that never attempted its injection cannot be declared
infeasible: the review must key on STATE FACTS (attribution / issued
injection calls within the current epoch), never on the replan request's
free text — the model hallucinated its evidence wholesale in the incident
task. The rule is phase- and fault-agnostic by design.

The second structural key (task inject-65bbf344): a selector-based
contract whose approved target set is PROVABLY empty — framework
empty-set receipt anchored to the approved selector — replans without an
attempt. The attempt is physically impossible when the target is gone,
and demanding one deadlocks the contract.
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes.execute.execute_loop import (
    _handle_replan,
    _injection_attempted_this_contract,
    _review_replan_request,
    _target_absence_proven_in_epoch,
)
from chaos_agent.agent.replan import ReplanRequest
from chaos_agent.tools.kubectl_cli import EMPTY_SELECTOR_HINT


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
            {"messages": [], "experiment_uid": "abc123"}
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

    # -- safety-kind structural exception (inject-cc2d5080) -------------

    def _safety_request(self) -> ReplanRequest:
        return ReplanRequest(
            kind="safety",
            decision="plan_invalid",
            invalidated_assumption="the recovery carrier can be stacked",
            affected_step="stack carrier + arm timer",
        )

    def _banned_receipt(self, tc_id: str = "k-1") -> ToolMessage:
        return ToolMessage(
            content=(
                "[target_guard] REJECT_BANNED — the manifest contains a "
                "non-whitelisted resource kind (ServiceAccount)"
            ),
            name="kubectl",
            tool_call_id=tc_id,
        )

    def test_safety_kind_with_banned_receipt_replans_without_attempt(self):
        """inject-cc2d5080: the refused call WAS the carrier the plan
        needed — the guard's own REJECT_BANNED receipt is the
        framework-proven infeasibility, and "issue the injection call
        first" would order the model to inject un-armed. A safety-kind
        request with that receipt on record replans immediately."""
        state = {
            "messages": [
                HumanMessage(content="baseline captured"),
                self._banned_receipt(),
            ],
        }
        assert _review_replan_request(state, self._safety_request()) is None

    def test_safety_kind_without_banned_receipt_still_demands_attempt(self):
        """The exception is earned by the RECEIPT, not the kind label:
        a safety replan with no guard refusal on record is still
        anticipation and keeps the anti-laziness rule."""
        state = {"messages": [HumanMessage(content="baseline captured")]}
        reason = _review_replan_request(state, self._safety_request())
        assert reason is not None
        assert "No injection attempt" in reason

    def test_non_safety_kind_with_banned_receipt_still_demands_attempt(self):
        """A feasibility/verification replan riding on a guard refusal
        is the pre-existing shape (an un-won retry plea) and keeps the
        standing rule — the exception is safety-only."""
        state = {
            "messages": [
                HumanMessage(content="baseline captured"),
                self._banned_receipt(),
            ],
        }
        reason = _review_replan_request(state, _request("plan_invalid"))
        assert reason is not None
        assert "No injection attempt" in reason

    def test_banned_receipt_under_old_contract_does_not_earn_exception(self):
        """The receipt collector is contract-relative: a REJECT_BANNED
        older than the newest plan-change approval judged a target set
        the new contract replaced, so it cannot license the exception."""
        state = {
            "messages": [
                self._banned_receipt("old-1"),
                HumanMessage(content="[PLAN CHANGE APPROVED] new target set"),
            ],
        }
        reason = _review_replan_request(state, self._safety_request())
        assert reason is not None
        assert "No injection attempt" in reason


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


# ---------------------------------------------------------------------------
# _target_absence_proven_in_epoch — the second structural key
# ---------------------------------------------------------------------------

ABSENCE_NS = "eb-cn-shanghai-cloudspe"
ABSENCE_LABELS = {
    "app.kubernetes.io/instance": "apiserver-flowlimit",
    "app.kubernetes.io/name": "mq-flowlimit",
}
_SELECTOR = (
    "app.kubernetes.io/instance=apiserver-flowlimit,"
    "app.kubernetes.io/name=mq-flowlimit"
)


def _selector_spec(**over) -> dict:
    """The incident contract shape: pure selector target, no explicit names."""
    spec = {
        "namespace": ABSENCE_NS,
        "scope": "pod",
        "names": [],
        "labels": dict(ABSENCE_LABELS),
        "fault_target": "pod",
        "fault_action": "delete",
    }
    spec.update(over)
    return spec


def _absence_probe(
    tc_id: str = "probe-1",
    v_args: str | None = None,
    tool: str = "kubectl",
) -> AIMessage:
    if v_args is None:
        v_args = f"pods -n {ABSENCE_NS} -l {_SELECTOR} -o wide"
    return AIMessage(content="", tool_calls=[{
        "name": tool,
        "id": tc_id,
        "args": {"subcommand": "get", "v_args": v_args},
    }])


def _empty_receipt(tc_id: str = "probe-1", tool: str = "kubectl") -> ToolMessage:
    """The framework-generated receipt (models cannot write ToolMessages)."""
    return ToolMessage(
        content=f"\n\n{EMPTY_SELECTOR_HINT}",
        name=tool,
        tool_call_id=tc_id,
    )


def _cli_empty_receipt(tc_id: str = "probe-1", tool: str = "kubectl") -> ToolMessage:
    """Direct-connection receipt: the local CLI table printer renders an
    empty match set as ``No resources found in <ns> namespace.`` (exit 0,
    kubernetes/kubectl#1596) with the hint appended after it — the guard
    must anchor this form as readily as the server-side empty stdout."""
    return ToolMessage(
        content=(
            # Real shape: the CLI line ends with its own newline, then the
            # generator appends "\n\n" + hint → three newlines total.
            f"No resources found in {ABSENCE_NS} namespace.\n\n\n"
            f"{EMPTY_SELECTOR_HINT}"
        ),
        name=tool,
        tool_call_id=tc_id,
    )


def _absence_state(messages, **kw) -> dict:
    state = {"messages": messages, "fault_spec": _selector_spec()}
    state.update(kw)
    return state


class TestTargetAbsenceProof:
    def test_empty_selector_receipt_unlocks_the_replan(self):
        """The incident shape verbatim: probe the approved selector, get the
        framework's empty-set receipt back — the replan must fire."""
        state = _absence_state([_absence_probe(), _empty_receipt()])
        assert _target_absence_proven_in_epoch(state) is True
        assert _review_replan_request(state, _request("plan_invalid")) is None

    def test_receipt_with_foreign_selector_still_rejected(self):
        """An empty result on SOME OTHER selector proves nothing about the
        approved target."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-9",
            "args": {
                "subcommand": "get",
                "v_args": f"pods -n {ABSENCE_NS} -l app=totally-different",
            },
        }])
        state = _absence_state([probe, _empty_receipt("probe-9")])
        assert _target_absence_proven_in_epoch(state) is False
        assert _review_replan_request(state, _request("plan_invalid")) is not None

    def test_nonempty_result_is_not_absence(self):
        """A get that RETURNED pods has no receipt — target still there."""
        probe = _absence_probe(tc_id="probe-2")
        receipt = ToolMessage(
            content="NAME  READY  STATUS\napi-pod-1  1/1  Running",
            name="kubectl",
            tool_call_id="probe-2",
        )
        state = _absence_state([probe, receipt])
        assert _target_absence_proven_in_epoch(state) is False

    def test_probe_before_epoch_boundary_does_not_count(self):
        """Probes under the OLD contract cannot unlock a replan for the
        current one — same contract boundary as the attempt proof."""
        pre_seam = [_absence_probe(), _empty_receipt()]
        state = _absence_state(
            pre_seam + [HumanMessage(content="[PLAN CHANGE APPROVED]")],
            attribution_epoch_index=len(pre_seam),
        )
        assert _target_absence_proven_in_epoch(state) is False

    def test_wrong_namespace_receipt_still_rejected(self):
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-3",
            "args": {
                "subcommand": "get",
                "v_args": f"pods -n other-ns -l {_SELECTOR}",
            },
        }])
        state = _absence_state([probe, _empty_receipt("probe-3")])
        assert _target_absence_proven_in_epoch(state) is False

    def test_events_probe_is_not_pod_absence(self):
        """An empty EVENTS list proves nothing about pods — the probe must
        list the approved resource kind."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-4",
            "args": {
                "subcommand": "get",
                "v_args": f"events -n {ABSENCE_NS} -l {_SELECTOR}",
            },
        }])
        state = _absence_state([probe, _empty_receipt("probe-4")])
        assert _target_absence_proven_in_epoch(state) is False

    def test_name_targeting_spec_out_of_scope(self):
        """Explicit-names specs need per-name NotFound evidence — a selector
        receipt is not that shape."""
        probe = _absence_probe(tc_id="probe-5")
        state = _absence_state(
            [probe, _empty_receipt("probe-5")],
            fault_spec=_selector_spec(names=["api-pod-1"], labels={}),
        )
        assert _target_absence_proven_in_epoch(state) is False

    def test_kubectl_read_probe_counts(self):
        """Both read channels share the impl, so both carry the receipt."""
        probe = _absence_probe(tc_id="probe-6", tool="kubectl_read")
        state = _absence_state([
            probe, _empty_receipt("probe-6", tool="kubectl_read"),
        ])
        assert _target_absence_proven_in_epoch(state) is True

    def test_long_form_flags_anchor_too(self):
        """VALUE matching, not flag spelling — the long forms anchor the
        receipt to the approved target just as well. ``--selector`` is
        used in its spaced form because that is the only form kubectl.py's
        hint generator recognises (``--selector=`` alone gets no hint,
        so it can never carry a real receipt)."""
        v_args = f"pods --namespace={ABSENCE_NS} --selector {_SELECTOR}"
        probe = _absence_probe(tc_id="probe-7", v_args=v_args)
        state = _absence_state([probe, _empty_receipt("probe-7")])
        assert _target_absence_proven_in_epoch(state) is True

    def test_prefix_namespace_is_not_anchored(self):
        """``demo`` must not anchor ``demo-prod`` — an empty set in a
        LONGER namespace is an empty set somewhere else."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-10",
            "args": {
                "subcommand": "get",
                "v_args": f"pods -n {ABSENCE_NS}-prod -l {_SELECTOR}",
            },
        }])
        state = _absence_state([probe, _empty_receipt("probe-10")])
        assert _target_absence_proven_in_epoch(state) is False

    def test_prefix_label_value_is_not_anchored(self):
        """``app=foo`` must not anchor ``app=foobar`` — a longer VALUE is a
        different selector."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-11",
            "args": {
                "subcommand": "get",
                "v_args": f"pods -n {ABSENCE_NS} -l app=foobar",
            },
        }])
        state = _absence_state(
            [probe, _empty_receipt("probe-11")],
            fault_spec=_selector_spec(labels={"app": "foo"}),
        )
        assert _target_absence_proven_in_epoch(state) is False

    def test_prefix_label_key_is_not_anchored(self):
        """``app=foo`` must not anchor ``xapp=foo`` — a longer KEY is a
        different selector."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-12",
            "args": {
                "subcommand": "get",
                "v_args": f"pods -n {ABSENCE_NS} -l xapp=foo",
            },
        }])
        state = _absence_state(
            [probe, _empty_receipt("probe-12")],
            fault_spec=_selector_spec(labels={"app": "foo"}),
        )
        assert _target_absence_proven_in_epoch(state) is False

    def test_duplicated_tool_call_id_cannot_pair_across_calls(self):
        """Adversarial id reuse: a foreign-selector empty receipt and a
        spec-anchored (non-empty) call sharing one ``tool_call_id`` must
        NOT pair into a fabricated absence proof."""
        first = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "dup-1",
            "args": {
                "subcommand": "get",
                "v_args": f"pods -n {ABSENCE_NS} -l app=other",
            },
        }])
        second = _absence_probe(tc_id="dup-1")
        second_receipt = ToolMessage(
            content="NAME  READY  STATUS\napi-pod-1  1/1  Running",
            name="kubectl",
            tool_call_id="dup-1",
        )
        state = _absence_state([
            first, _empty_receipt("dup-1"), second, second_receipt,
        ])
        assert _target_absence_proven_in_epoch(state) is False

    def test_mixed_names_and_labels_out_of_scope(self):
        """A spec with explicit names PLUS labels needs per-name NotFound
        evidence — a selector-only empty set cannot prove the whole
        target gone (combination semantics are the planner's to re-check)."""
        probe = _absence_probe(tc_id="probe-13")
        state = _absence_state(
            [probe, _empty_receipt("probe-13")],
            fault_spec=_selector_spec(names=["api-pod-1"]),
        )
        assert _target_absence_proven_in_epoch(state) is False

    def test_pod_spec_without_namespace_out_of_scope(self):
        """An empty-namespace pod spec cannot anchor: a probe without -n
        queries only the DEFAULT namespace, and its empty set proves
        nothing about the (unspecified) approved target."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-14",
            "args": {
                "subcommand": "get",
                "v_args": f"pods -l {_SELECTOR}",
            },
        }])
        state = _absence_state(
            [probe, _empty_receipt("probe-14")],
            fault_spec=_selector_spec(namespace=""),
        )
        assert _target_absence_proven_in_epoch(state) is False

    def test_node_scope_absence_proven_by_node_probe(self):
        """Nodes are cluster-scoped: no namespace anchor, kind check on
        ``nodes`` — an empty node list under the approved selector is a
        sound absence proof for a node-scope contract."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-15",
            "args": {
                "subcommand": "get",
                "v_args": f"nodes -l {_SELECTOR}",
            },
        }])
        state = _absence_state(
            [probe, _empty_receipt("probe-15")],
            fault_spec=_selector_spec(scope="node", namespace=""),
        )
        assert _target_absence_proven_in_epoch(state) is True

    def test_node_scope_with_pod_probe_is_kind_confusion(self):
        """An empty POD list proves nothing about NODES — the probed kind
        must match the target's."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-16",
            "args": {
                "subcommand": "get",
                "v_args": f"pods -l {_SELECTOR}",
            },
        }])
        state = _absence_state(
            [probe, _empty_receipt("probe-16")],
            fault_spec=_selector_spec(scope="node", namespace=""),
        )
        assert _target_absence_proven_in_epoch(state) is False

    def test_empty_scope_out_of_scope(self):
        """No scope → the probed resource kind cannot be verified against
        the target's; refuse the proof."""
        probe = _absence_probe(tc_id="probe-17")
        state = _absence_state(
            [probe, _empty_receipt("probe-17")],
            fault_spec=_selector_spec(scope=""),
        )
        assert _target_absence_proven_in_epoch(state) is False

    def test_narrower_selector_extra_pair_proves_nothing(self):
        """A probe with an EXTRA pair is NARROWER than the approved
        selector — it can be empty while the approved target thrives
        (the extra pair matches nothing). An honest over-constrained
        probe must not unlock a replan."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-18",
            "args": {
                "subcommand": "get",
                "v_args": f"pods -n {ABSENCE_NS} -l app=foo,component=web",
            },
        }])
        state = _absence_state(
            [probe, _empty_receipt("probe-18")],
            fault_spec=_selector_spec(labels={"app": "foo"}),
        )
        assert _target_absence_proven_in_epoch(state) is False

    def test_wider_selector_dropped_pair_is_sound(self):
        """A probe that DROPS a pair is WIDER: its match set contains the
        approved one, so an empty result still proves absence."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-19",
            "args": {
                "subcommand": "get",
                "v_args": (
                    f"pods -n {ABSENCE_NS} "
                    "-l app.kubernetes.io/instance=apiserver-flowlimit"
                ),
            },
        }])
        state = _absence_state([probe, _empty_receipt("probe-19")])
        assert _target_absence_proven_in_epoch(state) is True

    def test_last_selector_wins_for_pairing(self):
        """kubectl takes the LAST ``-l``; the proof must read the same
        one. A stale selector first and the approved one last anchors
        soundly."""
        v_args = f"pods -n {ABSENCE_NS} -l app=stale -l {_SELECTOR}"
        probe = _absence_probe(tc_id="probe-20", v_args=v_args)
        state = _absence_state([probe, _empty_receipt("probe-20")])
        assert _target_absence_proven_in_epoch(state) is True

    def test_last_selector_wins_attack_is_rejected(self):
        """Approved selector FIRST, foreign selector LAST: kubectl
        queried the foreign one, so the empty receipt is not about the
        target."""
        v_args = f"pods -n {ABSENCE_NS} -l {_SELECTOR} -l app=foreign"
        probe = _absence_probe(tc_id="probe-21", v_args=v_args)
        state = _absence_state([probe, _empty_receipt("probe-21")])
        assert _target_absence_proven_in_epoch(state) is False

    def test_cli_no_resources_receipt_unlocks_the_proof(self):
        """Direct-connection receipts: the local CLI renders an empty
        match set as ``No resources found in <ns> namespace.`` (exit 0)
        with the hint appended after it. The guard anchors on the hint
        text, so this form must unlock exactly like the server-side
        empty-stdout receipt — otherwise the escape is dead code on the
        kubeconfig channel."""
        probe = _absence_probe(tc_id="probe-29")
        state = _absence_state([probe, _cli_empty_receipt("probe-29")])
        assert _target_absence_proven_in_epoch(state) is True

    def test_structured_context_arg_probe_proves_nothing(self):
        """The cluster-switch rejection has TWO entry points: v_args
        ``--context`` strings AND the STRUCTURED ``context`` tool arg that
        ``_build_kubectl_global_args`` injects as the same global flag.
        An emptiness observed in another cluster proves nothing here.
        Round 8: only the v_args form was guarded."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-30",
            "args": {
                "subcommand": "get",
                "v_args": f"pods -n {ABSENCE_NS} -l {_SELECTOR}",
                "context": "other-cluster",
            },
        }])
        state = _absence_state([probe, _empty_receipt("probe-30")])
        assert _target_absence_proven_in_epoch(state) is False

    def test_structured_cluster_arg_probe_proves_nothing(self):
        """Same bypass via the structured ``cluster`` tool arg."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-31",
            "args": {
                "subcommand": "get",
                "v_args": f"pods -n {ABSENCE_NS} -l {_SELECTOR}",
                "cluster": "other-cluster",
            },
        }])
        state = _absence_state([probe, _empty_receipt("probe-31")])
        assert _target_absence_proven_in_epoch(state) is False

    def test_all_namespaces_probe_is_sound(self):
        """``-A`` covers every namespace, so the probe's match set
        CONTAINS the approved one — the same superset soundness as
        dropping selector pairs. Emptiness without a ``-n`` equality is
        still a valid absence proof."""
        probe = _absence_probe(
            tc_id="probe-32", v_args=f"pods -A -l {_SELECTOR}",
        )
        state = _absence_state([probe, _empty_receipt("probe-32")])
        assert _target_absence_proven_in_epoch(state) is True

    def test_all_namespaces_long_and_true_forms_are_sound(self):
        """``--all-namespaces`` (bare) and ``--all-namespaces=true`` are
        the pflag-legal enable forms and anchor like ``-A``."""
        for i, flag in enumerate(("--all-namespaces", "--all-namespaces=true")):
            probe = _absence_probe(
                tc_id=f"probe-33-{i}", v_args=f"pods {flag} -l {_SELECTOR}",
            )
            state = _absence_state([probe, _empty_receipt(f"probe-33-{i}")])
            assert _target_absence_proven_in_epoch(state) is True, flag

    def test_all_namespaces_false_form_still_needs_namespace(self):
        """``--all-namespaces=false`` EXPLICITLY disables the superset
        semantics — the probe is then a default-namespace query and
        must anchor through the ``-n`` equality like any other probe
        (the scanner parses ``=false`` as the flag's negation)."""
        probe = _absence_probe(
            tc_id="probe-34",
            v_args=f"pods --all-namespaces=false -l {_SELECTOR}",
        )
        state = _absence_state([probe, _empty_receipt("probe-34")])
        assert _target_absence_proven_in_epoch(state) is False

    def test_global_endpoint_and_identity_flags_refuse_the_proof(self):
        """Round 9: ``--server``/``--as``/``--token`` redirect the query
        to another endpoint or identity — an empty set THERE proves
        nothing about the approved cluster. They reached kubectl via
        ``_split_args`` passthrough yet no enumeration listed them; the
        closed flag grammar refuses any unlisted dash token outright."""
        for i, extra in enumerate((
            "--server=https://elsewhere:6443",
            "--as=nobody",
            "--token=abc",
        )):
            probe = _absence_probe(
                tc_id=f"probe-35-{i}",
                v_args=f"pods -n {ABSENCE_NS} -l {_SELECTOR} {extra}",
            )
            state = _absence_state([probe, _empty_receipt(f"probe-35-{i}")])
            assert _target_absence_proven_in_epoch(state) is False, extra

    def test_namespace_flag_valued_dash_A_is_not_all_namespaces(self):
        """pflag consumes the next token as ``-n``'s value
        UNCONDITIONALLY: in ``-n -A`` the ``-A`` is the NAMESPACE, not
        the all-namespaces flag — the query hit a (nonexistent)
        namespace ``-A``. Round-8 membership scanning treated it as the
        flag and skipped the namespace check (fail-open)."""
        probe = _absence_probe(
            tc_id="probe-36",
            v_args=f"pods -n -A -l {_SELECTOR}",
        )
        state = _absence_state([probe, _empty_receipt("probe-36")])
        assert _target_absence_proven_in_epoch(state) is False

    def test_all_namespaces_with_namespace_flag_is_ambiguous(self):
        """``-A`` alongside ``-n other`` leaves the receipt's effective
        scope unreadable from the command line — refuse rather than
        guess which of the two kubectl honoured."""
        probe = _absence_probe(
            tc_id="probe-37",
            v_args=f"pods -A -n other-ns -l {_SELECTOR}",
        )
        state = _absence_state([probe, _empty_receipt("probe-37")])
        assert _target_absence_proven_in_epoch(state) is False

    def test_positional_resource_name_narrows_the_query(self):
        """``pods web-1`` limits the get to ONE named resource; its
        emptiness proves nothing about the selector-defined set."""
        probe = _absence_probe(
            tc_id="probe-38",
            v_args=f"pods web-1 -n {ABSENCE_NS} -l {_SELECTOR}",
        )
        state = _absence_state([probe, _empty_receipt("probe-38")])
        assert _target_absence_proven_in_epoch(state) is False

    def test_display_only_bool_flags_anchor(self):
        """``--show-labels``/``--no-headers`` only change row RENDERING
        — an empty result is still an empty match set, so honest probes
        carrying them must not be refused."""
        for i, extra in enumerate(("--show-labels", "--no-headers")):
            probe = _absence_probe(
                tc_id=f"probe-39-{i}",
                v_args=f"pods -n {ABSENCE_NS} -l {_SELECTOR} {extra}",
            )
            state = _absence_state([probe, _empty_receipt(f"probe-39-{i}")])
            assert _target_absence_proven_in_epoch(state) is True, extra

    def test_all_namespaces_false_with_namespace_anchors(self):
        """``--all-namespaces=false -n approved``: the negated flag
        changes nothing — the ``-n`` equality carries the proof."""
        probe = _absence_probe(
            tc_id="probe-40",
            v_args=f"pods --all-namespaces=false -n {ABSENCE_NS} -l {_SELECTOR}",
        )
        state = _absence_state([probe, _empty_receipt("probe-40")])
        assert _target_absence_proven_in_epoch(state) is True

    def test_field_selector_probe_proves_nothing(self):
        """Empty under a ``--field-selector`` intersection is
        unattributable — the field filter alone may have emptied the
        result."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-22",
            "args": {
                "subcommand": "get",
                "v_args": (
                    f"pods -n {ABSENCE_NS} -l {_SELECTOR} "
                    "--field-selector spec.nodeName=node-1"
                ),
            },
        }])
        state = _absence_state([probe, _empty_receipt("probe-22")])
        assert _target_absence_proven_in_epoch(state) is False

    def test_template_output_probe_proves_nothing(self):
        """A jsonpath template renders existing resources to nothing —
        empty output is not an empty match set."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-23",
            "args": {
                "subcommand": "get",
                "v_args": (
                    f"pods -n {ABSENCE_NS} -l {_SELECTOR} "
                    "-o jsonpath={.items[9].metadata.name}"
                ),
            },
        }])
        state = _absence_state([probe, _empty_receipt("probe-23")])
        assert _target_absence_proven_in_epoch(state) is False

    def test_context_switch_probe_proves_nothing(self):
        """``--context`` points the query at another cluster; its empty
        set says nothing about the approved one."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-24",
            "args": {
                "subcommand": "get",
                "v_args": (
                    f"pods -n {ABSENCE_NS} -l {_SELECTOR} --context other"
                ),
            },
        }])
        state = _absence_state([probe, _empty_receipt("probe-24")])
        assert _target_absence_proven_in_epoch(state) is False

    def test_kubeconfig_flag_is_allowed(self):
        """Skill recipes mandate an explicit --kubeconfig; a probe
        carrying it still anchors (bounded-risk allowance, see the
        guard docstring)."""
        v_args = (
            f"pods --kubeconfig=/root/.kube/config "
            f"-n {ABSENCE_NS} -l {_SELECTOR}"
        )
        probe = _absence_probe(tc_id="probe-25", v_args=v_args)
        state = _absence_state([probe, _empty_receipt("probe-25")])
        assert _target_absence_proven_in_epoch(state) is True

    def test_last_namespace_wins(self):
        """``-n approved -n other`` queries OTHER (pflag last-wins) — the
        receipt is not about the approved namespace."""
        v_args = f"pods -n {ABSENCE_NS} -n other-ns -l {_SELECTOR}"
        probe = _absence_probe(tc_id="probe-26", v_args=v_args)
        state = _absence_state([probe, _empty_receipt("probe-26")])
        assert _target_absence_proven_in_epoch(state) is False

    def test_quoted_selector_anchors(self):
        """A quoted selector token (``-l 'a=1,b=2'``) anchors once the
        matched quotes are stripped."""
        v_args = f"pods -n {ABSENCE_NS} -l '{_SELECTOR}'"
        probe = _absence_probe(tc_id="probe-27", v_args=v_args)
        state = _absence_state([probe, _empty_receipt("probe-27")])
        assert _target_absence_proven_in_epoch(state) is True

    def test_pdb_probe_is_not_pod_absence(self):
        """Kind confusion through a shared PREFIX: an empty
        poddisruptionbudget list proves nothing about pods. Exact
        resource-name matching, not startswith — the events test below
        only passed by prefix luck."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-28",
            "args": {
                "subcommand": "get",
                "v_args": f"poddisruptionbudgets -n {ABSENCE_NS} -l app=foo",
            },
        }])
        state = _absence_state(
            [probe, _empty_receipt("probe-28")],
            fault_spec=_selector_spec(labels={"app": "foo"}),
        )
        assert _target_absence_proven_in_epoch(state) is False

    def test_jsonpath_file_output_proves_nothing(self):
        """A template FILE renders existing resources to nothing just
        like an inline template — and contains no ``jsonpath=``
        substring for a blacklist to catch. Only an output-format
        allowlist closes this."""
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-29",
            "args": {
                "subcommand": "get",
                "v_args": (
                    f"pods -n {ABSENCE_NS} -l app=foo "
                    "-o jsonpath-file=/tmp/tpl"
                ),
            },
        }])
        state = _absence_state(
            [probe, _empty_receipt("probe-29")],
            fault_spec=_selector_spec(labels={"app": "foo"}),
        )
        assert _target_absence_proven_in_epoch(state) is False

    def test_go_template_file_output_proves_nothing(self):
        probe = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "probe-30",
            "args": {
                "subcommand": "get",
                "v_args": (
                    f"pods -n {ABSENCE_NS} -l app=foo "
                    "-o go-template-file=/tmp/tpl"
                ),
            },
        }])
        state = _absence_state(
            [probe, _empty_receipt("probe-30")],
            fault_spec=_selector_spec(labels={"app": "foo"}),
        )
        assert _target_absence_proven_in_epoch(state) is False

    def test_equals_joined_short_flags_anchor(self):
        """pflag accepts ``-n=ns`` / ``-o=wide`` — an honest probe in
        that form must not be lost (over-strict rejection forces a
        redundant re-probe)."""
        v_args = f"pods -n={ABSENCE_NS} -l {_SELECTOR} -o=wide"
        probe = _absence_probe(tc_id="probe-31", v_args=v_args)
        state = _absence_state([probe, _empty_receipt("probe-31")])
        assert _target_absence_proven_in_epoch(state) is True

    def test_mixed_flag_forms_last_position_wins(self):
        """``-n approved -n=other``: the ``=``-form appears LATER on the
        command line, so kubectl queried OTHER — parsing must honour
        token order across forms, not form priority."""
        v_args = f"pods -n {ABSENCE_NS} -n=other-ns -l {_SELECTOR}"
        probe = _absence_probe(tc_id="probe-32", v_args=v_args)
        state = _absence_state([probe, _empty_receipt("probe-32")])
        assert _target_absence_proven_in_epoch(state) is False

    def test_output_name_format_is_row_faithful(self):
        """``-o name`` prints one line per match — empty output is a
        faithful empty match set and anchors."""
        v_args = f"pods -n {ABSENCE_NS} -l {_SELECTOR} -o name"
        probe = _absence_probe(tc_id="probe-33", v_args=v_args)
        state = _absence_state([probe, _empty_receipt("probe-33")])
        assert _target_absence_proven_in_epoch(state) is True


class TestHandleReplanAbsenceEscape:
    def test_proven_absence_fires_the_seam_instead_of_rejecting(self):
        """The deadlock case end-to-end: a request_replan tool call over a
        proven-empty target FIRES the replan seam (budget spent, routing
        flag set) instead of setting the deferred rejection."""
        response = AIMessage(content="", tool_calls=[{
            "name": "request_replan",
            "id": "rr-3",
            "args": {
                "kind": "feasibility",
                "decision": "plan_invalid",
                "invalidated_assumption": "target pods exist",
                "affected_step": "inject",
                "changes_target_or_risk": True,
            },
        }])
        state = _absence_state([_absence_probe(), _empty_receipt()])
        result: dict = {}
        _handle_replan(response, state, result)
        assert "_replan_review_rejection" not in result
        assert result.get("replan_requested") is True
        assert result.get("replan_count") == 1
        # Target change re-arms the user confirmation gate.
        assert result.get("needs_confirmation") is True
