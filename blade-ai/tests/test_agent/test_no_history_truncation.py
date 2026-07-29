"""Nodes must not truncate history behind the compaction hook's back.

Every LLM call in the main chain — ``agent_loop``, ``execute_loop``, ``verifier``,
and the three in ``recover_verifier`` — passes ``[SystemMessage] + messages``.
Two planning nodes used to slice instead: ``messages[-20:]`` in
``intent_clarification`` and ``messages[-30:]`` in ``plan_builder``. Both predate
the compaction hook (the first appeared 2026-05-09, when the node was created and
self-limiting was the only option) and were never removed once the hook existed.

What that cost, measured on a real 84-message checkpoint: the drill held exactly
ONE HumanMessage, at index 0. From message 22 onward a 20-message window
contained no user turn at all — the model was reasoning over tool output with no
statement of the task. The same window also evicts the operation summaries that
``write_operation_summary`` appends to the Intent Graph after each
inject/recover, i.e. the record of what earlier drills accomplished.

A fixed message count cannot do the hook's job: it is blind to message size, and
it ranks the task definition no higher than a routine tool result. The hook
bounds context by token budget, preserves tool-call pairing, and leaves a summary
behind. These tests keep the nodes on that mechanism.
"""

from __future__ import annotations

import inspect
import re

import pytest

from chaos_agent.agent.nodes.execute import agent_loop as agent_loop_mod
from chaos_agent.agent.nodes.execute import execute_loop as execute_loop_mod
from chaos_agent.agent.nodes.planning import intent_clarification as intent_mod
from chaos_agent.agent.nodes.planning import plan_builder as plan_mod
from chaos_agent.agent.nodes.recover import _recover_verifier_loop as recover_mod
from chaos_agent.agent.nodes.verify import verifier as verifier_mod

# A slice that drops the OLDEST messages. ``messages[-1]`` (take the latest) and
# ``messages[i:]`` (from an index onward) are different operations and fine.
_TRUNCATING_SLICE = re.compile(r"messages\[-\d{2,}:\]|messages\[-[2-9]:\]")

_NODE_MODULES = {
    "intent_clarification": intent_mod,
    "plan_builder": plan_mod,
    "agent_loop": agent_loop_mod,
    "execute_loop": execute_loop_mod,
    "verifier": verifier_mod,
    "recover_verifier_loop": recover_mod,
}


def _executable_source(module) -> str:
    """Module source with comment lines removed.

    The fixed slices are described in comments explaining why they were removed,
    so a naive substring scan would match its own documentation.
    """
    lines = []
    for line in inspect.getsource(module).splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        lines.append(line.split("  #")[0])
    return "\n".join(lines)


class TestNoNodeTruncatesHistory:
    @pytest.mark.parametrize("name", sorted(_NODE_MODULES))
    def test_no_fixed_size_message_slice(self, name):
        matches = _TRUNCATING_SLICE.findall(_executable_source(_NODE_MODULES[name]))
        assert not matches, (
            f"{name} truncates history with {matches} — a fixed message count "
            f"drops the user's request (index 0 in a real drill) and the "
            f"operation summaries of earlier drills. Context bounding belongs to "
            f"the compaction hook."
        )

    def test_the_guard_would_catch_a_regression(self):
        """The pattern must actually match what it claims to police."""
        assert _TRUNCATING_SLICE.search("llm_messages = [system_msg] + messages[-20:]")
        assert _TRUNCATING_SLICE.search("[system_msg] + messages[-30:] + accumulated")

    def test_taking_the_latest_message_is_not_flagged(self):
        """``messages[-1]`` is a lookup, not a truncation — screeners use it."""
        assert not _TRUNCATING_SLICE.search("last = messages[-1] if messages else None")

    def test_slicing_from_an_index_is_not_flagged(self):
        """``messages[i:]`` keeps the tail from a computed point; that is fine."""
        assert not _TRUNCATING_SLICE.search("trailing = messages[i:]")


class TestPlanningNodesRereadAfterCompaction:
    """Passing full history is only safe if the list is the post-hook one.

    ``agent_loop`` and ``execute_loop`` call the hook and THEN read
    ``state["messages"]``. Both planning nodes captured the list at the top of
    the function, before the hook ran, so they would have handed the LLM the
    pre-compaction history — exactly what the hook had just trimmed.
    """

    @pytest.mark.parametrize("name", ["intent_clarification", "plan_builder"])
    def test_messages_are_reread_after_the_hook(self, name):
        src = _executable_source(_NODE_MODULES[name])
        hook_call = src.index("await hook(state)")
        after_hook = src[hook_call:]
        assert 'state.get("messages"' in after_hook, (
            f"{name} does not re-read messages after the compaction hook, so the "
            f"LLM would receive the pre-compaction list"
        )

    @pytest.mark.parametrize("name", ["agent_loop", "execute_loop"])
    def test_main_chain_already_does_this(self, name):
        """Pins the pattern the fix was aligned to, so it cannot drift either."""
        src = _executable_source(_NODE_MODULES[name])
        hook_call = src.index("await hook(state)")
        assert 'state.get("messages"' in src[hook_call:]
