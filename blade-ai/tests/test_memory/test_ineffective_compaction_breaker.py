"""The circuit breaker must count "compacted but freed nothing" as a failure.

The breaker exists to stop futile retries. It only ever counted exceptions, so
the one failure mode that actually burns money went unnoticed: a compaction that
returns a valid summary while freeing almost nothing leaves the context just as
full, the next turn tries again, and each attempt spends an LLM call and appends
another summary. The success branch reset ``consecutive_failures`` to 0 every
time, so the breaker could never engage — the session simply ran until the
provider rejected the request.

That is not hypothetical. With every ``[Compressed History]`` retained, a
30-summary history sat 40K past the window while ``to_compact`` held 42 tokens:
each pass freed 42 and appended ~4,600.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.memory.context_manager import (
    INEFFECTIVE_COMPACTION_RATIO,
    MAX_CONSECUTIVE_COMPACT_FAILURES,
    CompactTrackingState,
    ContextManager,
)
from chaos_agent.memory.hook import PreReasoningHook


def _messages(count: int = 12, size: int = 400) -> list:
    out: list = [HumanMessage(content="演练请求", id="h0")]
    for i in range(count):
        out.append(AIMessage(content="执行注入" * size, id=f"a{i}"))
        out.append(
            ToolMessage(content="状态输出" * size, tool_call_id=f"c{i}", id=f"t{i}")
        )
    return out


def _hook(summary: str = "[摘要]") -> PreReasoningHook:
    """A hook whose compaction always 'succeeds', so only the token delta varies."""
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=AIMessage(content=summary))
    tool_compactor = MagicMock()
    # Must return the list it was given. The real ``compact(messages, task_id="")
    # -> list`` hands the message list onward, and the hook measures the context
    # from that return value — a bare MagicMock would be measured as an empty
    # context (2 tokens of batch priming), making every compaction look like a
    # catastrophic regression.
    tool_compactor.compact = MagicMock(side_effect=lambda msgs, **kw: msgs)
    hook = PreReasoningHook(
        context_manager=ContextManager(max_tokens=60_000, compact_ratio=0.8),
        tool_compactor=tool_compactor,
        session_store=MagicMock(),
        llm=llm,
    )
    # ``compact_threshold = 0`` forces the LLM path: otherwise ``strip`` alone
    # gets the total under budget and the hook short-circuits before compaction,
    # leaving the bookkeeping under test untouched. Same technique as test_hook.
    hook.context_manager.compact_threshold = 0
    hook._persist_to_session = MagicMock()
    hook._emit_compaction_event = MagicMock()
    hook._emit_context_size_snapshot = MagicMock()
    hook._async_session_append = AsyncMock()
    return hook


def _tracking_of(hook: PreReasoningHook, task_id: str) -> CompactTrackingState:
    """The hook owns tracking per task_id (``self._tracking``) — it is NOT read
    from the state dict. Reading it any other way silently observes a different
    object, which is how the first version of these tests asserted against an
    untouched default while the code under test was working correctly."""
    return hook._get_tracking(task_id)


def _emitted(hook: PreReasoningHook, status: str) -> list:
    return [c for c in hook._emit_compaction_event.call_args_list if c.args[1] == status]


class TestIneffectiveCompactionCountsAsFailure:
    @pytest.mark.asyncio
    async def test_no_progress_increments_the_failure_counter(self, monkeypatch):
        """The exact loop the breaker was blind to."""
        hook = _hook()
        msgs = _messages()
        # Force the "freed nothing" shape: to_keep is the whole history.
        monkeypatch.setattr(
            hook.context_manager, "check_context", lambda *a, **kw: (list(msgs), list(msgs), True)
        )
        await hook({"messages": msgs, "task_id": "t-ineffective"})

        assert _tracking_of(hook, "t-ineffective").consecutive_failures == 1
        assert _emitted(hook, "failed"), "an ineffective compaction must not report success"

    @pytest.mark.asyncio
    async def test_repeated_no_progress_trips_the_breaker(self, monkeypatch):
        hook = _hook()
        msgs = _messages()
        monkeypatch.setattr(
            hook.context_manager, "check_context", lambda *a, **kw: (list(msgs), list(msgs), True)
        )
        for _ in range(MAX_CONSECUTIVE_COMPACT_FAILURES):
            await hook({"messages": msgs, "task_id": "t-loop"})

        tracking = _tracking_of(hook, "t-loop")
        assert tracking.consecutive_failures >= MAX_CONSECUTIVE_COMPACT_FAILURES, (
            "the breaker must reach its limit instead of resetting every turn"
        )

    @pytest.mark.asyncio
    async def test_effective_compaction_clears_the_counter(self, monkeypatch):
        """A real reduction must still forgive earlier transient failures."""
        hook = _hook()
        msgs = _messages()
        _tracking_of(hook, "t-ok").consecutive_failures = 2
        # Keep almost nothing → a large reduction.
        monkeypatch.setattr(
            hook.context_manager, "check_context", lambda *a, **kw: (list(msgs), msgs[:1], True)
        )
        await hook({"messages": msgs, "task_id": "t-ok"})

        assert _tracking_of(hook, "t-ok").consecutive_failures == 0
        assert _emitted(hook, "completed")

    @pytest.mark.asyncio
    async def test_marked_as_compacted_either_way(self, monkeypatch):
        """``compacted``/``turn_count`` track that an attempt ran, not that it helped."""
        hook = _hook()
        msgs = _messages()
        monkeypatch.setattr(
            hook.context_manager, "check_context", lambda *a, **kw: (list(msgs), list(msgs), True)
        )
        await hook({"messages": msgs, "task_id": "t-mark"})

        tracking = _tracking_of(hook, "t-mark")
        assert tracking.compacted is True
        assert tracking.turn_count == 1


class TestTheSummaryItselfIsWeighed:
    """The measurement must be the context that will exist, summary included.

    Measuring only ``to_keep`` hides the summary's own cost, and the summary is
    not small. Against a 10.2K history a 4.5K summary leaves a real
    post-compaction context of 12.6K — LARGER than what it replaced — while the
    kept-only view reads 5.1K and calls it a success. That runaway is precisely
    what this breaker exists to catch, so it cannot be the case it misses.
    """

    @pytest.mark.asyncio
    async def test_a_summary_larger_than_the_savings_is_not_a_success(self, monkeypatch):
        hook = _hook(summary="巨大摘要" * 4000)
        msgs = _messages(count=6, size=100)
        # Keep a small slice: by the kept-only measure this looks like a big win.
        keep = msgs[:2]
        monkeypatch.setattr(
            hook.context_manager, "check_context", lambda *a, **kw: (list(msgs), keep, True)
        )
        await hook({"messages": msgs, "task_id": "t-fat-summary"})

        assert _tracking_of(hook, "t-fat-summary").consecutive_failures == 1, (
            "a summary bigger than the history it replaced was counted as progress"
        )

    @pytest.mark.asyncio
    async def test_a_compact_summary_still_counts_as_progress(self, monkeypatch):
        """The inverse: including the summary must not flag genuine wins."""
        hook = _hook(summary="简短摘要")
        msgs = _messages(count=20)
        keep = msgs[:2]
        _tracking_of(hook, "t-lean-summary").consecutive_failures = 1
        monkeypatch.setattr(
            hook.context_manager, "check_context", lambda *a, **kw: (list(msgs), keep, True)
        )
        await hook({"messages": msgs, "task_id": "t-lean-summary"})

        assert _tracking_of(hook, "t-lean-summary").consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_reported_size_matches_the_breaker_measurement(self, monkeypatch):
        """UI and breaker must not disagree about whether compaction helped."""
        hook = _hook(summary="摘要正文" * 200)
        msgs = _messages(count=10)
        keep = msgs[:4]
        monkeypatch.setattr(
            hook.context_manager, "check_context", lambda *a, **kw: (list(msgs), keep, True)
        )
        await hook({"messages": msgs, "task_id": "t-report"})

        emitted = hook._emit_context_size_snapshot.call_args_list
        assert emitted, "no context size was reported"
        reported = emitted[-1].args[1]
        detail = hook._emit_compaction_event.call_args_list[-1].kwargs["detail"]
        assert reported == detail["tokens_after"], (
            f"UI reported {reported} while the breaker weighed "
            f"{detail['tokens_after']}"
        )


class TestManualCompactDoesNotFeedTheBreaker:
    """A user pressing /compact must not be able to disable automatic compaction.

    ``check_context`` already exempts ``force`` from the breaker — "the breaker
    exists to protect the auto-trigger loop... a user pressing /compact wants a
    retry". Letting manual runs INCREMENT the counter is worse than ignoring it:
    three deliberate presses on a context dominated by incompressible content
    would trip the breaker and silently switch off automatic compaction for the
    rest of the session, with the user having no idea they caused it.
    """

    @staticmethod
    def _ineffective_hook(monkeypatch, msgs):
        hook = _hook(summary="巨大摘要" * 4000)
        monkeypatch.setattr(
            hook.context_manager, "check_context", lambda *a, **kw: (list(msgs), list(msgs), True)
        )
        return hook

    @pytest.mark.asyncio
    async def test_manual_runs_never_increment_the_counter(self, monkeypatch):
        msgs = _messages()
        hook = self._ineffective_hook(monkeypatch, msgs)
        for _ in range(MAX_CONSECUTIVE_COMPACT_FAILURES + 1):
            await hook({"messages": msgs, "task_id": "t-manual"}, force=True)

        assert _tracking_of(hook, "t-manual").consecutive_failures == 0, (
            "manual /compact presses tripped the breaker that guards the "
            "AUTOMATIC path"
        )

    @pytest.mark.asyncio
    async def test_automatic_runs_still_increment(self, monkeypatch):
        """The exemption must be scoped to force, not remove the check."""
        msgs = _messages()
        hook = self._ineffective_hook(monkeypatch, msgs)
        await hook({"messages": msgs, "task_id": "t-auto"}, force=False)

        assert _tracking_of(hook, "t-auto").consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_manual_run_does_not_clear_an_existing_count(self, monkeypatch):
        """It is exempt, not a reset — an ineffective run proves nothing works."""
        msgs = _messages()
        hook = self._ineffective_hook(monkeypatch, msgs)
        _tracking_of(hook, "t-keep").consecutive_failures = 2
        await hook({"messages": msgs, "task_id": "t-keep"}, force=True)

        assert _tracking_of(hook, "t-keep").consecutive_failures == 2

    @pytest.mark.asyncio
    async def test_manual_result_is_still_reported_as_ineffective(self, monkeypatch):
        """Exempt from the breaker, but the user must learn it did not help."""
        msgs = _messages()
        hook = self._ineffective_hook(monkeypatch, msgs)
        await hook({"messages": msgs, "task_id": "t-report-manual"}, force=True)

        failed = _emitted(hook, "failed")
        assert failed, "an ineffective manual compaction was reported as success"
        assert failed[-1].kwargs["detail"]["ineffective"] is True

    @pytest.mark.asyncio
    async def test_manual_message_omits_the_breaker_counter(self, monkeypatch):
        """Quoting the counter would show a stale automatic-path figure."""
        msgs = _messages()
        hook = self._ineffective_hook(monkeypatch, msgs)
        _tracking_of(hook, "t-msg").consecutive_failures = 2
        await hook({"messages": msgs, "task_id": "t-msg"}, force=True)

        message = _emitted(hook, "failed")[-1].args[2]
        assert f"/{MAX_CONSECUTIVE_COMPACT_FAILURES}" not in message, (
            f"manual message quotes a breaker count it did not contribute to: "
            f"{message!r}"
        )


class TestThresholdBoundary:
    """The ratio must not flag a compaction that genuinely made room."""

    def test_ratio_leaves_headroom_for_real_reductions(self):
        before = 100_000
        assert before * 0.5 < before * INEFFECTIVE_COMPACTION_RATIO
        assert before * 0.9 < before * INEFFECTIVE_COMPACTION_RATIO

    @pytest.mark.asyncio
    async def test_a_modest_but_real_reduction_is_not_flagged(self, monkeypatch):
        """Just past the ratio counts as progress — no false breaker trip."""
        hook = _hook()
        msgs = _messages(count=20)
        # A NON-ZERO start: asserting ==0 from a zero default would pass even if
        # the branch never ran, which is how the first version of this test
        # fooled itself.
        _tracking_of(hook, "t-modest").consecutive_failures = 1
        # Drop a quarter of the history: well under the 0.95 bar.
        keep = msgs[: int(len(msgs) * 0.75)]
        monkeypatch.setattr(
            hook.context_manager, "check_context", lambda *a, **kw: (list(msgs), keep, True)
        )
        await hook({"messages": msgs, "task_id": "t-modest"})

        assert _tracking_of(hook, "t-modest").consecutive_failures == 0
