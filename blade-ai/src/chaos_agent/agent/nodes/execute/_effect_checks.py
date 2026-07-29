"""Programmatic post-injection effect checks (disk fill / burn).

These bridge the trust gap between "blade query k8s says Success" and "the
fault effect is actually present" by probing the target after injection. The
fault-specific judgment lives here (fill markers, per-partition diskstats
throughput); *what* evidence to gather is declared as a semantic ``Probe`` (see
``_effect_probes``), and *where / how* it is sampled is delegated to a per-scope
``EffectSampleChannel`` (see ``_effect_channels``). The channel translates the
probe to the right command for its scope, so host / node / pod all flow through
one sampling path instead of inline ``if scope ==`` / hardcoded-command
branches.

They live in this leaf module so the ``VerificationProfile.post_injection_checks``
seam can reference them without importing the large ``direct_execute`` node
module. Cluster/tool-pod discovery stays lazily imported inside the channel
layer to avoid load-time coupling.
"""

import logging
import re

from chaos_agent.agent.nodes.execute._effect_channels import resolve_effect_channel
from chaos_agent.agent.nodes.execute._effect_probes import Probe

logger = logging.getLogger(__name__)


async def _verify_disk_fill_effect(
    scope: str,
    target: str,
    action: str,
    names: str,
    kubeconfig: str,
    params: dict,
    blade_uid: str,
    task_id: str,
    state: dict | None = None,
) -> dict | None:
    """Programmatic post-injection effect check for disk-fill faults.

    Bridges the trust gap between "blade query k8s says Success" and "the
    filesystem was actually filled". Samples the target (host directly, or the
    tool pod on the target node for node-scope faults) and checks for the
    ChaosBlade fill file plus current filesystem usage.

    Returns a dict with check results, or None if not applicable.
    """
    if not (target == "disk" and action == "fill"):
        return None

    fill_path = params.get("path", "/tmp")
    size = params.get("size", "?")

    # disk-fill samples the host directly or the target node's tool pod; pod
    # scope is intentionally not verified here (matches prior behaviour).
    channel = await resolve_effect_channel(
        scope,
        names=names,
        namespace="",
        kubeconfig=kubeconfig,
        task_id=task_id,
        state=state,
        allowed_scopes=("host", "node"),
    )
    if channel is None:
        return None

    # Check for the fill file. Only a ChaosBlade fill marker
    # (``chaos_fill*`` / ``chaosblade``) counts as positive evidence — a bare
    # non-empty listing is NOT evidence: ``fill_path`` is usually a directory
    # (default ``/tmp``), which lists non-empty regardless. The raw ls/df
    # output is still returned so the caller can judge host-native fills.
    ls_stdout = await channel.sample(Probe("disk_fill_listing", {"path": fill_path}))
    has_fill_file = any(pat in ls_stdout for pat in ("chaos_fill", "chaosblade"))

    df_stdout = await channel.sample(Probe("disk_usage", {"path": fill_path}))

    result = {
        "fill_file_found": has_fill_file,
        "requested_size": size,
        "ls_output": ls_stdout[:500],
        "df_output": df_stdout[:500],
        "blade_uid": blade_uid,
        "scope": scope,
    }
    if channel.pod_name:
        result["target_pod"] = channel.pod_name
    if channel.node_name:
        result["node"] = channel.node_name

    if has_fill_file:
        logger.info(
            "disk-fill post-check PASSED: fill file found (scope=%s, path=%s)",
            scope, fill_path,
        )
    else:
        logger.warning(
            "disk-fill post-check WARNING: no fill file found (scope=%s, path=%s) — "
            "blade_uid=%s reports Success but filesystem may not have been modified",
            scope, fill_path, blade_uid,
        )

    return result


async def _verify_disk_burn_effect(
    scope: str,
    target: str,
    action: str,
    names: str,
    kubeconfig: str,
    params: dict,
    blade_uid: str,
    task_id: str,
    namespace: str = "",
    state: dict | None = None,
) -> dict | None:
    """Programmatic post-injection effect check for disk-burn faults.

    Bridges the trust gap between "blade query k8s says Success" and "the disk
    I/O pressure is actually present" by sampling ``/proc/diskstats`` twice with
    a short interval and computing per-partition write throughput. The sample
    source (host directly, tool pod on the target node, or the target pod with a
    tool-pod fallback) is resolved by the per-scope channel.

    Returns a dict with check results, or None if not applicable.
    """
    if not (target == "disk" and action == "burn"):
        return None

    # Resolve where to sample /proc/diskstats from. The ``diskstats`` probe
    # doubles as the pod-support check (deciding whether the target pod is
    # directly sampleable).
    channel = await resolve_effect_channel(
        scope,
        names=names,
        namespace=namespace,
        kubeconfig=kubeconfig,
        task_id=task_id,
        state=state,
        probe=Probe("diskstats"),
        allowed_scopes=("host", "node", "pod"),
    )
    if channel is None:
        return None

    # Step 1: Sample /proc/diskstats twice with a 5-second interval.
    sample1_text = await channel.sample(Probe("diskstats"))
    if not sample1_text.strip():
        logger.warning(
            "disk-burn post-check: first diskstats sample empty (scope=%s)", scope,
        )
        return None

    import asyncio
    await asyncio.sleep(5)

    sample2_text = await channel.sample(Probe("diskstats"))

    # Step 2: Parse diskstats and compute write throughput per partition.
    _SECTOR_SIZE = 512
    _SAMPLE_INTERVAL = 5
    _BURN_DETECTION_THRESHOLD_MB_S = 10  # 10 MB/s sustained write = burn detected

    def _parse_diskstats(text: str) -> dict[str, int]:
        """Parse /proc/diskstats into {partition_name: sectors_written}.

        /proc/diskstats format (fields 0-based):
            0: major  1: minor  2: name  3: reads_completed  4: reads_merged
            5: sectors_read  6: ms_reading  7: writes_completed  8: writes_merged
            9: sectors_written  10: ms_writing  ...
        Field 9 = sectors_written (cumulative).
        """
        result = {}
        for line in text.strip().splitlines():
            fields = line.split()
            if len(fields) < 10:
                continue
            name = fields[2]
            # Skip partitions (e.g., vda1, vda3) — only track whole devices.
            # nvme0n1 is a whole device; nvme0n1p1 is a partition.
            # vdX/sdX without trailing digit = whole device.
            # vdX1/vdX3/sdX1 = partition.
            if re.match(r"^(vd|sd|xvd)\D+\d+$", name):
                # Partition: vda1, vda3, sda1 — skip
                continue
            if re.match(r"^nvme\d+n\d+p\d+$", name):
                # NVMe partition: nvme0n1p1 — skip
                continue
            try:
                sectors_written = int(fields[9])
                result[name] = sectors_written
            except (ValueError, IndexError):
                continue
        return result

    stats1 = _parse_diskstats(sample1_text)
    stats2 = _parse_diskstats(sample2_text)

    active_partitions = []
    burn_io_detected = False
    for name in stats2:
        if name not in stats1:
            continue
        delta_sectors = stats2[name] - stats1[name]
        if delta_sectors < 0:
            continue  # counter wraparound or parse error
        throughput_mb_s = delta_sectors * _SECTOR_SIZE / (1024 * 1024) / _SAMPLE_INTERVAL
        if throughput_mb_s > 0.1:  # Only report partitions with measurable I/O
            active_partitions.append({
                "name": name,
                "write_throughput_mb_s": round(throughput_mb_s, 1),
            })
        if throughput_mb_s > _BURN_DETECTION_THRESHOLD_MB_S:
            burn_io_detected = True

    # Sort by throughput descending
    active_partitions.sort(key=lambda p: p["write_throughput_mb_s"], reverse=True)

    result = {
        "burn_io_detected": burn_io_detected,
        "active_partitions": active_partitions,
        "target_pod": channel.pod_name,
        "node": channel.node_name,
        "scope": scope,
        "blade_uid": blade_uid,
        "sample_interval_seconds": _SAMPLE_INTERVAL,
    }

    if burn_io_detected:
        top_partition = active_partitions[0] if active_partitions else {}
        logger.info(
            f"disk-burn post-check PASSED: burn I/O detected on "
            f"{top_partition.get('name', '?')}: "
            f"~{top_partition.get('write_throughput_mb_s', 0)} MB/s write throughput"
        )
    else:
        logger.warning(
            f"disk-burn post-check WARNING: no significant I/O detected on any partition "
            f"(top: {active_partitions[0] if active_partitions else 'none'}) — "
            f"blade_uid={blade_uid} reports Success but no burn I/O observed"
        )

    return result
