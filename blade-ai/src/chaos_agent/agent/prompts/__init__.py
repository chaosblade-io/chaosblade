"""Blade AI prompt system — modular section-based composition.

This package provides the complete prompt assembly system for the fault-drill-agent.
All public APIs are re-exported here for backward compatibility with existing imports.

Usage:
    from chaos_agent.agent.prompts import build_inject_system_prompt
    from chaos_agent.agent.prompts import get_knowledge_registry
"""

# Builders
from chaos_agent.agent.prompts.builders import (
    build_inject_system_prompt,
    build_execute_system_prompt,
    build_verifier_prompt,
    build_intent_clarification_prompt,
    build_system_prompt,
)
# Constants
from chaos_agent.agent.prompts.constants import (
    REPLAN_MARKER,
    CACHE_BOUNDARY,
    MAX_AGENT_MD_BYTES,
    MAX_KNOWLEDGE_SUMMARY_BYTES,
    MAX_SYSTEM_PROMPT_CHARS,
)
# Knowledge registry (auto-discovered from frontmatter)
from chaos_agent.agent.prompts.knowledge_registry import get_knowledge_registry, rebuild_registry
# Modes
from chaos_agent.agent.prompts.modes import PromptMode
# Section functions — verification sub-sections (shared)
from chaos_agent.agent.prompts.sections import (
    get_fault_effect_delay_section, get_multi_iteration_section,
    get_minimal_container_section, get_verification_method_priority_section,
    get_verification_method_reasoning_section, get_evidence_sufficiency_section,
    get_handling_ambiguous_results_section,
)
# Section functions — intent clarification
from chaos_agent.agent.prompts.sections import (
    get_intent_role_section, get_intent_priorities_section,
    get_intent_dialogue_routing_section, get_intent_parameter_model_section,
    get_intent_inject_flow_section, get_intent_recover_flow_section,
    get_intent_batch_flow_section, get_intent_operation_freshness_section,
    get_intent_tools_section, get_intent_reflection_section, get_intent_output_section,
    get_intent_completeness_section, get_intent_reminder_section,
)
# Section functions — recovery verifier
from chaos_agent.agent.prompts.sections import (
    get_recover_role_section, get_recover_core_principles_section,
    get_recover_tools_section, get_recover_skill_priority_section,
    get_recover_output_format_section,
    get_recover_remember_section,
    build_recover_verifier_system_prompt,
)
# Section functions — replan
from chaos_agent.agent.prompts.sections import (
    get_replan_section, get_replan_directive_for_execution,
)
# Section functions — core
from chaos_agent.agent.prompts.sections import (
    get_role_section, get_env_section,
    get_knowledge_summary_section, get_domain_knowledge_section, get_skill_index_section,
    get_experience_section,
)
# Section functions — safety
from chaos_agent.agent.prompts.sections import (
    get_safety_section,
)
# Section functions — execution
from chaos_agent.agent.prompts.sections import (
    get_tools_section,
    get_guidelines_section, get_execution_directives_section,
)
# Section functions — verifier
from chaos_agent.agent.prompts.sections import (
    get_verifier_role_section, get_verifier_tools_section,
    get_verifier_layer2_section,
    get_verifier_output_format_section,
    get_verifier_core_principles_section, get_verifier_remember_section,
)
# Section functions — workflow & verification strategy
from chaos_agent.agent.prompts.sections import (
    get_workflow_section,
    get_core_principles_section,
    get_remember_section,
    get_executor_core_principles_section,
    get_executor_remember_section,
    get_verification_strategy_section,
)

__all__ = [
    # Constants
    "REPLAN_MARKER", "CACHE_BOUNDARY",
    "MAX_AGENT_MD_BYTES", "MAX_KNOWLEDGE_SUMMARY_BYTES", "MAX_SYSTEM_PROMPT_CHARS",
    # Modes
    "PromptMode",
    # Section functions — core
    "get_role_section", "get_env_section",
    "get_knowledge_summary_section", "get_domain_knowledge_section", "get_skill_index_section",
    "get_experience_section",
    "get_workflow_section",
    "get_core_principles_section", "get_remember_section",
    "get_executor_core_principles_section", "get_executor_remember_section",
    "get_verification_strategy_section",
    # Section functions — verification sub-sections (shared)
    "get_fault_effect_delay_section", "get_multi_iteration_section",
    "get_minimal_container_section", "get_verification_method_priority_section",
    "get_verification_method_reasoning_section", "get_evidence_sufficiency_section",
    "get_handling_ambiguous_results_section",
    # Section functions — safety
    "get_safety_section",
    # Section functions — execution
    "get_tools_section",
    "get_guidelines_section", "get_execution_directives_section",
    # Section functions — verifier
    "get_verifier_role_section", "get_verifier_tools_section",
    "get_verifier_layer2_section",
    "get_verifier_output_format_section",
    "get_verifier_core_principles_section", "get_verifier_remember_section",
    # Section functions — recovery verifier
    "get_recover_role_section", "get_recover_core_principles_section",
    "get_recover_tools_section", "get_recover_skill_priority_section",
    "get_recover_output_format_section",
    "get_recover_remember_section",
    "build_recover_verifier_system_prompt",
    # Section functions — replan
    "get_replan_section", "get_replan_directive_for_execution",
    # Section functions — intent clarification
    "get_intent_role_section", "get_intent_priorities_section",
    "get_intent_dialogue_routing_section", "get_intent_parameter_model_section",
    "get_intent_inject_flow_section", "get_intent_recover_flow_section",
    "get_intent_batch_flow_section", "get_intent_operation_freshness_section",
    "get_intent_tools_section", "get_intent_reflection_section", "get_intent_output_section",
    "get_intent_completeness_section", "get_intent_reminder_section",
    # Builders
    "build_inject_system_prompt", "build_execute_system_prompt",
    "build_verifier_prompt", "build_intent_clarification_prompt",
    "build_system_prompt",
    # Knowledge
    "get_knowledge_registry", "rebuild_registry",
]
