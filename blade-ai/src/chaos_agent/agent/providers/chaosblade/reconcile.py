"""Create-reconcile judgment material for the ChaosBlade carrier.

Companion to the generic state machine in
``agent/nodes/execute/_reconcile_gate.py``
(blade-create-reconcile-before-retry D6). The gate splits in two halves:

- STATE MACHINE half (generic, ``_reconcile_gate.py``): the three-state
  scan of the most recent create ToolMessage, the interception judgment,
  the release conditions (whitelist read / completed probe), the block
  cap, and the fabricate-and-route wiring. Zero carrier vocabulary.
- JUDGMENT MATERIAL half (THIS module, carrier-side): which tool is the
  create (``reconcile_create_tool_names`` declared on the provider), how
  its tool_call args become the four-dimension request fingerprint, the
  interception-time cluster probe, and the feedback texts that name this
  carrier's reconciliation tools (blade_status / blade_query_k8s /
  kubectl_read) and cleanup handle (blade_destroy).

The generic half consults this material exclusively through the registry
seam (``FaultProviderRegistry.build_reconcile_fingerprint`` /
``reconcile_hold_feedback`` / ``reconcile_batch_held_feedback`` and the
attribute unions) — never by import (phase-11 carrier-import discipline).
The probe reuses the conflict query safety_check runs
(``check_blade_conflicts``) with a different consumption: safety_check
warns on someone else's conflict, the gate recognises OUR OWN
possibly-in-effect create (reuse the UID instead of re-creating).

Fabricated-text contract: the hold feedback is headed by
``GATE_RECONCILE_BLOCKED_MARKER`` (neutral ground, ``tools/markers.py``)
and the batch-held notice carries the "was NOT executed" wording (plus
``status="error"`` set by the generic caller) — the generic three-state
scan keys on exactly these shapes to recognise never-executed answers,
so they are load-bearing wording, not presentation.
"""

import logging

from chaos_agent.agent.nodes.side_effect._conflict_check import (
    check_blade_conflicts,
)
from chaos_agent.tools.markers import GATE_RECONCILE_BLOCKED_MARKER
from chaos_agent.tools.request_identity import (
    RequestFingerprint,
    build_request_fingerprint,
)

logger = logging.getLogger(__name__)


def fingerprint_from_tool_call_args(args) -> RequestFingerprint:
    """Build the four-dimension fingerprint from blade_create tool_call args.

    The raw LLM-side argument shapes are not fixed: ``names`` arrives as a
    CSV string or a list, ``labels`` as a CSV string or a dict. Normalise
    all of that into the raw string forms ``check_blade_conflicts``
    consumes; order-insensitive equality is ``RequestFingerprint.matches``
    (sorted multiset), so a reordered labels/names retry still matches.
    Keys are the unified tool-schema face (scope/target/action — the
    ``blade_*`` legacy aliases are provider-layer vocabulary, confined
    there by the phase-10 key-face guard).

    scope/target/action are lowercased here (NOT inside the shared
    ``build_request_fingerprint``, which safety_check feeds with spec
    fields under a byte-identical regression anchor): ChaosBlade targets
    and actions are lowercase enums, and both registration and retry
    fingerprints go through THIS function, so the gate compares like with
    like while the shared construction stays untouched.
    """
    if not isinstance(args, dict):
        args = {}
    scope = _as_str(args.get("scope")).lower()
    target = _as_str(args.get("target")).lower()
    action = _as_str(args.get("action")).lower()
    return build_request_fingerprint(
        namespace=_as_str(args.get("namespace")),
        labels=_labels_to_csv(args.get("labels")),
        names=_names_to_csv(args.get("names")),
        scope=scope,
        target=target,
        action=action,
    )


# ---------------------------------------------------------------------------
# Interception-time probe: query the cluster for the registered request
# ---------------------------------------------------------------------------


def _is_host_scope_fingerprint(fp: RequestFingerprint) -> bool:
    """True when the registered request targets a bare host.

    Host-scope experiments record on the HOST's local blade DB — a
    cluster-CRD conflict query cannot see them (guaranteed miss, invalid
    conclusion), so the gate must not probe for them.
    """
    sta = fp.scope_target_action or ""
    scope = sta.split("-", 1)[0] if sta else ""
    if not scope:
        return False
    from chaos_agent.agent.spec.fault_registry import is_host_scope

    return is_host_scope(scope)


def _format_probe_hits(hits: list) -> str:
    """Probe result when active same-fingerprint experiments exist.

    Reuse-or-cleanup with the attribution judgement left to the LLM (the
    gate does NOT decide whether the found experiment IS the uncertain
    create — creation-time-vs-set-time comparison is the LLM's call).
    """
    lines = ["Probe result: ACTIVE experiment(s) match the registered request:"]
    for info in hits:
        lines.append(f"  - UID {info.uid} (same action, overlapping target)")
    lines.append(
        "Decide which applies:\n"
        "  a. This IS your uncertain create already in effect → REUSE UID "
        "above: do NOT create again; proceed with that UID (it is your "
        "experiment handle — verify/destroy as needed).\n"
        "  b. It is NOT yours (pre-existing) → destroy it first "
        "(blade_destroy <uid>), then re-issue the create."
    )
    return "\n".join(lines)


_PROBE_MISS_TEXT = (
    "Probe result: NO active experiment matches the registered request "
    "(the uncertain create did not materialise, or has already ended). "
    "You may re-issue the blade_create — the gate releases it."
)

_PROBE_FAILED_TEXT = (
    "Probe result: the automatic cluster-side check FAILED (query error "
    "or cluster unreachable) — no conclusion either way. Reconcile "
    "manually: blade_status / blade_query_k8s / kubectl_read for the "
    "request above, then re-issue."
)

_HOST_DEGRADE_TEXT = (
    "Probe result: not probed — the registered request is HOST-scope. "
    "Host experiments record on the HOST's local blade DB, not in "
    "cluster CRDs, so a cluster-side probe cannot see them. Verify on "
    "the host side (blade status through the host channel), then "
    "re-issue."
)


async def probe_registered_request(
    fp: RequestFingerprint, kubeconfig: str, task_id: str,
) -> tuple[str, bool]:
    """Interception-time reconciliation probe: query the cluster for the
    registered request (host scope excluded — see below) and return
    ``(feedback_section, counts_as_reconciliation)``.

    Hit (same action + overlapping target): feedback carries the UID(s)
    with the reuse-or-cleanup options, attribution left to the LLM; the
    probe counts as reconciliation. Miss: feedback confirms absence;
    counts as reconciliation. Failure/unreachable: degraded manual-guidance
    text; does NOT count (the next retry still judges by whitelist/cap).

    Host scope: host experiments live on the HOST's local blade DB, not
    in cluster CRDs — the CRD query is guaranteed to miss and its
    conclusion would be invalid, so the probe is skipped entirely and the
    feedback degrades to host-side verification guidance.
    """
    if _is_host_scope_fingerprint(fp):
        return _HOST_DEGRADE_TEXT, False

    # Reachability gate mirrors safety_check's (_cluster_reachable): an
    # unreachable cluster turns the probe into a guaranteed failure —
    # skip the doomed call and degrade to manual guidance.
    if not kubeconfig:
        from chaos_agent.transports import is_kubewiz_channel

        if not is_kubewiz_channel():
            return _PROBE_FAILED_TEXT, False

    try:
        _uids, details = await check_blade_conflicts(
            kubeconfig, task_id, **fp.as_query_kwargs(),
        )
    except Exception:
        # A probe failure must never break or block the gate itself.
        logger.warning(
            "create_reconcile gate: probe query failed — degrading to "
            "manual reconciliation guidance",
            exc_info=True,
        )
        return _PROBE_FAILED_TEXT, False
    hits = [
        info for info in (details or [])
        if info.same_action_as_request and info.overlaps_target
    ]
    if hits:
        return _format_probe_hits(hits), True
    return _PROBE_MISS_TEXT, True


# ---------------------------------------------------------------------------
# Fabricated feedback texts
# ---------------------------------------------------------------------------


def format_gate_feedback(
    fp: RequestFingerprint, new_count: int, block_limit: int,
    probe_section: str = "",
) -> str:
    """GuardFeedback-shaped interception body for the held blade_create.

    Reason / fix pairing with is_hard_floor=False (reconcile-first, not a
    ban), the registered fingerprint for the LLM to query against, and
    the release conditions. The GATE marker heads the text so the
    three-state scan recognises this as never-executed. ``probe_section``
    (when the gate probed the cluster) is spliced in before the fix so
    the LLM sees the probe's findings first — they usually decide the
    fix branch. ``block_limit`` is the generic gate's hold cap, threaded
    in by the caller so the release conditions stay one policy source.
    """
    dims = []
    if fp.namespace:
        dims.append(f"namespace={fp.namespace}")
    if fp.labels:
        dims.append(f"labels={fp.labels}")
    if fp.target_names:
        dims.append(f"names={fp.target_names}")
    if fp.scope_target_action:
        dims.append(f"action={fp.scope_target_action}")
    fp_text = ", ".join(dims) or "(dimensions unresolved)"
    body = (
        f"{GATE_RECONCILE_BLOCKED_MARKER} [create-reconcile] BLOCKED — this "
        f"blade_create was HELD, never sent to the cluster.\n"
        f"Reason: the previous blade_create for this same request returned "
        f"with an UNKNOWN outcome; a blind retry can create a DUPLICATE "
        f"experiment on the target.\n"
        f"Registered request: {fp_text}\n"
    )
    if probe_section:
        body += probe_section.rstrip("\n") + "\n"
    body += (
        f"Fix (is_hard_floor=False — reconcile first, then retry; this is "
        f"not a ban):\n"
        f"1. Reconcile: query whether an experiment matching the request "
        f"above is already active (blade_status / blade_query_k8s / "
        f"kubectl_read). If one is, REUSE its UID (do not create again) or "
        f"destroy it first (blade_destroy <uid>).\n"
        f"2. Having reconciled, re-issue this same blade_create — the gate "
        f"releases it. (The gate holds at most {block_limit} "
        f"un-reconciled retries per cycle; this is hold #{new_count}.)"
    )
    return body


def format_batch_held_feedback(tool_name: str) -> str:
    """Interception body for the batch's OTHER calls (all held together)."""
    return (
        f"Error: tool call `{tool_name or 'unknown'}` was NOT executed — a "
        f"blade_create in this same batch was held by the create-reconcile "
        f"gate (result-uncertain retry protection), so the whole batch was "
        f"held back. Nothing ran and no state changed. Reconcile first "
        f"(blade_status / blade_query_k8s / kubectl_read), then re-issue."
    )


# ---------------------------------------------------------------------------
# Raw argument-shape normalisers
# ---------------------------------------------------------------------------


def _as_str(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _names_to_csv(value) -> str:
    """``names`` arrives as a CSV string OR a list depending on the caller."""
    if isinstance(value, (list, tuple)):
        return ",".join(str(n) for n in value if n)
    return _as_str(value)


def _labels_to_csv(value) -> str:
    """``labels`` arrives as a CSV string OR a dict depending on the caller."""
    if isinstance(value, dict):
        return ",".join(f"{k}={v}" for k, v in value.items())
    return _as_str(value)
