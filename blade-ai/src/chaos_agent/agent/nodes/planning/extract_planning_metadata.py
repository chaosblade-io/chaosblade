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

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.node_names import TOOL_RESULT
from chaos_agent.agent.prompts.reminder import wrap_system_reminder
from chaos_agent.agent.spec.skill_identity import has_active_skill
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


def _extract_planning_duration(messages: list) -> int:
    """Extract duration_seconds from the LAST finish_planning tool-call args.

    Mirrors ``_extract_chosen_skill_case_path``: the declaration travels on
    the tool call itself (a control signal the tool body never consumes —
    this node reads it from the message history). Only the newest
    finish_planning counts: earlier declarations died with the planning
    rounds they belonged to (nudge/replan re-runs). Returns 0 when the
    call is absent, undeclared, or unparsable — callers treat 0 as
    "not declared".
    """
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            name = tc.get("name", "") if isinstance(tc, dict) else ""
            if name != "finish_planning":
                continue
            raw = (tc.get("args", {}) if isinstance(tc, dict) else {}).get(
                "duration_seconds", 0,
            )
            try:
                return max(int(str(raw).strip()), 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _extract_last_skill_resource_path(messages: list) -> str:
    """Extract the resource_path from the last read_skill_resource tool call.

    Scans AIMessages in reverse for the most recent read_skill_resource
    invocation and returns its resource_path argument.
    """
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            tc_name = tc.get("name", "") if isinstance(tc, dict) else ""
            if tc_name != "read_skill_resource":
                continue
            tc_args = tc.get("args", {}) if isinstance(tc, dict) else {}
            resource_path = tc_args.get("resource_path", "")
            if resource_path and "catalogue" in resource_path:
                return resource_path
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


def _extract_planning_fault_identity(messages: list) -> tuple[str, str, str]:
    """Read the planner's fault-identity declaration from the LAST finish_planning call.

    The declaration (``fault_scope`` / ``fault_target`` / ``fault_action``
    args) travels on the tool call itself — the tool body never consumes
    it (the same carrier pattern as ``duration_seconds``; this node reads
    it from the message history). Only the newest finish_planning counts:
    earlier declarations died with the planning rounds they belonged to
    (nudge/replan re-runs). ``save_fault_plan`` is NOT a declaration
    carrier — a saved draft is not a final decision (route_after_phase1_tools
    keeps Phase 1 going after a save).

    Returns a normalised (scope, target, action) tuple; every element is
    "" when the call is absent or undeclared.
    """
    def _norm(value: object) -> str:
        return str(value).strip().lower() if value else ""

    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            name = tc.get("name", "") if isinstance(tc, dict) else ""
            if name != "finish_planning":
                continue
            args = tc.get("args", {}) if isinstance(tc, dict) else {}
            return (
                _norm(args.get("fault_scope")),
                _norm(args.get("fault_target")),
                _norm(args.get("fault_action")),
            )
    return "", "", ""


def _identity_nudge_plan_family_reset() -> dict:
    """Plan-family reset carried by both identity nudges (F1).

    Both nudges are the first nudge family that fires AFTER a
    finalised round wrote the plan family into State (the catalogue
    nudge fires on the rejection round, before any write). Round 2
    must land its OWN plan — the round-1 plan attacked the identity
    the nudge is about — so the write-once guards (plan /
    plan_summary / plan_verification) must re-open.
    plan_change_confirm's approved branch resets the same seam (plan /
    plan_path / is_complex / skill_case_content); this is the union of
    both families.
    """
    return {
        "plan": None,
        "plan_summary": None,
        "plan_verification": None,
        "plan_path": None,
        "is_complex": False,
        "skill_case_content": None,
    }


def _derive_scope_target_action(source_text: str) -> tuple[str, str, str]:
    """Derive blade scope/target/action from the plan's Execution Steps.

    Last-resort tier (B83): only ever applied to text the LLM actually
    wrote as its plan — never to the skill-case document, which is a
    menu of main path + backup means whose first blade command hijacked
    #49's spec from pod to node. Fill-vacuum only: callers ignore every
    element the spec already carries.

    Returns:
        (scope, target, action) tuple. Any element may be "" if not found.
    """
    for match in _CB_SCOPE_TARGET_ACTION_RE.finditer(source_text):
        return match.group("scope"), match.group("target"), match.group("action")
    return "", "", ""


def _has_browsed_catalogue(messages: list) -> bool:
    """Check if the LLM called read_skill_resource at least once.

    Scans AIMessage tool_calls for any read_skill_resource invocation.
    This indicates the LLM followed the skill discovery flow and attempted
    to browse available use cases. We intentionally do NOT restrict the
    resource_path — any call to this tool counts as browsing effort.
    """
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            name = tc.get("name", "") if isinstance(tc, dict) else ""
            if name == "read_skill_resource":
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


def _find_saved_plan(messages: list) -> tuple[str, str]:
    """Locate the last saved fault plan in message history.

    Returns ``(plan_content, plan_path)``; either may be empty when no
    plan was saved (simple tasks). Content is read from the
    ``save_fault_plan`` tool-call ARGS (the authoritative copy that is
    always present in history), never from the tool-result echo, so
    hydration stays independent of the result format.
    """
    content = ""
    path = ""
    for msg in reversed(messages):
        if not path and isinstance(msg, ToolMessage) \
                and (getattr(msg, "name", "") or "") == "save_fault_plan":
            tm_text = msg.content if isinstance(msg.content, str) else ""
            if tm_text.startswith("Plan saved to "):
                path = tm_text.split("\n")[0].replace("Plan saved to ", "").strip()
        if not content and isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", None) or []:
                if tc.get("name") == "save_fault_plan":
                    _pc = (tc.get("args") or {}).get("plan_content") or ""
                    if isinstance(_pc, str) and _pc.strip():
                        content = _pc
                    break
        if content and path:
            break
    return content, path


# Markdown sections of the saved plan that carry verification value.
# Order matters: the strategy first, the anticipated effect second.
_PLAN_VERIFIER_SECTIONS = ("verification methods", "expected impact")


def _plan_section(plan: str, header: str) -> str:
    """Slice one ``## `` section out of a markdown plan (empty if absent)."""
    lines = plan.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().lower().startswith(f"## {header}"):
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].strip().startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


def _extract_plan_verification_slices(plan: str) -> str:
    """Verifier-facing slice of the plan: Verification Methods + Expected Impact.

    The planner's environment-adapted verification strategy (probed
    facts, anticipated negatives) overrides generic skill-case steps at
    Layer 2 — but only these two sections are surfaced; execution steps
    and rollback stay executor-only.
    """
    if not plan:
        return ""
    parts = [_plan_section(plan, header) for header in _PLAN_VERIFIER_SECTIONS]
    return "\n\n".join(p for p in parts if p)


async def extract_planning_metadata(state: AgentState) -> dict:
    """Extract planning metadata from agent_loop messages into State.

    Fills the State gap between agent_loop (which produces information
    in messages) and baseline_capture (which reads from State).

    For each field: only writes if the field is NOT already in State
    (an upstream node may have populated these).

    Returns:
        dict with keys to merge into State. May be empty if all fields
        are already populated.
    """
    from chaos_agent.agent.spec.fault_spec import read_fault_spec
    from chaos_agent.utils.fault_type import ensure_min_duration

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
                    logger.warning(
                        "extract_planning_metadata: LLM rejected without "
                        "browsing catalogue, nudging to browse first"
                    )
                    result["_catalogue_rejection_nudged"] = True
                    result["planning_rejected"] = True
                    result["messages"] = [HumanMessage(content=wrap_system_reminder(
                        "**REJECTION NOT ACCEPTED**: You concluded this fault "
                        "scenario is unsupported WITHOUT browsing the active "
                        "skill's resources. The skill bundles injection use "
                        "cases beyond what general command references "
                        "cover.\n\n"
                        "You MUST first browse them:\n"
                        "1. Follow the discovery flow described in SKILL.md, "
                        "using `read_skill_resource` (a directory path yields "
                        "a listing)\n"
                        "2. Find the use case matching the fault symptom\n"
                        "3. Read the specific use-case file\n\n"
                        "If a matching use case exists, follow it. "
                        "If no matching use case exists, design your own "
                        "injection plan based on the fault description and "
                        "proceed with `finish_planning`."
                    ))]
                    return result

                reason = tm_content.replace("Planning rejected. Reason: ", "")
                # Extract alternatives if present
                alternatives = ""
                if "\nAlternatives:\n" in reason:
                    reason, alternatives = reason.split("\nAlternatives:\n", 1)

                result["planning_rejected"] = True
                result["_planning_alternatives"] = alternatives.strip() if alternatives else ""
                result["_planning_rejection_reason"] = reason.strip()
                # Set error so the routing terminates at the reject node
                # instead of looping back to agent_loop. The nudge path
                # above intentionally omits error to give the LLM another
                # chance; this path is only reached after the LLM has
                # browsed the catalogue (or was already nudged once), so
                # the rejection is genuine and should be honoured.
                result["error"] = reason.strip()
                # The reject node renders safety_reason as the direct cause
                # (W-56-5 defect c): a planning rejection must land its own
                # reason there, or a stale gate reason from an earlier
                # attempt would be attributed instead.
                result["safety_reason"] = reason.strip()
                logger.warning(
                    "extract_planning_metadata: LLM rejected planning after "
                    "browsing catalogue. Routing to reject (terminate). "
                    "Reason: %s", reason,
                )
                from chaos_agent.agent.nodes.planning._planning_cleanup import (
                    cleanup_planning_debug_pods,
                )
                result.update(await cleanup_planning_debug_pods(state))
                return result

            if tm_content.startswith("Planning finalized"):
                summary = tm_content.replace("Planning finalized. Summary: ", "")
                if summary and not state.get("plan"):
                    result["plan"] = summary
                # Human-facing summary for the confirm card / CLI prompt /
                # experiment listing. On the complex track state["plan"]
                # carries the FULL saved plan, so this summary is the only
                # compact rendering of intent — do not drop it.
                if summary and not state.get("plan_summary"):
                    result.setdefault("plan_summary", summary)

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

    # Complex-task precedence: the saved FULL plan beats the
    # finish_planning summary for ``state["plan"]``. Phase 2's system
    # prompt must carry the exact '## Execution Steps' (sliced by
    # _execution_steps_only), not an LLM-compressed summary that may
    # drop commands, vehicle names, or preconditions — the message
    # history echo is a weaker carrier (compaction / attention decay).
    if not state.get("plan"):
        _saved_plan, _saved_path = _find_saved_plan(messages)
        if _saved_plan:
            result["plan"] = _saved_plan
            result.setdefault("is_complex", True)
            if _saved_path:
                result.setdefault("plan_path", _saved_path)

    # Fallback: if no exit TM found but skill is activated, use the last
    # AIMessage's text content as plan (LLM output pure text summary
    # without calling finish_planning).
    if not _exit_tm and not state.get("plan") and not result.get("plan"):
        if has_active_skill(state):
            for msg in reversed(messages):
                if isinstance(msg, AIMessage):
                    _content = (getattr(msg, "content", "") or "").strip()
                    if _content and not getattr(msg, "tool_calls", None):
                        result["plan"] = _content
                    break

    # Verifier-facing slice of the final plan: the planner's probed,
    # environment-adapted verification strategy feeds Layer 2 (empty for
    # simple tasks with no saved plan).
    if not state.get("plan_verification"):
        _plan_for_slice = result.get("plan") or state.get("plan") or ""
        _pv = _extract_plan_verification_slices(_plan_for_slice)
        if _pv:
            result["plan_verification"] = _pv

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

    # NOTE: Guard 1a (path-based case validation) removed.
    # LLM explicitly chooses the skill case via finish_planning — trust that decision.
    # Static keyword matching is strictly less capable than model reasoning and
    # produces false negatives (e.g. "Pod_OOM内存异常" vs fault_action="load").
    _case_content = result.get("skill_case_content") or state.get("skill_case_content") or ""

    # 1b. Guard: reject planning if no catalogue use-case was loaded.
    # Only enforce when messages exist (agent_loop has run). Empty messages
    # means a test entry — no guard needed.
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
            "[PLANNING REJECTED] No skill use-case was loaded during planning.\n\n"
            "You must either:\n"
            "  1. Follow the skill discovery flow described in SKILL.md: "
            "use read_skill_resource to browse the skill's resources, "
            "locate a matching use-case file, and load its full content.\n"
            "  2. If no matching use case exists, design your own injection "
            "plan and proceed with finish_planning.\n\n"
            "Do NOT proceed with a plan based solely on general command references."
        ))]
        return result

    # 2. fault_spec identity resolution (B83/B84, #49 post-mortem).
    #
    # The planner is the only actor that knows which mechanism the plan
    # chose, so the identity triple comes from its explicit declaration
    # on finish_planning (fault_scope / fault_target / fault_action).
    # Nothing is mined from the skill-case document anymore: a case doc
    # is a MENU (main path + backup means), and its first blade command
    # hijacked #49's spec from pod to node — the guard then enforced the
    # WRONG anchor and the intended fault never happened.
    #
    # Contract:
    # - complete identity + matching (or absent) declaration → no-op
    #   (TUI / structured modes land here)
    # - incomplete identity + declaration → the declaration is the
    #   planner's final word: write all three, override-if-different;
    #   a scope change clears names AND labels (both belong to the old
    #   scope — B76's kind-consistency lesson, applied to the
    #   declaration writer)
    # - complete identity + conflicting declaration → split nudge, once:
    #   a reviewed identity is never rewritten by a declaration; the
    #   revision exit is propose_plan_change (user-confirmed)
    # - incomplete identity + no declaration → derive from the plan's
    #   Execution Steps (the LLM's actual plan, never the case menu),
    #   fill-vacuum only; still unresolved after that (kubectl-native
    #   plan with no blade pattern) → declaration nudge, once; a spec
    #   that stays incomplete fails safety_check honestly with
    #   "No target specified" instead of executing a guessed fault
    spec = read_fault_spec(state)
    updates: dict = {}
    if spec is not None:
        identity_complete = bool(
            spec.scope and spec.fault_target and spec.fault_action
        )
        decl_scope, decl_target, decl_action = _extract_planning_fault_identity(messages)
        declared = bool(decl_scope or decl_target or decl_action)

        if declared:
            conflict = (
                (decl_scope and decl_scope != spec.scope)
                or (decl_target and decl_target != spec.fault_target)
                or (decl_action and decl_action != spec.fault_action)
            )
            if identity_complete and conflict:
                if not state.get("_identity_split_nudged"):
                    result["planning_rejected"] = True
                    result["_identity_split_nudged"] = True
                    result.update(_identity_nudge_plan_family_reset())
                    result["messages"] = [HumanMessage(content=wrap_system_reminder(
                        "**IDENTITY SPLIT**: The fault identity declared in "
                        "finish_planning conflicts with the reviewed FaultSpec, "
                        "and a reviewed identity is never rewritten by a "
                        "declaration.\n\n"
                        f"- Declared: {decl_scope or '-'}/{decl_target or '-'}/{decl_action or '-'}\n"
                        f"- Reviewed: {spec.scope}/{spec.fault_target}/{spec.fault_action}\n\n"
                        "Resolve the split before proceeding:\n"
                        "1. If the plan's MAIN mechanism really attacks a "
                        "different identity than reviewed, call "
                        "`propose_plan_change` with the new triple — the "
                        "change goes through user confirmation.\n"
                        "2. Otherwise re-run `finish_planning` with a "
                        "declaration that matches the reviewed identity.\n\n"
                        "Do NOT re-declare the same conflicting triple: after "
                        "this nudge a still-conflicting declaration is "
                        "discarded and the reviewed identity stands."
                    ))]
                    return result
                # Already nudged once — the reviewed identity stands and the
                # conflicting declaration is discarded (the guard remains
                # the final arbiter); fall through without any rewrite.
            elif not identity_complete:
                if decl_scope and decl_scope != spec.scope:
                    updates["scope"] = decl_scope
                    if spec.names:
                        updates["names"] = ()
                    if spec.labels:
                        updates["labels"] = {}
                if decl_target and decl_target != spec.fault_target:
                    updates["fault_target"] = decl_target
                if decl_action and decl_action != spec.fault_action:
                    updates["fault_action"] = decl_action
        elif not identity_complete:
            # No declaration → derive from the plan's Execution Steps first
            # (the LLM's actual plan — never the case menu): fill-vacuum
            # only, zero extra round-trips when the plan itself carries a
            # blade command.
            plan_text = result.get("plan") or state.get("plan") or ""
            derived_scope, derived_target, derived_action = _derive_scope_target_action(
                _plan_section(plan_text, "execution steps")
            )
            if derived_scope and not spec.scope:
                updates["scope"] = derived_scope
            if derived_target and not spec.fault_target:
                updates["fault_target"] = derived_target
            if derived_action and not spec.fault_action:
                updates["fault_action"] = derived_action
            # Still incomplete after the plan tier (kubectl-native plan with
            # no blade pattern anywhere) → nudge once for an explicit
            # declaration: only the planner knows which family a
            # kubectl-native mechanism belongs to, and the system will not
            # invent it (#49).
            _spec_after = spec.replace(**updates) if updates else spec
            if (
                not (_spec_after.scope and _spec_after.fault_target and _spec_after.fault_action)
                and not state.get("_identity_declaration_nudged")
            ):
                result["planning_rejected"] = True
                result["_identity_declaration_nudged"] = True
                result.update(_identity_nudge_plan_family_reset())
                result["messages"] = [HumanMessage(content=wrap_system_reminder(
                    "**IDENTITY UNDECLARED**: The plan is finalized but the "
                    "fault identity (scope / target / action) could not be "
                    "resolved, and the system will NOT invent one — the "
                    "skill-case document is a menu (main path + backup means), "
                    "so copying a triple from it can hijack the injection onto "
                    "a different fault (#49).\n\n"
                    "Re-run `finish_planning` declaring the identity triple of "
                    "the plan's MAIN injection mechanism:\n"
                    "- fault_scope: pod | node | container\n"
                    "- fault_target: the resource family attacked (cpu / mem / "
                    "network / disk / process / ...)\n"
                    "- fault_action: the fault action (fullload / burn / hold / "
                    "...)\n\n"
                    "A kubectl-native mechanism declares the family it belongs "
                    "to (e.g. a file-descriptor hold is process)."
                ))]
                return result
            # Nudged once, still unresolved → proceed without inventing: an
            # identity that stays incomplete fails safety_check honestly with
            # "No target specified" instead of executing a guessed fault.

        # 2b. duration_seconds contract backfill (B14).
        #
        # CLI NL mode: from_cli_nl intentionally leaves duration=0 for an
        # intent-extraction node the pipeline route never visits
        # (route_pipeline_start sends CLI NL straight to agent_loop;
        # intent_clarification is TUI-only). Without this backfill the
        # duration contract stays empty end-to-end (audit snapshots show
        # duration_seconds=0) and kubectl-native sleep timers ran with no
        # recorded window at all (#8/#9 evidence: user-asked 300s executed
        # as 300s, invisible to the contract). The planner's
        # finish_planning now carries an explicit duration_seconds
        # declaration; combine it with any hard-pinned entry value (CLI
        # --duration) monotonically and apply ensure_min_duration
        # (unspecified → configured default; declared values verbatim),
        # so every entry path lands the same contract the structured/TUI
        # constructors already apply.
        declared_duration = _extract_planning_duration(messages)
        _dur_candidate = max(declared_duration, spec.duration_seconds)
        _floor_scope = updates.get("scope") or spec.scope
        _floor_target = updates.get("fault_target") or spec.fault_target
        _floor_action = updates.get("fault_action") or spec.fault_action
        effective_duration = ensure_min_duration(
            _dur_candidate if _dur_candidate > 0 else None,
            _floor_scope, _floor_target, _floor_action,
        )
        if effective_duration != spec.duration_seconds:
            updates["duration_seconds"] = effective_duration
            logger.info(
                "extract_planning_metadata: duration contract backfilled "
                "declared=%ss previous=%ss effective=%ss (via "
                "ensure_min_duration)",
                declared_duration, spec.duration_seconds, effective_duration,
            )

        # 2c. case_resource_path backfill (same spec-backfill family as 2b):
        # the planner already hands the chosen case path to
        # finish_planning/save_fault_plan (skill_case_content extraction
        # reads it above), but the spec's own audit/hint field stayed
        # empty. Write-once — an intent-dialogue-settled path
        # (case_resource_path on the TUI spec) always wins.
        if not spec.case_resource_path:
            _case_path = _extract_chosen_skill_case_path(messages)
            if _case_path:
                updates["case_resource_path"] = _case_path

    if updates:
        new_spec = spec.replace(**updates)
        result["fault_spec"] = new_spec.to_dict()
        logger.debug(
            "spec-write: writer=extract_planning_metadata "
            "names %s -> %s basis=skill-case fault_type derivation + "
            "duration contract backfill (names never modified here)",
            list(spec.names), list(new_spec.names),
        )
        logger.info(
            "extract_planning_metadata: derived spec fields %s "
            "(CLI NL or initially incomplete spec path)",
            {k: v for k, v in updates.items()},
        )

    # Leaving Phase 1 for execution: remove any ephemeral capability-probe
    # debug pods the planner created (via kubectl_read debug) so they do not
    # linger into execution and a reject at the confirmation gate leaves the
    # cluster untouched. Idempotent + covers intent-created probes (same task
    # message history). Nudge/retry paths that loop back to agent_loop return
    # earlier and intentionally skip this.
    from chaos_agent.agent.nodes.planning._planning_cleanup import (
        cleanup_planning_debug_pods,
    )
    result.update(await cleanup_planning_debug_pods(state))

    return result
