"""Tests for SSEBatcher — server-side token/thinking event coalescing."""

import time

from chaos_agent.agent.streaming import SSEBatcher, StreamEvent


class TestSSEBatcherDisabled:
    """When flush_interval_ms <= 0, events pass through unchanged."""

    def test_disabled_passes_through(self):
        batcher = SSEBatcher(flush_interval_ms=0)
        evt = StreamEvent(type="token", content="hello")
        result = batcher.feed(evt)
        assert len(result) == 1
        assert '"token"' in result[0]
        assert "hello" in result[0]

    def test_disabled_flush_empty(self):
        batcher = SSEBatcher(flush_interval_ms=0)
        assert batcher.flush() == []

    def test_disabled_structural_passes_through(self):
        batcher = SSEBatcher(flush_interval_ms=0)
        evt = StreamEvent(type="tool_start", tool_name="kubectl")
        result = batcher.feed(evt)
        assert len(result) == 1
        assert "kubectl" in result[0]


class TestSSEBatcherBuffering:
    """Token/thinking events are buffered until flush conditions are met."""

    def test_single_token_buffered(self):
        batcher = SSEBatcher(flush_interval_ms=100, flush_chars=100)
        result = batcher.feed(StreamEvent(type="token", content="hi"))
        assert result == []

    def test_buffered_tokens_flushed_on_structural(self):
        batcher = SSEBatcher(flush_interval_ms=100, flush_chars=100)
        batcher.feed(StreamEvent(type="token", content="hello"))
        batcher.feed(StreamEvent(type="token", content=" world"))
        result = batcher.feed(StreamEvent(type="tool_start", tool_name="k"))
        # Should yield: merged token + structural event
        assert len(result) == 2
        assert "hello world" in result[0]
        assert "tool_start" in result[1]

    def test_thinking_buffered_separately(self):
        batcher = SSEBatcher(flush_interval_ms=100, flush_chars=200)
        batcher.feed(StreamEvent(type="token", content="tok"))
        batcher.feed(StreamEvent(type="thinking", content="think"))
        result = batcher.flush()
        # Token and thinking flushed as separate events
        assert len(result) == 2
        assert "tok" in result[0]
        assert "think" in result[1]

    def test_node_preserved_last(self):
        batcher = SSEBatcher(flush_interval_ms=100, flush_chars=200)
        batcher.feed(StreamEvent(type="token", content="a", node="node1"))
        batcher.feed(StreamEvent(type="token", content="b", node="node2"))
        result = batcher.flush()
        assert len(result) == 1
        assert "node2" in result[0]


class TestSSEBatcherSizeFlush:
    """Chars threshold triggers immediate flush."""

    def test_chars_threshold_triggers_flush(self):
        batcher = SSEBatcher(flush_interval_ms=1000, flush_chars=10)
        # First 9 chars — no flush
        result1 = batcher.feed(StreamEvent(type="token", content="123456789"))
        assert result1 == []
        # 10th char crosses threshold
        result2 = batcher.feed(StreamEvent(type="token", content="0"))
        assert len(result2) == 1
        assert "1234567890" in result2[0]

    def test_chars_threshold_includes_thinking(self):
        batcher = SSEBatcher(flush_interval_ms=1000, flush_chars=10)
        batcher.feed(StreamEvent(type="token", content="12345"))
        # 5 thinking chars brings total to 10
        result = batcher.feed(StreamEvent(type="thinking", content="abcde"))
        assert len(result) == 2  # token + thinking flushed separately


class TestSSEBatcherTimeFlush:
    """Time-based flush via deadline check on next feed()."""

    def test_deadline_flush_on_next_feed(self):
        batcher = SSEBatcher(flush_interval_ms=50, flush_chars=1000)
        batcher.feed(StreamEvent(type="token", content="old"))
        # Simulate time passing beyond deadline
        batcher._batch_start = time.monotonic() - 0.1
        # Next feed triggers deadline flush of old content first
        result = batcher.feed(StreamEvent(type="token", content="new"))
        assert len(result) == 1
        assert "old" in result[0]
        # "new" is now buffered
        final = batcher.flush()
        assert len(final) == 1
        assert "new" in final[0]

    def test_deadline_not_exceeded_keeps_buffering(self):
        batcher = SSEBatcher(flush_interval_ms=5000, flush_chars=1000)
        batcher.feed(StreamEvent(type="token", content="a"))
        # batch_start is fresh, 5s is far away
        result = batcher.feed(StreamEvent(type="token", content="b"))
        assert result == []
        final = batcher.flush()
        assert "ab" in final[0]


class TestSSEBatcherTaskId:
    """task_id is preserved on flushed batched events."""

    def test_task_id_preserved_on_flush(self):
        batcher = SSEBatcher(flush_interval_ms=100, flush_chars=200)
        batcher.feed(StreamEvent(type="token", content="hi", task_id="task-abc"))
        result = batcher.flush()
        assert len(result) == 1
        assert "task-abc" in result[0]

    def test_task_id_updated_from_latest_event(self):
        batcher = SSEBatcher(flush_interval_ms=100, flush_chars=200)
        batcher.feed(StreamEvent(type="token", content="a", task_id="t1"))
        batcher.feed(StreamEvent(type="token", content="b", task_id="t2"))
        result = batcher.flush()
        assert "t2" in result[0]


class TestSSEBatcherEmptyStream:
    """Edge cases: empty content, no events, only structural."""

    def test_flush_with_nothing_buffered(self):
        batcher = SSEBatcher(flush_interval_ms=100, flush_chars=100)
        assert batcher.flush() == []

    def test_only_structural_events(self):
        batcher = SSEBatcher(flush_interval_ms=100, flush_chars=100)
        evts = [
            StreamEvent(type="node_start", node="agent_loop"),
            StreamEvent(type="tool_start", tool_name="kubectl"),
            StreamEvent(type="tool_end", tool_name="kubectl", content="ok"),
            StreamEvent(type="node_end", node="agent_loop"),
        ]
        all_results = []
        for e in evts:
            all_results.extend(batcher.feed(e))
        all_results.extend(batcher.flush())
        # Each structural event passes through immediately
        assert len(all_results) == 4
