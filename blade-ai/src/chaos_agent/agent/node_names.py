"""Canonical graph node name constants.

Single source of truth for node identifiers used in:
- graph.py (add_node keys)
- session_store (message "node" field)
- tui/streaming.py (phase mapping)
- l4/agent.py (stage mapping)

Import from here instead of writing bare strings.
"""

INTENT_CLARIFICATION = "intent_clarification"
AGENT_LOOP = "agent_loop"
EXECUTE_LOOP = "execute_loop"
DIRECT_SETUP = "direct_setup"
DIRECT_EXECUTE = "direct_execute"
BASELINE_CAPTURE = "baseline_capture"
VERIFIER = "verifier"
FINALIZE_VERIFICATION = "finalize_verification"
RECOVER_VERIFIER = "recover_verifier"
FINALIZE_RECOVER_VERIFICATION = "finalize_recover_verification"
MEMORY_NODE = "memory_node"
MEMORY_HOOK = "memory_hook"
PLAN_CHANGE_CONFIRM = "plan_change_confirm"
TOOL_RESULT = "tool_result"
