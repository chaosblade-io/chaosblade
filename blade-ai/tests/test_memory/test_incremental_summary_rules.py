"""The incremental-summary rules are load-bearing, not prompt polish.

Older ``[Compressed History]`` summaries are now folded into each compaction
instead of being retained verbatim (``SUMMARIES_KEPT_VERBATIM``). That is what
stops context from growing without bound, but it moves the burden onto the
summarising model: whatever it drops when rewriting the previous summary is gone
for good. A bare "Previous summary to build upon:" header does not ask it to
carry facts forward — these rules do.

``blade_uid`` gets a named assertion because it is the one value nothing can
reconstruct. Lose it and an injected experiment can no longer be destroyed, so a
summarisation slip becomes a fault left running on a cluster.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.memory.compactor import (
    INCREMENTAL_SUMMARY_RULES,
    CompactionMode,
    compact_memory,
)


def _llm(summary: str = "<summary>新摘要</summary>") -> MagicMock:
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=AIMessage(content=summary))
    return llm


def _sent_prompt(llm: MagicMock) -> str:
    """The system prompt actually handed to the model."""
    messages = llm.ainvoke.call_args[0][0]
    return messages[0].content


_HISTORY = [HumanMessage(content="演练 CPU 故障"), AIMessage(content="已注入")]
_PREVIOUS = "1. Goal: 注入 CPU 故障\n5. Key Results: blade_uid: abc-123-def"


class TestRulesAreAttachedWithAPreviousSummary:
    @pytest.mark.asyncio
    async def test_rules_and_summary_both_reach_the_model(self):
        llm = _llm()
        await compact_memory(_HISTORY, previous_summary=_PREVIOUS, llm=llm)
        prompt = _sent_prompt(llm)
        assert INCREMENTAL_SUMMARY_RULES in prompt
        assert _PREVIOUS in prompt

    @pytest.mark.asyncio
    async def test_no_rules_without_a_previous_summary(self):
        """A first compaction has nothing to carry forward — don't confuse it."""
        llm = _llm()
        await compact_memory(_HISTORY, previous_summary="", llm=llm)
        assert INCREMENTAL_SUMMARY_RULES not in _sent_prompt(llm)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", list(CompactionMode))
    async def test_rules_apply_in_every_mode(self, mode):
        """The carry-forward burden exists regardless of which prompt is used."""
        llm = _llm()
        await compact_memory(_HISTORY, previous_summary=_PREVIOUS, llm=llm, mode=mode)
        assert INCREMENTAL_SUMMARY_RULES in _sent_prompt(llm)


class TestRulesStateWhatMustSurvive:
    """Each assertion pins one instruction that a rewrite could silently drop."""

    def test_declares_that_the_output_replaces_the_previous_summary(self):
        """Without this the model may assume the old text stays available."""
        assert "REPLACES" in INCREMENTAL_SUMMARY_RULES

    def test_requires_preserving_still_true_facts(self):
        assert "PRESERVE every fact" in INCREMENTAL_SUMMARY_RULES

    def test_names_blade_uid_explicitly(self):
        """The only identifier whose loss makes a fault unrecoverable."""
        assert "blade_uid" in INCREMENTAL_SUMMARY_RULES
        assert "unrecoverable" in INCREMENTAL_SUMMARY_RULES

    def test_names_the_other_literals_that_cannot_be_reconstructed(self):
        for token in ("namespace", "labels", "file paths", "error text"):
            assert token in INCREMENTAL_SUMMARY_RULES, token

    def test_asks_for_a_merge_not_an_append(self):
        """Appending would grow every summary and defeat the point."""
        assert "MERGE rather than append" in INCREMENTAL_SUMMARY_RULES

    def test_biases_toward_keeping_when_unsure(self):
        assert "When unsure, keep it" in INCREMENTAL_SUMMARY_RULES

    def test_requires_progress_and_next_steps_to_be_updated(self):
        assert "UPDATE Progress" in INCREMENTAL_SUMMARY_RULES
        assert "Next Steps" in INCREMENTAL_SUMMARY_RULES


class TestPreviousSummaryIsNotSilentlyTruncated:
    """Carrying it forward is pointless if the prompt drops half of it."""

    @pytest.mark.asyncio
    async def test_a_long_previous_summary_is_passed_in_full(self):
        llm = _llm()
        long_previous = "关键事实 " * 2000
        await compact_memory(_HISTORY, previous_summary=long_previous, llm=llm)
        assert long_previous in _sent_prompt(llm)


class TestFallbackSummaryIsBudgetedNotCounted:
    """The LLM-free path must obey the same "keep the facts" intent.

    Its output IS the summary that lands in state while the originals are deleted
    by ``RemoveMessage``, so whatever it omits leaves the live context. It used to
    take ``messages[-10:]`` and ``previous_summary[:500]``: on an 81-message input
    that summarised 10 and dropped 71, and cut a 1,625-character carried-forward
    summary by 69% — while the LLM path next to it is told to "preserve every
    fact" and keep ``blade_uid`` exactly.
    """

    @staticmethod
    def _history(count: int = 40) -> list:
        msgs: list = [HumanMessage(content="对 pod 注入 80% 丢包", id="h0")]
        for i in range(count):
            msgs.append(AIMessage(content=f"第{i}轮 blade_uid=uid-{i}", id=f"a{i}"))
            msgs.append(ToolMessage(content=f"结果{i}", tool_call_id=f"c{i}", id=f"t{i}"))
        return msgs

    def test_more_than_ten_messages_survive_when_they_are_small(self):
        from chaos_agent.memory.compactor import _simple_compact

        summary = _simple_compact(self._history())
        picked = [ln for ln in summary.splitlines() if ln.startswith("- ")]
        assert len(picked) > 10, (
            f"only {len(picked)} entries kept — a budget should hold far more "
            f"small messages than the old fixed count of 10"
        )

    def test_the_users_request_survives(self):
        """Index 0 is the task definition and the first thing a tail drops."""
        from chaos_agent.memory.compactor import _simple_compact

        assert "对 pod 注入 80% 丢包" in _simple_compact(self._history())

    def test_the_earliest_blade_uid_survives(self):
        """Losing it makes the experiment unrecoverable."""
        from chaos_agent.memory.compactor import _simple_compact

        assert "uid-0" in _simple_compact(self._history())

    def test_carried_forward_summary_is_not_cut_to_a_fixed_length(self):
        from chaos_agent.memory.compactor import _simple_compact

        previous = "累积摘要：" + "已完成注入并验证，blade_uid=abc-123。" * 60
        assert len(previous) > 500, "fixture must exceed the old 500-char cut"
        assert previous in _simple_compact(self._history(), previous_summary=previous)

    def test_carried_forward_summary_gets_first_claim_on_the_budget(self):
        """It is the only record of every earlier compaction round."""
        from chaos_agent.memory.compactor import (
            FALLBACK_PREVIOUS_SUMMARY_SHARE,
            FALLBACK_SUMMARY_BUDGET_CHARS,
            _simple_compact,
        )

        allowance = int(FALLBACK_SUMMARY_BUDGET_CHARS * FALLBACK_PREVIOUS_SUMMARY_SHARE)
        previous = "早期历史 " * (allowance)          # far beyond its allowance
        summary = _simple_compact(self._history(), previous_summary=previous)
        carried = [ln for ln in summary.splitlines() if ln.startswith("Previous context")]
        assert carried, "the carried-forward summary was dropped entirely"
        assert len(carried[0]) >= allowance * 0.9

    def test_an_oversized_carried_summary_keeps_its_tail(self):
        """A cumulative summary ends with the current state / next steps."""
        from chaos_agent.memory.compactor import (
            FALLBACK_SUMMARY_BUDGET_CHARS,
            _simple_compact,
        )

        previous = "早期" * FALLBACK_SUMMARY_BUDGET_CHARS + "最新状态：待恢复"
        assert "最新状态：待恢复" in _simple_compact([], previous_summary=previous)

    def test_output_stays_within_the_budget(self):
        from chaos_agent.memory.compactor import (
            FALLBACK_SUMMARY_BUDGET_CHARS,
            _simple_compact,
        )

        huge = [AIMessage(content="很长的内容" * 100, id=f"a{i}") for i in range(500)]
        summary = _simple_compact(huge, previous_summary="早期摘要" * 500)
        # Entries are capped at 200 chars each, so one final entry may overshoot.
        assert len(summary) <= FALLBACK_SUMMARY_BUDGET_CHARS + 500
