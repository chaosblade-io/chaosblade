"""Compaction must converge: summaries cannot accumulate without bound.

Every compaction appends one ``[Compressed History]`` summary. The previous
implementation also moved EVERY existing summary into ``messages_to_keep``, so
summaries were never reclaimed. Measured on the real 131,072-token window
(qwen3-max, ratio 0.80 → threshold 104,857) that inverted compaction outright:

    30 summaries → 145,165 tokens (past the threshold AND the window)
                 → to_compact held 29 messages worth 42 tokens
                 → "compacting" freed 42 tokens and appended ~4,600
                 → every pass grew the context, up to 290,286 (2.2× window)

Recycling the older summaries is lossless: their content reaches the
summarising LLM twice — through the cumulative ``previous_summary`` in the
prompt, and as messages inside ``to_compact`` itself. The newest summary is
still kept verbatim as the one safeguard that does not depend on the model
honouring "build upon the previous summary".
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import add_messages

from chaos_agent.config.settings import settings
from chaos_agent.memory.context_manager import (
    COMPRESSED_HISTORY_PREFIX,
    MAX_SUMMARY_SHARE_OF_RESERVE,
    SUMMARIES_KEPT_VERBATIM,
    ContextManager,
    _is_compressed_history,
)
from chaos_agent.memory.tokens import count_tokens_messages

# Sized so a handful of these crosses the real threshold, which is what makes
# the accumulation visible at all.
_SUMMARY_BODY = "摘要内容 " * 1150

# Convergence is a STRUCTURAL property — summaries are reclaimed regardless of
# how large the window happens to be — so the multi-cycle tests run on a small
# synthetic window. Tokenising enough CJK text to cross the real 104,857-token
# threshold thirty times costs over a minute of pure tiktoken work; the same
# behaviour reproduces in ~2s at 60K. The single-shot tests below still use the
# configured model's real budget, so the numbers in this module's docstring stay
# anchored to production.
_SMALL_WINDOW = 60_000
_SMALL_RATIO = 0.8
_SMALL_SUMMARY_BODY = "摘要内容 " * 300


def _summary(idx: int) -> SystemMessage:
    return SystemMessage(content=f"{COMPRESSED_HISTORY_PREFIX}\n{_SUMMARY_BODY}", id=f"s{idx}")


def _turn(rnd: int, size: int = 300) -> list:
    out: list = []
    for i in range(4):
        out.append(AIMessage(content="执行注入并核对指标" * size, id=f"a{rnd}_{i}"))
        out.append(
            ToolMessage(
                content="节点状态输出" * size, tool_call_id=f"c{rnd}_{i}", id=f"t{rnd}_{i}"
            )
        )
    return out


def _real_manager() -> ContextManager:
    """A manager on the configured model's real budget, not an invented one."""
    max_tokens, ratio = settings.resolve_context_budget()
    return ContextManager(max_tokens=max_tokens, compact_ratio=ratio)


def _small_manager() -> ContextManager:
    """A small-window manager for the multi-cycle tests (see note above)."""
    return ContextManager(max_tokens=_SMALL_WINDOW, compact_ratio=_SMALL_RATIO)


def _small_summary(idx: int) -> SystemMessage:
    return SystemMessage(
        content=f"{COMPRESSED_HISTORY_PREFIX}\n{_SMALL_SUMMARY_BODY}", id=f"ss{idx}"
    )


def _sized_summary(units: int, msg_id: str) -> SystemMessage:
    """A summary whose size can be dialled against the share budget."""
    return SystemMessage(
        content=f"{COMPRESSED_HISTORY_PREFIX}\n{'摘要 ' * max(units, 1)}", id=msg_id
    )


def _small_turn(rnd: int) -> list:
    out: list = []
    for i in range(4):
        out.append(AIMessage(content="执行注入" * 150, id=f"sa{rnd}_{i}"))
        out.append(
            ToolMessage(content="状态输出" * 150, tool_call_id=f"sc{rnd}_{i}", id=f"st{rnd}_{i}")
        )
    return out


def _count_summaries(messages: list) -> int:
    return sum(1 for m in messages if _is_compressed_history(m))


def _summaries_over_threshold(cm: ContextManager) -> list:
    """Enough summaries to actually cross ``cm``'s trigger, plus a tiny turn.

    Derived, never hard-coded. These tests originally used a flat 30 summaries,
    which crossed the 104,857 threshold of a 131,072-token window — and then
    ``qwen3-max`` was corrected to its real 262,144 window, the threshold moved to
    209,715, and 30 summaries (145,142 tokens) stopped triggering anything.
    ``check_context`` returned empty lists and all four assertions passed over a
    code path that never ran: a silent failure, in tests written to catch exactly
    that class of bug.
    """
    per_summary = count_tokens_messages([_summary(0)]).count
    needed = cm.compact_threshold // max(per_summary, 1) + 3
    messages = [_summary(i) for i in range(needed)] + _turn(0, size=1)
    # The guard the first version lacked: prove the fixture reaches the branch.
    assert count_tokens_messages(messages).safe_count > cm.compact_threshold, (
        f"fixture builds {count_tokens_messages(messages).safe_count} tokens "
        f"against a {cm.compact_threshold} threshold — it would not trigger "
        f"compaction and these assertions would pass vacuously"
    )
    return messages


class TestOlderSummariesAreReclaimed:
    def test_only_the_newest_summary_is_kept(self):
        cm = _real_manager()
        messages = _summaries_over_threshold(cm)
        to_compact, to_keep, _ = cm.check_context(messages)
        assert to_compact, "compaction did not trigger"
        assert _count_summaries(to_keep) == SUMMARIES_KEPT_VERBATIM

    def test_the_kept_summary_is_the_most_recent_one(self):
        """Recency matters: the newest summary already subsumes the older ones."""
        cm = _real_manager()
        messages = _summaries_over_threshold(cm)
        newest_summary_id = [
            m.id for m in messages if _is_compressed_history(m)
        ][-1]
        to_compact, to_keep, _ = cm.check_context(messages)
        assert to_compact, "compaction did not trigger"
        kept = [m for m in to_keep if _is_compressed_history(m)]
        assert kept[0].id == newest_summary_id

    def test_recycled_summaries_reach_the_compaction_input(self):
        """They must be summarised, not silently dropped."""
        cm = _real_manager()
        messages = _summaries_over_threshold(cm)
        total_summaries = _count_summaries(messages)
        to_compact, _, _ = cm.check_context(messages)
        assert _count_summaries(to_compact) == total_summaries - SUMMARIES_KEPT_VERBATIM

    def test_compaction_now_frees_more_than_it_adds(self):
        """The inversion: freeing 42 tokens while appending ~4,600."""
        cm = _real_manager()
        messages = _summaries_over_threshold(cm)
        to_compact, _, _ = cm.check_context(messages)
        freed = count_tokens_messages(to_compact).count
        appended = count_tokens_messages([_summary(999)]).count
        assert freed > appended * 10, (
            f"compaction freed {freed} tokens while a new summary costs "
            f"{appended} — this is the inverted behaviour the fix removes"
        )


class TestGeneratedSummariesAreRecognisable:
    """The prefix written by the hook must be the prefix the manager matches.

    Every summary-aware decision — which summaries to retain, which to recycle,
    whether recent context still fits — keys off ``_is_compressed_history``. If
    the writer used a literal and the constant were ever changed, summaries would
    simply stop being recognised as summaries and all of that logic would quietly
    stop applying. Nothing would raise; context would just start growing again.
    """

    def test_hook_writes_a_summary_the_manager_recognises(self):
        from chaos_agent.memory import hook as hook_module

        written = SystemMessage(
            content=f"{hook_module.COMPRESSED_HISTORY_PREFIX}\n摘要正文"
        )
        assert _is_compressed_history(written)

    def test_writer_and_matcher_share_one_constant(self):
        """Not two equal literals — the same object, so they cannot diverge."""
        from chaos_agent.memory import hook as hook_module

        assert hook_module.COMPRESSED_HISTORY_PREFIX is COMPRESSED_HISTORY_PREFIX

    def test_recognition_survives_renaming_the_prefix(self, monkeypatch):
        """A rename must keep working end to end, which a literal would not."""
        from chaos_agent.memory import context_manager as cm_module

        monkeypatch.setattr(cm_module, "COMPRESSED_HISTORY_PREFIX", "[Compacted]")
        written = SystemMessage(content=f"{cm_module.COMPRESSED_HISTORY_PREFIX}\n摘要")
        assert cm_module._is_compressed_history(written)


class TestOversizedSummaryDoesNotStarveRecentContext:
    """A summary too large to sit beside recent turns must be recycled too.

    The reservation pass stops once ``kept_tokens`` passes ``reserve_tokens``, so
    an oversized summary does not merely crowd recent messages out — it leaves
    NONE of them, handing the model a stale checkpoint with no idea what just
    happened. Reachable today: ``compact_memory`` prepends recovered critical
    context to the summary, and its skill budget alone (25,000) already exceeds
    the 20,000-token reservation.
    """

    @staticmethod
    def _oversized_summary(cm: ContextManager) -> SystemMessage:
        budget = int(cm.reserve_tokens * MAX_SUMMARY_SHARE_OF_RESERVE)
        body = "摘要内容 " * 3000
        msg = SystemMessage(content=f"{COMPRESSED_HISTORY_PREFIX}\n{body}", id="big")
        assert count_tokens_messages([msg]).count > budget, "fixture is not oversized"
        return msg

    def _history(self, cm: ContextManager, summary: SystemMessage) -> list:
        messages: list = [summary]
        for rnd in range(60):
            messages.extend(_small_turn(rnd))
        return messages

    def test_recent_messages_are_still_kept(self):
        cm = _small_manager()
        messages = self._history(cm, self._oversized_summary(cm))
        to_compact, to_keep, _ = cm.check_context(messages)
        assert to_compact, "fixture must trigger compaction"
        recent_kept = [m for m in to_keep if not _is_compressed_history(m)]
        assert recent_kept, (
            "an oversized summary consumed the whole reservation and left the "
            "model with no recent context at all"
        )

    def test_the_oversized_summary_is_recycled_not_dropped(self):
        """Its content must be re-summarised, so it has to reach to_compact."""
        cm = _small_manager()
        summary = self._oversized_summary(cm)
        to_compact, to_keep, _ = cm.check_context(self._history(cm, summary))
        assert _count_summaries(to_compact) == 1
        assert _count_summaries(to_keep) == 0

    def test_a_summary_within_its_share_is_still_retained(self):
        """The cap must not start discarding normal-sized summaries."""
        cm = _small_manager()
        summary = _small_summary(0)
        budget = int(cm.reserve_tokens * MAX_SUMMARY_SHARE_OF_RESERVE)
        assert count_tokens_messages([summary]).count < budget, "fixture too large"
        _, to_keep, _ = cm.check_context(self._history(cm, summary))
        assert _count_summaries(to_keep) == 1

    def test_the_newest_summary_wins_the_share(self, monkeypatch):
        """Priority must survive raising ``SUMMARIES_KEPT_VERBATIM``.

        The share is consumed in visit order, so walking oldest-first let an
        older summary claim the budget and pushed the NEWEST one into
        ``to_compact`` — inverting the priority the setting exists to express.
        Unreachable at 1, but its own comment weighs raising it, and a config
        change must not silently reverse which checkpoint survives.
        """
        from chaos_agent.memory import context_manager as cm_module

        monkeypatch.setattr(cm_module, "SUMMARIES_KEPT_VERBATIM", 2)
        cm = _small_manager()
        budget = int(cm.reserve_tokens * MAX_SUMMARY_SHARE_OF_RESERVE)
        per_unit = count_tokens_messages([_sized_summary(1000, "probe")]).count / 1000
        older = _sized_summary(int(budget * 0.9 / per_unit), "older")
        newest = _sized_summary(int(budget * 0.3 / per_unit), "newest")
        assert count_tokens_messages([older]).count < budget, "older must fit alone"
        assert (
            count_tokens_messages([older]).count + count_tokens_messages([newest]).count
            > budget
        ), "the pair must exceed the share for this test to mean anything"

        messages: list = [older, newest]
        for rnd in range(60):
            messages.extend(_small_turn(rnd))
        to_compact, to_keep, _ = cm.check_context(messages)

        kept_ids = [m.id for m in to_keep if _is_compressed_history(m)]
        assert kept_ids == ["newest"], (
            f"kept {kept_ids} — the newest checkpoint must win the share, not "
            f"whichever summary happened to be visited first"
        )
        assert [m.id for m in to_compact if _is_compressed_history(m)] == ["older"]


class TestRecycledSummariesCanActuallyBeRemoved:
    """Routing a summary into ``to_compact`` only helps if it can be deleted.

    The hook deletes compacted messages with ``RemoveMessage(id=...)`` behind an
    ``if msg_id:`` guard, and it creates summaries without an explicit id. The
    whole fix therefore rests on ``add_messages`` assigning one when the summary
    enters state. If it ever stopped doing so, summaries would be selected for
    recycling every turn and never actually removed — the reclaim would be a
    no-op and growth would resume, silently.
    """

    def test_a_summary_gains_an_id_when_it_enters_state(self):
        created = SystemMessage(content=f"{COMPRESSED_HISTORY_PREFIX}\n摘要")
        assert created.id is None, "fixture assumes the hook creates it without an id"

        in_state = add_messages([], [created])
        assert in_state[0].id, (
            "add_messages did not assign an id — RemoveMessage cannot delete "
            "this summary, so recycling would silently do nothing"
        )

    def test_removing_a_recycled_summary_leaves_only_the_new_one(self):
        """End to end: the reducer contract the reclaim depends on."""
        in_state = add_messages([], [SystemMessage(content=f"{COMPRESSED_HISTORY_PREFIX}\n旧摘要")])
        old = in_state[0]

        after = add_messages(
            in_state,
            [
                RemoveMessage(id=old.id),
                SystemMessage(content=f"{COMPRESSED_HISTORY_PREFIX}\n新摘要"),
            ],
        )
        assert _count_summaries(after) == 1
        assert "新摘要" in str(after[0].content)


class TestNothingChangesBelowTheThreshold:
    """Summaries must not be disturbed while there is room to spare."""

    def test_no_compaction_is_triggered_when_usage_is_low(self):
        cm = _real_manager()
        messages = [_summary(0)] + _turn(0, size=1)
        to_compact, to_keep, is_valid = cm.check_context(messages)
        assert to_compact == []
        assert to_keep == messages
        assert is_valid

    def test_a_single_summary_survives_compaction_untouched(self):
        """With only one summary there is nothing to reclaim — it must stay."""
        cm = _small_manager()
        messages = [_small_summary(0)]
        for rnd in range(20):
            messages.extend(_small_turn(rnd))
        to_compact, to_keep, _ = cm.check_context(messages)
        assert to_compact, "fixture must be large enough to trigger compaction"
        assert _count_summaries(to_keep) == 1
        assert _count_summaries(to_compact) == 0


class TestLongSessionConverges:
    """The property that actually matters, over many compaction cycles."""

    def test_long_session_stays_under_the_window(self):
        cm = _small_manager()
        messages: list = []
        peak = 0
        max_summaries = 0
        compactions = 0

        for rnd in range(1, 61):
            messages.extend(_small_turn(rnd))
            peak = max(peak, count_tokens_messages(messages).count)
            to_compact, to_keep, _ = cm.check_context(messages)
            if to_compact:
                compactions += 1
                # Mirror the hook's landing shape: kept messages + one summary.
                messages = list(to_keep) + [_small_summary(rnd)]
                max_summaries = max(max_summaries, _count_summaries(messages))

        assert compactions >= 5, (
            f"only {compactions} compactions ran — fixture stopped exercising "
            f"the repeated-cycle path this test exists to cover"
        )
        assert peak < _SMALL_WINDOW, (
            f"peak {peak} reached the {_SMALL_WINDOW} window despite compacting "
            f"{compactions} times — compaction is not converging"
        )
        # One retained + one freshly appended is the steady state.
        assert max_summaries <= SUMMARIES_KEPT_VERBATIM + 1, (
            f"summaries grew to {max_summaries}; they must stay bounded"
        )

    def test_each_compaction_reduces_tokens(self):
        """No cycle may end larger than it started."""
        cm = _small_manager()
        messages: list = []
        deltas: list[int] = []

        for rnd in range(1, 61):
            messages.extend(_small_turn(rnd))
            before = count_tokens_messages(messages).count
            to_compact, to_keep, _ = cm.check_context(messages)
            if to_compact:
                messages = list(to_keep) + [_small_summary(rnd)]
                deltas.append(count_tokens_messages(messages).count - before)

        assert len(deltas) >= 5, f"only {len(deltas)} compactions ran"
        assert all(d < 0 for d in deltas), f"a compaction increased tokens: {deltas}"


class TestPairIntegrityIsPreserved:
    """Moving summaries between the lists must not orphan a tool result."""

    def test_no_tool_message_is_separated_from_its_caller(self):
        cm = _small_manager()
        messages = [_small_summary(i) for i in range(10)]
        for rnd in range(20):
            messages.extend(_small_turn(rnd))
        to_compact, to_keep, _ = cm.check_context(messages)
        assert to_compact, "fixture must trigger compaction to be meaningful"
        compact_call_ids = {
            tc.get("id")
            for m in to_compact
            for tc in (getattr(m, "tool_calls", None) or [])
        }
        keep_call_ids = {
            tc.get("id")
            for m in to_keep
            for tc in (getattr(m, "tool_calls", None) or [])
        }
        for msg in to_keep:
            if getattr(msg, "type", "") == "tool":
                tc_id = getattr(msg, "tool_call_id", None)
                assert tc_id not in compact_call_ids or tc_id in keep_call_ids, (
                    f"tool result {tc_id} kept while its caller was compacted"
                )
