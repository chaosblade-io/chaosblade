"""Shared utilities between verifier.py and recover_verifier.py.

Decouples the cross-file dependency on private symbols so both files
can be independently split into sub-modules without circular imports.
"""

import re

from chaos_agent.agent.state import AgentState


# ---------------------------------------------------------------------------
# Status keyword parsing (used by both verifier & recover_verifier)
# ---------------------------------------------------------------------------

def _has_negative_prefix(text: str, keyword: str) -> bool:
    """Check if keyword is preceded by a negation like 'not' or 'un'."""
    idx = text.find(keyword)
    if idx <= 0:
        return False
    prefix = text[:idx].rstrip()
    return prefix.endswith("not") or prefix.endswith("un")


def _parse_status_keyword(text: str, keywords: tuple = (
    "recovered_before_observation",  # MUST be before "partial" — longer keyword first
    "passed",
    "failed",
    "skipped",
    "partial",
)) -> str:
    """Parse status keyword with negation awareness.

    Returns the status ('passed', 'failed', 'skipped', 'partial',
    'recovered_before_observation') or 'unknown'.
    Handles negation like 'not passed' → 'failed', 'not failed' → 'passed'.
    """
    for kw in keywords:
        if kw in text:
            if _has_negative_prefix(text, kw):
                # Negate: not passed → failed, not failed → passed, not skipped → failed
                return "failed" if kw in ("passed", "skipped") else "passed"
            return kw
    return "unknown"


# ---------------------------------------------------------------------------
# Disk path heuristics (used by both verifier & recover_verifier)
# ---------------------------------------------------------------------------

# Path-to-partition HEURISTIC for ChaosBlade K8s CRD mode (node-disk fill).
# These are based on common configurations; actual partition depends on node's
# mount layout. Verify with `df -h` (bare) on the actual node.
#
# NOTE: /var/lib/docker and /var/lib/containerd are the container runtime storage
# roots. When on a separate disk, they define imagefs (not nodefs). They are listed
# in _NODEFS_PATHS for the common case where they share the root partition.
# The verifier's `df -h` check is the source of truth, not this heuristic.
_IMAGEFS_PATHS = frozenset({
    "/tmp", "/var/log", "/var/run", "/run", "/tmp/", "/var/log/", "/var/run/", "/run/",
})
_NODEFS_PATHS = frozenset({
    "/var/lib/docker", "/var/lib/containerd", "/var/lib/kubelet",
    "/var/lib/docker/", "/var/lib/containerd/", "/var/lib/kubelet/",
    "/etc", "/etc/", "/root", "/root/", "/home", "/home/",
})


# ---------------------------------------------------------------------------
# Baseline confidence (used by both verifier & recover_verifier)
# ---------------------------------------------------------------------------

def _compute_baseline_confidence(state: AgentState) -> str:
    """Compute baseline_confidence from state's baseline_data.

    Returns:
        "high" — baseline captured with all commands succeeding
        "partial" — baseline captured but some commands failed
        "none" — no baseline data or zero successes
    """
    baseline = state.get("baseline_data")
    if not baseline:
        return "none"
    success_count = baseline.get("success_count", 0)
    if success_count <= 0:
        return "none"
    # Derive total from observations list length
    total = baseline.get("total_count", len(baseline.get("observations", [])))
    if total > 0 and success_count >= total:
        return "high"
    return "partial"


# ---------------------------------------------------------------------------
# Disk topology hints (used by verifier & recover_verifier via lazy import)
# ---------------------------------------------------------------------------

def _get_node_disk_topology_hints(blade_action: str | None = None) -> str:
    """Generate node disk topology hints tailored to the specific action.

    For fill: df -h is the primary verification method (fill accumulates data).
    For burn: /proc/diskstats delta is the primary method (burn produces I/O, not data).
    """
    base = (
        "Multi-disk node topology: A K8s node may have separate filesystems for "
        "nodefs (root partition, e.g. /dev/vda3) and imagefs (container runtime data, "
        "e.g. /dev/vdb). Kubelet monitors both independently for DiskPressure.\n"
        "- In ChaosBlade K8s CRD mode: `--path /tmp` or `--path /var/log` is inside "
        "the container overlay, TYPICALLY backed by imagefs (if the node has a separate "
        "imagefs; otherwise on nodefs). `--path /var/lib/docker` or host root paths are "
        "TYPICALLY on nodefs. The actual partition depends on the node's mount layout — "
        "verify with `df -h` (bare).\n"
    )
    if blade_action == "fill":
        base += (
            "- `df -h /host` inside the host-access pod shows nodefs ONLY. If the fill targeted "
            "imagefs, this command shows NO change even though fill succeeded — this is a "
            "FALSE NEGATIVE, not a failed injection.\n"
            "- YOUR FIRST CHECK MUST BE `df -h` (bare, no path) to list ALL mounted "
            "filesystems, then identify which partition shows increased usage. Do NOT use "
            "`df -h /host` as the sole disk check — it will give a false negative for "
            "imagefs-targeted fills. Match the partition against the 'path' parameter in "
            "Blade key parameters above.\n"
        )
    elif blade_action == "burn":
        base += (
            "For node-disk-burn (I/O stress, NOT data accumulation):\n"
            "- df -h is USELESS for burn verification — burn creates temporary I/O "
            "pressure, not persistent data. Do NOT use df -h as your primary burn check.\n"
            "- CORRECT verification: compare two /proc/diskstats samples (3-5s apart) "
            "and calculate write throughput delta on ALL partitions (not just the "
            "partition containing --path).\n"
            "- The dd processes write to the container overlay (typically on imagefs/vdb), "
            "so the I/O will appear on the overlay's backing partition, NOT on the nodefs "
            "partition where /host/tmp resides.\n"
            "- If any partition shows >10MB/s sustained write throughput → burn IS in "
            "effect. This is the PRIMARY evidence for burn verification.\n"
            "- DO NOT check /host/tmp/ for burn files — they exist in the container "
            "overlay, not on the host filesystem. Their absence does NOT mean the burn "
            "is not working.\n"
            "- If the Injection Engine Post-Check already detected burn I/O, that is "
            "AUTHORITATIVE evidence — use it as your primary verification and do NOT "
            "repeat the diskstats check yourself.\n"
        )
    else:
        # Generic case: include both strategies
        base += (
            "- YOUR FIRST CHECK MUST BE `df -h` (bare, no path) to list ALL mounted "
            "filesystems, then identify which partition shows changes. For fill faults, "
            "look for increased usage. For burn faults, `df -h` will NOT show changes "
            "— use `/proc/diskstats` delta sampling instead.\n"
        )
    return base


# ---------------------------------------------------------------------------
# Submit args extraction (used by both verifier & recover finalize)
# ---------------------------------------------------------------------------

def extract_submit_args(
    messages: list,
    *,
    tool_name: str,
    guard_markers: tuple[str, ...],
) -> dict | None:
    """Return args of the most recent submit tool_call, or None.

    Only scans messages AFTER the last HumanMessage containing any guard_marker
    string, preventing stale args from a prior round being re-used.
    """
    from langchain_core.messages import HumanMessage
    boundary_idx = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, HumanMessage):
            content = getattr(msg, "content", "") or ""
            if any(marker in content for marker in guard_markers):
                boundary_idx = i

    search_slice = messages[boundary_idx + 1:] if boundary_idx >= 0 else messages

    for msg in reversed(search_slice):
        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in tool_calls:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            if name == tool_name:
                args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                return args or {}
    return None


# ---------------------------------------------------------------------------
# Last AI text extraction (used by both verifier & recover finalize)
# ---------------------------------------------------------------------------

def last_ai_text(messages: list) -> str:
    """Return the content of the last assistant message (text fallback source).

    The assistant message is the last entry that is neither a HumanMessage,
    ToolMessage, nor SystemMessage — robust to both real AIMessage (type="ai")
    and test doubles (MagicMock without .type).
    """
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
    for msg in reversed(messages):
        if isinstance(msg, (HumanMessage, ToolMessage, SystemMessage)):
            continue
        content = getattr(msg, "content", "") or ""
        if content:
            return content if isinstance(content, str) else str(content)
    return ""


# ---------------------------------------------------------------------------
# Shared checklist parsing (used by both verifier & recover_verifier L2 parse)
# ---------------------------------------------------------------------------

def parse_checklist_items(
    text: str,
    *,
    section_marker: str,
    end_marker: str,
    patterns: list[re.Pattern],
    capture_evidence: bool = False,
) -> list[dict]:
    """Parse verification checklist items from LLM output.

    Args:
        section_marker: e.g. "VERIFICATION_CHECKLIST:" or "RECOVERY_VERIFICATION_CHECKLIST:"
        end_marker: e.g. "VERIFICATION_RESULT:" or "RECOVERY_VERIFICATION_RESULT:"
        patterns: compiled regex patterns (each must have group(1)=step, group(2)=status)
        capture_evidence: if True, extract group(3) as 'evidence' when present
    """
    items: list[dict] = []
    seen_steps: set[str] = set()

    checklist_section = text
    if section_marker in text:
        start = text.index(section_marker) + len(section_marker)
        remainder = text[start:]
        if end_marker in remainder:
            end = remainder.index(end_marker)
            checklist_section = remainder[:end]
        else:
            checklist_section = remainder

    for pattern in patterns:
        for match in pattern.finditer(checklist_section):
            if "[skipped]" in match.group(0).lower():
                step_str = match.group(1) if match.group(1) else str(len(seen_steps) + 1)
                status = "skipped"
            else:
                step_str = match.group(1)
                status = match.group(2).lower()

            if step_str in seen_steps:
                continue
            seen_steps.add(step_str)
            try:
                step_num = int(step_str)
            except ValueError:
                step_num = len(items) + 1
            item: dict = {"step": step_num, "status": status}
            if capture_evidence:
                evidence = match.group(3) if match.lastindex and match.lastindex >= 3 else None
                if evidence:
                    item["evidence"] = evidence.strip()
            items.append(item)

    return items


def has_checklist(
    text: str,
    *,
    section_marker: str,
    patterns: list[re.Pattern],
) -> bool:
    """Check if text contains a checklist section or items."""
    if section_marker in text:
        return True
    for pattern in patterns:
        if pattern.search(text):
            return True
    return False