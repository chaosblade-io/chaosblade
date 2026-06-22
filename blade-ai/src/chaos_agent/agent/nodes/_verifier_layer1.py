"""Layer 1 domain for verifier: data models, parsing, and execution.

Extracted from verifier.py — contains the Layer 1 verification logic
(blade_status + blade_query_k8s parsing, kubectl exec verification,
and Layer 1 result serialization/restoration).
"""

import json
import logging
from collections import namedtuple

from langchain_core.messages import ToolMessage

from chaos_agent.agent.nodes._injection_detection import (
    _was_kubectl_blade_injection_successful,
    _was_blade_create_attempted,
    _TOOL_POD_NAMESPACE,
)
from chaos_agent.agent.state import AgentState
from chaos_agent.agent.verdict import Layer1Result
from chaos_agent.observability.status_tracker import get_tracker

logger = logging.getLogger(__name__)

# Layer1Result is now a Pydantic model imported from chaos_agent.agent.verdict


# ---------------------------------------------------------------------------
# Refactor 2: 提取 blade_status JSON 解析为独立函数
# 原因: blade_status 返回值解析嵌套 5-6 层，在 verifier() 和
#        _verifier_with_llm() 中完全重复
# 做法: 独立函数 + 扁平化 if/elif，消除深层嵌套
# ---------------------------------------------------------------------------

_EXPIRED_STATES = frozenset({"Destroyed", "destroyed", "Revoked", "revoked", "Completed", "completed"})

_RUNNING_STATES = frozenset({"Running", "running", "Success", "success"})


def _parse_blade_status_output(raw: str) -> tuple[str, str, bool]:
    """Parse blade_status JSON output into (status, details, expired).

    Returns:
        status: "passed" if experiment is Running/Success, "failed" otherwise.
        details: Human-readable details string.
        expired: True if experiment status is Destroyed/Revoked/Completed (timeout expired).
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: raw string search
        if any(s in raw for s in _RUNNING_STATES):
            return "passed", "blade_status: Running (raw match)", False
        return "failed", raw[:200], False

    if not (data.get("success") or data.get("code") == 200):
        return "failed", raw[:200], False

    res = data.get("result", {})
    # Non-dict result (e.g. just a UID string) means success
    if not isinstance(res, dict):
        return "passed", "blade_status: Success (experiment running)", False

    exp_status = res.get("Status", res.get("status", "")) or res.get("phase", "")
    if exp_status in _RUNNING_STATES:
        return "passed", f"blade_status: {exp_status} (experiment running)", False
    if exp_status in _EXPIRED_STATES:
        return (
            "failed",
            f"Experiment status: {exp_status} — the fault has already expired (likely "
            f"due to --timeout being too short). Verification cannot observe fault effects "
            f"because they have already dissipated. Recommend increasing --duration to >= 60s.",
            True,
        )
    # Transient state: blade reports "please wait" when the experiment
    # is mid-transition (e.g. Initialized→Running during setup, or
    # Running→Destroyed during teardown). The fault may already be
    # in effect — let Layer 2 verify the actual cluster state.
    error_msg = res.get("Error", "")
    if "please wait" in error_msg.lower():
        return (
            "warning",
            f"Experiment in transient state ({error_msg}). "
            f"Layer 2 will verify actual cluster state.",
            False,
        )
    return "failed", f"Experiment status: {exp_status}", False


# ---------------------------------------------------------------------------
# Refactor 3: 提取 blade_query_k8s 结果解析为独立函数
# 原因: 同上，深层嵌套 + 两处重复
# 做法: 独立函数，职责单一——只负责解析 query k8s 返回值
# ---------------------------------------------------------------------------

_QueryK8sResult = namedtuple(
    "_QueryK8sResult", ["status", "details", "resource_statuses", "affected_count", "expired"],
)


def _parse_blade_query_k8s_output(raw: str) -> _QueryK8sResult:
    """Parse blade_query_k8s JSON output for per-resource status.

    Returns:
        _QueryK8sResult with:
            status: "passed" if all resources succeeded, "failed" if any failed,
                    "unknown" if output cannot be parsed (non-critical).
            details: Human-readable summary of resource-level results.
            resource_statuses: list of per-resource dicts from statuses[].
            affected_count: number of resources in statuses[].
            expired: True if any resource has state in _EXPIRED_STATES (Destroyed/Revoked/Completed).
    """
    _empty = _QueryK8sResult("unknown", "", [], 0, False)

    if not raw or raw.startswith("Error"):
        logger.debug(f"blade_query_k8s: empty/error output, raw={raw[:200]!r}")
        # Extract meaningful info from error messages (e.g., "not found" = CRD not yet ready)
        if "not found" in raw:
            return _QueryK8sResult("unknown", "blade_query_k8s: CRD not yet ready (will be available shortly)", [], 0, False)
        return _empty

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug(f"blade_query_k8s: non-JSON output, raw={raw[:200]!r}")
        return _empty

    if not (data.get("success") or data.get("code") == 200):
        logger.debug(f"blade_query_k8s: unsuccessful response, data={json.dumps(data, ensure_ascii=False)[:200]}")
        # ChaosBlade returns JSON errors like {"code":63061,"success":false,"error":"...not found"}
        err_msg = data.get("error", "")
        if "not found" in err_msg.lower():
            return _QueryK8sResult("unknown", "blade_query_k8s: CRD not found (experiment may still be initializing)", [], 0, False)
        return _empty

    qresult = data.get("result", {})
    statuses = qresult.get("statuses", [])

    if statuses:
        # Check for expired states FIRST (before generic failed check)
        expired_states = [s for s in statuses if s.get("state", "") in _EXPIRED_STATES]
        if expired_states:
            names = [s.get("name", "?") for s in expired_states]
            return _QueryK8sResult(
                "failed",
                f"blade query k8s: experiment expired (state: Destroyed/Revoked): {names}",
                statuses, len(statuses), True,
            )
        failed = [s for s in statuses if not s.get("success", True)]
        if failed:
            names = [s.get("name", "?") for s in failed]
            return _QueryK8sResult("failed", f"blade query k8s: failed resources: {names}", statuses, len(statuses), False)
        return _QueryK8sResult("passed", f"blade query k8s: all {len(statuses)} resource(s) Success", statuses, len(statuses), False)

    if isinstance(qresult, dict) and qresult.get("success", True):
        return _QueryK8sResult("passed", "blade query k8s: confirmed", [], 0, False)

    logger.debug(f"blade_query_k8s: unhandled format, result={json.dumps(qresult, ensure_ascii=False)[:200]}")
    return _empty


# ---------------------------------------------------------------------------
# Refactor 4: 提取完整的 Layer 1 验证流程为独立函数
# 原因: Layer 1 逻辑（blade_status → blade_query_k8s）在两个入口函数中
#        完全重复 ~80 行，且包含 try/except 错误处理
# 做法: 独立 async 函数，返回 Layer1Result dataclass，彻底消除重复
# Note: _resolve_kubeconfig moved to _kubeconfig_inject.py for shared use
# ---------------------------------------------------------------------------


def _find_blade_query_in_messages(messages: list, blade_uid: str) -> str:
    """Scan kubectl ToolMessages for blade query k8s output matching the given uid.

    When the host blade binary is unavailable, the LLM may have already run
    `blade query k8s create <uid>` via kubectl exec during the execution phase.
    This function finds that output so Layer 1 can use it as verification evidence.

    Returns the raw JSON string if found, empty string otherwise.
    """
    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") != "kubectl":
            continue
        content = msg.content if isinstance(msg.content, str) else ""
        if blade_uid in content and '"success"' in content:
            try:
                data = json.loads(content)
                if isinstance(data, dict) and data.get("success") is True:
                    result = data.get("result", {})
                    if isinstance(result, dict) and result.get("uid") == blade_uid:
                        return content
            except (json.JSONDecodeError, TypeError):
                pass
    return ""


def _map_query_k8s_to_layer1(
    q_result: _QueryK8sResult, raw: str, pod_name: str, source: str,
) -> Layer1Result:
    """Map _QueryK8sResult to Layer1Result with expired detection.

    Used when kubectl exec path uses `blade query k8s create <uid>`
    instead of `blade status <uid>` (CRD UID not in pod's local DB).
    """
    if q_result.status == "passed":
        layer1_status = "passed"
    elif q_result.expired:
        # expired=True means experiment Destroyed/Revoked
        layer1_status = "failed"
    else:
        layer1_status = q_result.status  # "failed" or "unknown"
    return Layer1Result(
        status=layer1_status,
        details=f"blade query k8s via kubectl exec ({pod_name}, {source}): {q_result.details}",
        raw_output=raw,
        resource_statuses=q_result.resource_statuses,
        affected_count=q_result.affected_count,
        expired=q_result.expired,
    )


async def _run_layer1_via_kubectl_exec(
    blade_uid: str, kubeconfig: str, *, task_id: str = "",
    injection_pod_name: str | None = None,
) -> Layer1Result:
    """Layer 1 verification via kubectl exec into a tool pod.

    Used when injection_method is "kubectl_exec" (host blade binary may be
    incompatible, so host blade_status would fail).

    If the original injection pod name is known (injection_pod_name), it is
    tried first (Step 0) before discovering new pods (Step 1). This maximises
    success probability since the original pod is where the experiment was
    created and is most likely to have it visible.

    Error handling follows the principle "infrastructure failure ≠ experiment failure":
    - Type A (infrastructure failure): can't discover pods or can't exec
      into them -> "skipped" (non-terminal, Layer 2 proceeds)
    - Type B (experiment status failure): blade status returns Error/Destroyed
      -> "failed" (terminal, blocks Layer 2)

    Retries up to 2 different pods before giving up.
    """
    tracker = get_tracker(task_id) if task_id else None

    try:
        from chaos_agent.tools.shell import run_command
        from chaos_agent.tools.kubectl import build_kubectl_cmd, _adapt_kubewiz_result

        # Step 0: Try the original injection pod first (if known)
        if injection_pod_name:
            # PRIMARY: blade query k8s (queries CRD, works with CRD UID)
            # blade status <crd_uid> returns "record not found" inside pod
            # because pod's local experiment DB uses a different UID.
            query_cmd = build_kubectl_cmd("exec", [
                injection_pod_name, "-n", _TOOL_POD_NAMESPACE,
                "--", "blade", "query", "k8s", "create", blade_uid,
            ], kubeconfig=kubeconfig)
            try:
                query_run_result = await run_command(query_cmd, task_id=task_id, source="verifier-L1")
                query_run_result = _adapt_kubewiz_result(query_run_result)
                raw = query_run_result.stdout

                # Check if blade query k8s is available (not in older ChaosBlade versions)
                if raw and "unknown command" not in raw and "command not found" not in raw:
                    if "error: unable to upgrade connection" in raw:
                        logger.info(
                            f"Original injection pod {injection_pod_name} unavailable, "
                            f"falling back to pod discovery"
                        )
                    else:
                        q_result = _parse_blade_query_k8s_output(raw)
                        # Only return if parseable; if unknown (kubectl exec error,
                        # non-JSON output), fall through to blade status fallback
                        if q_result.status != "unknown":
                            layer1_result = _map_query_k8s_to_layer1(q_result, raw, injection_pod_name, "original")
                            if tracker:
                                tracker.update(
                                    f"Layer 1 step 0: blade_query_k8s (kubectl exec {injection_pod_name}): {layer1_result.status}",
                                    {"step": "blade_query_k8s_kubectl", "status": layer1_result.status,
                                     "pod": injection_pod_name, "source": "original"},
                                )
                            return layer1_result
                        logger.info(
                            f"blade query k8s returned unparseable result from pod {injection_pod_name}, "
                            f"trying blade status fallback"
                        )
                elif raw and ("command not found" in raw or "No such file" in raw):
                    logger.info(
                        f"blade query k8s not available in pod {injection_pod_name}, "
                        f"trying blade status fallback"
                    )
                # If blade query k8s failed or unavailable, fall through to blade status
            except Exception as e:
                logger.info(
                    f"blade query k8s failed on original pod {injection_pod_name}: {e}, "
                    f"trying blade status fallback"
                )

            # FALLBACK: blade status (searches local DB, CRD UID may not be found)
            status_cmd = build_kubectl_cmd("exec", [
                injection_pod_name, "-n", _TOOL_POD_NAMESPACE,
                "--", "blade", "status", blade_uid,
            ], kubeconfig=kubeconfig)
            try:
                status_result = await run_command(status_cmd, task_id=task_id, source="verifier-L1")
                status_result = _adapt_kubewiz_result(status_result)
                raw = status_result.stdout

                # Check if the original pod is unavailable
                if raw and ("not found" in raw
                            or "error: unable to upgrade connection" in raw):
                    logger.info(
                        f"Original injection pod {injection_pod_name} unavailable, "
                        f"falling back to pod discovery"
                    )
                elif raw and ("command not found" in raw or "No such file" in raw):
                    logger.info(
                        f"blade binary not found in original pod {injection_pod_name}, "
                        f"falling back to pod discovery"
                    )
                elif raw:
                    # Got a parseable response from the original pod
                    status, details, expired = _parse_blade_status_output(raw)
                    if tracker:
                        tracker.update(
                            f"Layer 1 step 0: blade_status (kubectl exec {injection_pod_name}): {status}",
                            {"step": "blade_status_kubectl", "status": status,
                             "pod": injection_pod_name, "source": "original"},
                        )
                    return Layer1Result(
                        status=status,
                        details=f"blade_status via kubectl exec ({injection_pod_name}, original): {details}",
                        raw_output=raw,
                        expired=expired,
                    )
                else:
                    logger.info(
                        f"Empty response from original pod {injection_pod_name}, "
                        f"falling back to pod discovery"
                    )
            except Exception as e:
                logger.info(
                    f"Failed to query original pod {injection_pod_name}: {e}, "
                    f"falling back to pod discovery"
                )

        # Step 1: Discover running tool pods (cluster-wide)
        from chaos_agent.agent.nodes._injection_detection import discover_tool_pods_cluster_wide
        try:
            pods_with_ns = await discover_tool_pods_cluster_wide(kubeconfig, task_id)
        except Exception as e:
            msg = f"kubectl exec: failed to discover tool pods: {e}"
            if tracker:
                tracker.update(f"Layer 1 (kubectl exec): {msg} -> skipped",
                               {"step": "discover_pods", "status": "skipped"})
            return Layer1Result(
                status="skipped",
                details=f"{msg} (infrastructure issue, not experiment failure)",
            )

        if not pods_with_ns:
            msg = "kubectl exec: no running tool pods found, cannot verify blade status"
            if tracker:
                tracker.update(f"Layer 1 (kubectl exec): {msg} -> skipped",
                               {"step": "discover_pods", "status": "skipped"})
            return Layer1Result(
                status="skipped",
                details=f"{msg} (infrastructure issue, not experiment failure)",
            )

        # Step 2: Try blade query k8s (primary) then blade status (fallback)
        # via kubectl exec on each pod (up to 2)
        # NOTE: blade status v1.8.0 does NOT support --kubeconfig flag.
        # Inside the pod, blade can access the API server directly without kubeconfig.
        last_error = None
        for pod_name, pod_ns in pods_with_ns[:2]:
            # PRIMARY: blade query k8s (queries CRD, works with CRD UID)
            query_cmd = build_kubectl_cmd("exec", [
                pod_name, "-n", pod_ns,
                "--", "blade", "query", "k8s", "create", blade_uid,
            ], kubeconfig=kubeconfig)
            try:
                query_result = await run_command(query_cmd, task_id=task_id, source="verifier-L1")
                query_result = _adapt_kubewiz_result(query_result)
                raw = query_result.stdout

                # Check for Type A infrastructure errors
                if not raw or "error: unable to upgrade connection" in raw:
                    last_error = f"cannot exec into pod {pod_name}"
                    continue

                # If blade query k8s is available, use it
                if "unknown command" not in raw and "command not found" not in raw and "No such file" not in raw:
                    q_result = _parse_blade_query_k8s_output(raw)
                    # Only return if parseable; if unknown (kubectl exec error,
                    # non-JSON output), fall through to blade status fallback
                    if q_result.status != "unknown":
                        layer1_result = _map_query_k8s_to_layer1(q_result, raw, pod_name, "discovered")
                        if tracker:
                            tracker.update(
                                f"Layer 1 step 1/1: blade_query_k8s (kubectl exec {pod_name}): {layer1_result.status}",
                                {"step": "blade_query_k8s_kubectl", "status": layer1_result.status, "pod": pod_name},
                            )
                        return layer1_result
                    logger.info(f"blade query k8s returned unparseable result from pod {pod_name}, trying blade status")

                # FALLBACK: blade query k8s not available, try blade status
                logger.info(f"blade query k8s not available in pod {pod_name}, trying blade status")
            except Exception as e:
                logger.debug(f"blade query k8s failed in pod {pod_name}: {e}, trying blade status")

            # FALLBACK: blade status (searches local DB, CRD UID may not be found)
            status_cmd = build_kubectl_cmd("exec", [
                pod_name, "-n", pod_ns,
                "--", "blade", "status", blade_uid,
            ], kubeconfig=kubeconfig)
            try:
                status_result = await run_command(status_cmd, task_id=task_id, source="verifier-L1")
                status_result = _adapt_kubewiz_result(status_result)
                raw = status_result.stdout

                # Check for Type A infrastructure errors (can't execute command)
                if not raw or "command not found" in raw or "No such file" in raw:
                    last_error = f"blade binary not found in pod {pod_name}"
                    continue
                if "error: unable to upgrade connection" in raw:
                    last_error = f"cannot exec into pod {pod_name}"
                    continue

                # Type B: Parse blade status output (experiment status)
                status, details, expired = _parse_blade_status_output(raw)
                if tracker:
                    tracker.update(
                        f"Layer 1 step 1/1: blade_status (kubectl exec {pod_name}): {status}",
                        {"step": "blade_status_kubectl", "status": status, "pod": pod_name},
                    )
                return Layer1Result(
                    status=status,
                    details=f"blade_status via kubectl exec ({pod_name}): {details}",
                    raw_output=raw,
                    expired=expired,
                )
            except Exception as e:
                last_error = str(e)
                continue

        # All pods failed -- Type A (infrastructure failure) -> skipped
        msg = f"kubectl exec: could not execute blade status in any tool pod ({last_error})"
        if tracker:
            tracker.update(
                "Layer 1 (kubectl exec): all tool pods failed -> skipped",
                {"step": "blade_status_kubectl", "status": "skipped"},
            )
        return Layer1Result(
            status="skipped",
            details=f"{msg} -- infrastructure issue, not experiment failure",
        )

    except Exception as e:
        logger.error(f"Layer 1 kubectl exec verification failed: {e}")
        return Layer1Result(
            status="skipped",
            details=f"kubectl exec verification error: {e} -- infrastructure issue, allowing Layer 2 to proceed",
        )


async def _run_layer1_verification(
    blade_uid: str, kubeconfig: str, *, task_id: str = "",
    messages: list | None = None, injection_method: str | None = None,
    injection_pod_name: str | None = None,
) -> Layer1Result:
    """Execute Layer 1 verification: blade_status + blade_query_k8s.

    When injection_method is "kubectl_exec", uses kubectl exec into a tool pod
    instead of the host blade binary (which may be incompatible).

    If injection_pod_name is provided, the original injection pod is tried
    first before discovering new pods.

    Returns a Layer1Result with status, details, and raw output.
    Also emits per-step status events via StatusTracker so the user
    can see each check individually.
    """
    if not blade_uid:
        if messages and _was_blade_create_attempted(messages):
            # blade_create was called but extract_blade_uid rejected the UID
            # (e.g., 54000+success=false). blade's error report may be wrong
            # (ChaosBlade may use fallback mechanisms like tc instead of
            # iptables). Mark as WARNING (non-terminal) — Layer 2 will
            # verify actual cluster state to determine the truth.
            return Layer1Result(
                status="warning",
                details="blade_create was called but reported error — "
                        "fault may still be in effect via fallback mechanisms. "
                        "Layer 2 will verify actual cluster state.",
            )
        return Layer1Result(
            status="skipped",
            details="Non-ChaosBlade fault (no blade_create used), Layer 1 not applicable",
        )

    # kubectl_exec injection: use kubectl exec path instead of host blade_status
    if injection_method == "kubectl_exec":
        return await _run_layer1_via_kubectl_exec(
            blade_uid, kubeconfig, task_id=task_id,
            injection_pod_name=injection_pod_name,
        )

    # kubectl_native injection: no blade experiment to verify
    if injection_method == "kubectl_native":
        return Layer1Result(
            status="skipped",
            details="kubectl-native injection (no blade experiment), Layer 1 not applicable",
        )

    tracker = get_tracker(task_id) if task_id else None

    try:
        from chaos_agent.tools.blade import blade_status, blade_query_k8s

        # Step 1: blade_status — experiment-level check
        status_output = await blade_status.ainvoke(
            {"uid": blade_uid, "kubeconfig": kubeconfig}
        )
        raw = status_output if isinstance(status_output, str) else str(status_output)
        layer1_status, layer1_details, layer1_expired = _parse_blade_status_output(raw)

        # If blade_status failed because the experiment isn't in the local DB,
        # fall back to blade_query_k8s (cluster-side CRD query).
        # Two cases: (1) explicit "record not found" message, (2) empty stdout
        # (kubewiz mode — experiment runs remotely, no local record exists).
        _fallback_used = False
        if layer1_status == "failed" and (not raw.strip() or "record not found" in raw.lower()):
            logger.info(f"blade_status local DB miss (raw={raw[:80]!r}), trying blade_query_k8s as fallback")
            try:
                query_output = await blade_query_k8s.ainvoke(
                    {"uid": blade_uid, "kubeconfig": kubeconfig}
                )
                query_raw = query_output if isinstance(query_output, str) else str(query_output)
                q_result = _parse_blade_query_k8s_output(query_raw)
                if q_result.status != "unknown":
                    layer1_status = q_result.status
                    layer1_details = f"blade_query_k8s fallback: {q_result.details}"
                    layer1_expired = q_result.expired
                    # Preserve fallback data — will be used directly if Step 2 is skipped
                    q_resource_statuses = q_result.resource_statuses
                    q_affected_count = q_result.affected_count
                    _fallback_used = True
                    logger.info(f"blade_query_k8s fallback succeeded: status={q_result.status}")
            except Exception as qe:
                logger.debug(f"blade_query_k8s fallback also failed: {qe}")

        # Emit step 1 result
        step1_msg = f"Layer 1 step 1/2: blade_status: {layer1_status}"
        if layer1_details:
            step1_msg += f" - {layer1_details}"
        if tracker:
            tracker.update(step1_msg, {"step": "blade_status", "status": layer1_status})

        # Step 2: blade_query_k8s — per-resource check (supplementary)
        # Only if blade_status passed AND fallback was NOT used (fallback already
        # has the blade_query_k8s data; re-querying would waste an API call and
        # overwrite the fallback's resource_statuses/affected_count).
        query_status_str = "skipped"
        query_details_str = ""
        if _fallback_used:
            # Fallback already provided blade_query_k8s data — use it directly
            query_status_str = layer1_status
            query_details_str = layer1_details
        else:
            q_resource_statuses: list[dict] = []
            q_affected_count = 0
            if layer1_status == "passed":
                try:
                    query_output = await blade_query_k8s.ainvoke(
                        {"uid": blade_uid, "kubeconfig": kubeconfig}
                    )
                    query_raw = query_output if isinstance(query_output, str) else str(query_output)
                    q_result = _parse_blade_query_k8s_output(query_raw)
                    q_status = q_result.status
                    q_details = q_result.details
                    q_resource_statuses = q_result.resource_statuses
                    q_affected_count = q_result.affected_count
                    # If blade_query_k8s detected expired state, propagate expired flag
                    if q_result.expired:
                        layer1_expired = True
                    query_status_str = q_status
                    query_details_str = q_details

                    if q_status == "failed":
                        layer1_status = "failed"
                        layer1_details = f"blade_status: Running, but {q_details}"
                    elif q_status == "passed":
                        # CRD status settle guard: ChaosBlade CRD reports
                        # Success immediately upon creation, then asynchronously
                        # exec's the fault process into the target container.
                        # If exec fails (e.g. "dd: command not found" in minimal
                        # images), the CRD status flips to Error a few seconds
                        # later. Querying too early sees stale Success. Wait
                        # briefly and re-query to catch async failures.
                        import asyncio
                        if tracker:
                            tracker.update(
                                "CRD settle guard: waiting 5s to confirm injection process started",
                                {"step": "crd_settle_guard"},
                            )
                        await asyncio.sleep(5)
                        try:
                            recheck_output = await blade_query_k8s.ainvoke(
                                {"uid": blade_uid, "kubeconfig": kubeconfig}
                            )
                            recheck_raw = recheck_output if isinstance(recheck_output, str) else str(recheck_output)
                            recheck = _parse_blade_query_k8s_output(recheck_raw)
                            if recheck.status == "failed":
                                q_status = "failed"
                                q_details = f"re-check after 5s: {recheck.details}"
                                q_resource_statuses = recheck.resource_statuses
                                q_affected_count = recheck.affected_count
                                query_status_str = q_status
                                query_details_str = q_details
                                layer1_status = "failed"
                                layer1_details = f"blade_status: Running, but {q_details}"
                                logger.info("CRD settle guard: status flipped to failed after 5s re-check")
                            elif recheck.expired:
                                layer1_expired = True
                            else:
                                layer1_details = f"blade_status: Running, {q_details} (confirmed after re-check)"
                        except Exception:
                            layer1_details = f"blade_status: Running, {q_details}"
                    else:
                        # q_status == "unknown": non-critical, keep blade_status result
                        if not layer1_details:
                            layer1_details = "blade_status: Running (blade_query_k8s unavailable)"
                except Exception as qe:
                    query_status_str = "error"
                    query_details_str = str(qe)
                    logger.debug(f"blade query k8s failed (non-critical): {qe}")

        # Emit step 2 result
        step2_msg = f"Layer 1 step 2/2: blade_query_k8s: {query_status_str}"
        if query_details_str:
            step2_msg += f" - {query_details_str}"
        if tracker:
            tracker.update(step2_msg, {"step": "blade_query_k8s", "status": query_status_str})

        # Degradation: when blade_status/blade_query_k8s report failure but we have
        # evidence of successful injection via kubectl exec (host blade binary broken),
        # try to find blade query k8s results in message history as fallback.
        if layer1_status == "failed" and blade_uid and messages:
            # Fallback 1: find blade query k8s evidence from kubectl exec in message history
            fallback = _find_blade_query_in_messages(messages, blade_uid)
            if fallback:
                layer1_status = "passed"
                layer1_details = (
                    "blade_status unavailable (host blade error), "
                    "but blade query k8s from kubectl exec confirmed injection success"
                )
            elif _was_kubectl_blade_injection_successful(messages):
                # Fallback 2: kubectl exec injection output exists but no query evidence
                layer1_status = "skipped"
                layer1_details = (
                    f"blade_status/blade_query_k8s reported failure, "
                    f"but blade_uid={blade_uid} was extracted from kubectl exec injection output. "
                    f"Host blade binary may be incompatible."
                )

        # Fallback 3: Self-destructive fault detection.
        # Some faults (e.g. node-process stop containerd) destroy the very
        # communication channel Layer 1 uses to verify. The injection
        # succeeds, but blade_status/blade_query_k8s fail because the
        # target node is now unreachable. Detect this by checking if the
        # failure is a connectivity error AND the target node is NotReady
        # (which is observable via API server, not via the dead node).
        if layer1_status == "failed" and blade_uid:
            _conn_keywords = (
                "connection refused", "connection timed out",
                "unreachable", "dial tcp", "i/o timeout",
            )
            _all_text = ((layer1_details or "") + (raw or "")).lower()
            if any(kw in _all_text for kw in _conn_keywords):
                try:
                    from chaos_agent.tools.kubectl import kubectl_ro as _kro
                    _node_out = await _kro.ainvoke({
                        "subcommand": "get",
                        "v_args": "nodes",
                        "kubeconfig": kubeconfig,
                    })
                    _node_str = _node_out if isinstance(_node_out, str) else str(_node_out)
                    if "NotReady" in _node_str:
                        logger.info(
                            "Self-destructive fault detected: Layer 1 failed due to "
                            "connectivity loss, target node is NotReady — skipping "
                            "Layer 1 to let Layer 2 verify the actual fault effect"
                        )
                        layer1_status = "skipped"
                        layer1_details = (
                            "blade_status unreachable (node connectivity lost), "
                            "but target node is NotReady — consistent with a "
                            "self-destructive fault (e.g. containerd/kubelet stop). "
                            "Layer 2 will verify the actual fault effect."
                        )
                except Exception as _sde:
                    logger.debug(f"Self-destructive fault check failed: {_sde}")

        return Layer1Result(
            status=layer1_status,
            details=layer1_details,
            raw_output=raw,
            resource_statuses=q_resource_statuses,
            affected_count=q_affected_count,
            expired=layer1_expired,
        )

    except Exception as e:
        logger.error(f"Layer 1 verification failed: {e}")

        # Fallback 1: try to find blade query results from kubectl exec in message history.
        # When the host blade binary is unavailable, the LLM may have already
        # verified injection via kubectl exec blade query k8s.
        if blade_uid and messages:
            fallback = _find_blade_query_in_messages(messages, blade_uid)
            if fallback:
                return Layer1Result(
                    status="passed",
                    details="blade_status unavailable (host blade error), "
                            "but blade query k8s from kubectl exec confirmed injection success",
                    raw_output=fallback,
                )

        # Fallback 2: blade_uid exists but tools failed — allow Layer 2 to proceed.
        # This happens when blade_create failed but kubectl exec injection succeeded,
        # and the host blade binary also cannot run blade_status.
        if blade_uid:
            return Layer1Result(
                status="skipped",
                details=f"blade_status/blade_query_k8s unavailable ({e}), "
                        f"but blade_uid={blade_uid} was extracted from injection output",
                raw_output=str(e),
            )

        return Layer1Result(status="error", details=str(e), raw_output=str(e))


# ---------------------------------------------------------------------------
# Refactor 5: 提取 Layer 1 结果从 state 恢复的逻辑
# 原因: 后续迭代需要复用第一轮的 Layer 1 结果，但之前的实现用
#        f-string 拼凑丢失了 raw_output，LLM 看不到完整上下文
# 做法: 将 raw_output 也存入 verification dict，恢复时完整重建
# ---------------------------------------------------------------------------

def _restore_layer1_from_state(state: AgentState) -> Layer1Result:
    """Restore Layer 1 result from a previous iteration's cache."""
    cache = state.get("inject_layer1_cache") or {}
    return Layer1Result.model_validate(cache) if cache else Layer1Result()


def _layer1_to_dict(result: Layer1Result) -> dict:
    """Convert Layer1Result to the verification.layer1 dict."""
    return result.model_dump()
