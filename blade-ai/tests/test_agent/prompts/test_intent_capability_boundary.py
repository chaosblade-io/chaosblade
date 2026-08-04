"""An enumerated "not supported" must read as a conclusion, not a retry cue.

`Reflection` tells the intent model to suspect its METHOD rather than the target.
That is correct for an uncertain result — empty, error, irrelevant — and wrong for
an enumerated one, and nothing in the phase drew that line.

task-1707c16e is the cost. The user asked for 80% packet loss on a pod;
``blade create k8s pod-network`` offers ``dns``, ``drop`` and ``occupy``, and drop
is all-or-nothing with no percentage flag. The model found this and wrote it down
— "没有 loss 子命令", "drop 是全量丢包（100%屏蔽），而不是按百分比丢包" — then
called ``blade_help`` 28 times over 45 rounds, twice recording "我意识到我在重复
调用 blade_help" without stopping, until the operator aborted. It was following
Reflection faithfully. What it lacked was permission to treat the enumeration as
final and hand the choice back to the user.

These tests pin the section's presence, its position relative to Reflection (the
text refers to "the empty or failed query above"), and the three claims that make
it work. They cannot show that a weak model actually stops — only a live run can.
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.prompts.builders import build_intent_clarification_prompt
from chaos_agent.agent.prompts.sections.intent import (
    get_intent_capability_boundary_section,
    get_intent_reflection_section,
)
from chaos_agent.memory.tokens import count_tokens


def _flat(text: str) -> str:
    """Lowercased with runs of whitespace collapsed.

    The section is hard-wrapped, so a phrase that reads as one line in the source
    may straddle a newline. Asserting on the raw string made one test fail on
    ``incomplete, contradictory, or\nirrelevant`` — a wrapping artefact, not a
    content change.
    """
    return " ".join(text.lower().split())


@pytest.fixture
def prompt() -> str:
    """The prompt as the node actually builds it.

    ``intent_clarification`` always passes ``semantic_only=True``, so that is the
    only variant that reaches a model.
    """
    return build_intent_clarification_prompt(semantic_only=True)


class TestItReachesTheModel:
    def test_present_in_the_assembled_prompt(self, prompt):
        assert "# Capability Boundary" in prompt

    def test_present_verbatim(self, prompt):
        """Guards against a truncated copy drifting from the source."""
        assert get_intent_capability_boundary_section() in prompt

    def test_it_follows_reflection(self, prompt):
        """Positional, not cosmetic: the text says "the query above".

        Placed before Reflection, the contrast it draws would point at nothing.
        """
        assert prompt.index("# Reflection") < prompt.index("# Capability Boundary")


class TestTheThreeClaims:
    """Each answers one step of the observed failure."""

    def test_an_enumeration_is_treated_as_the_answer(self):
        """Against re-reading: the model read the same command list repeatedly."""
        text = _flat(get_intent_capability_boundary_section())
        assert "enumerates" in text
        assert "that list is the answer" in text

    def test_widening_the_scope_is_named_as_useless(self):
        """Against its actual escalation: loss → pod-network → create k8s."""
        text = _flat(get_intent_capability_boundary_section())
        assert "wider scope adds nothing" in text

    def test_a_gap_is_declared_a_legitimate_outcome(self):
        """The permission it lacked — it knew the answer and could not stop."""
        text = _flat(get_intent_capability_boundary_section())
        assert "legitimate outcome" in text

    def test_it_classifies_the_result_against_reflections_trigger(self):
        """Reflection fires on "incomplete, contradictory, or irrelevant".

        An enumeration is none of those, which is why no rule covered it. The
        section has to name that distinction or it just reads as a repeat.
        """
        text = _flat(get_intent_capability_boundary_section())
        assert "incomplete or irrelevant result above" in text
        assert "certainty" in text
        assert "incomplete, contradictory, or irrelevant" in _flat(
            get_intent_reflection_section(semantic_only=True)
        )


class TestItStaysCheap:
    def test_within_the_size_of_its_neighbour(self):
        """Prompt budget is contested; a new rule earns its place by being small."""
        boundary = count_tokens(get_intent_capability_boundary_section()).count
        reflection = count_tokens(get_intent_reflection_section()).count
        assert boundary <= reflection, (
            f"capability boundary is {boundary} tokens against Reflection's "
            f"{reflection} — compress it rather than expand the phase"
        )

    def test_under_a_hard_ceiling(self):
        assert count_tokens(get_intent_capability_boundary_section()).count < 130


class TestScopeIsNotOverreached:
    def test_only_the_intent_phase_carries_it(self):
        """Verify and recover deliver verdicts through submit tools.

        Their infeasibility handling already lives in their own contracts
        (``finish_planning(rejected=True)`` for planning), and a rule about
        handing a choice back to the user does not apply where there is no
        conversation.
        """
        from chaos_agent.agent.prompts.builders import (
            build_execute_system_prompt,
            build_inject_system_prompt,
            build_verifier_prompt,
        )

        for build in (
            lambda: build_verifier_prompt(),
            lambda: build_inject_system_prompt(skill_catalog=[]),
            lambda: build_execute_system_prompt(skill_catalog=[]),
        ):
            assert "# Capability Boundary" not in build()

    def test_reflection_survives_unchanged(self, prompt):
        """The new rule narrows Reflection's scope; it must not replace it.

        Asserts against the semantic_only text, which is the only one the node
        builds. The other variant reads very differently — it is where "SIMPLIFY"
        and "Suspect your METHOD" live — and an earlier draft of this section was
        argued from that unused branch.
        """
        assert "# Reflection" in prompt
        flat = _flat(prompt)
        assert "reassess the semantic interpretation" in flat
        assert "asking the user for one focused clarification" in flat


class TestPlanningPhaseCarriesTheSameRule:
    """agent_loop needs it more than intent does — that is where it happened.

    task-1707c16e ran entirely in the planning phase: message 0 is the
    ``[Intent Clarification Summary]`` handoff, message 5 is the Chaos Engineering
    Agent prompt, and the 28 ``blade_help`` calls start at message 19. The intent
    phase had already converged after 35 dialogue rounds.

    An earlier draft put the rule in intent only and asserted planning did NOT
    carry it, which fenced it off from the one phase that had the failure.

    Planning gets the rule through its own reject clause rather than the intent
    section, because the two phases exit differently: intent hands the choice back
    to the user in conversation, planning calls
    ``finish_planning(rejected=True)``. Copying the conversational wording into a
    phase with no user in the loop would be an instruction it cannot follow.
    """

    @staticmethod
    def _planning() -> str:
        from chaos_agent.agent.prompts.builders import build_inject_system_prompt

        return _flat(build_inject_system_prompt(skill_catalog=[]))

    def test_an_absent_enumerated_capability_is_grounds_to_reject(self):
        planning = self._planning()
        assert "help enumerates its capabilities" in planning
        assert "not among them" in planning

    def test_re_reading_is_excluded_from_exhausting_alternatives(self):
        """The gap the three original conditions left.

        None of them fit: the pod existed, the catalogue had a case, and the model
        could not certify it had exhausted alternatives. So it kept querying,
        which the old wording never counted against the "exhausting" bar.
        """
        planning = self._planning()
        assert "enumerated capability list is a complete answer" in planning
        assert "is not one of the alternatives to exhaust" in planning

    def test_the_three_original_reject_conditions_survive(self):
        """The third condition was strengthened: "no viable path after
        exhausting alternatives" let the model self-certify exhaustion without
        evidence; the current wording requires probed evidence for EVERY
        documented path before a reject is legal."""
        planning = self._planning()
        assert "target absent after verification" in planning
        assert "no matching use-case in the catalogue" in planning
        assert "every documented injection path unviable" in planning

    def test_it_does_not_reuse_the_conversational_wording(self):
        """Planning has no user to report to; its exit is finish_planning."""
        planning = self._planning()
        assert "# capability boundary" not in planning
        assert "rejected=true" in planning

    def test_probe_widening_stays_scoped_to_target_location(self):
        """The line that drove the escalation is kept, and kept narrow.

        "widen the search" is correct for finding a target and was misapplied to
        querying a capability. It still says "to locate the target", and it lives
        in both Core Principles and REMEMBER, which must stay verbatim aligned.
        """
        planning = self._planning()
        assert planning.count("widen the search to locate the target") == 2
