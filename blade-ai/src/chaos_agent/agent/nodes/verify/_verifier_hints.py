"""Hints domain code for the verifier node.

Extracted from verifier.py — contains observability hints, parameter-dependent
hint generators, baseline metric extraction, fault verification hint assembly,
and tool pod discovery for Layer 2 verification.

Per-fault-type verification knowledge (disk partition derivation, disk-fill /
disk-burn hint generators, static observability strings, node-disk topology /
event-filtering notes) now lives in
:mod:`chaos_agent.agent.nodes.verify._verification_profiles` behind the
``VerificationProfile`` seam. This module only assembles the fault-agnostic
scaffolding in ``_get_fault_verification_hints`` and delegates every
fault-specific fragment to the resolved profile's slots.
"""

import logging

logger = logging.getLogger(__name__)


def _extract_baseline_key_metrics(
    baseline: dict,
    blade_target: str,
    blade_action: str,
) -> dict[str, str]:
    """Extract structured key metrics from baseline observations.

    Thin wrapper around ``_metric_extractor.extract_baseline_metrics``
    (E2). The actual parsing lives in the shared extractor so Layer 2
    verification can reuse the same per-format parsers on
    post-injection kubectl output, not just baseline. Returns the
    fault-filtered dict the existing Layer 2 prompt builder expects.
    """
    from chaos_agent.agent.nodes.verify._metric_extractor import extract_baseline_metrics
    return extract_baseline_metrics(baseline, blade_target, blade_action)


_BASELINE_INTEGRITY_PROMPT: str = (
    "**BASELINE INTEGRITY** (applies to ALL quantitative metric verification — "
    "disk %, CPU %, memory %, latency ms, etc. Does NOT apply to qualitative status "
    "checks like 'Pod is Running' or 'Service is reachable'):\n"
    "1. IDENTIFY the exact resource you are measuring — be specific:\n"
    '   "imagefs /dev/vdb", "node cn-hongkong.10.0.2.69 CPU", "pod accounting memory", '
    '"endpoint /api/health latency"\n'
    '   "disk" or "CPU" alone is ambiguous — always include the resource identity.\n'
    "2. Your FIRST measurement is your BASELINE. Record the resource identity AND value together.\n"
    "3. ALL comparisons MUST be against the SAME resource. NEVER compare metrics from different resources:\n"
    '   ✅ "imagefs /dev/vdb: first-check 42% → re-check 84%" (same partition, valid delta)\n'
    '   ✅ "node X CPU: 12% → 89%" (same node, valid delta)\n'
    '   ❌ "first-check 16% → re-check 84%" (different partitions: 16% was nodefs /dev/vda3, '
    "84% was imagefs /dev/vdb — INVALID comparison)\n"
    "4. If you lack a pre-injection baseline for the target resource, say so explicitly:\n"
    '   "No pre-injection baseline available for imagefs /dev/vdb. Current value: 84%."\n'
    "5. HIGH post-injection values WITHOUT baseline context are ambiguous — the value may be "
    "pre-existing, not fault-caused. Look for corroborating evidence (e.g., DiskPressure condition, "
    "recent events, timestamp correlation with injection time).\n"
    "6. If your first-check value already matches the expected injection parameter "
    "(e.g., --percent 85 → first-check shows 84%), this IS evidence the fault is in effect — "
    "do NOT conclude 'no change' just because re-check shows the same value.\n"
    "7. EXPECTED NEGATIVE RESULTS: If the PRIMARY metric confirms the fault is in effect "
    "(e.g., disk usage matches --percent), but a THRESHOLD-DEPENDENT condition is not met "
    "(e.g., DiskPressure=False because usage is 84% vs 85% threshold), mark that step as "
    "'expected' — the negative result is anticipated and does not indicate injection failure. "
    "Do NOT use 'expected' as a synonym for 'failed'."
)


def _get_fault_verification_hints(
    blade_scope: str | None,
    blade_target: str | None,
    blade_action: str | None,
    parsed_flags: dict | None = None,
) -> str:
    """Generate verification hints based on fault metadata.

    Provides FACTUAL context (fault metadata, scenario, parameter observability)
    to help the LLM design verification. Does NOT provide operational advice or
    domain pitfalls — those come from knowledge files (read_knowledge_resource)
    and skill_case_content. Per-injection-method notes (kubectl_exec BusyBox,
    kubectl-native, host-native) are owned by the backend's
    :meth:`FaultProvider.verify_prompt_note` and inserted by the caller.

    The fault-type-specific fragments (parameter observability, dynamic partition
    hints, node-disk topology, event filtering) are owned by the
    ``VerificationProfile`` for ``blade_target``; this function only assembles
    the fault-agnostic scaffolding and inserts each profile slot at its former
    position.
    """
    hints = []

    # Node-level overlay filesystem hint
    if blade_scope == "node":
        # Host-level checks are done via kubectl_read(subcommand="debug").
        # Host paths inside the debug pod live under /host/...; the
        # verifier finalization scans message history and removes the
        # debug pod automatically (no manual cleanup required).
        pass

    # Fault metadata (factual context) — OR so partial metadata is still useful
    if blade_scope or blade_target or blade_action:
        known = []
        if blade_scope:
            known.append(f"Scope: {blade_scope}")
        if blade_target:
            known.append(f"Target: {blade_target}")
        if blade_action:
            known.append(f"Action: {blade_action}")
        hints.append(f"Fault metadata: {' | '.join(known)}")

        if blade_scope and blade_target and blade_action:
            scope_target_action = f"{blade_scope}-{blade_target} {blade_action}"
            hints.append(f"ChaosBlade scenario: {scope_target_action}")

    # Per-fault-type verification KNOWLEDGE is NOT emitted from code. It lives in
    # the data layer: the skill case (PRIMARY AUTHORITY, embedded by the caller)
    # and the knowledge docs (shared, channel-aware). Emit a per-target pointer
    # so the LLM loads the right doc on demand instead of us hardcoding it here.
    if blade_target:
        hints.append(
            f"For '{blade_target}' verification specifics (observation methods, "
            f"partition/overlay or protocol semantics, data-interpretation "
            f"pitfalls), load the relevant knowledge doc via `read_knowledge_resource` "
            f"— e.g. `fault-verification-strategies.md`."
        )
    else:
        hints.append(
            "For domain-specific verification patterns and data-interpretation "
            "pitfalls, use `read_knowledge_resource` to load the relevant knowledge doc."
        )

    # Evidence sufficiency is YOUR judgment, not a fixed metric checklist. If the
    # primary evidence is a terminal / unreachable state (e.g. node NotReady with
    # kubelet no longer posting status), that alone can be conclusive — submit the
    # verdict. Do NOT probe INTO an unreachable target to manufacture an extra
    # "cross metric": that call just times out and wastes the loop. If an
    # independent cross-check is genuinely useful, run it from a HEALTHY peer
    # (e.g. probe the target's port from a Pod on another node).
    hints.append(
        "Evidence sufficiency is your holistic judgment. A terminal/unreachable "
        "primary state can be conclusive on its own — do not probe into an "
        "unreachable target to collect an extra cross-metric (it only times out). "
        "If a cross-check helps, run it from a healthy peer."
    )

    return "\n".join(hints)
