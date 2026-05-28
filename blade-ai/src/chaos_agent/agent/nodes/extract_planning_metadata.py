"""Extract planning metadata from agent_loop message history into State.

This node bridges an information gap in NL (natural language) mode:
agent_loop produces skill_case_content and blade scope/target/action
information in the message stream, but these fields are only written
to State by execute_loop — which runs AFTER baseline_capture.

Without this extraction, baseline_capture finds all three strategies
(LLM, Registry, Scope_fallback) empty and produces source="none".

This node runs between agent_loop and safety_check, extracting the
missing fields so baseline_capture can function in NL mode.

Pure deterministic message parsing — no LLM calls, no async operations.
"""

import logging
import re

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.state import AgentState

logger = logging.getLogger(__name__)

# ── ChaosBlade command pattern ──
# Format: blade create k8s <scope>-<target> <action>
# Examples: pod-disk burn, node-cpu fullload, pod-network drop
_CB_SCOPE_TARGET_ACTION_RE = re.compile(
    r'(?:blade\s+create\s+k8s\s+)?'
    r'(?P<scope>pod|node|container)-(?P<target>\w+)\s+(?P<action>\w+)',
)

# ── Use-case content markers ──
# ToolMessages from read_skill_resource that contain actual use-case
# content (not directory listings) will have at least one of these.
_USE_CASE_MARKERS = ("**故障现象**", "**注入验证**", "**恢复验证**")

# ── Directory name → scope mapping ──
# Skill catalogue directories are named <层级>_<现象>.
# ChaosBlade only supports pod/node/container scopes for k8s.
_DIR_PREFIX_SCOPE_MAP: dict[str, str] = {
    "Pod": "pod",
    "Node": "node",
    "Workload": "pod",
    "Service": "pod",
    "PVC": "pod",
    "DaemonSet": "pod",
    "HPA": "pod",
    "节点容器运行时": "node",  # Node-level container runtime disk
}


def _extract_skill_case_from_messages(messages: list) -> str:
    """Extract skill case content from read_skill_resource ToolMessages.

    Scans messages in reverse order, finding the last ToolMessage from
    ``read_skill_resource`` that contains actual use-case content
    (not a directory listing).

    Returns:
        The use-case content string, or "" if not found.
    """
    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") != "read_skill_resource":
            continue
        content = msg.content if isinstance(msg.content, str) else ""
        if not content:
            continue
        # Exclude directory listings (start with "Directory:" or "Contents:")
        stripped = content.strip()
        if stripped.startswith("Directory:") or stripped.startswith("Contents:"):
            continue
        # Confirm it's use-case content (has at least one marker)
        if any(marker in content for marker in _USE_CASE_MARKERS):
            return content
    return ""


def _derive_scope_target_action(skill_case: str) -> tuple[str, str, str]:
    """Derive blade scope/target/action from skill case content.

    Searches for ChaosBlade command patterns like ``pod-disk burn``
    in the skill case content.

    Returns:
        (scope, target, action) tuple. Any element may be "" if not found.
    """
    for match in _CB_SCOPE_TARGET_ACTION_RE.finditer(skill_case):
        return match.group("scope"), match.group("target"), match.group("action")
    return "", "", ""


def _derive_scope_from_resource_path(messages: list) -> str:
    """Fallback: derive scope from read_skill_resource resource_path args.

    Scans AIMessages for tool_calls to ``read_skill_resource``, extracts
    the ``resource_path`` argument, and maps the directory prefix to a scope.

    Example: ``references/catalogue/Pod_磁盘IO过高/...`` → scope=pod

    Returns:
        scope string, or "" if not found.
    """
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in tool_calls:
            tc_name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            if tc_name != "read_skill_resource":
                continue
            tc_args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            resource_path = tc_args.get("resource_path", "")
            if not resource_path:
                continue
            # resource_path format: references/catalogue/<DirPrefix>_.../<file>.md
            # Extract the directory name prefix
            parts = resource_path.split("/")
            for part in parts:
                # Find the part that matches a known directory prefix
                for prefix, scope in _DIR_PREFIX_SCOPE_MAP.items():
                    if part.startswith(prefix):
                        return scope
    return ""


async def extract_planning_metadata(state: AgentState) -> dict:
    """Extract planning metadata from agent_loop messages into State.

    Fills the State gap between agent_loop (which produces information
    in messages) and baseline_capture (which reads from State).

    For each field: only writes if the field is NOT already in State
    (direct_setup may have populated these for CLI mode).

    Returns:
        dict with keys to merge into State. May be empty if all fields
        are already populated (direct mode).
    """
    from chaos_agent.agent.fault_spec import read_fault_spec

    result: dict = {}
    messages = state.get("messages", [])

    # 1. skill_case_content — needed by baseline_capture's LLM strategy.
    if not state.get("skill_case_content"):
        skill_case = _extract_skill_case_from_messages(messages)
        if skill_case:
            result["skill_case_content"] = skill_case
            logger.info(
                "extract_planning_metadata: extracted skill_case_content "
                "from messages (%d chars)", len(skill_case),
            )

    # 1b. Guard: reject planning if no catalogue use-case was loaded.
    # Only enforce when messages exist (agent_loop has run). Empty messages
    # means direct_setup path or test — no guard needed.
    has_case = bool(
        result.get("skill_case_content") or state.get("skill_case_content")
    )
    if messages and not has_case:
        from langchain_core.messages import SystemMessage
        logger.warning(
            "extract_planning_metadata: no catalogue use-case loaded, "
            "rejecting planning and routing back to agent_loop",
        )
        result["planning_rejected"] = True
        result["messages"] = [SystemMessage(content=(
            "[PLANNING REJECTED] No catalogue use-case was loaded during planning.\n\n"
            "You must either:\n"
            "  1. Follow the skill discovery flow described in SKILL.md: "
            "use read_skill_resource to browse the catalogue directory, "
            "locate a matching use-case file, and load its full content.\n"
            "  2. Or inform the user: this fault scenario is not currently supported.\n\n"
            "Do NOT proceed with a plan based solely on general command references."
        ))]
        return result

    # 2. fault_spec scope/blade_target/blade_action derivation.
    #
    # TUI mode: intent_clarification populates the spec from the user's
    # explicit submit_fault_intent — spec.is_complete is True here, this
    # block is a no-op.
    #
    # CLI NL mode: the entry point only writes a placeholder spec
    # (user_description + source). LLM's planning actions (activate
    # skill, read use-case) carry the fault_type information; without
    # this lazy derivation, safety_check would reject every CLI NL turn
    # with "No target specified".
    spec = read_fault_spec(state)
    if spec is not None and not (
        spec.scope and spec.blade_target and spec.blade_action
    ):
        source_case = (
            result.get("skill_case_content")
            or state.get("skill_case_content")
            or ""
        )
        derived_scope, derived_target, derived_action = _derive_scope_target_action(source_case)
        if not derived_scope:
            derived_scope = _derive_scope_from_resource_path(messages)

        # Build a partial-update dict for spec.replace() — only fill
        # fields the spec is missing. write-once semantics prevent
        # subsequent re-derivations from clobbering an earlier value.
        updates: dict = {}
        if derived_scope and not spec.scope:
            updates["scope"] = derived_scope
        if derived_target and not spec.blade_target:
            updates["blade_target"] = derived_target
        if derived_action and not spec.blade_action:
            updates["blade_action"] = derived_action
        if updates:
            new_spec = spec.replace(**updates)
            result["fault_spec"] = new_spec.to_dict()
            logger.info(
                "extract_planning_metadata: derived spec fields %s "
                "(CLI NL or initially incomplete spec path)",
                {k: v for k, v in updates.items()},
            )

    return result