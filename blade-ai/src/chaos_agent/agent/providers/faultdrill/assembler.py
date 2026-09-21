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
        qualified = (
            [f"{group}/{res}" for res in resources] if group else list(resources)
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
    discovered = [
        img for img in allowed
        if img in str(_discovered_images_text())
    ]
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


# ---------------------------------------------------------------------------
# Payload construction (§4 arming / §3 verification scripts)
# ---------------------------------------------------------------------------


def _shell_sq(text: str) -> str:
    """Single-quote a payload for a POSIX shell (``'`` → ``'\''``) —
    the standard's quote-fidelity-preferred outer form."""
    return "'" + str(text).replace("'", "'\\''") + "'"


def build_restore_script(
    duration_seconds: int, target_url: str, body: str,
    *, body_file: str = "",
) -> str:
    """The §4 compact-variable-form timer script (C/T/U assigned INSIDE
    the payload — the standard's approved form; ``body_file`` switches
    the curl data to ``-d @file`` for the §7 oversized-payload landing
    tier). Output lands in ``/tmp/restore.log`` — never ``/dev/null``
    (the §4 form discipline: silent restore failure is the unforgivable
    mode)."""
    data_arg = f"-d @{body_file}" if body_file else f"-d {_shell_sq(body)}"
    return (
        f"( sleep {int(duration_seconds)}; "
        f"C={_SA_CA_PATH}; T=$(cat {_SA_TOKEN_PATH}); U={target_url}; "
        f"curl -s -X PATCH --cacert $C "
        f'-H "Authorization: Bearer $T" '
        f'-H "Content-Type: {_JSON_PATCH_CONTENT_TYPE}" '
        f"{data_arg} $U"
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
    banned by the arming knowledge — it answers the CALLER's view)."""
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


def verify_patches_landed(target_json: dict, patches: list) -> list[str]:
    """Post-injection landing readback (ND6): every fault patch must be
    visible on the live object — replace/add resolve to the op value,
    remove resolves to absent. Unresolvable paths count as NOT verified
    (reported, not fatal — the patch command's exit code already
    carries the primary verdict)."""
    misses: list[str] = []
    for op in patches or []:
        if not isinstance(op, dict):
            continue
        path = str(op.get("path") or "")
        kind_op = str(op.get("op") or "")
        current = _resolve_json_path(target_json, path)
        if kind_op in ("replace", "add"):
            if current is _UNRESOLVED or current != op.get("value"):
                misses.append(f"{kind_op} {path!r} not visible on the target")
        elif kind_op == "remove":
            if current is not _UNRESOLVED:
                misses.append(f"remove {path!r}: path still present")
    return misses


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
            f"{str(result.stderr or '')[:200]}"
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
                f"delete {kind} {name}: {str(result.stderr or '')[:120]}"
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
    pool-local ``select_carrier_image`` fallback decides alone."""
    from chaos_agent.config.settings import settings

    # ---- 0. Input validation (fail before anything is built) --------
    kind = canonical_kind(target_kind)
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

    steps: list[dict] = []

    def _step(label: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": label, "ok": bool(ok), "detail": detail[:300]})

    # ---- 1. Recipe → RBAC (same-source derivation) ------------------
    writes = [{"kind": kind, "methods": ["PATCH"]}]
    rules = derive_role_rules(writes)

    # ---- 2. Baseline precheck (before the stack: an early fail
    #         builds nothing) -----------------------------------------
    result = await _run(
        "get", [kind, name, "-n", namespace, "-o", "json"], kubeconfig,
    )
    if result.exit_code != 0:
        raise _AssemblyError(
            f"target read for baseline precheck failed: "
            f"{str(result.stderr or '')[:200]}"
        )
    try:
        target_json = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise _AssemblyError("target read returned unparseable JSON") from None
    violations = verify_restore_baseline(target_json, restore_patches)
    if violations:
        raise _AssemblyError(
            "restore recipe is not baseline-consistent (armed timer would "
            "mutate the target, not restore it): " + "; ".join(violations)
        )
    _step("baseline_precheck", True)

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
            f"create serviceaccount failed: {str(sa_result.stderr or '')[:200]}"
        )
    built.append("serviceaccount")
    _step("create_serviceaccount", True)

    for sub, v_args, _stdin in role_plan:
        role_result = await _run(sub, v_args, kubeconfig)
        if role_result.exit_code != 0:
            return await _fail_closed(
                f"role construction ({sub}) failed: "
                f"{str(role_result.stderr or '')[:200]}"
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
            f"create rolebinding failed: {str(rb_result.stderr or '')[:200]}"
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
            f"carrier pod run failed: {str(run_result.stderr or '')[:200]}"
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
        _step("probe_get", True, f"http {code}")

        group, _, plural, _ = _KIND_REST_MAP[kind]
        for verb in sorted(
            v for r in rules for v in (r.get("verbs") or []) if v != "get"
        ):
            receipt_raw = await _exec_in_carrier(
                carrier, namespace,
                build_ssar_script(verb, group, plural, namespace), kubeconfig,
            )
            try:
                receipt = json.loads(receipt_raw)
            except (ValueError, TypeError):
                return await _fail_closed(
                    f"SSAR receipt for verb {verb!r} is not JSON: "
                    f"{receipt_raw[:120]!r}"
                )
            verdict, detail = ssar_verdict(
                receipt, verb, group, plural, namespace,
            )
            if verdict != "allowed":
                return await _fail_closed(
                    f"SSAR {verdict} for write verb {verb!r} on "
                    f"{plural!r}: {detail} — all write verbs must be "
                    "allowed before arming"
                )
            _step(f"ssar_{verb}", True)

        # ---- 5. Arm (two-step exec; §4 + §7 budget tiers) ------------
        body = json.dumps(restore_patches, separators=(",", ":"))
        arm_script = build_restore_script(duration, target_url, body)
        landing: list[str] = []
        if len(arm_script) > _ARM_PAYLOAD_SAFE_LINE:
            landing = build_body_landing_scripts(body)
            for script in landing:
                await _exec_in_carrier(carrier, namespace, script, kubeconfig)
            arm_script = build_restore_script(
                duration, target_url, body, body_file="/tmp/restore.json",
            )
            if len(arm_script) > _ARM_PAYLOAD_BUDGET:
                return await _fail_closed(
                    "arming payload exceeds the channel budget even with "
                    "the body landed in-carrier — recipe too large"
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
            _step("inject_patch", False,
                  str(inject_result.stderr or "")[:200])
            return _receipt(
                status="partial",
                error=f"injection patch failed (carrier stays armed until "
                      f"{deadline:.0f}): {str(inject_result.stderr or '')[:200]}",
                carrier=carrier, namespace=namespace, image=image,
                task_id=task_id, rules=rules, duration=duration,
                deadline=deadline, steps=steps, target=target,
                patches=patches, restore_patches=restore_patches,
                landing_verified=False,
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
                misses = verify_patches_landed(live, patches)
                landing_ok = not misses
                _step("landing_readback", landing_ok, "; ".join(misses))
            except (ValueError, TypeError):
                _step("landing_readback", False, "unparseable readback")
        else:
            _step("landing_readback", False,
                  str(readback.stderr or "")[:120])

        return _receipt(
            status="success" if landing_ok else "partial",
            error="" if landing_ok else "injection landed but readback could not confirm every patch",
            carrier=carrier, namespace=namespace, image=image,
            task_id=task_id, rules=rules, duration=duration,
            deadline=deadline, steps=steps, target=target,
            patches=patches, restore_patches=restore_patches,
            landing_verified=landing_ok,
        )
    except _AssemblyError as exc:
        return await _fail_closed(str(exc))


def _receipt(
    *, status: str, error: str, carrier: str, namespace: str, image: str,
    task_id: str, rules: list, duration: int, deadline: float,
    steps: list, target: dict, patches: list, restore_patches: list,
    landing_verified: bool,
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
      - restore_patches: JSON array of json-patch ops to RESTORE at TTL
        — values MUST be the captured pre-injection baseline (verified
        against the live object before anything is built).
      - duration_seconds: fault window (countdown starts at arm time;
        the skeleton self-expires at window + 1800s forensics buffer,
        so the window is bounded at max_sleep − 1800).
      - carrier_image: RECOMMENDED — your planning-phase image pick
        (recovery-carrier.md §9: probe candidates ∩ evidence archive,
        first hit). Must already be inside the task's carrier allowlist
        (configured ∪ discovered — probe candidates are auto-added);
        outside → fail-closed. Omitted → the tool picks from the pools.

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
      - NEVER call twice for one task window; the carrier is the single
        recovery point (early recovery = blade-ai recover).
    """
    try:
        fault_ops = _parse_patch_list(patches, "patches")
        restore_ops = _parse_patch_list(restore_patches, "restore_patches")
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
