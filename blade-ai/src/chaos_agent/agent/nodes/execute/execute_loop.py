"""Execute loop node: Phase 2 ReAct execution (follow skill instructions to call blade)."""

import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from chaos_agent.agent.node_names import EXECUTE_LOOP
from chaos_agent.agent.execution_artifacts import (
    cleanup_debug_pod_artifacts,
    collect_execution_artifacts,
)
from chaos_agent.agent.capabilities import (
    build_capability_context,
    filter_tools_for_context,
)
from chaos_agent.agent.spec.fault_spec import read_fault_spec
from chaos_agent.agent.nodes.execute._kubeconfig_inject import (
    _resolve_kubeconfig,
    inject_kubeconfig_into_tool_calls,
    inject_task_id_into_tool_calls,
    sync_kubewiz_runtime,
)
from chaos_agent.agent.nodes.store._store_sync import sync_to_store
from chaos_agent.agent.nodes.execute.llm_step_helpers import (
    build_stagnation_hint,
    persist_corrective_hint,
    persist_replaceable_hint,
    filter_stagnant_tool,
    post_invoke_debug,
)
from chaos_agent.agent.nodes.execute.react_helpers import (
    detect_action_stagnation,
    detect_repeated_tool_calls,
    detect_tool_error_hint,
    emit_debug_tool_messages,
    extract_rejected_params,
    extract_tool_call_fields,
    log_reasoning_content,
    record_ai_message,
    record_system_prompt,
    handle_truncated_response,
)
from chaos_agent.agent.replan import (
    REQUEST_REPLAN_TOOL_NAME,
    ReplanRequest,
    parse_replan_request,
)
from chaos_agent.agent.spec.skill_identity import read_active_skill_name
from chaos_agent.agent.state import AgentState
from chaos_agent.agent.state_mgmt.state_helpers import fail_state
from chaos_agent.agent.result.verdict import FailureCategory
from chaos_agent.config.settings import settings
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)
from chaos_agent.utils.blade_uid import extract_blade_uid
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)

MAX_EXECUTE_LOOP = settings.max_execute_loop


def _extract_original_replicas_from_messages(messages: list, resource_name: str) -> int | None:
    """Extract the original replica count for a resource from message history.

    Scans ToolMessages from kubectl get calls (JSON output) that were made
    BEFORE any scale operation, to find the pre-injection replica count.
    """
    import re as _re
    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        name = getattr(msg, "name", "") or ""
        if name != "kubectl":
            continue
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        # Look for "replicas": N in JSON output
        if resource_name in content and '"replicas"' in content:
            match = _re.search(r'"replicas"\s*:\s*(\d+)', content)
            if match:
                count = int(match.group(1))
                # Sanity check: replicas should be > 0 and reasonable
                if 0 < count <= 1000:
                    return count
    return None


def _parse_blade_uid_from_content(content) -> str | None:
    """Extract a ChaosBlade UID from ToolMessage content.

    Thin wrapper around `chaos_agent.utils.blade_uid.extract_blade_uid` —
    accepts the raw `content` field of a ToolMessage (string or other) and
    delegates multi-strategy parsing to the shared util.
    """
    if not isinstance(content, str):
        return None
    return extract_blade_uid(content)


def _parse_uid_from_status_content(content) -> str | None:
    """Extract experiment UID from blade_status or blade_query_k8s output.

    blade_status / blade_query_k8s return:
        {"code":200,"success":true,"result":{"uid":"<hex>","phase":"Running",...}}

    Unlike blade_create (where ``result`` is a string UID), these tools
    return ``result`` as a **dict** containing a ``uid`` field. The
    standard ``extract_blade_uid`` does not handle this case because its
    strategy 1 only accepts string results, and its regex strategy expects
    UUID format (8-4-4-4-12) while ChaosBlade UIDs are short hex strings.
    """
    if not isinstance(content, str) or not content:
        return None

    # First try the standard extractor (handles blade_create format
    # where result is a string, and chaosblade-<hex> resource names)
    uid = extract_blade_uid(content)
    if uid:
        return uid

    # Handle blade_status/blade_query_k8s format where result is a dict
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    # ChaosBlade success response with dict result
    if data.get("success") is True and data.get("code") == 200:
        result = data.get("result")
        if isinstance(result, dict):
            uid = result.get("uid")
            if isinstance(uid, str) and uid:
                return uid

    return None


def _collect_destroyed_uids(messages: list) -> set[str]:
    """UIDs the LLM has issued ``blade_destroy`` for.

    A UID sent to ``blade_destroy`` is no longer an active injection: whether
    the destroy succeeded (experiment gone) or failed (residual/errored
    experiment), it must NOT be picked up as the current fault's blade_uid.
    Without this guard, a failed-then-cleaned-up experiment's residual UID
    (echoed back by a post-cleanup ``blade_status`` check) pollutes
    ``state.blade_uid`` and misroutes the verifier onto the ChaosBlade Layer-1
    path for an experiment that no longer exists.
    """
    destroyed: set[str] = set()
    for msg in messages:
        for tc in (getattr(msg, "tool_calls", None) or []):
            name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            if name != "blade_destroy":
                continue
            args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            uid = args.get("uid", "") if isinstance(args, dict) else ""
            if uid:
                destroyed.add(uid)
    return destroyed


def _extract_blade_uid_from_messages(messages: list) -> str | None:
    """Scan messages for an experiment uid from a blade-family tool's output.

    ChaosBlade `blade create` returns JSON like:
        {"code": 200, "success": true, "result": "<uid>"}

    Sources scanned, in priority order:
      1. an experiment-creating tool (``blade_create`` for OS / K8s faults,
         ``blade_python_create`` for in-process application faults),
      2. ``kubectl exec ... blade create`` — the bypass the LLM may use when the
         blade tool fails on the host, where the success JSON lands in a kubectl
         ToolMessage,
      3. ``blade_status`` / ``blade_query_k8s`` — relevant when the create call
         timed out but the experiment was in fact created, so the LLM discovered
         the uid via a status query (uid nested in a dict ``result`` field).

    Only kubectl exec calls whose v_args contain "blade create" are considered —
    other kubectl outputs (get -o json, describe, ...) are NOT scanned, to
    prevent false-positive extraction from K8s resource ``metadata.uid`` fields.
    """
    kubectl_uid = None  # fallback uid from kubectl exec
    status_uid = None   # fallback uid from blade_status / blade_query_k8s

    # UIDs already sent to blade_destroy are cleaned-up / residual — never
    # treat them as the current active injection (root-cause guard).
    destroyed = _collect_destroyed_uids(messages)

    # Build a set of tool_call_ids that correspond to "kubectl exec ... blade create"
    blade_exec_call_ids: set[str] = set()
    for msg in messages:
        if not hasattr(msg, "tool_calls"):
            continue
        for tc in (msg.tool_calls or []):
            name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
            if name == "kubectl" and isinstance(args, dict):
                v_args = args.get("v_args", "")
                if "blade" in v_args and "create" in v_args:
                    blade_exec_call_ids.add(tc_id)

    # Check if an experiment-creating tool was attempted (even if it failed /
    # timed out). blade_status UID extraction is only relevant then — otherwise
    # the status check might pick up unrelated experiments.
    _EXPERIMENT_CREATE_TOOLS = ("blade_create", "blade_python_create")
    _has_blade_create = any(
        isinstance(msg, ToolMessage)
        and getattr(msg, "name", "") in _EXPERIMENT_CREATE_TOOLS
        for msg in messages
    )

    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            continue
        msg_name = getattr(msg, "name", "") or ""
        content = msg.content

        # Priority 1: a ToolMessage from an experiment-creating tool. Both
        # ``blade_create`` (OS / K8s carrier) and ``blade_python_create``
        # (in-process application carrier) return the same ChaosBlade CLI JSON
        # with the experiment uid, and both recover via ``blade destroy <uid>``.
        # Missing the python tool here would leave ``blade_uid`` unset on the
        # ReAct path, so verification and recovery would have no uid to act on.
        if msg_name in _EXPERIMENT_CREATE_TOOLS:
            uid = _parse_blade_uid_from_content(content)
            if uid and uid not in destroyed:
                return uid

        # Priority 2: kubectl exec blade ToolMessage ONLY
        if msg_name == "kubectl" and not kubectl_uid:
            tool_call_id = getattr(msg, "tool_call_id", "") or ""
            if tool_call_id in blade_exec_call_ids:
                _uid = _parse_blade_uid_from_content(content)
                if _uid and _uid not in destroyed:
                    kubectl_uid = _uid

        # Priority 3: blade_status / blade_query_k8s ToolMessage
        # Relevant when blade_create timed out but experiment was created.
        if msg_name in ("blade_status", "blade_query_k8s") and not status_uid:
            if _has_blade_create:
                _uid = _parse_uid_from_status_content(content)
                if _uid and _uid not in destroyed:
                    status_uid = _uid

    # Return by priority: blade_create > kubectl exec > blade_status
    return kubectl_uid or status_uid


# Regex for: blade create k8s <scope>-<target> <action>
# e.g. "blade create k8s pod-network drop --percent 100 ..."
_BLADE_CREATE_K8S_RE = re.compile(
    r"blade\s+create\s+k8s\s+(\w+)-(\w+)\s+(\w+)"
)

def _parse_blade_create_from_v_args(v_args: str) -> dict | None:
    """Parse scope/target/action/flags from kubectl exec blade create v_args.

    Returns dict with scope/target/action, plus ``flags`` if present, or None
    if v_args does not contain a ``blade create k8s`` command.
    """
    match = _BLADE_CREATE_K8S_RE.search(v_args)
    if not match:
        return None
    result = {"scope": match.group(1), "target": match.group(2), "action": match.group(3)}
    flags_str = v_args[match.end():].strip()
    if flags_str:
        result["flags"] = flags_str
    return result


def _build_replan_context(state: AgentState, request: ReplanRequest) -> dict:
    """Extract structured error context from conversation history for Phase 1 replan."""
    messages = state.get("messages", [])
    failed_calls = []
    existing_uids = []

    for msg in reversed(messages):
        if isinstance(msg, ToolMessage):
            name = getattr(msg, "name", "") or ""
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            tool_call_id = getattr(msg, "tool_call_id", "")

            # Collect failed and successful blade_create calls
            if name == "blade_create":
                try:
                    data = json.loads(content)
                    if not data.get("success", True):
                        failed_calls.append({
                            "name": name,
                            "tool_call_id": tool_call_id,
                            "error": content[:500],
                        })
                    else:
                        uid = data.get("result", "")
                        if uid:
                            existing_uids.append(uid)
                except (json.JSONDecodeError, TypeError):
                    if "error" in content.lower() or "fail" in content.lower():
                        failed_calls.append({
                            "name": name,
                            "tool_call_id": tool_call_id,
                            "error": content[:500],
                        })
            elif (
                getattr(msg, "status", None) == "error"
                or content.startswith("Error")
                or content.startswith("[target_guard]")
            ):
                failed_calls.append({
                    "name": name,
                    "tool_call_id": tool_call_id,
                    "error": content[:500],
                })

            if len(failed_calls) >= 5:
                break

    # Extract rejected params from all error sources
    all_rejected: list[str] = extract_rejected_params(request.invalidated_assumption)
    failed_tool_names: set[str] = set()
    for fc in failed_calls:
        all_rejected.extend(extract_rejected_params(fc.get("error", "")))
        if fc.get("name"):
            failed_tool_names.add(fc["name"])

    runtime_evidence_refs = list(dict.fromkeys(
        call["tool_call_id"]
        for call in failed_calls
        if call.get("tool_call_id")
    ))

    return {
        "error_summary": request.invalidated_assumption,
        **request.as_context(),
        # Runtime, not the model, owns opaque tool-call identifiers. Keep any
        # model-provided semantic references separately for audit context.
        "model_evidence_refs": list(request.evidence_refs),
        "evidence_refs": runtime_evidence_refs,
        "failed_tool_calls": failed_calls,
        "existing_blade_uids": existing_uids,
        "iteration_at_failure": state.get("execute_loop_count", 0),
        "rejected_params": list(dict.fromkeys(all_rejected)),
        "failed_tool_names": sorted(failed_tool_names),
    }


def _detect_consecutive_idle_turns(
    messages: list,
    replan_exhausted: bool = False,
) -> str | None:
    """Detect when the LLM is stuck producing text-only responses with no tools.

    Scans the most recent AI messages. If >= 3 consecutive AI messages have no
    tool_calls, the LLM is likely stuck in a "can't execute" loop and should
    either try a new tool or make a definitive conclusion.

    Early-exit on duplication: if just 2 consecutive idle AI messages have
    substantially similar content (first 50 chars match), the hint fires
    immediately — prevents the user from seeing the same text 3× before
    intervention.

    The hint adapts to ``replan_exhausted``: when ``replan_count >=
    max_replan_count`` the system can no longer route a replan request back
    to Phase 1, so suggesting it would invite an infinite loop where
    the LLM keeps requesting replan and the router keeps falling
    through to "continue" (the exact stuck-loop the user reports).
    With ``replan_exhausted=True`` the hint drops the replan
    option entirely and asks the LLM for a final conclusion.

    Returns a convergence hint if a stuck loop is detected, or None.
    """
    threshold = settings.idle_turn_threshold
    # Collect the last N AI messages (skipping non-AI messages)
    recent_ai = []
    for msg in reversed(messages):
        if hasattr(msg, "type") and msg.type == "ai":
            recent_ai.append(msg)
            if len(recent_ai) >= threshold:
                break

    # Early-exit on content duplication: if the last 2 AI messages are
    # both text-only AND have substantially similar content (first 50
    # chars match), fire the hint immediately — prevents the user from
    # seeing the same output repeated before the count-based threshold.
    content_dup = False
    if len(recent_ai) >= 2:
        m0, m1 = recent_ai[0], recent_ai[1]
        idle0 = not (hasattr(m0, "tool_calls") and m0.tool_calls)
        idle1 = not (hasattr(m1, "tool_calls") and m1.tool_calls)
        if idle0 and idle1:
            c0 = (getattr(m0, "content", "") or "").strip()
            c1 = (getattr(m1, "content", "") or "").strip()
            if c0 and c1 and c0[:50] == c1[:50]:
                content_dup = True

    if not content_dup:
        # Original path: need threshold consecutive idle turns
        if len(recent_ai) < threshold:
            return None
        all_idle = all(
            not (hasattr(m, "tool_calls") and m.tool_calls)
            for m in recent_ai
        )
        if not all_idle:
            return None

    if replan_exhausted:
        return (
            f"**EXECUTION CONVERGENCE NOTICE**: {threshold} consecutive responses "
            f"contained no tool calls, and the replan budget is exhausted. "
            f"A replan request can no longer change the graph state. Choose a response "
            f"that is grounded in the current evidence: take a safe, meaningful "
            f"available action if one remains; otherwise provide a concise final "
            f"conclusion without repeating prior text."
        )
    return (
        f"**EXECUTION CONVERGENCE NOTICE**: {threshold} consecutive responses "
        f"contained no tool calls and no new execution evidence. Reassess the "
        f"current hypothesis before continuing. Select a non-redundant safe action, "
        f"provide an evidence-based conclusion, or emit the structured replan "
        f"request when the approved plan itself requires reconsideration."
    )


def _detect_injection_method(
    messages: list, blade_uid: str | None, *, is_host: bool = False
) -> str | None:
    """Detect the injection method used based on conversation history.

    Determines how the fault was actually injected so the verifier can
    choose the correct Layer 1 verification strategy.

    Args:
        messages: The conversation history to scan.
        blade_uid: The extracted ChaosBlade UID, if any.
        is_host: Whether the resolved transport channel targets a host
            (ssh / kubewiz_host). Enables the ``host_native`` branch.

    Returns:
        "host_blade" | "kubectl_exec" | "kubectl_native" | "host_native" | None
    """
    # Delegated to the FaultProvider registry, the single dispatch point that
    # replaced the former inline fall-through. The registry first scopes
    # candidates by CHANNEL (``is_host`` → profile; a k8s channel never probes
    # the host backend, and vice versa), then probes the survivors in
    # precedence order (ChaosBlade UID scan → kubectl-native → host-native).
    # Attribution keys on the injection ATTEMPT (AIMessage tool_calls), not on
    # command text or tool result: ChaosbladeProvider owns the reverse UID scan;
    # K8sNativeProvider / HostShellProvider gate on ``not blade_uid`` (host also
    # on ``is_host``) and treat an attempted mutating call as the carrier.
    from chaos_agent.agent.providers import FaultProviderRegistry

    return FaultProviderRegistry.detect_method(messages, blade_uid, is_host=is_host)


def _should_redetect_injection_method(
    current_injection_method: str | None, blade_uid: str | None
) -> bool:
    """Gate the per-iteration history re-scan (channel B) to its two real jobs.

    Direction B records ``injection_method`` at ISSUE time (channel A), so the
    reverse history scan is only needed to:

    - RESUME: recover attribution when nothing is recorded yet (``current`` is
      empty) — e.g. after a restart where the injection lives in history, or on
      the first injection turn before channel A commits it.
    - UPGRADE: promote the provisional multi-step ``kubectl_native`` to the
      experiment backend once a ``blade_uid`` appears (the UID lives in the tool
      RESULT, which channel A cannot see).

    In steady state (a non-multi-step method already set, or no new UID) the
    scan would only re-derive the same answer, so we skip it.
    """
    if not current_injection_method:
        return True
    if not blade_uid:
        return False
    from chaos_agent.agent.providers import FaultProviderRegistry

    provider = FaultProviderRegistry.resolve_by_method(current_injection_method)
    return provider is not None and provider.is_multi_step

def _build_execution_hints(
    messages: list,
    state: AgentState,
    persist_into: list | None = None,
    counts_out: dict | None = None,
) -> tuple[list[HumanMessage], str | None]:
    """Build all execution-phase hints to inject before the LLM call.

    Returns (hints, stagnant_tool) where stagnant_tool is the tool name
    that should be filtered from bindings, or None.

    ``persist_into`` collects the copies that must reach ``result["messages"]``.
    Corrective hints (loop / stagnation / tool-error / idle) are re-derived every
    iteration, so a turn-local copy reads as a first-time warning forever and the
    model never learns it has already been told — measured in task-e9ee12d6,
    where the notice fired from turn 11 and the same call was issued 31 more
    times. The per-iteration convergence hints deliberately do NOT persist: their
    text names the current iteration number, so a stale copy would tell a later
    turn it has more budget than it does.
    """
    hints: list[HumanMessage] = []
    _persist = persist_into if persist_into is not None else []
    _history = state.get("messages", [])
    # Counts live on state so compaction cannot reset them; the caller folds
    # ``counts_out`` into its state update.
    _counts = counts_out if counts_out is not None else {}
    _counts.update(state.get("hint_repeat_counts") or {})

    loop_hint = detect_repeated_tool_calls(messages, phase="execute")
    if loop_hint:
        hints.append(persist_corrective_hint(
            _persist, _history, "loop", "execute", loop_hint,
            escalate_after=settings.hint_escalate_after,
            counts=_counts, counts_out=_counts,
        ))

    _, stagnant_tool = detect_action_stagnation(messages, phase="execute")
    if stagnant_tool:
        exec_hint = build_stagnation_hint(
            stagnant_tool,
            colon_suffix="to complete remaining injection steps",
            else_actions=[
                "Use a DIFFERENT tool to achieve the injection goal.",
                "Output your conclusion if injection already succeeded "
                "(include the injection UID if available).",
                "Emit a structured replan request only if the approved plan itself requires reconsideration.",
            ],
        )
        hints.append(persist_corrective_hint(
            _persist, _history, "stagnation", stagnant_tool, exec_hint,
            escalate_after=settings.hint_escalate_after,
            counts=_counts, counts_out=_counts,
        ))

    error_hint = detect_tool_error_hint(messages)
    if error_hint:
        hints.append(persist_corrective_hint(
            _persist, _history, "tool_error", "execute", error_hint,
            counts=_counts, counts_out=_counts,
        ))

    try:
        _max_replan = int(settings.max_replan_count)
    except (TypeError, ValueError):
        _max_replan = 2
    replan_exhausted = state.get("replan_count", 0) >= _max_replan
    idle_hint = _detect_consecutive_idle_turns(
        messages, replan_exhausted=replan_exhausted
    )
    if idle_hint:
        hints.append(persist_corrective_hint(
            _persist, _history, "idle", "execute", idle_hint,
            escalate_after=settings.hint_escalate_after,
            counts=_counts, counts_out=_counts,
        ))

    # conflict_uids: already handled by the pre-execution conflict gate.
    # Residual experiments are NOT the executor's concern — do NOT inject
    # hints about them, as they cause the LLM to investigate/verify instead
    # of focusing on its injection task.

    return hints, stagnant_tool


def _process_response_tool_calls(
    response,
    state: AgentState,
    result: dict,
    tracker,
    count: int,
) -> None:
    """Process tool_calls from the LLM response: blade params, FCAT, scale tracking."""
    tool_calls = getattr(response, "tool_calls", None) or []
    # A productive turn (issued tool calls) breaks any text-only stall streak:
    # reset the consecutive-stall counter so a later, unrelated stall gets a
    # fresh nudge budget instead of inheriting an old strike.
    if tool_calls and state.get("_execute_text_stall_count"):
        result["_execute_text_stall_count"] = 0
    for tc in tool_calls:
        tc_name, tc_args = extract_tool_call_fields(tc)

        if tc_name == "blade_create":
            flags_str = tc_args.get("flags", "")
            if flags_str:
                from chaos_agent.utils.fault_type import parse_blade_flags
                parsed = parse_blade_flags(flags_str)
                if parsed:
                    result["blade_parsed_flags"] = parsed
            logger.info(f"Blade create params: {tc_args}")

            target_metadata = state.get("target_metadata") or {}
            from chaos_agent.utils.fault_context import (
                lookup_adaptations, compute_safe_burn_size,
            )
            from chaos_agent.agent.spec.fault_spec import read_fault_spec as _rfs
            _spec_for_fcat = _rfs(state)
            _scope = (_spec_for_fcat.scope if _spec_for_fcat else "") or tc_args.get("scope", "")
            _target = (_spec_for_fcat.blade_target if _spec_for_fcat else "") or tc_args.get("target", "")
            _action = (_spec_for_fcat.blade_action if _spec_for_fcat else "") or tc_args.get("action", "")
            adaptations = lookup_adaptations(
                _scope, _target, _action, target_metadata,
                rule_type="param_override",
            )
            for adj in adaptations:
                if adj.mode in ("llm", "both") and "param_overrides" in adj.action:
                    for key, val in adj.action["param_overrides"].items():
                        if key == "size" and val == "auto":
                            safe_size = compute_safe_burn_size(
                                target_metadata.get("pod_memory_limit_mb")
                            )
                            tc_args[key] = str(safe_size)
                        else:
                            tc_args[key] = val
                    logger.info(
                        "FCAT: %s applied, params adjusted: %s",
                        adj.id, adj.action["param_overrides"],
                    )
                    from chaos_agent.memory.session_store import get_global_session_store
                    _fcat_store = get_global_session_store()
                    _fcat_tid = state.get("task_id", "")
                    if _fcat_store and _fcat_tid:
                        _mem_str = (
                            "unavailable" if target_metadata.get("pod_memory_limit_mb") is None
                            else f"{target_metadata.get('pod_memory_limit_mb')}MB"
                        )
                        _fcat_msg = f"[FCAT P0] {adj.id}: size adjusted to {tc_args.get(key, safe_size)}MB (pod_memory_limit={_mem_str})"
                        _fcat_store.append_messages(_fcat_tid, [HumanMessage(content=_fcat_msg)], node_name=EXECUTE_LOOP)
                    if settings.is_debug and tracker:
                        _mem_str_dbg = (
                            "unavailable" if target_metadata.get("pod_memory_limit_mb") is None
                            else f"{target_metadata.get('pod_memory_limit_mb')}MB"
                        )
                        tracker.update(
                            f"[FCAT P0] {adj.id}: size→{tc_args.get(key, safe_size)}MB (pod_mem={_mem_str_dbg})"[:200],
                            {"debug": True, "fcat": True},
                        )

        if tc_name == "kubectl" and tc_args.get("subcommand") == "exec":
            v_args = tc_args.get("v_args", "") or ""
            if "blade" in v_args and "create" in v_args:
                parsed = _parse_blade_create_from_v_args(v_args)
                if parsed:
                    flags_str = parsed.get("flags", "")
                    if flags_str:
                        from chaos_agent.utils.fault_type import parse_blade_flags
                        parsed_flags = parse_blade_flags(flags_str)
                        if parsed_flags:
                            result["blade_parsed_flags"] = parsed_flags
                    logger.info(
                        f"Kubectl exec blade params: scope={parsed['scope']}, "
                        f"target={parsed['target']}, action={parsed['action']}"
                    )

        if tc_name == "kubectl" and tc_args.get("subcommand") == "scale":
            v_args = tc_args.get("v_args", "")
            import re as _re
            replicas_match = _re.search(r"--replicas=(\d+)", v_args)
            resource_match = _re.search(
                r"(?:deployment|statefulset)\s+(\S+)", v_args
            )
            if replicas_match and resource_match:
                new_replicas = int(replicas_match.group(1))
                resource_name = resource_match.group(1)
                existing = state.get("original_replicas") or {}
                if resource_name not in existing:
                    orig_count = _extract_original_replicas_from_messages(
                        state.get("messages", []), resource_name
                    )
                    if orig_count is not None and orig_count != new_replicas:
                        existing[resource_name] = orig_count
                        result["original_replicas"] = existing
                        logger.info(
                            f"Recorded original_replicas: {resource_name}={orig_count}"
                        )

    # Direction B: record injection_method at ISSUE time from the freshly
    # issued tool_calls, rather than reverse-scanning history later. Only the
    # UID-less native methods (kubectl_native / host_native) are committed here
    # — the attempt IS the injection, so a severed exec result cannot hide it.
    # The experiment methods (host_blade / kubectl_exec) are deferred to the
    # blade_uid path (proof the ChaosBlade experiment actually succeeded), so a
    # failed blade attempt that falls back to kubectl-native is not
    # mis-recorded. Never overrides an already-set method (monotonic); the
    # blade_uid upgrade stays in the caller's re-detect block.
    if not (state.get("injection_method") or result.get("injection_method")):
        from chaos_agent.agent.nodes.execute._injection_detection import (
            classify_issue_time_method,
        )
        from chaos_agent.transports.registry import is_host_scope_channel

        _is_host = is_host_scope_channel(state)
        for tc in tool_calls:
            tc_name, tc_args = extract_tool_call_fields(tc)
            _issued = classify_issue_time_method(tc_name, tc_args, is_host=_is_host)
            if _issued in ("kubectl_native", "host_native"):
                result["injection_method"] = _issued
                logger.info("Recorded injection_method at issue time: %s", _issued)
                if (
                    not state.get("injection_start_time")
                    and "injection_start_time" not in result
                ):
                    result["injection_start_time"] = now_iso()
                    logger.info("Set injection_start_time (%s issued)", _issued)
                break

    post_invoke_debug(tracker, response, count, "Iteration")


def _detect_terminal_conclusion(
    response,
    state: AgentState,
    result: dict,
) -> None:
    """Detect when LLM gives a text-only terminal conclusion in Phase 2.

    The executor's job is ONLY injection. When the LLM outputs text (no
    tool_calls), we check whether it has actually performed an injection
    action. If an injection method has been detected (blade_uid set or
    injection_method detected), the text conclusion is the natural exit
    signal — do NOT nudge it back. Only nudge when NO injection action
    has been taken at all.

    EXCEPTION: multi-step injections (kubectl_native / host_shell) often span
    several actions (e.g., ``patch`` to add a finalizer, then ``delete`` to
    trigger termination). Setting the injection_method after the first step and
    then allowing a text-only exit could silently skip remaining steps. Before
    letting a multi-step backend exit, we offer a one-shot soft step self-check
    via ``build_injection_step_selfcheck`` (LLM decides completeness).
    """
    _has_tool_calls = bool(getattr(response, "tool_calls", None))
    _has_uid = bool(result.get("blade_uid") or state.get("blade_uid"))
    _injection_method = result.get("injection_method") or state.get("injection_method")
    _resp_content = (getattr(response, "content", "") or "").strip()

    # Blade injection (uid set) → single-step, text conclusion is correct.
    if _has_uid:
        return

    # A multi-step backend (kubectl_native / host_shell) may span several
    # injection steps. Before allowing a text-only exit, offer a SOFT one-shot
    # self-check when the skill case is multi-step. The backend declares its
    # eligibility via ``is_multi_step``; whether THIS scenario is multi-step is
    # read from the skill case. We do NOT verify each step programmatically
    # (brittle) — the LLM self-verifies and may exit if it judges the injection
    # complete.
    from chaos_agent.agent.providers import FaultProviderRegistry

    _method_provider = FaultProviderRegistry.resolve_by_method(_injection_method)
    if (
        not _has_tool_calls
        and _method_provider is not None
        and _method_provider.is_multi_step
        and not state.get("_injection_selfcheck_nudged")
    ):
        from chaos_agent.agent.nodes.execute._injection_detection import (
            build_injection_step_selfcheck,
        )
        _skill_case = (
            result.get("skill_case_content")
            or state.get("skill_case_content", "")
        )
        _all_msgs = state.get("messages", []) + result.get("messages", [])
        _selfcheck = build_injection_step_selfcheck(
            _skill_case, _all_msgs, _injection_method,
        )
        if _selfcheck:
            logger.info(
                "multi-step injection: emitting one-shot step self-check "
                "before allowing text-only exit"
            )
            result.setdefault("messages", []).append(
                HumanMessage(content=_selfcheck)
            )
            # Give the LLM one more turn to act on the self-check (route
            # "continue" via should_continue_execute_loop:336). The one-shot
            # guard ensures any later text-only exit passes through freely —
            # the LLM may conclude if it judges the injection complete.
            result["injection_method"] = None
            result["_injection_selfcheck_nudged"] = True
            return  # Don't fall through to generic nudge below

    # Non-kubectl_native injection method (host_blade, kubectl_exec)
    # or kubectl_native with all steps complete → exit is correct.
    if _injection_method:
        return

    # No injection method at all — text-only without any injection action.
    if (
        not _has_tool_calls
        and not result.get("error")
        and _resp_content
        and parse_replan_request(_resp_content) is None
    ):
        # Consecutive text-only stall: no tool call, no injection recorded, and
        # no parseable replan. A productive turn (tool_calls issued) resets this
        # counter in ``_process_response_tool_calls``, so only a genuine STREAK
        # of stalls accumulates — unrelated stalls separated by real work each
        # get their own nudge budget. Nudge until the budget is spent, then fail
        # fast so a stuck executor cannot burn the whole loop budget.
        try:
            max_stalls = int(settings.max_execute_text_stalls)
        except (TypeError, ValueError):
            max_stalls = 3
        if max_stalls < 1:
            max_stalls = 1
        stall_count = state.get("_execute_text_stall_count", 0) + 1
        if stall_count < max_stalls:
            result.setdefault("messages", []).append(
                HumanMessage(content=(
                    "**EXECUTION REQUIRED**: You output text instead of "
                    "calling a tool. You are in Phase 2 (execution) — "
                    "the plan is already approved. Call the injection "
                    "tool your approved plan requires (from your bound "
                    "tools) NOW to carry out the fault. Do NOT output "
                    "plans, summaries, or wait for confirmation. Execute "
                    "immediately."
                ))
            )
            result["_execute_text_stall_count"] = stall_count
        else:
            result.update(fail_state(
                FailureCategory.EXECUTION_FAILED,
                "LLM concluded without tool use",
                state.get("messages", []) + result.get("messages", []),
            ))


def _parse_replan_tool_call(response):
    """Extract a ReplanRequest from a ``request_replan`` tool call on *response*.

    Returns ``(replan_request, replan_tc_id, tool_calls)`` where ``tool_calls`` is
    the response's full tool_call list (used to answer every call once we route
    away from phase2_tools). Returns ``(None, None, [])`` when no valid
    request_replan call is present (caller then falls back to the free-text
    marker). We only read the CURRENT response's tool_calls (never an old
    ToolMessage — see ``_handle_replan`` docstring).
    """
    tool_calls = getattr(response, "tool_calls", None) or []
    if not tool_calls:
        return None, None, []

    for tc in tool_calls:
        name, args = extract_tool_call_fields(tc)
        if name != REQUEST_REPLAN_TOOL_NAME:
            continue
        # Drop keys whose value is None before validating. The tool signature
        # declares the optional list fields as ``list = None``, so its JSON
        # schema invites the model to emit an explicit ``null`` (e.g.
        # observed_evidence=null). ReplanRequest types those as ``list[str]``
        # (non-nullable), so a raw None would raise a validation error and be
        # misread as "malformed" — silently dropping a real plan_invalid replan.
        # Stripping None lets ReplanRequest's own defaults ([]) apply.
        raw_args = args if isinstance(args, dict) else {}
        clean_args = {k: v for k, v in raw_args.items() if v is not None}
        try:
            replan_request = ReplanRequest.model_validate(clean_args)
        except ValueError:
            # Genuinely malformed args (e.g. missing a required field). Don't
            # fire replan; leave the tool_call for the ToolNode to answer with
            # an error so the model can correct itself.
            logger.warning("request_replan tool call had invalid args: %s", args)
            return None, None, []
        replan_tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
        return replan_request, replan_tc_id, tool_calls

    return None, None, []


def _answer_replan_tool_calls(
    result: dict,
    tool_calls: list,
    replan_tc_id,
    replan_content: str,
) -> None:
    """Synthesize a ToolMessage for every tool_call in a request_replan turn.

    A fired/terminal replan routes to Phase 1 (or end) instead of phase2_tools,
    so these tool_calls never reach the ToolNode. Answering each one keeps the
    AIMessage's tool_calls all resolved so the next LLM call sees well-formed
    history. The synthesized ToolMessage is appended as the LAST message, which
    also keeps the ReAct shape normal for any continue routing.
    """
    if not tool_calls:
        return
    synthesized = []
    for tc in tool_calls:
        name, _ = extract_tool_call_fields(tc)
        tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
        if not tc_id:
            continue
        if tc_id == replan_tc_id:
            content = replan_content
        else:
            content = (
                "Not executed: a replan was requested in the same turn, so the "
                "current plan is being abandoned before this call ran."
            )
        synthesized.append(ToolMessage(content=content, tool_call_id=tc_id, name=name or "tool"))
    if synthesized:
        result.setdefault("messages", []).extend(synthesized)


def _handle_replan(
    response,
    state: AgentState,
    result: dict,
) -> None:
    """Apply state transitions for an explicit Phase 2 replan request.

    Tool errors remain inside the executor's ReAct loop. Looking backwards for
    an old ToolMessage here can pre-empt a newer model action, so graph re-entry
    is intentionally driven only by the model's current response.
    """
    replan_requested = False
    replan_context = None
    replan_request = None
    replan_tc_id = None
    replan_tool_calls = []

    if response is not None:
        # Preferred channel: a structured ``request_replan`` tool call. Tool-calling
        # models emit control signals as tool calls, not free text, so this is the
        # primary path; the ``<replan_request>{...}</replan_request>`` free-text
        # marker below stays as a backward-compatible fallback for text models.
        # We only read the CURRENT response's tool_calls (never an old ToolMessage
        # — see docstring). Because a fired/terminal replan routes to Phase 1 (or
        # end) instead of phase2_tools, every tool_call in this turn is answered
        # with a synthetic ToolMessage (per-outcome, below) to keep history
        # well-formed.
        replan_request, replan_tc_id, replan_tool_calls = _parse_replan_tool_call(response)
        if replan_request is None:
            content = getattr(response, "content", "") or ""
            replan_request = parse_replan_request(content)
        if replan_request is not None:
            replan_requested = True
            replan_context = _build_replan_context(state, replan_request)
            logger.info("Phase 2 LLM requested replan for step=%s", replan_request.affected_step)

    if not replan_requested:
        return

    review_reason = _review_replan_request(state, replan_request)
    if review_reason:
        # needs_investigation: keep executing, do NOT fire a Phase-1 replan.
        if replan_tool_calls:
            # Tool channel: leave the request_replan tool_call UNANSWERED here so
            # it flows once through phase2_tools (routing is "continue" ->
            # phase2_tools, which re-parses the latest AIMessage's tool_calls).
            # We must NOT synthesize a ToolMessage (phase2_tools would still
            # re-execute the call -> a duplicate answer for the same
            # tool_call_id) and must NOT interleave a HumanMessage between the
            # AIMessage and its ToolMessage (breaks tool-response adjacency).
            # The tool's own "Replan request recorded." result + the tool
            # docstring ("needs_investigation ... does NOT replan") carry the
            # semantics; the ReAct loop continues naturally.
            return
        result.setdefault("messages", []).append(HumanMessage(content=(
            f"[LIFECYCLE REVIEW] Continue execution: {review_reason} A tool result "
            "is evidence about that call, not by itself a conclusion that the "
            "approved plan is infeasible."
        )))
        return

    try:
        _max_replan = int(settings.max_replan_count)
    except (TypeError, ValueError):
        _max_replan = 2
    current_replan_count = state.get("replan_count", 0)
    replan_can_fire = current_replan_count < _max_replan

    if replan_can_fire:
        _answer_replan_tool_calls(
            result, replan_tool_calls, replan_tc_id,
            "Replan request recorded; returning to planning.",
        )
        result["replan_requested"] = True
        result["replan_context"] = replan_context
        result["replan_request"] = replan_request.model_dump()
        result["replan_count"] = current_replan_count + 1
        if settings.replan_reset_execute_count:
            result["execute_loop_count"] = 0
        result["error"] = None
        result["approved_target"] = None
        if replan_request.changes_target_or_risk:
            # The structured request is the authoritative declaration that
            # the next plan alters a confirmation boundary.  Re-entering
            # planning alone is insufficient; the new boundary must be shown
            # to the user even if later discovery happens to look similar.
            result["needs_confirmation"] = True
        if not replan_context.get("existing_blade_uids"):
            result["blade_uid"] = None
        history = list(state.get("replan_history") or [])
        history.append({
            "attempt": result["replan_count"],
            "original_error": replan_context.get("error_summary", ""),
            "action_taken": "(pending Phase 1 analysis)",
        })
        result["replan_history"] = history
        from chaos_agent.agent.attempt_tracker import (
            REASON_GRAPH_REPLAN,
            begin_attempt,
        )
        attempt_delta = begin_attempt(
            {**state, **result},
            target=state.get("fault_spec"),
            reason=REASON_GRAPH_REPLAN,
            notes=replan_context.get("error_summary", "")[:200],
        )
        result.update(attempt_delta)
    else:
        _answer_replan_tool_calls(
            result, replan_tool_calls, replan_tc_id,
            "Replan requested but the replan budget is exhausted; converting to "
            "terminal failure.",
        )
        _fs = fail_state(
            FailureCategory.REPLAN_EXHAUSTED,
            f"attempts={current_replan_count}, last_error={(replan_context or {}).get('error_summary', '')[:200]}",
            state.get("messages", []) + result.get("messages", []),
        )
        result.update(_fs)
        result["replan_requested"] = False
        logger.warning(
            "Replan exhausted: LLM emitted a replan request but "
            "replan_count=%d already at max=%d; converting to "
            "terminal failure",
            current_replan_count, _max_replan,
        )


def _review_replan_request(
    state: AgentState,
    request: ReplanRequest,
) -> str | None:
    """Keep explicit investigation in ReAct; plan-invalid requests replan."""
    if request.decision == "needs_investigation":
        return "The request says more investigation is needed."
    return None


async def execute_loop(state: AgentState) -> dict:
    """Phase 2: ReAct loop for execution.

    The LLM follows skill instructions to call blade/kubectl tools.

    Returns updated state fields.
    """
    task_id = state.get("task_id", "") or ""
    skill_name = read_active_skill_name(state)
    count = state.get("execute_loop_count", 0) + 1

    tracker = get_tracker(task_id)
    tracker.start(
        StatusCategory.NODE,
        "execute_loop",
        f"Execute loop iteration {count}: executing skill '{skill_name}'",
        {"iteration": count, "skill_name": skill_name},
    )

    if count > MAX_EXECUTE_LOOP:
        logger.warning(
            f"Execute loop exceeded max iterations ({MAX_EXECUTE_LOOP}) for task "
            f"{task_id}"
        )
        tracker.fail(f"Execute loop exceeded max iterations ({MAX_EXECUTE_LOOP})")
        return fail_state(
            FailureCategory.EXECUTION_TIMEOUT,
            f"max_iterations={MAX_EXECUTE_LOOP}",
        )

    tracker.complete(f"Execute loop iteration {count} done")
    return {"execute_loop_count": count}


async def _check_execute_loop_limits(
    state: AgentState, count: int, task_id: str, tracker,
) -> dict | None:
    """Phase 1 early exits: max-iteration budget and zombie-replan detection.

    Returns a short-circuit result dict (already ``sync_to_store``'d) when the
    loop must terminate, else ``None`` to continue. Pure extraction from
    ``_execute_loop_with_llm`` — behaviour unchanged.
    """
    if count > MAX_EXECUTE_LOOP:
        logger.warning(
            f"Execute loop exceeded max iterations ({MAX_EXECUTE_LOOP}) for task "
            f"{task_id}"
        )
        tracker.fail(f"Execute loop exceeded max iterations ({MAX_EXECUTE_LOOP})")
        result = fail_state(
            FailureCategory.EXECUTION_TIMEOUT,
            f"max_iterations={MAX_EXECUTE_LOOP}",
            state.get("messages", []),
        )
        await sync_to_store(state, result)
        return result

    # --- Zombie-replan early exit ---------------------------------
    # If the previous iteration's replan request pushed ``replan_count``
    # to ``max_replan_count`` AND the router refused to take the
    # "replan" branch (it returns False from ``_should_replan``
    # once the count cap is hit), ``state.replan_requested=True``
    # is sticky and unrelated subsequent iterations cannot escape
    # via any normal path:
    #
    #   * the gate's else branch only fires when THIS iteration's
    #     LLM emits another replan request (with the new no-replan
    #     hint, a well-behaved LLM stops emitting it)
    #   * ``state.error`` was cleared by the prior fire so the
    #     router's error branch can't end the turn
    #   * blade_uid is empty so verifier branch can't end either
    #
    # The router falls through to "continue" and execute_loop
    # spins until ``max_execute_loop`` is hit (default 50) — up
    # to ~47 wasted LLM calls between the cap and the budget. We
    # short-circuit that here: detect the stuck state, terminate
    # cleanly with REPLAN_EXHAUSTED, and let the router's
    # ``state.error`` branch take "end".
    try:
        _max_replan_zombie = int(settings.max_replan_count)
    except (TypeError, ValueError):
        _max_replan_zombie = 2
    zombie_replan = (
        state.get("replan_requested")
        and state.get("replan_count", 0) >= _max_replan_zombie
        and not state.get("blade_uid")
    )
    if zombie_replan:
        stuck_error = (
            f"Replan exhausted after {state.get('replan_count', 0)} "
            f"attempt(s); no further injection paths available."
        )
        logger.warning(
            "Zombie replan detected on task %s: count=%d max=%d; "
            "terminating early to avoid burning execute_loop budget",
            task_id,
            state.get("replan_count", 0),
            _max_replan_zombie,
        )
        tracker.fail(stuck_error)
        result = {
            **fail_state(
                FailureCategory.REPLAN_EXHAUSTED,
                f"attempts={state.get('replan_count', 0)}",
                state.get("messages", []),
            ),
            "replan_requested": False,
            "execute_loop_count": count,
        }
        await sync_to_store(state, result)
        return result
    return None


def _build_convergence_hints(
    count: int, persist_into: list | None = None,
) -> list[HumanMessage]:
    """Phase 3 convergence hints: tiered last-iteration conclusion prompts.

    Returns 0 or 1 ``HumanMessage`` nudging the LLM to conclude / replan as the
    iteration budget runs low.

    Persisted through ``persist_replaceable_hint`` — a STABLE id, so each tier
    replaces the previous notice instead of stacking. Turn-local injection alone
    meant the model saw the countdown once and the next turn had no idea how much
    budget was left; a plain append would be worse, leaving "iteration 3 of 15"
    in history for turn 12 to read. Replacement gives history exactly one entry
    that always states the current number.
    """
    remaining = MAX_EXECUTE_LOOP - count
    hints: list[HumanMessage] = []
    _persist = persist_into if persist_into is not None else []

    def _emit(text: str) -> None:
        hints.append(persist_replaceable_hint(
            _persist, "budget", "execute", text,
        ))

    if MAX_EXECUTE_LOOP - 5 <= count < MAX_EXECUTE_LOOP - 1:
        # Tier 1: Soft warning — iterations running low
        _emit(
            f"**Iteration Progress**: You are on iteration {count} of max {MAX_EXECUTE_LOOP} "
            f"({remaining} remaining). "
            f"Think the next step through BEFORE acting: state to yourself what "
            f"you already know, what is still genuinely unknown, and what the next "
            f"call would add that the last one did not. "
            f"Use the remaining budget only for safe, meaningful actions that advance "
            f"the approved objective or resolve relevant uncertainty. Avoid spending it "
            f"on unchanged repetition. If the execution evidence is sufficient, conclude; "
            f"if the plan itself requires reconsideration, emit the structured replan request."
        )
    elif count == MAX_EXECUTE_LOOP - 1:
        # Tier 2: Urgent warning — second-to-last iteration
        _emit(
            f"**CRITICAL WARNING**: This is iteration {count} of max {MAX_EXECUTE_LOOP} — "
            f"your SECOND-TO-LAST iteration. Reason it through before you act: "
            f"with one action left, name the single unknown that action would "
            f"resolve. If you cannot name one, there is nothing left to gather. "
            f"Choose the next action only if it is safe, "
            f"meaningful, and justified by a changed hypothesis or new evidence. Otherwise "
            f"produce an evidence-based conclusion, or emit the structured replan request when the plan needs "
            f"reconsideration."
        )
    elif count >= MAX_EXECUTE_LOOP:
        # Tier 3: Final conclusion — tools unbound, must provide conclusion
        _emit(
            f"**FINAL ITERATION**: This is iteration {count} of max {MAX_EXECUTE_LOOP}. "
            f"No further tool calls are available. Think through what the evidence "
            f"actually supports, then provide a concise, evidence-based "
            f"conclusion that distinguishes observed facts from remaining uncertainty. "
            f"If the available evidence shows that a different plan is required, output "
            f"the structured replan request with the plan assumption that needs reconsideration."
        )
    return hints


async def _build_execute_system_prompt(
    state: AgentState, task_id: str, skill_name: str, tools,
    skill_catalog: str, env_info, registry,
) -> tuple[str, object]:
    """Phase 3: build the execute-phase system prompt + capability context.

    Returns ``(execute_prompt, capability_context)``. Pure extraction from
    ``_execute_loop_with_llm`` — behaviour unchanged.
    """
    from chaos_agent.agent.prompts import build_system_prompt, PromptMode
    from chaos_agent.agent.env_info import compute_env_info
    from chaos_agent.agent.spec.fault_spec import read_fault_spec
    plan = state.get("plan")
    plan_path = state.get("plan_path")
    # Build structured_params_hint from FaultSpec
    _spec_for_hint = read_fault_spec(state)
    structured_params_hint = ""
    if _spec_for_hint and _spec_for_hint.is_complete:
        structured_params_hint = (
            f"scope={_spec_for_hint.scope}, "
            f"target={_spec_for_hint.blade_target}, "
            f"action={_spec_for_hint.blade_action}"
        )
    # Build user_params_hint from FaultSpec.params so user-specified
    # values (e.g. finalizer=...) take priority over skill template
    # placeholders during Phase 2 execution.
    user_params_hint = ""
    if _spec_for_hint and _spec_for_hint.params:
        user_params_hint = json.dumps(dict(_spec_for_hint.params), ensure_ascii=False)
    # Resolve env_info: prefer constructor arg, fallback to dynamic computation
    resolved_env_info = env_info or await compute_env_info(task_id)
    capability_context = build_capability_context(state, "execute", tools)
    execute_prompt = build_system_prompt(
        PromptMode.MINIMAL,
        skill_catalog=registry.build_catalog_prompt() if registry else skill_catalog,
        skill_name=skill_name,
        plan=plan or "",
        plan_path=plan_path or "",
        structured_params_hint=structured_params_hint,
        user_params_hint=user_params_hint,
        env_info=resolved_env_info,
        profile=capability_context.profile,
    )
    return execute_prompt, capability_context


def make_execute_loop(hook=None, llm=None, tools=None, skill_catalog="", env_info=None, registry=None):
    """Create an execute_loop node with optional PreReasoningHook and LLM.

    When llm is provided, the node performs actual LLM reasoning
    (calling the model with bound tools, returning the response as a message).
    When llm is None, behaves identically to the plain execute_loop
    (only tracks iteration count, for test compatibility).
    """
    if llm is None and hook is None:
        return execute_loop

    async def _execute_loop_with_llm(state: AgentState) -> dict:
        # 0. Reset time_wait consecutive-call guard if last round included
        # any non-wait tool (allows time_wait to be called again after a
        # real tool like kubectl ran).  Scans the entire most-recent
        # ToolMessage batch to handle parallel tool calls correctly.
        from chaos_agent.tools.wait import check_and_reset_wait_guard
        check_and_reset_wait_guard(state.get("messages", []))

        # 1. Iteration count + limit check
        task_id = state.get("task_id", "") or ""
        skill_name = read_active_skill_name(state)
        count = state.get("execute_loop_count", 0) + 1

        tracker = get_tracker(task_id)
        tracker.start(
            StatusCategory.NODE,
            "execute_loop",
            f"Execute loop iteration {count}: executing skill '{skill_name}'",
            {"iteration": count, "skill_name": skill_name},
        )

        # Phase 1 early exits (max-iteration + zombie-replan) extracted.
        early_exit = await _check_execute_loop_limits(state, count, task_id, tracker)
        if early_exit is not None:
            return early_exit

        # 2. Call pre_reason_hook (memory compaction)
        hook_updates = {}
        if hook:
            hook_updates = await hook(state)

        # 2b. Emit ToolMessage results from previous iteration (debug only)
        emit_debug_tool_messages(tracker, state)

        # 3. Call LLM with bound tools
        # Declared OUTSIDE the ``llm`` branch, matching agent_loop's
        # ``_injections_for_state``. The result-building code below reads it from
        # a SIBLING branch (``if response is not None``), not a nested one, so a
        # declaration inside would only be safe as long as ``llm is None`` stays
        # unreachable — which is true today but is not a property this line
        # should depend on.
        _hints_for_state: list = []
        if llm is not None:
            messages = list(state.get("messages", []))

            # --- Execution hints (stagnation, idle, conflict, errors) ---
            _hint_counts: dict = {}
            hints, stagnant_tool = _build_execution_hints(
                messages, state, persist_into=_hints_for_state,
                counts_out=_hint_counts,
            )
            messages.extend(hints)

            # --- Convergence hints (last-iteration conclusion prompts) ---
            messages.extend(_build_convergence_hints(
                count, persist_into=_hints_for_state,
            ))

            # Build execution prompt using the modular prompt system (Phase 3
            # prompt build extracted to _build_execute_system_prompt).
            execute_prompt, capability_context = await _build_execute_system_prompt(
                state, task_id, skill_name, tools, skill_catalog, env_info, registry,
            )
            # On last iteration, unbind tools to force text conclusion
            if count >= MAX_EXECUTE_LOOP:
                llm_to_call = llm
            else:
                visible_tools = filter_tools_for_context(tools, capability_context)
                # An empty GATE result must not degrade to an unbound LLM:
                # the model would still emit calls from the prompt's tool list.
                # Bind nothing instead. Only when a NON-EMPTY set was gated away
                # though — with no static tools at all (or after the benign
                # stagnant filter) the unbound LLM is the intended prose path,
                # and it is the only usable one, since a provider rejects a
                # request carrying an empty ``tools`` array.
                if tools and not visible_tools:
                    logger.warning(
                        "execute_loop: capability gate left no visible tools "
                        "(profile=%s) — binding an empty tool set",
                        capability_context.profile,
                    )
                    llm_to_call = llm.bind_tools([])
                else:
                    tools_this_iter = filter_stagnant_tool(visible_tools, stagnant_tool)
                    llm_to_call = llm.bind_tools(tools_this_iter) if tools_this_iter else llm

            # Record system prompt to session store (dedup handles repeated prompts)
            record_system_prompt(hook, state, execute_prompt, node_name=EXECUTE_LOOP)

            response = await llm_to_call.ainvoke(
                [SystemMessage(content=execute_prompt)] + messages
            )
        else:
            response = None

        # 4. Build result
        result = {"execute_loop_count": count}

        # Extract blade_uid from ToolMessages (blade_create results)
        messages = state.get("messages", [])
        fault_spec = read_fault_spec(state)
        artifacts = collect_execution_artifacts(
            messages,
            state.get("execution_artifacts"),
            task_id=task_id,
            operation_family=(fault_spec.blade_target if fault_spec else ""),
        )
        if artifacts != (state.get("execution_artifacts") or []):
            result["execution_artifacts"] = artifacts

        blade_uid = _extract_blade_uid_from_messages(messages)
        if blade_uid and blade_uid != state.get("blade_uid"):
            result["blade_uid"] = blade_uid
            logger.info(f"Extracted blade_uid from ToolMessage: {blade_uid}")
            if not state.get("injection_start_time"):
                result["injection_start_time"] = now_iso()
                logger.info("Set injection_start_time (blade_uid first seen)")

        # Detect injection method for verifier Layer 1 strategy selection.
        # Direction B: injection_method is recorded at ISSUE time (channel A) by
        # ``_process_response_tool_calls`` from the fresh ``response.tool_calls``.
        # This history re-scan is the NARROW fallback, gated by ``need_redetect``
        # so we do not re-derive the same answer every iteration:
        #   - no method yet          → RESUME fallback: recover attribution after
        #     a restart, where the injection lives in history (not this turn's
        #     response, which channel A can no longer see); also covers the
        #     first injection turn until channel A sets it below.
        #   - blade_uid appeared AND current method is the provisional multi-step
        #     kubectl_native → UPGRADE it to the experiment backend (host_blade /
        #     kubectl_exec). This is the one thing channel A structurally cannot
        #     do: the UID only exists in the tool RESULT, not the issue-time call.
        # Steady state (method already set, no upgrade trigger) skips the scan.
        from chaos_agent.agent.providers import FaultProviderRegistry
        from chaos_agent.transports.registry import is_host_scope_channel
        current_injection_method = state.get("injection_method") or result.get("injection_method")
        _cur_provider = FaultProviderRegistry.resolve_by_method(current_injection_method)
        if _should_redetect_injection_method(current_injection_method, blade_uid):
            detected_method = _detect_injection_method(
                messages, blade_uid, is_host=is_host_scope_channel(state)
            )
            if detected_method and detected_method != current_injection_method:
                _new_provider = FaultProviderRegistry.resolve_by_method(detected_method)
                # Experiment-carrying method wins over a provisional multi-step one:
                # if a blade_uid appeared, upgrade the multi-step backend (kubectl_native)
                # to the experiment backend (host_blade / kubectl_exec).
                if (
                    _cur_provider is not None
                    and _cur_provider.is_multi_step
                    and _new_provider is not None
                    and _new_provider.has_experiment_uid
                ):
                    result["injection_method"] = detected_method
                    logger.info(f"Upgraded injection_method: {current_injection_method} → {detected_method}")
                elif not current_injection_method:
                    result["injection_method"] = detected_method
                    logger.info(f"Detected injection_method: {detected_method}")
                # Set injection_start_time for non-ChaosBlade methods too.
                if not state.get("injection_start_time") and "injection_start_time" not in result:
                    result["injection_start_time"] = now_iso()
                    logger.info("Set injection_start_time (%s detected)", detected_method or current_injection_method)

        # Extract kubectl exec injection pod name for verifier preference
        current_pod_name = state.get("kubectl_exec_pod_name")
        if not current_pod_name:
            from chaos_agent.agent.nodes.execute._injection_detection import _extract_kubectl_exec_pod_name
            pod_name = _extract_kubectl_exec_pod_name(messages)
            if pod_name:
                result["kubectl_exec_pod_name"] = pod_name
                logger.info(f"Recorded kubectl exec pod name: {pod_name}")

        # Extract skill use-case content from read_skill_resource ToolMessages
        # (used by Layer 2 verification as PRIMARY AUTHORITY)
        current_skill_case = state.get("skill_case_content")
        if not current_skill_case:
            for msg in reversed(messages):
                if not isinstance(msg, ToolMessage):
                    continue
                if getattr(msg, "name", "") != "read_skill_resource":
                    continue
                content = msg.content if isinstance(msg.content, str) else ""
                # Detect catalogue use-case files by key section markers
                if content and ("**故障现象**" in content or "**注入验证**" in content or "**恢复验证**" in content):
                    result["skill_case_content"] = content
                    logger.info("Extracted skill_case_content from read_skill_resource ToolMessage")
                    break

        if response is not None:
            # Programmatic kubeconfig injection: ensure every kubectl/blade tool call
            # has the correct kubeconfig, even if the LLM forgot to include it.
            kubeconfig = _resolve_kubeconfig(state)
            inject_kubeconfig_into_tool_calls(response, kubeconfig)
            inject_task_id_into_tool_calls(response, task_id)
            sync_kubewiz_runtime(state)

            # Output-limit truncation: the tool calls in this response may carry
            # silently incomplete arguments, so none of them may run against the
            # cluster. Parseable calls get a synthetic error answer; calls whose
            # JSON is broken are stripped from the message instead (answering
            # those makes the provider parse the args and reject the request).
            # Either way the screener is flagged to route back here rather than
            # forwarding the batch to the ToolNode.
            truncated = handle_truncated_response(response)
            if truncated is not None:
                safe_message, truncated_results = truncated
                logger.warning(
                    "execute_loop: response truncated by output token limit — "
                    "%d tool call(s) failed unexecuted, %d unparseable call(s) dropped",
                    len(truncated_results),
                    len(getattr(response, "invalid_tool_calls", None) or []),
                )
                result["messages"] = _hints_for_state + [safe_message] + truncated_results
                if _hint_counts and _hint_counts != (state.get("hint_repeat_counts") or {}):
                    result["hint_repeat_counts"] = _hint_counts
                result["truncated_tool_calls"] = True
                record_ai_message(hook, state, response, node_name=EXECUTE_LOOP)
                log_reasoning_content(response, "Execute loop", count)
            else:
                result["messages"] = _hints_for_state + [response]
                if _hint_counts and _hint_counts != (state.get("hint_repeat_counts") or {}):
                    result["hint_repeat_counts"] = _hint_counts
                # Clear at the source every non-truncated turn: a flag left set
                # by a turn that exited via replan/end (bypassing the screener
                # that consumes it) must not reach a later, healthy batch.
                result["truncated_tool_calls"] = False

                # Immediately save AI message (including reasoning_content) to session
                record_ai_message(hook, state, response, node_name=EXECUTE_LOOP)

                # Diagnostic log for reasoning_content presence
                log_reasoning_content(response, "Execute loop", count)

                _process_response_tool_calls(response, state, result, tracker, count)

        from chaos_agent.memory.hook import merge_hook_updates
        merge_hook_updates(result, hook_updates)

        # A truncated turn is not a conclusion: it was cut off mid-emission, so
        # its (possibly empty) tool_calls say nothing about whether the executor
        # is done. Running the terminal-conclusion detector here would also let
        # it append a nudge AFTER the synthetic tool results, breaking the
        # "answers are the last messages" precondition the screener checks
        # before diverting the batch away from the ToolNode.
        if response is not None and not result.get("truncated_tool_calls"):
            _detect_terminal_conclusion(response, state, result)

        # --- Last-iteration failure attribution ---
        if count >= MAX_EXECUTE_LOOP:
            existing_uid = result.get("blade_uid") or state.get("blade_uid")
            if not existing_uid:
                _fs = fail_state(
                    FailureCategory.EXECUTION_TIMEOUT,
                    f"max_iterations={MAX_EXECUTE_LOOP}",
                    state.get("messages", []) + result.get("messages", []),
                )
                result.update(_fs)

        _handle_replan(response, state, result)

        # Replan must not carry helper pods from the failed execution attempt
        # into a newly approved plan. This is artifact cleanup, not fault
        # recovery; actual fault compensations remain the recover graph's job.
        if result.get("replan_requested"):
            merged_artifacts = result.get("execution_artifacts")
            if merged_artifacts is None:
                merged_artifacts = state.get("execution_artifacts")
            cleaned_artifacts, _ = await cleanup_debug_pod_artifacts(
                merged_artifacts,
                kubeconfig=_resolve_kubeconfig(state),
                task_id=task_id,
            )
            if cleaned_artifacts != (merged_artifacts or []):
                result["execution_artifacts"] = cleaned_artifacts

        tracker.complete(f"Execute loop iteration {count} done")
        await sync_to_store(state, result)
        # Patch C — wall-clock cause labelling. If the router is about
        # to terminate this loop due to ``settings.max_inject_seconds``,
        # stamp ``failure_reason = WALL_CLOCK_TIMEOUT`` so the result
        # envelope is honest. Only fires when budget > 0 and started.
        from chaos_agent.agent.router import (
            mark_loop_exhausted,
            mark_wall_clock_timeout,
        )
        result = mark_wall_clock_timeout(state, result)
        # Same idea for the iteration budget. The router stops on
        # ``count >= max_loop`` while the early-exit check above only fires on
        # ``count > MAX_EXECUTE_LOOP``, so a run that uses its budget EXACTLY
        # was ending with no recorded cause at all (task-ff057e7f: 100/100
        # iterations, ``failure_reason=""``, envelope said success).
        return mark_loop_exhausted(result, count, MAX_EXECUTE_LOOP)

    return _execute_loop_with_llm
