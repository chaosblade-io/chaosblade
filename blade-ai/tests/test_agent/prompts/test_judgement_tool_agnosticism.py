"""Judgement-level prompt sections must not name concrete tools.

Two separate rules, and the distinction is the whole point:

* Sections that convey JUDGEMENT (principles, heuristics, delay protocols) must
  stay tool-agnostic. A tool's own description already states when to use it, so
  naming it in a principle duplicates that, rots when the tool surface changes,
  and teaches the model to follow the prompt instead of its bound tools.
* Sections that define the OUTPUT CONTRACT may — and must — name the submission
  tool, because the model cannot deliver a verdict without knowing the channel.

The convergence principle is guarded here too. Verification had no terminal
state before it: task-c7c75263 issued the same metric query 26 times with the
model explaining "I need to change the observation angle to verify more
comprehensively", after effect / attribution / coverage were all already proven.
Completeness of observation has no end; an evidence burden does.
"""

from chaos_agent.agent.prompts.sections.recovery import get_recover_delay_section
from chaos_agent.agent.prompts.sections.verification import (
    get_verifier_core_principles_section,
    get_verifier_remember_section,
)
from chaos_agent.agent.prompts.sections.workflow import (
    get_verification_heuristics_compact_section,
)

# Tools that must never appear in a judgement-level section.
CONCRETE_TOOLS = (
    "time_wait",
    "blade_create",
    "blade_destroy",
    "blade_status",
    "kubectl_read",
    "host_read",
    "execute_skill_script",
    "read_skill_resource",
)

JUDGEMENT_SECTIONS = {
    "verifier_core_principles": get_verifier_core_principles_section,
    "verifier_remember": get_verifier_remember_section,
    "recover_delay": get_recover_delay_section,
    "verification_heuristics": get_verification_heuristics_compact_section,
}


class TestJudgementSectionsAreToolAgnostic:
    def test_no_concrete_tool_names(self):
        offenders = {}
        for name, fn in JUDGEMENT_SECTIONS.items():
            text = fn()
            found = [t for t in CONCRETE_TOOLS if t in text]
            if found:
                offenders[name] = found
        assert offenders == {}, (
            f"judgement sections must convey WHEN/WHY, not WHICH tool: {offenders}"
        )

    def test_recover_delay_still_conveys_the_wait_judgement(self):
        """Removing the tool name must not remove the rule it carried."""
        text = get_recover_delay_section()
        assert "let time elapse" in text
        assert "one reading" in text
        assert "prove nothing" in text


class TestVerifierConvergencePrinciple:
    """The phase must have a terminal state, expressed as an evidence burden."""

    def test_core_principles_state_the_three_elements(self):
        text = get_verifier_core_principles_section()
        assert "effect present" in text
        assert "attributable to the injection" in text
        assert "coverage of the target set" in text

    def test_core_principles_state_that_resampling_adds_nothing(self):
        text = get_verifier_core_principles_section()
        assert "adds no proof" in text

    def test_core_principles_reject_completeness_as_a_reason(self):
        """The model's actual justification for looping 26 times."""
        text = get_verifier_core_principles_section()
        assert "another angle exists" in text
        assert "never a reason to continue" in text

    def test_core_principles_treat_unobservable_as_terminal(self):
        text = get_verifier_core_principles_section()
        assert "unable to observe is itself a conclusion" in text

    def test_remember_restates_convergence_in_the_recency_zone(self):
        """The failure it prevents happens late, far from the primacy copy."""
        text = get_verifier_remember_section()
        assert "adds no proof" in text
        assert "completeness of observation is not the goal" in text

    def test_convergence_is_a_core_principle_not_a_heuristic(self):
        """Placement matters: the existing heuristics all push to keep observing.

        Appending a stop rule at the end of that list would fight the ten lines
        above it, so the convergence rule lives in the invariant primacy zone
        alongside Phase 1's "grounded → finish_planning" and Phase 2's "all
        steps done → STOP".
        """
        assert "adds no proof" in get_verifier_core_principles_section()
        assert "adds no proof" not in get_verification_heuristics_compact_section()
