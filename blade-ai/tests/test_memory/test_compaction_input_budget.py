"""The compaction call must size itself by what it actually sends.

``compact_memory`` hands the ORIGINAL message objects to ``llm.ainvoke``, so the
outbound patch serialises each one's ``reasoning_content`` alongside its
``content``. Budgeting on ``content`` alone therefore mis-measures the request
the same way the token counter used to: a 100-turn thinking history reports 200
characters while the call it produces is ~68k tokens.

That mis-measurement is worst exactly when it matters. Compaction runs because
the context is nearly full; if the compaction call itself overflows the window it
fails, and the failure path is ``_simple_compact`` — which, reading ``content``
only, used to emit a summary containing just its own header. The history is
dropped either way (``RemoveMessage``), so an empty summary means the model loses
every trace of what it already did: the non-convergence the reasoning replay was
added to prevent.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import chaos_agent.agent.factory  # noqa: F401  — import applies the replay patch
from chaos_agent.config.settings import settings
from chaos_agent.memory.compactor import (
    MAX_COMPACTION_INPUT_CHARS,
    _compaction_input_chars,
    _prepare_compaction_messages,
    _simple_compact,
)
from chaos_agent.utils.reasoning_replay import reasoning_model_key


def _key() -> str:
    return reasoning_model_key(settings.model_name)


def _thinking_turn(index: int, trace: str) -> list:
    """One turn in the shape a thinking model produces: empty content."""
    return [
        AIMessage(
            content="",
            additional_kwargs={"reasoning_content": trace, _key(): True},
            tool_calls=[{"name": "kubectl", "args": {"subcommand": "get"}, "id": f"c{index}"}],
        ),
        ToolMessage(content="ok", tool_call_id=f"c{index}", name="kubectl"),
    ]


def _history(turns: int, repeat: int = 40) -> list:
    trace = "已确认节点状态；上一轮注入CPU故障并验证通过；本轮应进入恢复阶段。" * repeat
    messages: list = []
    for i in range(turns):
        messages.extend(_thinking_turn(i, trace))
    return messages


def _content_only_chars(messages: list) -> int:
    """The old budget: ``content`` lengths, ignoring the replayed trace."""
    return sum(
        len(c) for m in messages
        if isinstance((c := getattr(m, "content", "")), str)
    )


class TestBudgetCountsWhatIsSent:
    def test_thinking_dominates_the_budget(self):
        """A turn whose content is empty still costs its whole trace."""
        messages = _thinking_turn(0, "思考" * 500)
        ai = messages[0]
        assert getattr(ai, "content", "") == ""
        assert _compaction_input_chars(ai) == 1000

    def test_unreplayed_thinking_is_not_charged(self):
        """Provenance mismatch means the trace is not sent, so not counted."""
        ai = AIMessage(content="", additional_kwargs={"reasoning_content": "思考" * 500})
        assert _compaction_input_chars(ai) == 0

    def test_content_and_thinking_both_counted(self):
        ai = AIMessage(
            content="A" * 100,
            additional_kwargs={"reasoning_content": "B" * 200, _key(): True},
        )
        assert _compaction_input_chars(ai) == 300

    @pytest.mark.parametrize("turns", [100, 200])
    def test_oversized_history_is_truncated(self, turns):
        """The case the old budget missed entirely.

        ``content`` sums to a few hundred characters, so the old check returned
        the list untouched and let the compaction request grow unbounded.
        """
        messages = _history(turns)
        assert _content_only_chars(messages) < MAX_COMPACTION_INPUT_CHARS, (
            "fixture no longer reproduces the blind spot"
        )
        assert sum(_compaction_input_chars(m) for m in messages) > MAX_COMPACTION_INPUT_CHARS

        kept = _prepare_compaction_messages(messages)
        assert len(kept) < len(messages), "oversized thinking history was not truncated"
        assert sum(_compaction_input_chars(m) for m in kept) <= MAX_COMPACTION_INPUT_CHARS

    def test_small_history_is_untouched(self):
        """Truncation must not kick in for a history that genuinely fits."""
        messages = _history(3)
        assert _prepare_compaction_messages(messages) is messages

    def test_truncation_keeps_the_newest_turns(self):
        """Dropping the tail would discard the state closest to now."""
        messages = _history(200)
        kept = _prepare_compaction_messages(messages)
        assert kept[-1] is messages[-1]


class TestFallbackSummaryIsNotEmpty:
    """``_simple_compact`` is the recovery path — it must carry something."""

    def test_thinking_only_history_produces_a_real_summary(self):
        messages = _history(3)
        summary = _simple_compact(messages)
        lines = [ln for ln in summary.splitlines() if ln.startswith("- ")]
        assert lines, (
            "fallback summary held nothing but its header — the history is "
            "removed regardless, so the model would lose all prior context"
        )
        assert any("[thinking]" in ln for ln in lines)

    def test_content_is_preferred_over_thinking(self):
        """When the model DID answer in content, that is the better summary."""
        msg = AIMessage(
            content="已完成注入",
            additional_kwargs={"reasoning_content": "内部推理不该覆盖结论", _key(): True},
        )
        summary = _simple_compact([msg])
        assert "已完成注入" in summary
        assert "[thinking]" not in summary

    def test_unreplayed_thinking_is_not_summarised(self):
        """Text the model will never see again should not enter the summary."""
        msg = AIMessage(content="", additional_kwargs={"reasoning_content": "跨模型思考"})
        summary = _simple_compact([msg])
        assert "跨模型思考" not in summary

    def test_thinking_tail_is_kept(self):
        """A trace states its conclusion at the END."""
        msg = AIMessage(
            content="",
            additional_kwargs={
                "reasoning_content": "前置铺垫" * 200 + "结论：下一步执行恢复",
                _key(): True,
            },
        )
        assert "结论：下一步执行恢复" in _simple_compact([msg])

    def test_previous_summary_still_carried(self):
        summary = _simple_compact(_history(2), previous_summary="早期上下文摘要")
        assert "早期上下文摘要" in summary

    def test_plain_history_unaffected(self):
        """Non-thinking messages summarise exactly as before."""
        messages = [HumanMessage(content="演练请求"), AIMessage(content="执行完毕")]
        summary = _simple_compact(messages)
        assert "演练请求" in summary and "执行完毕" in summary
