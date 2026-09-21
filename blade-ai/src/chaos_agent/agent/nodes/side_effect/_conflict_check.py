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
from chaos_agent.agent.dispatch import dispatch_node_message
from chaos_agent.agent.providers.uid_shapes import (
    HEX16_UID_SHAPE,
    HEX_HEAD_GUARD,
    HEX_TAIL_GUARD,
)
from chaos_agent.tools.request_identity import (  # noqa: F401 — re-export
    RequestFingerprint,
    build_request_fingerprint,
)

logger = logging.getLogger(__name__)


# Raw-output UID fallback anchor (round-21; edges re-legislated same
# round after the adversarial re-review; round-22 Q1 composed them from
# the single source): when the blade status JSON cannot be parsed,
# experiment existence is mined from the raw output by shape alone.
# Composes the single-source hex16 legislation (``agent/providers/
# uid_shapes.py`` — the arbitration layer this node is allowed to import)
# with BOTH word boundaries, CASE-DOUBLE on each edge (the same r19-N3b
# ruling as FAILED_CREATE_UID_RE's trailer lookahead, now single-sourced
# as HEX_HEAD_GUARD / HEX_TAIL_GUARD): the first draft's lowercase-only
# edges truncated a mixed-case token (16 lowercase hex + uppercase
# trailer) into a 16-hex fake, and the pre-round-21 hand-copied
# ``[0-9a-f]{16}`` (fixed 16, no edges) truncated a 40-hex sha256 shape
# into TWO fake well-shaped UIDs and split a legal 32-hex UID into two
# identical 16-char fakes, while never recognizing the 17-32 length range
# the legislation admits. The lookarounds keep a resource-name suffix
# (``chaosblade-<uid>``) capturable — the prefix is not a hex character —
# while refusing any mid-string or case-adjacent partial match. A
# hand-typed edge can drift casing silently while every shape-domain
# check stays green — the guards are composed, never re-typed.
FALLBACK_UID_RE = re.compile(HEX_HEAD_GUARD + HEX16_UID_SHAPE + HEX_TAIL_GUARD)


def _persist_conclusion(task_id: str, message: str, detail: dict) -> None:
    """Persist the conflict-check conclusion into the task record.

    ``dispatch_node_message`` is TUI-stream-only and tracker state is not
    exported to the task JSON — without this, the check's outcome left
    no trace in the task file and post-hoc forensics could only
    reconstruct it from behavioral signatures (inject-0db61248 analysis:
    the tier-2 undeterminable note was provably emitted but nowhere to
    be found in the persisted record). ``sync_node_status_to_session``
    only reads ``task_id`` from its state argument, which this module
    already holds, so conclusions can be persisted at the same place
    they are computed — same source of truth as tracker/dispatch, no
    drift possible. Fire-and-forget, never raises.
    """
    try:
        from chaos_agent.agent.nodes.store._store_sync import (
            sync_node_status_to_session,
        )
        sync_node_status_to_session(
            {"task_id": task_id}, "conflict-check", message, detail,
        )
    except Exception:
        logger.debug("conflict-check conclusion persistence failed", exc_info=True)


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
    # True when the Flag carries no --namespace (cri scope targets a
    # container-id, node scope a node name — neither lives in a namespace)
    # or when the blade status output was not parseable at all. Overlap
    # with the current target CANNOT be determined for these; they are
    # reported in details (weak note) but never treated as conflicts.
    undeterminable: bool = False


# RequestFingerprint / build_request_fingerprint were PROMOTED to neutral
# ground — chaos_agent/tools/request_identity.py, the create-reconcile
# gate's cross-layer identity contract (same rationale as
# tools/markers.py: the generic gate and the carrier-side judgment
# material both consume it) — and are re-exported from the import block
# above for historical import paths. The conflict QUERY itself
# (check_blade_conflicts below) is carrier judgment material and stays
# here.


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
    # Match --param=value or --param value. \S+ stops at whitespace;
    # blade CLI uses comma-separated values (--names a,b,c) so this
    # captures the full value string for all known flag formats.
    pattern = rf"--{bare}=(\S+)|--{bare}\s+(\S+)"
    match = re.search(pattern, flag)
    if match:
        return match.group(1) or match.group(2)
    return ""


def _parse_scope_target_action_from_flag(flag: str) -> str:
    """Extract scope-target-action from a blade Flag string.

    Flag format (k8s): "k8s pod-disk burn --namespace cms-demo ..."
    Flag format (host): "cpu fullload --cpu-percent 80 ..."
    Returns: "pod-disk-burn" / "cpu-fullload" or empty string if parsing fails.
    """
    # k8s mode: "k8s <scope-target> <action>"
    match = re.search(r"k8s\s+(\S+)\s+(\S+)", flag)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    # host mode fallback: "<target> <action>" (first two non-flag tokens)
    stripped = flag.strip()
    if stripped:
        match = re.match(r"(\S+)\s+(\S+)", stripped)
        if match and not match.group(1).startswith("--"):
            return f"{match.group(1)}-{match.group(2)}"
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

    Three-tier isolation semantics (destroyed/Revoked always excluded):

    1. PROVABLY UNRELATED — experiment in a KNOWN namespace different
       from the target's (pod-scope, both namespaces known): silently
       skipped. This preserves the isolation intent of the original
       namespace filter: shared clusters must not surface other teams'
       experiments as conflicts on every injection.
    2. UNDETERMINABLE — Flag carries no --namespace (cri scope targets
       a container-id, node scope a node name — node faults hit every
       namespace on that node, yet carry no ns to compare): kept in
       conflict_details with undeterminable=True but NEVER in uids, so
       the caller reports a weak note, not a "consider destroying"
       warning. Dropping this tier entirely is how the original bug
       reported "no active experiments" on a cluster with 4 recorded
       experiments, one of them a live cri mem-load (inject-17617837
       forensics, verified live).
    3. CONFLICT CANDIDATE — same namespace (or unknown target ns):
       appended to uids and run through overlap analysis.

    Falls back to regex UID extraction when JSON parsing fails; those
    UIDs are undeterminable (no Flag to analyze) and follow tier 2.

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
        from chaos_agent.tools.kubectl_cli import build_kubectl_cmd
        from chaos_agent.transports import (
            PROFILE_K8S,
            TransportTarget,
            execute_via_transport,
        )
        from chaos_agent.tools.pod_discovery import (
            discover_tool_pods_cluster_wide,
        )

        # Step 1: Discover running tool pods (all-namespaces, multiple label candidates)
        pods_with_ns = await discover_tool_pods_cluster_wide(kubeconfig, task_id)
        if not pods_with_ns:
            _msg = "Pre-injection conflict check: no tool pods found (skipped)"
            if tracker:
                tracker.complete(
                    _msg,
                    {"step": "conflict_check", "status": "skipped", "reason": "no_tool_pods"},
                )
            await dispatch_node_message("conflict-check", f"{_msg}\n\n")
            _persist_conclusion(task_id, _msg, {"step": "conflict_check", "status": "skipped", "reason": "no_tool_pods"})
            return ([], [])

        # Step 2: Run blade status --type create in the first available pod.
        # Any single pod suffices: blade status --type create queries
        # ChaosBlade CRDs via the K8s API, returning cluster-wide results.
        pod_name, pod_ns = pods_with_ns[0]
        status_cmd = build_kubectl_cmd("exec", [
            pod_name, "-n", pod_ns,
            "--", "blade", "status", "--type", "create",
        ], kubeconfig=kubeconfig)
        _target = TransportTarget.from_state({})
        status_result = await execute_via_transport(
            status_cmd, _target, task_id=task_id, source="conflict-check", expect_profile=PROFILE_K8S)
        raw = status_result.stdout

        # A failed or timed-out `blade status` (exit != 0, empty stdout)
        # must report UNKNOWN — never silently convert to "clear"
        # (inject-17617837: a 31s wiz timeout returned exit 1 + empty
        # stdout, which flowed through the regex fallback on an EMPTY
        # string, found no UIDs, and was reported as "no active
        # experiments" while the check had in fact verified nothing).
        if status_result.exit_code != 0 or not raw.strip():
            _reason = f"blade status exited {status_result.exit_code}"
            if not raw.strip():
                _reason += ", empty output"
            _msg = (
                f"Pre-injection conflict check: FAILED ({_reason}). "
                "Active experiments cannot be ruled out — treat as UNKNOWN, not clear."
            )
            if tracker:
                tracker.complete(
                    _msg,
                    {"step": "conflict_check", "status": "failed"},
                )
            await dispatch_node_message("conflict-check", f"{_msg}\n\n")
            _persist_conclusion(task_id, _msg, {"step": "conflict_check", "status": "failed", "reason": _reason})
            return ([], [])

        # Build ConflictInfo list with overlap analysis when JSON is available
        uids: list[str] = []
        conflict_details: list[ConflictInfo] = []

        # Prefer structured JSON parsing over regex. blade status --type
        # create returns JSON like:
        #   {"code":200,"success":true,"result":[{"Uid":"...","Flag":"..."}]}
        json_parsed = False
        if raw.strip():
            try:
                data = _json.loads(raw)
                if isinstance(data, dict) and data.get("success"):
                    result_list = data.get("result", [])
                    if isinstance(result_list, list):
                        json_parsed = True
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

                            # Three-tier isolation (inject-17617837 review):
                            # a NARROW skip replaces the old namespace
                            # pre-filter. The old filter dropped BOTH
                            # unrelated cross-ns experiments (fine) and
                            # namespace-less cri/node experiments (the bug:
                            # a live cri mem-load stayed invisible). The
                            # skip below only fires when BOTH namespaces
                            # are known and they differ — provably
                            # unrelated, safe to isolate away.
                            if namespace and exp_ns and exp_ns != namespace:
                                continue

                            ci = ConflictInfo(
                                uid=uid,
                                flag=flag,
                                namespace=exp_ns,
                                names=exp_names,
                                labels=exp_labels,
                                undeterminable=not exp_ns,
                            )

                            # Overlap analysis runs for every non-skipped
                            # experiment. For undeterminable ones it can
                            # still parse scope-target-action (same-action
                            # awareness); ns-based overlap needs both sides.
                            _analyze_overlap(
                                ci, namespace, target_names, labels,
                                request_scope_target_action=request_scope_target_action,
                            )

                            if not ci.undeterminable:
                                uids.append(uid)
                            conflict_details.append(ci)
            except Exception:
                logger.warning(
                    "blade status JSON parse failed; falling back to regex UID extraction"
                )

        # Fallback: regex-extract UIDs from raw output when JSON parsing
        # failed or returned non-standard format. Nothing is known about
        # these experiments beyond their existence, so they all follow the
        # UNDETERMINABLE tier: visible in details, never asserted as
        # conflicts (uids).
        if not json_parsed:
            fallback_uids = FALLBACK_UID_RE.findall(raw)
            for uid in fallback_uids:
                conflict_details.append(ConflictInfo(uid=uid, undeterminable=True))
        overlapping = [c for c in conflict_details if c.overlaps_target]
        no_ns = [c for c in conflict_details if c.undeterminable]
        if tracker:
            if uids:
                _hints = []
                if overlapping:
                    _hints.append(f"{len(overlapping)} with target overlap")
                if no_ns:
                    _hints.append(
                        f"{len(no_ns)} undeterminable (cri/node scope, "
                        f"no namespace info)"
                    )
                overlap_hint = f" ({'; '.join(_hints)})" if _hints else ""
                _msg = f"Pre-injection conflict check: {len(uids)} active experiment(s) in namespace '{namespace}'{overlap_hint}: {', '.join(uids[:5])}"
                _detail = {"step": "conflict_check", "status": "conflicts_found", "conflict_count": len(uids), "uids": uids[:5], "overlap_count": len(overlapping)}
                tracker.complete(
                    _msg,
                    _detail,
                )
                await dispatch_node_message("conflict-check", f"{_msg}\n\n")
                _persist_conclusion(task_id, _msg, _detail)
            elif no_ns:
                # uids is empty but undeterminable experiments exist: the
                # honest message is NOT "no active experiments" (the live
                # cluster proved that lie) — scope the claim to the
                # namespace and surface what cannot be ruled out.
                _msg = (
                    f"Pre-injection conflict check: no active experiments in "
                    f"namespace '{namespace}', but {len(no_ns)} active "
                    f"experiment(s) carry no namespace info (cri/node scope, "
                    f"overlap undeterminable): "
                    f"{', '.join(c.uid[:16] for c in no_ns[:5])}"
                )
                _detail = {"step": "conflict_check", "status": "clear",
                           "undeterminable_count": len(no_ns),
                           "undeterminable_uids": [c.uid[:16] for c in no_ns[:5]]}
                tracker.complete(
                    _msg,
                    _detail,
                )
                await dispatch_node_message("conflict-check", f"{_msg}\n\n")
                _persist_conclusion(task_id, _msg, _detail)
            else:
                _msg = f"Pre-injection conflict check: no active experiments in namespace '{namespace}'"
                tracker.complete(
                    _msg,
                    {"step": "conflict_check", "status": "clear"},
                )
                await dispatch_node_message("conflict-check", f"{_msg}\n\n")
                _persist_conclusion(task_id, _msg, {"step": "conflict_check", "status": "clear"})
        return (uids, conflict_details)
    except Exception:
        logger.debug(f"Blade conflict check failed for task {task_id}", exc_info=True)
        _msg = "Pre-injection conflict check: failed (soft, non-blocking)"
        if tracker:
            tracker.complete(
                _msg,
                {"step": "conflict_check", "status": "failed"},
            )
        await dispatch_node_message("conflict-check", f"{_msg}\n\n")
        _persist_conclusion(task_id, _msg, {"step": "conflict_check", "status": "failed", "reason": "exception"})
        return ([], [])
    finally:
        # Restore parent tracker state so the caller's subsequent
        # tracker.update/complete calls use the correct source/timing
        if saved_state is not None and tracker:
            tracker.restore_state(saved_state)
