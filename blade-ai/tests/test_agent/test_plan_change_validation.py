"""No-silent-discard validation for plan changes (task-5193538b question 3).

A ``propose_plan_change`` call used to return "Plan change proposed." for a
PARTIAL contract, after which the router dropped it silently — the model
believed the change was submitted, no confirmation card ever appeared, and
execution continued against the stale contract. The fix:

  - the tool itself refuses partial contracts with the exact missing fields,
  - the router logs every remaining discard instead of dropping quietly,
  - stale-revision / unchanged-contract proposals are DELEGATED to
    ``plan_change_confirm``, which answers with an actionable
    [PLAN CHANGE RETRY] message.
"""

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.factory import _build_skill_tools
from chaos_agent.agent.router import route_after_phase1_tools
from chaos_agent.agent.spec.fault_spec import missing_full_proposal_fields

_ALL_FIELDS = (
    "scope", "target", "action", "namespace", "names", "labels", "params",
    "params_flags", "duration_seconds", "objective", "boundaries",
    "constraints", "assumptions",
)


def _fault_spec() -> dict:
    return {
        "revision": 4,
        "objective": "inject packet loss",
        "scope": "pod",
        "blade_target": "network",
        "blade_action": "drop",
        "namespace": "default",
        "names": ["nginx"],
        "labels": {"app": "web"},
        "params": {"percent": "100"},
        "params_flags": [],
        "duration_seconds": 0,
        "boundaries": ["staging only"],
        "constraints": ["one logical experiment"],
        "assumptions": [],
    }


def _full_proposal(spec: dict, **overrides) -> dict:
    proposal = {
        "objective": spec["objective"], "scope": spec["scope"],
        "target": spec["blade_target"], "action": spec["blade_action"],
        "namespace": spec["namespace"], "names": spec["names"],
        "labels": spec["labels"], "params": spec["params"],
        "params_flags": spec["params_flags"],
        "duration_seconds": spec["duration_seconds"],
        "boundaries": spec["boundaries"], "constraints": spec["constraints"],
        "assumptions": spec["assumptions"],
    }
    proposal.update(overrides)
    return proposal


def _propose_messages(args: dict, call_id: str = "change-1") -> list:
    return [
        AIMessage(content="", tool_calls=[{
            "name": "propose_plan_change", "id": call_id, "args": args,
        }]),
        ToolMessage(
            content="Plan change proposed",
            name="propose_plan_change",
            tool_call_id=call_id,
        ),
    ]


class TestMissingFullProposalFields:
    def test_complete_proposal_reports_nothing(self):
        assert missing_full_proposal_fields(_full_proposal(_fault_spec())) == []

    def test_non_dict_reports_every_field(self):
        assert missing_full_proposal_fields(None) == sorted(_ALL_FIELDS)

    def test_absent_keys_reported(self):
        partial = _full_proposal(_fault_spec())
        del partial["namespace"]
        del partial["boundaries"]
        assert sorted(missing_full_proposal_fields(partial)) == [
            "boundaries", "namespace",
        ]

    def test_falsy_identity_fields_reported(self):
        # Presence alone is not enough for the fault identity triple.
        partial = _full_proposal(_fault_spec(), scope="")
        assert "scope" in missing_full_proposal_fields(partial)

    def test_empty_container_fields_count_as_present(self):
        # labels={} / assumptions=[] are LEGITIMATE contents of a full
        # contract (the key exists); only the identity triple needs truth.
        proposal = _full_proposal(_fault_spec(), labels={}, assumptions=[])
        assert missing_full_proposal_fields(proposal) == []


class TestProposeToolRefusesPartialContract:
    @staticmethod
    def _tool(mock_registry):
        tools = _build_skill_tools(mock_registry)
        return next(t for t in tools if t.name == "propose_plan_change")

    def test_partial_contract_lists_missing_fields(self, mock_registry):
        tool = self._tool(mock_registry)
        partial = _full_proposal(_fault_spec())
        del partial["boundaries"]
        del partial["constraints"]
        result = tool.invoke({
            "reason": "target unreachable",
            "proposed_fault": partial,
            "fault_revision": 4,
        })
        assert result.startswith("Error:")
        assert "boundaries" in result and "constraints" in result

    def test_name_instead_of_names_gets_similar_key_hint(self, mock_registry):
        tool = self._tool(mock_registry)
        partial = _full_proposal(_fault_spec())
        del partial["names"]
        partial["name"] = "nginx"
        result = tool.invoke({
            "reason": "target unreachable",
            "proposed_fault": partial,
            "fault_revision": 4,
        })
        assert result.startswith("Error:")
        assert "'names'" in result

    def test_complete_contract_accepted(self, mock_registry):
        tool = self._tool(mock_registry)
        result = tool.invoke({
            "reason": "target unreachable",
            "proposed_fault": _full_proposal(_fault_spec(), action="delay"),
            "fault_revision": 4,
        })
        assert result.startswith("Plan change proposed")


class TestContractRouteNoSilentDiscard:
    def test_partial_proposal_stays_in_agent_loop(self):
        """The tool now refuses partials with an Error reply, but if one
        still reaches the router it must stay in the loop (and be logged),
        never routed to a confirm that cannot extract it."""
        spec = _fault_spec()
        partial = _full_proposal(spec, action="delay")
        del partial["boundaries"]
        state = {
            "messages": _propose_messages({
                "reason": "drop is not feasible",
                "fault_revision": 4,
                "proposed_fault": partial,
            }),
            "fault_spec": spec,
        }
        assert route_after_phase1_tools(state) == "agent_loop"

    def test_stale_revision_delegates_to_confirm_for_retry_message(self):
        """Behaviour change (task-5193538b): a stale-revision proposal used
        to vanish into the agent loop. plan_change_confirm now answers it
        with the [PLAN CHANGE RETRY] HumanMessage."""
        spec = _fault_spec()
        state = {
            "messages": _propose_messages({
                "reason": "drop is not feasible",
                "fault_revision": 3,  # current revision is 4
                "proposed_fault": _full_proposal(spec, action="delay"),
            }, call_id="change-stale"),
            "fault_spec": spec,
        }
        assert route_after_phase1_tools(state) == "plan_change_confirm"

    def test_unchanged_contract_delegates_to_confirm_for_retry_message(self):
        spec = _fault_spec()
        state = {
            "messages": _propose_messages({
                "reason": "no-op",
                "fault_revision": 4,
                "proposed_fault": _full_proposal(spec),  # identical contract
            }, call_id="change-noop"),
            "fault_spec": spec,
        }
        assert route_after_phase1_tools(state) == "plan_change_confirm"

    def test_current_revision_material_change_still_routes_to_confirm(self):
        spec = _fault_spec()
        state = {
            "messages": _propose_messages({
                "reason": "drop is not feasible",
                "fault_revision": 4,
                "proposed_fault": _full_proposal(spec, action="delay"),
            }),
            "fault_spec": spec,
        }
        assert route_after_phase1_tools(state) == "plan_change_confirm"
