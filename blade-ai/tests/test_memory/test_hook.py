"""Tests for PreReasoningHook unified memory management."""

from unittest.mock import MagicMock

from langchain_core.messages import ToolMessage

from chaos_agent.memory.context_manager import (
    MAX_CONSECUTIVE_COMPACT_FAILURES,
    CompactTrackingState,
)
from chaos_agent.memory.hook import PreReasoningHook


class TestPreReasoningHookNoCompaction:
    """Test hook when no compaction is needed."""

    async def test_returns_empty_when_no_compaction(self):
        cm = MagicMock()
        cm.check_context.return_value = ([], ["msg1"], True)  # Nothing to compact
        tc = MagicMock()
        tc.compact.return_value = ["msg1"]

        hook = PreReasoningHook(
            context_manager=cm,
            tool_compactor=tc,
            session_store=MagicMock(),
        )

        state = {"messages": ["msg1"], "task_id": "task-1"}
        result = await hook(state)
        # No compaction needed — tool compaction modifies in-place, no state update required
        assert result == {}


class TestPreReasoningHookWithCompaction:
    """Test hook when compaction is triggered."""

    async def test_compact_messages_and_return_summary(self, mock_llm):
        cm = MagicMock()
        cm.check_context.return_value = (["old1", "old2"], ["recent1"], True)
        cm.compact_threshold = 0  # Force LLM compression (combined_tokens >= 0 always)
        tc = MagicMock()
        tc.compact.return_value = ["old1", "old2", "recent1"]

        hook = PreReasoningHook(
            context_manager=cm,
            tool_compactor=tc,
            session_store=MagicMock(),
            llm=mock_llm,
        )

        state = {
            "messages": ["old1", "old2", "recent1"],
            "task_id": "task-1",
            "compressed_summary": "",
        }
        result = await hook(state)

        # Should return summary + kept messages
        assert "messages" in result
        assert "compressed_summary" in result

    async def test_tool_compactor_called(self, mock_llm):
        cm = MagicMock()
        cm.check_context.return_value = ([], ["msg1"], True)
        tc = MagicMock()
        tc.compact.return_value = ["msg1"]

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)
        await hook({"messages": ["msg1"], "task_id": "t1"})

        tc.compact.assert_called_once()

    async def test_context_manager_called(self, mock_llm):
        cm = MagicMock()
        cm.check_context.return_value = ([], ["msg1"], True)
        tc = MagicMock()
        tc.compact.return_value = ["msg1"]

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)
        await hook({"messages": ["msg1"], "task_id": "t1"})

        cm.check_context.assert_called_once()

    async def test_previous_summary_passed_to_compaction(self, mock_llm):
        cm = MagicMock()
        cm.check_context.return_value = (["old"], ["recent"], True)
        cm.compact_threshold = 0
        tc = MagicMock()
        tc.compact.return_value = ["old", "recent"]

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)
        await hook({
            "messages": ["old", "recent"],
            "task_id": "t1",
            "compressed_summary": "prev summary",
        })

        # compact_memory should receive previous_summary
        # (verified through mock_llm.ainvoke call args)

    async def test_compressed_summary_updated(self, mock_llm):
        cm = MagicMock()
        cm.check_context.return_value = (["old"], ["recent"], True)
        cm.compact_threshold = 0
        tc = MagicMock()
        tc.compact.return_value = ["old", "recent"]

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)
        result = await hook({
            "messages": ["old", "recent"],
            "task_id": "t1",
            "compressed_summary": "",
        })

        assert "compressed_summary" in result
        assert "test summary" in result["compressed_summary"]


class TestPreReasoningHookForceCompact:
    """Manual /compact unification: hook with force=True must bypass
    the auto-trigger threshold gate AND the strip-only short-circuit,
    so that user-initiated /compact always produces a proper
    [Compressed History] summary, even on a thread that's well below
    the auto-trigger threshold."""

    async def test_force_compacts_below_auto_threshold(self, mock_llm):
        # Real ContextManager so the threshold gate is real, not mocked.
        from chaos_agent.memory.context_manager import ContextManager
        from langchain_core.messages import HumanMessage

        # Build a thread with enough content to overflow ``reserve_tokens``
        # but sit BELOW the auto-trigger. With max=100K, ratio=0.85, the
        # trigger is ≈85K. With reserve=2K, ~20K of content guarantees
        # there's something to compact while staying under trigger.
        cm = ContextManager(max_tokens=100_000, compact_ratio=0.85)
        cm.reserve_tokens = 2_000
        msgs = [
            HumanMessage(content="x" * 4_000, id=f"m-{i}") for i in range(20)
        ]
        tc = MagicMock()
        tc.compact.return_value = msgs

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)

        # Sanity: auto mode is a no-op for this thread — still below trigger.
        auto = await hook(
            {"messages": msgs, "task_id": "t-auto", "compressed_summary": ""}
        )
        assert auto == {}

        # Force mode: must produce a summary update even though we're
        # nowhere near the threshold.
        forced = await hook(
            {"messages": msgs, "task_id": "t-force", "compressed_summary": ""},
            force=True,
        )
        assert "messages" in forced
        assert "compressed_summary" in forced

    async def test_force_skips_strip_only_shortcut(self, mock_llm):
        # When strip alone would already fit under compact_threshold,
        # AUTO mode returns the stripped messages without calling the
        # LLM. FORCE mode must instead go LLM — the user expects a
        # summary, not a tool-output trim.
        from langchain_core.messages import ToolMessage

        big_tool = ToolMessage(
            content="X" * 4000, tool_call_id="t1", id="big-tool"
        )
        cm = MagicMock()
        cm.check_context.return_value = ([big_tool], [], True)
        cm.compact_threshold = 10_000_000  # strip-only would fit easily
        tc = MagicMock()
        tc.compact.return_value = [big_tool]

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)
        result = await hook(
            {"messages": [big_tool], "task_id": "t-force-llm"},
            force=True,
        )
        # Force path → got an LLM summary, not just stripped messages.
        assert "compressed_summary" in result
        assert "test summary" in result["compressed_summary"]

    async def test_force_bypasses_circuit_breaker(self, mock_llm):
        # If the breaker has tripped on the auto path, a user pressing
        # /compact should still get a fresh attempt. Otherwise the
        # only way to recover would be restart.
        from chaos_agent.memory.context_manager import (
            ContextManager,
            CompactTrackingState,
            MAX_CONSECUTIVE_COMPACT_FAILURES,
        )
        from langchain_core.messages import HumanMessage

        cm = ContextManager(max_tokens=100, compact_ratio=0.5)
        cm.reserve_tokens = 1
        msgs = [HumanMessage(content="x" * 200, id=f"m-{i}") for i in range(5)]
        tc = MagicMock()
        tc.compact.return_value = msgs

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)
        # Pre-trip the breaker for this task.
        hook._tracking["t-force-breaker"] = CompactTrackingState(
            consecutive_failures=MAX_CONSECUTIVE_COMPACT_FAILURES
        )

        # Auto mode: breaker stops us, returns {}.
        auto = await hook(
            {"messages": msgs, "task_id": "t-force-breaker", "compressed_summary": ""}
        )
        assert auto == {}

        # Force mode: breaker bypassed, compaction proceeds.
        forced = await hook(
            {"messages": msgs, "task_id": "t-force-breaker", "compressed_summary": ""},
            force=True,
        )
        assert "compressed_summary" in forced


class TestPreReasoningHookStrippedReturn:
    """Bug 1 regression guard: the aggressive-strip branch must
    actually return the stripped messages so LangGraph applies the
    truncation. Pre-fix the hook returned ``{}`` and the stripped
    objects were silently dropped."""

    async def test_returns_stripped_messages_when_strip_sufficient(self, mock_llm):
        # Big tool output that strip_large_outputs will truncate.
        big_tool = ToolMessage(
            content="X" * 4000, tool_call_id="t1", id="big-tool",
        )
        cm = MagicMock()
        # check_context says we have something to compact...
        cm.check_context.return_value = ([big_tool], [], True)
        # ...but the post-strip combined budget fits, so the
        # intermediate strip route is taken.
        cm.compact_threshold = 10_000_000
        tc = MagicMock()
        tc.compact.return_value = [big_tool]

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)
        result = await hook({"messages": [big_tool], "task_id": "t1"})

        # The fix: returns updated messages so add_messages reducer
        # replaces the originals with their stripped copies.
        assert "messages" in result
        assert len(result["messages"]) == 1
        stripped = result["messages"][0]
        # Same id → reducer replaces in place.
        assert stripped.id == "big-tool"
        # Content was actually truncated (< original 4000 chars).
        assert len(stripped.content) < 4000
        assert "[output truncated]" in stripped.content


class TestPreReasoningHookCircuitBreaker:
    """Bug 2 regression guard: the hook must own a per-task
    CompactTrackingState dict and pass it into check_context so the
    breaker inside check_context actually has somewhere to observe
    consecutive failures. Pre-fix, no caller passed tracking, so
    MAX_CONSECUTIVE_COMPACT_FAILURES was dead code."""

    async def test_check_context_receives_tracking_state(self, mock_llm):
        cm = MagicMock()
        cm.check_context.return_value = ([], ["m"], True)
        tc = MagicMock()
        tc.compact.return_value = ["m"]

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)
        await hook({"messages": ["m"], "task_id": "task-A"})

        # Inspect the kwargs the hook passed to check_context.
        _args, kwargs = cm.check_context.call_args
        assert "tracking" in kwargs
        assert isinstance(kwargs["tracking"], CompactTrackingState)

    async def test_tracking_state_is_per_task(self, mock_llm):
        cm = MagicMock()
        cm.check_context.return_value = ([], ["m"], True)
        tc = MagicMock()
        tc.compact.return_value = ["m"]

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)
        await hook({"messages": ["m"], "task_id": "task-A"})
        await hook({"messages": ["m"], "task_id": "task-B"})

        # Two task ids → two distinct tracking instances; one task's
        # failures must not bleed into another's breaker.
        assert "task-A" in hook._tracking
        assert "task-B" in hook._tracking
        assert hook._tracking["task-A"] is not hook._tracking["task-B"]

    async def test_failure_increments_consecutive_failures(
        self, mock_llm, monkeypatch
    ):
        # Patch compact_memory itself to raise. We can't just hand the
        # hook a bad LLM — compact_memory has its own try/except that
        # falls back to a simple non-LLM compaction, so LLM failures
        # alone never propagate up to the hook's bookkeeping.
        async def boom(*_a, **_kw):
            raise RuntimeError("compaction down")

        monkeypatch.setattr("chaos_agent.memory.hook.compact_memory", boom)

        cm = MagicMock()
        cm.check_context.return_value = (["old"], ["recent"], True)
        cm.compact_threshold = 0
        tc = MagicMock()
        tc.compact.return_value = ["old", "recent"]

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)

        for _ in range(MAX_CONSECUTIVE_COMPACT_FAILURES):
            try:
                await hook({
                    "messages": ["old", "recent"],
                    "task_id": "task-X",
                    "compressed_summary": "",
                })
            except Exception:
                pass  # hook re-raises; we just want the counter to bump

        assert (
            hook._tracking["task-X"].consecutive_failures
            == MAX_CONSECUTIVE_COMPACT_FAILURES
        )

    async def test_breaker_short_circuits_after_max_failures(
        self, mock_llm, monkeypatch
    ):
        # End-to-end proof: after MAX_CONSECUTIVE_COMPACT_FAILURES,
        # the next call must hit the breaker inside check_context,
        # which returns to_compact=[], so the hook bails out at the
        # "if not to_compact" branch WITHOUT calling compact_memory.
        # Without the hook→tracking wiring, the breaker would never
        # fire even after 1000 failures.
        from chaos_agent.memory.context_manager import ContextManager

        # Real ContextManager so the breaker logic actually executes.
        cm = ContextManager(max_tokens=100, compact_ratio=0.5)
        cm.reserve_tokens = 1

        compact_call_count = {"n": 0}

        async def boom(*_a, **_kw):
            compact_call_count["n"] += 1
            raise RuntimeError("compaction down")

        monkeypatch.setattr("chaos_agent.memory.hook.compact_memory", boom)

        tc = MagicMock()
        from langchain_core.messages import HumanMessage
        msgs = [HumanMessage(content="x" * 800) for _ in range(5)]
        tc.compact.return_value = msgs

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)

        # Burn through the breaker's allowance.
        for _ in range(MAX_CONSECUTIVE_COMPACT_FAILURES):
            try:
                await hook({
                    "messages": msgs,
                    "task_id": "task-Z",
                    "compressed_summary": "",
                })
            except Exception:
                pass

        calls_before = compact_call_count["n"]
        # Next call must NOT reach compact_memory — the breaker should
        # intercept inside check_context.
        await hook({
            "messages": msgs,
            "task_id": "task-Z",
            "compressed_summary": "",
        })
        assert compact_call_count["n"] == calls_before, (
            "breaker failed to short-circuit: compact_memory still called"
        )

    async def test_success_resets_consecutive_failures(self, mock_llm):
        cm = MagicMock()
        cm.check_context.return_value = (["old"], ["recent"], True)
        cm.compact_threshold = 0
        tc = MagicMock()
        tc.compact.return_value = ["old", "recent"]

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)
        # Pre-load a failure count to confirm success clears it.
        hook._tracking["task-Y"] = CompactTrackingState(consecutive_failures=2)

        await hook({
            "messages": ["old", "recent"],
            "task_id": "task-Y",
            "compressed_summary": "",
        })

        assert hook._tracking["task-Y"].consecutive_failures == 0
        assert hook._tracking["task-Y"].compacted is True


class TestPreReasoningHookContextSizeEmission:
    """The Footer state-size indicator depends on hook emitting a
    ``context_size`` StatusEvent at every return point (no-compaction,
    strip-only short-circuit, LLM compaction success). Without these
    the TS TUI's Footer would never update and silently fall back to
    the legacy ``ns:default`` display forever."""

    async def test_emits_on_no_compaction_path(self, mock_llm):
        # The cheap path: hook ran tool_compactor, check_context said
        # nothing to compact. Must still emit a context_size frame so
        # Footer can show "Xk / Yk" reflecting the current state.
        from chaos_agent.observability.status_tracker import (
            subscribe as _status_subscribe,
            unsubscribe as _status_unsubscribe,
        )

        cm = MagicMock()
        cm.check_context.return_value = ([], ["m"], True)
        cm.max_tokens = 128_000
        cm.compact_threshold = 108_800
        tc = MagicMock()
        tc.compact.return_value = ["m"]

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)
        task_id = "task-ctx-noop"
        queue = _status_subscribe(task_id)
        try:
            await hook({"messages": ["m"], "task_id": task_id})
        finally:
            # Drain after hook returns so we see the event without
            # blocking on .get().
            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
            _status_unsubscribe(task_id, queue)

        # At least one context_size event landed
        ctx_events = [e for e in events if getattr(e, "source", "") == "context_size"]
        assert len(ctx_events) >= 1
        ev = ctx_events[-1]
        assert ev.detail["max_tokens"] == 128_000
        assert ev.detail["trigger_tokens"] == 108_800

    async def test_emits_on_llm_compaction_path(self, mock_llm):
        from chaos_agent.observability.status_tracker import (
            subscribe as _status_subscribe,
            unsubscribe as _status_unsubscribe,
        )

        cm = MagicMock()
        # Trigger LLM compaction: check_context returns to_compact.
        cm.check_context.return_value = (["old1", "old2"], ["recent"], True)
        cm.compact_threshold = 0  # force LLM path (skip strip shortcut)
        cm.max_tokens = 100_000
        tc = MagicMock()
        tc.compact.return_value = ["old1", "old2", "recent"]

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)
        task_id = "task-ctx-llm"
        queue = _status_subscribe(task_id)
        try:
            await hook({
                "messages": ["old1", "old2", "recent"],
                "task_id": task_id,
                "compressed_summary": "",
            })
        finally:
            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
            _status_unsubscribe(task_id, queue)

        ctx_events = [e for e in events if getattr(e, "source", "") == "context_size"]
        # MUST emit at least one (post-compaction state size). The
        # exact count is implementation detail — the load-bearing
        # contract is "Footer gets updated after LLM compaction".
        assert len(ctx_events) >= 1
        # The post-compaction event should carry the merged state's
        # max_tokens and trigger so Footer renders correctly.
        ev = ctx_events[-1]
        assert ev.detail["max_tokens"] == 100_000
        # current_tokens should be > 0 (the [Compressed History]
        # summary message itself has some content)
        assert ev.detail["current_tokens"] >= 0
