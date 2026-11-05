"""Programmatic recovery-carrier assembler (openspec change
``faultdrill-cluster-native-recovery`` M1, design ND2/ND3/ND4/ND5/ND6).

The apiserver-write recovery execution layer as ONE provider EXECUTE
tool: the LLM calls ``faultdrill_assemble_carrier`` once and THIS module
deterministically assembles the one-shot recovery carrier — SA + minimal
Role/RoleBinding + bare Pod (``--restart=Never``, sleep skeleton), the
two-step exec arming (run = skeleton, exec = arm; arming knowledge
``recovery-carrier-arming.md``), then the synchronous fault injection —
zero LLM assembly rounds in between. The carrier's timer fires the
restorePatches in-cluster at TTL regardless of the agent process.

Safety is NOT the migrated LLM guard set (design ND3: those defend
against LLM mistake modes this deterministic path structurally cannot
produce). It is construction + fail-closed inline checks, legislated by
the recovery-carrier standard (``references/carrier/recovery-carrier.md``):

- carrier shape: fixed template, asserted through the SAME library
  predicate the guard dispatches on (``classifier._is_recovery_carrier_run``)
  — a self-check against assembler code regression, not LLM screening;
- minimal RBAC: derived from the SAME restorePatches that arm the
  payload (payload verbs ⊆ granted verbs by construction), wildcard
  free, two-step json-patch construction when a multi-family verb set
  is non-uniform (§2 derivation table + the union-broadcast rule);
- SA real-token verification: read-only GET hard gate, then per
  write-verb SSAR with the echo-self-attestation three-state verdict
  (§3) — 403 / ``allowed:false`` aborts and cleans the built stack;
- armed-before-inject: an internal control-flow order — the injection
  patch only runs after the arming exec echoed ``armed``;
- failure cleanup: any pre-injection failure deletes the four-object
  stack (idempotent, pod → binding → role → sa) and reports honestly.
  A POST-arming injection failure KEEPS the carrier: the timer must
  survive (a timed-out patch may have landed — dropping the timer could
  strand the fault), and its fire on an unpatched target is an
  idempotent no-op.

Execution rides the provider's single ``_kubectl`` seam
(``provider._kubectl`` → ``exec_kubectl_raw``, channel-agnostic
transport; the same patch point the reconciler tests use). All exec
payloads are dispatched as ``kubectl exec POD -- sh -c <script>`` with
the script as ONE argv element — the programmatic path never runs the
LLM string-tokenizer, and the guard's token checks deliberately exclude
container-command tokens after ``--`` (guard.py: exec-form, shell=False).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from typing import Any, Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

#: The provider EXECUTE tool this module contributes (provider.tools).
ASSEMBLER_TOOL_NAME = "faultdrill_assemble_carrier"

# ---------------------------------------------------------------------------
# Legislative constants (recovery-carrier.md standard, NOT reinvented)
# ---------------------------------------------------------------------------

#: In-cluster apiserver base the carrier's restore curls target (§4:
#: cluster-internal DNS direct to the apiserver).
_APISERVER_BASE = "https://kubernetes.default.svc"

#: Mounted service-account credential paths inside the carrier pod.
_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

#: Restore-forensics buffer on the sleep skeleton: window + 1800s
#: (2026-09-17 legislation — 600s did not cover the post-fire
#: out-of-band forensics cadence of #46/#48/#51), capped at 86400.
_SKELETON_BUFFER_SECONDS = 1800
_MAX_SLEEP_SECONDS = 86400

#: Channel payload budget (§7): 1024B hard cap on some exec channels;
#: ~900B is the legislated safety line (quote-escape headroom).
_ARM_PAYLOAD_BUDGET = 1024
_ARM_PAYLOAD_SAFE_LINE = 900

#: Self-termination lead (§4 ownerRef GC cascade — the W-83-3B root-fix).
#: The armed timer's self-delete tail fires ``_SKELETON_BUFFER_SECONDS -
#: _SELF_DELETE_LEAD_SECONDS`` after the restore, i.e. strictly BEFORE the
#: skeleton's own ``sleep`` ends (skeleton = duration + buffer). Two
#: guarantees fall out of the lead: (1) the delete curl runs while the
#: container is still alive — a background timer is killed the instant the
#: main ``sleep`` process exits, so a self-delete AT the skeleton boundary
#: would race the container teardown; (2) the ~25min post-fire window the
#: buffer exists for (the out-of-band ``cat /tmp/restore.log`` forensics
#: cadence of #46/#48/#51) is preserved almost whole.
_SELF_DELETE_LEAD_SECONDS = 300

#: REST method → RBAC verb map (§2 form-agnostic master rule). Closed
#: vocabulary — a Role rendered from it can never grow a wildcard.
REST_METHOD_VERBS = {
    "PATCH": "patch",
    "PUT": "update",
    "DELETE": "delete",
    "POST": "create",
}

#: The json-patch Content-Type the restore curls carry (the recipe is a
#: json-patch array; merge-patch's array semantics are the B58 trap).
_JSON_PATCH_CONTENT_TYPE = "application/json-patch+json"

#: Restore-request attribution prefix (B3): the armed curl's User-Agent
#: reads ``blade-ai-recovery/<carrier>`` so the apiserver's audit log
#: and managedFields entries attribute the TTL restore PATCH to THIS
#: drill's carrier, not to an anonymous in-cluster client.
_RESTORE_USER_AGENT_PREFIX = "blade-ai-recovery"

#: Characters a User-Agent value may keep — the value rides inside a
#: double-quoted shell string in the armed script, so quotes, ``$`` and
#: friends are stripped, never escaped.
_UA_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._/-]+")

#: Taints every pod tolerates by default — excluded from the derived
#: overrides tolerations (admitting them would be noise, not access).
_BUILTIN_TAINT_PREFIXES = (
    "node.kubernetes.io/",
    "node-role.kubernetes.io/",
)

#: kind (canonical lowercase singular) → (api_group, api_version,
#: plural, namespaced). ONE table feeds the restore URL, the SSAR
#: request and the Role rules, so they can never drift apart. Unknown
#: kinds fail closed (``canonical_kind`` raises). Cluster-scoped kinds
#: are listed for URL shape but REJECTED by the assembler — the
#: five-object ClusterRole variant is out of M1 scope.
_KIND_REST_MAP: dict[str, tuple[str, str, str, bool]] = {
    "deployment": ("apps", "v1", "deployments", True),
    "statefulset": ("apps", "v1", "statefulsets", True),
    "daemonset": ("apps", "v1", "daemonsets", True),
    "replicaset": ("apps", "v1", "replicasets", True),
    # batch/v1 CronJob (GA since k8s 1.21). Victim-own-body patch target for
    # the CronJob_任务异常 cases (spec.suspend / jobTemplate). Registered as a
    # fault scope in fault_registry; without this REST entry canonical_kind
    # would fail closed and the recovery carrier could not be built.
    "cronjob": ("batch", "v1", "cronjobs", True),
    "service": ("", "v1", "services", True),
    "configmap": ("", "v1", "configmaps", True),
    "secret": ("", "v1", "secrets", True),
    "persistentvolumeclaim": ("", "v1", "persistentvolumeclaims", True),
    "resourcequota": ("", "v1", "resourcequotas", True),
    "pod": ("", "v1", "pods", True),
    "ingress": ("networking.k8s.io", "v1", "ingresses", True),
    "node": ("", "v1", "nodes", False),
    "namespace": ("", "v1", "namespaces", False),
    "storageclass": ("storage.k8s.io", "v1", "storageclasses", False),
}

_KIND_ALIASES = {
    "deploy": "deployment", "deployments": "deployment",
    "svc": "service", "services": "service",
    "cm": "configmap", "configmaps": "configmap",
    "sts": "statefulset", "statefulsets": "statefulset",
    "ds": "daemonset", "daemonsets": "daemonset",
    "pvc": "persistentvolumeclaim",
    "persistentvolumeclaims": "persistentvolumeclaim",
    "quota": "resourcequota", "resourcequotas": "resourcequota",
    "po": "pod", "pods": "pod",
    "no": "node", "nodes": "node",
}


def canonical_kind(kind: str) -> str:
    """Normalise a targetRef kind to the REST map's canonical lowercase
    singular form (``Deployments``/``svc``/``ConfigMap`` → deployment/
    service/configmap). Raises ``ValueError`` on an unknown kind — the
    assembler fails closed rather than guessing a URL/RBAC shape."""
    key = str(kind or "").strip().lower()
    key = _KIND_ALIASES.get(key, key)
    if key not in _KIND_REST_MAP:
        raise ValueError(
            f"targetRef kind {kind!r} has no REST mapping (supported: "
            f"{', '.join(sorted(_KIND_REST_MAP))}); refusing to guess a "
            "restore URL or RBAC shape"
        )
    return key


def rest_group_of(kind: str) -> str:
    return _KIND_REST_MAP[canonical_kind(kind)][0]


def rest_plural_of(kind: str) -> str:
    return _KIND_REST_MAP[canonical_kind(kind)][2]


def rest_url_for(kind: str, name: str, namespace: str) -> str:
    """Cluster-internal REST URL of one target object — the §3 GET
    probe and the §4 restore payload draw on the same construction."""
    group, version, plural, namespaced = _KIND_REST_MAP[canonical_kind(kind)]
    if not namespaced:
        return f"{_APISERVER_BASE}/api/{version}/{plural}/{name}"
    if group:
        return (
            f"{_APISERVER_BASE}/apis/{group}/{version}"
            f"/namespaces/{namespace}/{plural}/{name}"
        )
    return (
        f"{_APISERVER_BASE}/api/{version}"
        f"/namespaces/{namespace}/{plural}/{name}"
    )


# ---------------------------------------------------------------------------
# RBAC verb×resource derivation (§2 derivation table — NEW safety-
# critical code; pinned by tests against the table entries)
# ---------------------------------------------------------------------------


def derive_role_rules(writes: list[dict]) -> list[dict]:
    """Write requests → minimal Role rules (§2: the Role carries ONLY
    the restore actions' verbs ∪ the probe ``get`` — the injection
    itself never rides the carrier).

    Args:
        writes: one entry per written resource family,
            ``{"kind": "Deployment", "methods": ["PATCH"]}`` — REST
            methods are mapped through :data:`REST_METHOD_VERBS`.

    Returns:
        One rule per resource family (stable order by plural), each
        ``{"apiGroups": [group], "resources": [plural],
        "verbs": sorted(verbs | {"get"})}`` — the ``get`` ride is the
        derivation-table row "验权探测 → 目标资源的 get" (the real-token
        GET probe shares the restore resource). Wildcards can never
        appear: verbs come from the closed REST-method map, resources
        from the closed REST map.

    Raises:
        ValueError: unknown kind or unmapped REST method — fail closed.
    """
    by_resource: dict[str, dict] = {}
    for entry in writes or []:
        kind = canonical_kind(str((entry or {}).get("kind") or ""))
        group, _, plural, _ = _KIND_REST_MAP[kind]
        verbs: set[str] = set()
        for method in (entry or {}).get("methods") or []:
            verb = REST_METHOD_VERBS.get(str(method).upper())
            if verb is None:
                raise ValueError(
                    f"REST method {method!r} has no RBAC verb mapping "
                    f"(known: {', '.join(sorted(REST_METHOD_VERBS))})"
                )
            verbs.add(verb)
        if not verbs:
            raise ValueError(
                f"write entry for kind {kind!r} carries no REST methods"
            )
        slot = by_resource.setdefault(
            plural, {"group": group, "verbs": set()},
        )
        if slot["group"] != group:
            raise ValueError(f"plural {plural!r} maps to two API groups")
        slot["verbs"].update(verbs)
    rules: list[dict] = []
    for plural in sorted(by_resource):
        slot = by_resource[plural]
        rules.append({
            "apiGroups": [slot["group"]],
            "resources": [plural],
            "verbs": sorted(slot["verbs"] | {"get"}),
        })
    return rules


def build_self_teardown_rule(name: str) -> dict:
    """The carrier's self-teardown grant (§4 ownerRef GC cascade): ONE
    independent Role rule, ``delete pods`` resourceNames-locked to the
    carrier pod itself — the least-privilege self-reference that lets the
    armed timer delete ITS OWN pod (and nothing else), which the
    ``ownerReferences`` cascade then folds into deleting the SA/Role/
    RoleBinding. This is the mechanism that retires the recovery-carrier
    standard's L264 "用自己授权删自己授权" deadlock: the old self-delete
    tail could only remove ONE Binding (deleting the Role first strands a
    dangling Binding; deleting the SA first kills the token before any
    delete lands), so the SA/Role lingered as inert shells owned by nobody
    but the agent's bypassable teardown. Deleting the POD instead needs no
    self-authorization over the RBAC at all — the apiserver's GC, using
    ITS OWN credentials, cascades the dependents.

    Two placement invariants (both pinned by tests):
    - it is appended as an INDEPENDENT ``/rules/-`` json-patch, NEVER
      merged into the ``kubectl create role`` broadcast (its
      ``resourceNames`` lock would otherwise pollute the main recovery
      rules — §1 two-step legislation + the standard's L265 判据②);
    - it stays OUT of the SSAR-verified ``rules`` list: a
      resourceNames-locked grant cannot be SSAR-probed (the review carries
      no resourceName, so it returns ``allowed:false`` against the lock) —
      it is fail-open and verified by construction, exactly the standard's
      "delete verb 无法用 GET 事前探测" clause."""
    return {
        "apiGroups": [""],
        "resources": ["pods"],
        "resourceNames": [name],
        "verbs": ["delete"],
    }


def verb_sets_uniform(rules: list[dict]) -> bool:
    """Whether every family carries the SAME verb set — the two-step
    decision (§1 Role construction legislation): a uniform set renders
    cleanly through ONE ``kubectl create role`` (the measured
    exception), while a NON-uniform set must go two-step (``create``
    the first family, then ``json-patch /rules/-`` each remaining
    family as an independent rule) — ``kubectl create role`` broadcasts
    the UNION of every ``--verb`` flag into every rule (the pairing
    semantics does not exist; a non-uniform single command always
    over-grants, e.g. delete×deployments + patch×resourcequotas)."""
    signatures = {
        tuple(sorted(r.get("verbs") or [])) for r in rules or []
    }
    return len(signatures) <= 1


def build_role_create_v_args(
    name: str, namespace: str, rules: list[dict],
) -> list[str]:
    """``kubectl create role`` v_args for a UNIFORM rule set (one clean
    single command; per-group ``--resource`` lists share the verb set
    kubectl broadcasts — exact, not over-granting).

    Raises:
        ValueError: empty rules, or a non-uniform set — the latter is
            the union-broadcast counterexample; the caller must route
            non-uniform sets through :func:`render_role_commands`.
    """
    if not rules:
        raise ValueError("no rules to render")
    if not verb_sets_uniform(rules):
        raise ValueError(
            "non-uniform verb sets must use the two-step construction "
            "(create the first family + json-patch append the rest) — "
            "a single create role would broadcast the verb union into "
            "every rule (over-grant)"
        )
    verbs = ",".join(sorted(rules[0].get("verbs") or []))
    by_group: dict[str, list[str]] = {}
    for rule in rules:
        group = str((rule.get("apiGroups") or [""])[0])
        by_group.setdefault(group, []).extend(list(rule.get("resources") or []))
    v_args = ["role", name, "-n", namespace, "--verb", verbs]
    for group, resources in by_group.items():
        # Group-qualified resources take kubectl's ``resource.group`` dot
        # form (``deployments.apps``) — the slash form is mis-parsed as
        # resource "apps" and fails live (B86, #43-R3 + #62).
        qualified = (
            [f"{res}.{group}" for res in resources] if group else list(resources)
        )
        v_args.extend(["--resource", ",".join(qualified)])
    return v_args


def build_role_append_patch(rules: list[dict], skip_first: int = 0) -> str:
    """``kubectl patch role --type=json`` payload appending the rules
    beyond the create command's families as INDEPENDENT entries (the
    two-step form, §1: one ``[{"op":"add","path":"/rules/-"}, ...]``
    array — multi-family appends land in ONE patch, measured legal).

    ``skip_first`` drops leading rules the create command already
    rendered (the single-command path carries the first family).
    """
    ops = [
        {"op": "add", "path": "/rules/-", "value": rule}
        for rule in (rules or [])[skip_first:]
    ]
    if not ops:
        raise ValueError("no rules left to append")
    return json.dumps(ops)


def render_role_commands(
    name: str, namespace: str, rules: list[dict],
) -> list[tuple[str, list[str], str]]:
    """Full Role construction plan → ``[(subcommand, v_args, stdin)]``.

    Uniform set: ONE ``create role``. Non-uniform set: ``create role``
    the FIRST family, then one ``patch role --type=json`` appending the
    remaining families as independent rules (§1 two-step legislation —
    the union-broadcast counterexample makes the single command illegal).
    """
    if not rules:
        raise ValueError("no rules to render")
    if verb_sets_uniform(rules):
        return [("create", build_role_create_v_args(name, namespace, rules), "")]
    plan: list[tuple[str, list[str], str]] = [
        ("create", build_role_create_v_args(name, namespace, rules[:1]), ""),
    ]
    plan.append((
        "patch",
        ["role", name, "-n", namespace, "--type=json",
         "-p", build_role_append_patch(rules, skip_first=1)],
        "",
    ))
    return plan


# ---------------------------------------------------------------------------
# Carrier naming / image selection / shape self-check
# ---------------------------------------------------------------------------


def carrier_name(task_id: str, target: dict, salt: str = "") -> str:
    """``drill-rc-<hash>`` — the four-object shared name (the standard's
    naming convention; ``_attach_recovery_carrier_rbac`` exact-match
    linking and the cleanup chain both rely on it)."""
    from chaos_agent.config.settings import settings

    prefix = str(settings.recovery_carrier_name_prefix or "drill-rc-")
    basis = "|".join([
        str(task_id or ""), str(salt or ""),
        str((target or {}).get("kind") or ""),
        str((target or {}).get("name") or ""),
        str((target or {}).get("namespace") or ""),
    ])
    digest = hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{prefix}{digest}"


def select_carrier_image() -> str:
    """Image choice from the effective allowlist (configured ∪
    auto-discovered — the same union the classifier legislates, reached
    through the registry seam so the assembler never re-implements it),
    discovered-FIRST and pool-local: when the discovery found node-cached
    images the choice is locked to that pool (a curl-capable NAME is only
    a tie-breaker WITHIN a pool, never a reason to leave it — #55 M3
    live run: the sole discovered image terway, empirically curl-capable
    but curl-less by name, was skipped in favour of a configured
    curlimages/curl the VPC cluster cannot pull); the configured pool is
    consulted only when discovery found nothing. The arming knowledge
    hard-requires sh+sleep+curl, and the GET probe exercises curl
    in-cluster, so a curl-less image fails closed at the probe, not at
    fire time."""
    from chaos_agent.agent.providers.registry import FaultProviderRegistry

    allowed = sorted(FaultProviderRegistry.recovery_carrier_allowed_images())
    if not allowed:
        raise ValueError(
            "recovery-carrier image allowlist is empty (neither "
            "configured nor auto-discovered) — cannot build a carrier"
        )
    # Exact membership, NOT substring: a configured bare name (e.g.
    # ``curlimages/curl:latest``, docker.io) is a substring of a discovered
    # FQ image (``<registry>/curlimages/curl:latest``) and would masquerade
    # as node-cached — the assembler would then build a carrier from the
    # unpullable bare name and burn the full _wait_pod_running timeout on
    # ImagePullBackOff. Match the discovered strings exactly so the pool
    # only ever contains genuinely node-cached images.
    discovered_set = {
        s.strip() for s in str(_discovered_images_text()).split(",") if s.strip()
    }
    discovered = [img for img in allowed if img in discovered_set]
    pool = discovered or allowed
    for img in pool:
        if "curl" in img:
            return img
    return pool[0]


def _discovered_images_text() -> str:
    from chaos_agent.config.settings import settings

    return str(settings.recovery_carrier_discovered_images or "")


def build_run_v_args(
    name: str, namespace: str, image: str, skeleton_seconds: int,
    tolerations: Optional[list[dict]] = None,
) -> list[str]:
    """``kubectl run`` v_args for the carrier skeleton — the fixed
    template (§1 step 4): bare sleep skeleton, ``--restart=Never``,
    SA attached through ``--overrides`` (the ONLY flag the shape admits
    for it), scheduling tolerations derived from the cluster's
    pre-existing taints."""
    spec: dict[str, Any] = {"serviceAccountName": name}
    if tolerations:
        spec["tolerations"] = tolerations
    return [
        name, "-n", namespace,
        "--image", image,
        "--restart", "Never",
        "--overrides", json.dumps({"spec": spec}),
        "--command", "--", "sleep", str(int(skeleton_seconds)),
    ]


def assert_carrier_shape(run_v_args: list[str], name: str) -> None:
    """Self-check the constructed ``kubectl run`` against the canonical
    carrier shape predicate (ND3: construction guarantee asserted, not
    re-derived — a miss here is an assembler code regression, and the
    call refuses to execute rather than dispatch a malformed carrier;
    the predicate itself lives in the k8s-native classifier, reached
    through the registry seam)."""
    from chaos_agent.agent.providers.registry import FaultProviderRegistry

    if not FaultProviderRegistry.is_recovery_carrier_run_shape(
        list(run_v_args), name
    ):
        raise ValueError(
            f"assembler self-check failed: constructed kubectl run for "
            f"{name!r} does not satisfy the recovery-carrier shape "
            f"(args={run_v_args!r})"
        )


def build_owner_ref_patch(pod_name: str, pod_uid: str) -> str:
    """Merge-patch setting ``metadata.ownerReferences`` to the carrier
    pod, so the apiserver's garbage collector cascades THIS object's
    deletion from the pod's (§4 root-fix: the whole stack self-terminates
    in-cluster when the armed timer deletes its own pod — the W-83-3B
    ownership inversion fix).

    Cross-group ownership is accepted by the apiserver and was
    cascade-verified live (实测 A/B, 2026-10-20, K8s v1.24.6): a ``v1``
    Pod owns a ``v1`` ServiceAccount and ``rbac.authorization.k8s.io/v1``
    Role/RoleBinding; deleting the pod GC-cascades all three within
    seconds. A merge patch REPLACES the (empty) ownerReferences array —
    the RBAC objects are agent-built with no prior owner, so this is
    additive, never clobbering.

    Scope boundary (honest): ownerRef cascade requires a SAME-SCOPE owner.
    All three objects are namespaced like the pod, so the four-object
    stack is fully covered. The five/six-object CLUSTER variants
    (ClusterRole/ClusterRoleBinding, for node-level targets) CANNOT be
    owned by a namespaced pod (K8s forbids a cross-scope ownerRef) — those
    keep the standard's L130 self-delete-Binding rule + the agent four-way
    teardown backstop. The assembler only ever builds the namespaced
    four-object stack (cluster-scoped targets are rejected upstream), so
    this helper is only reached on the covered path."""
    return json.dumps({"metadata": {"ownerReferences": [{
        "apiVersion": "v1",
        "kind": "Pod",
        "name": pod_name,
        "uid": pod_uid,
    }]}})


# ---------------------------------------------------------------------------
# Payload construction (§4 arming / §3 verification scripts)
# ---------------------------------------------------------------------------


def _shell_sq(text: str) -> str:
    """Single-quote a payload for a POSIX shell (``'`` → ``'\''``) —
    the standard's quote-fidelity-preferred outer form."""
    return "'" + str(text).replace("'", "'\\''") + "'"


def build_restore_script(
    duration_seconds: int, target_url: str, body: str,
    *, body_file: str = "", user_agent: str = "",
    delete_urls: Optional[list[str]] = None,
    self_delete_url: str = "", self_delete_hold_seconds: Optional[int] = None,
) -> str:
    """The §4 compact-variable-form timer script (C/T/U assigned INSIDE
    the payload — the standard's approved form; ``body_file`` switches
    the curl data to ``-d @file`` for the §7 oversized-payload landing
    tier). Output lands in ``/tmp/restore.log`` — never ``/dev/null``
    (the §4 form discipline: silent restore failure is the unforgivable
    mode).

    ``user_agent`` (B3): the apiserver records a request's User-Agent in
    audit logs and managedFields entries, so the armed restore PATCH
    carries ``blade-ai-recovery/<carrier>`` — apiserver-side attribution
    to THIS drill's carrier, not just the agent's own receipt trail.
    Unsuitable characters are stripped (the value rides inside a
    double-quoted shell string); an empty value falls back to the bare
    prefix.

    ``delete_urls`` (recovery-completeness inverse): after the patch-
    domain restore, the timer also DELETEs the create-domain prop objects
    the inject plan built OUTSIDE the patch domain (``extra_delete``) —
    a json-patch can only rewrite an existing object's fields, it cannot
    express "delete an independently created Secret", so without this the
    prop object would outlive the fault with no autonomous executor (the
    #51-R5 regression). PATCH runs FIRST (restoring the target — the fault
    itself — outranks prop hygiene); each DELETE carries no body and uses
    ``-s`` WITHOUT ``-f``, so a 404 (already gone — the recover replay may
    have deleted it first) is an idempotent no-op whose response still
    lands in restore.log. Empty/omitted → the script is byte-identical to
    the pure-patch form.

    ``self_delete_url`` (§4 ownerRef GC self-termination — the W-83-3B
    root-fix): when set (the caller passes it ONLY after
    :func:`_attach_self_termination` wired the ownerRef + self-teardown
    grant), the timer appends a self-delete tail that removes the carrier
    POD, which the ``ownerReferences`` cascade folds into removing the
    SA/Role/RoleBinding — the whole stack self-terminates in-cluster at
    TTL regardless of the agent process (timeout / crash / kill / a
    recover run that never reached finalize). Three disciplines, all
    legislated by recovery-carrier.md §4:
    - FAIL-OPEN GATE (L262): the tail is guarded by ``case "$RC" in 2*)``
      on the main restore's captured HTTP code — a restore that did NOT
      land (non-2xx) keeps the carrier alive for forensics and the
      out-of-band ``blade-ai recover`` retry, never tearing down the
      authorization/evidence of an unrecovered fault. Capturing ``$RC``
      redirects the PATCH body to ``/tmp/rr.body`` and ``cat``s it back,
      so the restore.log forensics (body + an explicit ``RESTORE_HTTP=``
      line) is preserved whole, not reduced to a code.
    - FORENSICS HOLD (L75-78 buffer semantics): the tail sleeps
      ``self_delete_hold_seconds`` (default = buffer − lead) after the
      restore BEFORE deleting, so the post-fire ``cat /tmp/restore.log``
      window survives AND the delete curl runs while the container is
      still alive (a background timer dies the instant the skeleton's main
      ``sleep`` exits — the lead keeps the self-delete strictly inside the
      pod's lifetime).
    - IMMEDIATE GRACE: the caller appends ``?gracePeriodSeconds=0`` to the
      URL — the skeleton is a stateless ``sleep`` (nothing to flush, the
      ``_cleanup_stack`` ``--force`` rationale), so the pod goes at once
      and the GC cascade completes without the 30s default grace
      (实测 B: default grace held the stack in Terminating ~30s).
    Empty/omitted → byte-identical to the pure-patch form (no ``$RC``
    capture, no tail).
    """
    ua = _UA_UNSAFE_CHARS_RE.sub("", str(user_agent)) or _RESTORE_USER_AGENT_PREFIX
    data_arg = f"-d @{body_file}" if body_file else f"-d {_shell_sq(body)}"
    delete_cmds = "".join(
        f"; curl -s -X DELETE --cacert $C "
        f'-H "Authorization: Bearer $T" '
        f'-H "User-Agent: {ua}" '
        f"{url}"
        for url in (delete_urls or [])
    )
    headers = (
        f'-H "Authorization: Bearer $T" '
        f'-H "Content-Type: {_JSON_PATCH_CONTENT_TYPE}" '
        f'-H "User-Agent: {ua}" '
    )
    if self_delete_url:
        hold = (
            _SKELETON_BUFFER_SECONDS - _SELF_DELETE_LEAD_SECONDS
            if self_delete_hold_seconds is None
            else int(self_delete_hold_seconds)
        )
        patch_cmd = (
            f"RC=$(curl -s -o /tmp/rr.body -w '%{{http_code}}' "
            f"-X PATCH --cacert $C {headers}{data_arg} $U); "
            f'echo "RESTORE_HTTP=$RC"; cat /tmp/rr.body'
        )
        self_delete = (
            f'; case "$RC" in 2*) sleep {max(0, hold)}; '
            f"curl -sf -X DELETE --cacert $C "
            f'-H "Authorization: Bearer $T" '
            f'-H "User-Agent: {ua}" '
            f'"{self_delete_url}" ;; esac'
        )
    else:
        patch_cmd = f"curl -s -X PATCH --cacert $C {headers}{data_arg} $U"
        self_delete = ""
    return (
        f"( sleep {int(duration_seconds)}; "
        f"C={_SA_CA_PATH}; T=$(cat {_SA_TOKEN_PATH}); U={target_url}; "
        f"{patch_cmd}"
        f"{delete_cmds}"
        f"{self_delete}"
        f" ) >/tmp/restore.log 2>&1 & echo armed"
    )


def build_body_landing_scripts(body: str, chunk_size: int = 720) -> list[str]:
    """§7 oversized-payload landing tier: write the restore body into
    the carrier as base64 chunks (the base64 alphabet carries no quote
    or ``$`` — zero expansion points at the writing layer, the legal
    landing form), then decode to ``/tmp/restore.json`` for the armed
    ``-d @`` reference. Chunked writes keep every single exec payload
    inside the channel budget no matter how large the recipe grows."""
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
    scripts: list[str] = []
    if not encoded:
        raise ValueError("restore body is empty")
    first = True
    for i in range(0, len(encoded), chunk_size):
        op = ">" if first else ">>"
        scripts.append(f"echo -n {encoded[i:i + chunk_size]} {op} /tmp/restore.b64")
        first = False
    scripts.append(
        "base64 -d /tmp/restore.b64 > /tmp/restore.json && rm -f /tmp/restore.b64"
    )
    return scripts


def build_probe_get_script(target_url: str) -> str:
    """§3 step 1 — the SA real-token read-only GET probe (``-w
    %{http_code}`` binary verdict; impersonated ``can-i --as`` is
    banned by the arming knowledge — it evaluates a simulated identity
    on the caller's channel, not the carrier's real token request
    path: SA group memberships do not ride an impersonated request)."""
    return (
        f"T=$(cat {_SA_TOKEN_PATH}); "
        f'curl -s -o /dev/null -w "%{{http_code}}" '
        f"--cacert {_SA_CA_PATH} "
        f'-H "Authorization: Bearer $T" '
        f"{target_url}"
    )


def build_ssar_script(
    verb: str, group: str, resource: str, namespace: str,
) -> str:
    """§3 step 2 — one SelfSubjectAccessReview per payload write verb.
    Field discipline (#51-R): ``group`` (NOT ``apiGroup`` — the Role
    rule spelling), and ``spec`` MUST be the object
    ``{"resourceAttributes": {...}}`` (a dropped brace is a hard 400)."""
    body = json.dumps({
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectAccessReview",
        "spec": {"resourceAttributes": {
            "namespace": namespace,
            "verb": verb,
            "group": group,
            "resource": resource,
        }},
    })
    return (
        f"T=$(cat {_SA_TOKEN_PATH}); "
        f"curl -s --cacert {_SA_CA_PATH} "
        f'-H "Authorization: Bearer $T" '
        f'-H "Content-Type: application/json" '
        f"-X POST {_APISERVER_BASE}"
        f"/apis/authorization.k8s.io/v1/selfsubjectaccessreviews "
        f"-d {_shell_sq(body)}"
    )


def ssar_verdict(
    response: dict, verb: str, group: str, resource: str, namespace: str,
) -> tuple[str, str]:
    """§3 echo-self-attestation three-state verdict on one SSAR
    receipt: ``("allowed"|"denied"|"form_error", detail)``.

    The echo check precedes the permission judgement — a silently
    dropped field (the #51-R ``apiGroup`` miss) drifts the question
    itself, and an ``allowed`` value over a drifted echo is meaningless.

    Echo completeness compares under the apiserver's SERIALIZATION
    contract, not byte-for-byte: ``ResourceAttributes`` fields are all
    Go ``omitempty`` strings, so an empty-string attribute is OMITTED
    from the serialized echo (no key), never written as ``""``. A
    missing key / JSON null therefore normalizes to the string
    zero-value and compares EQUAL to an explicitly-empty request — the
    core-group ``group`` lands exactly there (#55 M3 retest live-fire:
    a real apiserver omitted the key and a byte-exact comparison
    fail-closed every core-group target). The normalization is one-way
    safe: a NON-empty expectation can never be satisfied by an omitted
    key (``"" != "apps"``), so a genuinely dropped non-empty field
    still fails closed. The verdict itself rides ``status.allowed``
    (SubjectAccessReviewStatus — "Allowed is required"); a top-level
    ``allowed`` is NOT read (the apiserver never emits one — R2 review
    Bug#1, pinned by
    ``test_ssar_verdict_toplevel_allowed_is_form_error``)."""
    spec = (response or {}).get("spec")
    echo = spec.get("resourceAttributes") if isinstance(spec, dict) else None
    if not isinstance(echo, dict):
        return "form_error", "receipt carries no spec.resourceAttributes echo"
    for field, want in (
        ("namespace", namespace), ("verb", verb),
        ("group", group), ("resource", resource),
    ):
        got = echo.get(field)
        if got is None:
            # omitempty contract: an omitted key and an explicit empty
            # string are the same value server-side.
            got = ""
        if got != want:
            return (
                "form_error",
                f"echo field {field!r} drifted: got {got!r}, asked {want!r}",
            )
    status = (response or {}).get("status")
    allowed = status.get("allowed") if isinstance(status, dict) else None
    if allowed is True:
        return "allowed", ""
    if allowed is False:
        return "denied", f"verb {verb!r} on {resource!r} is not granted"
    return "form_error", f"allowed field absent/invalid: {allowed!r}"


# ---------------------------------------------------------------------------
# §3 step 3 — real-request server-side dry-run arm gate (content-aware)
# ---------------------------------------------------------------------------
#
# SSAR (step 2) and the B85 verb-reconcile both answer a VERB+RESOURCE
# question: "may this carrier ``create``/``patch`` ``rolebindings``/...?".
# A write's acceptance can ALSO depend on the request BODY, which no
# verb-level gate can see. The canonical instance is K8s's RBAC
# privilege-escalation guard: creating a RoleBinding whose ``roleRef``
# grants permissions the creator does not itself hold (and lacks ``bind``
# for) is rejected 403 EVEN THOUGH ``create rolebindings`` IS granted — so
# SSAR returns ``allowed:true`` and B85 reconciles clean, the timer is
# armed, and it fires into a 403 that ``curl -sf`` swallows into an empty
# restore.log (the ServiceAccount 权限被撤销 self-recovery failure). Admission
# webhooks, ResourceQuota and immutability guards deny on content the same
# way. A ``?dryRun=All`` request runs the FULL apiserver pipeline (authn →
# authz INCLUDING the escalation guard → admission → validation) with the
# carrier's OWN token and persists nothing, so it surfaces exactly the
# content-dependent denial the live fire would hit.

#: Only an AUTHORIZATION denial fails closed. Authz is state-independent —
#: a dry-run 401/403 means the live fire WILL be rejected (escalation guard,
#: webhook deny, quota). Every other code passes and stays owned by the
#: existing gates: 2xx/409/404 are success-or-idempotent; 400/422/5xx can be
#: dry-run-vs-live artifacts (a json-patch ``replace`` on a path the inject
#: would create is 422 against the pre-injection baseline yet applies fine at
#: fire time), so failing closed on them would break recoveries that work.
_DRYRUN_DENY_CODES = frozenset({"401", "403"})


def build_dryrun_script(
    target_url: str, body: str, *, body_file: str = "",
    user_agent: str = "", delete_urls: Optional[list[str]] = None,
) -> str:
    """§3 step 3 — server-side dry-run of the EXACT restore request with
    the carrier's real token: the content-aware arm gate verb-level SSAR
    structurally cannot be (see the module comment above).

    Mirrors :func:`build_restore_script`'s request shape (same method,
    body/``body_file`` landing choice, User-Agent) with ``?dryRun=All``
    appended, and prints one labelled ``%{http_code}`` line per request
    (``MAIN=`` for the patch-domain restore, ``DEL<i>=`` for each
    ``extra_delete``) so :func:`dryrun_verdict` reads a binary verdict in
    the same ``-w %{http_code}`` form as :func:`build_probe_get_script`.
    Nothing is persisted (dry-run is side-effect-free — measured: a
    dry-run create of an absent object leaves it absent).
    """
    ua = (
        _UA_UNSAFE_CHARS_RE.sub("", str(user_agent))
        or _RESTORE_USER_AGENT_PREFIX
    )
    data_arg = f"-d @{body_file}" if body_file else f"-d {_shell_sq(body)}"
    parts = [
        f"C={_SA_CA_PATH}",
        f"T=$(cat {_SA_TOKEN_PATH})",
        (
            'echo "MAIN=$(curl -s -o /dev/null -w \'%{http_code}\' '
            f'-X PATCH --cacert $C -H "Authorization: Bearer $T" '
            f'-H "Content-Type: {_JSON_PATCH_CONTENT_TYPE}" '
            f'-H "User-Agent: {ua}" {data_arg} '
            f"'{target_url}?dryRun=All')\""
        ),
    ]
    for i, url in enumerate(delete_urls or []):
        parts.append(
            f'echo "DEL{i}=$(curl -s -o /dev/null -w \'%{{http_code}}\' '
            f'-X DELETE --cacert $C -H "Authorization: Bearer $T" '
            f'-H "User-Agent: {ua}" '
            f"'{url}?dryRun=All')\""
        )
    return "; ".join(parts)


def dryrun_verdict(output: str) -> tuple[str, str]:
    """Three-state verdict on a dry-run receipt:
    ``("ok"|"denied"|"form_error", detail)``.

    Conservative by construction — ONLY an authorization denial (401/403,
    :data:`_DRYRUN_DENY_CODES`) returns ``denied`` (fail-closed). ``ok``
    means every labelled code was non-denying (success, idempotent
    409/404, or a dry-run-only artifact this authz-focused gate does not
    own). ``form_error`` means the receipt carried no parseable
    ``KEY=<digits>`` line at all — the probe itself broke, which the
    caller treats as "did not confirm" (distinct from a confirmed denial).
    """
    codes: dict[str, str] = {}
    for line in (output or "").splitlines():
        key, sep, val = line.strip().partition("=")
        if sep and key.strip() and val.strip().isdigit():
            codes[key.strip()] = val.strip()
    if not codes:
        return (
            "form_error",
            f"dry-run receipt carried no http_code lines: {output[:120]!r}",
        )
    denied = {k: v for k, v in codes.items() if v in _DRYRUN_DENY_CODES}
    if denied:
        return (
            "denied",
            f"authorization denied {denied} on a dry-run of the exact "
            "restore request — the live fire would be rejected too "
            "(403 = a content-dependent guard such as the RBAC "
            "privilege-escalation check, an admission webhook, or quota)",
        )
    return "ok", ""


# ---------------------------------------------------------------------------
# Computed cert-payload generation (the sole in-cluster managed compute
# channel for a cryptographic fault payload — design D1/D2/D6)
# ---------------------------------------------------------------------------
#
# A fault whose payload is a COMPUTED artifact (an expired TLS certificate —
# notBefore/notAfter cryptographically signed into the past) can be authored
# neither by the LLM (it cannot compute a signature) nor on the operator host
# (``_run`` is the single kubectl-only execution seam — it cannot spawn
# openssl). The carrier Pod is the ONLY managed in-cluster compute venue,
# reached through ``_exec_in_carrier`` exactly like the SA-token GET probe /
# SSAR / arming scripts — so cert generation adds no new channel and touches
# no guard (ToolGuard allowlist / drift / readonly are all unchanged), and the
# payload script rides as a SINGLE argv element (never through the LLM
# tokenizer). Inject ops carry a SENTINEL value; the assembler resolves it
# in-carrier and backfills the real base64 BEFORE arming/inject. No sentinel
# in ``patches`` → this whole path is a no-op (patches stay byte-identical).

# Sentinel grammar (strict, fail-closed): a value carrying the
# ``<faultdrill:`` namespace prefix MUST fully match, else assembly aborts —
# a malformed sentinel must never be injected as a literal string. ``cn`` is
# charset-restricted so the unquoted ``-subj`` in the gen script is
# injection-safe.
_CERT_SENTINEL_RE = re.compile(
    r"<faultdrill:expired-cert\s+cn=(?P<cn>[A-Za-z0-9.*_-]+)"
    r"\s+part=(?P<part>crt|key)>"
)
# A fixed PAST validity window: notBefore/notAfter both in the past, so the
# served cert is unambiguously expired independent of the fire-time clock.
_CERT_NOT_BEFORE = "20200101000000Z"
_CERT_NOT_AFTER = "20200102000000Z"
_CERT_CRT_MARKER = "===FD_CRT==="
_CERT_KEY_MARKER = "===FD_KEY==="
_CERT_END_MARKER = "===FD_END==="


def build_cert_capability_probe_script() -> str:
    """Fail-closed capability probe (mirrors the curl GET probe): confirm
    the carrier image can sign a cert with a past notAfter BEFORE arming, so
    an openssl-less image (e.g. chaosblade-tool, which has sh+sleep+curl but
    no openssl) fails closed here — a PRE-ARMING abort retryable with a
    corrected ``carrier_image``, never a fire-time surprise. Always exits 0;
    the verdict rides a stdout marker (``openssl ca -help`` exits non-zero on
    some builds, so the ``&& / ||`` normalizes it)."""
    return (
        "command -v openssl >/dev/null 2>&1 || { echo FD_NO_OPENSSL; exit 0; }; "
        "openssl ca -help 2>&1 | grep -q -- -enddate "
        "&& echo FD_OPENSSL_CA_OK || echo FD_NO_CA_ENDDATE"
    )


def build_cert_gen_script(
    cn: str, *, workdir: str,
    not_before: str = _CERT_NOT_BEFORE, not_after: str = _CERT_NOT_AFTER,
) -> str:
    """Sign an EXPIRED cert (+ its matching key) for ``cn`` inside the
    carrier via a throwaway mini-CA. ``openssl ca -enddate`` is the only
    expiry control available across the images the cluster actually caches
    (``req``/``x509`` ``-not_after`` need OpenSSL 3.2+; the cached terway is
    1.1.1f, the ingress-controller 3.1.3 — both support ``ca -enddate``,
    neither the newer flags). The absolute ``workdir`` is baked into the
    openssl.cnf so its ``$dir`` macro resolves WITHOUT shell expansion, and
    the QUOTED heredoc keeps ``$dir`` literal for openssl (not the shell) —
    the exact form verified end-to-end in-cluster. ``cn`` is charset-
    restricted by :func:`_cert_sentinel_specs`. Emits crt-b64 / key-b64 /
    notAfter between markers for :func:`_parse_cert_gen_output`."""
    return f"""set -e
D={workdir}
rm -rf "$D"; mkdir -p "$D/newcerts"; cd "$D"
: > index.txt; echo 1000 > serial
cat > cnf <<'FD_CNF'
[ca]
default_ca = ca_default
[ca_default]
dir = {workdir}
database = $dir/index.txt
serial = $dir/serial
new_certs_dir = $dir/newcerts
certificate = $dir/ca.crt
private_key = $dir/ca.key
default_md = sha256
policy = p_any
email_in_dn = no
rand_serial = no
unique_subject = no
[p_any]
commonName = supplied
[req]
distinguished_name = dn
[dn]
FD_CNF
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 3650 -subj "/CN=fd-drill-ca" -config cnf >/dev/null
openssl req -newkey rsa:2048 -nodes -keyout e.key -out e.csr -subj "/CN={cn}" -config cnf >/dev/null
openssl ca -batch -config cnf -startdate {not_before} -enddate {not_after} -in e.csr -out e.crt -notext >/dev/null
echo {_CERT_CRT_MARKER}
base64 -w0 e.crt
echo
echo {_CERT_KEY_MARKER}
base64 -w0 e.key
echo
echo {_CERT_END_MARKER}
openssl x509 -in e.crt -noout -enddate
rm -rf "$D"
"""


def _between_markers(text: str, start: str, end: str) -> str:
    """The whitespace-stripped slice between two literal markers."""
    m = re.search(
        re.escape(start) + r"\s*(.*?)\s*" + re.escape(end), text, re.S,
    )
    return m.group(1).strip() if m else ""


def _parse_cert_gen_output(out: str, cn: str) -> tuple[str, str, str]:
    """Split the in-carrier generation output into ``(crt_b64, key_b64,
    notAfter)``. Fail-closed on a missing half — a cert without its matching
    key (or vice versa) is not injectable — or a missing notAfter (the
    white-box verdict's evidence)."""
    crt = _between_markers(out, _CERT_CRT_MARKER, _CERT_KEY_MARKER)
    key = _between_markers(out, _CERT_KEY_MARKER, _CERT_END_MARKER)
    if not crt or not key:
        raise _AssemblyError(
            f"cert generation for cn={cn!r} did not yield both crt and key "
            f"(carrier output: {out.strip()[:160]!r})"
        )
    tail = out.split(_CERT_END_MARKER, 1)[-1]
    m = re.search(r"notAfter=(.+)", tail)
    not_after = m.group(1).strip() if m else ""
    if not not_after:
        raise _AssemblyError(
            f"cert generation for cn={cn!r} yielded no notAfter "
            f"(output tail: {tail.strip()[:120]!r})"
        )
    return crt, key, not_after


def _cert_sentinel_specs(patches: list[dict]) -> dict[str, dict[str, int]]:
    """Scan inject patches for computed cert-payload sentinels →
    ``{cn: {"crt": op_index, "key": op_index}}``. Fail-closed: (1) any value
    carrying the ``<faultdrill:`` namespace prefix that is not a well-formed
    expired-cert sentinel aborts (a malformed sentinel must never be injected
    as a literal string — that would silently land garbage and pass
    readback); (2) a duplicate part or a cn missing its crt/key partner
    aborts (the pair is atomic)."""
    specs: dict[str, dict[str, int]] = {}
    for i, op in enumerate(patches):
        val = op.get("value") if isinstance(op, dict) else None
        if not isinstance(val, str) or "<faultdrill:" not in val:
            continue
        m = _CERT_SENTINEL_RE.fullmatch(val.strip())
        if not m:
            raise _AssemblyError(
                f"malformed computed-payload sentinel in patches[{i}]: "
                f"{val!r} — expected the exact form "
                f"'<faultdrill:expired-cert cn=<host> part=crt|key>' "
                f"(cn charset [A-Za-z0-9.*_-])"
            )
        cn, part = m.group("cn"), m.group("part")
        bucket = specs.setdefault(cn, {})
        if part in bucket:
            raise _AssemblyError(
                f"duplicate cert-payload part={part!r} for cn={cn!r} in "
                f"patches — exactly one crt and one key op per cn"
            )
        bucket[part] = i
    for cn, parts in specs.items():
        if set(parts) != {"crt", "key"}:
            raise _AssemblyError(
                f"cert payload for cn={cn!r} needs BOTH part=crt and "
                f"part=key ops (got {sorted(parts)}) — a cert without its "
                f"matching key is not injectable"
            )
    return specs


# ---------------------------------------------------------------------------
# Plaintext→base64 sentinel resolution (the Secret/ConfigMap credential
# cousin of the computed-cert payload above — but pure Python, no carrier)
# ---------------------------------------------------------------------------
#
# A config-object content fault (Secret/ConfigMap credential tamper) injects a
# value K8s stores base64-encoded (``.data.<key>`` is base64 by contract).
# base64 is a DETERMINISTIC fact, yet the main path used to make the LLM
# hand-encode it into the inject op's ``value`` — slow (byte-by-byte derivation
# in reasoning) and error-prone: a mistyped constant lands the WRONG credential
# yet still passes landing readback (readback verifies the patch landed, not
# that it matches intent), and a mistyped gate constant false-negatives the
# verdict. Same class the ``value:null`` live-capture already solves on the
# RESTORE side (§2): deterministic bytes must not round-trip through LLM tokens.
#
# So the inject op authors the PLAINTEXT via a ``<faultdrill:b64>`` sentinel;
# the assembler base64-encodes it HERE (pure Python — unlike the cert payload it
# needs no openssl/carrier compute) and backfills ``value`` in place BEFORE
# anything is built, so the inject patch, the landing readback and the receipt
# all carry the program-encoded base64. Resolving in §0 (not §4.5 like cert)
# means a malformed sentinel aborts with zero stack to clean. No sentinel →
# no-op (patches stay byte-identical).
_B64_SENTINEL_PREFIX = "<faultdrill:b64>"
# Strict, fail-closed: the plaintext is everything after the prefix and MUST be
# non-empty (an empty credential is never injectable). The plaintext never
# reaches a shell — it goes straight into base64.b64encode — so it needs no
# charset restriction (unlike the cert ``cn``, interpolated into an unquoted
# openssl ``-subj``).
_B64_SENTINEL_RE = re.compile(r"<faultdrill:b64>(?P<plaintext>.+)", re.DOTALL)


def resolve_b64_sentinels(patches: list[dict]) -> int:
    """Backfill every ``<faultdrill:b64><plaintext>`` sentinel ``value`` in
    ``patches`` (in place) with the deterministic base64 of ``plaintext``;
    return how many ops were resolved. Fail-closed: a value carrying the
    ``<faultdrill:b64>`` prefix with an empty/absent plaintext aborts (an empty
    credential must never be silently injected). Ops without the sentinel —
    including ``value:null`` live-capture restore ops — are left byte-identical.
    Pure Python: no carrier, no guard, no transport touched.

    A ``<faultdrill:``-prefixed value that is NEITHER a well-formed b64 sentinel
    (resolved here) NOR a well-formed cert sentinel (resolved at §4.5) still
    aborts in :func:`_cert_sentinel_specs`, so no malformed sentinel of either
    kind is ever injected as a literal string.
    """
    resolved = 0
    for i, op in enumerate(patches):
        val = op.get("value") if isinstance(op, dict) else None
        if not isinstance(val, str) or not val.startswith(_B64_SENTINEL_PREFIX):
            continue
        m = _B64_SENTINEL_RE.fullmatch(val)
        if not m:
            raise _AssemblyError(
                f"malformed base64 sentinel in patch op {i}: {val!r} — "
                f"expected the exact form '<faultdrill:b64><plaintext>' with a "
                f"non-empty plaintext (an empty credential is never injectable)"
            )
        plaintext = m.group("plaintext")
        op["value"] = base64.b64encode(
            plaintext.encode("utf-8")
        ).decode("ascii")
        resolved += 1
    return resolved


# ---------------------------------------------------------------------------
# Recipe verification (baseline precheck + landing readback)
# ---------------------------------------------------------------------------

_UNRESOLVED = object()


def _resolve_json_path(doc: object, path: str) -> Any:
    """Resolve a JSON-pointer-style path with array-index support.
    Returns ``_UNRESOLVED`` when any segment fails (the caller then
    treats the op as unverifiable — conservative-keep, the same
    orientation as the reconciler's readback guards)."""
    if not path.startswith("/"):
        return _UNRESOLVED
    cur = doc
    for raw_seg in path.strip("/").split("/"):
        seg = raw_seg.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif (
            isinstance(cur, list) and seg.lstrip("-").isdigit()
            and seg.isdigit() and int(seg) < len(cur)
        ):
            cur = cur[int(seg)]
        else:
            return _UNRESOLVED
    return cur


def verify_restore_baseline(target_json: dict, restore_patches: list) -> list[str]:
    """Baseline precheck (armed BEFORE the stack is built — an early
    fail builds nothing): every restore op must be CONSISTENT with the
    target's CURRENT (pre-injection = baseline) state — a ``replace``
    whose value differs from the live value, or a ``remove``/``add``
    whose existence contradicts the op, would make the timer fire a
    MUTATION of the baseline instead of a restore. Unresolvable paths
    are skipped (conservative-keep)."""
    violations: list[str] = []
    for op in restore_patches or []:
        if not isinstance(op, dict):
            violations.append(f"non-object restore op: {op!r}")
            continue
        path = str(op.get("path") or "")
        kind_op = str(op.get("op") or "")
        current = _resolve_json_path(target_json, path)
        if current is _UNRESOLVED:
            continue
        if kind_op == "replace":
            if current != op.get("value"):
                violations.append(
                    f"replace {path!r}: restore value {op.get('value')!r} "
                    f"differs from the live baseline {current!r} — the timer "
                    "would mutate the target, not restore it"
                )
        elif kind_op == "remove":
            violations.append(
                f"remove {path!r}: the path already exists at baseline — "
                "removing it at fire time would delete baseline state"
            )
        elif kind_op == "add":
            violations.append(
                f"add {path!r}: the path already exists at baseline — the "
                "restore add collides with baseline state"
            )
        else:
            violations.append(f"unsupported restore op {kind_op!r} at {path!r}")
    return violations


def _capture_live_baselines(
    target_json: dict, restore_patches: list,
) -> int:
    """Substitute null/omitted ``value`` on ``replace`` restore ops with
    the LIVE pre-injection baseline (in place; returns the count).

    The live-capture form removes the LLM from the byte-exact relay
    path: cluster-state-dependent baselines (a live Corefile, a current
    selector) are read by the tool itself at assembly time — the same
    object the baseline guard verifies against — so transcription
    drift is structurally impossible for captured ops (2026-09-21
    NXDOMAIN: the plan relay froze a 2-space indent over a 1-space
    live Corefile; the guard caught it, but the retry cost 118s and a
    second call). The captured values flow into the timer payload and
    the receipt verbatim (audit).

    A capture whose path is absent on the live target fails closed:
    no baseline exists to capture, and a replace fired on an absent
    path at TTL would strand the fault.
    """
    captured = 0
    for op in restore_patches or []:
        if not isinstance(op, dict):
            continue
        if str(op.get("op") or "") != "replace":
            continue
        if op.get("value") is not None:
            continue
        path = str(op.get("path") or "")
        live_value = _resolve_json_path(target_json, path)
        if live_value is _UNRESOLVED:
            raise _AssemblyError(
                f"restore replace {path!r}: value omitted (live-capture "
                "requested) but the path is absent on the live target — "
                "no baseline exists to capture"
            )
        op["value"] = live_value
        captured += 1
    return captured


def _ws_normalised(value: Any) -> str:
    """Collapse every whitespace run to a single space (indentation-
    normal form) — the comparison shape for config-text targets whose
    format ignores whitespace (Corefile et al.)."""
    return " ".join(str(value).split())


def _first_diff_window(expected: Any, live: Any, span: int = 48) -> str:
    """First-divergence window between two values. Diagnosis: an
    ecosystem controller normalising indentation reads as a whitespace
    diff at byte N; a real miss diverges semantically — the window
    tells them apart at a glance (2026-09-21 NXDOMAIN: the valueless
    miss forced a 60s manual readback round to resolve exactly this
    question)."""
    exp_s, live_s = str(expected), str(live)
    n = min(len(exp_s), len(live_s))
    for i in range(n):
        if exp_s[i] != live_s[i]:
            lo = max(0, i - span)
            return (
                f"first diff at byte {i}: expected "
                f"...{exp_s[lo:i + span]!r} vs live ...{live_s[lo:i + span]!r}"
            )
    if len(exp_s) != len(live_s):
        i = n
        lo = max(0, i - span)
        longer = "expected" if len(exp_s) > len(live_s) else "live"
        return (
            f"first diff at byte {i} ({longer} is longer): expected "
            f"...{exp_s[lo:i + span]!r} vs live ...{live_s[lo:i + span]!r}"
        )
    return "values stringify equal"


def verify_patches_landed(
    target_json: dict, patches: list, *,
    whitespace_insensitive: bool = False,
) -> tuple[list[str], list[str]]:
    """Post-injection landing readback (ND6): every fault patch must be
    visible on the live object — replace/add resolve to the op value,
    remove resolves to absent. Unresolvable paths count as NOT verified
    (reported, not fatal — the patch command's exit code already
    carries the primary verdict).

    Returns ``(misses, notes)``. A miss carries the first-divergence
    window (expected vs live), so the LLM can distinguish whitespace
    normalisation by an ecosystem controller from a real miss without
    a manual readback round.

    ``whitespace_insensitive`` (opt-in per call): for config-text
    targets whose format ignores whitespace, a whitespace-normalised
    match counts as LANDED (recorded in notes, not misses). The
    default exact mode is unchanged — formats where whitespace may
    carry semantics (e.g. Python in a ConfigMap) must stay exact.
    """
    misses: list[str] = []
    notes: list[str] = []
    for op in patches or []:
        if not isinstance(op, dict):
            continue
        path = str(op.get("path") or "")
        kind_op = str(op.get("op") or "")
        current = _resolve_json_path(target_json, path)
        if kind_op in ("replace", "add"):
            if current is _UNRESOLVED or current != op.get("value"):
                if (
                    whitespace_insensitive
                    and current is not _UNRESOLVED
                    and _ws_normalised(current)
                    == _ws_normalised(op.get("value"))
                ):
                    notes.append(
                        f"{kind_op} {path!r} landed (whitespace-normalised "
                        "by an ecosystem controller — exact compare "
                        f"differs: {_first_diff_window(op.get('value'), current)})"
                    )
                    continue
                detail = ""
                if current is not _UNRESOLVED:
                    detail = (
                        " ("
                        + _first_diff_window(op.get("value"), current)
                        + ")"
                    )
                misses.append(
                    f"{kind_op} {path!r} not visible on the target{detail}"
                )
        elif kind_op == "remove":
            if current is not _UNRESOLVED:
                misses.append(f"remove {path!r}: path still present")
    return misses, notes


# ---------------------------------------------------------------------------
# Assembly core
# ---------------------------------------------------------------------------


class _AssemblyError(Exception):
    """Fail-closed abort (pre-arming): the message is reported verbatim
    and the built stack is cleaned. Post-arming failures raise nothing —
    the carrier must survive (see module docstring)."""


async def _run(subcommand: str, v_args: list[str], kubeconfig: str, *,
               stdin_data: str = "", timeout: float = 30.0):
    """Single execution seam — the provider's ``_kubectl`` (attribute
    access, so the tests' single patch point covers every call here;
    the same patch point the restore tests use)."""
    from . import provider as _provider_mod

    return await _provider_mod._kubectl(
        subcommand, v_args, kubeconfig,
        stdin_data=stdin_data, timeout=timeout,
    )


def _error_text(result, *, limit: int = 200) -> str:
    """Failure detail from a ``CommandResult`` — stderr AND stdout merged.

    The channel layer now RESTORES the stream split on arrival (sentinel
    unfold in ``transports.protocol.parse_wiz_output`` — the B86 root
    fix), so ``result.stderr`` carries the remote error text again. The
    merge stays as defense in depth: markerless legacy receipts still
    surface the fold on stdout, and a stderr-only read renders those
    detail-less (B86 face 2, the original incident). stderr leads,
    stdout follows (partial output).
    """
    parts = (
        str(result.stderr or "").strip(),
        str(result.stdout or "").strip(),
    )
    return "\n".join(p for p in parts if p)[:limit]


async def _exec_in_carrier(
    pod: str, namespace: str, script: str, kubeconfig: str,
) -> str:
    """Run one ``sh -c`` script inside the carrier (the standard's only
    exec form). Returns stdout; raises ``_AssemblyError`` on a failed
    dispatch (non-zero exit / empty stdout)."""
    result = await _run(
        "exec", [pod, "-n", namespace, "--", "sh", "-c", script],
        kubeconfig, timeout=60.0,
    )
    if result.exit_code != 0:
        raise _AssemblyError(
            f"carrier exec failed (rc={result.exit_code}): "
            f"{_error_text(result)}"
        )
    return str(result.stdout or "")


async def _cleanup_stack(name: str, namespace: str, kubeconfig: str) -> list[str]:
    """Fail-closed cleanup: the four-way delete, reverse-dependency
    order (pod → rolebinding → role → sa), ``--ignore-not-found`` so a
    replay after a partial delete converges. Returns the failures (an
    honest report beats a silent orphan — the skeleton self-expires and
    an unbound Role/SA is inert, but the residue stays on the record).

    The pod delete carries ``--force --grace-period=0`` (the
    ``_debug_pod.delete_debug_pod`` form): a Pod's default termination
    grace is 30s, ``kubectl delete`` WAITS it out, and the 30s command
    timeout fires first — reporting a failure for a delete that was in
    fact accepted (#55 M3 retest live-fire: cleanup_failures carried a
    bogus 30s pod-delete timeout while the pod was actually converging
    to gone). The skeleton is a stateless ``sleep`` process — force
    deletion has nothing to lose. RBAC objects carry no grace concept
    and take the plain form."""
    failures: list[str] = []
    for kind in ("pod", "rolebinding", "role", "serviceaccount"):
        v_args = [kind, name, "-n", namespace, "--ignore-not-found"]
        if kind == "pod":
            v_args += ["--force", "--grace-period=0"]
        result = await _run("delete", v_args, kubeconfig)
        if result.exit_code != 0:
            failures.append(
                f"delete {kind} {name}: {_error_text(result, limit=120)}"
            )
    return failures


async def _wait_pod_running(
    name: str, namespace: str, kubeconfig: str, *,
    timeout_seconds: float = 120.0,
) -> None:
    """Poll the skeleton to Running (image pull / scheduling). A
    terminal phase or a timeout is a fail-closed abort — §8: no
    retry-detours, report and fail the task honestly."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = await _run(
            "get", ["pod", name, "-n", namespace, "-o", "json"], kubeconfig,
        )
        if result.exit_code == 0:
            try:
                phase = str(
                    (json.loads(result.stdout).get("status") or {}).get("phase")
                    or ""
                )
            except (ValueError, TypeError):
                phase = ""
            if phase == "Running":
                return
            if phase in ("Failed", "Succeeded"):
                raise _AssemblyError(
                    f"carrier pod reached terminal phase {phase!r} before arming"
                )
        await asyncio.sleep(2)
    raise _AssemblyError(
        f"carrier pod did not reach Running within {timeout_seconds:.0f}s "
        "(unschedulable node / image not pullable) — refusing to arm"
    )


async def _attach_self_termination(
    carrier: str, namespace: str, kubeconfig: str,
) -> tuple[bool, str]:
    """Wire the §4 ownerRef GC self-termination — the W-83-3B root-fix.

    Three deterministic steps, in the order the apiserver requires:
    1. read the carrier pod's UID (the owner must EXIST with a UID before
       a dependent may reference it);
    2. append the self-teardown grant (``delete pods`` resourceNames-locked
       to the carrier) to its Role as an INDEPENDENT ``/rules/-`` json-patch
       (:func:`build_self_teardown_rule` — never the create broadcast);
    3. set ``ownerReferences→Pod`` on the SA/Role/RoleBinding
       (:func:`build_owner_ref_patch`), so the apiserver's GC cascades all
       three from the pod's deletion.

    Returns ``(enabled, self_delete_url)``. ``enabled`` is True ONLY when
    EVERY step landed — the three are a single indivisible mechanism: a
    self-delete tail without the ownerRef would delete the pod and ORPHAN
    the three live RBAC objects (strictly worse than the agent-teardown
    status quo, which at least finds all four by name), and an ownerRef
    without the grant leaves the timer unable to delete the pod. So on ANY
    failure the caller suppresses the tail and cleanup falls back to the
    four-way teardown. Fail-safe, never fail-worse. The grant's RBAC-cache
    convergence (seconds) is a non-issue: the tail fires ≥25min later."""
    uid_result = await _run(
        "get",
        ["pod", carrier, "-n", namespace, "-o", "jsonpath={.metadata.uid}"],
        kubeconfig,
    )
    pod_uid = str(uid_result.stdout or "").strip()
    if uid_result.exit_code != 0 or not pod_uid:
        return False, ""
    grant = await _run(
        "patch",
        ["role", carrier, "-n", namespace, "--type=json", "-p",
         json.dumps([{"op": "add", "path": "/rules/-",
                      "value": build_self_teardown_rule(carrier)}])],
        kubeconfig,
    )
    if grant.exit_code != 0:
        return False, ""
    owner_patch = build_owner_ref_patch(carrier, pod_uid)
    for kind in ("serviceaccount", "role", "rolebinding"):
        res = await _run(
            "patch",
            [kind, carrier, "-n", namespace, "--type=merge", "-p", owner_patch],
            kubeconfig,
        )
        if res.exit_code != 0:
            return False, ""
    return True, rest_url_for("pod", carrier, namespace) + "?gracePeriodSeconds=0"


async def _derive_tolerations(kubeconfig: str) -> list[dict]:
    """Best-effort taint probe (§1 scheduling warning): enterprise
    clusters commonly taint EVERY node; a carrier without tolerations
    would sit Pending forever. Tolerances derived for PRE-EXISTING
    taints only (NoSchedule/NoExecute, builtin taints excluded — every
    pod gets those). A failed probe yields no tolerations — the
    readiness gate still fails closed if the pod cannot schedule."""
    result = await _run("get", ["nodes", "-o", "json"], kubeconfig)
    if result.exit_code != 0:
        return []
    try:
        nodes = json.loads(result.stdout).get("items") or []
    except (ValueError, TypeError):
        return []
    seen: dict[tuple, dict] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for taint in (node.get("spec") or {}).get("taints") or []:
            if not isinstance(taint, dict):
                continue
            if taint.get("effect") not in ("NoSchedule", "NoExecute"):
                continue
            key = str(taint.get("key") or "")
            if not key or key.startswith(_BUILTIN_TAINT_PREFIXES):
                continue
            seen[(key, str(taint.get("value") or ""), str(taint.get("effect")))] = {
                "key": key,
                "operator": "Equal",
                "value": str(taint.get("value") or ""),
                "effect": str(taint.get("effect")),
            }
    return list(seen.values())


def _parse_patch_list(raw: str, field: str) -> list[dict]:
    """Parse one recipe JSON array (patches / restorePatches). Fail
    closed on anything that is not a JSON array of objects."""
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list) or not parsed:
        raise ValueError(f"{field} must be a non-empty JSON array of patch ops")
    if not all(isinstance(op, dict) for op in parsed):
        raise ValueError(f"{field} entries must all be JSON objects")
    return parsed


def _parse_delete_list(raw: str, field: str) -> list[dict]:
    """Parse the OPTIONAL ``extra_delete`` recipe (a JSON array of prop
    objects the recovery lifecycle must DELETE — the create-domain inverse
    of the inject plan's non-patch writes). Empty/omitted → ``[]`` (a
    pure-patch fault has no prop objects). Fail closed on malformed JSON,
    a non-array, or a non-object entry."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"{field} must be a JSON array of delete targets")
    if not all(isinstance(op, dict) for op in parsed):
        raise ValueError(f"{field} entries must all be JSON objects")
    return parsed


def _normalize_delete_targets(
    raw_entries: Optional[list], default_namespace: str,
) -> list[dict]:
    """Validate + normalize the optional ``extra_delete`` recipe into
    ``[{"kind", "name", "namespace"}]`` (canonical kind, namespace
    defaulted to the target's). These are the create-domain prop objects
    the inject plan built outside the patch domain; their recovery inverse
    is a DELETE (no baseline exists to restore).

    Fail-closed (before anything is built): an unknown kind, a nameless
    entry, a cluster-scoped kind, or a cross-namespace target (the
    namespaced carrier Role cannot authorize a delete outside its own
    namespace — a ClusterRole variant is out of M1 scope) all abort.
    """
    normalized: list[dict] = []
    for entry in raw_entries or []:
        if not isinstance(entry, dict):
            raise _AssemblyError(
                f"extra_delete entries must be JSON objects, got {entry!r}"
            )
        try:
            kind = canonical_kind(str(entry.get("kind") or ""))
        except ValueError as exc:
            raise _AssemblyError(f"extra_delete: {exc}") from exc
        name = str(entry.get("name") or "").strip()
        if not name:
            raise _AssemblyError(
                f"extra_delete entry for kind {kind!r} carries no name"
            )
        if _KIND_REST_MAP[kind][3] is False:
            raise _AssemblyError(
                f"extra_delete kind {kind!r} is cluster-scoped — the M1 "
                "namespaced carrier Role cannot authorize its deletion"
            )
        ns = str(entry.get("namespace") or "").strip() or default_namespace
        if ns != default_namespace:
            raise _AssemblyError(
                f"extra_delete target {kind}/{name} is in namespace {ns!r}, "
                f"but the carrier Role is namespaced to {default_namespace!r} "
                "— a cross-namespace delete needs a ClusterRole (out of M1 "
                "scope)"
            )
        normalized.append({"kind": kind, "name": name, "namespace": ns})
    return normalized


def build_carrier_artifact(
    *, name: str, namespace: str, task_id: str, rules: list[dict],
    duration_seconds: int, deadline_epoch: float,
    recovery_handle: dict,
) -> dict:
    """The ``recovery_carrier`` artifact dict — schema-EQUIVALENT to the
    LLM path's screener registration (tool_screener inline literal) with
    the arming stamp pre-applied (``recovery_armed`` + deadline, the
    ``_mark_bounded_host_recovery`` field semantics), so every consumer
    — vehicle exemption chain, finalize/recover stack cleanup, armed
    gate, sweep — reads it unchanged. ``created_tool_call_id`` /
    ``host_exec_*`` id fields are left empty HERE: only the receipt
    branch (which sees the ToolMessage) can fill them."""
    verbs: set[str] = set()
    for rule in rules:
        verbs.update(rule.get("verbs") or [])
    return {
        "artifact_id": f"recovery_carrier:{namespace}/{name}",
        "type": "recovery_carrier",
        "kind": "pod",
        "status": "recovery_armed",
        "task_id": str(task_id or ""),
        "name": name,
        "namespace": namespace,
        "operation_family": "recovery_carrier",
        "created_tool_call_id": "",
        "rbac_family": [
            {"kind": "serviceaccount", "name": name, "namespace": namespace},
            {"kind": "role", "name": name, "namespace": namespace,
             "verbs": sorted(verbs)},
            {"kind": "rolebinding", "name": name, "namespace": namespace},
        ],
        "cleanup": [
            {"tool": "kubectl", "subcommand": "delete",
             "v_args": f"pod {name} -n {namespace} --ignore-not-found"},
            {"tool": "kubectl", "subcommand": "delete",
             "v_args": f"rolebinding {name} -n {namespace} --ignore-not-found"},
            {"tool": "kubectl", "subcommand": "delete",
             "v_args": f"role {name} -n {namespace} --ignore-not-found"},
            {"tool": "kubectl", "subcommand": "delete",
             "v_args": f"serviceaccount {name} -n {namespace} --ignore-not-found"},
        ],
        "recovery_timeout_seconds": int(duration_seconds),
        "recovery_deadline_epoch": float(deadline_epoch),
        "host_exec_tool_call_id": "",
        "host_exec_seen_ids": [],
        # Symmetric delete-side replay guard (case #13): the arming ledger
        # above stops a re-seen exec re-stamping; this one stops a re-seen
        # ``kubectl delete`` re-disarming. Declared together so the carrier's
        # two replay-guard ledgers are both present from birth — collect's
        # delete branch would otherwise create it lazily via setdefault.
        "disarm_seen_ids": [],
        "recovery_handle": recovery_handle,
    }


async def assemble_recovery_carrier(
    *,
    target_kind: str,
    target_name: str,
    target_namespace: str,
    patches: list[dict],
    restore_patches: list[dict],
    duration_seconds: int,
    kubeconfig: str = "",
    task_id: str = "",
    carrier_image: str = "",
    readback_compare: str = "auto",
    extra_delete: Optional[list[dict]] = None,
) -> dict:
    """Assemble + arm + inject. Returns the receipt dict (see the tool
    docstring). Every fail-closed abort cleans the built stack first
    (§8: honest failure, no orphaned authorization).

    ``carrier_image`` (optional): the planning-phase image decision —
    the LLM's §9 selection (probe candidates ∩ evidence archive, first
    hit). When given it MUST be inside the effective allowlist union
    (configured ∪ auto-discovered — the same seam the classifier
    legislates); anything outside is fail-closed BEFORE the stack is
    built (allowlist semantics are never relaxed). Empty → the
    pool-local ``select_carrier_image`` fallback decides alone.

    ``readback_compare`` (B4): "auto" (default — ConfigMap resolves to
    whitespace-insensitive, everything else exact), or an explicit
    "exact" / "whitespace_insensitive" override."""
    from chaos_agent.config.settings import settings

    # ---- 0. Input validation (fail before anything is built) --------
    raw_readback = str(readback_compare or "auto").strip().lower()
    if raw_readback not in ("auto", "exact", "whitespace_insensitive"):
        raise _AssemblyError(
            f"readback_compare {readback_compare!r} must be 'auto', 'exact' "
            "or 'whitespace_insensitive'"
        )
    kind = canonical_kind(target_kind)
    # B4 'auto' resolution per target kind: ConfigMap is the config-text
    # family whose ecosystem controllers rewrite whitespace (Corefile
    # re-indentation etc.), so a whitespace-normalised match counts as
    # landed; every other kind keeps exact — whitespace may carry
    # semantics (e.g. Python) and leniency there would mask true misses.
    if raw_readback == "auto":
        readback_mode = (
            "whitespace_insensitive" if kind == "configmap" else "exact"
        )
        readback_label = f"auto->{readback_mode}"
    else:
        readback_mode = raw_readback
        readback_label = readback_mode
    name = str(target_name or "").strip()
    namespace = str(target_namespace or "").strip() or "default"
    if not name:
        raise _AssemblyError("target_name is required")
    if _KIND_REST_MAP[kind][3] is False:
        raise _AssemblyError(
            f"target kind {kind!r} is cluster-scoped — the M1 four-object "
            "namespaced stack cannot authorize it (five-object variant "
            "is out of M1 scope)"
        )
    max_sleep = int(
        getattr(settings, "recovery_carrier_max_sleep_seconds", None)
        or _MAX_SLEEP_SECONDS
    )
    duration = int(duration_seconds)
    # ``duration`` arriving here IS the fault's own recovery timer (D+G —
    # pinned at the registry dispatch point under the two-number window
    # contract; the carrier's restore sleep arms to this value verbatim).
    # The skeleton is capped at ``max_sleep`` (a conservative carrier
    # lifetime bound), so the window must leave the restore timer the
    # FULL forensics buffer after its fire: duration + buffer ≤ max_sleep.
    # R2 review Bug#2: duration=86399 used to pass the old 1..max_sleep-1
    # bound while the cap squeezed the post-fire margin to 1s minus the
    # run→arm delay — the carrier died before the restore curl could
    # land, stranding the fault past TTL.
    duration_max = max_sleep - _SKELETON_BUFFER_SECONDS
    if not 0 < duration <= duration_max:
        raise _AssemblyError(
            f"duration_seconds {duration} outside 1..{duration_max} — the "
            f"{_SKELETON_BUFFER_SECONDS}s restore-forensics buffer must "
            f"survive the skeleton cap ({max_sleep}s), else the carrier "
            "dies before the restore timer's fire completes"
        )
    # Image resolution: the caller's planning-phase decision wins (the
    # LLM owns the §9 evidence the selector lacks — probe candidates ∩
    # evidence archive), gated on the allowlist union; empty falls back
    # to the pool-local selector. #55 M3 live run: the LLM had decided
    # terway (node-cached 37/37, empirically curl-capable) while the old
    # selector alone picked an unpullable curlimages/curl — the decision
    # channel is what was missing, not just a better heuristic.
    image = str(carrier_image or "").strip()
    if image:
        from chaos_agent.agent.providers.registry import FaultProviderRegistry
        if image not in FaultProviderRegistry.recovery_carrier_allowed_images():
            raise _AssemblyError(
                f"carrier_image {image!r} is outside the recovery-carrier "
                "allowlist (configured ∪ auto-discovered) — allowlist "
                "semantics are never relaxed by an override"
            )
    else:
        image = select_carrier_image()
    target = {"kind": kind, "name": name, "namespace": namespace}
    target_url = rest_url_for(kind, name, namespace)
    # Recovery-completeness inverse: the create-domain prop objects the
    # inject plan builds OUTSIDE the patch domain (frontmatter
    # mechanism_writes) have no baseline to restore — their inverse is a
    # DELETE. Normalized here (fail-closed before anything is built) so
    # the RBAC derivation, the per-family SSAR and the timer payload all
    # draw on ONE validated list.
    delete_targets = _normalize_delete_targets(extra_delete, namespace)
    delete_urls = [
        rest_url_for(d["kind"], d["name"], d["namespace"])
        for d in delete_targets
    ]

    steps: list[dict] = []

    def _step(label: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": label, "ok": bool(ok), "detail": detail[:300]})

    # ---- 0.5 Deterministic value resolution (before anything is built) --
    # Resolve ``<faultdrill:b64><plaintext>`` sentinels to base64 in pure
    # Python so no hand-transcribed base64 constant rides the inject/restore
    # patch (mirrors the §2 value:null live-capture: deterministic bytes never
    # round-trip through LLM tokens). Fail-closed on a malformed sentinel HERE
    # — before any stack object exists, so there is nothing to clean up.
    b64_inject = resolve_b64_sentinels(patches)
    b64_restore = resolve_b64_sentinels(restore_patches)
    if b64_inject or b64_restore:
        _step(
            "b64_resolve", True,
            f"base64-encoded {b64_inject} inject + {b64_restore} restore "
            f"plaintext sentinel value(s)",
        )

    # ---- 1. Recipe → RBAC (same-source derivation) ------------------
    # The Role carries the target's restore PATCH ∪ every extra_delete
    # prop object's DELETE — one rule per resource family (derive_role_
    # rules unions verbs within a family), so a multi-family set renders
    # through the two-step construction (create + json-patch append).
    writes = [{"kind": kind, "methods": ["PATCH"]}]
    writes.extend(
        {"kind": d["kind"], "methods": ["DELETE"]} for d in delete_targets
    )
    rules = derive_role_rules(writes)

    # ---- 2. Baseline precheck (before the stack: an early fail
    #         builds nothing) -----------------------------------------
    result = await _run(
        "get", [kind, name, "-n", namespace, "-o", "json"], kubeconfig,
    )
    if result.exit_code != 0:
        raise _AssemblyError(
            f"target read for baseline precheck failed: "
            f"{_error_text(result)}"
        )
    try:
        target_json = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise _AssemblyError("target read returned unparseable JSON") from None
    # Null-value replace restore ops capture the live baseline HERE —
    # deterministic bytes must not round-trip through LLM tokens (the
    # captured values ride the timer body and the receipt verbatim).
    captured_count = _capture_live_baselines(target_json, restore_patches)
    violations = verify_restore_baseline(target_json, restore_patches)
    if violations:
        raise _AssemblyError(
            "restore recipe is not baseline-consistent (armed timer would "
            "mutate the target, not restore it): " + "; ".join(violations)
        )
    _step(
        "baseline_precheck", True,
        (f"live-captured {captured_count} replace baseline value(s) "
         f"(readback={readback_label})") if captured_count else
        f"readback={readback_label}",
    )

    # ---- 3. Stack build (SA → Role → RoleBinding → Pod) -------------
    carrier = carrier_name(task_id, target, salt=str(time.time()))
    role_plan = render_role_commands(carrier, namespace, rules)
    tolerations = await _derive_tolerations(kubeconfig)
    built: list[str] = []

    async def _fail_closed(reason: str) -> dict:
        cleanup_failures = await _cleanup_stack(carrier, namespace, kubeconfig)
        _step("cleanup", not cleanup_failures, "; ".join(cleanup_failures))
        return {
            "status": "failed",
            "error": reason,
            "cleanup_failures": cleanup_failures,
            "carrier": {"name": carrier, "namespace": namespace,
                        "image": image, "armed": False},
            "steps": steps,
        }

    sa_result = await _run(
        "create", ["serviceaccount", carrier, "-n", namespace], kubeconfig,
    )
    if sa_result.exit_code != 0:
        return await _fail_closed(
            f"create serviceaccount failed: {_error_text(sa_result)}"
        )
    built.append("serviceaccount")
    _step("create_serviceaccount", True)

    for sub, v_args, _stdin in role_plan:
        role_result = await _run(sub, v_args, kubeconfig)
        if role_result.exit_code != 0:
            return await _fail_closed(
                f"role construction ({sub}) failed: "
                f"{_error_text(role_result)}"
            )
        _step(f"role_{sub}", True)
    built.append("role")

    rb_result = await _run(
        "create",
        ["rolebinding", carrier, "-n", namespace,
         f"--role={carrier}", f"--serviceaccount={namespace}:{carrier}"],
        kubeconfig,
    )
    if rb_result.exit_code != 0:
        return await _fail_closed(
            f"create rolebinding failed: {_error_text(rb_result)}"
        )
    built.append("rolebinding")
    _step("create_rolebinding", True)

    skeleton = min(duration + _SKELETON_BUFFER_SECONDS, max_sleep)
    run_v_args = build_run_v_args(
        carrier, namespace, image, skeleton, tolerations,
    )
    # Shape self-check BEFORE dispatch (ND3: construction asserted, a
    # miss is an assembler regression — refuse rather than dispatch).
    assert_carrier_shape(run_v_args, carrier)
    run_result = await _run("run", run_v_args, kubeconfig)
    if run_result.exit_code != 0:
        return await _fail_closed(
            f"carrier pod run failed: {_error_text(run_result)}"
        )
    _step("run_carrier", True, f"image={image} skeleton={skeleton}s")

    try:
        await _wait_pod_running(carrier, namespace, kubeconfig)
        _step("wait_running", True)

        # ---- 4. Real-token verification (§3, three steps) -----------
        probe = await _exec_in_carrier(
            carrier, namespace, build_probe_get_script(target_url), kubeconfig,
        )
        code = probe.strip().splitlines()[-1].strip() if probe.strip() else ""
        if code != "200":
            return await _fail_closed(
                f"SA real-token GET probe returned {code or '<empty>'!r} "
                "(403 = missing permission; anything else = channel/target "
                "unreachable) — aborting before arming"
            )
        # The step already records the outcome; name the TRANSPORT too.
        # This probe is a curl issued from inside the carrier pod to the
        # in-cluster apiserver ClusterIP URL — a completed pod-side
        # ClusterIP round trip, which is a connectivity fact about the
        # cluster at that moment, not just an RBAC check. Case #64: the
        # verifier concluded "ClusterIP egress from pods is broken
        # environment-wide" while this receipt sat in its context saying
        # only "http 200", so nothing told it a pod had reached a
        # ClusterIP successfully. Stating where the request came from and
        # what it hit is the fact the program already has and was dropping.
        _step(
            "probe_get", True,
            f"http {code} — in-carrier curl from pod {carrier} to "
            f"in-cluster apiserver ClusterIP {target_url}",
        )

        # Per-family write-verb verification (§3 step 2): EVERY rule's
        # non-get verbs are SSAR-checked against THAT rule's own
        # group/plural — a multi-family Role (target PATCH + extra_delete
        # DELETE on a prop kind) verifies each family independently, so a
        # delete the Role does not grant fails closed HERE, before arming
        # (the B85 lesson: a write verb discovered unauthorized only at
        # fire time is a silent partial recovery).
        for rule in rules:
            r_group = str((rule.get("apiGroups") or [""])[0])
            r_plural = str((rule.get("resources") or [""])[0])
            # Single-family keeps the historic ``ssar_{verb}`` label; a
            # multi-family Role (target PATCH + extra_delete DELETE) needs
            # the plural to disambiguate two families sharing a verb.
            label_prefix = "" if len(rules) == 1 else f"{r_plural}_"
            for verb in sorted(
                v for v in (rule.get("verbs") or []) if v != "get"
            ):
                receipt_raw = await _exec_in_carrier(
                    carrier, namespace,
                    build_ssar_script(verb, r_group, r_plural, namespace),
                    kubeconfig,
                )
                try:
                    receipt = json.loads(receipt_raw)
                except (ValueError, TypeError):
                    return await _fail_closed(
                        f"SSAR receipt for verb {verb!r} on {r_plural!r} "
                        f"is not JSON: {receipt_raw[:120]!r}"
                    )
                verdict, detail = ssar_verdict(
                    receipt, verb, r_group, r_plural, namespace,
                )
                if verdict != "allowed":
                    return await _fail_closed(
                        f"SSAR {verdict} for write verb {verb!r} on "
                        f"{r_plural!r}: {detail} — all write verbs must be "
                        "allowed before arming"
                    )
                _step(f"ssar_{label_prefix}{verb}", True)

        # ---- 4.5 Computed cert-payload resolution (in-carrier) ------
        # A cryptographic fault payload (expired TLS cert) is authored
        # neither by the LLM nor on the operator host (_run is kubectl-
        # only) — the carrier is the sole in-cluster managed compute
        # channel. Resolve sentinel-valued inject ops HERE: capability
        # probe (fail-closed, pre-arming → retryable with a corrected
        # carrier_image), then openssl signs an expired cert in-carrier
        # and its base64 backfills the crt/key ops IN PLACE before
        # arming/inject (so the inject patch, the landing readback and the
        # receipt all see the resolved values). No sentinel → no-op.
        cert_specs = _cert_sentinel_specs(patches)
        cert_payloads: list[dict] = []
        if cert_specs:
            cap = await _exec_in_carrier(
                carrier, namespace,
                build_cert_capability_probe_script(), kubeconfig,
            )
            if "FD_OPENSSL_CA_OK" not in cap:
                return await _fail_closed(
                    f"carrier image {image!r} lacks openssl-ca capability "
                    f"(an expired-cert payload needs `openssl ca -enddate`): "
                    f"{cap.strip()[:120]!r} — pre-arming abort, retryable "
                    f"with a corrected carrier_image"
                )
            for cn, parts in cert_specs.items():
                uniq = hashlib.sha1(
                    f"{cn}|{time.time()}".encode("utf-8")
                ).hexdigest()[:10]
                out = await _exec_in_carrier(
                    carrier, namespace,
                    build_cert_gen_script(cn, workdir=f"/tmp/fd-cert-{uniq}"),
                    kubeconfig,
                )
                crt_b64, key_b64, not_after = _parse_cert_gen_output(out, cn)
                patches[parts["crt"]]["value"] = crt_b64
                patches[parts["key"]]["value"] = key_b64
                cert_payloads.append(
                    {"cn": cn, "not_after": not_after, "crt_b64": crt_b64}
                )
                _step("cert_gen", True, f"cn={cn} notAfter={not_after}")

        # ---- 4.9 Owner GC self-termination (§4; W-83-3B root-fix) -----
        # Wire the stack to self-terminate IN-CLUSTER at TTL regardless
        # of the agent process: ownerRef→pod on the RBAC trio + a
        # self-teardown grant, so the armed timer's self-delete tail
        # cascades the whole four-object stack via the apiserver's GC.
        # Placed AFTER every verification gate (probe / SSAR / cert) and
        # BEFORE arming, so it is wired only for a stack that will
        # actually arm. Fail-safe: on any wiring failure the tail is
        # suppressed and cleanup falls back to the agent four-way teardown
        # (never worse than the status quo — see _attach_self_termination).
        self_term_ok, self_delete_url = await _attach_self_termination(
            carrier, namespace, kubeconfig,
        )
        _step(
            "self_termination", self_term_ok,
            "ownerRef→pod + delete-pods(self) grant wired — the armed "
            "timer's fail-open self-delete cascades the whole stack via "
            "apiserver GC at TTL (agent-process-independent)"
            if self_term_ok else
            "ownerRef/grant wiring incomplete — self-delete tail "
            "suppressed; cleanup falls back to the agent four-way teardown",
        )

        # ---- 5. Arm (two-step exec; §4 + §7 budget tiers) ------------
        body = json.dumps(restore_patches, separators=(",", ":"))
        # B3: the restore curl's User-Agent names THIS carrier, so the
        # apiserver's audit log / managedFields attribute the TTL PATCH
        # to the drill, not to an anonymous in-cluster client.
        _ua = f"{_RESTORE_USER_AGENT_PREFIX}/{carrier}"
        _sd = self_delete_url if self_term_ok else ""
        arm_script = build_restore_script(
            duration, target_url, body, user_agent=_ua,
            delete_urls=delete_urls, self_delete_url=_sd,
        )
        landing: list[str] = []
        if len(arm_script) > _ARM_PAYLOAD_SAFE_LINE:
            landing = build_body_landing_scripts(body)
            for script in landing:
                await _exec_in_carrier(carrier, namespace, script, kubeconfig)
            arm_script = build_restore_script(
                duration, target_url, body, body_file="/tmp/restore.json",
                user_agent=_ua, delete_urls=delete_urls, self_delete_url=_sd,
            )
            if len(arm_script) > _ARM_PAYLOAD_BUDGET and _sd:
                # §7 budget subordination (recovery-carrier.md L263): the
                # self-delete tail is the LOWEST-priority segment — the
                # main restore and the prop deletes outrank it. Drop the
                # tail (cleanup falls back to the agent four-way teardown)
                # before failing the whole recovery on a byte overflow.
                _step(
                    "selfdelete_dropped", True,
                    "arming payload over budget with the self-delete tail — "
                    "dropped per §7 budget subordination; self-termination "
                    "falls back to the agent four-way teardown",
                )
                _sd = ""
                arm_script = build_restore_script(
                    duration, target_url, body, body_file="/tmp/restore.json",
                    user_agent=_ua, delete_urls=delete_urls, self_delete_url="",
                )
            if len(arm_script) > _ARM_PAYLOAD_BUDGET:
                return await _fail_closed(
                    "arming payload exceeds the channel budget even with "
                    "the body landed in-carrier — recipe too large"
                )

        # ---- 5.5 Content-aware dry-run arm gate (§3 step 3) ---------
        # SSAR (step 2) proved the carrier may PATCH/CREATE/DELETE these
        # resources; a verb+resource answer cannot see the request BODY.
        # A server-side ``?dryRun=All`` runs the FULL apiserver pipeline
        # (authn → authz INCLUDING the RBAC privilege-escalation guard →
        # admission → validation) with the carrier's OWN token and
        # persists nothing, so it surfaces a content-dependent denial
        # (escalation guard, admission webhook, ResourceQuota) BEFORE the
        # timer is armed into a fire that would hit it and be swallowed
        # by the restore. Same body / body_file / delete_urls as the arm,
        # so the dry-run rehearses the exact request the timer will send.
        # Verdict discipline: only 401/403 fail closed (authz is state-
        # independent — a dry-run denial means the live fire WILL be
        # denied); a form_error (unparseable receipt) is recorded but
        # does NOT abort, so a transport artifact never breaks a recovery
        # that would otherwise work.
        dryrun_script = build_dryrun_script(
            target_url, body,
            body_file=("/tmp/restore.json" if landing else ""),
            user_agent=_ua, delete_urls=delete_urls,
        )
        dry_receipt = await _exec_in_carrier(
            carrier, namespace, dryrun_script, kubeconfig,
        )
        dverdict, ddetail = dryrun_verdict(dry_receipt)
        if dverdict == "denied":
            return await _fail_closed(
                f"server-side dry-run arm gate rejected the restore "
                f"request: {ddetail} — verb-level SSAR passed but the "
                "write is content-denied, so the armed timer would fire "
                "into this rejection; aborting before arming"
            )
        _step(
            "dryrun_gate", dverdict == "ok",
            ddetail or "restore write dry-run clean (content-aware authz)",
        )

        echo = await _exec_in_carrier(carrier, namespace, arm_script, kubeconfig)
        if "armed" not in echo:
            return await _fail_closed(
                f"arming exec did not echo 'armed': {echo[:120]!r}"
            )
        arm_time = time.time()
        deadline = arm_time + duration
        _step("arm", True, f"landing={'inline' if not landing else 'b64'}")

        # ---- 6. Inject (armed-before-inject hard order; ND6) ---------
        inject_result = await _run(
            "patch",
            [kind, name, "-n", namespace, "--type=json", "-p",
             json.dumps(patches)],
            kubeconfig,
        )
        if inject_result.exit_code != 0:
            # POST-arming: the carrier STAYS (timer armed; a timed-out
            # patch may have landed — dropping the timer could strand
            # the fault; firing on an unpatched target is a no-op).
            _step("inject_patch", False, _error_text(inject_result))
            return _receipt(
                status="partial",
                error=f"injection patch failed (carrier stays armed until "
                      f"{deadline:.0f}): {_error_text(inject_result)}",
                carrier=carrier, namespace=namespace, image=image,
                task_id=task_id, rules=rules, duration=duration,
                deadline=deadline, steps=steps, target=target,
                patches=patches, restore_patches=restore_patches,
                landing_verified=False, extra_delete=delete_targets,
                cert_payloads=cert_payloads,
            )
        _step("inject_patch", True)

        # ---- 7. Landing readback (ND6) -------------------------------
        readback = await _run(
            "get", [kind, name, "-n", namespace, "-o", "json"], kubeconfig,
        )
        landing_ok = False
        if readback.exit_code == 0:
            try:
                live = json.loads(readback.stdout)
                misses, notes = verify_patches_landed(
                    live, patches,
                    whitespace_insensitive=(
                        readback_mode == "whitespace_insensitive"
                    ),
                )
                landing_ok = not misses
                _step("landing_readback", landing_ok,
                      "; ".join(misses + notes))
            except (ValueError, TypeError):
                _step("landing_readback", False, "unparseable readback")
        else:
            _step("landing_readback", False, _error_text(readback, limit=120))

        return _receipt(
            status="success" if landing_ok else "partial",
            error="" if landing_ok else "injection landed but readback could not confirm every patch",
            carrier=carrier, namespace=namespace, image=image,
            task_id=task_id, rules=rules, duration=duration,
            deadline=deadline, steps=steps, target=target,
            patches=patches, restore_patches=restore_patches,
            landing_verified=landing_ok, extra_delete=delete_targets,
            cert_payloads=cert_payloads,
        )
    except _AssemblyError as exc:
        return await _fail_closed(str(exc))


def _receipt(
    *, status: str, error: str, carrier: str, namespace: str, image: str,
    task_id: str, rules: list, duration: int, deadline: float,
    steps: list, target: dict, patches: list, restore_patches: list,
    landing_verified: bool, extra_delete: Optional[list] = None,
    cert_payloads: Optional[list] = None,
) -> dict:
    """Success/partial receipt: the carrier is armed, so the artifact
    and recovery handle ride along (the receipt branch moves them into
    state; consumption — exemptions, cleanup, armed gate — is
    schema-equivalent and unchanged)."""
    recovery_handle = {
        "kind": "recovery_carrier",
        "value": f"{namespace}/{carrier}",
        "target_ref": dict(target),
        "patches": list(patches),
        "restore_patches": list(restore_patches),
        # The create-domain prop objects the recovery lifecycle deletes
        # (timer + recover replay); rides the durable recipe so BOTH
        # hydration rungs (message / ledger) can replay the deletion.
        "extra_delete": list(extra_delete or []),
        "duration_seconds": int(duration),
        "recovery_deadline_epoch": float(deadline),
        "carrier": {"name": carrier, "namespace": namespace, "image": image},
    }
    return {
        "status": status,
        "error": error,
        "carrier": {
            "name": carrier, "namespace": namespace, "image": image,
            "armed": True,
            "recovery_timeout_seconds": int(duration),
            "recovery_deadline_epoch": float(deadline),
            "landing_verified": bool(landing_verified),
        },
        "artifact": build_carrier_artifact(
            name=carrier, namespace=namespace, task_id=task_id, rules=rules,
            duration_seconds=duration, deadline_epoch=deadline,
            recovery_handle=recovery_handle,
        ),
        "recovery_handle": recovery_handle,
        "steps": steps,
        "cert_payloads": list(cert_payloads or []),
    }


# ---------------------------------------------------------------------------
# The provider EXECUTE tool (blade_create precedent: LLM decides once,
# the tool executes the whole chain deterministically)
# ---------------------------------------------------------------------------


@tool
async def faultdrill_assemble_carrier(
    target_kind: str,
    target_name: str,
    target_namespace: str,
    patches: str,
    restore_patches: str,
    duration_seconds: int,
    kubeconfig: str = "",
    task_id: str = "",
    carrier_image: str = "",
    readback_compare: str = "auto",
    extra_delete: str = "",
) -> str:
    """Phase 2 ONLY — mutating: assemble the one-shot recovery carrier
    for an apiserver-write fault and inject it synchronously
    (recovery_channel: apiserver-write cases). One call = whole chain:
    minimal RBAC from restore_patches → SA/Role/RoleBinding/bare-Pod
    stack (drill-rc-<hash>) → SA real-token GET + per-verb SSAR →
    two-step exec arming (countdown starts at ARM) → fault patch on the
    target → landing readback.

    Inputs (all from the case recipe / captured baseline):
      - target_kind/target_name/target_namespace: the fault target
        (namespaced kinds only; e.g. Service, Deployment, ConfigMap).
      - patches: JSON array of json-patch ops to INJECT (the fault).
        A COMPUTED cert payload is declared with a sentinel value on the
        tls.crt / tls.key ops — ``<faultdrill:expired-cert cn=<host>
        part=crt|key>`` (one crt + one key op per cn). The tool signs an
        EXPIRED cert in-carrier (openssl ca -enddate) and backfills the
        base64 before inject, so the payload is never LLM-authored; the
        carrier image must provide ``openssl ca`` (probed fail-closed).
        The receipt carries ``cert_payloads`` (cn + notAfter + crt base64)
        as the white-box verdict's machinery-borne evidence.
        A PLAINTEXT Secret/ConfigMap ``.data.<key>`` credential (base64 in
        K8s storage) uses a ``<faultdrill:b64><plaintext>`` sentinel value;
        the tool base64-encodes it (pure Python, no carrier) before inject,
        so the stored base64 is program-derived, never LLM-hand-encoded.
      - restore_patches: JSON array of json-patch ops to RESTORE at TTL.
        ``replace`` with ``"value": null`` (or value omitted) =
        live-capture: the tool captures the live pre-injection baseline
        itself, byte-exact — no LLM transcription; the captured value
        rides the receipt verbatim. Prefer it for cluster-state-dependent
        baselines: relaying live bytes through the plan risks
        baseline-guard fail-closed aborts.
      - duration_seconds: NOT authoritative — overwritten at dispatch with
        D+G (approved fault window + recovery grace); the carrier always
        arms D+G and the value passed here has no effect. Countdown starts
        at arm time; the skeleton self-expires at window + 1800s forensics
        buffer, so the window is bounded at max_sleep − 1800.
      - carrier_image: RECOMMENDED — your planning-phase image pick
        (recovery-carrier.md §9: probe candidates ∩ evidence archive,
        first hit). Must already be inside the task's carrier allowlist
        (configured ∪ discovered — probe candidates are auto-added);
        outside → fail-closed. Omitted → the tool picks from the pools.
      - readback_compare: "auto" (default: ConfigMap targets compare
        whitespace-insensitive — their ecosystem controllers rewrite
        indentation, e.g. Corefile; every other kind stays exact),
        or explicit "exact" / "whitespace_insensitive" — insensitive
        treats a whitespace-normalised match as landed (a true miss
        still reports the first-divergence window); keep "exact"
        wherever whitespace may carry semantics.
      - extra_delete: OPTIONAL JSON array of create-domain prop objects to
        DELETE at recovery — the inverse of non-patch inject writes outside
        the patch domain (e.g. a fault-prop Secret from the case
        frontmatter's mechanism_writes). Entry ``{"kind","name","namespace"?}``
        (namespace defaults to the target's; cross-namespace / cluster-scoped
        refused — the namespaced Role cannot authorize it). Rides both the
        armed timer and the recover replay, idempotent (404 = no-op). Omit
        for a pure-patch fault.

    Output: JSON receipt {status: success|partial|failed, error,
    carrier{name,namespace,image,armed,recovery_deadline_epoch},
    artifact, recovery_handle, steps}. `failed` = nothing injected,
    built objects cleaned; `partial` = carrier armed but the injection
    or readback unconfirmed (carrier stays armed — do NOT rebuild; use
    blade-ai recover for early convergence).

    Constraints:
      - restore_patches baseline consistency verified pre-build; a
        stale baseline aborts with nothing built (re-capture first).
      - 403 / SSAR denied / non-Running pod / oversized recipe →
        fail-closed: stack cleaned, honest failure, no injection.
      - One ARMING per task window: a PRE-ARMING fail-closed abort
        (nothing built, nothing armed — e.g. a stale baseline caught
        by the precheck) may be retried with corrected parameters;
        once the carrier is ARMED never build a second one (early
        convergence = blade-ai recover).
    """
    try:
        fault_ops = _parse_patch_list(patches, "patches")
        restore_ops = _parse_patch_list(restore_patches, "restore_patches")
        delete_ops = _parse_delete_list(extra_delete, "extra_delete")
    except ValueError as exc:
        return json.dumps({
            "status": "failed", "error": str(exc),
            "carrier": {"armed": False}, "steps": [],
        })
    try:
        receipt = await assemble_recovery_carrier(
            target_kind=target_kind,
            target_name=target_name,
            target_namespace=target_namespace,
            patches=fault_ops,
            restore_patches=restore_ops,
            duration_seconds=duration_seconds,
            kubeconfig=kubeconfig,
            task_id=task_id,
            carrier_image=carrier_image,
            readback_compare=readback_compare,
            extra_delete=delete_ops,
        )
    except _AssemblyError as exc:
        return json.dumps({
            "status": "failed", "error": str(exc),
            "carrier": {"armed": False}, "steps": [],
        })
    except Exception as exc:  # noqa: BLE001 — honest receipt, never a crash
        logger.exception("carrier assembly crashed")
        return json.dumps({
            "status": "failed",
            "error": f"assembler internal error: {exc}",
            "carrier": {"armed": False}, "steps": [],
        })
    receipt.pop("recovery_handle", None)
    return json.dumps(receipt)


def parse_receipt(content: str) -> Optional[dict]:
    """Parse a tool-result receipt for the provider artifact hook.
    Only receipts whose artifact is armed and registerable count —
    ``failed`` receipts cleaned their stack and register NOTHING (a
    failed assembly must not wire the vehicle exemption chain onto a
    deleted pod)."""
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    artifact = parsed.get("artifact")
    if not isinstance(artifact, dict) or not artifact.get("name"):
        return None
    if parsed.get("status") not in ("success", "partial"):
        return None
    return parsed
