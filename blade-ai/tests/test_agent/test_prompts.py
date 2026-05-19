"""Tests for system prompt templates."""

from chaos_agent.agent.prompts import (
    INJECT_SYSTEM_PROMPT,
    VERIFIER_PROMPT,
    build_inject_system_prompt,
    build_intent_clarification_prompt,
    get_role_section,
    get_workflow_section,
    get_nl_mode_section,
    get_safety_section,
    get_actions_section,
    get_tools_section,
    get_output_section,
    get_k8s_connection_section,
    get_guidelines_section,
    get_env_section,
)
from chaos_agent.agent.prompts.modes import PromptMode
from chaos_agent.agent.prompts.sections.intent import (
    get_intent_role_section,
    get_intent_critical_rules_section,
    get_intent_safety_section,
    get_intent_dialogue_modes_section,
    get_intent_convergence_section,
    get_intent_tools_section,
    get_intent_output_section,
    get_intent_completeness_section,
    get_intent_critical_rules_reminder_section,
)


class TestInjectSystemPrompt:
    """Test INJECT_SYSTEM_PROMPT (deprecated backward-compat constant)."""

    def test_contains_skill_index_section(self):
        """PATD: skill index is now in stable section, not a {skill_catalog} placeholder.

        The deprecated INJECT_SYSTEM_PROMPT constant was built with
        skill_catalog="{skill_catalog}" which produced a raw fallback.
        PATD replaces this with get_skill_index_section() which parses
        "- name: description" format and produces a structured Skill Index.
        """
        # Skill Index section is present (PATD stable section)
        assert "Skill Index" in INJECT_SYSTEM_PROMPT

    def test_contains_safety_rules(self):
        assert "kube-system" in INJECT_SYSTEM_PROMPT
        assert "Safety Rules" in INJECT_SYSTEM_PROMPT

    def test_contains_activate_skill_instruction(self):
        assert "activate_skill" in INJECT_SYSTEM_PROMPT

    def test_contains_workflow_steps(self):
        assert "Analyze" in INJECT_SYSTEM_PROMPT
        assert "Activate" in INJECT_SYSTEM_PROMPT
        assert "Verify" in INJECT_SYSTEM_PROMPT


class TestVerifierPrompt:
    """Test VERIFIER_PROMPT."""

    def test_contains_verification_steps(self):
        assert "blade_status" in VERIFIER_PROMPT

    def test_contains_structured_output(self):
        assert "verified" in VERIFIER_PROMPT
        assert "Layer1" in VERIFIER_PROMPT
        assert "Layer2" in VERIFIER_PROMPT


class TestSectionFunctions:
    """Test individual section functions (迁移点 1/2/3/4)."""

    def test_role_section_not_empty(self):
        assert len(get_role_section()) > 0
        assert "Chaos Engineering Agent" in get_role_section()

    def test_workflow_section_contains_phases(self):
        section = get_workflow_section()
        assert "Phase 1" in section
        assert "Phase 2" in section
        assert "activate_skill" in section

    def test_nl_mode_section_contains_extract(self):
        section = get_nl_mode_section()
        assert "Extract" in section
        assert "ambiguous" in section

    def test_safety_section_contains_rules(self):
        section = get_safety_section()
        assert "kube-system" in section
        assert "NEVER" in section
        assert "ALWAYS" in section

    def test_actions_section_contains_scope_matching(self):
        """迁移点 2: 审慎性指导"""
        section = get_actions_section()
        assert "Scope Matching" in section
        assert "Irreversible Operations" in section
        assert "Progressive Caution" in section
        assert "blast radius" in section

    def test_tools_section_contains_priority(self):
        """迁移点 3: 工具使用规范"""
        section = get_tools_section()
        assert "Tool Selection Priority" in section
        assert "Parallel Calls" in section
        assert "Avoid Redundancy" in section
        assert "read_skill_resource" in section
        assert "Resource Lookup Priority" in section

    def test_output_section_contains_style(self):
        """迁移点 4: 沟通效率与输出风格"""
        section = get_output_section()
        assert "Lead with conclusions" in section
        assert "Structured results" in section
        assert "blade_uid" in section

    def test_k8s_connection_section_contains_kubeconfig(self):
        section = get_k8s_connection_section()
        assert "kubeconfig" in section
        assert "namespace" in section

    def test_guidelines_section_contains_blade_uid(self):
        section = get_guidelines_section()
        assert "blade UID" in section
        assert "improvise" in section

    def test_env_section_format(self):
        section = get_env_section({"blade_version": "1.7.0", "k8s_available": True})
        assert "## Environment" in section
        assert "blade_version: 1.7.0" in section
        assert "k8s_available: True" in section


class TestBuildInjectSystemPrompt:
    """Test the prompt assembler (迁移点 1)."""

    def test_basic_assembly(self):
        prompt = build_inject_system_prompt(skill_catalog="- pod-kill: Pod kill skill")
        assert "Chaos Engineering Agent" in prompt
        assert "pod-kill" in prompt
        assert "Skill Index" in prompt

    def test_contains_all_sections(self):
        prompt = build_inject_system_prompt(skill_catalog="test-skill")
        # All major section headers should be present
        assert "Workflow" in prompt
        assert "Natural Language Mode" in prompt
        assert "Safety Rules" in prompt
        assert "Executing Actions with Care" in prompt
        assert "Tool Usage Guidelines" in prompt
        assert "Communication Style" in prompt
        assert "K8s Cluster Connection" in prompt
        assert "Important Guidelines" in prompt

    def test_with_env_info(self):
        """迁移点 7: 环境信息注入"""
        prompt = build_inject_system_prompt(
            skill_catalog="test-skill",
            env_info={"blade_version": "1.7.0", "k8s_available": True},
        )
        assert "## Environment" in prompt
        assert "blade_version: 1.7.0" in prompt
        # Environment section should appear early (after role section)
        role_pos = prompt.find("Chaos Engineering Agent")
        env_pos = prompt.find("## Environment")
        assert env_pos > role_pos

    def test_without_env_info_no_environment_section(self):
        prompt = build_inject_system_prompt(skill_catalog="test-skill")
        assert "## Environment" not in prompt

    def test_no_empty_sections(self):
        """All sections should produce non-empty output."""
        prompt = build_inject_system_prompt(skill_catalog="test-skill")
        # No double newlines from empty sections
        assert "\n\n\n\n" not in prompt

    def test_backward_compat_with_old_constant(self):
        """INJECT_SYSTEM_PROMPT still contains key content for backward compat."""
        assert "Chaos Engineering Agent" in INJECT_SYSTEM_PROMPT
        assert "Skill Index" in INJECT_SYSTEM_PROMPT


class TestIntentClarificationSectionFunctions:
    """Test intent clarification section functions — English, U-shaped."""

    def test_role_section_english(self):
        section = get_intent_role_section()
        assert "Blade AI" in section
        assert "chaos engineering" in section
        # Chinese output instruction present
        assert "简体中文" in section
        # NOT a classifier assertion
        assert "NOT a classifier" in section

    def test_critical_rules_section_has_5_rules(self):
        section = get_intent_critical_rules_section()
        assert "CRITICAL RULES" in section
        # 5 specific rules present
        assert "NEVER re-ask" in section
        assert "summarize intent" in section
        assert "classify_intent is ONLY" in section
        assert "protected namespaces" in section
        assert "Single routing action" in section
        # Execution keywords present (Chinese — user-facing trigger words)
        assert "开始" in section or "执行" in section

    def test_safety_section_has_key_rules(self):
        section = get_intent_safety_section()
        assert "verify the target" in section
        assert "uncertain" in section
        assert "test/dev namespaces" in section

    def test_dialogue_modes_section_has_three_modes(self):
        section = get_intent_dialogue_modes_section()
        assert "Chat Mode" in section
        assert "Intent Routing" in section
        assert "Cluster Query" in section
        # classify_intent restriction
        assert "Do NOT call classify_intent" in section

    def test_convergence_section_has_principles(self):
        section = get_intent_convergence_section()
        assert "ONE question at a time" in section
        assert "submit_fault_intent" in section
        assert "hypothesis" in section
        assert "success_criteria" in section

    def test_tools_section_has_available_not_available(self):
        section = get_intent_tools_section()
        assert "Available Tools" in section
        assert "NOT Available" in section
        assert "blade_create" in section
        assert "kubectl" in section

    def test_output_section_has_content_field(self):
        section = get_intent_output_section()
        assert "content field" in section
        assert "简体中文" in section

    def test_completeness_section_all_filled(self):
        section = get_intent_completeness_section({
            "scope": "pod", "target": "cpu", "action": "fullload",
            "namespace": "default", "labels": "app=test",
        })
        assert "⚠️ ALL REQUIRED" in section
        assert "Confirmed Parameters" in section

    def test_completeness_section_missing(self):
        section = get_intent_completeness_section({"scope": "pod"})
        assert "Still missing" in section

    def test_completeness_section_none(self):
        section = get_intent_completeness_section(None)
        assert section == ""

    def test_reminder_section_recaps_rules(self):
        section = get_intent_critical_rules_reminder_section()
        assert "REMINDER" in section
        assert "Do NOT re-ask" in section
        assert "intent summary" in section
        assert "classify_intent is ONLY" in section
        assert "kube-system" in section


class TestBuildIntentClarificationPrompt:
    """Test intent clarification prompt builder — U-shaped assembly."""

    def test_basic_assembly(self):
        prompt = build_intent_clarification_prompt()
        assert "Blade AI" in prompt
        assert "CRITICAL RULES" in prompt
        assert "REMINDER" in prompt

    def test_u_shaped_structure(self):
        """CRITICAL rules at beginning + reminder at end."""
        prompt = build_intent_clarification_prompt()
        # CRITICAL rules near beginning (primacy zone)
        critical_pos = prompt.find("CRITICAL RULES")
        # REMINDER near end (recency zone)
        reminder_pos = prompt.find("REMINDER — Critical Rules Recap")
        assert critical_pos > 0
        assert reminder_pos > 0
        assert reminder_pos > critical_pos
        # Reminder should be in the last 20% of the prompt
        assert reminder_pos > len(prompt) * 0.8

    def test_cache_boundary_present(self):
        """CACHE_BOUNDARY separates stable from dynamic sections."""
        prompt = build_intent_clarification_prompt()
        assert "BLADE_AI_CACHE_BOUNDARY" in prompt

    def test_with_fault_intent(self):
        """Dynamic section (confirmed parameters) injected below cache boundary."""
        prompt = build_intent_clarification_prompt(
            fault_intent={"scope": "pod", "target": "cpu", "action": "fullload", "namespace": "default"},
        )
        assert "Confirmed Parameters" in prompt
        assert "scope: pod" in prompt

    def test_without_fault_intent_no_confirmed_block(self):
        """No fault_intent → no dynamic Confirmed Parameters section (## header)."""
        prompt = build_intent_clarification_prompt()
        # "Confirmed Parameters" appears in CRITICAL RULES text,
        # but the ## header section should NOT be present without fault_intent.
        assert "## Confirmed Parameters" not in prompt

    def test_prompt_mode_intent(self):
        """PromptMode.INTENT routes to build_intent_clarification_prompt."""
        from chaos_agent.agent.prompts.builders import build_system_prompt
        prompt = build_system_prompt(PromptMode.INTENT)
        assert "Blade AI" in prompt
        assert "CRITICAL RULES" in prompt
