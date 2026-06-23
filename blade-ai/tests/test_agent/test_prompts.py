"""Tests for system prompt templates."""

from chaos_agent.agent.prompts import (
    build_inject_system_prompt,
    build_intent_clarification_prompt,
    get_role_section,
    get_core_principles_section,
    get_remember_section,
    get_executor_core_principles_section,
    get_executor_remember_section,
    get_workflow_section,
    get_safety_section,
    get_tools_section,
    get_guidelines_section,
    get_env_section,
)
from chaos_agent.agent.prompts.modes import PromptMode
from chaos_agent.agent.prompts.sections.intent import (
    get_intent_role_section,
    get_intent_priorities_section,
    get_intent_dialogue_routing_section,
    get_intent_parameter_model_section,
    get_intent_inject_flow_section,
    get_intent_recover_flow_section,
    get_intent_batch_flow_section,
    get_intent_operation_freshness_section,
    get_intent_tools_section,
    get_intent_output_section,
    get_intent_completeness_section,
    get_intent_reminder_section,
)


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

    def test_safety_section_contains_rules(self):
        section = get_safety_section()
        assert "namespace blacklist" in section
        assert "NEVER" in section
        assert "ALWAYS" in section

    def test_tools_section_contains_priority(self):
        """迁移点 3: 工具使用规范"""
        section = get_tools_section(phase=1)
        assert "Tool Selection Priority" in section
        assert "Parallel Calls" in section
        assert "Avoid Redundancy" in section
        assert "read_skill_resource" in section
        assert "Timeout Protection" in section

    def test_guidelines_section_contains_follow_instructions(self):
        section = get_guidelines_section(phase=2)
        assert "improvise" in section
        assert "Runtime Feedback Priority" in section

    def test_core_principles_section_content(self):
        section = get_core_principles_section()
        assert "# Core Principles" in section
        assert "FAULT INTENT parameters are UNVERIFIED" in section
        assert "TOOL is correct" in section
        assert "finish_planning" in section

    def test_remember_section_content(self):
        section = get_remember_section()
        assert "# REMEMBER" in section
        assert "FAULT INTENT parameters are UNVERIFIED" in section
        assert "TOOL is correct" in section
        assert "finish_planning" in section
        assert "propose_plan_change" in section

    def test_core_principles_and_remember_are_aligned(self):
        """REMEMBER must reinforce the same rules as Core Principles (U-shaped attention)."""
        core = get_core_principles_section()
        remember = get_remember_section()
        # Each Core Principles rule must appear verbatim in REMEMBER
        for line in core.splitlines():
            if line.startswith("- "):
                assert line in remember, (
                    f"Core Principles rule not found in REMEMBER: {line!r}"
                )

    def test_executor_core_principles_section_content(self):
        section = get_executor_core_principles_section()
        assert "# Core Principles" in section
        assert "UNVERIFIED" in section
        assert "TOOL is right" in section
        assert "STOP" in section

    def test_executor_remember_section_content(self):
        section = get_executor_remember_section()
        assert "# REMEMBER" in section
        assert "UNVERIFIED" in section
        assert "TOOL is right" in section
        assert "STOP" in section
        assert "[REPLAN]" in section

    def test_executor_core_principles_and_remember_are_aligned(self):
        """REMEMBER must reinforce the same rules as executor Core Principles."""
        core = get_executor_core_principles_section()
        remember = get_executor_remember_section()
        for line in core.splitlines():
            if line.startswith("- "):
                assert line in remember, (
                    f"Executor Core Principles rule not found in REMEMBER: {line!r}"
                )

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
        assert "Safety Rules" in prompt
        assert "Tool Usage Guidelines" in prompt
        assert "Important Guidelines" in prompt
        # REMEMBER segment (U-shaped recency zone)
        assert "# REMEMBER" in prompt
        # Removed from Phase 1 (tool-agnostic redesign):
        # Communication Style and K8s Cluster Connection are Phase 2 only
        assert "Communication Style" not in prompt
        assert "K8s Cluster Connection" not in prompt

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


class TestIntentClarificationSectionFunctions:
    """Test intent clarification section functions — English, U-shaped."""

    def test_role_section_english(self):
        section = get_intent_role_section()
        assert "Blade AI" in section
        assert "chaos engineering" in section
        # Three intent types present (lowercase in role section)
        assert "inject" in section
        assert "batch" in section
        assert "recover" in section

    def test_priorities_section_has_3_priorities(self):
        section = get_intent_priorities_section()
        assert "Three Priorities" in section
        assert "Truthfulness" in section
        assert "Proactiveness" in section
        assert "Convergence" in section

    def test_dialogue_routing_section_has_routes(self):
        section = get_intent_dialogue_routing_section()
        assert "Dialogue Routing" in section
        assert "Recover" in section
        assert "Batch" in section
        assert "Pure text response" in section

    def test_parameter_model_section(self):
        section = get_intent_parameter_model_section()
        assert "scope" in section
        assert "target" in section
        assert "action" in section
        assert "namespace" in section

    def test_inject_flow_section(self):
        section = get_intent_inject_flow_section()
        assert "Inject Flow" in section
        assert "submit_fault_intent" in section
        assert "Probe" in section
        assert "Recommend" in section

    def test_recover_flow_section(self):
        section = get_intent_recover_flow_section()
        assert "Recover Flow" in section
        assert "recover_task" in section
        assert "task_id" in section

    def test_batch_flow_section(self):
        section = get_intent_batch_flow_section()
        assert "Batch Flow" in section
        assert "Diversity Principle" in section
        assert "fault type diversity" in section
        assert "submit_batch_intent" in section

    def test_operation_freshness_section(self):
        section = get_intent_operation_freshness_section()
        assert "Operation Freshness" in section
        assert "stale" in section
        assert "re-query" in section

    def test_tools_section_has_categories(self):
        section = get_intent_tools_section()
        assert "Probe" in section
        assert "Submit" in section
        assert "Route" in section
        assert "bound to you" in section

    def test_output_section(self):
        section = get_intent_output_section()
        assert "Chinese" in section
        assert "structured plain text" in section
        assert "emoji" in section

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
        section = get_intent_reminder_section()
        assert "REMEMBER" in section
        assert "kubectl_ro" in section
        assert "submit" in section
        assert "Probe" in section


class TestBuildIntentClarificationPrompt:
    """Test intent clarification prompt builder — U-shaped assembly."""

    def test_basic_assembly(self):
        prompt = build_intent_clarification_prompt()
        assert "Blade AI" in prompt
        assert "Three Priorities" in prompt
        assert "REMEMBER" in prompt

    def test_u_shaped_structure(self):
        """Priorities at beginning + reminder at end."""
        prompt = build_intent_clarification_prompt()
        # Priorities near beginning (primacy zone)
        priorities_pos = prompt.find("Three Priorities")
        # REMEMBER near end (recency zone)
        reminder_pos = prompt.find("# REMEMBER")
        assert priorities_pos > 0
        assert reminder_pos > 0
        assert reminder_pos > priorities_pos
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

    def test_inject_flow_in_assembled_prompt(self):
        prompt = build_intent_clarification_prompt()
        assert "Inject Flow" in prompt
        assert "Recover Flow" in prompt
        assert "Batch Flow" in prompt

    def test_prompt_mode_intent(self):
        """PromptMode.INTENT routes to build_intent_clarification_prompt."""
        from chaos_agent.agent.prompts.builders import build_system_prompt
        prompt = build_system_prompt(PromptMode.INTENT)
        assert "Blade AI" in prompt
        assert "Three Priorities" in prompt
