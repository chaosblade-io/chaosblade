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

from chaos_agent.agent.prompts.constants import (
    CACHE_BOUNDARY,
    MAX_SYSTEM_PROMPT_CHARS,
)
from chaos_agent.agent.prompts.modes import PromptMode
from chaos_agent.agent.prompts.assembly import (
    PromptPriority,
    PromptSegment,
    assemble_prompt,
)
from chaos_agent.transports import PROFILE_K8S
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
    get_intent_capability_boundary_section,
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


def _segment(mode: PromptMode, name: str, content: str, priority: PromptPriority) -> PromptSegment:
    return PromptSegment(
        name=f"{mode.value}.{name}",
        content=content,
        priority=priority,
        cacheable=priority in ("invariant", "contract"),
        source="builder",
    )


def _assemble(mode: PromptMode, sections: list[tuple[str, str, PromptPriority]]) -> str:
    """Assemble named, semantically-prioritized prompt sections."""
    return assemble_prompt(
        [_segment(mode, name, content, priority) for name, content, priority in sections],
        MAX_SYSTEM_PROMPT_CHARS,
    )


def _provider_prompt_fragment(profile: str, attr: str) -> str:
    """Assemble the ``identity`` prompt fragment contributed by providers for
    ``profile``.

    ``attr`` is "identity" — the only field on
    :class:`~chaos_agent.agent.providers.base.ProviderPrompts`. Per-method
    verify / recover wording now lives in ``verify_prompt_note`` /
    ``recover_layer2_context`` and is inserted by the nodes directly.

    All built-in providers currently return empty fragments, so this yields ""
    and is transparently filtered out by ``join(... if s)`` in the builders.
    When a new backend populates a fragment it is automatically included without
    editing any builder or shared prompt section — the "new backend = register"
    extensibility guarantee.
    """
    # Lazy import: breaks the providers ← prompts import cycle.
    from chaos_agent.agent.providers import FaultProviderRegistry

    parts = []
    for p in FaultProviderRegistry.applicable(profile):
        fragment = getattr(p.prompt_fragments(), attr, "")
        if fragment:
            parts.append(fragment)
    return "\n\n".join(parts)


def _environment_prompt_fragment(profile: str | None, phase: str) -> str:
    """Return the registered environment capability wording for one phase.

    This is separate from provider fragments: providers describe the injection
    backend while an environment profile describes target authority and the
    observation surface. Empty/unknown profiles deliberately receive an
    explicit unsupported fragment rather than a Kubernetes fallback.
    """
    from chaos_agent.agent.environment_profiles import get_environment_profile

    if profile is None:
        return ""
    environment = get_environment_profile(profile)
    if environment is None:
        return (
            "## Capability Profile\n"
            "The current environment profile is unsupported. Do not attempt "
            "injection, recovery, or baseline collection. Report the missing "
            "environment capability."
        )
    return environment.prompt_fragment(phase)


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
    profile = kwargs.get("profile", PROFILE_K8S)

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
    sections: list[tuple[str, str, PromptPriority]] = [
        ("role", get_role_section(), "invariant"),
        ("core_principles", get_core_principles_section(), "invariant"),
        ("experience", get_experience_section(), "optional"),
        ("knowledge_summary", get_knowledge_summary_section(), "optional"),
        ("skill_catalog", get_skill_index_section(skill_catalog), "optional"),
        ("workflow", get_workflow_section(), "context"),
        ("safety", get_safety_section(level="hard_only"), "invariant"),
        ("tools", get_tools_section(phase=1), "contract"),
        ("guidelines", get_guidelines_section(include_method_switching=False, phase=1), "context"),
        ("environment_profile", _environment_prompt_fragment(profile, "plan"), "context"),
        ("provider_identity", _provider_prompt_fragment(profile, "identity"), "context"),
        ("remember", get_remember_section(), "invariant"),
        ("cache_boundary", CACHE_BOUNDARY.strip(), "contract"),
    ]
    if kwargs.get("env_info"):
        sections.append(("runtime_environment", get_env_section(kwargs["env_info"]), "context"))

    fault_spec = kwargs.get("fault_spec")
    if fault_spec:
        from chaos_agent.agent.spec.fault_spec import FaultSpec

        spec = FaultSpec.from_dict(fault_spec)
        if spec is not None:
            declaration = (
                "\n\n## Planning Contract Declaration\n"
                f"FaultSpec revision {spec.revision} is authoritative and is the "
                "sole contract executed. Preserve it as-is and call "
                "`finish_planning` to complete planning. To change the target or "
                "fault type, call `propose_plan_change` with that revision and a "
                "full `proposed_fault`."
            )
            sections.append((
                "fault_contract",
                "## Reviewed FaultSpec\n"
                "Preserve this user outcome, boundaries, and constraints. Runtime "
                "evidence may correct implementation details, but it must not silently "
                "change the outcome. A material change must go through the plan-change "
                "confirmation path.\n\n"
                + str(spec.to_intent_dict())
                + declaration,
                "contract",
            ))

    replan_context = kwargs.get("replan_context")
    if replan_context:
        sections.append((
            "replan_context",
            get_replan_section(replan_context, kwargs.get("replan_history")),
            "contract",
        ))

    return _assemble(PromptMode.FULL, sections)


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
    profile = kwargs.get("profile", PROFILE_K8S)
    sections: list[tuple[str, str, PromptPriority]] = [
        ("role", get_executor_role_section(), "invariant"),
        ("core_principles", get_executor_core_principles_section(), "invariant"),
    ]
    if kwargs.get("env_info"):
        sections.append(("runtime_environment", get_env_section(kwargs["env_info"]), "context"))
    sections.extend([
        ("experience", get_experience_section(), "optional"),
        ("knowledge_summary", get_knowledge_summary_section(), "optional"),
        ("safety", get_safety_section(level="hard_only"), "invariant"),
        ("tools", get_tools_section(phase=2), "contract"),
        ("guidelines", get_guidelines_section(include_method_switching=True, phase=2), "context"),
        ("provider_identity", _provider_prompt_fragment(profile, "identity"), "context"),
        ("environment_profile", _environment_prompt_fragment(profile, "execute"), "context"),
    ])
    if not skill_name:
        sections.append(("skill_catalog", get_skill_index_section(skill_catalog), "optional"))
    sections.extend([
        ("execution_directives", "\n---\n" + get_execution_directives_section(
            skill_name=skill_name,
            structured_params_hint=structured_params_hint,
            user_params_hint=user_params_hint,
            plan=plan,
            plan_path=plan_path,
        ), "invariant"),
        ("replan_contract", get_replan_directive_for_execution(), "contract"),
        ("remember", get_executor_remember_section(), "invariant"),
    ])
    return _assemble(PromptMode.MINIMAL, sections)


def build_verifier_prompt(profile: str = PROFILE_K8S, **kwargs) -> str:
    """Build the verifier system prompt by composing section functions.

    Uses shared sub-sections from workflow.py (delay, iteration, container,
    method priority) to eliminate copy-paste duplication, while maintaining
    the same level of detail as the original inline version (P2 principle).

    ``profile`` ("k8s"|"host") selects which providers contribute verify-phase
    prompt fragments; extra kwargs are accepted for dispatch symmetry.

    MUST preserve 'JSON' keyword for Bailian API response_format compatibility.
    """
    _ = kwargs
    experience = get_experience_section()

    sections: list[tuple[str, str, PromptPriority]] = [
        ("role", get_verifier_role_section(), "invariant"),
        ("core_principles", get_verifier_core_principles_section(), "invariant"),
        ("experience", experience, "optional"),
        ("knowledge_summary", get_knowledge_summary_section(), "optional"),
        ("tools", get_verifier_tools_section(), "context"),
        ("layer2", get_verifier_layer2_section(), "context"),
        ("environment_profile", _environment_prompt_fragment(profile, "verify"), "context"),
        ("verification_heuristics", get_verification_heuristics_compact_section(), "context"),
        ("output_contract", get_verifier_output_format_section(), "contract"),
        ("remember", get_verifier_remember_section(), "invariant"),
    ]
    return _assemble(PromptMode.VERIFICATION, sections)


def build_intent_clarification_prompt(
    fault_spec: dict | None = None,
    skill_catalog: str = "",
    **kwargs,
) -> str:
    """Build intent_clarification system prompt using U-shaped composition.

    Follows the same architecture pattern as build_verifier_prompt():
    CRITICAL rules at BEGINNING (primacy) + END (recency), with
    dialogue modes, convergence logic, and tools in the middle.

    Dynamic FaultSpec context is placed below CACHE_BOUNDARY so stable sections can be cached
    across turns. The CRITICAL rules reminder occupies the very end
    of the prompt (after all dynamic content) for maximum recency effect.

    Args:
        fault_spec: Reviewed FaultSpec from previous dialogue turns. It is the
            only persistent fault contract.

    Returns:
        Assembled system prompt string.
    """
    # Intent recognition is deliberately independent of the configured
    # transport.  In semantic mode it sees the complete skill catalog; the
    # later feasibility stage validates the selected fault family against the
    # actual environment profile.
    semantic_only = bool(kwargs.get("semantic_only"))
    profile = None if semantic_only else kwargs.get("profile", PROFILE_K8S)

    sections: list[tuple[str, str, PromptPriority]] = [
        ("role", get_intent_role_section(semantic_only=semantic_only), "invariant"),
        ("priorities", get_intent_priorities_section(semantic_only=semantic_only), "invariant"),
        ("dialogue_routing", get_intent_dialogue_routing_section(), "context"),
        ("parameter_model", get_intent_parameter_model_section(), "context"),
        ("inject_flow", get_intent_inject_flow_section(semantic_only=semantic_only), "context"),
        ("recover_flow", get_intent_recover_flow_section(), "context"),
        ("batch_flow", get_intent_batch_flow_section(
            semantic_only=semantic_only,
        ), "context"),
        ("operation_freshness", get_intent_operation_freshness_section(semantic_only=semantic_only), "context"),
        ("tools", get_intent_tools_section(semantic_only=semantic_only), "context"),
        ("reflection", get_intent_reflection_section(semantic_only=semantic_only), "context"),
        ("capability_boundary", get_intent_capability_boundary_section(), "context"),
        ("output_contract", get_intent_output_section(), "contract"),
        ("environment_profile", _environment_prompt_fragment(profile, "intent"), "context"),
        ("skill_catalog", get_skill_index_section(skill_catalog), "optional"),
        ("cache_boundary", CACHE_BOUNDARY.strip(), "contract"),
    ]
    completeness = get_intent_completeness_section(
        fault_spec,
        kwargs.get("batch_faults"),
    )
    if completeness:
        sections.append(("completeness", completeness, "context"))
    sections.append(("remember", get_intent_reminder_section(profile), "invariant"))
    return _assemble(PromptMode.INTENT, sections)


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
    profile = kwargs.get("profile", PROFILE_K8S)
    planning_mode = kwargs.get("planning_mode", "guided")
    sections: list[tuple[str, str, PromptPriority]] = [
        ("role", get_plan_builder_role_section(planning_mode), "invariant"),
        ("critical_rules", get_plan_builder_critical_rules_section(planning_mode), "invariant"),
        ("workflow", get_plan_builder_workflow_section(planning_mode), "context"),
        ("tools", get_plan_builder_tools_section(planning_mode), "contract"),
        ("environment_profile", _environment_prompt_fragment(profile, "plan"), "context"),
        ("skill_catalog", get_skill_index_section(skill_catalog), "optional"),
        ("output_contract", get_plan_builder_output_format_section(planning_mode), "contract"),
        ("cache_boundary", CACHE_BOUNDARY.strip(), "contract"),
    ]
    progress = get_plan_builder_progress_section(
        collected_faults or [], fault_spec
    )
    if progress:
        sections.append(("progress", progress, "context"))
    sections.append((
        "remember",
        get_plan_builder_critical_rules_reminder_section(planning_mode),
        "invariant",
    ))
    return _assemble(PromptMode.PLAN_BUILDER, sections)


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
