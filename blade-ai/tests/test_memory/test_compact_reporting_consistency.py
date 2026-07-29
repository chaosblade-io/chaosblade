"""``/compact`` must report the same quantity the engine decides on.

Both surfaces (TUI ``_cmd_compact``, server ``/compact`` SSE) used to report
``count_tokens_messages(messages).count`` — message text only. The stated reason
was to match "their own kubectl/blade cost dashboards", but a dashboard shows the
provider's ``input_tokens``, which also covers the system prompt and every tool
schema: 13K-17K that never appears in ``messages``. The bare count was therefore
the one figure that did NOT match, and once the trigger switched to
provider-anchored totals the same ``/compact`` press produced two different
numbers depending on who you asked.

The ``after`` side is the subtle half. It cannot re-anchor on usage, because the
newest report in the post-compaction state may be one that SURVIVED compaction
and so still describes the conversation as it was before — re-anchoring makes
``before == after`` and every compaction looks like it freed nothing (measured:
saved 1,005 -> 0). Projecting the post-compaction text through the
pre-compaction overhead keeps both ends on one ruler.
"""

from __future__ import annotations

import inspect

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from chaos_agent.memory.context_manager import COMPRESSED_HISTORY_PREFIX
from chaos_agent.memory.tokens import count_tokens_messages, estimate_context_tokens
from chaos_agent.server.routes import sessions as sessions_route
from chaos_agent.tui.controllers import commands as tui_commands


def _ai_with_usage(input_tokens: int, content: str, msg_id: str) -> AIMessage:
    msg = AIMessage(content=content, id=msg_id)
    msg.usage_metadata = {
        "input_tokens": input_tokens,
        "output_tokens": 10,
        "total_tokens": input_tokens + 10,
    }
    return msg


def _source_of(fn) -> str:
    return inspect.getsource(fn)


class TestBothSurfacesUseTheAnchoredReading:
    """A grep-style guard: the reading itself is what drifted before."""

    def test_tui_compact_anchors_before_on_provider_usage(self):
        src = _source_of(tui_commands.CommandDispatcher._compact_thread)
        assert "estimate_context_tokens(messages)" in src
        assert "count_tokens_messages(messages).count" not in src, (
            "TUI /compact fell back to counting message text, which understates "
            "the context by the system prompt and tool schemas"
        )

    def test_tui_compact_projects_after_instead_of_re_anchoring(self):
        src = _source_of(tui_commands.CommandDispatcher._compact_thread)
        assert "usage_before.project(" in src
        assert "estimate_context_tokens(\n" not in src.split("snapshot_after")[-1], (
            "the post-compaction side re-anchored on usage; a surviving report "
            "describes the pre-compaction conversation and zeroes out the saving"
        )

    def test_server_compact_anchors_before_on_provider_usage(self):
        src = _source_of(sessions_route)
        assert "usage_before = estimate_context_tokens(messages)" in src
        assert "before = count_tokens_messages(messages).count" not in src

    def test_server_compact_projects_after(self):
        src = _source_of(sessions_route)
        assert "after = usage_before.project(" in src


class TestTheArithmeticIsSound:
    """Same overhead on both ends, so the saving is the real difference."""

    @staticmethod
    def _before_after():
        anchor = _ai_with_usage(18_000, "执行" * 300, "a0")
        before_msgs = [HumanMessage(content="演练" * 200, id="h0"), anchor]
        after_msgs = [
            SystemMessage(content=f"{COMPRESSED_HISTORY_PREFIX}\n摘要", id="s0"),
            anchor,
        ]
        usage = estimate_context_tokens(before_msgs)
        before = usage.tokens
        after = usage.project(count_tokens_messages(after_msgs).count)
        return usage, before, after, before_msgs, after_msgs

    def test_saving_matches_the_text_that_was_actually_removed(self):
        """The overhead is on both sides, so it must cancel out.

        Allowance of 2 tokens: ``count_tokens_messages`` adds a fixed batch
        priming per call, and deriving the overhead splits the history into two
        calls, so that priming is counted once more than in a single-shot count.
        Inherent to segmented counting, 0.01% at these magnitudes — worth naming
        rather than engineering away.
        """
        usage, before, after, before_msgs, after_msgs = self._before_after()
        text_saving = (
            count_tokens_messages(before_msgs).count
            - count_tokens_messages(after_msgs).count
        )
        assert abs((before - after) - text_saving) <= 2

    def test_reported_total_is_dominated_by_the_overhead(self):
        """This is why the percentage must be read alongside the fixed cost."""
        usage, before, _, _, _ = self._before_after()
        assert usage.overhead_tokens > before // 2

    def test_re_anchoring_after_would_erase_the_saving(self):
        """Pins the trap the ``project`` call exists to avoid."""
        _, before, after, _, after_msgs = self._before_after()
        naive_after = estimate_context_tokens(after_msgs).tokens
        assert before - after > 0, "the correct arithmetic must show a saving"
        assert before - naive_after <= 0, (
            "fixture no longer reproduces the re-anchoring trap"
        )


class TestOverheadIsDisclosedToTheUser:
    """A 5% figure without context reads as 'compaction barely worked'."""

    def test_tui_names_the_incompressible_portion(self):
        src = _source_of(tui_commands.CommandDispatcher._compact_thread)
        assert "overhead_tokens" in src
        assert "不可压缩" in src

    def test_disclosure_is_omitted_when_there_is_no_overhead(self):
        """Without a usage anchor there is no overhead to disclose."""
        src = _source_of(tui_commands.CommandDispatcher._compact_thread)
        assert "if usage_before.overhead_tokens" in src
