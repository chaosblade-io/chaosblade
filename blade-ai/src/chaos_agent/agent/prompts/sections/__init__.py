"""Section functions for prompt composition, organized by semantic group."""

from chaos_agent.agent.prompts.sections.execution import (
    get_tools_section,
    get_guidelines_section,
    get_execution_directives_section,
)
from chaos_agent.agent.prompts.sections.experience_section import get_experience_section
from chaos_agent.agent.prompts.sections.identity import get_role_section, get_executor_role_section, get_env_section
from chaos_agent.agent.prompts.sections.intent import (
    get_intent_role_section,
    get_intent_priorities_section,
    get_intent_inject_flow_section,
    get_intent_recover_flow_section,
    get_intent_output_section,
    get_intent_completeness_section,
)
from chaos_agent.agent.prompts.sections.knowledge_sections import \
    get_knowledge_summary_section, get_skill_index_section
from chaos_agent.agent.prompts.sections.recovery import (
    get_recover_role_section,
    get_recover_core_principles_section,
    get_recover_delay_section,
    get_recover_skill_priority_section,
    get_recover_output_format_section,
    build_recover_verifier_system_prompt,
)
from chaos_agent.agent.prompts.sections.safety import (
    get_safety_section,
)
from chaos_agent.agent.prompts.sections.verification import (
    get_verifier_role_section,
    get_verifier_layer2_section,
    get_verifier_output_format_section,
    get_verifier_core_principles_section,
)
from chaos_agent.agent.prompts.sections.workflow import (
    get_workflow_section,
    get_core_principles_section,
    get_executor_core_principles_section,
    get_replan_section,
    get_replan_directive_for_execution,
)

__all__ = [
    "get_intent_role_section", "get_intent_priorities_section",
    "get_intent_inject_flow_section", "get_intent_recover_flow_section",
    "get_intent_output_section",
    "get_intent_completeness_section",
    "get_role_section", "get_executor_role_section", "get_env_section",
    "get_knowledge_summary_section", "get_skill_index_section",
    "get_experience_section",
    "get_workflow_section",
    "get_core_principles_section",
    "get_executor_core_principles_section",
    "get_replan_section", "get_replan_directive_for_execution",
    "get_safety_section",
    "get_tools_section",
    "get_guidelines_section", "get_execution_directives_section",
    "get_verifier_role_section",
    "get_verifier_layer2_section",
    "get_verifier_output_format_section",
    "get_verifier_core_principles_section",
    "get_recover_role_section", "get_recover_core_principles_section",
    "get_recover_delay_section", "get_recover_skill_priority_section",
    "get_recover_output_format_section",
    "build_recover_verifier_system_prompt",
]
