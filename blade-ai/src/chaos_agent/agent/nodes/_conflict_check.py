"""Conflict check: detect active ChaosBlade experiments before injection.

Used by safety_check to prevent overlapping injections without user confirmation.
When conflicts are found, safety_check sets safety_status="warning", which triggers
the confirmation_gate to prompt the user before proceeding.
When target overlap is detected (same pod/node), safety_check issues a warning.
"""

import json as _json
import logging
import re
from dataclasses import dataclass

from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)

logger = logging.getLogger(__name__)


@dataclass
class ConflictInfo:
    """Structured conflict information with target overlap analysis."""

    uid: str
    flag: str = ""           # blade status Flag field (full command line)
    namespace: str = ""      # extracted --namespace value
    names: str = ""          # extracted --names value
    labels: str = ""         # extracted --labels value
    scope_target_action: str = ""  # parsed "scope-target-action" from flag (e.g. "pod-disk-burn")
    same_action_as_request: bool = False  # True when action matches current request (P1 escalation)
    overlaps_target: bool = False  # whether this experiment overlaps the current target
    overlap_reason: str = ""       # human-readable reason for overlap


def _extract_param_from_flag(flag: str, param_name: str) -> str:
    """Extract a parameter value from a blade Flag string.

    Handles both formats:
      --param-name=value  (e.g. --namespace=cms-demo)
      --param-name value  (e.g. --namespace cms-demo)

    Args:
        flag: The full Flag string from blade status output.
        param_name: Parameter name with leading dashes (e.g. "--namespace", "--names").

    Returns:
        Extracted value, or empty string if not found.
    """
    bare = param_name.lstrip("-")
    # Match --param=value or --param value
    pattern = rf"--{bare}=(\S+)|--{bare}\s+(\S+)"
    match = re.search(pattern, flag)
    if match:
        return match.group(1) or match.group(2)
    return ""


def _parse_scope_target_action_from_flag(flag: str) -> str:
    """Extract scope-target-action from a blade Flag string.

    Flag format: "k8s pod-disk burn --namespace cms-demo ..."
    Returns: "pod-disk-burn" or empty string if parsing fails.
    """
    # Match "k8s <scope-target> <action>" — two separate tokens.
    # e.g. "k8s pod-disk burn" → "pod-disk-burn"
    match = re.search(r"k8s\s+(\S+)\s+(\S+)", flag)
    if match:
        return f"{match.group(1)}-{match.group(2)}"  # e.g. "pod-disk-burn"
    return ""


def _analyze_overlap(
    conflict: ConflictInfo,
    target_namespace: str,
    target_names: str,
    target_labels: str,
    request_scope_target_action: str = "",
) -> None:
    """Analyze whether a conflict overlaps with the current injection target.

    Modifies conflict in-place to set overlaps_target and overlap_reason.
    Also sets same_action_as_request when action matches (P1 escalation).

    Overlap detection logic:
    - Exact name overlap (same --names in same --namespace) → overlaps
    - Labels overlap (same --labels in same --namespace) → overlaps
    - Same scope-target-action as request → same_action_as_request
    """
    reasons: list[str] = []

    # Parse scope-target-action from flag for action compatibility check (P1)
    sta = _parse_scope_target_action_from_flag(conflict.flag)
    if sta:
        conflict.scope_target_action = sta
        # Check if action matches current request (e.g. both are pod-disk-burn)
        if request_scope_target_action and sta == request_scope_target_action:
            conflict.same_action_as_request = True

    # Check namespace-level: only compare if both have namespace info
    ns_match = (
        conflict.namespace and target_namespace
        and conflict.namespace == target_namespace
    )

    # Check exact name overlap: same namespace AND same --names value
    if ns_match and conflict.names and target_names:
        conflict_name_set = set(n.strip() for n in conflict.names.split(",") if n.strip())
        target_name_set = set(n.strip() for n in target_names.split(",") if n.strip())
        overlap_names = conflict_name_set & target_name_set
        if overlap_names:
            reasons.append(
                f"same target: ns/{conflict.namespace} name/{','.join(sorted(overlap_names))}"
            )

    # Check labels overlap: same namespace AND same --labels value
    if ns_match and conflict.labels and target_labels:
        # Labels are comma-separated key=value pairs
        conflict_label_set = set(lbl.strip() for lbl in conflict.labels.split(",") if lbl.strip())
        target_label_set = set(lbl.strip() for lbl in target_labels.split(",") if lbl.strip())
        overlap_labels = conflict_label_set & target_label_set
        if overlap_labels:
            reasons.append(
                f"same labels: ns/{conflict.namespace} labels/{','.join(sorted(overlap_labels))}"
            )

    if reasons:
        conflict.overlaps_target = True
        conflict.overlap_reason = "; ".join(reasons)


async def check_blade_conflicts(
    kubeconfig: str, task_id: str,
    namespace: str = "", labels: str = "",
    target_names: str = "",
    request_scope_target_action: str = "",
) -> tuple[list[str], list[ConflictInfo]]:
    """Best-effort check for active ChaosBlade experiments on the cluster.

    Optionally filters results by namespace and/or labels when the
    blade status output is JSON (ChaosBlade >= 1.0).  Falls back to
    returning all detected UIDs when JSON parsing fails or no filter
    criteria are provided.

    When target_names is provided, also analyzes whether any active
    experiment targets the same resource (exact name or label overlap
    in the same namespace).  Experiments with target overlap are
    flagged via ConflictInfo.overlaps_target.

    Returns:
        Tuple of (uids, conflict_details):
        - uids: list of active experiment UIDs (backward compatible)
        - conflict_details: list of ConflictInfo with overlap analysis

    This is a SOFT check -- it reports conflicts but does not block injection.
    The caller (safety_check) decides whether to route to confirmation_gate
    or reject based on overlap severity.

    Emits a complete STARTED -> COMPLETED lifecycle under source
    "conflict-check" so the CLI shows the check as a distinct phase
    with a clear conclusion.  Saves and restores the tracker state
    so the parent operation's source/timing is not corrupted.
    """
    tracker = get_tracker(task_id) if task_id else None
    # Save parent tracker state to avoid corruption from sub-operations
    # (run_command now uses emit() instead of start/complete, so this
    # save/restore is defensive — protects against any future sub-ops
    # that might call tracker.start())
    saved_state = tracker.save_state() if tracker else None

    # Emit STARTED event for the conflict check as a whole
    if tracker:
        tracker.start(
            StatusCategory.NODE,
            "conflict-check",
            "Pre-injection conflict check: checking for active experiments",
            {"step": "conflict_check"},
        )

    try:
        from chaos_agent.tools.shell import run_command
        from chaos_agent.tools.kubectl import _build_kubectl_global_args
        from chaos_agent.agent.nodes._injection_detection import (
            discover_tool_pods, _TOOL_POD_NAMESPACE, _TOOL_POD_LABEL_SELECTOR,
        )

        kubeconfig_args = _build_kubectl_global_args(kubeconfig=kubeconfig)

        # Step 1: Discover running tool pods
        discover_cmd = (
            ["kubectl", "get", "pods", "-n", _TOOL_POD_NAMESPACE,
             "-l", _TOOL_POD_LABEL_SELECTOR]
            + kubeconfig_args
        )
        discover_result = await run_command(discover_cmd, task_id=task_id, source="conflict-check")
        pods = discover_tool_pods(discover_result.stdout)
        if not pods:
            if tracker:
                tracker.complete(
                    "Pre-injection conflict check: no tool pods found (skipped)",
                    {"step": "conflict_check", "status": "skipped", "reason": "no_tool_pods"},
                )
            return ([], [])

        # Step 2: Run blade status --type create in the first available pod
        status_cmd = (
            ["kubectl", "exec", pods[0], "-n", _TOOL_POD_NAMESPACE]
            + kubeconfig_args
            + ["--", "blade", "status", "--type", "create"]
        )
        status_result = await run_command(status_cmd, task_id=task_id, source="conflict-check")
        raw = status_result.stdout

        # Parse UIDs from output (handles both table and JSON format)
        uids = re.findall(r"[0-9a-f]{16}", raw)

        # Build ConflictInfo list with overlap analysis when JSON is available
        conflict_details: list[ConflictInfo] = []

        # Optional: filter by namespace and/or labels using JSON output.
        # blade status --type create returns JSON like:
        #   {"code":200,"success":true,"result":[{"Uid":"...","Flag":"..."}]}
        json_parsed = False
        if raw.strip():
            try:
                data = _json.loads(raw)
                if isinstance(data, dict) and data.get("success"):
                    result_list = data.get("result", [])
                    if isinstance(result_list, list):
                        json_parsed = True
                        filtered: list[str] = []
                        for exp in result_list:
                            if not isinstance(exp, dict):
                                continue
                            flag = exp.get("Flag", "")
                            uid = exp.get("Uid", "")
                            if not uid:
                                continue

                            # Skip experiments that are no longer active.
                            # ChaosBlade "blade status --type create" may return
                            # Destroyed/Revoked experiments in some versions;
                            # these should not be counted as conflicts.
                            status = exp.get("Status", "")
                            if status in ("Destroyed", "Revoked"):
                                continue

                            # Extract target info from Flag
                            exp_ns = _extract_param_from_flag(flag, "--namespace")
                            exp_names = _extract_param_from_flag(flag, "--names")
                            exp_labels = _extract_param_from_flag(flag, "--labels")

                            ci = ConflictInfo(
                                uid=uid,
                                flag=flag,
                                namespace=exp_ns,
                                names=exp_names,
                                labels=exp_labels,
                            )

                            # Filter by namespace/labels using extracted values.
                            # Uses _extract_param_from_flag result instead of
                            # substring matching on flag, so both
                            # "--namespace=cms-demo" and "--namespace cms-demo"
                            # formats are handled correctly.
                            if namespace and exp_ns != namespace:
                                continue
                            if labels and exp_labels != labels:
                                # Also check partial label overlap: include
                                # experiments sharing any label key=value pair.
                                target_label_set = set(
                                    lbl.strip() for lbl in labels.split(",") if lbl.strip()
                                )
                                exp_label_set = set(
                                    lbl.strip() for lbl in exp_labels.split(",") if lbl.strip()
                                )
                                if not (target_label_set & exp_label_set):
                                    continue

                            # Analyze overlap with current target
                            if target_names or labels:
                                _analyze_overlap(
                                    ci, namespace, target_names, labels,
                                    request_scope_target_action=request_scope_target_action,
                                )

                            filtered.append(uid)
                            conflict_details.append(ci)
                        uids = filtered
            except Exception:
                # Fall back to regex-extracted UIDs on parse failure
                pass

        # If JSON was not parsed, build basic ConflictInfo from regex UIDs
        if not json_parsed:
            for uid in uids:
                conflict_details.append(ConflictInfo(uid=uid))
        overlapping = [c for c in conflict_details if c.overlaps_target]
        if tracker:
            if uids:
                overlap_hint = f" ({len(overlapping)} with target overlap)" if overlapping else ""
                tracker.complete(
                    f"Pre-injection conflict check: {len(uids)} active experiment(s) found{overlap_hint}: {', '.join(uids[:5])}",
                    {"step": "conflict_check", "status": "conflicts_found", "conflict_count": len(uids), "uids": uids[:5], "overlap_count": len(overlapping)},
                )
            else:
                tracker.complete(
                    "Pre-injection conflict check: no active experiments",
                    {"step": "conflict_check", "status": "clear"},
                )
        return (uids, conflict_details)
    except Exception:
        logger.debug(f"Blade conflict check failed for task {task_id}", exc_info=True)
        if tracker:
            tracker.complete(
                "Pre-injection conflict check: failed (soft, non-blocking)",
                {"step": "conflict_check", "status": "failed"},
            )
        return ([], [])
    finally:
        # Restore parent tracker state so the caller's subsequent
        # tracker.update/complete calls use the correct source/timing
        if saved_state is not None and tracker:
            tracker.restore_state(saved_state)
