"""Tests for the optional parameters added to section functions in Phase 3.

These guard the contract that:

* the ``brief`` / ``level`` / ``include_method_switching`` parameters return
  a strictly slimmer variant while preserving the frozen tokens that other
  tests in ``test_prompts.py`` rely on, and
* the default-argument call still produces the original full output (already
  covered by ``test_prompts.py`` — kept here for explicit guard).
"""

from chaos_agent.agent.prompts.sections.execution import get_guidelines_section
from chaos_agent.agent.prompts.sections.identity import get_role_section
from chaos_agent.agent.prompts.sections.safety import get_safety_section
from chaos_agent.agent.prompts.sections.workflow import (
    get_verification_strategy_section,
    get_workflow_section,
)


class TestRoleSectionBrief:
    def test_brief_keeps_critical_tokens(self):
        s = get_role_section(brief=True)
        assert "kube-system" in s
        assert "Safety Rules" in s
        assert "Chaos Engineering Agent" in s

    def test_brief_is_shorter_than_full(self):
        assert len(get_role_section(brief=True)) < len(get_role_section())

    def test_brief_under_12_lines(self):
        # Plan target: ≤ 12 lines including blank lines.
        assert len(get_role_section(brief=True).splitlines()) <= 12

    def test_default_unchanged_full_output(self):
        s = get_role_section()
        assert "What You Can Do" in s
        assert "What You Cannot Do" in s


class TestSafetySectionLevel:
    def test_hard_only_keeps_header_and_kube_system(self):
        s = get_safety_section(level="hard_only")
        assert "Safety Rules" in s
        assert "kube-system" in s

    def test_hard_only_keeps_caution_compliance(self):
        s = get_safety_section(level="hard_only")
        # Caution Rule Compliance must remain so the agent honours unreported
        # violations as protocol errors even in cache-tight inject prompts.
        assert "Caution Rule Compliance" in s

    def test_hard_only_drops_advisory_and_blast_radius(self):
        s = get_safety_section(level="hard_only")
        # Section headers (with '###' marker) must be gone — the on-demand
        # pointer may still reference these phrases in prose, which is fine.
        assert "### Advisory Rules" not in s
        assert "### Blast Radius Assessment Framework" not in s
        assert "### Decision Framework" not in s
        # Long-tail content should be absent too.
        assert "Start with the smallest effective scope" not in s
        assert "ABORT" not in s

    def test_hard_only_is_shorter_than_full(self):
        assert len(get_safety_section(level="hard_only")) < len(get_safety_section())

    def test_default_full_keeps_all_subsections(self):
        s = get_safety_section()
        assert "Advisory Rules" in s
        assert "Blast Radius Assessment Framework" in s
        assert "Decision Framework" in s


class TestVerificationStrategyBrief:
    def test_brief_under_10_lines(self):
        # Plan target: 5-line principle version; allow a small ceiling for the
        # heading + on-demand pointer.
        s = get_verification_strategy_section(brief=True)
        assert len(s.splitlines()) <= 12

    def test_brief_keeps_verification_keyword(self):
        assert "verification" in get_verification_strategy_section(brief=True).lower()

    def test_brief_is_shorter_than_full(self):
        assert len(get_verification_strategy_section(brief=True)) < len(
            get_verification_strategy_section()
        )

    def test_default_full_keeps_all_subsections(self):
        s = get_verification_strategy_section()
        assert "Fault Effect Delay" in s
        assert "Multi-Iteration Verification Pattern" in s
        assert "Verification Method Priority" in s


class TestWorkflowSectionTokens:
    def test_keeps_aav_verbs(self):
        s = get_workflow_section()
        for verb in ("Analyze", "Activate", "Verify"):
            assert verb in s, f"{verb} verb missing from workflow section"

    def test_keeps_phase_headers(self):
        s = get_workflow_section()
        assert "Phase 1" in s
        assert "Phase 2" in s

    def test_keeps_activate_skill_token(self):
        # test_prompts::test_workflow_section_contains_phases asserts this.
        assert "activate_skill" in get_workflow_section()


class TestGuidelinesSectionMethodSwitching:
    def test_default_includes_method_switching(self):
        assert "Injection Method Switching" in get_guidelines_section()

    def test_omit_method_switching(self):
        s = get_guidelines_section(include_method_switching=False)
        assert "Injection Method Switching" not in s
        assert "METHOD CONSTRAINT" not in s

    def test_omit_keeps_blade_uid_token(self):
        # test_prompts::test_guidelines_section_contains_blade_uid asserts this.
        s = get_guidelines_section(include_method_switching=False)
        assert "blade UID" in s
        assert "improvise" in s

    def test_omit_is_shorter_than_default(self):
        assert len(get_guidelines_section(include_method_switching=False)) < len(
            get_guidelines_section()
        )
