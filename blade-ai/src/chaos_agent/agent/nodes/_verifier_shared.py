"""Shared utilities between verifier.py and recover_verifier.py.

Decouples the cross-file dependency on private symbols so both files
can be independently split into sub-modules without circular imports.
"""

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
            "- `df -h /host` inside kubectl debug shows nodefs ONLY. If the fill targeted "
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