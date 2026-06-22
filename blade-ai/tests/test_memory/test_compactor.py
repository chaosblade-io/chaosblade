"""Tests for LLM-based structured compaction."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from chaos_agent.memory.compactor import (
    _prepare_compaction_messages,
    _simple_compact,
    build_post_compact_context_message,
    compact_memory,
    COMPACTION_PROMPT,
    CompactionMode,
    extract_critical_context,
    format_compact_summary,
    NO_TOOLS_PREAMBLE,
    NO_TOOLS_TRAILER,
    POST_COMPACT_SKILLS_TOKEN_BUDGET,
    SKILL_TRUNCATION_MARKER,
    truncate_to_tokens,
)


# ---------------------------------------------------------------------------
# Original tests (preserved for backward compatibility)
# ---------------------------------------------------------------------------


class TestCompactMemoryWithLLM:
    """Test compaction with a mock LLM."""

    async def test_calls_llm_ainvoke(self, mock_llm):
        msgs = [MagicMock(content="test message")]
        result = await compact_memory(msgs, llm=mock_llm)
        mock_llm.ainvoke.assert_called_once()
        # mock_llm returns content like "[Summary] test summary"
        assert "test summary" in result

    async def test_with_previous_summary(self, mock_llm):
        msgs = [MagicMock(content="more context")]
        result = await compact_memory(msgs, previous_summary="old summary", llm=mock_llm)
        # The prompt should include previous summary
        call_args = mock_llm.ainvoke.call_args[0][0]
        prompt_text = call_args[0].content
        assert "old summary" in prompt_text

    async def test_llm_exception_propagates(self, mocker):
        failing_llm = AsyncMock()
        failing_llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM down"))
        msgs = [MagicMock(content="some content")]
        with pytest.raises(RuntimeError, match="LLM down"):
            await compact_memory(msgs, llm=failing_llm)


class TestCompactMemoryWithoutLLM:
    """Test compaction fallback without LLM."""

    async def test_no_llm_uses_simple_compact(self):
        msgs = [MagicMock(content="test content")]
        result = await compact_memory(msgs, llm=None)
        assert "Compressed History" in result

    async def test_simple_compact_includes_previous_summary(self):
        msgs = [MagicMock(content="current")]
        result = await compact_memory(msgs, previous_summary="old context", llm=None)
        assert "old context" in result


class TestPrepareCompactionMessages:
    """Test message truncation for compaction input."""

    def test_short_messages_pass_through(self):
        msgs = [MagicMock(content="short")]
        result = _prepare_compaction_messages(msgs)
        assert len(result) == 1

    def test_very_long_messages_truncated(self):
        # Create messages exceeding MAX_COMPACTION_INPUT_CHARS
        msgs = [MagicMock(content="a" * 60000), MagicMock(content="b" * 60000)]
        result = _prepare_compaction_messages(msgs)
        # Should truncate before the second message
        assert len(result) < 2


class TestSimpleCompact:
    """Test simple fallback compaction."""

    def test_output_format(self):
        msgs = [MagicMock(content="hello world")]
        result = _simple_compact(msgs)
        assert "[Compressed History]" in result

    def test_includes_last_10_messages(self):
        msgs = [MagicMock(content=f"msg {i}") for i in range(15)]
        result = _simple_compact(msgs)
        assert "msg 14" in result

    def test_with_previous_summary(self):
        msgs = [MagicMock(content="current")]
        result = _simple_compact(msgs, previous_summary="previous info")
        assert "previous info" in result


class TestCompactionPrompt:
    """Test compaction prompt content."""

    def test_prompt_contains_sections(self):
        assert "Goal" in COMPACTION_PROMPT
        assert "Target" in COMPACTION_PROMPT
        assert "Skill" in COMPACTION_PROMPT
        assert "Progress" in COMPACTION_PROMPT
        assert "Key Results" in COMPACTION_PROMPT
        assert "Next Steps" in COMPACTION_PROMPT


# ---------------------------------------------------------------------------
# New tests: Two-step compaction, three modes, context recovery
# ---------------------------------------------------------------------------


class TestCompactionMode:
    """Test CompactionMode enum."""

    def test_base_mode(self):
        assert CompactionMode.BASE.value == "base"

    def test_partial_mode(self):
        assert CompactionMode.PARTIAL.value == "partial"

    def test_up_to_mode(self):
        assert CompactionMode.UP_TO.value == "up_to"


class TestNoToolsPreamble:
    """Test NO_TOOLS_PREAMBLE and NO_TOOLS_TRAILER content."""

    def test_preamble_warns_against_tools(self):
        assert "Do NOT call any tools" in NO_TOOLS_PREAMBLE
        assert "<analysis>" in NO_TOOLS_PREAMBLE
        assert "<summary>" in NO_TOOLS_PREAMBLE

    def test_trailer_reminds_no_tools(self):
        assert "Do NOT call any tools" in NO_TOOLS_TRAILER
        assert "<analysis>" in NO_TOOLS_TRAILER
        assert "<summary>" in NO_TOOLS_TRAILER


class TestFormatCompactSummary:
    """Test format_compact_summary strips analysis and formats summary."""

    def test_strips_analysis_block(self):
        raw = "<analysis>Some draft thinking</analysis>\n<summary>Final summary</summary>"
        result = format_compact_summary(raw)
        assert "Some draft thinking" not in result
        assert "Final summary" in result

    def test_formats_summary_tags(self):
        raw = "<summary>Key findings here</summary>"
        result = format_compact_summary(raw)
        assert "<summary>" not in result
        assert "</summary>" not in result
        assert "Summary:" in result
        assert "Key findings here" in result

    def test_plain_text_passes_through(self):
        raw = "Just a regular summary without XML tags"
        result = format_compact_summary(raw)
        assert result == raw

    def test_cleans_extra_whitespace(self):
        raw = "<summary>Content</summary>\n\n\n\nMore text"
        result = format_compact_summary(raw)
        assert "\n\n\n" not in result

    def test_full_two_step_format(self):
        raw = """<analysis>
Step 1: User asked for pod-kill
Step 2: Skill was activated
</analysis>

<summary>
1. Goal: Kill a pod
2. Target: default/pod/my-app
3. Skill: pod-kill
</summary>"""
        result = format_compact_summary(raw)
        assert "Step 1" not in result
        assert "Goal: Kill a pod" in result
        assert "Target: default/pod/my-app" in result


class TestExtractCriticalContext:
    """Test extract_critical_context extracts key operational state."""

    def test_extracts_blade_uid_from_tool_message(self):
        msgs = [
            MagicMock(content='blade_uid: abc123def456'),
        ]
        state = {}
        result = extract_critical_context(msgs, state)
        assert result["active_blade_uid"] == "abc123def456"

    def test_extracts_blade_uid_from_json_result(self):
        msgs = [
            MagicMock(content='{"code": 200, "success": true, "result": "f00baa123"}'),
        ]
        state = {}
        result = extract_critical_context(msgs, state)
        assert result["active_blade_uid"] == "f00baa123"

    def test_extracts_skill_from_state(self):
        msgs = []
        state = {"skill_name": "pod-kill"}
        result = extract_critical_context(msgs, state)
        assert result["active_skill"] == "pod-kill"

    def test_extracts_target_from_state(self):
        msgs = []
        state = {
            "target": {
                "namespace": "default",
                "resource_type": "pod",
                "names": ["my-pod"],
            }
        }
        result = extract_critical_context(msgs, state)
        assert result["target"]["namespace"] == "default"
        assert result["target"]["names"] == ["my-pod"]

    def test_extracts_plan_from_state(self):
        msgs = []
        state = {
            "plan_path": "/tmp/plan.md",
            "plan": "# Fault Injection Plan",
        }
        result = extract_critical_context(msgs, state)
        assert result["plan_path"] == "/tmp/plan.md"
        assert result["plan"] == "# Fault Injection Plan"

    def test_blade_uid_from_state_fallback(self):
        msgs = []
        state = {"blade_uid": "aabb1122ccdd"}
        result = extract_critical_context(msgs, state)
        assert result["active_blade_uid"] == "aabb1122ccdd"

    def test_message_blade_uid_takes_priority_over_state(self):
        msgs = [MagicMock(content='blade_uid: cc1234ab5678')]
        state = {"blade_uid": "ff9876ba5432"}
        result = extract_critical_context(msgs, state)
        # Message-extracted UID should take priority
        assert result["active_blade_uid"] == "cc1234ab5678"

    def test_empty_state_returns_empty(self):
        msgs = [MagicMock(content="no relevant content")]
        state = {}
        result = extract_critical_context(msgs, state)
        assert result == {}


class TestBuildPostCompactContextMessage:
    """Test build_post_compact_context_message formatting."""

    def test_empty_context_returns_empty(self):
        result = build_post_compact_context_message({})
        assert result == ""

    def test_includes_blade_uid(self):
        result = build_post_compact_context_message({"active_blade_uid": "abc123"})
        assert "[Context preserved after compaction]" in result
        assert "abc123" in result
        assert "blade_uid" in result

    def test_includes_skill(self):
        result = build_post_compact_context_message({"active_skill": "pod-kill"})
        assert "pod-kill" in result
        assert "Active skill" in result

    def test_includes_target_dict(self):
        target = {
            "namespace": "prod",
            "resource_type": "pod",
            "names": ["api-server"],
        }
        result = build_post_compact_context_message({"target": target})
        assert "prod" in result
        assert "api-server" in result

    def test_includes_plan_path(self):
        result = build_post_compact_context_message({"plan_path": "/tmp/plan.md"})
        assert "/tmp/plan.md" in result
        assert "Plan file" in result

    def test_includes_plan_content(self):
        result = build_post_compact_context_message(
            {"plan": "# Fault Injection Plan"}
        )
        assert "Fault Injection Plan" in result
        assert "Plan content" in result

    def test_full_context_message(self):
        ctx = {
            "active_blade_uid": "abc123",
            "active_skill": "pod-kill",
            "target": {"namespace": "default", "resource_type": "pod", "names": ["my-pod"]},
            "plan_path": "/tmp/plan.md",
        }
        result = build_post_compact_context_message(ctx)
        assert "[Context preserved after compaction]" in result
        assert "abc123" in result
        assert "pod-kill" in result
        assert "default" in result
        assert "/tmp/plan.md" in result


class TestCompactMemoryWithModes:
    """Test compact_memory with different modes and context recovery."""

    async def test_base_mode_default(self, mock_llm):
        msgs = [MagicMock(content="test message")]
        result = await compact_memory(msgs, llm=mock_llm, mode=CompactionMode.BASE)
        mock_llm.ainvoke.assert_called_once()
        # Verify prompt includes BASE mode content
        call_args = mock_llm.ainvoke.call_args[0][0]
        prompt = call_args[0].content
        assert "Do NOT call any tools" in prompt

    async def test_partial_mode_prompt(self, mock_llm):
        msgs = [MagicMock(content="test message")]
        result = await compact_memory(msgs, llm=mock_llm, mode=CompactionMode.PARTIAL)
        call_args = mock_llm.ainvoke.call_args[0][0]
        prompt = call_args[0].content
        assert "RECENT portion" in prompt

    async def test_up_to_mode_prompt(self, mock_llm):
        msgs = [MagicMock(content="test message")]
        result = await compact_memory(msgs, llm=mock_llm, mode=CompactionMode.UP_TO)
        call_args = mock_llm.ainvoke.call_args[0][0]
        prompt = call_args[0].content
        assert "continuing session" in prompt

    async def test_context_recovery_prepended(self):
        msgs = [MagicMock(content="test message")]
        state = {
            "skill_name": "pod-kill",
            "blade_uid": "abc123",
        }
        result = await compact_memory(msgs, llm=None, state=state)
        assert "[Context preserved after compaction]" in result
        assert "abc123" in result
        assert "pod-kill" in result

    async def test_no_context_recovery_without_state(self):
        msgs = [MagicMock(content="test message")]
        result = await compact_memory(msgs, llm=None, state=None)
        assert "[Context preserved after compaction]" not in result

    async def test_llm_summary_is_formatted(self, mocker):
        """Test that LLM output with <analysis>/<summary> tags is formatted."""
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(
            return_value=MagicMock(
                content="<analysis>Draft thoughts</analysis>\n<summary>Final result</summary>"
            )
        )
        msgs = [MagicMock(content="test")]
        result = await compact_memory(msgs, llm=llm)
        assert "Draft thoughts" not in result
        assert "Final result" in result


# ---------------------------------------------------------------------------
# New tests: Skill token budget (Migration Point 12)
# ---------------------------------------------------------------------------


class TestTruncateToTokens:
    """Test truncate_to_tokens for skill content."""

    def test_short_content_not_truncated(self):
        result = truncate_to_tokens("short content", 1000)
        assert result == "short content"

    def test_long_content_truncated(self):
        long_content = "a" * 100000  # ~25000 tokens
        result = truncate_to_tokens(long_content, 1000)
        assert SKILL_TRUNCATION_MARKER in result
        assert len(result) < len(long_content)
        assert result.startswith("aaa")  # Keeps the head

    def test_truncated_content_respects_budget(self):
        # max_tokens=500 → ~2000 chars budget
        long_content = "b" * 5000
        result = truncate_to_tokens(long_content, 500)
        # Result should be roughly 2000 chars (500 tokens * 4 chars/token)
        assert len(result) < 5000


class TestSkillContentExtraction:
    """Test that extract_critical_context preserves skill content."""

    def test_extracts_skill_content_from_messages(self):
        skill_msg = MagicMock(
            content="pod-kill skill instruction: Pre-checks and injection procedure"
        )
        msgs = [skill_msg]
        state = {"skill_name": "pod-kill"}
        result = extract_critical_context(msgs, state)
        assert "active_skill_content" in result
        assert "pod-kill" in result["active_skill_content"]

    def test_skill_content_truncated_to_budget(self):
        # Create a very long skill content message that matches extraction heuristics
        long_content = "pod-kill skill instruction: " + "x" * 100000
        skill_msg = MagicMock(content=long_content)
        msgs = [skill_msg]
        state = {"skill_name": "pod-kill"}
        result = extract_critical_context(msgs, state)
        assert "active_skill_content" in result
        # Per-skill budget truncates to ~POST_COMPACT_MAX_TOKENS_PER_SKILL tokens
        # (5000 tokens * 4 chars = 20000 chars + truncation marker)
        assert len(result["active_skill_content"]) < len(long_content)
        assert SKILL_TRUNCATION_MARKER in result["active_skill_content"]

    def test_no_skill_content_when_not_found(self):
        msgs = [MagicMock(content="unrelated content")]
        state = {"skill_name": "pod-kill"}
        result = extract_critical_context(msgs, state)
        assert "active_skill_content" not in result
        assert result["active_skill"] == "pod-kill"


class TestBuildPostCompactContextWithSkillContent:
    """Test build_post_compact_context_message includes skill content."""

    def test_includes_skill_instructions(self):
        ctx = {
            "active_skill": "pod-kill",
            "active_skill_content": "Pre-checks: verify pod exists\nInjection: blade create...",
        }
        result = build_post_compact_context_message(ctx)
        assert "Skill instructions (preserved)" in result
        assert "Pre-checks" in result

    def test_full_context_with_skill_content(self):
        ctx = {
            "active_blade_uid": "abc123",
            "active_skill": "pod-kill",
            "active_skill_content": "Kill the target pod",
            "target": {"namespace": "default", "resource_type": "pod", "names": ["my-pod"]},
        }
        result = build_post_compact_context_message(ctx)
        assert "blade_uid" in result
        assert "pod-kill" in result
        assert "Skill instructions" in result
        assert "Kill the target pod" in result


# ---------------------------------------------------------------------------
# New tests: Layered Compaction (Migration Point 13)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Note: ``TestTryLightweightCompact`` and ``TestCompactIfNeeded`` test
# classes were removed when those functions were retired in favour of
# the unified ``PreReasoningHook`` (called with ``force=True`` for the
# manual /compact path). Hook-level coverage lives in
# ``test_hook.py::TestPreReasoningHookForceCompact``.
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# New tests: Skill token budget enforcement (Migration Point 12)
# ---------------------------------------------------------------------------


class TestSkillTokenBudgetEnforcement:
    """Test that POST_COMPACT_SKILLS_TOKEN_BUDGET total budget is enforced."""

    def test_single_skill_within_budget(self):
        """Single skill content should be truncated to per-skill budget."""
        skill_content = "pod-kill skill instruction: " + "x" * 100000
        msgs = [MagicMock(content=skill_content)]
        state = {"skill_name": "pod-kill"}
        result = extract_critical_context(msgs, state)
        assert "active_skill_content" in result
        # Per-skill budget truncates content
        assert len(result["active_skill_content"]) < len(skill_content)
        assert SKILL_TRUNCATION_MARKER in result["active_skill_content"]

    def test_multiple_skills_total_budget(self):
        """Multiple skills should respect total token budget."""
        skill1_content = "pod-kill skill instruction: " + "a" * 30000
        skill2_content = "pod-network-delay skill instruction: " + "b" * 30000
        msgs = [
            MagicMock(content=skill1_content),
            MagicMock(content=skill2_content),
        ]
        state = {
            "skill_name": "pod-kill",
            "active_skills": ["pod-kill", "pod-network-delay"],
        }
        result = extract_critical_context(msgs, state)
        assert "active_skill_content" in result
        # Total content should be within SKILLS_TOKEN_BUDGET
        total_chars = len(result["active_skill_content"])
        # Each char ≈ 0.25 tokens, so total_tokens ≈ total_chars / 4
        estimated_tokens = total_chars // 4
        # Allow some margin for the separator
        assert estimated_tokens <= POST_COMPACT_SKILLS_TOKEN_BUDGET + 100

    def test_budget_exhaustion_stops_adding(self):
        """When total budget is exhausted, no more skills are added."""
        # Create skill content that alone exceeds the budget
        huge_content = "pod-kill skill instruction: " + "z" * (POST_COMPACT_SKILLS_TOKEN_BUDGET * 5)
        msgs = [MagicMock(content=huge_content)]
        state = {
            "skill_name": "pod-kill",
            "active_skills": ["pod-kill", "pod-network-delay"],
        }
        result = extract_critical_context(msgs, state)
        # Should have skill content but within budget
        assert "active_skill_content" in result
        total_chars = len(result["active_skill_content"])
        estimated_tokens = total_chars // 4
        assert estimated_tokens <= POST_COMPACT_SKILLS_TOKEN_BUDGET + 100

    def test_no_skills_no_budget_issue(self):
        """No active skills means no skill content in context."""
        msgs = [MagicMock(content="unrelated content")]
        state = {}
        result = extract_critical_context(msgs, state)
        assert "active_skill" not in result
        assert "active_skill_content" not in result
