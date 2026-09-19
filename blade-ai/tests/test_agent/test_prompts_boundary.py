"""Tests for the phase boundary declaration module (prompts/boundary.py).

Anchors the contract's construction-side guarantees: the three axes are
locatable in the built text, the tool-surface axis is genuinely
conditional (D7), the graft instance is channel-neutral, and the text
composes correctly with the reminder wrapping used at injection sites.
"""

import re

from chaos_agent.agent.prompts.boundary import (
    INJECT_TO_RECOVER_BOUNDARY_DECLARATION,
    build_boundary_declaration,
)
from chaos_agent.agent.prompts.reminder import (
    SYSTEM_REMINDER_CLOSE,
    SYSTEM_REMINDER_OPEN,
    wrap_system_reminder,
)

# Channel-specific vocabulary that must never appear in a boundary
# declaration: the declaration is channel-neutral by contract, the
# injection site supplies channel context.
_CHANNEL_VOCABULARY = (
    "kubectl", "kubeconfig", "host_read", "host_inject", "pod",
    "namespace", "cluster", "node",
)


class TestBuildBoundaryDeclaration:
    def test_three_axes_locatable_in_built_text(self):
        text = build_boundary_declaration(
            role="you are now in phase X; phase Y is over",
            evidence_tense="history above is a snapshot; baseline only",
            tool_surface="currently bound tools are the authority",
        )
        # Role axis: the marker line plus the Role bullet.
        assert "**PHASE BOUNDARY" in text
        assert "- Role: you are now in phase X" in text
        # Temporal axis: the provenance framing plus the Evidence bullet.
        assert "COMPLETED earlier phase" in text
        assert "- Evidence: history above is a snapshot" in text
        # Tool-surface axis: the Tools bullet.
        assert "- Tools: currently bound tools are the authority" in text

    def test_tool_surface_none_omits_the_axis_entirely(self):
        # D7: seams whose tool-surface axis does not apply (inertia
        # stripped / surface unchanged / duty covers it) pass None and
        # the built text carries no tool-surface wording at all.
        text = build_boundary_declaration(
            role="role text",
            evidence_tense="tense text",
            tool_surface=None,
        )
        assert "- Tools:" not in text
        assert "authority" not in text

    def test_history_framing_addresses_the_messages_above(self):
        # The declaration is injected BETWEEN grafted history and the
        # task context; its framing must point at the messages ABOVE it.
        text = build_boundary_declaration(
            role="r", evidence_tense="t", tool_surface="s",
        )
        assert "history above" in text


class TestInjectToRecoverDeclaration:
    def test_instance_carries_all_three_axes(self):
        text = INJECT_TO_RECOVER_BOUNDARY_DECLARATION
        # Role: recovery task active, inject task over.
        assert "RECOVERY task" in text
        assert "inject task is over" in text
        # Temporal: snapshot framing with the comparison-baseline use.
        assert "snapshot from" in text
        assert "BEFORE or DURING injection" in text
        assert "comparison baseline" in text
        assert "NEVER evidence of" in text
        # Tool surface: bound tools authoritative, history names stale.
        assert "currently bound to you are the authority" in text
        assert "different" in text and "phase's surface" in text

    def test_instance_is_channel_neutral(self):
        text = INJECT_TO_RECOVER_BOUNDARY_DECLARATION.lower()
        for word in _CHANNEL_VOCABULARY:
            assert word not in text, (
                f"channel vocabulary '{word}' leaked into the graft "
                "declaration — the injection site owns channel context"
            )

    def test_instance_composes_with_reminder_wrapping(self):
        wrapped = wrap_system_reminder(INJECT_TO_RECOVER_BOUNDARY_DECLARATION)
        # Exactly one tag pair (wrap is idempotent; the declaration text
        # itself contains no tags).
        assert wrapped.count(SYSTEM_REMINDER_OPEN) == 1
        assert wrapped.count(SYSTEM_REMINDER_CLOSE) == 1
        assert wrapped.startswith(SYSTEM_REMINDER_OPEN)
        # Round-trip: the declaration survives intact inside the tags.
        inner = wrapped[len(SYSTEM_REMINDER_OPEN):wrapped.rfind(SYSTEM_REMINDER_CLOSE)]
        assert inner.strip() == INJECT_TO_RECOVER_BOUNDARY_DECLARATION.strip()
        assert wrap_system_reminder(wrapped) == wrapped

    def test_instance_is_plain_text_without_markup_leaks(self):
        # No stray tags from the declaration itself (it will be wrapped
        # by the injection site; nested tags would break the contract).
        text = INJECT_TO_RECOVER_BOUNDARY_DECLARATION
        assert SYSTEM_REMINDER_OPEN not in text
        assert SYSTEM_REMINDER_CLOSE not in text
        # Marker form mirrors the Phase-2 kickoff convention (bold,
        # em-dash separated) so the visual register stays consistent.
        assert re.match(r"^\*\*PHASE BOUNDARY — READ THIS", text)
