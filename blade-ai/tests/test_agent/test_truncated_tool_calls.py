"""Truncation protection: tool calls from a token-limited response must not run.

``finish_reason == "length"`` means the response was cut off mid-emission.
Streamed tool arguments are finalized with a best-effort JSON parse, so a
truncated call can yield args that parse AND validate yet are silently
incomplete — a ``kubectl patch`` whose JSON body lost its tail, a
``blade create`` missing half its flags. Executing those against a live cluster
is unsafe, so the batch is neutralised and the model re-issues.

The two kinds of truncated call need OPPOSITE treatment. Measured against
DashScope (3 runs each, assistant message carrying one tool call):

    args valid JSON   + answered   → accepted
    args valid JSON   + unanswered → accepted   (pairing is NOT enforced)
    args invalid JSON + answered   → 400 "function.arguments" must be JSON
    args invalid JSON + unanswered → accepted

So parseable calls are answered, and calls with broken args are STRIPPED —
answering one is precisely what makes the provider parse its arguments and
reject the entire request.
"""

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.nodes.execute.react_helpers import handle_truncated_response
from chaos_agent.agent.nodes.planning.phase1_screener import (
    PHASE1_SCREENER_ROUTE_RETRY,
)
from chaos_agent.agent.nodes.planning.tool_screener import SCREENER_ROUTE_RETRY

PATCH_CALL = {
    "name": "kubectl",
    "args": {"subcommand": "patch", "v_args": '-p {"spec":{"template"'},
    "id": "c1",
}
GET_CALL = {"name": "kubectl_read", "args": {"subcommand": "get"}, "id": "c2"}
BROKEN_CALL = {
    "type": "invalid_tool_call",
    "id": "bad1",
    "name": "kubectl",
    "args": '{"subcommand":"patch"',
    "error": "not valid JSON",
}


def _truncated(**kwargs) -> AIMessage:
    kwargs.setdefault("content", "")
    return AIMessage(response_metadata={"finish_reason": "length"}, **kwargs)


class TestParseableCallsAreAnswered:
    def test_every_call_is_failed_unexecuted(self):
        msg, answers = handle_truncated_response(
            _truncated(tool_calls=[dict(PATCH_CALL), dict(GET_CALL)])
        )
        assert len(answers) == 2
        assert all(isinstance(m, ToolMessage) for m in answers)

    def test_each_call_gets_its_own_paired_answer(self):
        _, answers = handle_truncated_response(
            _truncated(tool_calls=[dict(PATCH_CALL), dict(GET_CALL)])
        )
        assert {m.tool_call_id for m in answers} == {"c1", "c2"}

    def test_message_states_nothing_ran(self):
        _, answers = handle_truncated_response(_truncated(tool_calls=[dict(PATCH_CALL)]))
        body = answers[0].content
        assert "NOT executed" in body
        assert "no state changed" in body
        assert "token limit" in body

    def test_parseable_calls_are_kept_on_the_message(self):
        """They are answered, so they may stay — and their args are valid JSON."""
        msg, answers = handle_truncated_response(_truncated(tool_calls=[dict(GET_CALL)]))
        assert [tc["id"] for tc in msg.tool_calls] == ["c2"]
        assert [m.tool_call_id for m in answers] == ["c2"]

    def test_call_without_id_is_skipped(self):
        """No tool_call_id means no pairing is possible; nothing to answer."""
        _, answers = handle_truncated_response(
            _truncated(tool_calls=[{"name": "kubectl", "args": {}, "id": ""}])
        )
        assert answers == []


class TestBrokenCallsAreStrippedNotAnswered:
    """Answering a call whose args are malformed makes DashScope reject the request.

    Regression guard: an earlier version of this fix answered ``invalid_tool_calls``
    as well, which turned an accepted-but-confusing turn into a hard 400.
    """

    def test_broken_call_is_removed_from_the_message(self):
        msg, answers = handle_truncated_response(
            _truncated(tool_calls=[], invalid_tool_calls=[dict(BROKEN_CALL)])
        )
        assert msg.invalid_tool_calls == []
        assert msg.tool_calls == []

    def test_broken_call_is_not_answered(self):
        _, answers = handle_truncated_response(
            _truncated(tool_calls=[], invalid_tool_calls=[dict(BROKEN_CALL)])
        )
        assert answers == []

    def test_outbound_payload_carries_no_malformed_arguments(self):
        """The decisive check — this is what the provider rejects."""
        import chaos_agent.agent.factory  # noqa: F401  (applies the patches)
        from langchain_openai.chat_models.base import _convert_message_to_dict

        msg, _ = handle_truncated_response(
            _truncated(
                content="部分文本",
                tool_calls=[],
                invalid_tool_calls=[dict(BROKEN_CALL)],
            )
        )
        payload = _convert_message_to_dict(msg)
        assert "tool_calls" not in payload
        assert payload["content"] == "部分文本"

    def test_mixed_batch_answers_valid_and_strips_broken(self):
        import json

        import chaos_agent.agent.factory  # noqa: F401
        from langchain_openai.chat_models.base import _convert_message_to_dict

        msg, answers = handle_truncated_response(
            _truncated(
                tool_calls=[dict(GET_CALL)], invalid_tool_calls=[dict(BROKEN_CALL)]
            )
        )
        assert [m.tool_call_id for m in answers] == ["c2"]
        payload = _convert_message_to_dict(msg)
        # Every emitted call is answered AND has parseable arguments.
        assert [tc["id"] for tc in payload["tool_calls"]] == ["c2"]
        for tc in payload["tool_calls"]:
            json.loads(tc["function"]["arguments"])

    def test_original_response_is_not_mutated(self):
        """Logs and the session store must still see what the model really sent."""
        response = _truncated(tool_calls=[], invalid_tool_calls=[dict(BROKEN_CALL)])
        handle_truncated_response(response)
        assert len(response.invalid_tool_calls) == 1


class TestNonTruncatedIsUntouched:
    def test_stop_finish_reason_is_ignored(self):
        response = AIMessage(
            content="",
            tool_calls=[dict(PATCH_CALL)],
            response_metadata={"finish_reason": "stop"},
        )
        assert handle_truncated_response(response) is None

    def test_tool_calls_finish_reason_is_ignored(self):
        response = AIMessage(
            content="",
            tool_calls=[dict(PATCH_CALL)],
            response_metadata={"finish_reason": "tool_calls"},
        )
        assert handle_truncated_response(response) is None

    def test_missing_metadata_is_ignored(self):
        assert handle_truncated_response(
            AIMessage(content="", tool_calls=[dict(PATCH_CALL)])
        ) is None

    def test_truncated_text_without_tool_calls_is_ignored(self):
        """A cut-off prose answer needs no intervention."""
        assert handle_truncated_response(_truncated(content="长篇分析被截断")) is None

    def test_broken_call_without_truncation_is_ignored(self):
        """Malformed JSON absent truncation is a separate, pre-existing problem."""
        response = AIMessage(
            content="",
            tool_calls=[],
            invalid_tool_calls=[dict(BROKEN_CALL)],
            response_metadata={"finish_reason": "stop"},
        )
        assert handle_truncated_response(response) is None


class TestScreenersDivertTruncatedBatches:
    """The ToolNode must never see a truncated batch.

    It is the screeners that decide whether pending calls are forwarded, so the
    diversion belongs there — and each has a ``retry`` edge back to its loop.
    """

    ANSWERED = [
        AIMessage(content="", tool_calls=[dict(PATCH_CALL)]),
        ToolMessage(content="Error: ... NOT executed ...", tool_call_id="c1"),
    ]
    # All calls were unparseable and got stripped: the turn ends on an AIMessage
    # with no tool_calls, and there is nothing to answer.
    ALL_STRIPPED = [AIMessage(content="部分文本", tool_calls=[])]

    async def test_tool_screener_retries_on_answered_batch(self):
        from chaos_agent.agent.nodes.planning.tool_screener import tool_screener

        delta = await tool_screener(
            {"truncated_tool_calls": True, "messages": list(self.ANSWERED)}
        )
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        assert delta["truncated_tool_calls"] is False

    async def test_tool_screener_retries_when_everything_was_stripped(self):
        from chaos_agent.agent.nodes.planning.tool_screener import tool_screener

        delta = await tool_screener(
            {"truncated_tool_calls": True, "messages": list(self.ALL_STRIPPED)}
        )
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY

    async def test_phase1_screener_retries_on_answered_batch(self):
        from chaos_agent.agent.nodes.planning.phase1_screener import phase1_screener

        state = {
            "truncated_tool_calls": True,
            "messages": [
                AIMessage(content="", tool_calls=[dict(GET_CALL)]),
                ToolMessage(content="Error: ... NOT executed ...", tool_call_id="c2"),
            ],
        }
        delta = await phase1_screener(state)
        assert delta["screener_route"] == PHASE1_SCREENER_ROUTE_RETRY
        assert delta["truncated_tool_calls"] is False

    async def test_phase1_screener_retries_when_everything_was_stripped(self):
        from chaos_agent.agent.nodes.planning.phase1_screener import phase1_screener

        delta = await phase1_screener(
            {"truncated_tool_calls": True, "messages": list(self.ALL_STRIPPED)}
        )
        assert delta["screener_route"] == PHASE1_SCREENER_ROUTE_RETRY


class TestStaleFlagCannotDivertAHealthyBatch:
    """A leftover flag must not strand a fresh batch's calls unexecuted.

    ``should_continue_execute_loop`` checks ``_should_replan`` BEFORE inspecting
    tool_calls, so a truncated turn can route execute_loop → replan → agent_loop
    without passing the screener that consumes the flag. If the flag then
    diverted agent_loop's own legitimate batch, the loop would spin without those
    calls ever running.

    Two defences: the loops clear the flag at the source on every non-truncated
    turn, and the screeners only honour it when no UNANSWERED batch is pending.
    """

    FRESH = [AIMessage(content="", tool_calls=[dict(GET_CALL)])]

    async def test_tool_screener_ignores_a_stale_flag(self):
        from chaos_agent.agent.nodes.planning.tool_screener import tool_screener

        delta = await tool_screener(
            {"truncated_tool_calls": True, "messages": list(self.FRESH)}
        )
        assert delta["screener_route"] != SCREENER_ROUTE_RETRY

    async def test_phase1_screener_ignores_a_stale_flag(self):
        from chaos_agent.agent.nodes.planning.phase1_screener import phase1_screener

        delta = await phase1_screener(
            {"truncated_tool_calls": True, "messages": list(self.FRESH)}
        )
        assert delta["screener_route"] != PHASE1_SCREENER_ROUTE_RETRY


class TestTruncatedTurnRecordsNoEffects:
    """A truncated turn must not record effects of calls that never executed.

    The concrete harm is ``skill_name``: ``build_*_prompt`` drops the skill
    catalogue when it is set (``if not skill_name``), but the ``activate_skill``
    call never ran so the skill's content was never loaded — leaving the model
    with neither. Recording nothing is strictly better; the model re-issues the
    call on the next turn.
    """

    async def _run_agent_loop(self, monkeypatch, response):
        import chaos_agent.agent.nodes.execute.agent_loop as loop_mod
        from unittest.mock import AsyncMock, MagicMock

        from chaos_agent.agent.nodes.execute.agent_loop import make_agent_loop

        llm = MagicMock()
        llm.bind_tools = MagicMock(return_value=llm)
        llm.ainvoke = AsyncMock(return_value=response)
        monkeypatch.setattr(loop_mod, "compute_env_info", AsyncMock(return_value=""))
        monkeypatch.setattr(loop_mod, "sync_to_store", AsyncMock())

        node = make_agent_loop(llm=llm, tools=[])
        return await node({
            "task_id": "task-truncated-skill",
            "operation": "inject",
            "agent_loop_count": 0,
            "messages": [],
            "fault_spec": {"scope": "pod", "blade_target": "cpu", "blade_action": "fullload"},
        })

    async def test_skill_name_is_not_recorded_on_a_truncated_turn(self, monkeypatch):
        response = AIMessage(
            content="",
            tool_calls=[{
                "name": "activate_skill",
                "args": {"skill_name": "k8s-chaos-skills"},
                "id": "s1",
            }],
            response_metadata={"finish_reason": "length"},
        )
        result = await self._run_agent_loop(monkeypatch, response)
        assert result.get("truncated_tool_calls") is True
        assert "skill_name" not in result

    async def test_skill_name_is_recorded_on_a_normal_turn(self, monkeypatch):
        """Control: the same call without truncation must still be recorded."""
        response = AIMessage(
            content="",
            tool_calls=[{
                "name": "activate_skill",
                "args": {"skill_name": "k8s-chaos-skills"},
                "id": "s1",
            }],
            response_metadata={"finish_reason": "tool_calls"},
        )
        result = await self._run_agent_loop(monkeypatch, response)
        assert result.get("truncated_tool_calls") is False
        assert result.get("skill_name") == "k8s-chaos-skills"
