"""Prompt builders — assemble section functions into complete system prompts.

Each builder corresponds to a PromptMode:
  - build_inject_system_prompt()  → FULL (agent_loop)
  - build_execute_system_prompt() → MINIMAL (execute_loop)
  - build_verifier_prompt()       → VERIFICATION (verifier_loop)
  - build_intent_clarification_prompt() → INTENT (intent_clarification)

Unified entry point:
  - build_system_prompt(mode, ...) → routes to the correct builder by PromptMode

Skill loading follows PATD (Pipeline-Aware Progressive Skill Delivery):
  - T0: Skill index (name + full description) in stable section of system prompt
  - T1: Active skill name only in execute prompt (Phase 2 doesn't select skills)
  - T2: Skill use-case content loaded on demand via activate_skill tool
"""

import logging
import warnings

from chaos_agent.agent.prompts.constants import (
    CACHE_BOUNDARY,
    MAX_SYSTEM_PROMPT_CHARS,
)
from chaos_agent.agent.prompts.modes import PromptMode
from chaos_agent.agent.prompts.sections import (
    get_role_section,
    get_executor_role_section,
    get_env_section,
    get_knowledge_summary_section,
    get_experience_section,
    get_workflow_section,
    get_core_principles_section,
    get_remember_section,
    get_executor_core_principles_section,
    get_executor_remember_section,
    get_safety_section,
    get_tools_section,
    get_guidelines_section,
    get_skill_index_section,
    get_replan_section,
    get_replan_directive_for_execution,
    get_execution_directives_section,
)
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
    get_intent_reflection_section,
    get_intent_output_section,
    get_intent_completeness_section,
    get_intent_reminder_section,
)
from chaos_agent.agent.prompts.sections.plan_builder import (
    get_plan_builder_role_section,
    get_plan_builder_critical_rules_section,
    get_plan_builder_workflow_section,
    get_plan_builder_tools_section,
    get_plan_builder_output_format_section,
    get_plan_builder_progress_section,
    get_plan_builder_critical_rules_reminder_section,
)
from chaos_agent.agent.prompts.sections.verification import (
    get_verifier_role_section,
    get_verifier_core_principles_section,
    get_verifier_remember_section,
    get_verifier_tools_section,
    get_verifier_layer2_section,
    get_verifier_output_format_section,
)
from chaos_agent.agent.prompts.sections.workflow import (
    get_verification_heuristics_compact_section,
)

logger = logging.getLogger(__name__)


def _enforce_prompt_budget(prompt: str, mode: PromptMode) -> str:
    """Truncate prompt if it exceeds the global character budget.

    Truncation priority (largest dynamic section first):
    1. Experience section (AGENT.md content)
    2. Knowledge summary section
    3. Skill catalog (if excessively long)
    """
    if len(prompt) <= MAX_SYSTEM_PROMPT_CHARS:
        return prompt

    warnings.warn(
        f"System prompt ({len(prompt)} chars) exceeds budget ({MAX_SYSTEM_PROMPT_CHARS}). "
        f"Truncating dynamic sections for {mode.value} mode.",
        RuntimeWarning,
        stacklevel=3,
    )

    # Simple truncation: cut the prompt at the budget limit
    # In production, would re-assemble with truncated dynamic sections
    return prompt[:MAX_SYSTEM_PROMPT_CHARS]


def build_inject_system_prompt(
    skill_catalog: str,
    *,
    input_is_nl: bool = False,
    **kwargs,
) -> str:
    """Dynamically assemble the inject system prompt from sections.

    This follows the Claude Code pattern of section-based prompt composition
    (cf. src/utils/systemPrompt.ts buildEffectiveSystemPrompt).

    Args:
        skill_catalog: The available skills catalog string.
        input_is_nl: When True, the user request arrived via the NL entry
            point. Currently informational — the NL Mode section is included
            unconditionally for backward compatibility with test contracts.
        **kwargs: Optional keyword arguments:
            env_info (dict): Runtime environment info to inject.
            replan_context (dict): Phase 2 → Phase 1 error feedback.
            replan_history (list): Prior replan attempts.

    Returns:
        Assembled system prompt string.
    """
    # ``input_is_nl`` is accepted for API symmetry with builder callers;
    # the NL Mode section stays unconditional for now because
    # tests/test_agent/test_prompts.py freezes its presence.
    _ = input_is_nl

    # Stable sections (above cache boundary — reusable across turns).
    #
    # Tool abstraction boundary:
    # - Internal framework APIs (activate_skill, finish_planning, save_fault_plan,
    #   propose_plan_change, read_skill_resource, read_knowledge_resource) —
    #   keep original names in ALL sections. These are the agent's own interface.
    # - External CLI tools (blade_create, blade_destroy, blade_status, kubectl) —
    #   abstract in principle sections (Workflow/Guidelines/Safety/Replan) using
    #   generic terms (injection tool, cluster query tool, experiment ID).
    #   Concrete names appear ONLY in Phase 2 Tools section, execution
    #   directives, and skill case files. Phase 1 Tools section is also
    #   tool-agnostic.
    # When adding a new injection tool (chaos-mesh, litmus, etc.), only update
    # the Phase 2 Tools section + execution directives + skill catalogue —
    #   principle sections need no changes.
    stable_sections = [
        get_role_section(),
        get_core_principles_section(),
        get_experience_section(),
        get_knowledge_summary_section(),
        get_skill_index_section(skill_catalog),
    ]
    stable_sections.extend([
        get_workflow_section(),
        get_safety_section(level="hard_only"),
        get_tools_section(phase=1),
        get_guidelines_section(include_method_switching=False, phase=1),
    ])
    # REMEMBER segment — U-shaped attention recency zone.
    # Reinforces the anti-hallucination principles from Core Principles
    # and Workflow Ground Truth.
    stable_sections.append(get_remember_section())

    # Dynamic sections (below cache boundary — may change between turns)
    dynamic_sections = []
    if kwargs.get("env_info"):
        dynamic_sections.append(get_env_section(kwargs["env_info"]))

    # Replan context (Phase 2 → Phase 1 error feedback)
    replan_context = kwargs.get("replan_context")
    replan_history = kwargs.get("replan_history")
    if replan_context:
        dynamic_sections.append(get_replan_section(replan_context, replan_history))

    # skill_index_only flag is no longer used — skill index is always in
    # the stable section with full descriptions. The P2 injection pattern
    # (tool_result on first iteration) has been removed (see PATD #3).

    parts = [s for s in stable_sections if s]
    parts.append(CACHE_BOUNDARY.strip())
    parts.extend(s for s in dynamic_sections if s)

    prompt = "\n\n".join(parts)
    return _enforce_prompt_budget(prompt, PromptMode.FULL)


def build_execute_system_prompt(
    skill_catalog: str,
    skill_name: str = "",
    plan: str = "",
    plan_path: str = "",
    structured_params_hint: str = "",
    user_params_hint: str = "",
    **kwargs,
) -> str:
    """Build execute_loop system prompt with U-shaped attention.

    Same pattern as build_inject_system_prompt, build_verifier_prompt,
    build_intent_clarification_prompt, and build_plan_builder_prompt:
    Core Principles at BEGINNING (primacy) + REMEMBER at END (recency).

    Args:
        skill_catalog: The available skills catalog string.
        skill_name: Active skill name.
        plan: Execution plan text.
        plan_path: Path to saved plan file.
        structured_params_hint: Pre-defined scope/target/action hint from CLI
            structured params (e.g., "scope=pod, target=cpu, action=fullload").
            When set, the LLM should use these parameters instead of inferring.
    """
    # U-shaped attention: Core Principles at BEGINNING (primacy effect)
    sections = [
        get_executor_role_section(),
        get_executor_core_principles_section(),
        get_experience_section(),
        get_knowledge_summary_section(),
        get_safety_section(level="hard_only"),
        get_tools_section(phase=2),
        get_guidelines_section(include_method_switching=True, phase=2),
    ]

    # Inject env info if provided (after Core Principles, before Experience)
    if kwargs.get("env_info"):
        sections.insert(2, get_env_section(kwargs["env_info"]))

    # Fallback: inject full catalog when no skill is active
    # (shouldn't happen in normal flow — Phase 1 selects the skill)
    if not skill_name:
        sections.append(get_skill_index_section(skill_catalog))

    # Execution-specific directives (skill_name injected here, not above)
    sections.append(
        "\n---\n"
        + get_execution_directives_section(
            skill_name=skill_name,
            structured_params_hint=structured_params_hint,
            user_params_hint=user_params_hint,
            plan=plan,
            plan_path=plan_path,
        )
    )

    # Replan directive
    sections.append(get_replan_directive_for_execution())

    # U-shaped attention: REMEMBER at END (recency zone)
    sections.append(get_executor_remember_section())

    prompt = "\n\n".join(s for s in sections if s)
    return _enforce_prompt_budget(prompt, PromptMode.MINIMAL)


def build_verifier_prompt() -> str:
    """Build the verifier system prompt by composing section functions.

    Uses shared sub-sections from workflow.py (delay, iteration, container,
    method priority) to eliminate copy-paste duplication, while maintaining
    the same level of detail as the original inline version (P2 principle).

    MUST preserve 'JSON' keyword for Bailian API response_format compatibility.
    """
    experience = get_experience_section()

    parts = [
        # U-shaped attention: Core Principles at BEGINNING (primacy)
        get_verifier_role_section(),
        get_verifier_core_principles_section(),
        experience if experience else "",
        get_knowledge_summary_section(),
        get_verifier_tools_section(),
        get_verifier_layer2_section(),
        # Compact merged section replaces 5 separate sections (P2-3)
        get_verification_heuristics_compact_section(),
        get_verifier_output_format_section(),
        # U-shaped attention: REMEMBER at END (recency)
        get_verifier_remember_section(),
    ]
    prompt = "\n\n".join(p for p in parts if p)
    return _enforce_prompt_budget(prompt, PromptMode.VERIFICATION)


def build_intent_clarification_prompt(
    fault_intent: dict | None = None,
    skill_catalog: str = "",
    **kwargs,
) -> str:
    """Build intent_clarification system prompt using U-shaped composition.

    Follows the same architecture pattern as build_verifier_prompt():
    CRITICAL rules at BEGINNING (primacy) + END (recency), with
    dialogue modes, convergence logic, and tools in the middle.

    Dynamic section (completeness signal + confirmed parameters) is
    placed below CACHE_BOUNDARY so stable sections can be cached
    across turns. The CRITICAL rules reminder occupies the very end
    of the prompt (after all dynamic content) for maximum recency effect.

    Args:
        fault_intent: Already-confirmed fault parameters from previous
            dialogue turns. When present, a "Confirmed Parameters" block
            and a completeness signal (missing/all-filled) are injected
            in the dynamic section below the cache boundary.

    Returns:
        Assembled system prompt string.
    """
    # Stable sections (above cache boundary — reusable across turns)
    stable_sections = [
        # §1-2: Role + Priorities at BEGINNING (primacy effect)
        get_intent_role_section(),
        get_intent_priorities_section(),
        # §3-9: Routing, model, flows, tools
        get_intent_dialogue_routing_section(),
        get_intent_parameter_model_section(),
        get_intent_inject_flow_section(),
        get_intent_recover_flow_section(),
        get_intent_batch_flow_section(),
        get_intent_operation_freshness_section(),
        get_intent_tools_section(),
        get_intent_reflection_section(),
        get_intent_output_section(),
        # §12: Skill Index (dynamic catalog)
        get_skill_index_section(skill_catalog),
    ]

    # Dynamic sections (below cache boundary — may change between turns)
    dynamic_sections = []
    completeness = get_intent_completeness_section(
        fault_intent,
        batch_submit_args=kwargs.get("batch_submit_args"),
    )
    if completeness:
        dynamic_sections.append(completeness)

    parts = [s for s in stable_sections if s]
    parts.append(CACHE_BOUNDARY.strip())
    parts.extend(s for s in dynamic_sections if s)
    # §13: Reminder at END (recency effect)
    parts.append(get_intent_reminder_section())

    prompt = "\n\n".join(parts)
    return _enforce_prompt_budget(prompt, PromptMode.INTENT)


def build_plan_builder_prompt(
    collected_faults: list | None = None,
    fault_spec=None,
    skill_catalog: str = "",
    **kwargs,
) -> str:
    """Build plan_builder system prompt using U-shaped composition.

    Same pattern as build_intent_clarification_prompt():
    CRITICAL at BEGINNING + END, dynamic below CACHE_BOUNDARY.
    """
    stable_sections = [
        get_plan_builder_role_section(),
        get_plan_builder_critical_rules_section(),
        get_plan_builder_workflow_section(),
        get_plan_builder_tools_section(),
        get_skill_index_section(skill_catalog),
        get_plan_builder_output_format_section(),
    ]

    dynamic_sections: list[str] = []
    progress = get_plan_builder_progress_section(
        collected_faults or [], fault_spec
    )
    if progress:
        dynamic_sections.append(progress)

    parts = [s for s in stable_sections if s]
    parts.append(CACHE_BOUNDARY.strip())
    parts.extend(s for s in dynamic_sections if s)
    parts.append(get_plan_builder_critical_rules_reminder_section())

    prompt = "\n\n".join(parts)
    return _enforce_prompt_budget(prompt, PromptMode.PLAN_BUILDER)


# ---------------------------------------------------------------------------
# P1: PromptMode-driven builder dispatch
# ---------------------------------------------------------------------------

_BUILDER_DISPATCH = {
    PromptMode.FULL: build_inject_system_prompt,
    PromptMode.MINIMAL: build_execute_system_prompt,
    PromptMode.VERIFICATION: build_verifier_prompt,
    PromptMode.INTENT: build_intent_clarification_prompt,
    PromptMode.PLAN_BUILDER: build_plan_builder_prompt,
}


def build_system_prompt(mode: PromptMode, **kwargs) -> str:
    """Unified prompt builder entry point — routes by PromptMode.

    This is the P1 integration: all nodes should call this function
    instead of directly calling specific builders, so that PromptMode
    drives builder selection consistently.

    Args:
        mode: The PromptMode for the current workflow stage.
        **kwargs: Forwarded to the mode-specific builder.
            Common kwargs:
              - skill_catalog (str): Skill catalog string (FULL, MINIMAL)
              - env_info (dict): Runtime environment info (FULL, MINIMAL)
              - skill_name (str): Active skill name (MINIMAL)
              - plan (str): Execution plan (MINIMAL)
              - plan_path (str): Plan file path (MINIMAL)
              - fault_intent (dict): Already-confirmed fault parameters (INTENT)

    Returns:
        Assembled system prompt string.

    Raises:
        ValueError: If mode is not recognized.
    """
    builder = _BUILDER_DISPATCH.get(mode)
    if builder is None:
        raise ValueError(
            f"Unknown PromptMode: {mode!r}. "
            f"Expected one of: {', '.join(m.value for m in PromptMode)}"
        )
    return builder(**kwargs)



