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
    get_verification_strategy_section,
    get_nl_mode_section,
    get_safety_section,
    get_actions_section,
    get_tools_section,
    get_output_section,
    get_k8s_connection_section,
    get_guidelines_section,
    get_skill_index_section,
    get_replan_section,
    get_replan_directive_for_execution,
    get_execution_directives_section,
)
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
from chaos_agent.agent.prompts.sections.verification import (
    get_verifier_role_section,
    get_verifier_critical_rules_section,
    get_verifier_critical_rules_reminder_section,
    get_verifier_tools_section,
    get_verifier_layer2_section,
    get_verifier_output_format_section,
    get_verifier_kubeconfig_section,
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
    # Phase 4 slimming:
    #   * role             → brief variant (≤12 lines, keeps kube-system / Safety Rules tokens)
    #   * safety           → hard_only variant (Hard Rules + Caution Compliance only)
    #   * verification     → brief variant (5-line principles; full heuristics in knowledge doc)
    #   * guidelines       → no method-switching block (Phase 1 doesn't execute)
    #   * failure_modes    → dropped (sourced on demand from failure-modes.md)
    # PATD: skill index is now in stable section (unchanged across turns).
    # Previously it was in dynamic section + P2 tool_result injection,
    # creating 3× redundancy (system prompt + P2 + tool docstring).
    stable_sections = [
        get_role_section(brief=True),
        get_experience_section(),
        get_knowledge_summary_section(),
        get_skill_index_section(skill_catalog),
    ]
    stable_sections.extend([
        get_workflow_section(),
        get_verification_strategy_section(brief=True),
        get_safety_section(level="hard_only"),
        get_tools_section(phase=1),
        get_output_section(),
        get_k8s_connection_section(),
        get_guidelines_section(include_method_switching=False),
    ])

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
    **kwargs,
) -> str:
    """Build execute_loop system prompt.

    Reuses main prompt structure but excludes planning-specific sections
    (chat_routing, workflow, nl_mode) and appends execution-phase directives.

    Args:
        skill_catalog: The available skills catalog string.
        skill_name: Active skill name.
        plan: Execution plan text.
        plan_path: Path to saved plan file.
        structured_params_hint: Pre-defined scope/target/action hint from CLI
            structured params (e.g., "scope=pod, target=cpu, action=fullload").
            When set, the LLM should use these parameters instead of inferring.
    """
    # Phase 4 slimming for execute_loop:
    #   * verification_strategy → dropped (verifier_loop owns full heuristics)
    #   * failure_modes         → dropped (sourced on demand from failure-modes.md)
    #   * safety                → hard_only (executor still bound by Hard Rules)
    sections = [
        get_executor_role_section(),
        get_experience_section(),
        get_knowledge_summary_section(),
        get_safety_section(level="hard_only"),
        get_tools_section(phase=2),
        get_output_section(),
        get_k8s_connection_section(),
        get_guidelines_section(include_method_switching=True),
    ]

    # Inject env info if provided
    if kwargs.get("env_info"):
        sections.insert(1, get_env_section(kwargs["env_info"]))

    # PATD: Phase 2 only needs T1 — active skill name + key directive.
    # Previously injected full catalog (433 chars) even though Phase 2
    # never selects skills (selection is Phase 1's job).
    if skill_name:
        sections.append(f"Active skill: {skill_name}")
        sections.append("Follow the use-case steps exactly. Do not improvise fault injection operations.")
    else:
        # Fallback when no skill is active (shouldn't happen in normal flow)
        sections.append(get_skill_index_section(skill_catalog))

    # Append execution-specific directives (extracted section function)
    sections.append(
        "\n---\n"
        + get_execution_directives_section(
            skill_name=skill_name,
            structured_params_hint=structured_params_hint,
            plan=plan,
            plan_path=plan_path,
        )
    )

    # Append replan directive for Phase 2
    sections.append(get_replan_directive_for_execution())

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
        # U-shaped attention: CRITICAL rules at BEGINNING (primacy effect)
        get_verifier_role_section(),
        get_verifier_critical_rules_section(),
        experience if experience else "",
        get_knowledge_summary_section(),
        get_verifier_tools_section(),
        get_verifier_layer2_section(),
        # Compact merged section replaces 5 separate sections (P2-3)
        get_verification_heuristics_compact_section(),
        get_verifier_output_format_section(),
        get_verifier_kubeconfig_section(),
        # U-shaped attention: CRITICAL rules at END (recency effect)
        get_verifier_critical_rules_reminder_section(),
    ]
    prompt = "\n\n".join(p for p in parts if p)
    return _enforce_prompt_budget(prompt, PromptMode.VERIFICATION)


def build_intent_clarification_prompt(
    fault_intent: dict | None = None,
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
        # U-shaped attention: CRITICAL rules at BEGINNING (primacy effect)
        get_intent_role_section(),
        get_intent_critical_rules_section(),
        get_intent_safety_section(),
        # Middle zone: dialogue modes, convergence, tools, output
        get_intent_dialogue_modes_section(),
        get_intent_convergence_section(),
        get_intent_tools_section(),
        get_intent_output_section(),
    ]

    # Dynamic sections (below cache boundary — may change between turns)
    dynamic_sections = []
    completeness = get_intent_completeness_section(fault_intent)
    if completeness:
        dynamic_sections.append(completeness)

    parts = [s for s in stable_sections if s]
    parts.append(CACHE_BOUNDARY.strip())
    parts.extend(s for s in dynamic_sections if s)
    # U-shaped attention: CRITICAL rules at END (recency effect)
    parts.append(get_intent_critical_rules_reminder_section())

    prompt = "\n\n".join(parts)
    return _enforce_prompt_budget(prompt, PromptMode.INTENT)


def build_plan_builder_prompt(
    collected_faults: list | None = None,
    fault_spec=None,
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



