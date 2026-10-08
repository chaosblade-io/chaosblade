"""Structured side-effect artifacts discovered from executed tool results.

The current graph remains message-driven.  This module adds a small durable
index over those messages so safety and recovery code do not have to infer a
debug pod's identity from prose every time.  It intentionally records facts
only after a ToolMessage exists; an LLM-proposed tool call is never an artifact.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from copy import deepcopy
from typing import Any, Callable

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.target_guard.classifier import (
    KIND_ALIASES,
    canonicalise_kind,
    is_cluster_scoped_kind,
)
from chaos_agent.agent.target_guard.types import SCOPE_ESCAPE
from chaos_agent.agent.tool_verdicts import message_result_failed

logger = logging.getLogger(__name__)

# Debug pods are created with a bounded lifetime (entrypoint ``-- sleep 3600``),
# so cleanup is fire-and-forget: attempt each delete exactly once and mark the
# artifact ``cleaned`` regardless of outcome. A delete that does not land under
# an in-progress network fault is intentionally NOT retried — the pod's own
# ``sleep`` bound lets it lapse on its own, and retrying would ride the very API
# path the fault is severing (slow, unbounded teardown).
#
# Debug-pod deletes are independent and idempotent, so a whole-zone fan-out
# (dozens of node-debugger pods) is cleaned concurrently rather than one-at-a
# -time — bounded so we never open an unbounded burst of API/transport calls.
# Serial cleanup made an AZ-partition drill's teardown take minutes — task-76c59364.
_CLEANUP_CONCURRENCY = 10


_DEBUG_META_RE = re.compile(r"\[debug-pod-meta:\s*(\{.*?\})\]")

# Drill occupancy vehicle registration emitted by skill scripts (e.g.
# inject_cni_exhaust.py): a machine-readable line naming the transient
# workload the script created so the task can exempt it from drift and
# guarantee cleanup. Consumed by this module only — it never lands in the
# cluster, so the drill stays indistinguishable from a real incident.
_DRILL_VEHICLE_RE = re.compile(r"\[drill-vehicle:\s*(\{.*?\})\]")

# Artifact types that count as injection VEHICLES (task-owned machinery,
# never fault targets): debug pods plus the two drill-occupancy forms
# (a behaviourless occupant Pod holding a resource; an exhauster Deployment
# created by a curated skill script) and the recovery-carrier Pod (the
# self-built timer host for API-plane fault rollback — its sa/role/
# rolebinding siblings are recorded on the artifact as ``rbac_family``).
VEHICLE_ARTIFACT_TYPES: frozenset[str] = frozenset({
    "debug_pod", "occupant_pod", "occupant_deployment", "recovery_carrier",
})

# Recovery-bearing carriers: the vehicle subset whose lifecycle actually
# underwrites a fault's reversal. ``debug_pod`` is a PROBE channel — its
# lifecycle says nothing about whether the fault was reversed — so it is
# deliberately excluded. Single source for BOTH the injection-time
# underwriter check (nodes.planning.tool_screener) and the clearance-time
# liability predicate (persistence.task_store.may_carry_live_fault), so the
# two can never drift on "which artifact types are recovery carriers".
RECOVERY_CARRIER_TYPES: frozenset[str] = VEHICLE_ARTIFACT_TYPES - {"debug_pod"}


# ---------------------------------------------------------------------------
# Recovery-clock anchors (W-67-15 / W-67-16)
# ---------------------------------------------------------------------------
#
# A recovery carrier's countdown is described by FIVE distinct moments that
# live on FIVE different clocks, and until now each sat in its own field (or
# in no field at all), so nothing in the record ever put two of them side by
# side:
#
#   confirmed_live     the carrier pod was read back as ready  (framework clock)
#   framework_commit   the framework processed the arming exec (framework clock)
#   host_armed         systemd actually accepted the transient unit (HOST clock)
#   recovery_deadline  framework_commit + the parsed window    (framework clock)
#   host_fire          the unit actually ran                   (HOST clock)
#
# The two clocks are not the same and the gap is not bounded by anything the
# framework controls: between commit and host_armed sit the transport
# round-trip, host-side queueing, and the ``&&``-chain's first half. Case #66
# measured that gap at 22s, case #67 at 51s (deadline vs. fire) with an 85s
# commit-vs-armed gap — and BOTH numbers were produced by hand, out of band,
# by cross-reading the task JSON against the host journal. Nothing in the
# record showed the drift, so nothing could regress on it.
#
# The anchors are therefore laid out side by side, each tagged with WHERE the
# number came from. ``host_armed`` and ``host_fire`` are ``unobserved`` today:
# systemd-run reports ``Running as unit: ...`` on stderr and the success path
# drops stderr, so the host clock is genuinely not on record. Recording that
# absence explicitly is the point — a slot that says "unobserved" is a standing,
# machine-readable statement of what this framework cannot currently prove,
# and it is the slot the stderr-receipt work fills in without any further
# change to the schema or to a consumer.

#: The number came from the framework's own wall clock at the moment it
#: processed a tool call. Usable for conservative estimates ("at least N
#: seconds remain"), NOT usable as evidence that anything happened on the host.
PROVENANCE_FRAMEWORK = "framework_bookkeeping"
#: The number was read back from the host (a unit receipt, a journal line).
#: This is the only provenance that can serve as evidence of a host-side event.
PROVENANCE_HOST = "host_observed"
#: No channel currently records this moment. Distinct from "0" / "missing":
#: an unobserved anchor is a known gap, not an absent value.
PROVENANCE_UNOBSERVED = "unobserved"

#: The five anchors in causal order. Single source for both the writer
#: (:func:`recovery_time_anchors`) and every reader, so a new anchor cannot
#: appear in one view and not the other.
RECOVERY_TIME_ANCHOR_SLOTS: tuple[str, ...] = (
    "confirmed_live",
    "framework_commit",
    "host_armed",
    "recovery_deadline",
    "host_fire",
)

#: Consecutive anchor pairs whose gap is worth persisting. Only pairs whose
#: BOTH ends are observed yield a number (:func:`recovery_anchor_offsets`);
#: the rest are reported as gaps. ``commit->host_armed`` and
#: ``deadline->host_fire`` are the two the #66/#67 audits measured by hand.
RECOVERY_ANCHOR_OFFSET_PAIRS: tuple[tuple[str, str], ...] = (
    ("confirmed_live", "framework_commit"),
    ("framework_commit", "host_armed"),
    ("host_armed", "recovery_deadline"),
    ("recovery_deadline", "host_fire"),
)


def _epoch(value: Any) -> float | None:
    """Coerce a stored timestamp to ``float``, or ``None`` when there is none.

    ``bool`` is excluded explicitly: it is an ``int`` subclass, so a corrupted
    ``True`` would otherwise read as epoch 1 and anchor a timeline in 1970.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def recovery_time_anchors(artifact: dict | None) -> dict:
    """The five recovery-clock anchors of one carrier, side by side.

    Pure derivation from the artifact's own authoritative fields — it stores
    nothing and cannot drift from them. Every slot is ALWAYS present, so a
    consumer can iterate the schema without first probing which fields this
    particular carrier happens to carry, and so an unobserved anchor is
    indistinguishable-by-shape from an observed one only in its ``epoch``
    (``None``), never in its ``provenance``.

    Returns an all-unobserved table for a non-dict / empty artifact rather than
    ``{}``: "this carrier has no recovery clock on record" is a fact worth
    rendering, an empty dict is not.
    """
    art = artifact if isinstance(artifact, dict) else {}

    def _slot(value: Any, provenance: str) -> dict:
        epoch = _epoch(value)
        if epoch is None:
            return {"epoch": None, "provenance": PROVENANCE_UNOBSERVED}
        return {"epoch": epoch, "provenance": provenance}

    return {
        # Stamped once, when the pod first registered as active (see the
        # freshness note in ``collect_execution_artifacts``). A framework read
        # of the API server — real evidence about the POD, still framework-
        # clocked, and absent for carriers that never went through the
        # debug-pod registration path.
        "confirmed_live": _slot(
            art.get("confirmed_live_epoch"), PROVENANCE_FRAMEWORK,
        ),
        "framework_commit": _slot(
            art.get("recovery_armed_commit_epoch"), PROVENANCE_FRAMEWORK,
        ),
        # Populated only by a host receipt. No writer exists yet — see the
        # block comment above. The slot is declared so the absence is part of
        # the schema instead of a hole the reader has to know about.
        "host_armed": _slot(art.get("host_armed_epoch"), PROVENANCE_HOST),
        "recovery_deadline": _slot(
            art.get("recovery_deadline_epoch"), PROVENANCE_FRAMEWORK,
        ),
        "host_fire": _slot(art.get("host_fire_epoch"), PROVENANCE_HOST),
    }


def recovery_anchor_offsets(anchors: dict | None) -> dict:
    """Gap between each consecutive anchor pair, plus the gaps we cannot see.

    This is the reconciliation the #66/#67 audits had to do by hand. Split in
    two on purpose: ``seconds`` holds only pairs whose BOTH ends are observed
    (a number computed from one observed end and one framework-clock end would
    look like a measurement while being a guess), ``unobserved`` names every
    pair that is missing an end and which end is missing. A reader — human or
    regression test — therefore sees the drift AND the reason the drift is
    partly unknown, from the record alone.
    """
    table = anchors if isinstance(anchors, dict) else {}
    seconds: dict[str, float] = {}
    unobserved: dict[str, list[str]] = {}
    for first, second in RECOVERY_ANCHOR_OFFSET_PAIRS:
        label = f"{first}_to_{second}"
        a = table.get(first) if isinstance(table.get(first), dict) else {}
        b = table.get(second) if isinstance(table.get(second), dict) else {}
        start, end = _epoch(a.get("epoch")), _epoch(b.get("epoch"))
        if start is not None and end is not None:
            seconds[label] = round(end - start, 3)
            continue
        missing = [
            name
            for name, value in ((first, start), (second, end))
            if value is None
        ]
        unobserved[label] = missing
    return {"seconds": seconds, "unobserved": unobserved}


def parse_debug_pod_metadata(content: str) -> dict:
    """Parse the structured marker emitted by ``tools.kubectl_cli``."""
    if not isinstance(content, str):
        return {}
    match = _DEBUG_META_RE.search(content)
    if not match:
        return {}
    try:
        value = json.loads(match.group(1))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def debug_meta_scope(metadata: dict) -> str:
    """Classify a ``[debug-pod-meta]`` payload: ``"pod"`` or ``"node"``.

    Single semantic authority for debug-meta payloads produced by
    ``tools.kubectl_cli``. A payload carrying ``ephemeral_container`` is a
    POD-scoped ``kubectl debug <pod> --target=<c>``: the attached ephemeral
    container runs INSIDE the target pod, so ``name`` is the USER'S
    workload — not a tool-created vehicle, and no standalone debug pod
    exists to register or clean up. Every other payload is a node-scoped
    debug: a standalone ``node-debugger-*`` pod that IS task-owned
    machinery. An empty or invalid payload returns ``""``.

    Consumers must ask HERE instead of re-deriving the rule locally: the
    three sibling guards (artifact collection, cleanup extraction, vehicle
    screening) each grew their own ``ephemeral_container`` check at
    different times — two were fixed under Task-5193538b while the third
    drifted and misread the fault TARGET as a vehicle in a live run
    (self-contradictory VEHICLE WARNING naming the anchor).
    """
    if not isinstance(metadata, dict) or not metadata:
        return ""
    return "pod" if metadata.get("ephemeral_container") else "node"


def parse_drill_vehicle_markers(content: str) -> list[dict]:
    """Parse ``[drill-vehicle: {...}]`` registration lines from script output.

    A skill script that creates a transient occupancy workload emits one line
    per created resource (``{"kind": "deployment", "name": ..., "namespace":
    ...}``). Returns the parsed dicts (empty list when none / malformed) so
    ``collect_execution_artifacts`` can register them as vehicle artifacts —
    which is what makes their later delete drift-exempt and their cleanup
    guaranteed even when the task dies before recovery runs.
    """
    if not isinstance(content, str):
        return []
    vehicles: list[dict] = []
    for match in _DRILL_VEHICLE_RE.finditer(content):
        try:
            value = json.loads(match.group(1))
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("name"):
            vehicles.append(value)
    return vehicles


def vehicle_artifact_types(name: str, state: dict | None) -> frozenset[str]:
    """The vehicle artifact TYPES registered under ``name`` (empty = none).

    Finer-grained than :func:`is_vehicle_name`: the screener's drift
    exemption must match the vehicle's KIND to the operation's scope (a
    registered occupant Deployment exempts ``delete deployment``; it must not
    exempt a same-named pod operation, and debug pods never exempt deployment
    operations).
    """
    if not name or not isinstance(state, dict):
        return frozenset()
    return frozenset(
        str(artifact.get("type"))
        for artifact in state.get("execution_artifacts") or []
        if isinstance(artifact, dict)
        and artifact.get("type") in VEHICLE_ARTIFACT_TYPES
        and artifact.get("name") == name
    )


def is_vehicle_name(name: str, state: dict | None) -> bool:
    """True if ``name`` is a transient injection vehicle, not a fault target.

    Task-29848471: a k3-class replan once quoted the ``kubectl debug`` pod
    name as the fault target and the verifier validated against the vehicle.
    Data sources first, naming-convention heuristic last:

      1. ``execution_artifacts`` — any registered vehicle artifact name
         (``debug_pod``, ``occupant_pod``, ``occupant_deployment``; durable
         facts that survive message trimming).
      2. ``kubectl_exec_pod_name`` — the tool pod used for exec-injection.
      3. ``debug-pod-meta`` tags in message history (covers artifacts not yet
         collected this iteration). A POD-scoped debug meta is skipped —
         its ``name`` is the TARGET pod, the user's workload (see
         :func:`debug_meta_scope`).
      4. Heuristic: the ``node-debugger-`` creation prefix.
    """
    if not name or not isinstance(state, dict):
        return bool(name) and str(name).startswith("node-debugger-")
    for artifact in state.get("execution_artifacts") or []:
        if (
            isinstance(artifact, dict)
            and artifact.get("type") in VEHICLE_ARTIFACT_TYPES
            and artifact.get("name") == name
        ):
            return True
    if state.get("kubectl_exec_pod_name") == name:
        return True
    for message in state.get("messages") or []:
        content = getattr(message, "content", None)
        if not isinstance(content, str) or "debug-pod-meta" not in content:
            continue
        metadata = parse_debug_pod_metadata(content)
        if debug_meta_scope(metadata) == "pod":
            # Pod-scoped debug: ``name`` is the TARGET pod (the user's
            # workload), not a vehicle. Without this skip the anchor itself
            # reads as a vehicle and the verifier's VEHICLE WARNING turns
            # self-contradictory ("verify ONLY X … X is NOT a fault target").
            continue
        if metadata.get("name") == name:
            return True
    from chaos_agent.agent.nodes.execute._debug_pod import DEBUG_POD_NAME_PREFIX
    return str(name).startswith(DEBUG_POD_NAME_PREFIX)


def is_vehicle_teardown_delete(
    effective: Any, artifacts: list, *, v_args: str = "",
) -> bool:
    """True when a delete's every (kind, namespace, name) hits a registered vehicle.

    Shared teardown judgement (R6-1, extracted from the screener's
    carrier-gate exemption so the issue-time attribution layer answers the
    SAME question): ``effective`` is the classifier's view of a ``kubectl
    delete`` (scope = kind, namespace, names — duck-typed; both consumers
    classify with the same ``infer_effective_target``), ``artifacts`` the
    task's vehicle registrations. A delete naming only registered vehicle
    assets — the carrier/occupant main asset (kind: the RECORDED ``kind``
    field since P5, falling back to the ``*_deployment`` suffix convention
    for artifacts persisted before the field existed) or an attached
    ``rbac_family`` member — removes machinery this task built, never a
    fault target; status is irrelevant (a ``cleaned`` registration still
    names assets whose idempotent ``--ignore-not-found`` replay is
    cleanup). Nameless (label-selector) deletes are not asset-targeted;
    one unregistered name in the batch poisons it.

    Batch names (G-4/R20): kubectl legally accepts ``pod a,b``,
    ``pod/a,pod/b`` and ``pod a b``, but the shared classifier keeps a
    comma-joined list as ONE name (its tuple is the drift check's single
    anchor — changing its granularity would move every drift rejection),
    so callers holding the raw command pass ``v_args`` and the names are
    expanded HERE, single-sourced, per the poison law above. Every name
    is matched against registrations of ITS OWN kind — a mixed-kind
    batch (``pod/carrier,role/carrier`` — the §6 four-way sweep in one
    call) is teardown iff every member is registered, regardless of
    which kind the classifier anchored the scope on. Namespace matching
    follows the kind's TOPOLOGY (:func:`_ns_matches`): a cluster-scoped
    member has no namespace, so the dimension is ignored on both sides
    (a pre-R21 legacy registration under the carrier ns still matches).
    """
    kind = canonicalise_kind(getattr(effective, "scope", "") or "")
    namespace = getattr(effective, "namespace", "") or ""
    pairs = _expanded_delete_name_pairs(
        v_args, getattr(effective, "names", None) or (),
    )
    targets = {
        (name, canonicalise_kind(seg_kind) if seg_kind else kind)
        for name, seg_kind in pairs
    }
    if not targets:
        return False
    registered: set[tuple[str, str, str]] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        if artifact.get("type") not in VEHICLE_ARTIFACT_TYPES:
            continue
        # P5: the RECORDED kind field is authoritative; the suffix
        # convention survives only as the hydration fallback for artifacts
        # persisted before the field existed (never rewritten in place —
        # a merge pass only fills EMPTY fields).
        recorded_kind = str(artifact.get("kind") or "").strip().lower()
        main_kind = recorded_kind or (
            "deployment"
            if str(artifact.get("type") or "").endswith("_deployment")
            else "pod"
        )
        # Registered under its OWN canonical kind (not the call's scope
        # kind) — a mixed-kind batch matches each member where it lives.
        if artifact.get("name"):
            registered.add((
                str(artifact["name"]),
                canonicalise_kind(main_kind),
                str(artifact.get("namespace") or ""),
            ))
        for member in artifact.get("rbac_family") or []:
            if isinstance(member, dict) and member.get("name"):
                registered.add((
                    str(member["name"]),
                    canonicalise_kind(str(member.get("kind") or "")),
                    str(member.get("namespace") or ""),
                ))
    return all(
        any(
            reg_name == name
            and reg_kind == target_kind
            and _ns_matches(target_kind, reg_ns, namespace)
            for reg_name, reg_kind, reg_ns in registered
        )
        for name, target_kind in targets
    )


def _ns_matches(kind: str, registered_ns: str, effective_ns: str) -> bool:
    """Single-source namespace match under the kind's topology (R21/G-5).

    A cluster-scoped object lives in NO namespace — the ``-n`` on its
    commands is noise kubectl silently ignores, so neither side's ns
    presence may discriminate: a registration with the true topology
    (""), a legacy pre-R21 registration (carrier-ns fallback), and a
    command with or without ``-n`` all match. A namespaced object keeps
    strict equality (both sides empty-normalised).
    """
    if is_cluster_scoped_kind(kind):
        return True
    return (registered_ns or "") == (effective_ns or "")


def issue_call_is_registered_teardown(
    tool_name: str, tool_args: Any, artifacts: list,
) -> bool:
    """True when a freshly-issued call is registered-vehicle MACHINERY.

    Call-level machinery matcher (R6-1's issue-time twin, single-sourced here
    since P3; generalized to the machinery≠mutation domain in R22/G-6 and
    R23/G-7): the exemption asks whether a call operates on THIS TASK'S OWN
    recovery machinery, never on a fault target — four faces over the
    operation space (CHANNEL × VERB × ASSET × DOMAIN), one principle:

    - DEMOLITION face (kubectl × delete, R6-1): a delete whose every
      (kind, namespace, name) resolves to a task-registered vehicle asset
      (judged by :func:`is_vehicle_teardown_delete`, classified with the
      same ``infer_effective_target`` the screener uses) removes machinery
      this task built — §6's four-way carrier cleanup, the idempotent
      replay of a partial sweep, the drill-target recovery delete.
    - CHANNEL face (kubectl × exec, R22/G-6): an exec whose target is
      the REGISTERED recovery carrier (judged by
      :func:`is_registered_channel_exec`) is machinery maintenance — §3's
      SA-token verify probe and §4's timer arm/re-arm travel exactly this
      shape, and both are judged MUTATING by the fail-safe vocabulary
      (the arm payload carries ``curl -X PATCH``; the verify payload's
      ``$(cat ...)`` substitution is un-analysable, so readonly fails
      closed on it). Without this face the carrier's own standard
      playbook was attributed as ``kubectl_native`` injection evidence:
      a re-arm beside a live experiment mis-marked ``combo_native_issued``
      (recovery permanently re-routed to the LLM path), a verify exec
      pre-empted the first-native attribution slot, and a state-less
      restored session read a re-arm as "native takeover".
    - BLADE-DEMOLITION face (kubectl × exec × blade destroy/revoke,
      R25/G-8, judged by :func:`_is_blade_demolition_exec`): an exec
      delivering a PURE blade destroy/revoke — the tool-pod demolition
      the kubelet-stall case prescribes as PREFERRED recovery and the
      re-arm protocol's teardown half. Registration-INDEPENDENT by
      design: the delivery targets the CLUSTER's tool pod, which the
      artifact registry never tracks, and the inline-blade classifier
      route eats the pod name (``names=()``) so the CHANNEL face has
      structurally nothing to compare. Without this face the destroy
      half of a re-arm ran the R22 harm chain verbatim: combo mis-mark
      beside a live experiment, replan "attempt" evidence, and
      recency/mutation-index scan pollution.
    - HOST face (host_inject × timer-arm, R23/G-7): a host command in
      its systemd-run timer form (judged by
      :func:`host_call_is_registered_recovery`, form primitive shared
      with ToolGuard's admission rule) is the host twin of the carrier
      arm — every host skill 降级方案 prescribes "先武装定时恢复，再
      注入", and the payload runs at the DEADLINE, never at issue time.
      Without this face the arm-first order pre-empted the host_native
      slot (a failed injection after a successful arm reported the task
      as injected), mis-marked combo on blade+timer pairs, and read as
      native takeover in a restored session (the P3 seam was severed on
      the host side). R25/G-8 extends the same face to the DIRECT
      ``blade destroy/revoke`` argv — the host twin of the
      BLADE-DEMOLITION face above.

    Machinery is not a fault mutation, so mutation-evidence consumers must
    neither commit it as ``injection_method`` nor weigh it as
    combo/confirmation/step-credit evidence. Face anchors are deliberate:
    the kubectl DELETE face is REGISTRY-matched (a delete of an UNREGISTERED
    object — the delete-pod-to-restart fault form — is a real native
    mutation), while the two kubectl EXEC faces split by PAYLOAD SYNTAX
    (the CHANNEL face keeps its registry match for target identity;
    the BLADE-DEMOLITION face rides the verb×domain syntax anchor,
    tool-pod deliveries being registry-external) and the host face is
    FORM-matched (the host channel has
    no artifact registry: a timer unit is never created through a
    registerable tool call, so ToolGuard's admission-only-in-timer-form
    rule IS the machinery verdict's single source).
    """
    if not isinstance(tool_args, dict):
        return False
    if tool_name != "kubectl":
        return host_call_is_registered_recovery(tool_name, tool_args)
    subcommand = tool_args.get("subcommand")
    if subcommand not in ("delete", "exec"):
        return False
    from chaos_agent.agent.target_guard.classifier import infer_effective_target

    effective = infer_effective_target(tool_name, tool_args)
    if subcommand == "exec":
        if _is_blade_demolition_exec(effective, tool_args):
            return True
        return is_registered_channel_exec(effective, artifacts)
    return is_vehicle_teardown_delete(
        effective, artifacts,
        v_args=str(tool_args.get("v_args") or ""),
    )


def _is_host_blade_demolition(argv: list[str]) -> bool:
    """True when a host-channel argv is a DIRECT blade destroy/revoke.

    The host twin of the BLADE-DEMOLITION face (R25/G-8): the host shell
    has no artifact registry and no classifier flag, so the twin judges
    the DIRECT argv form ONLY — ``blade destroy <uid>`` / ``blade revoke
    <uid>`` (``exec_host_command``'s binary+args shape lands here
    verbatim; ``host_inject``'s raw command shlex-splits to it). No
    wrapper expansion (``sh -c 'blade destroy …; …'``): a wrapped
    compound stays ATTRIBUTED, the conservative direction — the k8s face
    withholds on the classifier's ``fault_binary_mutation`` flag, this
    face has no such flag, so it withholds by refusing depth. The verb
    pair is the blade CLI's demolition vocabulary, the same
    ``{"destroy", "revoke"}`` ``verify.classify_blade_exec_payload``
    legislates (single source; the argv form reaches it without the
    registry seam because the JUDGEMENT here is form, not syntax).
    """
    if len(argv) < 2:
        return False
    head = argv[0].rsplit("/", 1)[-1]
    return head == "blade" and argv[1] in ("destroy", "revoke")


def host_call_is_registered_recovery(
    tool_name: str, tool_args: dict,
) -> bool:
    """True when a host-channel call is recovery MACHINERY (R23/G-7).

    The HOST face of the machinery exemption. Boundaries, all deliberate:

    - TIMER FORM OR DIRECT DEMOLITION, single-sourced. ``systemd-run``
      reaches this layer only through ToolGuard, which admits it ONLY as
      a self-recovery timer (:func:`chaos_agent.tools.guard.is_systemd_run_timer`
      — the SAME primitive the guard's ``_check_systemd_run`` admits on;
      never re-derived here). An admitted systemd-run call is therefore
      a timer registration by construction, and every other host inject
      command (``kill -STOP``, ``iptables``, ``dd`` — the fault binaries)
      stays attributed. R25/G-8 adds the DIRECT ``blade destroy/revoke``
      argv (judged by :func:`_is_host_blade_demolition`) — the host
      twin of the kubectl BLADE-DEMOLITION face: the host shell's
      ``issue_time_method`` has no verb filter on inject tools, so the
      same demolition delivery was mis-attributed ``host_native`` there.
    - INJECT CARRIERS ONLY. The tool-name domain is the provider's own
      ``inject_tool_names`` (``host_inject`` / ``exec_host_command`` /
      ``shell``), single-sourced through the declaration seam
      (``HOST_INJECT_TOOL_NAMES`` — the same constant the provider's
      class attribute reads; phase-11 carrier-import boundary keeps the
      generic layer off the provider class) — a ``host_read``
      diagnostic is the readonly layer's domain, never the
      attributor's.
    - ARGS SHAPES. ``host_inject`` carries ``command`` (a raw string);
      ``exec_host_command`` carries ``binary`` + ``args`` (an argv list).
      Both resolve to an argv and meet the same timer verdict — the same
      two-shape handling the readonly layer's
      ``_host_native_call_is_readonly`` uses.

    Deliberate blind spot (same family as the R22 carrier-payload one):
    the payload behind ``--on-active`` is statically ambiguous (a recovery
    inverse vs a delayed fault); the guard's payload readmission narrows
    but does not eliminate it, and the exemption trusts the skill's
    arm-first discipline, not the payload's semantics.
    """
    from chaos_agent.agent.providers.host_shell.declaration import (
        HOST_INJECT_TOOL_NAMES,
    )

    if tool_name not in HOST_INJECT_TOOL_NAMES:
        return False
    command = tool_args.get("command")
    if isinstance(command, str) and command.strip():
        try:
            argv = shlex.split(command)
        except ValueError:
            # Unparseable payload: not provably a timer — fail toward
            # attribution (the conservative direction for the exemption).
            return False
    else:
        binary = tool_args.get("binary")
        extra = tool_args.get("args") or []
        if not isinstance(binary, str) or not binary:
            return False
        argv = [binary] + [str(a) for a in extra]
    from chaos_agent.tools.guard import is_systemd_run_timer

    if _is_host_blade_demolition(argv):
        return True
    return is_systemd_run_timer(argv)


def is_registered_channel_exec(effective: Any, artifacts: list) -> bool:
    """True when an exec's target is a REGISTERED recovery carrier (R22/G-6).

    The channel face of the machinery exemption. Boundaries are deliberate;
    the two guard layers are SAME-FAMILY but NOT mirrored domains (the
    drift layer's ``is_vehicle_exec`` exempts identity review over the
    WHOLE vehicle universe — ``is_vehicle_name`` registrations plus
    cluster-discovered tool pods, occupant deployments included — while
    this attribution face exempts only the recovery carrier under strict
    ns; only the FAULT-BINARY WITHHOLD below is a true mirror, one flag,
    one question across both layers):

    - RECOVERY CARRIERS ONLY. A ``debug_pod`` is the native-takeover
      channel (``exec <debug-pod> -- tc qdisc add ...`` is a real
      injection), and an occupant pod has no exec channel in the
      standard — both stay attributed. The screener registered the
      carrier with the stated purpose "makes later execs into the
      carrier (token probe / timer arm / re-arm) vehicle-exempt"; the
      drift layer honoured that promise, and until R22 the attribution
      layer never did.
    - FAULT-BINARY WITHHOLD. ``effective.fault_binary_mutation`` must be
      False — the SAME flag the drift layer withholds its exemption on
      (a static classifier cannot rule out a hostNetwork carrier, so
      ``stress-ng``/``tc``/``iptables`` inside the carrier keeps
      identity review — see ``_classify_kubectl_exec``).
    - NAMESPACE STRICTNESS. The carrier pod is namespaced, so the
      command's ns must equal the registration's (topology rule via
      :func:`_ns_matches`, R21/G-5 single source).

    The exec classifier puts the target pod name in ``effective.names``
    (first positional — flags and ``-n`` values already skipped, the
    ``--`` payload excluded) and keeps a ``kind/name`` slash prefix
    intact; the name is un-prefixed here (``rpartition`` on the single
    target token — RIGHT partition, so a bare name with no slash yields
    itself while ``pod/name`` yields ``name``; the R20 batch primitive
    is comma-shaped and not reused for the single-name exec shape).
    """
    if getattr(effective, "fault_binary_mutation", False):
        return False
    names = tuple(getattr(effective, "names", None) or ())
    if not names:
        # READONLY / UNKNOWN shapes carry no target name — nothing to
        # match (and a readonly exec is outside the attribution domain
        # anyway: the vocabulary never judges it mutating).
        return False
    namespace = getattr(effective, "namespace", "") or ""
    matched = True
    for raw_name in names:
        _, _, name = str(raw_name).rpartition("/")
        matched = matched and any(
            isinstance(artifact, dict)
            and artifact.get("type") == "recovery_carrier"
            and str(artifact.get("name") or "") == name
            and _ns_matches(
                "pod", str(artifact.get("namespace") or ""), namespace,
            )
            for artifact in artifacts
        )
    return matched


def _is_blade_demolition_exec(effective: Any, tool_args: dict) -> bool:
    """True when an exec delivers a PURE blade destroy/revoke (R25/G-8).

    The BLADE-DEMOLITION face of the machinery exemption — deliberately
    registration-INDEPENDENT: the delivery targets the CLUSTER's tool pod
    (``kubectl exec <chaosblade-tool-pod> -- blade destroy <uid>``),
    cluster infrastructure the artifact registry never tracks (the
    kubelet-stall case's preferred recovery; the re-arm protocol's
    teardown half). The CHANNEL face cannot legislate this shape: the
    inline-blade classifier route eats the pod name (``names=()``), so
    the registered-carrier match has structurally nothing to compare.
    The verdict rides the VERB×DOMAIN syntax anchor
    (:meth:`FaultProviderRegistry.is_blade_exec_destroy_delivery` over
    ``classify_blade_exec_payload`` — segment-level, wrapper-tolerant,
    sh -c-expanding, covering non-hex UID spellings), never the E3
    hex-UID form anchor (narrower: a non-hex spelling would silently
    fall back to attribution).

    Two withholds, both mirrors of the CHANNEL face:
    - FAULT-BINARY WITHHOLD — ``effective.fault_binary_mutation`` must
      be False (a fault binary riding past ``;`` withholds the
      exemption; G-9 makes the flag segment-level, so the mixed
      destroy+stress-ng compound stays attributed).
    - CREATE WITHHOLD — the seam itself excludes any payload carrying a
      ``blade create`` segment (a REAL experiment delivery; the
      chaosblade provider's own issue-time hook claims it first by
      registration order anyway).

    Third withhold (R27/G-11a), the escape twin of the first:
    - ESCAPE WITHHOLD — ``effective.scope`` must not be SCOPE_ESCAPE:
      a payload whose segments reach the HOST (``destroy; nsenter -t 1
      -m sh``) is not PURE demolition, and the classifier has already
      legislated it — exempting it anyway would swallow the escape
      stage's evidence whole (learning mode really executes the
      payload, and the escape runs with zero attribution). The CHANNEL
      face is structurally immune (SCOPE_ESCAPE carries ``names=()``,
      nothing to match); this R25 face reads only the payload syntax
      anchor, so the scope check must be explicit. An escape payload
      riding past ``;`` withholds the exemption — the same sentence
      the fbm withhold legislates.
    """
    if getattr(effective, "fault_binary_mutation", False):
        return False
    if getattr(effective, "scope", None) == SCOPE_ESCAPE:
        return False
    from chaos_agent.agent.providers.registry import FaultProviderRegistry

    return FaultProviderRegistry.is_blade_exec_destroy_delivery(
        tool_args.get("v_args")
    )


def _delete_positional_tokens(v_args: str) -> list[str]:
    """A delete command's positional tokens (flags and their values out).

    Shared tokenizer for the G-4 name expansion — mirrors the flags the
    pre-G-4 ``_deleted_pod_identity`` skipped (``-n`` / ``--namespace`` /
    ``-l`` / ``--selector`` consume a value; every other leading-dash
    token is a valueless flag). """
    try:
        args = shlex.split(v_args)
    except ValueError:
        args = v_args.split()
    positionals: list[str] = []
    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if token in ("-n", "--namespace", "-l", "--selector"):
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        positionals.append(token)
    return positionals


def _split_name_segment(token: str) -> list[tuple[str, str]]:
    """Split one raw name token into ``(name, kind)`` comma segments.

    ``kind`` is ``""`` for a bare name (it inherits the delete's scope
    kind); a ``kind/name`` slash segment carries its own so a mixed-kind
    batch (``pod/a,svc/b``) can be recognised as poisoned. """
    pairs: list[tuple[str, str]] = []
    for seg in token.split(","):
        seg = seg.strip()
        if not seg:
            continue
        if "/" in seg:
            kind_head, _, name = seg.partition("/")
            pairs.append((name, kind_head))
        else:
            pairs.append((seg, ""))
    return pairs


def _is_delete_kind_head(token: str) -> bool:
    """True when a positional token is the command's KIND head, not a name.

    A bare asset name that collides with a kind keyword is
    indistinguishable from the kind head at this layer (the classifier's
    positional disambiguation admits the same ambiguity) — the collision
    drops the name, which can only NARROW the exemption (conservative). """
    if "/" in token:
        return False
    return token.split(".", 1)[0].lower().strip() in KIND_ALIASES


def _expanded_delete_name_pairs(
    v_args: str, classifier_names: Any = (),
) -> list[tuple[str, str]]:
    """Per-name expansion of a delete's target list (G-4/R20 single source).

    Returns ``(name, kind)`` pairs. ``v_args`` (preferred) is expanded
    from positional tokens so EVERY legal batch spelling is covered
    (``pod a,b`` / ``pod/a,pod/b`` / ``pod a b``), including names the
    classifier's single-anchor tuple drops; ``classifier_names`` are
    comma-split as the fallback for callers without the raw string
    (tests constructing an ``EffectiveTarget`` directly). Duplicate
    names across sources are harmless — callers compare sets. """
    pairs: list[tuple[str, str]] = []
    if v_args:
        for token in _delete_positional_tokens(v_args):
            if _is_delete_kind_head(token):
                continue
            pairs.extend(_split_name_segment(token))
    for raw in classifier_names or ():
        pairs.extend(_split_name_segment(str(raw)))
    return pairs


def make_teardown_matcher(artifacts: list) -> Callable[[str, Any], bool]:
    """Build the ``(tool_name, tool_args) -> bool`` teardown closure (P3).

    The vocabulary-layer threading seam: agent-side callers snapshot the
    CURRENT registry into this closure and hand it to the scan primitives'
    ``is_teardown`` parameter, so the teardown≠mutation exemption is applied
    INSIDE the vocabulary layer at CALL granularity (mixed batches included)
    instead of each consumer remembering to pre-filter whole messages.
    Construct it FRESH at each seam invocation — the registry grows during
    a task (a vehicle created this iteration must be recognisable by the
    next scan), and closing over a stale snapshot would narrow the
    exemption mid-loop. ``artifacts`` semantics match
    :func:`is_vehicle_teardown_delete`` (status-irrelevant, per-task).
    """
    def _is_teardown(tool_name: str, tool_args: Any) -> bool:
        return issue_call_is_registered_teardown(tool_name, tool_args, artifacts)

    return _is_teardown


def collect_execution_artifacts(
    messages: list,
    existing: list[dict] | None = None,
    *,
    task_id: str = "",
    operation_family: str = "",
) -> list[dict]:
    """Merge artifact facts from tool results into the current durable list."""
    artifacts: dict[str, dict] = {}
    for item in existing or []:
        if not isinstance(item, dict):
            continue
        key = _artifact_key(item)
        if key:
            artifacts[key] = deepcopy(item)

    tool_calls = _tool_call_lookup(messages)
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        call = tool_calls.get(getattr(message, "tool_call_id", ""), {})
        tool_name = call.get("name")
        content = message.content if isinstance(message.content, str) else ""

        # Skill scripts that create occupancy workloads register them via a
        # ``[drill-vehicle: {...}]`` line (see parse_drill_vehicle_markers).
        # Registration here — not in the screener — because the script's
        # inner kubectl calls never pass the guard: the marker is the ONLY
        # durable record of what the script created.
        if tool_name == "execute_skill_script":
            for vehicle in parse_drill_vehicle_markers(content):
                artifact = _drill_vehicle_artifact(
                    vehicle,
                    task_id=task_id,
                    operation_family=operation_family,
                    tool_call_id=getattr(message, "tool_call_id", ""),
                )
                key = _artifact_key(artifact)
                if key and key not in artifacts:
                    artifacts[key] = artifact
            continue

        if tool_name != "kubectl":
            continue
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        subcommand = args.get("subcommand", "")

        if subcommand == "debug":
            metadata = parse_debug_pod_metadata(content)
            artifact = _debug_pod_artifact(
                metadata,
                task_id=task_id,
                operation_family=operation_family,
                tool_call_id=getattr(message, "tool_call_id", ""),
                debug_v_args=str(args.get("v_args") or ""),
            )
            key = _artifact_key(artifact)
            if key:
                existing_artifact = artifacts.get(key)
                if existing_artifact is None:
                    # Stamp the freshness marker exactly once, when the pod
                    # first registers as active. It is a durable fact like
                    # ``uid``/``node``: message replay rebuilds this artifact
                    # every loop, so we must NOT re-derive ``time.time()`` on
                    # each rebuild or the liveness window would never expire.
                    # Subsequent rebuilds take the merge branch below, which
                    # preserves existing non-empty fields.
                    if (
                        artifact.get("status") == "active"
                        and not artifact.get("confirmed_live_epoch")
                    ):
                        artifact["confirmed_live_epoch"] = time.time()
                    artifacts[key] = artifact
                else:
                    _merge_discovered_artifact(existing_artifact, artifact)
            continue

        if subcommand == "delete" and not _tool_result_failed(message):
            pod_names, namespace = _deleted_pod_identities(
                args.get("v_args", ""),
            )
            if not pod_names:
                continue
            deleted = set(pod_names)
            for artifact in artifacts.values():
                if artifact.get("type") == "recovery_carrier":
                    # A confirmed manual ``kubectl delete pod <carrier>``
                    # (the standard §6 four-way delete, executed by the
                    # LLM) is a manual DISARM, not a completion: the timer
                    # host is gone, so keep-while-armed must stop holding
                    # the artifact back — otherwise a partially-executed
                    # manual sweep (pod deleted, RBAC deletes not yet run)
                    # leaves the family with no system-side sweeper until
                    # the now-meaningless deadline passes. Flip back to
                    # active so the next cleanup round runs the idempotent
                    # four-way sweep. Already-cleaned artifacts stay
                    # cleaned (replayed deletes must not revive them).
                    if (
                        artifact.get("name") in deleted
                        and artifact.get("status") == "recovery_armed"
                        and (
                            not namespace
                            or artifact.get("namespace") == namespace
                        )
                    ):
                        artifact["status"] = "active"
                        artifact.pop("recovery_deadline_epoch", None)
                        artifact["cleanup_tool_call_id"] = getattr(
                            message, "tool_call_id", "",
                        )
                    continue
                if artifact.get("type") != "debug_pod":
                    continue
                if artifact.get("name") not in deleted:
                    continue
                if namespace and artifact.get("namespace") != namespace:
                    continue
                # #59 cascade: deleting an ARMED carrier pod kills a
                # CARRIER-RESIDENT reversal (an in-chain sleep or timeout
                # wrapper rides the pod's own process tree) — the fill it
                # would have reclaimed leaks with no pending self-recovery.
                # Stamp the void fact so the early-recovery lane keeps
                # accepting a standalone reclaim for the recorded fill path.
                # A host-managed timer survives the pod under PID 1 and
                # stays valid — no stamp, the lane stays correctly closed.
                # (The same stamp the dead-carrier branch of cleanup
                # applies; this is its manual-delete twin.)
                if (
                    artifact.get("status") == "recovery_armed"
                    and str(artifact.get("recovery_form") or "")
                    != "host_timer"
                ):
                    artifact["recovery_void"] = True
                artifact["status"] = "cleaned"
                artifact["cleanup_tool_call_id"] = getattr(
                    message, "tool_call_id", "",
                )
            continue

        # A successful RBAC create whose NAME matches a registered recovery
        # carrier is that carrier's asset family (the standard prescribes
        # sa/role/rolebinding share the carrier Pod's name exactly — exact
        # match, never prefix). Attachment is data-driven from the tool
        # result so finalize's stack cleanup deletes what was ACTUALLY
        # created; names that deviate from the convention simply stay
        # unattached (leftover RBAC is inert, and the case doc's manual
        # full-clean covers it).
        if subcommand == "create" and not _tool_result_failed(message):
            _attach_recovery_carrier_rbac(
                artifacts, str(args.get("v_args") or ""),
            )
            continue

        # A successful json-patch verb ADDITION on a carrier-family Role/
        # ClusterRole (the standard's two-step self-delete rule) widens the
        # registered grant: the member's ``verbs`` union the patch payload's
        # arrays so the B85 arming-time reconciliation (tool_screener) sees
        # the grant as it actually stands, not as the create alone left it.
        if subcommand == "patch" and not _tool_result_failed(message):
            _extend_recovery_carrier_rbac_verbs(
                artifacts, str(args.get("v_args") or ""),
            )
            continue

        if subcommand == "exec" and not _tool_result_failed(message):
            _mark_bounded_host_recovery(
                artifacts,
                args.get("v_args", ""),
                getattr(message, "tool_call_id", ""),
            )

    # Provider-owned carrier artifacts (openspec faultdrill-cr-channel
    # task 2.6): a carrier that rides the standard kubectl face (e.g. the
    # FaultDrill CR apply) discovers its own landed objects from the same
    # history, claim-based through the registry — this module stays free
    # of carrier-specific imports and vocabulary. ALL landed applies
    # register: the fault HANDLE is latest-wins, so an early CR from a
    # rename-retry would otherwise be orphaned with no recovery
    # reference (review P11) — the ledger is the durable full set, and
    # the sweep owns each object's settle decision.
    try:
        from chaos_agent.agent.providers.registry import FaultProviderRegistry

        for artifact in FaultProviderRegistry.collect_provider_artifacts(
            messages, task_id=task_id, operation_family=operation_family,
        ):
            key = _artifact_key(artifact)
            if not key:
                continue
            if key not in artifacts:
                artifacts[key] = artifact
            else:
                _merge_discovered_artifact(artifacts[key], artifact)
    except Exception:  # noqa: BLE001 — ledger enrichment is never fatal
        logger.debug("provider artifact collection failed", exc_info=True)

    return list(artifacts.values())


def find_active_debug_pod(
    artifacts: list[dict] | None,
    pod_name: str,
    namespace: str,
) -> dict | None:
    """Return a registered executable debug pod matching exact identity."""
    matches: list[dict] = []
    for artifact in reversed(artifacts or []):
        if not isinstance(artifact, dict):
            continue
        if (
            artifact.get("type") != "debug_pod"
            or artifact.get("status") not in ("active", "recovery_armed")
        ):
            continue
        if artifact.get("name") != pod_name:
            continue
        if namespace and (artifact.get("namespace") or "default") != namespace:
            continue
        if not artifact.get("uid") or not (artifact.get("target") or {}).get("name"):
            continue
        matches.append(artifact)
    # An omitted namespace means "the active transport namespace". The debug
    # pod was created through that same transport, so a unique registered name
    # is authoritative. Ambiguity still fails closed.
    return matches[0] if len(matches) == 1 else None


#: Which record a carrier row in :func:`carrier_status_facts` came from.
#: The structured ledger, which survives message trimming and carries type,
#: namespace and lifecycle status.
CARRIER_SOURCE_LEDGER = "execution_artifacts"
#: The bare name set the framework's own cleanup paths write. A planning-phase
#: probe carrier deleted through the transport leaves NO ``kubectl`` ToolMessage
#: behind, so it never reaches the ledger — this set is the only record that the
#: framework issued its delete.
CARRIER_SOURCE_CLEANUP_RECORD = "cleanup_record"


def carrier_status_facts(state: dict | None) -> dict:
    """One roster of every carrier this task built, with each row's source.

    Two records answer "what happened to this task's carriers?" and until now
    no reader merged them, so every consumer saw a partial view:

    * ``execution_artifacts`` — structured and durable, but populated only from
      ``kubectl`` / ``execute_skill_script`` ToolMessages. The framework's own
      cleanup deletes through the transport, so a carrier IT removed leaves no
      ToolMessage and no ledger transition.
    * ``cleaned_debug_pods`` — a bare name set written by exactly those cleanup
      paths (planning exit, verify finalize). It is the only record of a
      planning-probe carrier's fate, and nothing rendered it.

    Case #67 is what the gap cost: the plan text recorded "two Completed
    node-debugger pods (fdfj5, 7j6b2) folded into the cleanup step", the
    framework force-deleted both at planning exit, and the verifier — reading
    only prose, with no authoritative roster in its context — still reported
    them as "STILL NEEDS CLEANUP". The report was false while every durable
    field was correct.

    Honesty constraint carried into every row: ``status == "cleaned"`` means a
    delete was ISSUED, not that absence was confirmed — cleanup is
    fire-and-forget by design (see ``_CLEANUP_CONCURRENCY``). A row therefore
    never claims more than its source can prove, and the render must not either.
    """
    values = state if isinstance(state, dict) else {}
    carriers: list[dict] = []
    seen: set[str] = set()
    for artifact in values.get("execution_artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        if artifact.get("type") not in VEHICLE_ARTIFACT_TYPES:
            continue
        name = str(artifact.get("name") or "")
        if not name:
            continue
        seen.add(name)
        carriers.append({
            "name": name,
            "namespace": str(artifact.get("namespace") or ""),
            "type": str(artifact.get("type") or ""),
            "kind": str(artifact.get("kind") or ""),
            "status": str(artifact.get("status") or ""),
            "source": CARRIER_SOURCE_LEDGER,
        })
    # Names the framework deleted that have NO ledger row — planning-probe
    # carriers, and any artifact collection that has not run yet this turn.
    # ``cleaned`` (not ``active``): the delete was issued; absence is unproven.
    # ``kind`` is recorded alongside ``type`` per this module's convention
    # (``_debug_pod_artifact`` does the same, and the P5 vehicle-kind sentinel
    # enforces it): ``cleaned_debug_pods`` only ever holds debug POD names, so
    # the kind is a fact, not a placeholder.
    for name in values.get("cleaned_debug_pods") or []:
        name = str(name or "")
        if not name or name in seen:
            continue
        seen.add(name)
        carriers.append({
            "name": name,
            "namespace": "",
            "type": "debug_pod",
            "kind": "pod",
            "status": "cleaned",
            "source": CARRIER_SOURCE_CLEANUP_RECORD,
        })
    return {"carriers": carriers}


async def _read_pod_phase(
    artifact: dict,
    *,
    kubeconfig: str,
    task_id: str,
) -> str | None:
    """Live-read a vehicle pod's Kubernetes phase; ``None`` on any failure.

    #59: the carrier ledger's ``phase`` was written once at creation and
    never refreshed, so a carrier evicted mid-drill kept reporting
    ``Running`` in the terminal recovery handle. This is the cheap
    single-purpose read that refreshes it — fire-and-forget like the rest
    of cleanup; a failed read yields ``None`` and callers keep their
    previous (ledger-only) behaviour.
    """
    from chaos_agent.tools.kubectl_cli import build_kubectl_cmd
    from chaos_agent.transports import (
        PROFILE_K8S,
        TransportTarget,
        execute_via_transport,
    )

    name = str(artifact.get("name") or "")
    namespace = str(artifact.get("namespace") or "")
    if not name or not namespace:
        return None
    get_cmd = build_kubectl_cmd("get", [
        "pod", name, "-n", namespace,
        "-o", "jsonpath={.status.phase}",
    ], kubeconfig=kubeconfig)
    try:
        result = await execute_via_transport(
            get_cmd, TransportTarget.from_state({}), timeout=15,
            task_id=task_id, expect_profile=PROFILE_K8S,
        )
    except Exception:  # noqa: BLE001 — liveness read is best-effort
        return None
    if result.exit_code != 0:
        return None
    phase = (result.stdout or "").strip()
    return phase or None


async def cleanup_debug_pod_artifacts(
    artifacts: list[dict] | None,
    *,
    kubeconfig: str,
    task_id: str,
) -> tuple[list[dict], list[str]]:
    """Fire-and-forget cleanup of tracked debug pods.

    Attempts each delete exactly once (no retry, no backoff) and marks the
    artifact ``cleaned`` regardless of whether removal was confirmed. Debug
    pods are created with a bounded lifetime (``-- sleep 3600``), so a delete
    that does not land under an in-progress network fault is left to lapse on
    its own rather than retried. Deletes fan out concurrently (bounded by
    ``_CLEANUP_CONCURRENCY``) so a whole-zone teardown stays fast.
    """
    from chaos_agent.agent.nodes.execute._debug_pod import delete_debug_pod

    updated = deepcopy(artifacts or [])
    cleaned: list[str] = []
    sem = asyncio.Semaphore(_CLEANUP_CONCURRENCY)

    async def _clean_one(artifact: dict) -> None:
        if not isinstance(artifact, dict):
            return
        if artifact.get("type") not in VEHICLE_ARTIFACT_TYPES:
            # Provider-owned carrier artifact (openspec
            # faultdrill-cr-channel task 2.6): claim-based sweep — the
            # owning carrier decides the lifecycle (the CR channel keeps
            # an Injected CR alive and settles a Recovered / externally-
            # removed one). No claim → untouched; ``False`` (keep) → the
            # next sweep round re-examines. A claiming-hook error is
            # never fatal here either — same fire-and-forget contract as
            # every other artifact type.
            swept = await _sweep_provider_artifact(
                artifact, kubeconfig, task_id,
            )
            if swept:
                artifact["status"] = "cleaned"
                cleaned.append(str(artifact.get("name") or ""))
            return
        if artifact.get("status") == "cleaned":
            return
        recovery_deadline = artifact.get("recovery_deadline_epoch")
        if (
            artifact.get("status") == "recovery_armed"
            and isinstance(recovery_deadline, (int, float))
            and recovery_deadline > time.time()
        ):
            # #59 liveness truth: keep-while-armed used to be a pure ledger
            # judgement — a carrier evicted by its own fault stayed "held"
            # (and the ledger kept its stale phase) long after the pod was
            # gone. Verify the carrier is ALIVE before holding: a dead
            # carrier releases the hold (there is nothing left to keep)
            # and a CARRIER-RESIDENT reversal is stamped void (a host-managed
            # timer survives the carrier and stays valid). An unreadable
            # phase keeps the hold — fail-safe, not fail-open. ``recovery_void``
            # has no in-process reader by design: it rides the artifact dict
            # into the task JSON (execution_artifacts / recovery_handle) as an
            # audit fact — a recover-graph or external consumer reads WHY no
            # self-recovery is pending off the persisted record, not off a
            # live render here.
            phase = await _read_pod_phase(
                artifact, kubeconfig=kubeconfig, task_id=task_id,
            )
            if phase is None:
                return
            artifact["phase"] = phase
            if phase in ("Running", "Pending"):
                return
            if str(artifact.get("recovery_form") or "") != "host_timer":
                artifact["recovery_void"] = True
            # Fall through to delete: the carrier is dead, so the
            # keep-while-armed hold protects nothing.
        name = str(artifact.get("name") or "")
        namespace = str(artifact.get("namespace") or "")
        if not name or not namespace:
            return
        kind = (
            "deployment"
            if artifact.get("type") == "occupant_deployment"
            else "pod"
        )
        # Fire-and-forget: one delete attempt, bounded by the shared semaphore
        # so at most ``_CLEANUP_CONCURRENCY`` are in flight at once.
        async with sem:
            if artifact.get("type") == "recovery_carrier":
                # The carrier's stack cleanup deletes the pod AND the RBAC
                # family recorded on the artifact (task 2.2). Order: pod
                # first (stop the timer host), then rolebinding → role →
                # serviceaccount so no dangling grant survives a partial
                # sweep. Each delete is its own fire-and-forget attempt —
                # the pod's bounded ``-- sleep`` skeleton self-expires even
                # when a delete does not land, and unattached RBAC objects
                # are inert without a binding.
                family = [
                    (
                        str(member.get("kind")),
                        str(member.get("name") or ""),
                        # Cross-ns variant members live in the recovery ns
                        # (e.g. kube-system) — delete each where it lives,
                        # falling back to the carrier ns for same-ns stacks
                        # recorded before members carried their own ns.
                        str(member.get("namespace") or namespace),
                    )
                    for member in artifact.get("rbac_family") or []
                    if isinstance(member, dict) and member.get("name")
                ]
                family.sort(key=lambda trio: {
                    "rolebinding": 0, "role": 1, "serviceaccount": 2,
                    # Cluster-scoped pair deletes AFTER the namespaced
                    # stack, binding before role (same dangling-grant
                    # ordering principle as the namespaced pair).
                    "clusterrolebinding": 3, "clusterrole": 4,
                }.get(trio[0], 5))
                outcomes = [("pod", name, await delete_debug_pod(
                    name, kubeconfig, task_id, namespace=namespace,
                ))]
                for family_kind, family_name, family_ns in family:
                    if family_name == name and family_kind == "pod":
                        continue
                    outcomes.append((family_kind, family_name, await delete_debug_pod(
                        family_name, kubeconfig, task_id,
                        namespace=family_ns, kind=family_kind,
                    )))
                unconfirmed = [
                    f"{k}/{n}" for k, n, o in outcomes if o != "confirmed"
                ]
                if unconfirmed:
                    logger.info(
                        "recovery carrier stack delete not confirmed; "
                        "left to lapse (not retried): %s/%s %s",
                        namespace, name, ", ".join(unconfirmed),
                    )
            else:
                outcome = await delete_debug_pod(
                    name, kubeconfig, task_id, namespace=namespace, kind=kind,
                )
        # Mark cleaned regardless of outcome — the pod's ``-- sleep 3600`` bound
        # lets an unlanded delete lapse on its own; we never retry it.
        artifact["status"] = "cleaned"
        cleaned.append(name)
        if artifact.get("type") == "recovery_carrier":
            # The recovery_carrier branch logged its own per-member outcomes
            # above; the generic single-delete report below has no variable
            # to read (and would duplicate the message).
            return
        if outcome != "confirmed":
            logger.info(
                "debug pod delete not confirmed; leaving it to expire "
                "(not retried): %s/%s", namespace, name,
            )

    # ``gather`` runs the per-artifact coroutines concurrently; each mutates its
    # own artifact dict in place (single event loop → no lock needed).
    await asyncio.gather(*(_clean_one(a) for a in updated))
    return updated, cleaned


async def _sweep_provider_artifact(
    artifact: dict, kubeconfig: str, task_id: str,
) -> bool | None:
    """Registry claim-sweep for one provider-owned artifact (never raises)."""
    from chaos_agent.agent.providers.registry import FaultProviderRegistry

    try:
        return await FaultProviderRegistry.sweep_artifact(
            artifact, kubeconfig=kubeconfig, task_id=task_id,
        )
    except Exception:  # noqa: BLE001 — a sweep failure keeps the artifact
        logger.warning(
            "provider artifact sweep failed (non-fatal, kept): %s",
            artifact.get("name") if isinstance(artifact, dict) else artifact,
            exc_info=True,
        )
        return False


def _debug_pod_artifact(
    metadata: dict,
    *,
    task_id: str,
    operation_family: str,
    tool_call_id: str,
    debug_v_args: str,
) -> dict:
    if not metadata:
        return {}
    name = str(metadata.get("name") or "")
    namespace = str(metadata.get("namespace") or "")
    uid = str(metadata.get("uid") or "")
    node = str(metadata.get("node") or "")
    if not name or not namespace:
        return {}
    # Pod-scoped debug attaches an EPHEMERAL CONTAINER to an existing pod. That
    # pod is the USER'S workload — ``name`` is NOT a tool-created debug pod. It
    # must never be registered with a delete cleanup: an ephemeral container is
    # removed only when the pod itself is recreated, and firing
    # ``kubectl delete pod <name>`` here would destroy the user's workload. So a
    # pod-scoped debug produces NO durable debug_pod artifact (the ephemeral
    # container is not a separately-managed carrier — the tc/exec runs in the
    # target pod's own namespaces, screened as a plain scope=pod call).
    # Classification via the shared authority (debug_meta_scope) so this guard
    # cannot drift from its siblings in _debug_pod / is_vehicle_name.
    if debug_meta_scope(metadata) == "pod":
        return {}
    ready = metadata.get("ready") is True
    cleaned = metadata.get("cleaned") is True
    return {
        "artifact_id": uid or f"debug_pod:{namespace}/{name}",
        "type": "debug_pod",
        "kind": "pod",
        "status": (
            "cleaned" if cleaned else "active" if ready and uid and node else "failed"
        ),
        "task_id": task_id,
        "name": name,
        "namespace": namespace,
        "uid": uid,
        "target": {"scope": "node", "name": node},
        "operation_family": operation_family,
        "debug_profile": _option_value(debug_v_args, "--profile") or str(metadata.get("debug_profile") or ""),
        "privileged": metadata.get("privileged") is True,
        "phase": metadata.get("phase") or "Unknown",
        "created_tool_call_id": tool_call_id,
        "cleanup": {
            "tool": "kubectl",
            "subcommand": "delete",
            "v_args": f"pod {name} -n {namespace} --ignore-not-found",
        },
    }


def _drill_vehicle_artifact(
    vehicle: dict,
    *,
    task_id: str,
    operation_family: str,
    tool_call_id: str,
) -> dict:
    """Build a vehicle artifact from a script-emitted registration line."""
    kind = str(vehicle.get("kind") or "").strip().lower()
    name = str(vehicle.get("name") or "")
    namespace = str(vehicle.get("namespace") or "")
    if kind == "deployment":
        artifact_type = "occupant_deployment"
    elif kind == "pod":
        artifact_type = "occupant_pod"
    else:
        return {}
    if not name or not namespace:
        return {}
    return {
        "artifact_id": f"{artifact_type}:{namespace}/{name}",
        "type": artifact_type,
        "kind": kind,
        "status": "active",
        "task_id": task_id,
        "name": name,
        "namespace": namespace,
        "operation_family": operation_family or "resource_occupancy",
        "created_tool_call_id": tool_call_id,
        "cleanup": {
            "tool": "kubectl",
            "subcommand": "delete",
            "v_args": (
                f"{'deployment' if kind == 'deployment' else 'pod'} {name} "
                f"-n {namespace} --ignore-not-found"
            ),
        },
    }


def _tool_call_lookup(messages: list) -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls or []:
            call_id = call.get("id", "") if isinstance(call, dict) else ""
            if call_id:
                lookup[call_id] = call
    return lookup


def _merge_discovered_artifact(current: dict, discovered: dict) -> None:
    """Fill durable facts without rewinding lifecycle state on replay.

    Message history is replayed on every execute-loop iteration and after TUI
    cancellation. The same debug creation event must not turn ``cleaned`` back
    into ``active`` or move an already-armed recovery deadline forward.
    """
    for key, value in discovered.items():
        if key == "status":
            continue
        if key not in current or current[key] in (None, "", [], {}):
            current[key] = deepcopy(value)


def _tool_result_failed(message: ToolMessage) -> bool:
    """Delegate to the single-source verdict (``agent/tool_verdicts.py``).

    Was ``status == "error" or content.startswith("Error:") or
    content.startswith("[target_guard]")`` — the same three terms every
    other consumer re-derived locally. Every caller here is already scoped
    to a kubectl subcommand, so the practical effect of routing through the
    shared verdict is the widened ``Error`` (colon-less) prefix; that is
    fail-closed for this module, whose verdicts gate artifact registration
    (a result that MIGHT be a failure must not register a recovery carrier
    or a deleted-pod identity).
    """
    return message_result_failed(message)


def _systemd_timer_seconds(inner: str) -> int:
    """The fault window a systemd-run transient timer arms, in seconds.

    A duration-style ``--on-active/--on-boot/--on-startup/--on-unit-active``
    value is a fixed delay and converts to seconds (systemd duration
    semantics: a bare number is seconds; ``s``/``min``/``h`` suffixes).
    ``--on-calendar`` is a recurrence spec with no single firing delay, and a
    polyglot value that does not parse is no deadline at all — both return 0.
    """
    best = 0
    for value in re.findall(
        r"--on-(?:active|boot|startup|unit-active)=(\S+)", inner,
    ):
        match = re.fullmatch(r"(\d+)([a-z]*)", value.lower())
        if not match:
            continue
        number, unit = int(match.group(1)), match.group(2)
        if unit in ("", "s", "sec", "second", "seconds"):
            seconds = number
        elif unit in ("m", "min", "minute", "minutes"):
            seconds = number * 60
        elif unit in ("h", "hr", "hour", "hours"):
            seconds = number * 3600
        else:
            continue
        best = max(best, seconds)
    return best


def _timeout_bound_seconds(inner: str) -> int:
    """The fault window a ``timeout N`` / ``--timeout N`` self-termination arms.

    Self-terminating forms (a timeout-wrapped IO burn loop, a bounded nc
    listener, a stressor with ``--timeout``) carry no systemd timer and no
    recovery-meaningful sleep — their bound IS the window. Zero when no
    positive bound is present.
    """
    best = 0
    for pattern in (
        r"\btimeout\s+([1-9][0-9]*)\b",
        r"--timeout(?:=|\s+)([1-9][0-9]*)",
    ):
        for value in re.findall(pattern, inner):
            best = max(best, int(value))
    return best


def _mark_bounded_host_recovery(
    artifacts: dict[str, dict],
    v_args: str,
    tool_call_id: str,
) -> None:
    """Keep a debug carrier alive while its node-local rollback timer runs.

    Extended to recovery carriers (recovery-carrier-standard): the carrier's
    arming exec — ``sh -c '( sleep N; <REST rollback> ) & echo armed'`` —
    carries no host-family inverse, so the host classifier gate applies only
    to debug pods; for a recovery carrier the sleep timer below IS the bound.
    """
    try:
        args = shlex.split(v_args)
    except ValueError:
        return
    if "--" not in args:
        return
    separator = args.index("--")
    outer = args[:separator]
    inner = " ".join(args[separator + 1:])
    pod_name, namespace = _exec_pod_identity(outer)
    if not pod_name:
        return
    matches = [
        artifact for artifact in artifacts.values()
        if artifact.get("type") in ("debug_pod", "recovery_carrier")
        and artifact.get("name") == pod_name
        and (not namespace or artifact.get("namespace") == namespace)
    ]
    if len(matches) != 1:
        return
    artifact = matches[0]
    # Replay-proofing is a SET of seen tool-call ids, not a single pointer.
    # ``collect_execution_artifacts`` rescans the FULL message history every
    # execute-loop iteration; with a one-slot guard any later exec moves the
    # pointer away, and the ORIGINAL arming message re-fires on every replay
    # — pushing the deadline to ``now + window`` each round (and reviving a
    # ``cleaned`` artifact into armed). Once a call id has fired it must
    # never fire again.
    seen_ids = artifact.setdefault("host_exec_seen_ids", [])
    if tool_call_id in seen_ids:
        return
    seen_ids.append(tool_call_id)
    if artifact.get("status") == "cleaned":
        # A cleaned artifact stays cleaned: the only fresh execs that can
        # arrive after cleanup are replays of history (the pod is gone).
        return
    if artifact.get("type") == "debug_pod":
        from chaos_agent.agent.target_guard.carriers import (
            RECOVERY_FAMILIES,
            classify_host_operation,
            host_operation_has_bounded_recovery,
        )

        family = classify_host_operation(inner)
        if not family or not host_operation_has_bounded_recovery(inner, family):
            return
        # #59 / W-67-8: record WHAT the fault armed so the carrier gate's
        # early-recovery lane can later match a standalone pure inverse against
        # it, and so the recovery ledger can tell a host-managed timer
        # (survives the carrier) from an in-chain reversal (dies with it). The
        # disk family records the filled path AND any device-mapper mapping
        # name (reclaimed by ``dmsetup remove``); the network family records the
        # inserted rule fingerprints (reclaimed by a matching ``-D`` / ``qdisc
        # del``). The recovery registry drives EVERY bounded-reclaim face from
        # one enumeration, so a new family records its fingerprint here by
        # construction — no per-family ``if`` to forget (the gap that left the
        # network face's ``iptables -D`` lane structurally unreachable).
        for face in RECOVERY_FAMILIES:
            if face.family != family:
                continue
            value = face.ledger_extract(inner)
            if value:
                artifact[face.ledger_key] = value
    # The deadline is the FAULT WINDOW, not any sleep in the command. A
    # timer-armed stop loop carries a short loop-interval sleep (e.g.
    # ``sleep 15`` between rounds) while its window is the systemd-run
    # duration — arming from the interval released the carrier while the
    # fault was still running (task inject-e47de3e8 review). Prefer the
    # timer when it parses to a fixed delay, then a self-termination bound
    # (timeout N / --timeout), then the sleep-based form's reading — the
    # MAX sleep when several appear (a re-arm command pkill's its OLD
    # timer's pattern, and that pattern's embedded ``sleep 30[0]`` literal
    # would otherwise arm the carrier for a fraction of the real window).
    timeout_seconds = _systemd_timer_seconds(inner)
    recovery_form = "host_timer" if timeout_seconds else ""
    if not timeout_seconds:
        timeout_seconds = _timeout_bound_seconds(inner)
        recovery_form = "timeout" if timeout_seconds else ""
    if not timeout_seconds:
        sleeps = re.findall(r"\bsleep\s+([1-9][0-9]*)\b", inner)
        if not sleeps:
            return
        timeout_seconds = max(int(value) for value in sleeps)
        recovery_form = "in_chain"
    # Keep-while-armed is a LOWER bound: a later exec carrying a SHORTER
    # sleep (a verification probe riding the carrier — REST probes wrap
    # ``sleep 2 && curl ...``) must not eat into an armed window, or
    # finalize's cleanup deletes a timer host whose countdown is still
    # running. A LONGER re-arm still extends the window.
    old_deadline = artifact.get("recovery_deadline_epoch")
    commit_epoch = time.time()
    new_deadline = commit_epoch + timeout_seconds
    if (
        artifact.get("status") == "recovery_armed"
        and isinstance(old_deadline, (int, float))
        and old_deadline >= new_deadline
    ):
        # Refresh the replay guard only; window, bound, and armed status
        # stay as they are.
        artifact["host_exec_tool_call_id"] = tool_call_id
        return
    artifact["status"] = "recovery_armed"
    artifact["host_exec_tool_call_id"] = tool_call_id
    artifact["recovery_timeout_seconds"] = timeout_seconds
    artifact["recovery_deadline_epoch"] = new_deadline
    # The framework's own commit moment, stored rather than left as the
    # ``deadline - timeout`` difference it is arithmetically equal to: the
    # difference only reconstructs it while both operands stay in sync, and a
    # re-arm moves the deadline without recording which commit it moved from.
    # Provenance matters more than the value — this is a FRAMEWORK clock reading
    # taken while processing a tool call, not the moment systemd accepted the
    # unit on the host (W-67-15: those two were 85s apart in case #67).
    artifact["recovery_armed_commit_epoch"] = commit_epoch
    # Reconciliation table, persisted so the anchor spread survives into the
    # task JSON instead of having to be re-derived by hand from four scattered
    # fields at audit time. Derived by the single-source reader and rewritten
    # on every (re-)arm, so the persisted copy is a snapshot of the derivation
    # rather than a fifth independent statement of the same facts — a merge
    # pass never touches it (``_merge_discovered_artifact`` only fills EMPTY
    # fields) and nothing mutates it in place.
    artifact["recovery_time_anchors"] = recovery_time_anchors(artifact)
    # #59: which execution realm the reversal lives in — a host systemd
    # timer fires from PID 1 regardless of the carrier's fate, while an
    # in-chain / timeout-bound reversal is carrier-resident and is VOID
    # the moment the carrier dies (the DiskPressure drill evicted its own
    # recovery carrier and the ledger kept counting down a dead timer).
    # Absent for ledger entries written before this field existed; readers
    # treat the absence as the carrier-resident (conservative) case.
    if recovery_form:
        artifact["recovery_form"] = recovery_form


def _option_value(v_args: str, option: str) -> str:
    try:
        args = shlex.split(v_args)
    except ValueError:
        return ""
    for index, token in enumerate(args):
        if token == option and index + 1 < len(args):
            return args[index + 1]
        if token.startswith(f"{option}="):
            return token.split("=", 1)[1]
    return ""


def _exec_pod_identity(args: list[str]) -> tuple[str, str]:
    """The pod/namespace an exec line targets, ahead of its ``--``.

    R46: flag arity comes from the SHARED table
    (``_readonly_facts.kubectl_flag_takes_value``). The hand-written pair
    this replaces (``-n``/``-c`` only) missed the globals that really
    arrive in RAW v_args — hygiene strips ``--context``/``--kubeconfig``
    downstream but this reader runs first, and ``--request-timeout 30s`` /
    ``-v 6`` / ``--as admin`` are never stripped at all — so a flag's
    VALUE was read as the pod name (``--context prod drill-rc-x`` →
    ``prod``): the B85 layer-1 reconcile then found no registered carrier
    (fail-open) and the carrier arming marker matched nothing (the window
    never armed).
    """
    from chaos_agent.tools._readonly_facts import kubectl_flag_takes_value

    namespace = ""
    pod_name = ""
    skip_next = False
    for index, token in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if token in ("-n", "--namespace") and index + 1 < len(args):
            namespace = args[index + 1]
            skip_next = True
            continue
        if token.startswith("--namespace="):
            namespace = token.split("=", 1)[1]
            continue
        if kubectl_flag_takes_value(token):
            skip_next = True
            continue
        if not token.startswith("-") and not pod_name:
            pod_name = token
    return pod_name, namespace


def _deleted_pod_identities(v_args: str) -> tuple[list[str], str]:
    """Per-name expansion of a delete's POD targets (G-4/R20).

    Returns ``(pod_names, namespace)``. A successful batch delete removed
    every pod it named (``pod a,b`` / ``pod/a,pod/b`` / ``pod a b``), so
    collect flips EACH registered one — the registry tracks the physical
    world, exemption verdict aside (a partially-registered batch still
    flips its registered half). Empty list = the delete names no pod
    target: a label-selector delete, a non-pod kind head (deployment/…)
    or a bare malformed name — the pre-G-4 behaviour for those shapes.
    Non-pod slash segments (``pod/a,svc/b``) are excluded: only kinds
    collect tracks here (debug_pod / recovery-carrier pods) can flip.
    """
    namespace = ""
    try:
        args = shlex.split(v_args)
    except ValueError:
        args = v_args.split()
    for index, token in enumerate(args):
        if token in ("-n", "--namespace") and index + 1 < len(args):
            namespace = args[index + 1]
        elif token.startswith("--namespace="):
            namespace = token.split("=", 1)[1]
    positionals = _delete_positional_tokens(v_args)
    pod_names: list[str] = []
    if not positionals:
        return pod_names, namespace
    first = positionals[0]
    if "/" in first:
        # ``kind/name`` positional(s): every comma segment carries its kind
        for token in positionals:
            for seg in token.split(","):
                seg = seg.strip()
                if not seg or "/" not in seg:
                    continue
                kind_head, _, name = seg.partition("/")
                if canonicalise_kind(kind_head) == "pod":
                    pod_names.append(name)
        return pod_names, namespace
    if canonicalise_kind(first) != "pod":
        return pod_names, namespace
    # ``pod NAME...`` — every following positional is a possibly
    # comma-joined pod name
    for token in positionals[1:]:
        for seg in token.split(","):
            seg = seg.strip()
            if seg:
                pod_names.append(seg)
    return pod_names, namespace


# ``kubectl create`` kinds that can belong to a recovery carrier's asset
# stack. The map covers the spellings kubectl accepts; anything else is not
# carrier machinery and is left alone. Cluster-scoped members
# (ClusterRole/ClusterRoleBinding) belong to the FIVE-object carrier
# variant (recovery writes touching cluster-scoped resources, e.g. node
# taint/label restore): they carry no namespace, so ``-n`` in their
# create/delete commands is absent (kubectl ignores it for cluster-scoped
# kinds when present anyway).
_RECOVERY_CARRIER_CREATE_KINDS: dict[str, str] = {
    "sa": "serviceaccount",
    "serviceaccount": "serviceaccount",
    "role": "role",
    "rolebinding": "rolebinding",
    "clusterrole": "clusterrole",
    "clusterrolebinding": "clusterrolebinding",
}

# Cluster-scoped members of a carrier's RBAC family — DERIVED from the
# create-kinds map and the classifier's topology single source (R21/G-5):
# a future cluster-scoped kind added to the map is cluster-scoped here
# automatically, no second hand-kept list to drift. Guarded by
# ``test_carrier_family_cluster_set_is_derived``.
_RECOVERY_CARRIER_CLUSTER_SCOPED_KINDS = frozenset({
    kind for kind in _RECOVERY_CARRIER_CREATE_KINDS.values()
    if is_cluster_scoped_kind(kind)
})


def _attach_recovery_carrier_rbac(
    artifacts: dict[str, dict], v_args: str,
) -> None:
    """Attach a successful RBAC ``create`` to its recovery-carrier artifact.

    Shape parsed: ``kubectl create <kind> <name> [-n <ns>]`` — first
    positional is the kind, second is the name (value-taking flags skipped).
    Attachment requires the name to EXACTLY equal a registered recovery
    carrier's name in the same namespace: the standard's naming convention
    (all four objects share the carrier Pod's name) is what links them, and
    exact match keeps a ``drill-rc-x``/``drill-rc-xy`` prefix collision from
    mis-attributing assets between two carriers in one namespace.

    Role/ClusterRole members additionally record the ``--verb`` grant
    (``--verb=get,patch`` / ``--verb get --verb patch``, repeatable) as
    ``verbs`` — the reconciliation input for the B85 arming-time check
    (payload write verbs ⊆ registered verbs). Absent only when the create
    carried no parseable ``--verb`` (kubectl rejects that shape, so a
    successful receipt implies a non-empty grant).

    Cross-namespace variant (kube-system recovery targets): the Role and
    RoleBinding are created in the RECOVERY namespace (e.g. kube-system)
    while the carrier Pod and SA stay in the target namespace — k8s RBAC
    lets a RoleBinding's subject reference a foreign-namespace SA. The
    family member is recorded under the namespace the create command
    actually targeted, and the name match below keys on the SA's home
    namespace so the recovery-ns create still attaches to its carrier.
    """
    try:
        args = shlex.split(v_args)
    except ValueError:
        return
    positionals: list[str] = []
    namespace = ""
    pending_ns: bool | None = None
    skip_next = False
    for token in args:
        if skip_next:
            # ``-n``/``--namespace`` consumed their value above; record it
            # rather than letting it masquerade as a positional.
            if pending_ns is not None:
                namespace, pending_ns = token, None
            skip_next = False
            continue
        if token in ("-n", "--namespace"):
            pending_ns = True
            skip_next = True
            continue
        if token.startswith("--namespace="):
            namespace = token.split("=", 1)[1]
            continue
        if token.startswith("-"):
            continue
        positionals.append(token)
    if len(positionals) < 2:
        return
    kind = _RECOVERY_CARRIER_CREATE_KINDS.get(positionals[0].lower())
    if not kind:
        return
    name = positionals[1]
    if not name:
        return
    # The verbs grant — only Role/ClusterRole carry rules; SA/binding
    # creates never pass ``--verb`` (their flags are --clusterrole/
    # --serviceaccount, which the parser below ignores by design).
    verbs = (
        _create_role_verbs(args)
        if kind in ("role", "clusterrole")
        else None
    )
    for artifact in artifacts.values():
        if artifact.get("type") != "recovery_carrier":
            continue
        if artifact.get("name") != name:
            continue
        carrier_ns = str(artifact.get("namespace") or "default")
        # Cross-ns variant: the SA (the carrier's own identity) must be
        # created in the carrier ns; a Role/RoleBinding created in ANOTHER
        # ns (e.g. kube-system for a cross-ns recovery target) still belongs
        # to this carrier by name — record the create command's ns on the
        # family member so cleanup deletes it where it lives.
        if kind == "serviceaccount" and (namespace or "default") != carrier_ns:
            continue
        # Topology (R21/G-5): a cluster-scoped member has NO namespace —
        # record "" (its true topology), not the carrier-ns fallback: the
        # member's own cleanup audit entry below deliberately carries no
        # ``-n``, and a carrier-ns registration would make the predicate
        # reject the very replay that audit list prescribes.
        member_ns = (
            ""
            if kind in _RECOVERY_CARRIER_CLUSTER_SCOPED_KINDS
            else (namespace or carrier_ns)
        )
        family = artifact.setdefault("rbac_family", [])
        existing = next(
            (
                member for member in family
                if isinstance(member, dict)
                and member.get("kind") == kind
                and member.get("name") == name
            ),
            None,
        )
        if existing is None:
            member = {
                "kind": kind, "name": name, "namespace": member_ns,
            }
            if verbs is not None:
                member["verbs"] = verbs
            family.append(member)
            if kind in _RECOVERY_CARRIER_CLUSTER_SCOPED_KINDS:
                # Cluster-scoped members (B28 five-object variant) have no
                # pre-rendered audit entry — registration renders only the
                # same-ns four-way stack. Append one WITHOUT ``-n`` so an
                # out-of-band operator replaying the cleanup list also
                # deletes the cluster-level objects (a miss would leave a
                # global-scope RBAC grant silently behind, worse than a
                # namespaced orphan).
                cleanup_list = artifact.setdefault("cleanup", [])
                if not any(
                    isinstance(entry, dict)
                    and entry.get("subcommand") == "delete"
                    and str(entry.get("v_args") or "").startswith(
                        f"{kind} {name} "
                    )
                    for entry in cleanup_list
                ):
                    cleanup_list.append({
                        "tool": "kubectl",
                        "subcommand": "delete",
                        "v_args": f"{kind} {name} --ignore-not-found",
                    })
            elif member_ns != carrier_ns:
                # B26 (run7 out-of-band finding): the carrier artifact's
                # ``cleanup`` audit trail is pre-rendered at registration
                # time with the CARRIER ns on every stack member — a
                # cross-ns member (Role/RoleBinding in the recovery ns)
                # renders wrong. The executor-side delete already honours
                # ``rbac_family``; this rewrites the member's audit entry
                # so an out-of-band operator replaying the cleanup list
                # also deletes where the object actually lives (a
                # ``--ignore-not-found`` miss would otherwise leave an
                # orphan RBAC grant silently behind). Best-effort: a
                # non-matching entry shape is left as-is.
                stale_prefix = f"{kind} {name} -n {carrier_ns} "
                for entry in artifact.get("cleanup") or []:
                    v_args = str(entry.get("v_args") or "") if isinstance(entry, dict) else ""
                    if (
                        entry.get("subcommand") == "delete"
                        and v_args.startswith(stale_prefix)
                    ):
                        entry["v_args"] = (
                            f"{kind} {name} -n {member_ns} "
                            + v_args[len(stale_prefix):]
                        )
        elif verbs is not None:
            # Replay/refresh: the same create receipt rescans every loop —
            # union the grant (idempotent) instead of clobbering, so a
            # member enriched by a json-patch addition keeps its wider
            # verbs across replayed creates.
            existing["verbs"] = sorted(
                set(existing.get("verbs") or []) | set(verbs)
            )
        return


def _create_role_verbs(args: list[str]) -> list[str]:
    """The ``--verb`` grant of a ``kubectl create role/clusterrole``.

    Covers every spelling kubectl accepts: inline ``--verb=get,patch``,
    separate ``--verb get,patch``, repeated flags (union). Returns [] when
    no ``--verb`` appears — a successful create cannot be verb-less
    (kubectl errors out), so an empty grant marks an UNPARSEABLE shape.
    """
    verbs: list[str] = []
    for index, token in enumerate(args):
        if token == "--verb" and index + 1 < len(args):
            verbs.extend(args[index + 1].split(","))
        elif token.startswith("--verb="):
            verbs.extend(token.split("=", 1)[1].split(","))
    return sorted({
        verb.strip().lower() for verb in verbs if verb.strip()
    })


_PATCH_RBAC_KINDS = frozenset({"role", "clusterrole"})


def _extend_recovery_carrier_rbac_verbs(
    artifacts: dict[str, dict], v_args: str,
) -> None:
    """Fold a successful json-patch verb ADDITION into its carrier member.

    The standard's two-step self-delete rule (``kubectl patch <kind>
    <carrier-name> --type=json -p '[..."verbs":["delete"]...]'``) widens the
    grant AFTER the create attached the member. Without this fold the B85
    arming reconciliation would judge the grant by the create alone and
    false-reject the standard six-object flow (self-delete DELETE curl in
    the payload, "delete" granted only via patch).

    The verbs are read off the RAW v_args (``\"verbs\": [...]\"`` arrays in
    the JSON patch payload) rather than shlex tokens: a patch payload with
    inner spaces splits into many tokens, and only the raw line keeps the
    arrays whole. Target matching stays positional (kind, name) exactly
    like the create attach — union, idempotent under replay.
    """
    try:
        args = shlex.split(v_args)
    except ValueError:
        args = v_args.split()
    positionals = [
        token for token in args if not token.startswith("-")
    ]
    if len(positionals) < 2:
        return
    kind = positionals[0].lower()
    if kind not in _PATCH_RBAC_KINDS:
        return
    name = positionals[1]
    add_verbs = {
        verb.strip().lower()
        for match in re.finditer(r'"verbs"\s*:\s*\[([^\]]*)\]', v_args)
        for verb in re.findall(r'"([^"]+)"', match.group(1))
        if verb.strip()
    }
    if not add_verbs:
        return
    for artifact in artifacts.values():
        if artifact.get("type") != "recovery_carrier":
            continue
        if artifact.get("name") != name:
            continue
        for member in artifact.get("rbac_family") or []:
            if (
                isinstance(member, dict)
                and member.get("kind") == kind
                and member.get("name") == name
            ):
                member["verbs"] = sorted(
                    set(member.get("verbs") or []) | add_verbs
                )
        return


def _artifact_key(artifact: dict) -> str:
    artifact_id = str(artifact.get("artifact_id") or "")
    if artifact_id:
        return artifact_id
    artifact_type = str(artifact.get("type") or "")
    name = str(artifact.get("name") or "")
    namespace = str(artifact.get("namespace") or "")
    return f"{artifact_type}:{namespace}/{name}" if artifact_type and name else ""


__all__ = [
    "carrier_status_facts",
    "cleanup_debug_pod_artifacts",
    "collect_execution_artifacts",
    "find_active_debug_pod",
    "is_vehicle_name",
    "is_vehicle_teardown_delete",
    "issue_call_is_registered_teardown",
    "make_teardown_matcher",
    "parse_debug_pod_metadata",
    "parse_drill_vehicle_markers",
    "recovery_anchor_offsets",
    "recovery_time_anchors",
    "VEHICLE_ARTIFACT_TYPES",
    "vehicle_artifact_types",
]
