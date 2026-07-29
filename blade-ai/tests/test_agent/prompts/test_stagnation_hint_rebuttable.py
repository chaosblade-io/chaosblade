"""Stagnation hints must be rebuttable where the tool is still available.

Only the tool-level branch of ``build_stagnation_hint`` actually removes the
tool (``filter_stagnant_tool`` skips subcommand-level stagnation by design), so a
flat prohibition on the subcommand branch is one the model can disprove on its
next turn. A hint the model can disprove teaches it to discount these warnings
and invites it to argue rather than reconsider.
"""

import re

from chaos_agent.agent.nodes.execute.llm_step_helpers import build_stagnation_hint


class TestSubcommandStagnationHintIsRebuttable:
    def test_subcommand_branch_issues_no_prohibition(self):
        hint = build_stagnation_hint("kubectl_read:get", colon_suffix="(describe)")
        assert "Do NOT call" not in hint
        assert "Stop using this subcommand" not in hint

    def test_subcommand_branch_leaves_a_way_to_continue(self):
        hint = build_stagnation_hint("kubectl_read:get")
        assert "genuinely required here" in hint
        assert "say why and continue" in hint

    def test_tool_branch_keeps_its_prohibition(self):
        """Here the tool really is gone, so the statement is a fact."""
        hint = build_stagnation_hint("blade_help", else_actions=["Use another tool."])
        assert "temporarily removed" in hint
        assert "Do NOT attempt to call" in hint

    def test_intent_splice_anchor_still_exists(self):
        """Guards the string-splice in intent_clarification against drift.

        The splice anchors on the shared closing sentence; if that wording
        changes and the splice is not updated, the phase-ending option silently
        stops being offered.
        """
        from chaos_agent.agent.nodes.planning import intent_clarification  # noqa: F401

        base = build_stagnation_hint(
            "kubectl_read:get",
            colon_suffix="",
            else_actions=["Return a normal reply."],
        )
        assert "If repeating" in base, (
            "intent_clarification splices on this anchor; keep them in sync"
        )

    def test_no_hard_prohibition_in_any_phase_body(self):
        """Guards every phase's canned body, not just the one under test."""
        from chaos_agent.agent.nodes.execute.react_helpers import (
            _LOOP_HINTS,
            _STAGNATION_HINTS,
        )

        banned = re.compile(
            r"(?i)\b(do not|don't|must not|never)\s+(call|use|repeat|retry)"
        )
        for name, table in (("loop", _LOOP_HINTS), ("stagnation", _STAGNATION_HINTS)):
            for phase, text in table.items():
                assert not banned.search(text), f"{name}/{phase} issues an order"
