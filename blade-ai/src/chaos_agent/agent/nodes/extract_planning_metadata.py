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

from chaos_agent.agent.node_names import TOOL_RESULT
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
    "workload": "pod",
    "Service": "pod",
    "PVC": "pod",
    "DaemonSet": "pod",
    "HPA": "pod",
    "DNS": "pod",
    "节点容器运行时": "node",
}


_CASE_NAME_RE = re.compile(r"\*\*用例名称\*\*\s*(.+?)\s*$", re.MULTILINE)


def _extract_chosen_skill_case_path(messages: list) -> str:
    """Extract skill_case_resource from finish_planning or save_fault_plan args."""
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            name = tc.get("name", "") if isinstance(tc, dict) else ""
            if name in ("finish_planning", "save_fault_plan"):
                args = tc.get("args", {}) if isinstance(tc, dict) else {}
                path = args.get("skill_case_resource", "")
                if path:
                    return path
    return ""


def _find_skill_case_by_path(messages: list, resource_path: str) -> str:
    """Find the read_skill_resource ToolMessage matching the given path."""
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            name = tc.get("name", "") if isinstance(tc, dict) else ""
            args = tc.get("args", {}) if isinstance(tc, dict) else {}
            if name == "read_skill_resource" and args.get("resource_path", "") == resource_path:
                tc_id = tc.get("id", "")
                if tc_id:
                    for resp in messages:
                        if (isinstance(resp, ToolMessage)
                                and getattr(resp, "tool_call_id", "") == tc_id):
                            content = resp.content if isinstance(resp.content, str) else ""
                            if content and any(m in content for m in _USE_CASE_MARKERS):
                                return content
    return ""


def _extract_skill_case_from_messages(messages: list, plan: str = "") -> str:
    """Extract skill case content from read_skill_resource ToolMessages.

    When multiple use-case ToolMessages exist (agent read several cases
    for comparison), matches against the plan summary and AIMessage text
    to identify which one the agent actually chose.

    Returns:
        The use-case content string, or "" if not found.
    """
    candidates: list[str] = []
    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") != "read_skill_resource":
            continue
        content = msg.content if isinstance(msg.content, str) else ""
        if not content:
            continue
        stripped = content.strip()
        if stripped.startswith("Directory:") or stripped.startswith("Contents:"):
            continue
        if any(marker in content for marker in _USE_CASE_MARKERS):
            candidates.append(content)

    if not candidates:
        return ""
    if len(candidates) == 1:
        return candidates[0]

    # Multiple candidates — find which one the agent chose.
    # Extract identifiers from each candidate's **用例名称** line.
    # Case name format: "原因 导致 现象" — extract the cause part
    # (most unique) for matching against plan text and AIMessages.
    named: list[tuple[list[str], str]] = []
    for c in candidates:
        m = _CASE_NAME_RE.search(c)
        keys: list[str] = []
        if m:
            full_name = m.group(1)
            keys.append(full_name)
            parts = full_name.split(" 导致 ", 1)
            if len(parts) == 2:
                keys.append(parts[0])  # cause: "镜像不存在或标签错误"
                keys.append(parts[1])  # phenomenon: "Pod_镜像拉取失败"
        named.append((keys, c))

    # Collect reference texts: plan (most authoritative) + AIMessages
    # in reverse (later messages more likely to contain final decision).
    reference_texts = [plan] if plan else []
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            ai_text = getattr(msg, "content", "") or ""
            if ai_text:
                reference_texts.append(ai_text)

    for ref in reference_texts:
        if not ref:
            continue
        for keys, content in named:
            if any(k and k in ref for k in keys):
                return content

    # Fallback: first candidate (first-read is typically the primary choice)
    return candidates[0]


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


def _has_browsed_catalogue(messages: list) -> bool:
    """Check if the LLM called read_skill_resource with a catalogue path.

    Scans AIMessage tool_calls for any read_skill_resource invocation
    whose resource_path contains "catalogue". This indicates the LLM
    followed the skill discovery flow and browsed available use cases.
    """
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            name = tc.get("name", "") if isinstance(tc, dict) else ""
            args = tc.get("args", {}) if isinstance(tc, dict) else {}
            if name == "read_skill_resource" and "catalogue" in args.get("resource_path", ""):
                return True
    return False


def _find_planning_exit_tool_message(messages: list) -> ToolMessage | None:
    """Find the most recent successful finish_planning or save_fault_plan ToolMessage."""
    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            break
        if getattr(msg, "status", None) == "error":
            continue
        msg_name = getattr(msg, "name", "") or ""
        if msg_name in ("finish_planning", "save_fault_plan"):
            return msg
    return None


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
    from chaos_agent.agent.state_helpers import fail_state
    from chaos_agent.agent.verdict import FailureCategory

    result: dict = {}
    messages = state.get("messages", [])

    # Extract plan metadata from ToolNode-produced ToolMessages
    _exit_tm = _find_planning_exit_tool_message(messages)
    if _exit_tm:
        _kwargs = getattr(_exit_tm, "additional_kwargs", None)
        if isinstance(_kwargs, dict):
            _kwargs.setdefault("_node", TOOL_RESULT)
        tm_name = getattr(_exit_tm, "name", "")
        tm_content = _exit_tm.content if isinstance(_exit_tm.content, str) else ""

        if tm_name == "finish_planning":
            if tm_content.startswith("Planning rejected"):
                if (
                    not _has_browsed_catalogue(messages)
                    and not state.get("_catalogue_rejection_nudged")
                ):
                    from langchain_core.messages import HumanMessage
                    logger.warning(
                        "extract_planning_metadata: LLM rejected without "
                        "browsing catalogue, nudging to browse first"
                    )
                    result["_catalogue_rejection_nudged"] = True
                    result["planning_rejected"] = True
                    result["messages"] = [HumanMessage(content=(
                        "**REJECTION NOT ACCEPTED**: You concluded this fault "
                        "scenario is unsupported WITHOUT browsing the skill "
                        "catalogue. The catalogue contains kubectl-native "
                        "injection use cases beyond ChaosBlade primitives.\n\n"
                        "You MUST first browse the catalogue:\n"
                        "1. `read_skill_resource(resource_path='references/catalogue/')`\n"
                        "2. Find the directory matching the fault symptom\n"
                        "3. Read the specific use-case .md file\n\n"
                        "If a matching use case exists, follow it. "
                        "If no matching use case exists, design your own "
                        "injection plan based on the fault description and "
                        "proceed with `finish_planning`."
                    ))]
                    return result

                reason = tm_content.replace("Planning rejected. Reason: ", "")
                result.update(fail_state(
                    FailureCategory.PLANNING_REJECTED, reason, messages,
                ))
                return result

            if tm_content.startswith("Planning finalized"):
                summary = tm_content.replace("Planning finalized. Summary: ", "")
                if summary and not state.get("plan"):
                    result["plan"] = summary

        elif tm_name == "save_fault_plan":
            if not tm_content.startswith("Plan saved to "):
                result["planning_rejected"] = True
                return result
            result["is_complex"] = True
            first_line = tm_content.split("\n")[0]
            result["plan_path"] = first_line.replace("Plan saved to ", "").strip()
            plan_body = tm_content.split("\n\n", 1)[1] if "\n\n" in tm_content else ""
            if plan_body and not state.get("plan"):
                result["plan"] = plan_body

    # Fallback: if no exit TM found but skill is activated, use the last
    # AIMessage's text content as plan (LLM output pure text summary
    # without calling finish_planning).
    if not _exit_tm and not state.get("plan") and not result.get("plan"):
        if state.get("skill_name"):
            for msg in reversed(messages):
                if isinstance(msg, AIMessage):
                    _content = (getattr(msg, "content", "") or "").strip()
                    if _content and not getattr(msg, "tool_calls", None):
                        result["plan"] = _content
                    break

    # 1. skill_case_content — needed by baseline_capture's LLM strategy.
    #    Primary: agent specifies skill_case_resource in finish_planning.
    #    Fallback: infer from read_skill_resource ToolMessages.
    if not state.get("skill_case_content"):
        chosen_path = _extract_chosen_skill_case_path(messages)
        if chosen_path:
            skill_case = _find_skill_case_by_path(messages, chosen_path)
        else:
            plan_text = result.get("plan") or state.get("plan") or ""
            skill_case = _extract_skill_case_from_messages(messages, plan=plan_text)
        if skill_case:
            result["skill_case_content"] = skill_case
            logger.info(
                "extract_planning_metadata: extracted skill_case_content "
                "from messages (%d chars)", len(skill_case),
            )

    # 1b. Guard: reject planning if no catalogue use-case was loaded.
    # Only enforce when messages exist (agent_loop has run). Empty messages
    # means direct_setup path or test — no guard needed.
    # Bypass: if the LLM has browsed the catalogue and found no match,
    # it may design its own plan — allow that through.
    has_case = bool(
        result.get("skill_case_content") or state.get("skill_case_content")
    )
    if messages and not has_case and not _has_browsed_catalogue(messages):
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
            "  2. If no matching use case exists, design your own injection "
            "plan and proceed with finish_planning.\n\n"
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
        scope_from_blade = bool(derived_scope)
        if not derived_scope:
            derived_scope = _derive_scope_from_resource_path(messages)

        # Scope derivation has two sources with different authority:
        #
        # 1. ChaosBlade command pattern (scope_from_blade=True):
        #    AUTHORITATIVE — the blade scope IS the injection scope.
        #    Always override agent_loop's value, and clear names if
        #    scope changes (names belong to the old scope).
        #
        # 2. Catalogue directory prefix (scope_from_blade=False):
        #    SYMPTOM-level only (e.g. "Pod_镜像拉取失败" → pod).
        #    For non-ChaosBlade faults the injection scope comes from
        #    the kubectl resource type (deployment, node, etc.) which
        #    agent_loop already derived correctly. Write-once fallback:
        #    only fill when spec.scope is empty.
        updates: dict = {}
        if derived_scope:
            if scope_from_blade and spec.scope != derived_scope:
                updates["scope"] = derived_scope
                if spec.names:
                    updates["names"] = ()
            elif not scope_from_blade and not spec.scope:
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