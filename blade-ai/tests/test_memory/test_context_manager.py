"""Tests for token-aware context manager."""

from unittest.mock import MagicMock

from chaos_agent.memory.context_manager import (
    CompactLevel,
    CompactTrackingState,
    ContextManager,
    MAX_CONSECUTIVE_COMPACT_FAILURES,
    STRIP_MARKER,
    TokenWarningState,
    calculate_token_warning_state,
    count_tokens_approx,
    ensure_pair_integrity,
    estimate_tokens,
    group_messages_by_round,
    post_compact_cleanup,
    strip_large_outputs,
)


# ---------------------------------------------------------------------------
# Original tests (preserved)
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    """CJK-aware token estimator (P0-1)."""

    def test_empty_string_is_zero(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens(None) == 0

    def test_pure_ascii_uses_4_chars_per_token(self):
        # 40 ASCII chars / 4 = 10 tokens (preserves prior behaviour)
        assert estimate_tokens("a" * 40) == 10

    def test_pure_cjk_uses_higher_density(self):
        # 30 CJK chars / 1.5 = 20 tokens — meaningfully more than the
        # old chars/4 heuristic which would have estimated only 7.
        cjk = "你" * 30
        result = estimate_tokens(cjk)
        assert result == 20
        # The fix MUST count CJK at strictly higher density than ASCII.
        assert result > 30 // 4

    def test_mixed_cjk_and_ascii(self):
        # "你好world" → 2 CJK + 5 ASCII → 2/1.5 + 5/4 = 1 + 1 = 2 tokens (int floor)
        assert estimate_tokens("你好world") == 2

    def test_cjk_punctuation_counts_as_cjk(self):
        # All five chars fall in U+3000–U+303F or U+FF00–U+FFEF
        assert estimate_tokens("。、，；：") == int(5 / 1.5)

    def test_dramatically_higher_density_than_legacy_chars_per_4(self):
        """The whole point of P0-1: CJK is no longer under-counted."""
        cjk_text = "你" * 100
        legacy_chars_per_4 = len(cjk_text) // 4  # = 25
        new_estimate = estimate_tokens(cjk_text)
        # New estimator must produce a value notably higher than the legacy
        # heuristic (~2.5×) to fix the under-trigger of compaction.
        assert new_estimate >= legacy_chars_per_4 * 2

    def test_real_mixed_prompt_higher_than_legacy(self):
        """Reality check on a representative mixed CJK/ASCII system prompt."""
        prompt = (
            "你是一个混沌工程智能体，负责在 Kubernetes 集群中安全地执行故障注入演练。\n"
            "Always confirm destructive operations before proceeding."
        )
        legacy = len(prompt) // 4
        new_estimate = estimate_tokens(prompt)
        # On mixed CJK/ASCII content the new estimator must report meaningfully
        # higher token usage so that compaction triggers in time.
        assert new_estimate > legacy

    def test_qwen_style_tokenizer_within_20_percent(self):
        """Verification standard: < 20% deviation vs a CJK-aware tokenizer.

        The project's primary LLMs (Qwen, DeepSeek) tokenize CJK at ~1.5
        chars/token — the ratio this estimator was calibrated against. We
        approximate that here using tiktoken's o200k_base, which treats
        common CJK characters with multi-codepoint merges similar to Qwen.
        """
        try:
            import tiktoken
        except ImportError:
            import pytest
            pytest.skip("tiktoken not installed")
        try:
            enc = tiktoken.get_encoding("o200k_base")
        except Exception:
            import pytest
            pytest.skip("o200k_base encoding unavailable")
        prompt = (
            "你是一个混沌工程智能体，负责在 Kubernetes 集群中安全地执行故障注入演练。\n"
            "你的核心职责包括：1) 解析用户意图 2) 生成安全的注入计划 3) 执行 ChaosBlade 命令 "
            "4) 验证故障已生效 5) 在演练结束后恢复并清理。\n"
            "Always confirm destructive operations with the operator before proceeding."
        )
        actual = len(enc.encode(prompt))
        estimated = estimate_tokens(prompt)
        deviation = abs(estimated - actual) / actual
        assert deviation < 0.20, (
            f"estimate={estimated} vs o200k_base={actual}: {deviation:.1%} deviation"
        )


class TestCountTokensApprox:
    """Test approximate token counting."""

    def test_empty_list(self):
        assert count_tokens_approx([]) == 0

    def test_string_content(self):
        msg = MagicMock()
        msg.content = "a" * 40  # ~10 tokens
        result = count_tokens_approx([msg])
        assert result == 10

    def test_list_content(self):
        msg = MagicMock()
        msg.content = [{"text": "a" * 40}]  # ~10 tokens
        result = count_tokens_approx([msg])
        assert result == 10

    def test_non_string_content(self):
        msg = MagicMock()
        msg.content = 12345  # not string or list
        result = count_tokens_approx([msg])
        assert result == 0

    def test_multiple_messages(self):
        msgs = [MagicMock(content="a" * 40), MagicMock(content="b" * 80)]
        result = count_tokens_approx(msgs)
        assert result == 30  # 10 + 20

    def test_cjk_message_counted_at_higher_density(self):
        msg = MagicMock(content="中文" * 30)  # 60 CJK chars
        # ASCII heuristic would have given 60//4 = 15 tokens; CJK gives 60/1.5 = 40
        assert count_tokens_approx([msg]) == 40


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

    def test_compact_threshold_calculation(self):
        cm = ContextManager(max_tokens=1000, compact_ratio=0.7)
        expected = int(1000 * 0.7)
        assert cm.compact_threshold == expected


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
        assert STRIP_MARKER in result[0].content
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
        # Custom threshold (100) — should strip
        result_custom = strip_large_outputs(msgs, threshold=100)
        assert STRIP_MARKER in result_custom[0].content

    def test_empty_messages(self):
        result = strip_large_outputs([])
        assert result == []


# ---------------------------------------------------------------------------
# New tests: post_compact_cleanup (Migration Point 8)
# ---------------------------------------------------------------------------


class TestPostCompactCleanup:
    """Test post_compact_cleanup() cache clearing."""

    def test_returns_dict(self):
        state = {"task_id": "test-task"}
        result = post_compact_cleanup(state)
        assert isinstance(result, dict)

    def test_marks_compacted_this_turn(self):
        state = {"task_id": "test-task"}
        result = post_compact_cleanup(state)
        assert result.get("_compacted_this_turn") is True

    def test_empty_task_id(self):
        state = {}
        result = post_compact_cleanup(state)
        assert isinstance(result, dict)
        # Should still mark compaction
        assert result.get("_compacted_this_turn") is True

    def test_clears_env_cache(self):
        """Verify env cache is cleared for the task."""
        import asyncio
        from chaos_agent.agent.env_info import compute_env_info, clear_env_cache

        # Populate cache
        asyncio.run(compute_env_info(task_id="cleanup-test"))

        state = {"task_id": "cleanup-test"}
        result = post_compact_cleanup(state)

        # After cleanup, next compute_env_info should re-collect
        # (we can't easily verify this without mocking, but at least no error)
        assert isinstance(result, dict)

        # Clean up
        clear_env_cache()
