"""Context fullness must be anchored on what the provider actually received.

``count_tokens_messages`` measures a message list as text. That is not how full
the window is, because a request also carries the system prompt (assembled
skills, knowledge, baselines) and every tool's JSON schema — none of which appear
in ``messages``.

Measured on this project's real checkpoint database (qwen3-max, 87 recorded
calls in one drill):

    call 1:  provider input_tokens = 6,936   message list as text = 11
    ...
    mean absolute error over 9 consecutive calls:
        anchored on provider usage   8%
        message text only           77%   (low EVERY time: -67%..-95%)

Low is the dangerous direction — it delays compaction. The invisible overhead is
also not a constant: across that same drill it grew from 6,925 to 16,157 tokens
as skills loaded, which is why it is re-derived from the newest report rather
than averaged or hard-coded.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.memory.tokens import (
    count_tokens_messages,
    estimate_context_tokens,
)


def _ai_with_usage(input_tokens: int, content: str = "回答", msg_id: str = "a") -> AIMessage:
    return AIMessage(
        content=content,
        id=msg_id,
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": 10,
            "total_tokens": input_tokens + 10,
        },
    )


class TestProviderUsageAnchorsTheEstimate:
    def test_reported_input_replaces_a_local_sum_of_the_history(self):
        """The 630x case: a short history whose real request was 6,936 tokens."""
        messages = [HumanMessage(content="演练 CPU 故障", id="h0"), _ai_with_usage(6_936)]
        usage = estimate_context_tokens(messages)

        assert usage.exact
        assert usage.usage_tokens == 6_936
        assert usage.tokens > 6_936, "the anchor must be additive, not a replacement"
        assert usage.tokens < 6_936 + 200, "only the trailing slice may be added"

    def test_the_newest_report_wins(self):
        """Older anchors describe a smaller conversation and must not be used."""
        messages = [
            HumanMessage(content="请求", id="h0"),
            _ai_with_usage(5_000, msg_id="a0"),
            ToolMessage(content="ok", tool_call_id="c0", id="t0"),
            _ai_with_usage(9_000, msg_id="a1"),
        ]
        assert estimate_context_tokens(messages).usage_tokens == 9_000

    def test_messages_after_the_anchor_are_added(self):
        anchor_only = estimate_context_tokens([_ai_with_usage(5_000)])
        with_tail = estimate_context_tokens(
            [_ai_with_usage(5_000), ToolMessage(content="输出" * 500, tool_call_id="c0", id="t0")]
        )
        assert with_tail.tokens > anchor_only.tokens
        assert with_tail.usage_tokens == anchor_only.usage_tokens == 5_000

    def test_falls_back_to_a_local_estimate_without_usage(self):
        """Marked inexact so callers know the figure is blind to the overhead."""
        messages = [HumanMessage(content="请求" * 100, id="h0"), AIMessage(content="回答", id="a0")]
        usage = estimate_context_tokens(messages)

        assert not usage.exact
        assert usage.usage_tokens == 0
        assert usage.overhead_tokens == 0
        assert usage.tokens == count_tokens_messages(messages).count

    def test_empty_history_is_zero(self):
        usage = estimate_context_tokens([])
        assert usage.tokens == 0 and not usage.exact


class TestMalformedUsageIsIgnored:
    """A bad report must degrade to estimating, never poison the number."""

    def test_non_assistant_message_carrying_usage_is_skipped(self):
        human = HumanMessage(content="请求", id="h0")
        human.usage_metadata = {"input_tokens": 99_999}
        assert not estimate_context_tokens([human]).exact

    def test_zero_and_negative_input_tokens_are_rejected(self):
        for bad in (0, -1):
            msg = AIMessage(content="x", id="a0")
            # Assigned after construction: the constructor validates the full
            # UsageMetadata shape, and these malformed values are exactly what
            # this test needs to reach the guard.
            msg.usage_metadata = {"input_tokens": bad}
            assert not estimate_context_tokens([msg]).exact, bad

    def test_non_integer_input_tokens_is_rejected(self):
        msg = AIMessage(content="x", id="a0")
        msg.usage_metadata = {"input_tokens": "6936"}
        assert not estimate_context_tokens([msg]).exact

    def test_boolean_is_not_accepted_as_a_count(self):
        """``True`` is an int in Python; it is not a token count."""
        msg = AIMessage(content="x", id="a0")
        msg.usage_metadata = {"input_tokens": True}
        assert not estimate_context_tokens([msg]).exact

    def test_missing_usage_metadata_is_skipped(self):
        msg = AIMessage(content="x", id="a0")
        msg.usage_metadata = None
        assert not estimate_context_tokens([msg]).exact


class TestOverheadDerivation:
    """The overhead is what the report cannot be explained by visible text."""

    def test_overhead_is_the_unexplained_remainder(self):
        history = [HumanMessage(content="演练", id="h0")]
        visible = count_tokens_messages(history).count
        messages = history + [_ai_with_usage(6_936)]

        usage = estimate_context_tokens(messages)
        assert usage.overhead_tokens == 6_936 - visible

    def test_overhead_never_goes_negative(self):
        """After a compaction the anchor can predate messages already removed."""
        messages = [
            HumanMessage(content="很长的历史" * 5_000, id="h0"),
            _ai_with_usage(100),
        ]
        assert estimate_context_tokens(messages).overhead_tokens == 0

    def test_project_adds_the_overhead_to_a_hypothetical_set(self):
        messages = [HumanMessage(content="演练", id="h0"), _ai_with_usage(6_936)]
        usage = estimate_context_tokens(messages)
        assert usage.project(1_000) == 1_000 + usage.overhead_tokens

    def test_project_is_a_noop_without_an_anchor(self):
        """No usage means no derivable overhead — do not invent one."""
        usage = estimate_context_tokens([HumanMessage(content="请求", id="h0")])
        assert usage.project(1_000) == 1_000


class TestSafeTokensMarginScope:
    """The margin belongs on the estimate, not on the provider's own number."""

    def test_margin_is_not_applied_to_the_anchor(self):
        usage = estimate_context_tokens([_ai_with_usage(10_000, content="")])
        # Only the trailing slice may be inflated, and an empty-content anchor
        # contributes almost nothing to it.
        assert usage.safe_tokens - 10_000 < 50

    def test_safe_tokens_is_never_below_tokens(self):
        messages = [
            HumanMessage(content="请求" * 50, id="h0"),
            _ai_with_usage(5_000),
            ToolMessage(content="输出" * 200, tool_call_id="c0", id="t0"),
        ]
        usage = estimate_context_tokens(messages)
        assert usage.safe_tokens >= usage.tokens
