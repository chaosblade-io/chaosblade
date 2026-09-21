"""Tool screener: gate ``execute_loop`` tool_calls against the approved target.

Slotted between ``execute_loop`` (the LLM node) and ``phase2_tools``
(the LangGraph ``ToolNode``). For every tool_call in the most recent
AIMessage:

  1. Classify the call into an ``EffectiveTarget`` via
     ``chaos_agent.agent.target_guard.infer_effective_target``.
  2. Compare against the snapshot in ``state.approved_target`` via
     ``target_drift_guard``.
  3. Aggregate verdicts and choose one of three routes:

     - ``pass``  — all calls allowed; ToolNode executes normally.
     - ``interrupt`` — at least one call drifted; pause graph via
                       interrupt() for human confirmation. Approve
                       corrects fault_spec + approved_target and passes;
                       reject retries (LLM gets one chance to
                       self-correct before hard termination).
     - ``retry`` — at least one call was BANNED/UNKNOWN; fabricate
                   ToolMessage rejections so the LLM sees the failure
                   and tries again next iteration. Route back to
                   ``execute_loop``.

Two operating modes governed by ``settings.target_guard_enforcing``:

  - **Enforcing** (default in production after grey rollout): the
    above logic runs as described. Rejections actually block tools.
  - **Log-only** (default before grey rollout finishes): the verdict
    is computed and logged at WARNING level for any non-ALLOW result,
    but the call is allowed to proceed to phase2_tools. Used to
    surface false-positives in production traffic before flipping
    enforcement on.

The screener emits a fabricated ToolMessage for EVERY tool_call in the
AIMessage when any one is rejected. LangChain's ToolNode would normally
do this matching; bypassing ToolNode means we have to satisfy the
"every tool_call needs a corresponding ToolMessage" invariant ourselves,
otherwise the next LLM iteration sees a malformed conversation.

Inside such a fabricated batch the REJECTED calls get the rejection
rendering, while calls the screener itself ALLOWED (allow / readonly
verdicts) get a DEFERRED rendering instead: the batch is atomic (no
call in it executed), and telling an allowed call it was "rejected —
adjust and retry" teaches the model wrong facts about its own call
(case-32: a READONLY update_progress answered with a READONLY rejection
read to the LLM like a guard verdict against the tool itself).
"""

from __future__ import annotations

import base64
import logging
import re
import shlex
import time
from dataclasses import replace
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import interrupt

from chaos_agent.agent.spec.fault_registry import carrier_actions, carrier_targets
from chaos_agent.agent.spec.fault_spec import read_fault_spec
from chaos_agent.agent.capabilities import explain_tool_refusal, tool_call_allowed
from chaos_agent.agent.execution_artifacts import (
    _RECOVERY_CARRIER_CREATE_KINDS,
    _exec_pod_identity,
    VEHICLE_ARTIFACT_TYPES,
    is_vehicle_name,
    is_vehicle_teardown_delete,
    vehicle_artifact_types,
)
from chaos_agent.agent.kubeconfig import resolve_kubeconfig
from chaos_agent.agent.nodes.execute.llm_step_helpers import hint_count_key
from chaos_agent.agent.nodes.execute.react_helpers import _stagnation_key
from chaos_agent.agent.state import AgentState
from chaos_agent.agent.state_mgmt.state_helpers import fail_state
from chaos_agent.agent.target_guard import (
    ApprovedTarget,
    ConfidenceLevel,
    EffectiveTarget,
    GuardDecision,
    GuardVerdict,
    approved_from_dict,
    freeze_approved_target_from_spec,
    infer_effective_target,
)
from chaos_agent.agent.target_guard.mechanism_writes import (
    RECOVERY_CHANNEL_APISERVER_WRITE,
    entries_from_list,
)
from chaos_agent.agent.target_guard.carriers import (
    LIVE_DISCOVERY_RETRYABLE_REASONS,
    CarrierResolution,
    discover_unregistered_carrier,
    effective_target_from_registered_carrier,
    is_host_carrier_call,
    registered_carrier_is_current,
)
from chaos_agent.agent.target_guard.classifier import (
    SCOPE_ESCAPE,
    SCOPE_READONLY,
    SCOPE_UNKNOWN,
    canonicalise_kind,
)
from chaos_agent.agent.providers.message_scanning import (
    KUBECTL_WRITE_SUBCOMMANDS,
)
from chaos_agent.agent.providers.registry import FaultProviderRegistry
from chaos_agent.agent.result.verdict import FailureCategory
from chaos_agent.config.settings import settings
from chaos_agent.tools.guard_gateway import decision_to_feedback, get_guard_gateway

logger = logging.getLogger(__name__)


def _experiment_uids_created_by_current_task(
    messages: list, state: AgentState | None = None,
) -> set[str]:
    """Return experiment UIDs proven by this task's create results.

    Carrier-neutral facade (Task B): the per-carrier evidence — a backend's
    own create ToolMessages (including failed-create CRDs that still need
    cleanup) plus its durable state record — lives in each provider's
    ``created_experiment_ids``; the registry unions them. This keeps the
    destroy-provenance gate free of carrier vocabulary, so a new backend's
    experiment ids join the whitelist by registration alone.

    Durability note: message evidence is not durable (compression removes old
    ToolMessages BY DESIGN); each provider also claims its durable state
    record, so the whitelist keeps proving provenance across compaction.
    """
    from chaos_agent.agent.providers import FaultProviderRegistry

    return FaultProviderRegistry.created_experiment_ids(messages, state or {})


async def _discover_vehicle_pods(
    state: AgentState, candidates: list[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """Live-discover which ``candidates`` are injection tool pods.

    Reuses the SAME label-selector discovery the baseline / conflict checks
    use (``discover_tool_pods_cluster_wide``): an all-namespace lookup of
    the ChaosBlade tooling labels, verified against the LIVE cluster. Pod
    identity is thus a cluster fact, not a naming convention — deployments
    that rename or relocate the tool DaemonSet are still recognised as long
    as they carry the tooling labels, and site-specific tool pods are
    covered by task-side registration (``kubectl_exec_pod_name``).

    Returns ``(positives, misses)``. A failed probe caches its candidates
    as misses: re-probing every screener iteration under an active network
    fault would ride the very API path the fault is severing; the fail-
    closed outcome (the call still reaches drift review) is safe.
    """
    from chaos_agent.tools.pod_discovery import (
        discover_tool_pods_cluster_wide,
    )

    kubeconfig = str(state.get("kubeconfig") or "")
    try:
        pods = await discover_tool_pods_cluster_wide(
            kubeconfig, str(state.get("task_id") or ""),
        )
    except Exception:
        logger.warning(
            "target_guard: vehicle live-discovery failed; candidates %s "
            "keep identity review (fail closed)", candidates,
        )
        return frozenset(), frozenset(candidates)
    discovered = {name for name, _ns in pods}
    positives = frozenset(n for n in candidates if n in discovered)
    misses = frozenset(n for n in candidates if n not in discovered)
    if positives:
        logger.info(
            "target_guard: live discovery confirmed injection vehicle(s) %s",
            sorted(positives),
        )
    return positives, misses


async def _resolve_exec_pod_node(
    state: AgentState, pod_name: str, pod_ns: str,
    vehicle_cache: dict[str, Any],
) -> str:
    """Resolve which node hosts ``pod_name`` (exec-vehicle node binding).

    A host-level ``blade create`` inside ``kubectl exec POD -- ...`` has no
    selector of its own — the fault lands on the pod's host node, so the
    guard's identity comparison needs that node name. One bounded in-band
    read per pod per task; the outcome is persisted in
    ``exec_pod_node_bindings`` (an empty node string caches a failed probe
    as a negative entry), so later screener rounds never re-probe — the
    same self-poisoning rationale as ``vehicle_probe_misses``.

    Returns the node name, or "" when it cannot be resolved (fail closed:
    the caller keeps the drift review).
    """
    cached = {
        pod: node
        for pod, node in (state.get("exec_pod_node_bindings") or ())
    }
    if pod_name in cached:
        return cached[pod_name]

    from chaos_agent.transports import (
        PROFILE_K8S,
        TransportTarget,
        execute_via_transport,
    )
    from chaos_agent.tools.kubectl_cli import build_kubectl_cmd

    kubeconfig = str(state.get("kubeconfig") or "")
    node = ""
    try:
        cmd = build_kubectl_cmd(
            "get",
            ["pod", pod_name, "-n", pod_ns or "default",
             "-o", "jsonpath={.spec.nodeName}"],
            kubeconfig=kubeconfig,
        )
        result = await execute_via_transport(
            cmd, TransportTarget.from_state({}),
            timeout=settings.timeout_kubectl,
            task_id=str(state.get("task_id") or ""),
            source="node-binding-check",
            expect_profile=PROFILE_K8S,
        )
        if getattr(result, "exit_code", 1) == 0:
            node = str(getattr(result, "stdout", "") or "").strip()
    except Exception:
        logger.warning(
            "target_guard: exec-pod node binding probe failed for %s/%s; "
            "keeping identity review (fail closed)", pod_ns, pod_name,
        )

    bindings = tuple(cached.items()) + ((pod_name, node),)
    vehicle_cache["exec_pod_node_bindings"] = bindings
    if node:
        logger.info(
            "target_guard: resolved exec-pod node binding %s/%s -> %s",
            pod_ns, pod_name, node,
        )
    return node


def _selector_probe_key(namespace: str, labels: dict[str, str]) -> str:
    """Stable cache key for a label-selector name probe."""
    return namespace + "|" + ",".join(
        f"{k}={v}" for k, v in sorted(labels.items())
    )


async def _resolve_label_pod_names(
    state: AgentState, namespace: str, labels: dict[str, str],
    vehicle_cache: dict[str, Any],
) -> tuple[str, ...]:
    """Resolve the pod names a label selector CURRENTLY matches.

    Powers the selector cross-shape resolution (labels-vs-names): the
    guard policy compares selectors statically and cannot see that an
    approved name set and an executed label selector pick the same pods,
    so the screener resolves the live set here, DATA-side — the same
    division of labour as the exec-pod node binding.

    One bounded in-band read per (namespace, selector) per task; the
    outcome is persisted in ``selector_name_probes`` (an empty tuple
    caches a failed or empty probe as a negative entry), so later
    screener rounds never re-probe — the same self-poisoning rationale
    as ``vehicle_probe_misses``: under an active network fault the probe
    would ride the very API path the fault is severing.

    Returns the matching pod names; empty means "no pass" (fail closed:
    the caller keeps the static drift review).
    """
    cached = dict(state.get("selector_name_probes") or ())
    key = _selector_probe_key(namespace, labels)
    if key in cached:
        return tuple(cached[key])

    from chaos_agent.transports import (
        PROFILE_K8S,
        TransportTarget,
        execute_via_transport,
    )
    from chaos_agent.tools.kubectl_cli import build_kubectl_cmd

    kubeconfig = str(state.get("kubeconfig") or "")
    selector = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    names: tuple[str, ...] = ()
    try:
        cmd = build_kubectl_cmd(
            "get",
            ["pods", "-n", namespace or "default", "-l", selector,
             "-o", "jsonpath={.items[*].metadata.name}"],
            kubeconfig=kubeconfig,
        )
        result = await execute_via_transport(
            cmd, TransportTarget.from_state({}),
            timeout=settings.timeout_kubectl,
            task_id=str(state.get("task_id") or ""),
            source="selector-name-probe",
            expect_profile=PROFILE_K8S,
        )
        if getattr(result, "exit_code", 1) == 0:
            names = tuple(
                str(getattr(result, "stdout", "") or "").split()
            )
    except Exception:
        logger.warning(
            "target_guard: selector name probe failed for %s -l %s; "
            "keeping identity review (fail closed)", namespace, selector,
        )

    vehicle_cache["selector_name_probes"] = (
        tuple(cached.items()) + ((key, names),)
    )
    if names:
        logger.info(
            "target_guard: resolved selector %s -l %s -> %s",
            namespace, selector, sorted(names),
        )
    return names


def _identity_matches_approved(
    effective: EffectiveTarget, approved: ApprovedTarget | None,
) -> bool:
    """Cheap structural match between an effective target and the approval.

    True means identity drift cannot fire for this call, so the vehicle
    block (probe + exemption) may be skipped entirely. That matters beyond
    efficiency: the discovery probe rides the in-band API path, which an
    injected network fault may already be severing — no probe may fire for
    an exec into the approved target itself. False negatives are harmless:
    at worst one extra fail-closed, cached probe.
    """
    if approved is None or not effective.names:
        return False
    if (effective.namespace or "default") != (approved.namespace or "default"):
        return False
    if approved.is_namespace_wide:
        return True
    known = (
        set(approved.names)
        | set(approved.resolved_names)
        | set(approved.owner_names)
    )
    return bool(known) and all(n in known for n in effective.names)


# Vehicles whose presence underwrites an object-write injection's
# bounded recovery, split by RECOVERY MODEL:
#   - ``recovery_carrier`` — a self-built timer host that patches a
#     PRE-EXISTING (stock) target back. Its recovery is the ARMED timer's
#     restore payload, so only ``recovery_armed`` underwrites; a bare
#     ``sleep N`` skeleton restores nothing (run6 inject-3d5de7fa).
#   - the drill-occupancy forms (``occupant_pod`` / ``occupant_deployment``)
#     — task-BUILT targets whose cleanup record deletes the asset, and the
#     fault riding it, wholesale. Teardown-by-deletion needs no timer, so an
#     ``active`` registration already underwrites recovery.
# A ``debug_pod`` is a PROBE channel, not a recovery underwriter, and is
# deliberately excluded.
_RECOVERY_UNDERWRITER_TYPES: frozenset[str] = frozenset(
    VEHICLE_ARTIFACT_TYPES - {"debug_pod"},
)
_RECOVERY_CARRIER_TYPE = "recovery_carrier"
_TEARDOWN_UNDERWRITER_TYPES: frozenset[str] = frozenset(
    _RECOVERY_UNDERWRITER_TYPES - {_RECOVERY_CARRIER_TYPE},
)


def _underwrites_bounded_recovery(a: dict[str, Any]) -> bool:
    """Whether one registered vehicle artifact underwrites a bounded recovery.

    Recovery-model split (see ``_RECOVERY_UNDERWRITER_TYPES``): a carrier
    must be ARMED (``recovery_armed``); a task-built drill occupant
    underwrites at ``active`` because its recovery is wholesale deletion.
    ``cleaned`` never counts — the vehicle is gone, and a fresh injection
    would again run un-recovered.
    """
    atype = a.get("type")
    status = a.get("status")
    if status == "cleaned":
        return False
    if atype == _RECOVERY_CARRIER_TYPE:
        return status == "recovery_armed"
    if atype in _TEARDOWN_UNDERWRITER_TYPES:
        return status in ("active", "recovery_armed")
    return False


def _recovery_vehicle_armed(
    vehicle_cache: dict[str, Any], state: AgentState,
) -> bool:
    """A recovery-underwriter vehicle can still reverse the fault.

    Reads the screening round's cache FIRST — the carrier ``run`` branch
    and the occupant registration register their artifacts at ALLOW time
    (screening precedes execution), so a same-batch vehicle + injection
    pair sees the registration even before the state delta lands.

    The per-artifact verdict is :func:`_underwrites_bounded_recovery`: a
    ``recovery_carrier`` counts ONLY when ``recovery_armed`` — a carrier pod
    sitting on its bare ``sleep N`` skeleton is registered but its timer host
    carries no restore payload, so it will never reverse the fault (run6
    inject-3d5de7fa: a bare-sleep carrier passed the old ``active``-tolerant
    face and the selector patch landed permanently unrecovered; the
    ``recovery_armed`` status is stamped by an ARMING EXEC into the carrier
    whose payload carries a timer form, see execution_artifacts
    ``_mark_recovery_armed``). A task-built drill occupant still underwrites
    at ``active`` — its cleanup deletes the asset and the fault wholesale, so
    it needs no timer.
    """
    artifacts = vehicle_cache.get(
        "execution_artifacts", state.get("execution_artifacts") or [],
    )
    return any(
        isinstance(a, dict) and _underwrites_bounded_recovery(a)
        for a in artifacts
    )


def _vehicle_delete_is_cleanup(
    tool_args: Any, effective: EffectiveTarget,
    vehicle_cache: dict[str, Any], state: AgentState,
) -> bool:
    """A delete naming only task-registered vehicle assets is TEARDOWN.

    Thin wrapper over the shared core
    (:func:`execution_artifacts.is_vehicle_teardown_delete`, extracted
    R6-1 so the issue-time attribution layer answers the SAME question):
    this side adds the screening-round cache as the artifacts source
    (the carrier ``run`` branch registers at ALLOW time, so a same-batch
    vehicle + delete pair sees the registration before the state delta
    lands) and the subcommand gate. The guard's identity verdict has
    already ALLOWed the call by the time this exemption runs — it only
    bypasses the carrier gate, never the drift net.
    """
    if not isinstance(tool_args, dict) or tool_args.get("subcommand") != "delete":
        return False
    artifacts = vehicle_cache.get(
        "execution_artifacts", state.get("execution_artifacts") or [],
    )
    return is_vehicle_teardown_delete(
        effective, artifacts,
        v_args=str(tool_args.get("v_args") or ""),
    )


def _carrier_family_in_write_set(approved: ApprovedTarget) -> bool:
    """The frozen write-set admits carrier-family objects for this target.

    The workload/pod nets' ``secondary_scopes`` statically include the
    RBAC family (every such approval can legally stack a carrier); the
    node net does not — its bounded recovery rides the host carrier
    (debug pod + systemd-run timer), deliberately outside this gate's
    object-write first cut. Case-manifest ``mechanism_entries``
    contribute their scopes the same way. Vocabulary is
    :data:`_RECOVERY_CARRIER_CREATE_KINDS`' value set — single source
    with the manifest-channel reshape branch and the artifact attach.
    """
    carrier_kinds = frozenset(_RECOVERY_CARRIER_CREATE_KINDS.values())
    write_set = set(approved.secondary_scopes or ())
    write_set.update(
        e.scope for e in (approved.mechanism_entries or ())
    )
    return bool(write_set & carrier_kinds)


def _declared_verbs_in_symmetric_revert_domain(approved: ApprovedTarget) -> bool:
    """Do the frozen intent verbs land in the symmetric-revert carrier's vocabulary?

    FALLBACK PROXY (D3 source 3's temporary M2 stand-in — tasks.md 2.4:
    the gate could not depend on case metadata that M3 task 3.1 had not
    written yet). The caller consults the frozen snapshot's explicit
    ``recovery_channel`` declaration FIRST and only reaches this proxy
    when no declaration exists: the proxy classifies by TAXONOMY VERBS,
    and a k8s-native mechanism whose verbs happen to land in the blade
    vocabulary (NXDOMAIN: target=network action=dns — blade's dns action
    is a HIJACK to an IP, it has no rcode forgery) is symmetric-revert
    UNreachable despite the verb hit (run8 inject-2a8cd99a: the proxy's
    rejection deadlocked the executor — the suggested blade route had no
    equivalent action). Only the case legislation can distinguish the
    two; the proxy stays as the no-declaration fallback.

    The ChaosBlade carrier vocabulary (cpu/mem/network/disk/process ×
    fullload/load/…) is the mechanical projection of the symmetric-revert
    routing class: a fault expressible as a blade experiment is recovered
    by ``blade destroy`` on its experiment UID — no apiserver write happens
    in its recovery, so it cannot justify the declarative-restore CR
    channel (whose entire value is apiserver-write recovery). EITHER the
    frozen ``fault_target`` or ``fault_action`` matching the vocabulary
    marks the domain: the two carrier vocabularies (chaosblade vs
    k8s_native) are disjoint on both axes, so a single hit is decisive —
    and a target like ``cpu`` paired with a k8s-native action (or vice
    versa) is still a blade-shaped fault the plan mis-declared. Empty
    verbs on both axes mean "not decidable": the write-set admission
    (case-manifest ``mechanism_entries``) remains the guard there, and
    this fallback does not block what it cannot classify.
    """
    bt = str(approved.fault_target or "").strip().lower()
    ba = str(approved.fault_action or "").strip().lower()
    if not bt and not ba:
        return False
    blade_targets = set(carrier_targets("chaosblade"))
    blade_actions = set(carrier_actions("chaosblade"))
    return bt in blade_targets or ba in blade_actions


# REST method → RBAC verb (recovery-carrier.md §2 form-agnostic rule;
# #51/B85: the grant must follow the payload's ACTUAL write verbs, not
# the form the plan pinned at design time).
_REST_WRITE_VERB_BY_METHOD = {
    "PATCH": "patch",
    "PUT": "update",
    "DELETE": "delete",
    "POST": "create",
}

# ``curl -X <METHOD>`` in an arming payload — either spelling (``-X PATCH``
# separate, ``-XPATCH`` fused). Case-sensitive on the flag itself: curl's
# lowercase ``-x`` is the PROXY flag and must not match.
_CURL_METHOD_RE = re.compile(r"-X\s*['\"]?(\w+)")

# A long base64 run inside an arming payload. The local-encode two-step
# (recovery-carrier.md §7 iron rule 2 / tier table ③ fallback: ``echo <b64> |
# base64 -d >/tmp/r.sh; sh /tmp/r.sh``) is a LEGAL staging form whose payload
# carries the restore verbs only in encoded form — the layer-1 reconcile
# must read through it or the grant check goes blind exactly where #51 bit.
# 32+ chars of the base64 alphabet excludes ordinary shell words (URLs break
# on ``:``/``.``; JWT/CA blobs decode to non-curl bytes and reconcile to
# nothing), so the probe is idempotent on plain payloads.
_B64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{32,}={0,2}")

# K8s review-family APIs are QUERY-shaped POSTs: the body carries the
# access question, the response carries the verdict, and NOTHING persists
# — the apiserver writes no cluster state for them (authorization.k8s.io:
# selfsubjectaccessreviews / selfsubjectrulesreviews / subjectaccessreviews
# / localsubjectaccessreviews; authentication.k8s.io: tokenreviews; all
# granted globally via system:basic-user, so no carrier Role can or should
# carry their verbs). §3's SSAR arm-time gate MANDATES these probes inside
# the carrier payload — a bare method→verb mapping would have layer 1
# reject layer 2's own legislation (live in #51-R: gate=carrier_verb_
# reconcile refused the SSAR probe's POST as write verb [create], forcing
# a pointless Role grant to get past the guard).
_NO_STATE_POST_RESOURCES = frozenset({
    "selfsubjectaccessreviews",
    "selfsubjectrulesreviews",
    "subjectaccessreviews",
    "localsubjectaccessreviews",
    "tokenreviews",
})

# Shell command separators — each curl invocation (and therefore each
# invocation's URL) lives inside its own segment, never in a neighbour's.
_CURL_CMD_SPLIT_RE = re.compile(r";|&&|\|\||\n")

# A review-family resource in URL path position: ``/selfsubjectaccessreviews``
# etc. (API paths are lowercase; the ``\b`` keeps ``...reviews2``-shaped names
# from matching). Alternation order puts the longer ``self*`` spellings
# first, though ``/subject...`` cannot match inside ``/selfsubject...``
# anyway because the required ``/`` never precedes the embedded substring.
_NO_STATE_POST_URL_RE = re.compile(
    r"/(?:" + "|".join(sorted(_NO_STATE_POST_RESOURCES)) + r")\b"
)


def _verbs_in_payload_text(payload: str) -> set[str]:
    """REST write verbs carried by ``curl -X`` spellings in raw text.

    The payload is split into shell command segments (``;`` / ``&&`` /
    ``||`` / newline) so each ``-X METHOD`` is judged together with its own
    invocation's URL: a POST aimed at a review-family resource is a
    query-shaped call (no cluster state changes) and carries no write verb
    the grant must cover; every other method keeps the §2 form-agnostic
    method→verb mapping. Residual fail-open edge (same family as the
    documented shard-split boundary): one curl invocation listing BOTH a
    real write URL and a review URL keeps its POST exempt — layer-2 SSAR
    at arm time owns the remainder.
    """
    verbs: set[str] = set()
    for segment in _CURL_CMD_SPLIT_RE.split(payload):
        no_state_post = _NO_STATE_POST_URL_RE.search(segment) is not None
        for method in _CURL_METHOD_RE.findall(segment):
            verb = _REST_WRITE_VERB_BY_METHOD.get(method.upper())
            if verb == "create" and no_state_post:
                continue
            if verb:
                verbs.add(verb)
    return verbs


def _carrier_restore_write_verbs(v_args: str) -> set[str]:
    """Write verbs the exec payload AFTER ``--`` actually uses (curl -X).

    Everything past the first ``--`` is the pod-side payload (``sh -c
    '...'`` with its restore curls inside); only its REST WRITE methods
    map to RBAC verbs — GET probes and log cats reconcile to nothing.

    The local-encode base64 two-step is decoded too (iron rule 2): the
    verbs live inside the encoded blob, and a blind spot here re-opens
    the #51 leak through a md-sanctioned staging form. Review-family
    POSTs are exempt from verb extraction in BOTH readings (plain and
    decoded) — the §3 SSAR probe itself is not a restore write. Residual
    edge: a blob SPLIT across segment-staged execs (each exec lands one
    shard) hides a verb cut at the shard boundary — single-exec vision
    cannot reconcile cross-command content, same fail-open boundary as
    the staged-script form documented in ``_screen_carrier_restore_verbs``.
    """
    _, _, payload = v_args.partition("--")
    if not payload:
        return set()
    verbs = _verbs_in_payload_text(payload)
    for run in _B64_RUN_RE.findall(payload):
        try:
            decoded = base64.b64decode(run + "=" * (-len(run) % 4))
        except (ValueError, TypeError):
            continue
        verbs |= _verbs_in_payload_text(decoded.decode("utf-8", "replace"))
    return verbs


def _registered_recovery_carrier(
    vehicle_cache: dict[str, Any], state: AgentState, pod_name: str,
) -> dict | None:
    """The registered recovery-carrier artifact named by an exec target.

    Cache-first with a STATE default (``get`` default-value form, same as
    ``_recovery_vehicle_armed``): the cache key only ever lands
    populated (ALLOW-time registration), but the get-default form keeps
    the two readers' semantics identical — an empty cached list would
    shadow state under an ``or`` form, silently hiding registrations.
    """
    artifacts = vehicle_cache.get(
        "execution_artifacts", state.get("execution_artifacts") or [],
    )
    for artifact in artifacts:
        if (
            isinstance(artifact, dict)
            and artifact.get("type") == "recovery_carrier"
            and artifact.get("name") == pod_name
        ):
            return artifact
    return None


def _screen_carrier_restore_verbs(
    tool_args: Any,
    vehicle_cache: dict[str, Any],
    state: AgentState,
) -> tuple[str, str] | None:
    """B85 layer-1 admission check: arming payload ⊆ registered grant.

    The #51/B85 failure form: the plan pinned PUT, the agent lawfully
    switched the restore form at arm time (2 PATCH + 1 DELETE), and the
    stack's Role still granted ``get,update`` — the timer fired into 403s
    and the pod "recovered" without recovering. The form-agnostic rule
    (recovery-carrier.md §2) legislates the behaviour; THIS is the
    code-level fail-closed assertion of it — an exec into a REGISTERED
    carrier whose payload carries REST write verbs must see every one of
    them inside the registered Role/ClusterRole verbs (create-time
    ``--verb`` + json-patch additions, folded by
    ``execution_artifacts._extend_recovery_carrier_rbac_verbs``).

    Returns ``(reason, suggestion)`` when the grant is missing verbs,
    ``None`` to admit. Deliberate fail-OPEN scope (layer-2 SSAR at arm
    time owns the remainder): an unregistered pod (other guards judge
    it), a payload with no write verbs (read probes / log cats), a
    carrier with no Role member on record (manifest-built stack, resumed
    pre-verbs artifact), or an empty grant parse — each reconciles
    nothing and passes with a warning where the blind spot is real.
    """
    if not isinstance(tool_args, dict) or tool_args.get("subcommand") != "exec":
        return None
    v_args = str(tool_args.get("v_args") or "")
    try:
        tokens = shlex.split(v_args)
    except ValueError:
        return None
    # Pod identity lives BEFORE the ``--`` separator only — the payload
    # after it may itself contain ``-n``-looking tokens (curl URLs, sh
    # flags), which must never be misread as the exec's namespace flag
    # (same outer/inner split as ``_mark_bounded_host_recovery``).
    separator = tokens.index("--") if "--" in tokens else len(tokens)
    pod_name, _namespace = _exec_pod_identity(tokens[:separator])
    if not pod_name:
        return None
    payload_verbs = _carrier_restore_write_verbs(v_args)
    if not payload_verbs:
        return None
    carrier = _registered_recovery_carrier(vehicle_cache, state, pod_name)
    if carrier is None:
        return None
    granted: set[str] = set()
    has_role_member = False
    for member in carrier.get("rbac_family") or []:
        if (
            isinstance(member, dict)
            and member.get("kind") in ("role", "clusterrole")
        ):
            has_role_member = True
            granted.update(
                str(verb).strip().lower()
                for verb in member.get("verbs") or []
                if str(verb).strip()
            )
    # RBAC's ``verbs: ["*"]`` wildcard grants EVERY verb — reconcile to
    # full admission (a literal set-difference would false-reject every
    # payload verb against the "*" string).
    if "*" in granted:
        return None
    if not has_role_member or not granted:
        logger.warning(
            "carrier-verb-reconcile: carrier %s armed with write verbs "
            "%s but no parseable Role verbs on record (rbac_family=%r) "
            "— passing; §3 SSAR at arm time owns the check",
            pod_name, sorted(payload_verbs), carrier.get("rbac_family"),
        )
        return None
    missing = sorted(payload_verbs - granted)
    if not missing:
        return None
    return (
        "carrier-verb-reconcile (B85): the arming payload's write verbs "
        f"[{','.join(missing)}] are not in the registered carrier "
        f"Role/ClusterRole verbs [{','.join(sorted(granted))}] — arming "
        "now would let the timer fire into 403s and a pod that reports "
        "recovery without recovering",
        "Reconcile the grant to the payload's ACTUAL write verbs "
        "(form-agnostic rule, references/carrier/recovery-carrier.md §2): "
        "kubectl patch the carrier's Role/ClusterRole (two-step json-patch "
        "rule form) to add the missing verbs, then re-issue this arming "
        "exec. The §3 SSAR token probe still applies before arm.",
    )


def _screen_vehicle_manifest(
    effective: EffectiveTarget, approved: ApprovedTarget | None,
) -> GuardDecision:
    """Screen an occupant-vehicle Pod manifest against the approval anchor.

    The classifier already enforced the occupant contract (sleep-only
    command, no privilege, PVC-only volumes, bounded deadline) — this gate
    answers the IDENTITY question: does the occupancy land on resources the
    drill target actually uses?

    The anchor is ``approved.pvc_claims``: the PVC claim names the frozen
    target's pods reference, discovered by safety_check at freeze time. The
    occupant must claim a non-empty SUBSET of them, in the approved
    namespace. There is deliberately NO cluster-side marker to identify a
    vehicle (the drill must stay indistinguishable from a real incident),
    so claims are the only anchor — without them there is nothing to verify
    against, and the mechanism stays banned (fail closed → replan).
    """
    if approved is None or not approved.pvc_claims:
        return GuardDecision(
            verdict=GuardVerdict.REJECT_BANNED,
            reason=(
                "occupant vehicle manifests are only permitted when the "
                "approved target's PVC claims are known; no claim anchor "
                "exists for this approval"
            ),
            suggestion="",
            effective=replace(effective, mechanism_banned=True),
        )
    if (effective.namespace or "default") != (approved.namespace or "default"):
        return GuardDecision(
            verdict=GuardVerdict.REJECT_BANNED,
            reason=(
                f"occupant pod namespace {effective.namespace or 'default'} "
                f"is outside the approved namespace "
                f"{approved.namespace or 'default'}"
            ),
            suggestion=(
                f"apply the occupant pod in namespace "
                f"{approved.namespace or 'default'}"
            ),
            effective=effective,
        )
    approved_claims = set(approved.pvc_claims)
    outside = sorted(set(effective.occupant_claims) - approved_claims)
    if outside:
        return GuardDecision(
            verdict=GuardVerdict.REJECT_BANNED,
            reason=(
                "occupant pod claims PVC(s) not used by the approved "
                f"target: {', '.join(outside)}"
            ),
            suggestion=(
                "the occupant may only claim PVCs of the approved target: "
                + ", ".join(sorted(approved_claims))
            ),
            effective=effective,
        )
    return GuardDecision(
        verdict=GuardVerdict.ALLOW,
        reason=(
            "occupant vehicle manifest approved: claims "
            + ", ".join(sorted(set(effective.occupant_claims)))
            + " belong to the approved target"
        ),
        suggestion="",
        effective=effective,
    )


def _screen_destroy_uid_provenance(
    uid: str,
    effective: EffectiveTarget,
    messages: list,
    state: AgentState | None = None,
) -> GuardDecision:
    """The provenance check shared by BOTH destroy delivery faces.

    ALLOW only when the UID was produced by this graph task — message
    evidence plus the durable birth registry (``owned_experiment_uids``
    union ``experiment_uid``), which inline ``kubectl exec ... blade
    create`` receipts register into too, so the in-cluster recovery
    path the registry itself instructs stays usable. Empty and foreign
    UIDs both fail closed. The REJECT carries the tool face's original
    wording so the model reads one consistent lesson whichever channel
    its cleanup attempt rode.
    """
    if uid and uid in _experiment_uids_created_by_current_task(messages, state):
        return GuardDecision(
            verdict=GuardVerdict.ALLOW,
            reason="experiment UID was created by this task",
            effective=effective,
        )
    return GuardDecision(
        verdict=GuardVerdict.REJECT_UNKNOWN,
        reason="blade_destroy UID was not produced by this task's blade_create",
        effective=effective,
        suggestion=(
            "Only clean the UID reported by the current failed blade_create "
            "call."
        ),
    )


def _screen_blade_destroy(
    tool_args: Any, messages: list, state: AgentState | None = None,
) -> tuple[EffectiveTarget, GuardDecision]:
    """Allow cleanup only for an experiment created by this graph task."""
    args = tool_args if isinstance(tool_args, dict) else {}
    uid = str(args.get("uid") or "").strip()
    effective = EffectiveTarget(
        scope="__blade_cleanup__",
        namespace="",
        confidence=ConfidenceLevel.HIGH,
        raw_command=f"blade_destroy uid={uid}",
    )
    return effective, _screen_destroy_uid_provenance(
        uid, effective, messages, state,
    )


# Sentinel used by ``route_after_screener`` to dispatch to the right
# successor node. Cleared each time the screener runs so a stale
# value can't leak into a later iteration.
SCREENER_ROUTE_PASS = "pass"
SCREENER_ROUTE_REPLAN = "replan"
SCREENER_ROUTE_RETRY = "retry"
# Hard-termination route: unlike RETRY (loop back and keep running), FAIL
# routes to the reject terminal node. A hard stop that kept returning RETRY
# was a ghost termination (W-56-5 defect a, #56): the graph kept executing,
# the fail_state error leaked into the next attempt (should_continue_agent_loop
# rejects on any set error), and the terminal renderer stitched two stale
# reasons together.
SCREENER_ROUTE_FAIL = "fail"


def _carrier_within_liveness_window(
    artifact: Any, from_registered: bool, current_task_id: str,
) -> bool:
    """Whether a registered carrier is fresh enough to skip the live re-probe.

    ``registered_carrier_is_current`` re-reads the pod via ``kubectl get pod``
    to reject stale/recreated carriers. Under an in-band network fault that
    probe rides the very API path the fault is severing, so it times out and
    turns "injection actually cut the link" into a false "carrier unavailable"
    rejection. When this task itself created the debug pod and confirmed it
    active within ``carrier_liveness_ttl_seconds``, we trust the in-memory
    registration and skip the probe.

    Only the *registered* path qualifies: live-discovered carriers carry no
    ``confirmed_live_epoch`` / ``task_id`` and must still be probed. ``ttl<=0``
    disables the window entirely (always probe = pre-optimization behaviour),
    and the explicit ``ttl > 0`` guard avoids a degenerate float-equality skip.
    """
    if not from_registered or not isinstance(artifact, dict):
        return False
    if artifact.get("status") != "active":
        return False
    ttl = int(getattr(settings, "carrier_liveness_ttl_seconds", 0) or 0)
    if ttl <= 0:
        return False
    if not current_task_id or artifact.get("task_id") != current_task_id:
        return False
    epoch = artifact.get("confirmed_live_epoch")
    if not isinstance(epoch, (int, float)):
        return False
    return (time.time() - float(epoch)) <= ttl


_HARD_BLOCK_MARK = "refused as stagnant"


def _blocked_on_previous_attempt(state: AgentState, tool_name: str) -> bool:
    """Did the immediately preceding attempt at this tool get hard-blocked?

    Used to make the block ALTERNATE rather than latch. A permanent block is the
    wrong shape: the subcommand is often legitimately needed again once the model
    changes angle, and refusing forever converts "stop repeating" into "you may
    never look at this again" — which the model cannot satisfy and which would
    make an otherwise recoverable drill unfinishable.

    Alternating breaks the streak (every other attempt fails) while keeping the
    call reachable. Derived from the rejection message the previous block already
    left in history, so no extra state field is needed and the two cannot drift
    apart: the evidence IS the previous decision.

    Scans backwards to the most recent result for this tool and asks only about
    THAT one — an older block separated by successful calls is not "the previous
    attempt" and must not grant a free pass now.
    """
    for msg in reversed(state.get("messages", []) or []):
        if getattr(msg, "type", "") != "tool":
            continue
        if (getattr(msg, "name", "") or "") != tool_name:
            continue
        content = getattr(msg, "content", "")
        return _HARD_BLOCK_MARK in (content if isinstance(content, str) else "")
    return False


def _hard_stagnation_block(
    state: AgentState, tool_name: str, tool_args,
) -> tuple[str, str]:
    """Refuse a call whose ``tool:subcommand`` has exhausted the soft warnings.

    Returns ``(reason, suggestion)``, both empty when the call may proceed.

    Keyed through ``_stagnation_key`` — the SAME function the detector uses — so
    the block covers exactly what was warned about. Deriving the key here
    independently is how a block ends up refusing a call nobody warned about, or
    letting through the one that was.

    The block ALTERNATES: an attempt right after a blocked one is let through.
    See :func:`_blocked_on_previous_attempt` for why latching is the wrong shape.

    Read-only on state: the count is written by the loop nodes when they issue
    the hint. This function only decides whether the count has passed the point
    where notices were shown not to work.
    """
    if not settings.target_guard_enforcing:
        # The guard's global switch governs refusals; in log-only mode a stuck
        # model is a diagnosis, not something to block.
        return "", ""
    counts = state.get("hint_repeat_counts") or {}
    if not counts:
        return "", ""
    key = _stagnation_key(tool_name, tool_args if isinstance(tool_args, dict) else {})
    issued = 0
    try:
        issued = int(counts.get(hint_count_key("stagnation", key), 0) or 0)
    except (TypeError, ValueError):
        return "", ""
    threshold = int(settings.hint_escalate_after or 0)
    if threshold <= 0 or issued <= threshold:
        return "", ""
    if _blocked_on_previous_attempt(state, tool_name):
        # Just blocked — let this one through so the model can act on a changed
        # angle. If it repeats again, the next attempt is blocked again.
        return "", ""

    _, _, sub = key.partition(":")
    what = f"'{tool_name}' with subcommand '{sub}'" if sub else f"'{tool_name}'"
    reason = (
        f"{what} was {_HARD_BLOCK_MARK}: the stagnation notice was issued "
        f"{issued} times for this exact call shape and the call kept repeating, "
        f"so this attempt is refused instead of warned about again"
    )
    suggestion = (
        "This is a refusal, not advice — but it alternates: the NEXT attempt at "
        "this call will be allowed, and blocked again after that if nothing "
        "changes. Use that opening deliberately. Two moves remain: use a "
        "DIFFERENT "
        + ("subcommand or tool" if sub else "tool")
        + " to obtain the information, or stop gathering and state your "
        "conclusion from the evidence already collected. Being unable to observe "
        "further is itself a reportable conclusion."
    )
    return reason, suggestion


def _register_drill_target_artifact(
    vehicle_cache: dict[str, Any],
    state: AgentState,
    effective: EffectiveTarget,
    tool_call_id: str,
) -> None:
    """Register a drill-target Deployment as a task-side vehicle artifact.

    Registration tracks EXECUTION, not just the ALLOW verdict — the callers
    are the screener's ALLOW branch, a human-approved drift card (the drifted
    apply executes once approved), and log-only mode's pass-through sweep.
    An idempotent re-apply (the LLM re-issues the same manifest after a
    timeout) keeps the FIRST registration rather than clobbering it — dedup
    by artifact_id, same discipline as the occupant and carrier branches.
    """
    dt_name = effective.names[0] if effective.names else ""
    if not dt_name:
        return
    dt_ns = effective.namespace or "default"
    # P5: the recorded ``kind`` field is the teardown predicate's
    # authoritative source (the ``_deployment`` suffix derivation is a
    # legacy-artifact fallback only) — register it like every other
    # vehicle constructor does.
    target_artifact = {
        "artifact_id": f"occupant_deployment:{dt_ns}/{dt_name}",
        "type": "occupant_deployment",
        "kind": "deployment",
        "status": "active",
        "task_id": str(state.get("task_id") or ""),
        "name": dt_name,
        "namespace": dt_ns,
        "operation_family": "drill_target",
        "created_tool_call_id": tool_call_id,
        "cleanup": {
            "tool": "kubectl",
            "subcommand": "delete",
            "v_args": (
                f"deployment {dt_name} -n {dt_ns} "
                "--ignore-not-found"
            ),
        },
    }
    merged: dict[str, dict] = {
        str(a.get("artifact_id") or ""): a
        for a in vehicle_cache.get(
            "execution_artifacts",
            state.get("execution_artifacts") or [],
        )
        if isinstance(a, dict)
    }
    if target_artifact["artifact_id"] not in merged:
        merged[target_artifact["artifact_id"]] = target_artifact
    vehicle_cache["execution_artifacts"] = list(merged.values())


async def _refresh_carrier_images_if_starved(state: AgentState) -> None:
    """L: refill a starved carrier-image discovery set, never raising.

    Run5 lesson (openspec faultdrill-cr-channel 3.4 finding ⑥): the
    discovery half of the carrier image allowlist is process-lifetime
    state whose ONLY writer was the preplan probe — one gateway jitter
    there starved both sync consumers (the carrier-run shape check and
    the drill-target manifest T4 check) for the whole process, and on a
    VPC cluster without docker.io egress the configured-only allowlist
    is a dead end no LLM adaptation can escape. This gives the starved
    state its execute-time second chance BEFORE classification, so the
    call's own verdict and the dispatch-time ToolGuard re-check both
    see the repopulated set (``settings`` is process-wide). The
    once-per-process bound lives in the refresh itself; any failure is
    non-fatal — the verdict then falls back to exactly the pre-change
    behaviour (configured-only allowlist).
    """
    if str(settings.recovery_carrier_discovered_images or "").strip():
        return
    from chaos_agent.agent.nodes.gates.preplan_probe import (
        refresh_carrier_image_discovery,
    )

    try:
        await refresh_carrier_image_discovery(resolve_kubeconfig(state))
    except Exception:  # noqa: BLE001 — admission enrichment, never fatal
        logger.debug(
            "carrier-image lazy refresh failed (non-fatal)", exc_info=True,
        )


async def tool_screener(state: AgentState) -> dict:
    """Inspect pending tool_calls and decide whether to forward them.

    Returns a state delta. The delta always sets ``screener_route`` so
    the conditional edge can dispatch deterministically; it may also
    append synthetic ``ToolMessage`` responses (for REJECT/BANNED cases)
    or interrupt for human confirmation (for DRIFT cases).

    Fail-open policy: if the screener itself throws (classifier crash
    on malformed args, unexpected tool_call shape, etc.) the whole
    in-flight turn would die. We catch at the per-tool_call boundary,
    log the exception, and treat the offending call as ALLOW. The
    alternative — fail-closed — would let a classifier bug take
    production down. Operator sees ERROR-level logs and can intervene.
    """
    messages = state.get("messages", [])
    last_msg = messages[-1] if messages else None

    # Truncated response: execute_loop already neutralised the batch — parseable
    # calls were answered with a synthetic error, unparseable ones were stripped.
    # Route straight back to the loop so the ToolNode never sees it.
    #
    # Gated on there being no UNANSWERED batch pending: that is the flag's
    # precondition. A stale flag (from a turn that exited via replan/end without
    # passing a screener) would otherwise divert a FRESH batch, and the loop would
    # spin without those calls ever running.
    if state.get("truncated_tool_calls"):
        pending = isinstance(last_msg, AIMessage) and getattr(last_msg, "tool_calls", None)
        if not pending:
            return {
                "screener_route": SCREENER_ROUTE_RETRY,
                "truncated_tool_calls": False,
            }
        logger.warning(
            "tool_screener: stale truncated_tool_calls flag with an unanswered "
            "batch pending — clearing and screening normally",
        )

    # Create-reconcile gate hold (blade-create-reconcile-before-retry D6):
    # execute_loop held a whole batch after a same-fingerprint gate-armed
    # create retry (result-uncertain protection; which creates are
    # gate-armed is declared provider-side, consumed through the registry
    # seam) and fabricated its answers — route straight back to the loop
    # so the ToolNode never sees it. Same staleness precondition as the
    # truncated flag above.
    if state.get("_reconcile_gate_blocked"):
        pending = isinstance(last_msg, AIMessage) and getattr(last_msg, "tool_calls", None)
        if not pending:
            return {
                "screener_route": SCREENER_ROUTE_RETRY,
                "_reconcile_gate_blocked": False,
            }
        logger.warning(
            "tool_screener: stale _reconcile_gate_blocked flag with an "
            "unanswered batch pending — screening normally",
        )

    # Defensive: no tool_calls to screen → pass through. This shouldn't
    # happen in practice because ``should_continue_execute_loop`` only
    # routes to "continue" when the last AIMessage has tool_calls, but
    # belt-and-braces.
    if not isinstance(last_msg, AIMessage) or not getattr(last_msg, "tool_calls", None):
        return {"screener_route": SCREENER_ROUTE_PASS, "truncated_tool_calls": False}

    approved = approved_from_dict(state.get("approved_target"))
    enforcing = bool(settings.target_guard_enforcing)
    skill_script_allowed = bool(settings.skill_script_default_allow)

    decisions: list[dict[str, Any]] = []
    has_drift = False
    has_other_reject = False
    has_provenance_reject = False
    has_context_reject = False
    # Vehicle live-discovery cache for this screening round + the state
    # delta persisting its outcome (positive and negative alike).
    cluster_vehicles: frozenset[str] = frozenset(
        state.get("known_vehicle_pods") or (),
    )
    probe_misses: frozenset[str] = frozenset(
        state.get("vehicle_probe_misses") or (),
    )
    probed_this_round = False
    vehicle_cache: dict[str, Any] = {}
    for tc in last_msg.tool_calls:
        tool_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
        tool_args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
        tool_call_id = (
            tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
        ) or ""
        # Which carrier gate refused, when one did. Carried to the single
        # rejection-logging outlet below (same pattern as ``constraint``) so a
        # stuck drill can be grouped by GATE in logs instead of by prose that
        # may be reworded.
        carrier_gate = ""

        # Carrier-image admission resilience (run5 lesson, finding ⑥):
        # the image-allowlist-consuming subcommands get one execute-time
        # discovery refill when the set is starved — BEFORE classification
        # (see _refresh_carrier_images_if_starved). ``run`` feeds the
        # carrier-run shape check and ``apply`` the drill-target manifest
        # T4 check — the two direct allowlist consumers. ``create`` consumes
        # nothing image-wise itself (sa/role stack, CR objects) but rides
        # the same carrier build-out path, so the trigger set is
        # deliberately a superset: the once-per-process bound keeps the
        # over-trigger cost one probe per process, while a strict set
        # could miss a first-contact shape (e.g. a CR apply written as
        # ``create -f``).
        if (
            tool_name == "kubectl"
            and isinstance(tool_args, dict)
            and tool_args.get("subcommand") in ("run", "apply", "create")
        ):
            await _refresh_carrier_images_if_starved(state)

        # Shared capability verdict (fail-CLOSED), see capabilities.context.
        if not tool_call_allowed(tool_name, state, "execute"):
            # The verdict alone ("unavailable for the current environment
            # capability profile") is the SAME sentence for every tool in every
            # profile — it names neither the profile in force, nor the one the
            # tool belongs to, nor what to use instead. Ask the layer that made
            # the judgement for the actual cause.
            _cap_reason, _cap_fix = explain_tool_refusal(tool_name, state, "execute")
            decisions.append({
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "verdict": GuardVerdict.REJECT_UNKNOWN.value,
                "reason": _cap_reason,
                "suggestion": _cap_fix,
                "effective": None,
            })
            has_other_reject = True
            has_context_reject = True
            continue

        # Hard stagnation block. The soft path (a hint appended to the turn) is
        # the only lever ``filter_stagnant_tool`` leaves for SUBCOMMAND-level
        # stagnation, because removing the whole tool would blind the phase. That
        # lever assumes the model reads the hint and reconsiders — and
        # task-ff057e7f showed what happens when it does not: 100 iterations,
        # ~20 consecutive notices, and reasoning_content present on 2 of 100
        # turns. A model that is not reasoning cannot be reached by text, so no
        # number of reminders was ever going to work; only refusing the call can.
        #
        # The threshold is the escalation point itself, not a further grace
        # period: escalation already means "overwriting the notice has been shown
        # not to change behaviour", and there is no evidence that more notices
        # after that help. This runs in the screener rather than at bind time
        # because a subcommand is an ARGUMENT — there is nothing to unbind.
        _blocked_reason, _blocked_fix = _hard_stagnation_block(
            state, tool_name, tool_args,
        )
        if _blocked_reason:
            decisions.append({
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                # Its OWN verdict, not REJECT_UNKNOWN: the call is admissible
                # and this refusal alternates, so labelling it "unknown to the
                # classifier" would teach the model the tool is unavailable.
                "verdict": GuardVerdict.REJECT_STAGNANT.value,
                "reason": _blocked_reason,
                "suggestion": _blocked_fix,
                "effective": None,
            })
            has_other_reject = True
            carrier_gate = "stagnation_hard_block"
            continue

        try:
            if tool_name == "blade_destroy":
                effective, decision = _screen_blade_destroy(tool_args, messages, state)
                if decision.verdict != GuardVerdict.ALLOW:
                    has_provenance_reject = True
                feedback = decision_to_feedback(decision)
            else:
                effective = infer_effective_target(
                    tool_name, tool_args,
                    skill_script_allowed=skill_script_allowed,
                )
                # B85 layer-1 (fail-closed, admission time): an arming
                # exec into a REGISTERED recovery carrier must reconcile
                # its payload's REST write verbs against the registered
                # Role/ClusterRole grant — the form-agnostic rule as a
                # code assertion, ahead of the drift net so the refusal
                # names the missing verb instead of a generic drift.
                _b85 = (
                    _screen_carrier_restore_verbs(
                        tool_args, vehicle_cache, state,
                    )
                    if tool_name == "kubectl"
                    else None
                )
                if _b85:
                    _b85_reason, _b85_fix = _b85
                    decisions.append({
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "verdict": GuardVerdict.REJECT_BANNED.value,
                        "reason": _b85_reason,
                        "suggestion": _b85_fix,
                        "is_hard_floor": False,
                        "constraint": "",
                        "carrier_gate": "carrier_verb_reconcile",
                        "effective": effective,
                    })
                    has_other_reject = True
                    continue
                if effective.blade_destroy_uid:
                    # Inline ``kubectl exec ... blade destroy/revoke``
                    # (twelfth-round E3): the classifier extracted the UID;
                    # run the SAME provenance gate the blade_destroy tool
                    # face rides. The gate owns the verdict — the ordinary
                    # drift net (and carrier resolution) is skipped below
                    # via the ``blade_destroy_uid`` marker, so a provenance
                    # ALLOW is not re-judged by the UNKNOWN scope and a
                    # provenance REJECT keeps its own reason intact. The
                    # decision then flows through the standard append /
                    # reject accounting / routing below, exactly like the
                    # tool face's.
                    decision = _screen_destroy_uid_provenance(
                        effective.blade_destroy_uid, effective,
                        messages, state,
                    )
                    if decision.verdict != GuardVerdict.ALLOW:
                        has_provenance_reject = True
                    feedback = decision_to_feedback(decision)
            if tool_name != "blade_destroy" and effective.is_vehicle_manifest:
                # Occupant-vehicle apply: an identity verdict of its own
                # (claims-vs-approval anchor) instead of check_target — the
                # occupant's name can never match the approved target's
                # identity, so the standard drift comparison would always
                # fire here by construction.
                decision = _screen_vehicle_manifest(effective, approved)
                # The decision may carry a rebuilt effective (e.g. the
                # mechanism_banned marker for the no-anchor case) — that is
                # the one the rejection renderer must see.
                effective = decision.effective or effective
                feedback = decision_to_feedback(decision)
                if decision.verdict == GuardVerdict.ALLOW:
                    # Register the occupant as a vehicle artifact NOW
                    # (screening precedes execution): its identity is only
                    # tracked task-side — no label marks it in the cluster
                    # — so the finalize/recover cleanup depends entirely on
                    # this registration to delete it. Dedup by artifact_id:
                    # a screening round replays the pending batch as a whole.
                    occ_name = effective.names[0] if effective.names else ""
                    occ_ns = effective.namespace or "default"
                    occ_type = "occupant_pod"
                    occupant_artifact = {
                        "artifact_id": f"{occ_type}:{occ_ns}/{occ_name}",
                        "type": occ_type,
                        "status": "active",
                        "task_id": str(state.get("task_id") or ""),
                        "name": occ_name,
                        "namespace": occ_ns,
                        "claims": list(effective.occupant_claims),
                        "operation_family": "resource_occupancy",
                        "created_tool_call_id": tool_call_id,
                        "cleanup": {
                            "tool": "kubectl",
                            "subcommand": "delete",
                            "v_args": (
                                f"pod {occ_name} -n {occ_ns} "
                                "--ignore-not-found"
                            ),
                        },
                    }
                    merged: dict[str, dict] = {
                        str(a.get("artifact_id") or ""): a
                        for a in vehicle_cache.get(
                            "execution_artifacts",
                            state.get("execution_artifacts") or [],
                        )
                        if isinstance(a, dict)
                    }
                    merged[occupant_artifact["artifact_id"]] = occupant_artifact
                    vehicle_cache["execution_artifacts"] = list(merged.values())
                decisions.append({
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "verdict": decision.verdict.value,
                    "reason": decision.reason,
                    "suggestion": decision.suggestion,
                    "is_hard_floor": feedback.is_hard_floor,
                    "constraint": feedback.constraint.value,
                    "carrier_gate": "vehicle_manifest",
                    "effective": effective,
                })
                if decision.verdict in (
                    GuardVerdict.REJECT_BANNED, GuardVerdict.REJECT_UNKNOWN,
                ):
                    has_other_reject = True
                continue
            if tool_name != "blade_destroy" and effective.is_recovery_carrier:
                # Recovery-carrier run (recovery-carrier-standard): judged by
                # the ORDINARY net — the shape marker from the classifier
                # exempts nothing by itself. In-net pod secondary scope +
                # same namespace is the whole anchor (design D3/D7); a
                # carrier run outside the net keeps the standard drift
                # verdict and its routing, unchanged. ALLOW additionally
                # registers the pod as a task-side vehicle artifact
                # (occupant pattern — screening precedes execution): the
                # registration is what makes later execs into the carrier
                # (token probe / timer arm / re-arm) vehicle-exempt and
                # finalize/recover's stack cleanup delete pod + RBAC family.
                rc_decision, rc_feedback = get_guard_gateway().check_target(
                    effective, approved,
                )
                if rc_decision.verdict == GuardVerdict.ALLOW:
                    rc_name = effective.names[0] if effective.names else ""
                    rc_ns = effective.namespace or "default"
                    if rc_name:
                        carrier_artifact = {
                            "artifact_id": (
                                f"recovery_carrier:{rc_ns}/{rc_name}"
                            ),
                            "type": "recovery_carrier",
                            "kind": "pod",
                            "status": "active",
                            "task_id": str(state.get("task_id") or ""),
                            "name": rc_name,
                            "namespace": rc_ns,
                            "operation_family": "recovery_carrier",
                            "created_tool_call_id": tool_call_id,
                            # sa/role/rolebinding members attach here as
                            # their successful ``kubectl create`` results
                            # arrive (execution_artifacts collects them).
                            "rbac_family": [],
                            # The four-way stack delete, recorded for the
                            # audit trail (the executor lives in
                            # ``cleanup_debug_pod_artifacts``). Delete order
                            # mirrors it: pod, then binding → role → sa.
                            "cleanup": [
                                {
                                    "tool": "kubectl",
                                    "subcommand": "delete",
                                    "v_args": (
                                        f"pod {rc_name} -n {rc_ns} "
                                        "--ignore-not-found"
                                    ),
                                },
                                {
                                    "tool": "kubectl",
                                    "subcommand": "delete",
                                    "v_args": (
                                        f"rolebinding {rc_name} -n {rc_ns} "
                                        "--ignore-not-found"
                                    ),
                                },
                                {
                                    "tool": "kubectl",
                                    "subcommand": "delete",
                                    "v_args": (
                                        f"role {rc_name} -n {rc_ns} "
                                        "--ignore-not-found"
                                    ),
                                },
                                {
                                    "tool": "kubectl",
                                    "subcommand": "delete",
                                    "v_args": (
                                        f"serviceaccount {rc_name} -n {rc_ns} "
                                        "--ignore-not-found"
                                    ),
                                },
                            ],
                        }
                        merged: dict[str, dict] = {
                            str(a.get("artifact_id") or ""): a
                            for a in vehicle_cache.get(
                                "execution_artifacts",
                                state.get("execution_artifacts") or [],
                            )
                            if isinstance(a, dict)
                        }
                        # A repeat ALLOW for the SAME carrier (LLM re-issues
                        # the run after a timeout, or an idempotency probe)
                        # must NOT clobber the registered artifact: rbac_family
                        # members collected from create results and the
                        # recovery_armed/deadline stamped by an arming exec
                        # are durable facts. Resetting them would make
                        # finalize's keep-while-armed cleanup delete a live
                        # timer host (family + armed state lost = immediate
                        # stack delete) — the re-run is AlreadyExists noise.
                        if carrier_artifact["artifact_id"] not in merged:
                            merged[carrier_artifact["artifact_id"]] = (
                                carrier_artifact
                            )
                        vehicle_cache["execution_artifacts"] = (
                            list(merged.values())
                        )
                    decisions.append({
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "verdict": rc_decision.verdict.value,
                        "reason": rc_decision.reason,
                        "suggestion": rc_decision.suggestion,
                        "is_hard_floor": rc_feedback.is_hard_floor,
                        "constraint": rc_feedback.constraint.value,
                        "carrier_gate": "recovery_carrier",
                        "effective": effective,
                    })
                    continue
                # Non-ALLOW: fall through to the standard path so the drift
                # interrupt / retry rendering keeps its exact pre-existing
                # behaviour — the carrier marker must not change rejection
                # routing.
            if tool_name != "blade_destroy" and effective.is_drill_target_manifest:
                # Drill-target Deployment apply (drill-target-contract): the
                # classifier already enforced the shape contract; this gate
                # is the ORDINARY drift net — unlike an occupant (whose
                # generated name can never match the approval) the drill
                # target's name IS the approved identity, so check_target
                # alone anchors it: name+namespace match = ALLOW, anything
                # else = standard drift rejection. ALLOW additionally
                # registers the deployment as a task-side
                # ``occupant_deployment`` vehicle artifact (screening
                # precedes execution): that registration is what
                # finalize/recover's cleanup chain deletes when the task dies
                # without an explicit teardown, and what the recovery
                # delete's deployment-kind exemption keys on (the identity
                # match alone would already ALLOW the delete — the
                # registration is the durable, drift-independent belt).
                dt_decision, dt_feedback = get_guard_gateway().check_target(
                    effective, approved,
                )
                if dt_decision.verdict == GuardVerdict.ALLOW:
                    _register_drill_target_artifact(
                        vehicle_cache, state, effective, tool_call_id,
                    )
                    decisions.append({
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "verdict": dt_decision.verdict.value,
                        "reason": dt_decision.reason,
                        "suggestion": dt_decision.suggestion,
                        "is_hard_floor": dt_feedback.is_hard_floor,
                        "constraint": dt_feedback.constraint.value,
                        "carrier_gate": "drill_target_manifest",
                        "effective": effective,
                    })
                    continue
                # Non-ALLOW: fall through to the standard path so the drift
                # interrupt / retry rendering keeps its exact pre-existing
                # behaviour — the drill-target marker must not change
                # rejection routing.
            if (
                tool_name != "blade_destroy"
                # Inline destroy carries its own provenance verdict; carrier
                # resolution would REPLACE the UNKNOWN-with-uid effective and
                # silently drop the uid marker, re-opening the E3 bypass via a
                # registered exec vehicle.
                and not effective.blade_destroy_uid
                and (
                effective.scope in (SCOPE_UNKNOWN, SCOPE_ESCAPE)
                or (
                    is_host_carrier_call(tool_name, tool_args)
                    # A host-entry wrapper (chroot/nsenter) around a READ-ONLY
                    # inner command is a diagnostic probe, not an injection. The
                    # classifier already resolved it to __readonly__ (its inner
                    # argv is read-only AND carries no shell metacharacter — the
                    # same test host_inject uses for its skip-guard fast path).
                    # Routing it into carrier resolution anyway made
                    # ``classify_host_operation`` return an empty family and the
                    # call was rejected as an "uncleared host-escape primitive"
                    # — wrongly, since it mutates nothing. A mutating inner
                    # command is classified __escape__ (not __readonly__) and
                    # still enters carrier resolution here.
                    and effective.scope != SCOPE_READONLY
                )
                )
            ):
                carrier_resolved = False
                try:
                    carrier_resolution = effective_target_from_registered_carrier(
                        tool_name,
                        tool_args,
                        state.get("execution_artifacts"),
                        approved,
                    )
                except Exception as exc:
                    logger.exception(
                        "target_guard: carrier resolution failed; keeping call UNKNOWN"
                    )
                    carrier_resolution = CarrierResolution.errored(exc)
                # A carrier resolved here comes from this task's in-memory
                # registration; the live-discovery fallback below does not
                # qualify for the freshness window.
                carrier_from_registered = carrier_resolution.resolved
                # Fallback: live discovery for unregistered debug pods
                # (e.g. kubectl debug timed out before emitting metadata).
                # By exec time the pod is guaranteed to exist — the LLM
                # saw it in kubectl get pods before attempting exec.
                #
                # Only gates that a live read could actually overturn are
                # retried (see ``LIVE_DISCOVERY_RETRYABLE_REASONS``). A
                # FAMILY_MISMATCH / NO_BOUNDED_RECOVERY verdict is about the
                # COMMAND, so re-reading the cluster cannot change it — and it
                # would additionally let a synthetic (family-less) artifact
                # bypass the registered carrier's ``operation_family`` check.
                if (
                    carrier_resolution.reason in LIVE_DISCOVERY_RETRYABLE_REASONS
                    and is_host_carrier_call(tool_name, tool_args)
                ):
                    try:
                        carrier_resolution = await discover_unregistered_carrier(
                            tool_name,
                            tool_args,
                            state,
                            approved,
                        )
                    except Exception as exc:
                        logger.debug(
                            "target_guard: live carrier discovery failed",
                            exc_info=True,
                        )
                        carrier_resolution = CarrierResolution.errored(exc)
                if carrier_resolution.resolved:
                    carrier_effective = carrier_resolution.effective
                    artifact = carrier_resolution.artifact or {}
                    # A carrier that came from the live-discovery fallback (i.e.
                    # NOT from in-memory registration) was JUST confirmed by a
                    # fresh in-band ``kubectl get pod`` inside
                    # ``discover_unregistered_carrier`` (privileged + approved
                    # node + uid). Re-probing it via
                    # ``registered_carrier_is_current`` would be a redundant
                    # second in-band read on the very API path a network fault is
                    # severing — self-poisoning. Trust the discovery probe.
                    carrier_from_live_discovery = not carrier_from_registered
                    if carrier_from_live_discovery or _carrier_within_liveness_window(
                        artifact,
                        carrier_from_registered,
                        state.get("task_id", ""),
                    ):
                        # Either a freshly live-discovered carrier (already
                        # probed once) or a fresh, this-task, active registered
                        # carrier within the liveness window: trust it and skip
                        # the live re-probe, which under an in-band network fault
                        # would time out on the same API path the fault is
                        # severing (self-poisoning).
                        effective = carrier_effective
                        carrier_resolved = True
                    else:
                        try:
                            carrier_is_current = await registered_carrier_is_current(
                                artifact, state,
                            )
                        except Exception as exc:
                            logger.exception(
                                "target_guard: registered carrier verification failed; "
                                "keeping call UNKNOWN"
                            )
                            # "The re-read raised" and "the re-read disagreed"
                            # are different facts, and only one was observed.
                            # Reporting a mismatch we never saw would repeat, at
                            # a smaller scale, the misattribution this whole
                            # change removes.
                            carrier_resolution = CarrierResolution.verification_failed(
                                str(artifact.get("name") or "<unknown>"), exc,
                            )
                            carrier_is_current = False
                        if carrier_is_current:
                            effective = carrier_effective
                            carrier_resolved = True
                        elif carrier_resolution.resolved:
                            # Probe completed and disagreed (it did not raise, so
                            # ``carrier_resolution`` is still the resolved one).
                            carrier_resolution = CarrierResolution.stale(
                                str(artifact.get("name") or "<unknown>"),
                            )
                if is_host_carrier_call(tool_name, tool_args) and not carrier_resolved:
                    # Forward the gate's OWN account of the refusal. The
                    # screener does NOT infer it: resolution used to answer
                    # ``tuple | None``, and this branch guessed "pod not
                    # registered" for all ~12 gates — which is how
                    # task-866648cc was told its (correctly registered) debug
                    # pod was unapproved while the real gate was a missing
                    # self-reversal on an otherwise valid ``tc netem`` command.
                    carrier_gate = (
                        carrier_resolution.reason.value
                        if carrier_resolution.reason else "unknown"
                    )
                    effective = EffectiveTarget(
                        scope=SCOPE_ESCAPE,
                        namespace="",
                        confidence=ConfidenceLevel.UNKNOWN,
                        raw_command=effective.raw_command,
                        reject_detail=carrier_resolution.detail,
                        reject_suggestion=carrier_resolution.suggestion,
                    )
            if (
                tool_name != "blade_destroy"
                and effective.scope in ("pod", "deployment")
                and not effective.is_vehicle_exec
                and effective.names
                and not _identity_matches_approved(effective, approved)
            ):
                # An exec into an injection VEHICLE is machinery access, not
                # an operation on the fault target — exempt it from identity
                # drift. Vehicle identity is DATA-driven, never name-based:
                #   1. task-registered vehicles (``is_vehicle_name``: debug
                #      pod artifacts, kubectl_exec_pod_name, meta tags);
                #   2. LIVE cluster discovery for pods this task never
                #      registered (e.g. the ChaosBlade tool DaemonSet), one
                #      bounded probe per screening round, outcome persisted.
                # The probe runs on the fault-binary branch too: a drift
                # verdict that SURVIVES there can still be human-approved,
                # and ``_apply_drift_correction`` needs the discovered
                # identity to refuse rewriting the contract toward
                # machinery. Only the exemption FLAG is withheld from that
                # branch — a fault binary inside a privileged / hostNetwork
                # tool pod shapes the HOST and keeps identity review.
                unregistered = [
                    n for n in effective.names
                    if not is_vehicle_name(n, state)
                    and n not in cluster_vehicles
                    and n not in probe_misses
                    # Deployment-scoped vehicles (occupant deployments) are
                    # only recognisable via task-side registration — the
                    # tool-pod label probe below discovers PODS, so probing
                    # a deployment name can only ever return a miss.
                    and not (
                        effective.scope == "deployment"
                        and "occupant_deployment" in vehicle_artifact_types(
                            n, state,
                        )
                    )
                ]
                if (
                    unregistered
                    and effective.scope == "pod"
                    and not probed_this_round
                ):
                    probed_this_round = True
                    positives, misses = await _discover_vehicle_pods(
                        state, unregistered,
                    )
                    cluster_vehicles = cluster_vehicles | positives
                    probe_misses = probe_misses | misses
                    new_known = positives - frozenset(
                        state.get("known_vehicle_pods") or (),
                    )
                    if new_known:
                        vehicle_cache["known_vehicle_pods"] = tuple(
                            (state.get("known_vehicle_pods") or ())
                            + tuple(sorted(new_known)),
                        )
                    if misses:
                        vehicle_cache["vehicle_probe_misses"] = tuple(
                            sorted(
                                frozenset(
                                    state.get("vehicle_probe_misses") or (),
                                ) | misses,
                            ),
                        )
                if not effective.fault_binary_mutation and all(
                    is_vehicle_name(n, state)
                    or n in cluster_vehicles
                    or (
                        # A registered occupant deployment exempts a
                        # deployment-scoped operation on it (its recovery
                        # deletion); pod-type vehicles never match here.
                        effective.scope == "deployment"
                        and "occupant_deployment"
                        in vehicle_artifact_types(n, state)
                    )
                    for n in effective.names
                ):
                    # EffectiveTarget is frozen, so rebuild instead of mutating.
                    effective = replace(effective, is_vehicle_exec=True)
            if (
                tool_name != "blade_destroy"
                and effective.scope == "node"
                and not effective.names
                and not effective.labels
                and effective.exec_pod_name
                and approved is not None
            ):
                # Exec-vehicle node binding: a host-level ``blade create``
                # inside a tool pod carries no selector — the fault lands on
                # the pod's host node. Resolve that node DATA-side and pin
                # it as the effective name so the identity comparison works
                # on a cluster fact instead of rejecting the selector-less
                # shape as drift (task-ccfadf7d: an approved node-mem load
                # executed in the node's own tool pod was misread as
                # "resource selection drift"). A binding that resolves
                # OUTSIDE the approved set is genuine drift; one that
                # cannot be resolved keeps the fail-closed review.
                approved_name_set = approved.names or approved.resolved_names
                if approved_name_set:
                    bound_node = await _resolve_exec_pod_node(
                        state,
                        effective.exec_pod_name,
                        effective.exec_pod_namespace,
                        vehicle_cache,
                    )
                    if bound_node and bound_node in approved_name_set:
                        effective = replace(effective, names=(bound_node,))
            if tool_name != "blade_destroy" and approved is not None:
                # Selector cross-shape resolution (labels vs names): the
                # policy layer compares selectors statically — a call that
                # selects the SAME pods through a different selector shape
                # than the approval can only be rejected as drift there
                # (drift_policy documents the limitation). The screener has
                # cluster access, so it resolves the live correspondence
                # DATA-side before the comparison, exactly like the node
                # binding above. Both directions stay strictly inside the
                # approval: the resolved set must be a subset of the
                # approved name set (A: labels executed under a names-only
                # approval), or the executed names must be live members of
                # the approved labels (B: pod churn under a labels
                # approval). Anything wider, unresolvable, or from a failed
                # probe keeps the fail-closed drift review.
                approved_name_set = approved.names or approved.resolved_names
                if (
                    effective.scope == "pod"
                    and canonicalise_kind(approved.scope) == "pod"
                    and effective.labels
                    and not effective.names
                    and approved_name_set
                    and not approved.labels
                ):
                    probe_ns = effective.namespace or approved.namespace
                    resolved = await _resolve_label_pod_names(
                        state, probe_ns, dict(effective.labels),
                        vehicle_cache,
                    )
                    if resolved and all(
                        n in approved_name_set for n in resolved
                    ):
                        effective = replace(effective, names=tuple(resolved))
                elif (
                    effective.scope == "pod"
                    and canonicalise_kind(approved.scope) == "pod"
                    and effective.names
                    and not effective.labels
                    and approved.labels
                    and not all(
                        n in approved_name_set for n in effective.names
                    )
                ):
                    # No ``approved_name_set`` precondition here: a labels
                    # approval that was NEVER resolved at freeze time has an
                    # empty name set, and ``all(n in ())`` is False for any
                    # executed name — exactly the shape that needs the live
                    # probe most. The subset verdict comes from the probe.
                    resolved = await _resolve_label_pod_names(
                        state, approved.namespace or effective.namespace,
                        dict(approved.labels), vehicle_cache,
                    )
                    resolved_set = set(resolved)
                    if resolved_set and all(
                        n in resolved_set for n in effective.names
                    ):
                        # Refresh the frozen resolution to the live set;
                        # the labels stay authoritative, so the policy's
                        # names-subset check validates against CURRENT
                        # members of the approved selector. Local rebuild
                        # only — the state's approval snapshot is untouched.
                        approved = replace(
                            approved,
                            resolved_names=tuple(sorted(resolved_set)),
                        )
            if tool_name != "blade_destroy" and not effective.blade_destroy_uid:
                # Single funnel: identity / recoverability verdict via the
                # gateway. ``decision`` drives routing (drift interrupt /
                # retry); ``feedback`` is the uniform shape rendered + audited
                # — reused below, never recomputed.
                decision, feedback = get_guard_gateway().check_target(
                    effective, approved,
                )
                # Armed-before-inject gate (inject-cc2d5080): a kubectl
                # OBJECT-WRITE injection (the verb itself is the mutation,
                # ``KUBECTL_WRITE_SUBCOMMANDS`` — the same single-source
                # vocabulary the issue-time attribution uses) carries no
                # experiment UID and no self-timeout, so its ONLY bounded
                # recovery is the recovery-carrier timer the plan stacks
                # (a REST-reversible object write can always construct the
                # SA carrier path — no exemption form exists). In that
                # task the carrier stack was refused (manifest-channel RBAC
                # mislabel), the replan review then demanded "issue the
                # injection call first", and the fault landed with no timer
                # armed — this gate is the graph-level invariant that fires
                # regardless of what any prompt or review text says.
                # Gated on the write-set admitting the carrier family
                # (workload/pod nets) so the node domain's host-carrier
                # timer forms stay outside this first cut. Deliberately
                # narrow elsewhere: command-mode exec/debug injections
                # excluded (their timer forms are case-legislated, not
                # structurally provable here). The gate's underwriter test
                # (:func:`_recovery_vehicle_armed`) splits by recovery model:
                # a ``recovery_carrier`` (timer host patching a STOCK target
                # back) must be ARMED (status ``recovery_armed``, stamped by
                # the arming exec), NOT merely registered — a carrier sitting
                # on its bare ``sleep N`` skeleton is refused until the arming
                # exec lands (run6 inject-3d5de7fa: the old ``active``-tolerant
                # face let a bare-sleep carrier through and the selector patch
                # landed permanently unrecovered). A task-built drill occupant
                # still underwrites at ``active``: its cleanup deletes the
                # asset and the fault wholesale, so it needs no timer.
                # A delete naming only task-registered vehicle assets is
                # teardown, never an injection, and is exempt
                # (:func:`_vehicle_delete_is_cleanup`, F1-C — the §6
                # four-way delete replay after the sweep marked the
                # artifact ``cleaned`` was refused with "stack the carrier
                # FIRST", ordering the model to re-build the asset class
                # it is deleting).
                # NOT mechanism_banned: stacking the carrier IS the
                # reshape, so the rejection must render as retryable form,
                # not a hard floor.
                if (
                    decision.verdict == GuardVerdict.ALLOW
                    and tool_name == "kubectl"
                    and isinstance(tool_args, dict)
                    and tool_args.get("subcommand") in KUBECTL_WRITE_SUBCOMMANDS
                    and approved is not None
                    and _carrier_family_in_write_set(approved)
                    and not _recovery_vehicle_armed(vehicle_cache, state)
                    and not _vehicle_delete_is_cleanup(
                        tool_args, effective, vehicle_cache, state,
                    )
                ):
                    decision = GuardDecision(
                        verdict=GuardVerdict.REJECT_BANNED,
                        reason=(
                            "armed-before-inject: this object-write "
                            "injection has no ARMED recovery vehicle (a "
                            "carrier registered on a bare `sleep N` "
                            "skeleton is not armed) — a kubectl-native "
                            "fault carries no UID and no self-timeout, so "
                            "issuing it now would leave no bounded recovery "
                            "at all"
                        ),
                        effective=effective,
                        suggestion=(
                            "Arm a recovery carrier BEFORE re-issuing this "
                            "injection. (1) If none is stacked yet: kubectl "
                            "run drill-rc-* --restart=Never --command -- "
                            "sleep N + imperative create for the "
                            "SA/Role/RoleBinding (recovery-carrier.md §1). "
                            "(2) ARM its timer via exec — the bare sleep "
                            "skeleton restores nothing: kubectl exec "
                            "<carrier> -n <ns> -- sh -c '( sleep <window>; "
                            "<restore: curl -X PATCH the target via the "
                            "carrier SA token> ) >/tmp/restore.log 2>&1 & "
                            "echo armed' — this stamps the carrier "
                            "recovery_armed; the SA-token pre-auth and the "
                            "exact restore form are in recovery-carrier.md "
                            "§3-4 (knowledge: recovery-carrier-arming.md). "
                            "Arm immediately before the fault lands; the "
                            "countdown starts at arming. (3) THEN re-issue "
                            "the injection. If the carrier genuinely cannot "
                            "be armed, request_replan with kind=safety — an "
                            "honest failure, not an un-armed injection."
                        ),
                    )
                    feedback = decision_to_feedback(decision)
                    carrier_gate = "armed_before_inject"
                # CR-channel route gate (openspec faultdrill-cr-channel,
                # design D3 source 3): a FaultDrill CR creation (kubectl
                # apply/create whose stdin manifest is all-FaultDrill
                # documents — the classifier anchors it as
                # scope="faultdrill") that has ALREADY passed write-set
                # admission (ALLOW here means a case-manifest
                # mechanism_entries faultdrill entry covered the call —
                # the widened contract a human approved) still has to
                # survive the three-way routing check. The channel is
                # reserved for faults whose recovery MUST write apiserver
                # state (recovery_channel: apiserver-write); routing into
                # it is a CASE-SEMANTIC decision (D3: not derivable from
                # fault_spec mechanically), and every upstream source can
                # be wrong — the case author can mis-declare the channel,
                # the planning prompt can be ignored. THIS gate is the
                # programmatic fallback that does not trust either — but
                # it does trust the third programmatic source: the frozen
                # snapshot's case-file ``recovery_channel`` declaration
                # (D3 source 1, loaded by code from the settled case at
                # freeze time — not an LLM input). Declaration first:
                # ``apiserver-write`` admits the route (the case author
                # legislated the recovery address; the verb-vocabulary
                # proxy below is only the NO-declaration fallback, and
                # run8 inject-2a8cd99a proved why — its taxonomy verbs
                # target=network action=dns hit the blade vocabulary
                # while the mechanism had no blade equivalent). Without
                # a declaration, declared fault verbs landing in the
                # ChaosBlade carrier vocabulary mean the fault is presumed
                # symmetric-revert reachable (blade destroy recovers it by
                # experiment UID — zero apiserver writes), so the CR
                # channel's declarative-restore machinery is the wrong
                # route for it (spec scenario "零工坊 case 误路由被审批门
                # 拒绝"). The rejection reports the routing conflict
                # WITHOUT prescribing a mechanism — whether a blade
                # equivalent action exists is feasibility knowledge the
                # planning layer has and this gate does not.
                # Host-domain mis-routes never reach here structurally:
                # a host-scope approval fails the guard's cross-profile
                # check before this point. Only the CREATING verbs
                # (apply/create) are gated — a delete/patch of the CR is
                # the recovery / re-recipe path whose admission the
                # manifest entries already govern. Disabled flag: while
                # faultdrill_enabled is False the provider is not even
                # registered — an attempted CR apply would carry no
                # attribution, no ledger recipe and no recovery path at
                # all, so it is rejected outright regardless of verb
                # domain (parity with the pre-change kind-ban: still
                # rejected, never silently admitted). The
                # installability consult (D2/D7, the old third branch)
                # retired with the CR channel's ensure_crd wiring (M2
                # task 2.4): an admitted migration-window apply is
                # governed by its own not-landed error family, and its
                # recovery rides the task ledger either way.
                # The verdict is retryable form guidance, same family as the
                # armed-before-inject gate above: the reshape is a
                # re-plan onto the correct channel, not a mechanism ban.
                if (
                    decision.verdict == GuardVerdict.ALLOW
                    and tool_name == "kubectl"
                    and isinstance(tool_args, dict)
                    and tool_args.get("subcommand") in ("apply", "create")
                    and effective.scope == "faultdrill"
                    and approved is not None
                ):
                    if not settings.faultdrill_enabled:
                        decision = GuardDecision(
                            verdict=GuardVerdict.REJECT_BANNED,
                            reason=(
                                "cr-channel route: the FaultDrill carrier "
                                "is not enabled (faultdrill_enabled=false) "
                                "— the provider is unregistered, so this "
                                "CR apply would carry no attribution, no "
                                "ledger recipe and no recovery path"
                            ),
                            effective=effective,
                            suggestion=(
                                "Re-plan onto the standard recovery-carrier "
                                "SOP form (references/carrier/"
                                "recovery-carrier.md) — the FaultDrill "
                                "carrier only carries drills while "
                                "faultdrill_enabled is on."
                            ),
                        )
                        feedback = decision_to_feedback(decision)
                        carrier_gate = "cr_channel_route"
                    elif (
                        approved.recovery_channel != RECOVERY_CHANNEL_APISERVER_WRITE
                        and _declared_verbs_in_symmetric_revert_domain(approved)
                    ):
                        # Verb-proxy fallback ONLY — the frozen snapshot
                        # carries no case-file ``recovery_channel:
                        # apiserver-write`` declaration. The explicit
                        # declaration (D3 source 1, loaded by code from
                        # the settled case file at freeze time) OUTRANKS
                        # the proxy: the proxy classifies by taxonomy
                        # verbs, and a k8s-native mechanism whose verbs
                        # happen to land in the blade vocabulary
                        # (NXDOMAIN: target=network action=dns — blade
                        # only has dns HIJACK, no rcode forgery; run8
                        # inject-2a8cd99a deadlocked here) is only
                        # distinguishable via the case legislation. A
                        # declared case skips this branch and rides the
                        # installability check below.
                        decision = GuardDecision(
                            verdict=GuardVerdict.REJECT_BANNED,
                            reason=(
                                "cr-channel route: the declared fault verbs "
                                f"(target={approved.fault_target or ''!r} "
                                f"action={approved.fault_action or ''!r}) "
                                "fall in the ChaosBlade symmetric-revert "
                                "domain — blade destroy recovers such faults "
                                "by experiment UID with zero apiserver "
                                "writes — and the frozen snapshot carries no "
                                "case-file recovery_channel declaration "
                                "outranking that inference, so the faultdrill "
                                "CR channel (reserved for recovery_channel: "
                                "apiserver-write cases) cannot be justified "
                                "for this route"
                            ),
                            effective=effective,
                            suggestion=(
                                "Re-plan this fault on the route that fits "
                                "its ACTUAL recovery address — the mechanism "
                                "decision belongs to planning, not this "
                                "gate: a symmetric-revert-reachable fault "
                                "recovers via blade destroy on its "
                                "experiment UID (see references/carrier/"
                                "recovery-carrier.md for the route forms). "
                                "If this case's recovery genuinely must "
                                "write apiserver state (e.g. the blade "
                                "vocabulary has no equivalent action for "
                                "the mechanism), the case file is missing "
                                "its recovery_channel: apiserver-write "
                                "front-matter legislation — route the "
                                "recovery-carrier SOP form instead; the "
                                "CR channel stays closed until the case "
                                "declares otherwise."
                            ),
                        )
                        feedback = decision_to_feedback(decision)
                        carrier_gate = "cr_channel_route"
        except Exception as exc:
            if is_host_carrier_call(tool_name, tool_args):
                logger.exception(
                    "target_guard: host carrier screening crashed; failing closed"
                )
                decisions.append({
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "verdict": GuardVerdict.REJECT_UNKNOWN.value,
                    "reason": (
                        "host carrier safety classification failed: "
                        f"{exc.__class__.__name__}"
                    ),
                    "suggestion": "Use a registered, current execution carrier.",
                    "effective": None,
                })
                has_other_reject = True
                continue
            # Fail-open: classifier or guard crashed. Log loudly so
            # the bug surfaces, but don't kill the turn — produce an
            # ALLOW decision for this tool_call. The pre-existing
            # safety layers (safety_check, confirmation_gate) still
            # gate the broader plan.
            logger.exception(
                "target_guard: screener crashed on tool=%s args=%r; "
                "failing open (allowing the call)",
                tool_name, tool_args,
            )
            decisions.append({
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "verdict": "allow",  # treated as ALLOW for routing
                "reason": f"screener exception: {exc.__class__.__name__}: {exc}",
                "suggestion": "",
                "effective": None,
            })
            continue

        decisions.append({
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "verdict": decision.verdict.value,
            "reason": decision.reason,
            "suggestion": decision.suggestion,
            "is_hard_floor": feedback.is_hard_floor,
            "constraint": feedback.constraint.value,
            "carrier_gate": carrier_gate,
            "effective": effective,
        })

        if decision.verdict == GuardVerdict.REJECT_DRIFT:
            has_drift = True
        elif decision.verdict in (
            GuardVerdict.REJECT_BANNED, GuardVerdict.REJECT_UNKNOWN,
        ):
            has_other_reject = True

    any_reject = has_drift or has_other_reject

    # Log every non-ALLOW outcome so operators can audit false-positives
    # before flipping enforcement on. Logging happens regardless of mode.
    for d in decisions:
        if d["verdict"] in ("allow", "readonly"):
            continue
        _gate = d.get("carrier_gate") or ""
        logger.warning(
            "target_guard: %s [%s] tool=%s%s reason=%s%s",
            d["verdict"], d.get("constraint", "-"), d["tool_name"],
            f" gate={_gate}" if _gate else "",
            d["reason"],
            "" if enforcing else " (log-only, enforcement disabled)",
        )

    # Log-only mode: pass through regardless of verdicts. The CLEANUP chain
    # is orthogonal to enforcement: a drifted (or otherwise rejected)
    # drill-target apply still executes here and must still register —
    # registration follows execution. ALLOW-verdict registrations already
    # happened per-call above; this sweep catches the non-ALLOW ones the
    # pass-through is about to run (dedup makes re-registering the
    # already-registered calls a no-op).
    if (
        not enforcing
        and not has_provenance_reject
        and not has_context_reject
    ) or not any_reject:
        for d in decisions:
            d_eff = d.get("effective")
            if d_eff is not None and getattr(
                d_eff, "is_drill_target_manifest", False,
            ):
                _register_drill_target_artifact(
                    vehicle_cache, state, d_eff, d["tool_call_id"],
                )
        return {"screener_route": SCREENER_ROUTE_PASS, **vehicle_cache}

    # Enforcing mode + at least one reject — fabricate ToolMessages so
    # the LangChain conversation stays well-formed (every tool_call
    # needs a matching response) and the LLM sees the failure text.
    # Cleared siblings get the DEFERRED rendering (B43): the batch is
    # atomic, but an allowed call must not be told it was rejected.
    rejection_msgs = [
        ToolMessage(
            content=(
                _format_deferred_for_llm(d)
                if d["verdict"] in _CLEARED_VERDICTS
                else _format_rejection_for_llm(d, approved is None, approved)
            ),
            name=d["tool_name"],
            tool_call_id=d["tool_call_id"],
            status="error",
        )
        for d in decisions
    ]

    # --- Drift path: interrupt for human confirmation ---
    if has_drift:
        drifted = [d for d in decisions if d["verdict"] == GuardVerdict.REJECT_DRIFT.value]
        first_eff = drifted[0].get("effective") if drifted else None
        drift_reject_count = int(state.get("drift_reject_count") or 0)

        if drift_reject_count >= 1:
            # Already rejected once — hard terminate.
            # Category honesty (B12): in CLI mode NO human was ever
            # consulted — the first rejection was the mode's silent
            # auto-reject (CLI has no interactive drift card) — so this
            # is not a user rejection. Report the dedicated
            # drift-termination category. TUI keeps USER_REJECTED: a
            # human really did reject the drift-correction card before
            # this second drift (auto mode never accumulates the count).
            if state.get("interaction_mode") == "cli":
                _ctx = (
                    "Repeated target drift terminated the run without "
                    "human confirmation; no user was consulted in CLI "
                    "drift handling."
                )
                # W-56-5 (defect a): FAIL routes to the reject terminal node.
                # The former RETRY here let the graph keep running, leaked the
                # fail error into attempt 2, and produced the contradictory
                # double-rendered terminal attribution (#56).
                return {
                    "messages": rejection_msgs,
                    "screener_route": SCREENER_ROUTE_FAIL,
                    "safety_reason": _ctx,
                    **fail_state(FailureCategory.DRIFT_TERMINATED, _ctx),
                    **vehicle_cache,
                }
            _ctx = "Target drift persists after user rejection; terminating."
            return {
                "messages": rejection_msgs,
                "screener_route": SCREENER_ROUTE_FAIL,
                "safety_reason": _ctx,
                **fail_state(FailureCategory.USER_REJECTED, _ctx),
                **vehicle_cache,
            }

        # CLI mode: no interactive human to confirm drift — reject and
        # let LLM self-correct. Second drift hits drift_reject_count>=1
        # hard-terminate above.
        if state.get("interaction_mode") == "cli":
            logger.warning(
                "target_guard: drift in CLI mode (count=%d), rejecting tool_calls",
                drift_reject_count,
            )
            return {
                "messages": rejection_msgs,
                "screener_route": SCREENER_ROUTE_RETRY,
                "drift_reject_count": drift_reject_count + 1,
                **vehicle_cache,
            }

        _reason = drifted[0]["reason"] if drifted else ""
        agent_reason = _extract_agent_reason(last_msg)
        drift_info = {
            "type": "target_change",
            "summary": f"Target change detected: {_reason}",
            "reason": _reason,
            "agent_reason": agent_reason,
            "original": _format_approved_for_card(approved),
            "proposed": _format_effective_for_card(first_eff) if first_eff else {},
            "tool_calls": [
                {"name": d["tool_name"], "reason": d["reason"]}
                for d in drifted
            ],
        }

        user_decision = interrupt(drift_info)

        if user_decision == "approved":
            spec_delta = _apply_drift_correction(
                state, first_eff, cluster_vehicles,
            )
            # Registration follows EXECUTION, not just the ALLOW verdict:
            # the drifted apply runs once a human approves the card, and the
            # Deployment it creates must not orphan when the task dies
            # without teardown — the exact residual this change exists to
            # close. Only the manifest channel carries the cleanup promise;
            # sibling kinds (configmap, …) have no registration machinery
            # and keep their standing behaviour.
            for d in drifted:
                d_eff = d.get("effective")
                if d_eff is not None and getattr(
                    d_eff, "is_drill_target_manifest", False,
                ):
                    _register_drill_target_artifact(
                        vehicle_cache, state, d_eff, d["tool_call_id"],
                    )
            return {
                "screener_route": SCREENER_ROUTE_PASS,
                "drift_reject_count": 0,
                **spec_delta,
                **vehicle_cache,
            }
        else:
            return {
                "messages": rejection_msgs,
                "screener_route": SCREENER_ROUTE_RETRY,
                "drift_reject_count": drift_reject_count + 1,
                **vehicle_cache,
            }

    # --- Non-drift reject (BANNED / UNKNOWN): retry in place ---
    return {
        "messages": rejection_msgs,
        "screener_route": SCREENER_ROUTE_RETRY,
        **vehicle_cache,
    }


def route_after_screener(state: AgentState) -> str:
    """Map the screener's ``screener_route`` field to a graph edge.

    Mirrors the SCREENER_ROUTE_* sentinels. Defaults to "pass" so a
    missing/unknown value never strands the graph. FAIL is the hard
    termination: it routes to the reject terminal node — a hard stop that
    kept looping as RETRY was a ghost termination (W-56-5 defect a, #56).
    """
    route = state.get("screener_route") or SCREENER_ROUTE_PASS
    if route == SCREENER_ROUTE_FAIL:
        return "reject"
    if route == SCREENER_ROUTE_REPLAN:
        return "replan"
    if route == SCREENER_ROUTE_RETRY:
        return "retry"
    return "pass"


def _format_rejection_for_llm(
    decision: dict[str, Any],
    approved_missing: bool,
    approved: ApprovedTarget | None = None,
) -> str:
    """Render a ToolMessage body explaining why the call was blocked.

    Three goals:
      - Tell the LLM WHAT went wrong (reason) so it can rethink.
      - Tell the LLM what WOULD have been allowed (suggestion).
      - Tell the LLM whether the path is a hard floor (stop) or a reshapeable
        form issue (fix and retry), so it keeps its exploration space instead
        of concluding a viable path is a dead-end.
      - Be short — long rejections waste context tokens.
    """
    verdict = decision["verdict"]
    reason = decision["reason"]
    suggestion = decision["suggestion"]
    is_hard_floor = decision.get("is_hard_floor", False)
    parts = [
        f"[target_guard] {verdict.upper()} — {reason}",
    ]
    if suggestion:
        parts.append(suggestion)
    if approved_missing and verdict == GuardVerdict.REJECT_UNKNOWN.value:
        parts.append(
            "no approved target on record; the screener default-denies "
            "destructive calls until confirmation_gate has been passed."
        )
    # Node-scope drift on a node-scope task is almost always "host-escape in the
    # right shape, wrong node" (e.g. `kubectl debug node/<unapproved>`). The
    # audit reason already embeds approved.names, but buried in dense text the
    # LLM tends to read it as a dead-end and bail to verify. Surface an
    # imperative, copy-pasteable hint so it re-targets an approved node instead.
    eff = decision.get("effective")
    if (
        verdict == GuardVerdict.REJECT_DRIFT.value
        and approved is not None
        and approved.scope == "node"
        and approved.names
        and eff is not None
        and getattr(eff, "scope", "") == "node"
    ):
        parts.append(
            "Approved nodes: [" + ", ".join(approved.names) + "]. For a "
            "host-level change, target ONE of these approved nodes "
            "(e.g. `kubectl debug node/<approved-node> --profile=sysadmin`) "
            "— not any other node."
        )
    mechanism_banned = (
        bool(getattr(eff, "mechanism_banned", False)) if eff is not None else False
    )
    if mechanism_banned:
        parts.append(
            "This injection MECHANISM is banned by policy — no reshape of this "
            "call will pass, so do NOT retry it in another form. If the approved "
            "plan depends on it, call `request_replan` to switch to a mechanism "
            "that acts on the existing approved target instead of creating a new "
            "workload."
        )
    elif is_hard_floor:
        parts.append(
            "This is a boundary the guard will not relax; operate within the "
            "approved target or abort if the task cannot proceed."
        )
    else:
        parts.append(
            "This is not a dead-end: adjust the tool_call as above and retry."
        )
    return " ".join(parts)


# Verdicts the screener itself cleared; in a fabricated (rejected) batch
# these calls get the DEFERRED rendering below instead of a rejection.
_CLEARED_VERDICTS = ("allow", "readonly")


def _format_deferred_for_llm(decision: dict[str, Any]) -> str:
    """Render a ToolMessage body for an ALLOWED call that did not execute.

    The batch is atomic: when any sibling call is rejected, NONE of the
    batch's calls run (the screener routes back to the loop before
    phase2_tools ever sees the turn). LangChain still requires an answer
    for every tool_call, so this message replaces the full-rejection text
    the allowed call used to receive (B43): a READONLY meta tool answered
    with "[target_guard] READONLY — adjust the tool_call and retry" reads
    like a guard verdict against the call itself — the model then either
    misdiagnoses its own (correct) call or delays re-issuing it. The
    honest status is "not rejected, not executed, re-issue it".

    Kept status="error" upstream: the call did not succeed and replan
    failure-collection treats it accordingly.
    """
    return (
        f"[screener] DEFERRED — {decision['tool_name']} was NOT rejected; "
        "the screener found nothing wrong with this call. A sibling call "
        "in the same batch was rejected, so the whole batch was returned "
        "unexecuted. Re-issue this call (with the corrected siblings, or "
        "on its own) and it will run normally."
    )


def _format_approved_for_card(approved: ApprovedTarget | None) -> dict:
    if approved is None:
        return {}
    return {
        "scope": approved.scope,
        "namespace": approved.namespace,
        "names": list(approved.names),
        "labels": dict(approved.labels),
        "fault_target": approved.fault_target,
    }


def _format_effective_for_card(eff: EffectiveTarget) -> dict:
    return {
        "scope": eff.scope,
        "namespace": eff.namespace,
        "names": list(eff.names),
        "labels": dict(eff.labels),
        "fault_target": eff.fault_target,
    }


_AGENT_REASON_MAX_LEN = 200


def _extract_agent_reason(msg: AIMessage) -> str:
    """Extract a short explanation from the AIMessage that triggered drift.

    Prefers ``content`` (the LLM's visible text); falls back to a
    truncated ``reasoning_content`` (thinking trace).

    ``content`` may be a str or a list of content blocks (multimodal /
    thinking models). We normalise to str before truncating.
    """
    raw = getattr(msg, "content", "") or ""
    if isinstance(raw, list):
        raw = " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in raw
        ).strip()
    text = raw.strip() if isinstance(raw, str) else ""
    if text:
        return text[:_AGENT_REASON_MAX_LEN]
    additional = getattr(msg, "additional_kwargs", None) or {}
    reasoning = (additional.get("reasoning_content", "") or "").strip()
    if reasoning:
        return reasoning[:_AGENT_REASON_MAX_LEN]
    return ""


def _apply_drift_correction(
    state: AgentState,
    eff: EffectiveTarget | None,
    discovered_vehicles: frozenset[str] = frozenset(),
) -> dict:
    """Correct fault_spec + refreeze approved_target after user approves drift."""
    from chaos_agent.config.settings import settings as _settings

    spec = read_fault_spec(state)
    if not spec or not eff:
        return {}

    # Never rewrite fault_spec toward an injection vehicle. Even if a
    # vehicle exec still produced a drift verdict (a residual path that
    # keeps identity review, e.g. the fault-binary branch) and a human
    # approved it, the correction must not point the spec at injection
    # machinery — that is how a drift loop ends its run "targeting" the
    # tool pod instead of the workload. Same DATA-driven oracle as the
    # screener: task-registered vehicles, previously persisted discoveries,
    # and vehicles discovered in THIS screening round (an interrupt resumes
    # before the round's cache reaches state, so the caller passes it in).
    if eff.names and all(
        is_vehicle_name(n, state)
        or n in frozenset(state.get("known_vehicle_pods") or ())
        or n in discovered_vehicles
        for n in eff.names
    ):
        logger.warning(
            "target_guard: drift correction toward vehicle pod(s) %s skipped "
            "(fault_spec must never point at injection machinery)",
            list(eff.names),
        )
        return {}

    # Kind-consistency guard (task-51193464): an effective target whose
    # KIND differs from the spec's scope is not a target CORRECTION — it is
    # an auxiliary-resource operation (create/delete a PVC the victim pod
    # needs, a configmap, ...) or a genuine scope escape. Either way the
    # approval must NOT rewrite the spec's identity: replacing only
    # ``names``/``namespace`` while the scope stays would freeze a corrupt
    # hybrid anchor (``scope=node, names=[<pvc-name>]``) that turns every
    # later legitimate operation — including the REAL injection against the
    # approved node — into further drift, demanding one confirmation card
    # after another. An approve here means "allow THIS operation", not "the
    # fault target has changed": approving is a one-shot pass-through, the
    # anchor stays as confirmed at the gate. A genuine target change goes
    # through the planning seam (propose_plan_change) where the full
    # FaultSpec contract — scope included — is re-reviewed.
    if canonicalise_kind(eff.scope) != canonicalise_kind(spec.scope):
        logger.warning(
            "target_guard: drift correction for kind %s under %s approval "
            "skipped (auxiliary/scope-change operation approved for THIS "
            "call only; approved target unchanged)",
            canonicalise_kind(eff.scope), canonicalise_kind(spec.scope),
        )
        return {}

    corrections: dict = {}
    if eff.namespace and eff.namespace != spec.namespace:
        if eff.namespace not in (_settings.blacklist_namespaces or []):
            corrections["namespace"] = eff.namespace
    if eff.names and tuple(eff.names) != spec.names:
        corrections["names"] = tuple(eff.names)
    if eff.labels and eff.labels != spec.labels:
        corrections["labels"] = eff.labels

    if corrections:
        new_spec = spec.replace(**corrections)
        if "names" in corrections:
            logger.debug(
                "spec-write: writer=tool_screener._apply_drift_correction "
                "names %s -> %s basis=user-approved drift EffectiveTarget",
                list(spec.names), list(corrections["names"]),
            )
    else:
        new_spec = spec

    # Preserve the discovered frozen sets (owner_names / resolved_names)
    # that safety_check discovered, UNLESS the correction changed the
    # identity they were derived from. resolved_names is label-derived
    # (label selector → concrete names), so "labels changed" invalidates
    # it. owner_names is now DUAL-SOURCED — labels-matched owners AND the
    # names→ownerReferences chain (the generation anchor) — so ANY
    # identity change (names OR labels) can stale one of its sources;
    # rather than track which source survives, both identity changes
    # drop it (conservative, aligned with pvc_claims below: the guard
    # then falls back to namespace-only anchoring until the next
    # approval re-discovers).
    existing = state.get("approved_target") or {}
    if "labels" in corrections:
        owner_names: tuple[str, ...] = ()
        resolved_names: tuple[str, ...] = ()
    elif "names" in corrections:
        owner_names = ()
        resolved_names = tuple(existing.get("resolved_names") or ())
    else:
        owner_names = tuple(existing.get("owner_names") or ())
        resolved_names = tuple(existing.get("resolved_names") or ())
    # pvc_claims anchor the occupant-vehicle exception and were discovered
    # against the CONCRETE approved identities IN THE APPROVED NAMESPACE:
    # any identity change (names, labels OR namespace) stales them — the
    # frozen claim names are namespace-scoped facts about the OLD target,
    # and a same-name PVC in the corrected namespace would silently widen
    # the whitelist onto a disk the target never uses. Drop, fail closed
    # (the vehicle exception stays banned until a fresh approval
    # re-discovers claims).
    if (
        "labels" in corrections
        or "names" in corrections
        or "namespace" in corrections
    ):
        pvc_claims: tuple[str, ...] = ()
    else:
        pvc_claims = tuple(existing.get("pvc_claims") or ())

    # Case-manifest mechanism entries are legislation parsed from the
    # case file at settlement — orthogonal to the victim identity
    # correction above: a names/labels correction never stales them
    # (they anchor the MECHANISM domain, not the victim). Carry them
    # forward verbatim so mechanism writes stay in-contract after the
    # snapshot rebuild; dropping them would regress every frozen
    # mechanism write into drift.
    mechanism_entries = entries_from_list(existing.get("mechanism_entries"))

    # The case-file ``recovery_channel`` legislation is carried forward
    # verbatim for the same reason: it anchors the CASE's recovery route,
    # not the victim identity — a correction never stales it, and dropping
    # it would demote the route gate back to its verb-vocabulary proxy.
    recovery_channel = str(existing.get("recovery_channel") or "")

    result: dict = {"fault_spec": new_spec.to_dict()}
    result["approved_target"] = freeze_approved_target_from_spec(
        new_spec, owner_names=owner_names, resolved_names=resolved_names,
        pvc_claims=pvc_claims, mechanism_entries=mechanism_entries,
        recovery_channel=recovery_channel,
        # The drift-correction card ran under a human's eyes, so the
        # rebuild counts as an approval: the pending marker clears
        # (explicit here to document the semantics; the sentinel would
        # otherwise terminate the corrected run at the next entry).
        widening_pending_approval=False,
    )
    return result


__all__ = [
    "SCREENER_ROUTE_PASS",
    "SCREENER_ROUTE_REPLAN",
    "SCREENER_ROUTE_RETRY",
    "route_after_screener",
    "tool_screener",
]
