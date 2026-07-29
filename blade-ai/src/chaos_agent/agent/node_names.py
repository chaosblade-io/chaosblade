"""Canonical graph node name constants.

Single source of truth for node identifiers used in:
- graph.py (add_node keys)
- session_store (message "node" field)
- tui/streaming.py (phase mapping)
- l4/agent.py (stage mapping)

Import from here instead of writing bare strings.
"""

INTENT_CLARIFICATION = "intent_clarification"
INTENT_CONFIRM = "intent_confirm"
PLAN_BUILDER = "plan_builder"
BATCH_SETUP = "batch_setup"
AGENT_LOOP = "agent_loop"
EXTRACT_PLANNING_METADATA = "extract_planning_metadata"
PLAN_CHANGE_CONFIRM = "plan_change_confirm"
SAFETY_CHECK = "safety_check"
CONFIRMATION_GATE = "confirmation_gate"
BASELINE_CAPTURE = "baseline_capture"
EXECUTE_LOOP = "execute_loop"
DIRECT_SETUP = "direct_setup"
DIRECT_EXECUTE = "direct_execute"
VERIFIER = "verifier"
VERIFIER_LOOP = "verifier_loop"
SE_DETECT = "se_detect"
FINALIZE_VERIFICATION = "finalize_verification"
RECOVER_VERIFIER = "recover_verifier"
RECOVER_VERIFIER_LOOP = "recover_verifier_loop"
FINALIZE_RECOVER_VERIFICATION = "finalize_recover_verification"
MEMORY_NODE = "memory_node"
MEMORY_HOOK = "memory_hook"
SAVE_MEMORY = "save_memory"
RECOVER_HANDLER = "recover_handler"
REJECT = "reject"
TOOL_RESULT = "tool_result"
