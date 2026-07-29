"""Token accounting must equal the reasoning text actually put on the wire.

Two subsystems decide this independently, and a divergence is silent:

  * ``agent.factory``'s outbound patch chooses whether to replay a thinking
    trace and how much of it;
  * ``memory.tokens.count_tokens_messages`` feeds EVERY context decision —
    auto-compact WARNING/ERROR/BLOCKING, the strip-vs-compress choice in
    ``PreReasoningHook``, per-message keep/drop.

Under-counting was the shipped state: a thinking model returns empty ``content``
with the whole rationale in ``reasoning_content``, so a 10-turn history measured
83 tokens against a 7,547-token payload — a 91× gap. Compaction never fired and
the first symptom was a context-length error from the API. Worse, the strip path
truncates ``content`` only, so even when it did run it could not reduce this
payload; only the compress path drops it, and that choice is made on this count.

Over-counting is the mirror failure: charging for thinking the provenance guard
will NOT replay (a checkpointer-restored session from a different model) forces
compaction that frees nothing.

So the assertion here is an equality against the real wire payload, produced by
calling langchain's own ``_convert_message_to_dict`` — not a re-implementation of
the rules, which would drift alongside the code it is meant to police. The only
permitted difference is the per-message envelope overhead the counter adds by
design (~4 tokens/message + 2 batch priming).
"""

from __future__ import annotations

import langchain_openai.chat_models.base as lc_base
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import chaos_agent.agent.factory  # noqa: F401  — import applies the patches
from chaos_agent.config.settings import settings
from chaos_agent.memory.tokens import count_tokens, count_tokens_messages
from chaos_agent.utils.reasoning_replay import reasoning_model_key, replayed_reasoning

# Envelope allowance: 4 tokens per message carrying text + 2 batch priming.
_ENVELOPE_PER_MSG = 4
_BATCH_PRIMING = 2


def _key() -> str:
    return reasoning_model_key(settings.model_name)


def _wire_text_tokens(messages: list) -> int:
    """Tokens of the text langchain will actually serialise for these messages.

    Deliberately routed through the real ``_convert_message_to_dict`` so the
    expectation tracks the patch rather than a copy of its logic.
    """
    total = 0
    for msg in messages:
        payload = lc_base._convert_message_to_dict(msg)
        for field in ("content", "reasoning_content"):
            value = payload.get(field)
            if isinstance(value, str) and value:
                total += count_tokens(value).count
    return total


def _assert_matches_wire(messages: list) -> None:
    counted = count_tokens_messages(messages).count
    wire = _wire_text_tokens(messages)
    allowance = _ENVELOPE_PER_MSG * len(messages) + _BATCH_PRIMING
    assert wire <= counted <= wire + allowance, (
        f"counted {counted} vs wire {wire} (allowance {allowance}) — the token "
        f"counter and the outbound patch disagree about what is sent"
    )


def _thinking_ai(trace: str, *, provenance: bool = True, content: str = "") -> AIMessage:
    akw: dict = {"reasoning_content": trace}
    if provenance:
        akw[_key()] = True
    return AIMessage(content=content, additional_kwargs=akw)


class TestReplayedTextIsCounted:
    """The gap that shipped: thinking replayed but not measured."""

    def test_thinking_only_message_is_not_measured_as_empty(self):
        """``content=""`` + a long trace must not read as a near-zero message."""
        msg = _thinking_ai("已注入CPU故障并验证通过，现进入恢复阶段。" * 30)
        counted = count_tokens_messages([msg]).count
        assert counted > 100, (
            "a thinking-only message measured as if it were empty — this is the "
            "91x under-report that let compaction never fire"
        )
        _assert_matches_wire([msg])

    def test_multi_turn_history_matches_the_wire(self):
        messages: list = [HumanMessage(content="演练节点 CPU 故障")]
        trace = "已注入CPU故障并验证通过，现进入恢复阶段。" * 30
        for i in range(10):
            messages.append(
                AIMessage(
                    content="",
                    additional_kwargs={"reasoning_content": trace, _key(): True},
                    tool_calls=[
                        {"name": "kubectl", "args": {"subcommand": "get"}, "id": f"c{i}"}
                    ],
                )
            )
            messages.append(
                ToolMessage(content="NAME READY STATUS", tool_call_id=f"c{i}", name="kubectl")
            )
        _assert_matches_wire(messages)

    def test_content_and_thinking_are_both_counted(self):
        msg = _thinking_ai("思考正文" * 50, content="对外回答" * 50)
        _assert_matches_wire([msg])


class TestNotReplayedIsNotCounted:
    """The mirror failure: charging for text the guard will drop.

    Compaction triggered by phantom tokens frees nothing, so it would loop.
    """

    @pytest.mark.parametrize("msg_factory,label", [
        (lambda t: _thinking_ai(t, provenance=False), "no provenance key (legacy msg)"),
        (lambda t: AIMessage(
            content="", additional_kwargs={"reasoning_content": t, "_reasoning_model:other-llm": True},
        ), "produced by a different model"),
        (lambda t: _thinking_ai("   "), "blank trace"),
        (lambda t: HumanMessage(content="", additional_kwargs={"reasoning_content": t, _key(): True}),
         "non-assistant role"),
        (lambda t: ToolMessage(
            content="", tool_call_id="x",
            additional_kwargs={"reasoning_content": t, _key(): True},
        ), "tool role"),
        (lambda t: SystemMessage(content="", additional_kwargs={"reasoning_content": t, _key(): True}),
         "system role"),
    ])
    def test_unreplayed_thinking_adds_no_tokens(self, msg_factory, label):
        trace = "不会被回放的思考" * 100
        msg = msg_factory(trace)
        assert replayed_reasoning(msg) == "", label
        # Only the batch priming remains; no content, no thinking.
        assert count_tokens_messages([msg]).count <= _BATCH_PRIMING, label
        _assert_matches_wire([msg])


class TestTruncationAgreesOnBothSides:
    """The tail-truncation limit must be applied identically, not twice."""

    def test_oversized_trace_counted_at_the_capped_length(self):
        limit = settings.reasoning_replay_max_chars
        msg = _thinking_ai("A" * (limit * 3) + "CONCLUSION")
        _assert_matches_wire([msg])
        assert len(replayed_reasoning(msg)) == limit

    def test_capped_trace_keeps_the_conclusion(self):
        """Truncating the HEAD would drop what the replay exists to carry."""
        limit = settings.reasoning_replay_max_chars
        msg = _thinking_ai("A" * limit + "CONCLUSION_AT_END")
        assert replayed_reasoning(msg).endswith("CONCLUSION_AT_END")


class TestSharedDecisionIsTheOnlySource:
    """Both sides must call the shared helper, not their own copy.

    A second implementation is exactly how the two drift apart again, and the
    drift is invisible until the API rejects an over-long request.
    """

    def test_outbound_patch_and_counter_agree_under_a_changed_limit(self, monkeypatch):
        """Moving the limit must move both sides together."""
        monkeypatch.setattr(settings, "reasoning_replay_max_chars", 50)
        msg = _thinking_ai("B" * 500)
        assert len(replayed_reasoning(msg)) == 50
        _assert_matches_wire([msg])

    def test_limit_of_zero_disables_truncation_on_both_sides(self, monkeypatch):
        monkeypatch.setattr(settings, "reasoning_replay_max_chars", 0)
        trace = "C" * 5000
        msg = _thinking_ai(trace)
        assert replayed_reasoning(msg) == trace
        _assert_matches_wire([msg])

    def test_model_switch_stops_counting_and_sending_together(self, monkeypatch):
        """Provenance is resolved against the CURRENT model on both sides."""
        msg = _thinking_ai("意图" * 100)
        _assert_matches_wire([msg])          # matching model: counted and sent
        monkeypatch.setattr(settings, "model_name", "some-other-model")
        assert replayed_reasoning(msg) == ""
        assert count_tokens_messages([msg]).count <= _BATCH_PRIMING
        _assert_matches_wire([msg])          # after the switch: neither
