"""Tests for the provider-compatibility layer (issue #1344)."""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.llm_compat import (
    LLMProviderIncompatibilityError,
    ProviderCompatMixin,
    sanitize_tool_sequence,
)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _FakeResponse:
    status_code = 400
    headers: dict = {}
    request = None


def make_bad_request(text):
    import openai

    return openai.BadRequestError(text, response=_FakeResponse(), body=None)


def _generation(text="done"):
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


class FakeBase:
    """Stands in for ChatOpenAI: scripted outcomes, records what it saw."""

    scripted: ClassVar[list] = []
    seen_messages: ClassVar[list | None] = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_messages = list(messages)
        outcome = self.scripted.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_messages = list(messages)
        outcome = self.scripted.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        for chunk in outcome or []:
            yield chunk


class CompatChat(ProviderCompatMixin, FakeBase):
    extra_body: ClassVar[dict | None] = None


def make_model():
    model = CompatChat()
    model.extra_body = {"enable_thinking": True}
    return model


# ---------------------------------------------------------------------------
# sanitize_tool_sequence
# ---------------------------------------------------------------------------


def test_sanitize_keeps_matched_tool_messages():
    ai = AIMessage(content="", tool_calls=[{"name": "f", "args": {}, "id": "c1"}])
    tool = ToolMessage(content="ok", tool_call_id="c1")
    out = sanitize_tool_sequence([HumanMessage(content="go"), ai, tool])
    assert out == [HumanMessage(content="go"), ai, tool]


def test_sanitize_drops_orphan_tool_messages():
    orphan = ToolMessage(content="stray", tool_call_id="nope")
    out = sanitize_tool_sequence([HumanMessage(content="go"), orphan])
    assert out == [HumanMessage(content="go")]


def test_sanitize_drops_second_answer_for_same_call():
    ai = AIMessage(content="", tool_calls=[{"name": "f", "args": {}, "id": "c1"}])
    first = ToolMessage(content="first", tool_call_id="c1")
    dup = ToolMessage(content="second", tool_call_id="c1")
    out = sanitize_tool_sequence([ai, first, dup])
    assert out == [ai, first]


def test_sanitize_handles_multi_tool_calls():
    ai = AIMessage(
        content="",
        tool_calls=[
            {"name": "f", "args": {}, "id": "c1"},
            {"name": "g", "args": {}, "id": "c2"},
        ],
    )
    out = sanitize_tool_sequence([ai, ToolMessage(content="1", tool_call_id="c1"), ToolMessage(content="2", tool_call_id="c2")])
    assert len(out) == 3


def test_sanitize_survives_replan_hints_between_turns():
    """Hints inserted between an assistant tool_calls message and its answers
    must not break the pairing check."""
    ai = AIMessage(content="", tool_calls=[{"name": "f", "args": {}, "id": "c1"}])
    hint = HumanMessage(content="iteration warning")
    tool = ToolMessage(content="ok", tool_call_id="c1")
    out = sanitize_tool_sequence([ai, hint, tool])
    assert ai in out and hint in out and tool in out


# ---------------------------------------------------------------------------
# outgoing reasoning_content echo (DeepSeek thinking continuation)
# ---------------------------------------------------------------------------


def test_reasoning_content_echoed_outgoing():
    from langchain_openai.chat_models import base as lc_base

    from chaos_agent.agent.llm_compat import patch_openai_message_conversion

    patch_openai_message_conversion()
    msg = AIMessage(
        content="the answer",
        additional_kwargs={"reasoning_content": "step by step"},
    )
    d = lc_base._convert_message_to_dict(msg)
    assert d["role"] == "assistant"
    assert d["reasoning_content"] == "step by step"


def test_reasoning_content_absent_left_alone():
    from langchain_openai.chat_models import base as lc_base

    from chaos_agent.agent.llm_compat import patch_openai_message_conversion

    patch_openai_message_conversion()
    d = lc_base._convert_message_to_dict(AIMessage(content="plain"))
    assert "reasoning_content" not in d


# ---------------------------------------------------------------------------
# enable_thinking fallback (provider rejects the parameter)
# ---------------------------------------------------------------------------


def test_enable_thinking_rejected_retries_without_it():
    model = make_model()
    model.scripted = [
        make_bad_request(
            "400 - {'error': {'message': 'Unsupported parameter: "
            "enable_thinking', 'type': 'invalid_request_error'}}"
        ),
        _generation("ok"),
    ]
    res = model._generate([HumanMessage(content="hi")], None, None)
    assert res.generations[0].message.content == "ok"
    assert model.extra_body in (None, {})


def test_enable_thinking_rejection_remembered_for_later_calls():
    model = make_model()
    model.scripted = [
        make_bad_request("400 - Unsupported parameter: enable_thinking"),
        _generation("first"),
        _generation("second"),
    ]
    model._generate([HumanMessage(content="1")], None, None)
    model._generate([HumanMessage(content="2")], None, None)
    assert model.extra_body in (None, {})


def test_unrelated_bad_request_not_swallowed():
    model = make_model()
    err = make_bad_request("400 - invalid api key")
    model.scripted = [err]
    with pytest.raises(openai_error_type()) as ei:
        model._generate([HumanMessage(content="hi")], None, None)
    assert ei.value is err


def openai_error_type():
    import openai

    return openai.BadRequestError


def test_responses_routing_error_explained():
    model = make_model()
    model.scripted = [
        make_bad_request(
            "function_call_output requires item_reference ids matching each "
            "call_id on HTTP requests"
        )
    ]
    with pytest.raises(LLMProviderIncompatibilityError) as ei:
        model._generate([HumanMessage(content="hi")], None, None)
    assert "Responses API" in str(ei.value)
    assert ei.value.__cause__ is not None


def test_sanitize_runs_before_generate():
    """The orphan ToolMessage that makes providers reject the whole turn is
    dropped before the request goes out."""
    model = make_model()
    orphan = ToolMessage(content="stray", tool_call_id="nope")
    model.scripted = [_generation("ok")]
    model._generate([HumanMessage(content="hi"), orphan], None, None)
    assert model.seen_messages == [HumanMessage(content="hi")]


def test_astream_fallback():
    model = make_model()
    model.scripted = [
        make_bad_request("400 - Unsupported parameter: enable_thinking"),
        None,
    ]

    async def run():
        chunks = []
        async for c in model._astream([HumanMessage(content="hi")], None, None):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(run())
    assert model.extra_body in (None, {})
    assert chunks == []


import openai  # noqa: E402  (used by openai_error_type)
