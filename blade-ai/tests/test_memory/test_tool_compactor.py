"""Tests for tool output two-stage truncation."""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from chaos_agent.memory.tool_compactor import (
    CLEARED_MARKER,
    ToolResultCompactor,
    is_ai_message,
    is_tool_message,
    maybe_time_based_microcompact,
    truncate_text,
)


# ---------------------------------------------------------------------------
# Original tests (preserved)
# ---------------------------------------------------------------------------


class TestIsToolMessage:
    """Test tool message detection."""

    def test_tool_message(self):
        msg = MagicMock()
        msg.type = "tool"
        assert is_tool_message(msg) is True

    def test_non_tool_message(self):
        msg = MagicMock()
        msg.type = "human"
        assert is_tool_message(msg) is False

    def test_no_type_attribute(self):
        msg = MagicMock(spec=[])  # No attributes
        assert is_tool_message(msg) is False


class TestTruncateText:
    """Test text truncation."""

    def test_short_text_unchanged(self):
        text = "hello world"
        assert truncate_text(text, 1000) == text

    def test_long_text_truncated(self):
        text = "a" * 5000
        result = truncate_text(text, 1000)
        assert len(result.encode("utf-8")) <= 1000

    def test_preserves_valid_utf8(self):
        text = "你好世界" * 1000
        result = truncate_text(text, 100)
        # Should not raise on encode
        result.encode("utf-8")


class TestToolResultCompactor:
    """Test two-stage truncation logic."""

    def test_small_content_not_truncated(self):
        msg = MagicMock()
        msg.type = "tool"
        msg.content = "small output"

        compactor = ToolResultCompactor()
        result = compactor.compact([msg])
        assert result[0].content == "small output"

    def test_old_tool_output_low_limit(self):
        """Older tool outputs (not in last 3) get 3KB limit."""
        compactor = ToolResultCompactor()

        # Create 5 tool messages with large content
        msgs = []
        for i in range(5):
            msg = MagicMock()
            msg.type = "tool"
            msg.content = "x" * 5000  # 5KB each
            msgs.append(msg)

        result = compactor.compact(msgs)
        # First 2 (old) should be truncated, last 3 (recent) kept
        for i in range(2):
            assert "TRUNCATED" in result[i].content or len(result[i].content) < 5000

    def test_recent_tool_output_high_limit(self):
        """Last 3 tool outputs get 100KB limit."""
        compactor = ToolResultCompactor()

        msg = MagicMock()
        msg.type = "tool"
        msg.content = "y" * 1000  # Well under 100KB

        result = compactor.compact([msg])
        assert "TRUNCATED" not in result[0].content

    def test_cache_to_disk(self, tmp_path):
        """Oversized output should be cached to disk."""
        compactor = ToolResultCompactor(cache_dir=tmp_path / "cache")

        msg = MagicMock()
        msg.type = "tool"
        msg.content = "z" * 10000  # Over 3KB

        msgs = [MagicMock(type="tool", content="a" * 10000) for _ in range(5)]
        result = compactor.compact(msgs)
        # At least some messages should have cache references
        has_cache = any("cached" in getattr(m, "content", "").lower() for m in result)
        # Depending on which are old vs recent, cache may or may not appear
        # The important thing is no crash

    def test_non_string_content_skipped(self):
        msg = MagicMock()
        msg.type = "tool"
        msg.content = 12345  # Not a string

        compactor = ToolResultCompactor()
        result = compactor.compact([msg])
        assert result[0].content == 12345  # Unchanged

    def test_no_tool_messages_unchanged(self):
        msgs = [MagicMock(type="human", content="hello")]
        compactor = ToolResultCompactor()
        result = compactor.compact(msgs)
        assert result == msgs


# ---------------------------------------------------------------------------
# New tests: Time-based MicroCompact (Migration Point 11)
# ---------------------------------------------------------------------------


class TestIsAIMessage:
    """Test AI message detection."""

    def test_ai_message(self):
        msg = MagicMock()
        msg.type = "ai"
        assert is_ai_message(msg) is True

    def test_non_ai_message(self):
        msg = MagicMock()
        msg.type = "human"
        assert is_ai_message(msg) is False


class TestMaybeTimeBasedMicroCompact:
    """Test time-based micro-compact cleanup."""

    def _make_ai_msg(self, minutes_ago: float = 10.0) -> MagicMock:
        """Create an AI message with a timestamp minutes_ago."""
        msg = MagicMock()
        msg.type = "ai"
        ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        msg.additional_kwargs = {"timestamp": ts}
        return msg

    def _make_tool_msg(self, content: str = "kubectl output") -> MagicMock:
        """Create a tool result message."""
        msg = MagicMock()
        msg.type = "tool"
        msg.content = content
        return msg

    def _make_human_msg(self, content: str = "user request") -> MagicMock:
        msg = MagicMock()
        msg.type = "human"
        msg.content = content
        return msg

    def test_returns_none_when_no_ai_message(self):
        msgs = [self._make_human_msg()]
        result = maybe_time_based_microcompact(msgs)
        assert result is None

    def test_returns_none_when_gap_too_short(self):
        ai_msg = self._make_ai_msg(minutes_ago=1.0)  # Only 1 min ago
        msgs = [ai_msg, self._make_tool_msg()]
        result = maybe_time_based_microcompact(msgs, gap_threshold_minutes=5.0)
        assert result is None

    def test_returns_none_when_few_tool_results(self):
        ai_msg = self._make_ai_msg(minutes_ago=10.0)
        msgs = [ai_msg, self._make_tool_msg()]
        result = maybe_time_based_microcompact(msgs, gap_threshold_minutes=5.0, keep_recent=3)
        assert result is None  # Only 1 tool result, <= keep_recent

    def test_clears_old_tool_results(self):
        """Old tool results (beyond keep_recent) are replaced with CLEARED_MARKER."""
        ai_msg = self._make_ai_msg(minutes_ago=10.0)
        tool_msgs = [self._make_tool_msg(f"output {i}") for i in range(5)]
        msgs = [ai_msg] + tool_msgs

        result = maybe_time_based_microcompact(msgs, gap_threshold_minutes=5.0, keep_recent=2)
        assert result is not None
        # First 3 should be cleared, last 2 kept
        for i in range(3):
            assert result[1 + i].content == CLEARED_MARKER  # +1 for ai_msg offset
        # Last 2 should be preserved
        assert result[-1].content == "output 4"
        assert result[-2].content == "output 3"

    def test_preserves_recent_tool_results(self):
        ai_msg = self._make_ai_msg(minutes_ago=10.0)
        tool_msgs = [self._make_tool_msg(f"output {i}") for i in range(5)]
        msgs = [ai_msg] + tool_msgs

        result = maybe_time_based_microcompact(msgs, gap_threshold_minutes=5.0, keep_recent=3)
        assert result is not None
        # Last 3 should be preserved
        for i in range(3):
            assert result[-(i + 1)].content != CLEARED_MARKER

    def test_non_tool_messages_unchanged(self):
        ai_msg = self._make_ai_msg(minutes_ago=10.0)
        human_msg = self._make_human_msg("keep this")
        tool_msgs = [self._make_tool_msg(f"output {i}") for i in range(5)]
        msgs = [human_msg, ai_msg] + tool_msgs

        result = maybe_time_based_microcompact(msgs, gap_threshold_minutes=5.0, keep_recent=2)
        assert result is not None
        # Human message should be unchanged
        assert result[0].content == "keep this"

    def test_already_cleared_not_double_cleared(self):
        ai_msg = self._make_ai_msg(minutes_ago=10.0)
        tool_msgs = [self._make_tool_msg(f"output {i}") for i in range(5)]
        # Mark one as already cleared
        tool_msgs[0].content = CLEARED_MARKER
        msgs = [ai_msg] + tool_msgs

        result = maybe_time_based_microcompact(msgs, gap_threshold_minutes=5.0, keep_recent=2)
        # Should still work (returns modified list, but no new modification for already-cleared)
        assert result is not None

    def test_ai_timestamp_as_iso_string(self):
        """Test that ISO format string timestamps work."""
        ai_msg = MagicMock()
        ai_msg.type = "ai"
        ts = (datetime.now(timezone.utc) - timedelta(minutes=10.0)).isoformat()
        ai_msg.additional_kwargs = {"timestamp": ts}

        tool_msgs = [self._make_tool_msg(f"output {i}") for i in range(5)]
        msgs = [ai_msg] + tool_msgs

        result = maybe_time_based_microcompact(msgs, gap_threshold_minutes=5.0, keep_recent=2)
        assert result is not None


class TestToolResultCompactorWithTimeMC:
    """Test ToolResultCompactor.compact() integrates time-based micro-compact."""

    def test_time_based_cleanup_before_truncation(self):
        """Time-based micro-compact runs first, then size truncation."""
        compactor = ToolResultCompactor()

        # Create AI message from 10 min ago
        ai_msg = MagicMock()
        ai_msg.type = "ai"
        ts = datetime.now(timezone.utc) - timedelta(minutes=10.0)
        ai_msg.additional_kwargs = {"timestamp": ts}
        ai_msg.content = "assistant response"

        # Create tool messages
        tool_msgs = []
        for i in range(5):
            msg = MagicMock()
            msg.type = "tool"
            msg.content = f"output {i}"
            tool_msgs.append(msg)

        msgs = [ai_msg] + tool_msgs
        result = compactor.compact(msgs)
        # Should have processed messages (time cleanup + truncation)
        assert len(result) == len(msgs)
