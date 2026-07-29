from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.replan import (
    REPLAN_REQUEST_CLOSE,
    REPLAN_REQUEST_OPEN,
    ReplanRequest,
    parse_replan_request,
)


def _content(**overrides) -> str:
    payload = {
        "kind": "feasibility",
        "decision": "plan_invalid",
        "invalidated_assumption": "the selected injection capability is present",
        "observed_evidence": ["tool output says capability is unavailable"],
        "evidence_refs": ["tool output says capability is unavailable"],
        "affected_step": "create experiment",
        "unresolved_questions": ["which capability remains available?"],
        "changes_target_or_risk": False,
    }
    payload.update(overrides)
    serialized = ReplanRequest(
        **payload,
    ).model_dump_json()
    return f"{REPLAN_REQUEST_OPEN}{serialized}{REPLAN_REQUEST_CLOSE}"


def test_parse_replan_request_requires_complete_typed_payload():
    request = parse_replan_request(_content(changes_target_or_risk=True))

    assert request is not None
    assert request.affected_step == "create experiment"
    assert request.changes_target_or_risk is True
    assert parse_replan_request(f"{REPLAN_REQUEST_OPEN}{{}}{REPLAN_REQUEST_CLOSE}") is None
    assert parse_replan_request("[REPLAN] retry another method") is None


def test_execute_loop_records_structured_replan_request():
    from chaos_agent.agent.nodes.execute.execute_loop import _handle_replan

    result: dict = {}
    _handle_replan(
        AIMessage(content=_content()),
        {"messages": [], "replan_count": 0, "execute_loop_count": 1},
        result,
    )

    assert result["replan_requested"] is True
    assert result["replan_request"]["affected_step"] == "create experiment"
    assert result["replan_context"]["evidence_refs"] == []
    assert result["replan_context"]["model_evidence_refs"] == [
        "tool output says capability is unavailable",
    ]


def test_plan_invalid_replan_uses_runtime_evidence_not_model_tool_call_ids():
    from chaos_agent.agent.nodes.execute.execute_loop import _handle_replan

    result: dict = {}
    _handle_replan(
        AIMessage(content=_content(evidence_refs=[
            "kubectl exec chroot /host returned REJECT_BANNED",
        ])),
        {
            "messages": [
                ToolMessage(
                    content="[target_guard] REJECT_BANNED - host mutation denied",
                    name="kubectl",
                    tool_call_id="call-runtime-failure",
                    status="error",
                ),
            ],
            "replan_count": 0,
            "execute_loop_count": 1,
        },
        result,
    )

    assert result["replan_requested"] is True
    assert result["replan_context"]["evidence_refs"] == ["call-runtime-failure"]
    assert result["replan_context"]["model_evidence_refs"] == [
        "kubectl exec chroot /host returned REJECT_BANNED",
    ]


def test_plan_invalid_replan_records_blade_failure_call_id():
    from chaos_agent.agent.nodes.execute.execute_loop import _handle_replan

    result: dict = {}
    _handle_replan(
        AIMessage(content=_content(evidence_refs=[])),
        {
            "messages": [
                ToolMessage(
                    content="Error: injection failed permanently",
                    name="blade_create",
                    tool_call_id="call-blade-failure",
                    status="error",
                ),
            ],
            "replan_count": 0,
            "execute_loop_count": 1,
        },
        result,
    )

    assert result["replan_requested"] is True
    assert result["replan_context"]["evidence_refs"] == ["call-blade-failure"]


def test_target_or_risk_replan_requires_a_new_confirmation_gate():
    from chaos_agent.agent.nodes.execute.execute_loop import _handle_replan

    result: dict = {}
    _handle_replan(
        AIMessage(content=_content(changes_target_or_risk=True)),
        {"messages": [], "replan_count": 0, "execute_loop_count": 1},
        result,
    )

    assert result["needs_confirmation"] is True


def test_needs_investigation_does_not_leave_execute_loop():
    from chaos_agent.agent.nodes.execute.execute_loop import _handle_replan

    result: dict = {}
    _handle_replan(
        AIMessage(content=_content(decision="needs_investigation")),
        {"messages": [], "replan_count": 0, "execute_loop_count": 1},
        result,
    )

    assert "replan_requested" not in result
    assert "LIFECYCLE REVIEW" in result["messages"][-1].content


def _ai_with_request_replan(tool_calls) -> AIMessage:
    return AIMessage(content="", tool_calls=tool_calls)


def _request_replan_tool_call(call_id="call-replan-1", **overrides):
    args = {
        "kind": "feasibility",
        "decision": "plan_invalid",
        "invalidated_assumption": "the selected injection capability is present",
        "observed_evidence": ["tool output says capability is unavailable"],
        "affected_step": "create experiment",
        "unresolved_questions": ["which capability remains available?"],
        "changes_target_or_risk": False,
    }
    args.update(overrides)
    return {"name": "request_replan", "args": args, "id": call_id, "type": "tool_call"}


def test_request_replan_tool_call_fires_replan_and_answers_tool_call():
    from chaos_agent.agent.nodes.execute.execute_loop import _handle_replan

    result: dict = {"messages": [_ai_with_request_replan([_request_replan_tool_call()])]}
    _handle_replan(
        result["messages"][0],
        {"messages": [], "replan_count": 0, "execute_loop_count": 1},
        result,
    )

    assert result["replan_requested"] is True
    assert result["replan_request"]["affected_step"] == "create experiment"
    # The request_replan tool_call is answered so the history stays well-formed
    # once we route to Phase 1 (it never reaches the ToolNode).
    answers = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(answers) == 1
    assert answers[0].tool_call_id == "call-replan-1"


def test_request_replan_tool_call_with_null_optional_lists_still_fires():
    # The tool signature declares optional lists as ``list = None``, so a model
    # may emit an explicit ``observed_evidence=null``. ReplanRequest types those
    # as list[str] (non-nullable): without None-stripping this would be misread
    # as "malformed" and a real plan_invalid replan would be silently dropped.
    from chaos_agent.agent.nodes.execute.execute_loop import _handle_replan

    tc = _request_replan_tool_call(
        observed_evidence=None, evidence_refs=None, unresolved_questions=None
    )
    result: dict = {"messages": [_ai_with_request_replan([tc])]}
    _handle_replan(
        result["messages"][0],
        {"messages": [], "replan_count": 0, "execute_loop_count": 1},
        result,
    )

    assert result["replan_requested"] is True
    assert result["replan_request"]["observed_evidence"] == []
    answers = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(answers) == 1


def test_request_replan_tool_call_answers_sibling_tool_calls():
    from chaos_agent.agent.nodes.execute.execute_loop import _handle_replan

    sibling = {"name": "kubectl", "args": {"subcommand": "get"}, "id": "call-kubectl", "type": "tool_call"}
    response = _ai_with_request_replan([_request_replan_tool_call(), sibling])
    result: dict = {"messages": [response]}
    _handle_replan(
        response,
        {"messages": [], "replan_count": 0, "execute_loop_count": 1},
        result,
    )

    assert result["replan_requested"] is True
    answered_ids = {m.tool_call_id for m in result["messages"] if isinstance(m, ToolMessage)}
    assert answered_ids == {"call-replan-1", "call-kubectl"}


def test_request_replan_tool_call_needs_investigation_flows_through_phase2():
    from chaos_agent.agent.nodes.execute.execute_loop import _handle_replan

    response = _ai_with_request_replan(
        [_request_replan_tool_call(decision="needs_investigation")]
    )
    result: dict = {"messages": [response]}
    _handle_replan(
        response,
        {"messages": [], "replan_count": 0, "execute_loop_count": 1},
        result,
    )

    # No Phase-1 replan. The tool_call is deliberately left UNANSWERED so it
    # flows once through phase2_tools (routing "continue" -> phase2_tools). We
    # must NOT synthesize a ToolMessage here (phase2_tools would re-execute the
    # call -> a duplicate answer for the same tool_call_id) and must NOT inject
    # a HumanMessage (would break tool-response adjacency).
    assert "replan_requested" not in result
    assert result["messages"] == [response]
    assert not any(isinstance(m, ToolMessage) for m in result["messages"])
    assert not any(isinstance(m, HumanMessage) for m in result["messages"])


def test_request_replan_classified_readonly():
    from chaos_agent.agent.target_guard.classifier import (
        SCOPE_READONLY,
        infer_effective_target,
    )

    et = infer_effective_target("request_replan", {"kind": "feasibility"})
    assert et.scope == SCOPE_READONLY


def test_request_replan_tool_execution_accepts_null_optional_lists():
    # needs_investigation lets the request_replan call flow through phase2_tools
    # (the ToolNode actually executes it). The tool's own arg schema must accept
    # an explicit ``null`` for the optional lists (signature ``list | None``),
    # else the model's signal degrades into a ValidationError ToolMessage.
    from chaos_agent.agent.replan import request_replan

    out = request_replan.invoke({
        "kind": "feasibility",
        "decision": "needs_investigation",
        "invalidated_assumption": "x",
        "affected_step": "y",
        "observed_evidence": None,
        "evidence_refs": None,
        "unresolved_questions": None,
    })
    assert out == "Replan request recorded."


def test_replan_request_coerces_json_string_and_scalar_lists():
    # qwen frequently emits list fields as a JSON-encoded string or a bare
    # scalar string. The field_validator must coerce both to list[str] (path (1):
    # ReplanRequest.model_validate) so a real replan is not dropped as malformed.
    req = ReplanRequest.model_validate({
        "kind": "feasibility",
        "decision": "plan_invalid",
        "invalidated_assumption": "x",
        "affected_step": "y",
        "observed_evidence": '["a", "b"]',   # JSON-encoded list string
        "evidence_refs": "single ref",        # bare scalar string
        "unresolved_questions": "[]",         # empty JSON list
    })
    assert req.observed_evidence == ["a", "b"]
    assert req.evidence_refs == ["single ref"]
    assert req.unresolved_questions == []
    # A valid list is passed through unchanged.
    assert parse_replan_request(_content()).observed_evidence == [
        "tool output says capability is unavailable",
    ]


def test_replan_request_coerces_numeric_list_elements():
    # Models sometimes emit numeric evidence (e.g. HTTP status codes) inside the
    # list. list[str] would reject raw ints and drop the whole replan signal, so
    # scalar elements are stringified — both as a real list and as a JSON string.
    req = ReplanRequest.model_validate({
        "kind": "feasibility",
        "decision": "plan_invalid",
        "invalidated_assumption": "x",
        "affected_step": "y",
        "observed_evidence": [500, 502],      # native list of ints
        "evidence_refs": "[1, 2]",            # JSON string list of ints
        "unresolved_questions": [],
    })
    assert req.observed_evidence == ["500", "502"]
    assert req.evidence_refs == ["1", "2"]


def test_request_replan_tool_execution_accepts_json_string_lists():
    # needs_investigation flows through the ToolNode, which validates against the
    # @tool signature schema (NOT ReplanRequest). The list params are widened to
    # ``list | str | None`` so a JSON-string list does not degrade into a
    # ValidationError ToolMessage.
    from chaos_agent.agent.replan import request_replan

    out = request_replan.invoke({
        "kind": "feasibility",
        "decision": "needs_investigation",
        "invalidated_assumption": "x",
        "affected_step": "y",
        "observed_evidence": '["evidence a"]',
        "evidence_refs": "ref",
        "unresolved_questions": '["q1", "q2"]',
    })
    assert out == "Replan request recorded."


def test_request_replan_tool_call_coerces_json_string_lists():
    # Path (1) end-to-end: a plan_invalid tool call whose list args are JSON
    # strings must still fire a replan with correctly parsed lists.
    from chaos_agent.agent.nodes.execute.execute_loop import _handle_replan

    tc = _request_replan_tool_call(
        observed_evidence='["evidence a", "evidence b"]',
        unresolved_questions="single question",
    )
    result: dict = {"messages": [_ai_with_request_replan([tc])]}
    _handle_replan(
        result["messages"][0],
        {"messages": [], "replan_count": 0, "execute_loop_count": 1},
        result,
    )
    assert result["replan_requested"] is True
    assert result["replan_request"]["observed_evidence"] == ["evidence a", "evidence b"]
    assert result["replan_request"]["unresolved_questions"] == ["single question"]
