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

from chaos_agent.agent.nodes.verify._verifier_messages import (
    _EVIDENCE_SEMANTICS_PROMPT,
)
from chaos_agent.agent.prompts.sections.recovery import (
    get_recover_delay_section,
    get_recover_output_format_section,
)
from chaos_agent.agent.prompts.sections.verification import (
    get_verifier_core_principles_section,
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

    # test_remember_restates_convergence_in_the_recency_zone removed in
    # the 2026-09-20 verifier cleanup (pass-4): the REMEMBER mirror it
    # pinned is gone (OQ3 — recency rides the message tail; a
    # prompt-end mirror never held the position). The convergence
    # wording it pinned stays guarded on its real carrier by
    # ``test_convergence_is_a_core_principle_not_a_heuristic`` above
    # ("adds no proof" in Core Principles).

    def test_convergence_is_a_core_principle_not_a_heuristic(self):
        """Placement matters: the existing heuristics all push to keep observing.

        Appending a stop rule at the end of that list would fight the ten lines
        above it, so the convergence rule lives in the invariant primacy zone
        alongside Phase 1's "grounded → finish_planning" and Phase 2's "all
        steps done → STOP".
        """
        assert "adds no proof" in get_verifier_core_principles_section()
        assert "adds no proof" not in get_verification_heuristics_compact_section()


class TestVerifierEffectDecides:
    """E-decides, stated as evidence semantics — not behavioural rules.

    Postmortem (inject-6001154d): the verifier held CrashLoopBackOff watch for
    ~5 minutes after RESTARTS+1 and the back-off event were already in hand.
    The old convergence text demanded convergence "at or near the declared
    value" — contradicting Primary Evidence's "does NOT require reaching the
    exact target value" — and the model followed the more concrete, slower
    instruction. Effect decides, receipts only diagnose (three field-proven
    false-positive receipts: tool Success with zero fault delivered).

    Register history (user review rounds 2-3): v1 stated numeric budgets
    ("at most ONE confirmation") and was rejected like the old "keep
    sampling"; v2 dropped the numbers but kept imperative rules ("NEVER
    passes ... alone") — programmatic thinking that caps the model; v3
    states what each observation MEANS (injected value = mechanism
    parameter, receipts speak for the mechanism, labels render quantities)
    and lets the model derive the behaviour. Both branches of Primary
    Evidence stay open: absolute values are legitimate evidence when no
    baseline was captured.
    """

    def test_semantics_state_both_primary_evidence_branches(self):
        # Baseline change OR healthy-state deviation — the OR branch is what
        # keeps absolute-value judgement legal without a baseline.
        assert "significant change from baseline" in _EVIDENCE_SEMANTICS_PROMPT
        assert "with no baseline" in _EVIDENCE_SEMANTICS_PROMPT
        assert "significant deviation from the expected healthy state" in _EVIDENCE_SEMANTICS_PROMPT

    def test_injected_value_is_a_mechanism_parameter(self):
        # The convergence-to-value demand is replaced by what the value IS.
        assert "mechanism parameter, not a measurement promise" in _EVIDENCE_SEMANTICS_PROMPT
        assert "magnitude information for Warnings" in _EVIDENCE_SEMANTICS_PROMPT

    def test_old_convergence_wording_is_gone(self):
        assert (
            "convergence at or near the declared value"
            not in _EVIDENCE_SEMANTICS_PROMPT
        )
        assert "keep sampling while it trends" not in _EVIDENCE_SEMANTICS_PROMPT

    def test_prompt_stays_descriptive_not_imperative(self):
        # Round-3 review: heuristic semantics, never commanded procedures —
        # no MUST/NEVER emphasis, no count caps, no verdict mappings.
        for gone in ("MUST", "NEVER", "do NOT", "at most ONE",
                     "re-check ONCE", "more than twice", "one sampling window",
                     "→ '"):
            assert gone not in _EVIDENCE_SEMANTICS_PROMPT, gone

    def test_receipts_speak_for_the_mechanism(self):
        assert "speaks for the mechanism, not the outcome" in _EVIDENCE_SEMANTICS_PROMPT
        assert "reported Success while delivering no fault" in _EVIDENCE_SEMANTICS_PROMPT
        assert "points to the failed layer" in _EVIDENCE_SEMANTICS_PROMPT

    def test_derived_labels_render_quantities(self):
        assert "the quantities are the evidence" in _EVIDENCE_SEMANTICS_PROMPT
        assert "display form" in _EVIDENCE_SEMANTICS_PROMPT

    def test_propagation_predates_information(self):
        # Physical delay explains early observations — an allowance, not a
        # fixed retry count.
        assert "predates propagation says nothing yet" in _EVIDENCE_SEMANTICS_PROMPT

    # test_remember_mirrors_effect_decides removed in the 2026-09-20
    # verifier cleanup (pass-4): the REMEMBER mirror it pinned is gone.
    # The mechanism-vs-outcome wording it guarded stays pinned on its
    # real carrier by the assertion above (L166-family: "speaks for the
    # mechanism, not the outcome" in _EVIDENCE_SEMANTICS_PROMPT).

    def test_effect_decides_prompt_stays_tool_agnostic(self):
        # The CONCRETE_TOOLS guard extended to the new prompt: it conveys
        # judgement (what counts as evidence), never which tool to call.
        found = [t for t in CONCRETE_TOOLS if t in _EVIDENCE_SEMANTICS_PROMPT]
        assert found == [], (
            f"judgement prompts must not name tools: {found}"
        )


class TestRecoverAttributionContract:
    """Recover Layer-2 judges by attribution, not by a snapshot wait.

    The old timing-only protocol exited with "a re-check after the delay
    still shows incomplete recovery -> partial", misjudging the propagation
    COST of recovery as recovery failure (recover-d93a4ddf: cause revoked
    instantly, rollout convergence takes minutes, verdict was partial
    although attribution in the model's reasoning was correct). The contract
    mirrors the injection verifier's fourth principle: one claim, three
    elements, burden discharged once each element has evidence.
    """

    def test_the_single_claim_decomposes_into_three_elements(self):
        text = get_recover_delay_section()
        assert "evidence chain for ONE claim" in text
        assert "Cause undone" in text
        assert "Residual attribution" in text
        assert "Coverage restored" in text

    def test_burden_discharged_replaces_the_timing_exit_rule(self):
        text = get_recover_delay_section()
        assert "burden is discharged" in text
        # The old timing-only exit rule is gone.
        assert 'Only conclude "partial" when a re-check AFTER that delay' not in text

    def test_propagation_cost_is_never_recorded_as_partial(self):
        text = get_recover_delay_section()
        assert "recovery propagation cost" in text
        assert "fault residual" in text
        assert "clean-attribution tail must never be recorded as partial" in text

    def test_wait_judgement_survives_the_rewrite(self):
        """Attribution replaces the exit rule, not the reading discipline."""
        text = get_recover_delay_section()
        assert "let time elapse" in text
        assert "one reading" in text
        assert "prove nothing" in text

    def test_output_section_separates_facts_from_judgement(self):
        """Mirror of the injection verifier: a partial checklist item (e.g.
        convergence still in progress) must not mechanically aggregate into
        a partial overall — that aggregation produced recover-d93a4ddf."""
        text = get_recover_output_format_section()
        assert "Checklist = OBSERVED FACTS" in text
        assert "Overall = HOLISTIC JUDGMENT" in text
        assert "converging recovery tail is NOT partial" in text

    def test_recover_prompt_closes_on_the_output_contract(self):
        """pass-5 (2026-09-20): the recover REMEMBER recency mirror was
        deleted — every bullet restated a named carrier, and the ReAct
        loop's message tail owns recency. What the deletion must NOT do is
        silently change where the prompt ends: the machine-parsed Output
        contract is now the closing block, the shape the parser and the
        submit tool actually consume."""
        from chaos_agent.agent.prompts.sections.recovery import (
            build_recover_verifier_system_prompt,
        )

        prompt = build_recover_verifier_system_prompt()
        assert "# REMEMBER" not in prompt
        assert prompt.rstrip().endswith("parsed programmatically.")
        # Carrier spot-check for the two bullets whose carriers sat farthest
        # from the head: primary evidence and attribution both remain
        # stated in their functional sections.
        assert "NOT generic health" in prompt
        assert "attributed to recovery propagation" in prompt
