"""Prompt loading mode enumeration.

Borrowed from OpenCLAW's PromptMode concept — different workflow nodes
require different levels of prompt detail.
"""

from enum import Enum


class PromptMode(Enum):
    """Prompt loading mode for different workflow stages.

    - FULL: Complete system prompt with all sections (agent_loop — planning phase)
    - MINIMAL: Core sections only, skip planning-specific content (execute_loop — execution phase)
    - VERIFICATION: Verification-focused sections only (verifier_loop — verification phase)
    - INTENT: Intent clarification with U-shaped critical rules (intent_clarification — dialogue phase)
    """

    FULL = "full"                   # agent_loop: 全量 section 组装
    MINIMAL = "minimal"             # execute_loop: 跳过 chat_routing/workflow/nl_mode
    VERIFICATION = "verification"   # verifier_loop: 仅安全+知识+经验+验证指令
    INTENT = "intent"               # intent_clarification: U-shaped 对话+收敛+路由
