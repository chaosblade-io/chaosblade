"""Tests for token-aware context manager.

Note (E1): Token-counting was extracted to
``chaos_agent.memory.tokens`` with a 4-layer model-aware fallback.
The exhaustive CJK / mixed / multi-modal coverage lives in
``tests/test_memory/test_tokens.py``. The two test classes that used
to exercise the legacy ``estimate_tokens`` / ``count_tokens_approx``
symbols were removed because their semantics are now Layer 4 of the
new module, fully covered by ``TestLayer4HeuristicFallback`` and
``TestMessageAggregation`` over there.
"""

import logging
from unittest.mock import MagicMock

from chaos_agent.memory.context_manager import (
    CompactLevel,
    CompactTrackingState,
    ContextManager,
    MAX_CONSECUTIVE_COMPACT_FAILURES,
    MAX_SUMMARY_SHARE_OF_RESERVE,
    TokenWarningState,
    calculate_token_warning_state,
    ensure_pair_integrity,
    group_messages_by_round,
    resolve_auto_compact_threshold,
    strip_large_outputs,
)


class TestEnsurePairIntegrity:
    """Test tool_call/tool_result pair integrity."""

    def test_empty_to_compact(self):
        to_compact, to_keep = ensure_pair_integrity([], [MagicMock()])
        assert to_compact == []
        assert len(to_keep) == 1

    def test_last_message_has_tool_calls(self):
        """If last in to_compact has tool_calls, move it to to_keep."""
        msg_with_calls = MagicMock()
        msg_with_calls.tool_calls = [{"name": "test"}]

        to_keep = [MagicMock()]
        to_compact = [MagicMock(), msg_with_calls]

        result_compact, result_keep = ensure_pair_integrity(to_compact, to_keep)
        # The message with tool_calls should be moved to to_keep
        assert msg_with_calls not in result_compact

    def test_no_tool_calls_unchanged(self):
        msg = MagicMock()
        msg.tool_calls = []
        to_compact = [msg]
        to_keep = [MagicMock()]

        result_compact, result_keep = ensure_pair_integrity(to_compact, to_keep)
        assert len(result_compact) == 1


class TestContextManager:
    """Test ContextManager.check_context()."""

    def test_below_threshold_no_compaction(self):
        cm = ContextManager(max_tokens=50000)
        msgs = [MagicMock(content="short message")]
        to_compact, to_keep, valid = cm.check_context(msgs)
        assert to_compact == []
        assert to_keep == msgs

    def test_above_threshold_triggers_compaction(self):
        cm = ContextManager(max_tokens=100)
        # Override reserve_tokens to be small so compaction actually happens
        cm.reserve_tokens = 10
        msgs = [MagicMock(content="a" * 400) for _ in range(10)]  # Large messages
        to_compact, to_keep, valid = cm.check_context(msgs)
        assert len(to_compact) > 0

    def test_reserves_recent_messages(self):
        cm = ContextManager(max_tokens=100)
        recent_msg = MagicMock(content="recent")
        msgs = [MagicMock(content="a" * 400) for _ in range(10)] + [recent_msg]
        to_compact, to_keep, valid = cm.check_context(msgs)
        # The recent message should be in to_keep
        assert recent_msg in to_keep

    def test_compact_threshold_matches_the_trigger_judgement(self):
        """``compact_threshold`` must equal the auto-compact trigger point.

        Previously it was a bare ``max_tokens * compact_ratio`` while
        ``calculate_token_warning_state`` applied a buffer ceiling and a 50%
        floor on top. The two agreed at the default ratio, so the split stayed
        invisible — but the hook compares its post-strip total against
        ``compact_threshold``, so any drift lets it accept a context the trigger
        has already rejected. Asserting equality (rather than restating a
        formula) is what keeps them from separating again.
        """
        for max_tokens, ratio in (
            (1000, 0.7),        # tiny window: the 50% floor decides
            (131_072, 0.8),     # production default
            (131_072, 0.95),    # ratio above the buffer ceiling
            (32_768, 0.5),
        ):
            cm = ContextManager(max_tokens=max_tokens, compact_ratio=ratio)
            assert cm.compact_threshold == resolve_auto_compact_threshold(
                max_tokens, ratio
            ), f"threshold drifted at max_tokens={max_tokens}, ratio={ratio}"
            # And the trigger really does fire at exactly that count.
            state = calculate_token_warning_state(
                cm.compact_threshold, max_tokens, compact_ratio=ratio
            )
            assert state.is_above_auto_compact, (
                f"compact_threshold={cm.compact_threshold} is not yet a trigger "
                f"point at max_tokens={max_tokens}, ratio={ratio}"
            )

    def test_operator_ratio_can_only_trigger_earlier(self):
        """A higher ratio must never delay compaction past the buffer ceiling."""
        max_tokens = 131_072
        ceiling = resolve_auto_compact_threshold(max_tokens, 1.0)
        for ratio in (0.8, 0.9, 0.95, 1.0):
            assert (
                resolve_auto_compact_threshold(max_tokens, ratio) <= ceiling
            ), f"ratio={ratio} pushed the threshold past the buffer ceiling"

    def test_compact_ratio_stored_on_instance(self):
        # Bug 4 setup: the manager must KEEP the raw ratio so
        # check_context can thread it into
        # calculate_token_warning_state. Without this attribute the
        # warning state always falls back to the function's default
        # (0.85), making the operator's BLADE_AI_CONTEXT_COMPACT_RATIO
        # setting cosmetic only.
        cm = ContextManager(max_tokens=128_000, compact_ratio=0.6)
        assert cm.compact_ratio == 0.6

    def test_check_context_threads_compact_ratio_through(self):
        # Bug 4 end-to-end: a ContextManager built with a lower
        # compact_ratio MUST trigger compaction at a lower token count
        # than one built with the default. Pre-fix, both managers
        # behaved identically because check_context didn't pass the
        # ratio through.
        #
        # Use real HumanMessages with VARIED content. The original
        # test used "x" * 35_000 which the legacy chars/4 heuristic
        # estimated at ~8750 tokens but tiktoken-based counters (E1)
        # correctly compress to ~10 tokens (BPE handles repeated
        # chars efficiently). Realistic varied prose produces
        # predictable token counts under any tokenizer.
        from langchain_core.messages import HumanMessage
        # Build ~5000-token messages × 16 → ~80k tokens total, well
        # above the aggressive 64k threshold but below the default
        # 108.8k threshold (compact_ratio=0.85 of 128k).
        sentence = (
            "The chaos engineering agent must safely plan, execute, "
            "verify, and recover ChaosBlade experiments on Kubernetes "
            "clusters with measurable steady-state hypotheses. "
            "故障注入演练 必须 在可控范围内 进行，每次注入前 都需要 "
            "明确的回滚方案 和 监控指标。"
        )
        # ~1 token per ASCII word + ~1 token per CJK char ≈ ~60 tokens
        # per sentence × 80 repeats = ~4800 tokens per message
        long_content = (sentence + "\n") * 80
        msgs = [HumanMessage(content=long_content) for _ in range(16)]
        cm_default = ContextManager(max_tokens=128_000, compact_ratio=0.85)
        cm_aggressive = ContextManager(max_tokens=128_000, compact_ratio=0.5)
        cm_default.reserve_tokens = 1
        cm_aggressive.reserve_tokens = 1
        d_compact, _, _ = cm_default.check_context(msgs)
        a_compact, _, _ = cm_aggressive.check_context(msgs)
        # Default (threshold ≈ 108.8K): ~80K is BELOW, no compaction.
        # Aggressive (threshold = 64K): ~80K is ABOVE, compaction.
        assert d_compact == []
        assert len(a_compact) > 0


# ---------------------------------------------------------------------------
# New tests: Multi-level warning + circuit breaker (Migration Point 10)
# ---------------------------------------------------------------------------


class TestCompactLevel:
    """Test CompactLevel enum."""

    def test_levels_exist(self):
        assert CompactLevel.NORMAL.value == "normal"
        assert CompactLevel.WARNING.value == "warning"
        assert CompactLevel.ERROR.value == "error"
        assert CompactLevel.AUTO_COMPACT.value == "auto_compact"
        assert CompactLevel.BLOCKING.value == "blocking"


class TestTokenWarningState:
    """Test TokenWarningState dataclass."""

    def test_fields(self):
        state = TokenWarningState(
            percent_left=50,
            level=CompactLevel.WARNING,
            is_above_warning=True,
            is_above_error=False,
            is_above_auto_compact=False,
            is_at_blocking=False,
        )
        assert state.percent_left == 50
        assert state.level == CompactLevel.WARNING


class TestCalculateTokenWarningState:
    """Test calculate_token_warning_state multi-level decision."""

    def test_normal_level_low_usage(self):
        ws = calculate_token_warning_state(1000, 50000)
        assert ws.level == CompactLevel.NORMAL
        assert not ws.is_above_warning
        assert not ws.is_above_auto_compact
        assert not ws.is_at_blocking

    def test_warning_level_approaching_threshold(self):
        # With max=50000, auto_compact_threshold = max(37000, 36000) = 37000
        # warning_threshold = max(37000 - 20000, 0) = 17000
        ws = calculate_token_warning_state(21000, 50000)
        assert ws.is_above_warning
        assert ws.level in (CompactLevel.WARNING, CompactLevel.ERROR, CompactLevel.AUTO_COMPACT)

    def test_auto_compact_level_above_threshold(self):
        # auto_compact_threshold = max(50000 - 13000, 50000 * 0.72) = max(37000, 36000) = 37000
        ws = calculate_token_warning_state(41000, 50000)
        assert ws.is_above_auto_compact
        assert ws.level in (CompactLevel.AUTO_COMPACT, CompactLevel.BLOCKING)

    def test_blocking_level_at_limit(self):
        # blocking_limit = 50000 - 3000 = 47000
        ws = calculate_token_warning_state(48000, 50000)
        assert ws.is_at_blocking
        assert ws.level == CompactLevel.BLOCKING

    def test_auto_compact_disabled(self):
        ws = calculate_token_warning_state(38000, 50000, auto_compact_enabled=False)
        assert not ws.is_above_auto_compact
        # Should not reach AUTO_COMPACT level, might be ERROR or WARNING
        assert ws.level != CompactLevel.AUTO_COMPACT or ws.level == CompactLevel.BLOCKING

    def test_percent_left_decreases(self):
        ws_low = calculate_token_warning_state(1000, 50000)
        ws_high = calculate_token_warning_state(30000, 50000)
        assert ws_low.percent_left > ws_high.percent_left

    def test_percent_left_non_negative(self):
        ws = calculate_token_warning_state(60000, 50000)
        assert ws.percent_left >= 0

    def test_compact_ratio_lowers_trigger_threshold(self):
        # Bug 4 regression guard: the operator-tunable ``compact_ratio``
        # was dead code under the old ``max(buffer, ratio)`` formula —
        # the buffer almost always won. The new ``min(...)`` formula
        # MUST let a lower ratio pull the trigger earlier.
        #
        # Setup: 128K window. Default 0.85 ratio → threshold ≈ 108.8K
        # (buffer floor 115K loses to ratio). With ratio 0.5 → 64K.
        # A usage of 70K must therefore:
        #   - sit BELOW the 0.85 trigger (108.8K), and
        #   - sit ABOVE the 0.5 trigger (64K).
        # If either assert flips, the regression is back.
        below_default = calculate_token_warning_state(
            70_000, 128_000, compact_ratio=0.85,
        )
        above_aggressive = calculate_token_warning_state(
            70_000, 128_000, compact_ratio=0.5,
        )
        assert not below_default.is_above_auto_compact
        assert above_aggressive.is_above_auto_compact

    def test_compact_ratio_capped_by_buffer_ceiling(self):
        # The buffer floor (max_tokens - AUTOCOMPACT_BUFFER_TOKENS) is a
        # SAFETY ceiling on the trigger — a too-large ratio (e.g. 0.99)
        # must NOT push compaction past it, otherwise we risk overrunning
        # the provider's hard window before compaction has room to run.
        ws = calculate_token_warning_state(
            117_000, 128_000, compact_ratio=0.99,
        )
        # buffer ceiling = 128000 - 13000 = 115000. 0.99 * 128000 = 126720.
        # min(115000, 126720) = 115000. 117000 > 115000 → triggers.
        assert ws.is_above_auto_compact

    def test_compact_ratio_floor_at_50_percent(self):
        # Defensive floor: an operator typo (compact_ratio=0.05) must
        # not let the threshold collapse to almost-zero, otherwise the
        # system would try to compact on every single message and
        # never make progress. The clamp keeps the trigger at ≥ 50%.
        ws_low = calculate_token_warning_state(
            60_000, 128_000, compact_ratio=0.05,
        )
        ws_normal = calculate_token_warning_state(
            60_000, 128_000, compact_ratio=0.5,
        )
        # 0.05 ratio is clamped to 0.5 internally, so 60K (< 64K floor)
        # must NOT trigger — same as the 0.5 case.
        assert not ws_low.is_above_auto_compact
        assert not ws_normal.is_above_auto_compact


class TestCompactTrackingState:
    """Test circuit breaker tracking state."""

    def test_default_values(self):
        ts = CompactTrackingState()
        assert ts.compacted is False
        assert ts.turn_count == 0
        assert ts.consecutive_failures == 0

    def test_custom_values(self):
        ts = CompactTrackingState(compacted=True, turn_count=5, consecutive_failures=2)
        assert ts.compacted is True
        assert ts.turn_count == 5
        assert ts.consecutive_failures == 2


class TestContextManagerWithTracking:
    """Test ContextManager.check_context() with circuit breaker."""

    def test_circuit_breaker_trips(self):
        """When consecutive failures exceed limit, no compaction attempted."""
        cm = ContextManager(max_tokens=100)
        cm.reserve_tokens = 10
        tracking = CompactTrackingState(
            consecutive_failures=MAX_CONSECUTIVE_COMPACT_FAILURES
        )
        msgs = [MagicMock(content="a" * 400) for _ in range(10)]
        to_compact, to_keep, valid = cm.check_context(msgs, tracking=tracking)
        assert to_compact == []
        assert valid is False  # Blocked by circuit breaker

    def test_circuit_breaker_not_tripped_under_limit(self):
        """When failures are under limit, compaction proceeds normally."""
        cm = ContextManager(max_tokens=100)
        cm.reserve_tokens = 10
        tracking = CompactTrackingState(consecutive_failures=1)
        msgs = [MagicMock(content="a" * 400) for _ in range(10)]
        to_compact, to_keep, valid = cm.check_context(msgs, tracking=tracking)
        assert len(to_compact) > 0

    def test_no_tracking_compacts_normally(self):
        """Without tracking state, compaction works as before."""
        cm = ContextManager(max_tokens=100)
        cm.reserve_tokens = 10
        msgs = [MagicMock(content="a" * 400) for _ in range(10)]
        to_compact, to_keep, valid = cm.check_context(msgs, tracking=None)
        assert len(to_compact) > 0

    def test_blocking_level_returns_invalid(self):
        """At blocking level, is_valid is False even without tracking."""
        cm = ContextManager(max_tokens=100)
        cm.reserve_tokens = 10
        # Create very large messages to exceed blocking limit
        msgs = [MagicMock(content="z" * 2000) for _ in range(10)]
        to_compact, to_keep, valid = cm.check_context(msgs)
        # At blocking level, valid should be False
        assert valid is False


# ---------------------------------------------------------------------------
# New tests: group_messages_by_round (Migration Point 8)
# ---------------------------------------------------------------------------


class TestGroupMessagesByRound:
    """Test group_messages_by_round() robust API round grouping."""

    def _make_msg(self, content: str, msg_type: str = "human", has_tool_calls: bool = False) -> MagicMock:
        msg = MagicMock()
        msg.type = msg_type
        msg.content = content
        if has_tool_calls:
            msg.tool_calls = [{"name": "test", "args": {}}]
        else:
            msg.tool_calls = []
        return msg

    def test_empty_messages(self):
        assert group_messages_by_round([]) == []

    def test_single_human_message(self):
        msgs = [self._make_msg("hello")]
        groups = group_messages_by_round(msgs)
        assert len(groups) == 1
        assert len(groups[0]) == 1

    def test_ai_with_tool_calls_starts_new_group(self):
        """AI message with tool_calls starts a new round."""
        msgs = [
            self._make_msg("user question"),
            self._make_msg("thinking", msg_type="ai", has_tool_calls=True),
            self._make_msg("tool result", msg_type="tool"),
        ]
        groups = group_messages_by_round(msgs)
        # First group: user question
        # Second group: AI + tool result
        assert len(groups) == 2
        assert groups[1][0].content == "thinking"
        assert groups[1][1].content == "tool result"

    def test_consecutive_ai_messages_form_separate_groups(self):
        """Each AI message starts its own group."""
        msgs = [
            self._make_msg("user1"),
            self._make_msg("response1", msg_type="ai"),
            self._make_msg("user2"),
            self._make_msg("response2", msg_type="ai"),
        ]
        groups = group_messages_by_round(msgs)
        assert len(groups) >= 2

    def test_tool_result_not_split_from_ai(self):
        """Tool results must stay with their AI caller."""
        msgs = [
            self._make_msg("user"),
            self._make_msg("calling tool", msg_type="ai", has_tool_calls=True),
            self._make_msg("result 1", msg_type="tool"),
            self._make_msg("result 2", msg_type="tool"),
        ]
        groups = group_messages_by_round(msgs)
        # Find the group with the AI message
        ai_group = None
        for g in groups:
            if any(getattr(m, "type", "") == "ai" for m in g):
                ai_group = g
                break
        assert ai_group is not None
        # Both tool results should be in the same group as the AI message
        tool_count = sum(1 for m in ai_group if getattr(m, "type", "") == "tool")
        assert tool_count == 2

    def test_all_messages_preserved(self):
        """No messages should be lost during grouping."""
        msgs = [
            self._make_msg("user"),
            self._make_msg("ai", msg_type="ai", has_tool_calls=True),
            self._make_msg("result", msg_type="tool"),
            self._make_msg("user2"),
        ]
        groups = group_messages_by_round(msgs)
        total_msgs = sum(len(g) for g in groups)
        assert total_msgs == len(msgs)


# ---------------------------------------------------------------------------
# New tests: strip_large_outputs (Migration Point 8)
# ---------------------------------------------------------------------------


class TestStripLargeOutputs:
    """Test strip_large_outputs() progressive compression."""

    def _make_tool_msg(self, content: str) -> MagicMock:
        msg = MagicMock()
        msg.type = "tool"
        msg.content = content
        return msg

    def _make_human_msg(self, content: str) -> MagicMock:
        msg = MagicMock()
        msg.type = "human"
        msg.content = content
        return msg

    def test_short_tool_output_unchanged(self):
        content = "short output"
        msgs = [self._make_tool_msg(content)]
        result = strip_large_outputs(msgs)
        assert result[0].content == content

    def test_large_tool_output_truncated(self):
        # Content above threshold
        long_content = "x" * 3000
        msgs = [self._make_tool_msg(long_content)]
        result = strip_large_outputs(msgs)
        # Quantified elision marker (shared dialect) — the hidden middle
        # is visible, not silent.
        assert "chars elided" in result[0].content
        assert len(result[0].content) < len(long_content)

    def test_head_and_tail_preserved(self):
        long_content = "A" * 600 + "MIDDLE" + "Z" * 600
        msgs = [self._make_tool_msg(long_content)]
        result = strip_large_outputs(msgs)
        assert result[0].content.startswith("AAA")
        assert result[0].content.endswith("ZZZ")

    def test_human_messages_not_stripped(self):
        long_content = "x" * 5000
        msgs = [self._make_human_msg(long_content)]
        result = strip_large_outputs(msgs)
        assert result[0].content == long_content

    def test_custom_threshold(self):
        content = "x" * 500
        msgs = [self._make_tool_msg(content)]
        # Default threshold (2000) — should not strip
        result_default = strip_large_outputs(msgs)
        assert result_default[0].content == content
        # Custom threshold mirroring the hook's live path (1000): content
        # above BOTH the threshold and elided_preview's own passthrough
        # boundary (head+tail=1000) gets the quantified both-ends cut.
        # Fresh mock — strip_large_outputs mutates plain mocks in place,
        # so reusing one across calls would compare against the ALREADY
        # stripped content.
        long_content = "x" * 1500
        result_custom = strip_large_outputs(
            [self._make_tool_msg(long_content)], threshold=1000
        )
        assert "chars elided" in result_custom[0].content
        # 1000 < len <= 2000 under the DEFAULT threshold: untouched (the
        # threshold gate must run first — elided_preview alone would elide).
        result_default_long = strip_large_outputs([self._make_tool_msg(long_content)])
        assert result_default_long[0].content == long_content

    def test_empty_messages(self):
        result = strip_large_outputs([])
        assert result == []


class TestCompactionBoundaryFailClosed:
    """to_keep head orphan recycling must be fail-closed (issue #1344 sig #2).

    Uses REAL langchain messages — the boundary loop checks ``msg.type ==
    "tool"``, which a MagicMock attribute (itself a MagicMock) never equals.

    Sizing discipline (a wrong size silently skips the code under test):
    the head ToolMessages must FIT the reserve so pass 2 keeps them and the
    BOUNDARY LOOP is what moves them; the filler must overflow the reserve so
    the split lands before it. Sizes are verified with the production token
    counter instead of guessed.
    """

    RESERVE = 80
    CM_LOGGER = "chaos_agent.memory.context_manager"

    def _cm(self):
        cm = ContextManager(max_tokens=100)
        cm.reserve_tokens = self.RESERVE
        return cm

    @staticmethod
    def _tokens(msg) -> int:
        from chaos_agent.memory.tokens import count_tokens_messages

        return count_tokens_messages([msg]).count

    def _assert_fits_reserve(self, kept_msgs):
        total = sum(self._tokens(m) for m in kept_msgs)
        assert total <= self.RESERVE, (
            f"test sizing broken: head messages need {total} tokens > "
            f"reserve {self.RESERVE}; they would never reach the boundary loop"
        )

    def _split(self, msgs):
        to_compact, to_keep, _valid = self._cm().check_context(msgs)
        return to_compact, to_keep

    def test_head_tool_with_caller_in_compact_moves(self, caplog):
        """Spec: caller 在 to_compact 中——配对移动 (no orphan warning)."""
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        caller = AIMessage(content="old " * 250, tool_calls=[
            {"name": "t", "args": {}, "id": "c1", "type": "tool_call"},
        ])
        tool = ToolMessage(content="result " * 8, tool_call_id="c1")
        tail = HumanMessage(content="recent " * 8)
        self._assert_fits_reserve([tool, tail])
        filler = HumanMessage(content="x " * 500)
        with caplog.at_level(logging.WARNING, logger=self.CM_LOGGER):
            to_compact, to_keep = self._split([filler, caller, tool, tail])
        assert caller in to_compact
        assert tool in to_compact  # boundary loop moved it back to pair
        assert tail in to_keep
        # caller found — paired move, so the orphan WARNING must not fire
        assert "ORPHAN" not in caplog.text

    def test_orphan_head_tool_still_moves_fail_closed(self, caplog):
        """Spec: caller 查不到——仍移动（fail-closed）+ WARNING.

        The old code did ``break`` here and left the orphan at the head of
        to_keep, shipping it to the provider on the next call.
        """
        from langchain_core.messages import HumanMessage, ToolMessage

        orphan = ToolMessage(content="stale " * 8, tool_call_id="gone")
        tail = HumanMessage(content="recent " * 8)
        self._assert_fits_reserve([orphan, tail])
        filler = HumanMessage(content="x " * 500)
        with caplog.at_level(logging.WARNING, logger=self.CM_LOGGER):
            to_compact, to_keep = self._split([filler, orphan, tail])
        assert orphan in to_compact  # fail-closed: moved despite missing caller
        assert orphan not in to_keep
        assert tail in to_keep
        assert "ORPHAN ToolMessage" in caplog.text
        assert "gone" in caplog.text

    def test_consecutive_orphans_all_recycled(self):
        """The loop continues through consecutive head ToolMessages."""
        from langchain_core.messages import HumanMessage, ToolMessage

        o1 = ToolMessage(content="r1 " * 8, tool_call_id="g1")
        o2 = ToolMessage(content="r2 " * 8, tool_call_id="g2")
        tail = HumanMessage(content="recent " * 8)
        self._assert_fits_reserve([o1, o2, tail])
        filler = HumanMessage(content="x " * 500)
        to_compact, to_keep = self._split([filler, o1, o2, tail])
        assert o1 in to_compact and o2 in to_compact
        assert not any(getattr(m, "type", "") == "tool" for m in to_keep)

    def test_non_tool_head_stops_loop_immediately(self):
        """Spec: 头部之后不受影响——first non-summary non-tool message stops."""
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        # A paired round fully inside to_keep: the head is an AI message, so
        # the boundary loop must stop and NOT touch the ToolMessage behind it.
        caller = AIMessage(content="", tool_calls=[
            {"name": "t", "args": {}, "id": "k1", "type": "tool_call"},
        ])
        tool = ToolMessage(content="kept result", tool_call_id="k1")
        self._assert_fits_reserve([caller, tool])
        filler = HumanMessage(content="x " * 500)
        to_compact, to_keep = self._split([filler, caller, tool])
        assert filler in to_compact
        assert caller in to_keep and tool in to_keep  # pair stays in to_keep

    def test_recycled_summary_does_not_reorder_to_compact(self):
        """A moved message must land at its CHRONOLOGICAL position.

        The boundary loop ``append``s, which is only the right home when
        to_compact's tail is the message just before the moved one. The
        recent-window pass SKIPS summaries without spending budget, so a
        RECYCLED summary can sit in to_compact at a position LATER than a kept
        message the loop then moves — measured ``[0, 2, 1]`` before the
        re-sort. to_compact is the summariser's input, so that is a real (if
        mild) quality regression rather than a cosmetic one.
        """
        from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

        from chaos_agent.memory.context_manager import COMPRESSED_HISTORY_PREFIX

        orphan = ToolMessage(content="stale " * 8, tool_call_id="gone")
        tail = HumanMessage(content="recent " * 8)
        self._assert_fits_reserve([orphan, tail])
        # Oversized so the summary share (reserve * 0.5) recycles it instead of
        # keeping it verbatim — recycling is what puts it in to_compact at a
        # position LATER than ``orphan``, which is the whole scenario.
        summary = SystemMessage(
            content=f"{COMPRESSED_HISTORY_PREFIX}\n" + "s " * 500
        )
        assert self._tokens(summary) > self.RESERVE * MAX_SUMMARY_SHARE_OF_RESERVE, (
            "test sizing broken: the summary would be kept verbatim and never "
            "reach to_compact, so nothing would sit out of order"
        )
        filler = HumanMessage(content="x " * 500)

        msgs = [filler, orphan, summary, tail]
        pos = {id(m): i for i, m in enumerate(msgs)}
        to_compact, to_keep = self._split(msgs)

        # Anti-vacuity: ``orphan`` was kept by the recent-window pass, so it is
        # in to_compact ONLY if the boundary loop moved it. Without this the
        # ordering assertion would pass on an unmoved (already sorted) list.
        assert orphan in to_compact, "boundary loop did not move the orphan"
        assert summary in to_compact
        assert len(to_keep) == 1 and tail in to_keep

        positions = [pos[id(m)] for m in to_compact]
        assert positions == sorted(positions), (
            f"to_compact is out of chronological order: {positions}"
        )

    def test_summary_interleaved_between_head_tools_does_not_skip_one(self):
        """A summary sitting between two head tools must not STOP the scan.

        Every other case here holds a run of CONSECUTIVE head ToolMessages, so
        the ``continue`` that steps over a kept summary is never exercised —
        no summary ever lands mid-scan. This one puts a verbatim-kept summary
        between two tools, which is the only shape where treating it as a
        boundary (``break`` instead of ``continue``) leaves the SECOND tool at
        the head of to_keep to ship unpaired.

        Both off-by-one mutations of this loop were measured, and they are
        killed by different tests, so neither shape is redundant:

        * ``continue`` → ``break`` at the summary: only THIS test fails.
        * ``i += 1`` added after ``pop(i)``: this test still PASSES — popping
          shifts the summary down to ``i``, so the increment lands on the
          second tool anyway. That mutant is killed by the consecutive-orphan
          cases instead, where the increment jumps straight to the tail and
          breaks. An earlier draft of this docstring claimed the opposite
          causality; it was wrong and is corrected here.

        Measured shape: to_compact ``[filler, tool, tool]``, to_keep
        ``[summary, tail]``.
        """
        from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

        from chaos_agent.memory.context_manager import COMPRESSED_HISTORY_PREFIX

        # Small enough to be KEPT verbatim — a recycled summary lands in
        # to_compact instead and never interleaves with the head tools.
        summary = SystemMessage(
            content=f"{COMPRESSED_HISTORY_PREFIX}\n" + "s " * 6
        )
        assert self._tokens(summary) <= self.RESERVE * MAX_SUMMARY_SHARE_OF_RESERVE, (
            "test sizing broken: the summary would be recycled into to_compact, "
            "so the two branches would never alternate"
        )
        t1 = ToolMessage(content="r1 " * 8, tool_call_id="g1")
        t2 = ToolMessage(content="r2 " * 8, tool_call_id="g2")
        tail = HumanMessage(content="recent " * 8)
        self._assert_fits_reserve([t1, summary, t2, tail])
        filler = HumanMessage(content="x " * 500)

        to_compact, to_keep = self._split([filler, t1, summary, t2, tail])

        assert t1 in to_compact and t2 in to_compact, \
            "both head tools must be recycled, not just the one before the summary"
        assert summary in to_keep, "a verbatim-kept summary is not the loop's business"
        assert not any(getattr(m, "type", "") == "tool" for m in to_keep)

    def test_to_keep_drained_entirely_exits_without_index_error(self):
        """``while i < len(messages_to_keep)`` when the loop empties the list.

        Every other case keeps a tail message behind, so the loop always exits
        by hitting a non-tool head. With nothing behind the tools it exits by
        draining instead, and ``messages_to_keep[i]`` is read at the TOP of each
        iteration — an off-by-one there raises IndexError inside compaction, on
        the hook path, which would take the whole turn down.
        """
        from langchain_core.messages import HumanMessage, ToolMessage

        t1 = ToolMessage(content="r1 " * 8, tool_call_id="g1")
        t2 = ToolMessage(content="r2 " * 8, tool_call_id="g2")
        self._assert_fits_reserve([t1, t2])
        filler = HumanMessage(content="x " * 500)

        to_compact, to_keep = self._split([filler, t1, t2])

        assert to_keep == [], "everything was recyclable, so nothing is kept"
        assert t1 in to_compact and t2 in to_compact
        assert filler in to_compact

    def test_empty_to_compact_skips_the_scan_because_nothing_is_compacting(self):
        """The guard's other half, pinned so it is not mistaken for fail-open.

        ``if messages_to_keep and messages_to_compact`` also skips the scan when
        to_compact is EMPTY, which leaves a head orphan in to_keep. That is not
        the fail-open this class closes: ``check_context`` returns
        ``is_valid=True`` here, meaning no compaction was warranted at all, so
        there is no summary to recycle the orphan into and moving it would
        destroy content for nothing. The messages ship as they are and the
        send-side gate (utils/message_integrity.py) remains the defence — the
        same layering the boundary loop's own comment relies on.
        """
        from langchain_core.messages import HumanMessage, ToolMessage

        orphan = ToolMessage(content="stale", tool_call_id="gone")
        tail = HumanMessage(content="hi")

        to_compact, to_keep, valid = self._cm().check_context([orphan, tail])

        assert to_compact == [], "nothing overflowed, so there is nothing to compact"
        assert valid is True, "no compaction warranted — this is not the blocked path"
        assert orphan in to_keep, \
            "the scan is skipped, so the orphan stays; the send-side gate owns it"
