"""Classify a tool_call into the resource it would actually act on.

Companion to ``guard.py`` — the classifier produces an
``EffectiveTarget``; the guard compares that to ``ApprovedTarget``
and emits a ``GuardDecision``.

This module is the heaviest piece of the target-guard subsystem
because kubectl alone has 60+ subcommands with non-uniform argument
shapes. Coverage policy:

  - **READONLY**: kubectl get/describe/top/logs/events/version/...
    Returns ``EffectiveTarget(confidence=HIGH)`` with sentinel
    ``scope="__readonly__"``. The guard maps this to ``READONLY``
    verdict — no comparison needed.

  - **DESTRUCTIVE_KNOWN**: kubectl scale/cordon/drain/patch/set/
    delete/edit/replace/run/label/annotate/autoscale/expose/debug/
    attach/cp/exec, plus ``blade_create``. Each has a dedicated
    sub-classifier that parses its specific arg shape into
    ``(scope, namespace, names, labels)``.

  - **BANNED**: kubectl apply (any -f), kubectl config write, kubectl
    rollout (state-changing subs), explicit ``_execute_skill_script``
    when the opt-in flag is missing. Returns sentinel
    ``scope="__banned__"``.

  - **UNKNOWN**: anything else — unrecognised tool name, unrecognised
    kubectl subcommand, malformed args. Returns sentinel
    ``scope="__unknown__"`` so the guard can emit ``REJECT_UNKNOWN``.

Argument parsing handles all 5 standard kubectl flag positions:
``-n ns`` / ``-n=ns`` / ``--namespace ns`` / ``--namespace=ns`` /
``--namespace`` before-subcommand. Missing namespace on a
namespace-scoped subcommand is NORMALISED to "default" — kubectl's
own behaviour without --context override — so downstream comparison
against ``ApprovedTarget(namespace="default")`` doesn't false-positive.

``kubectl exec POD -- INNER_CMD`` is RECURSIVELY classified: the
effective target of an exec is whatever INNER_CMD acts on. This
plugs the most dangerous bypass — without recursion, an LLM could
``kubectl exec approved-pod -- blade create node-cpu --node X`` and
escape onto the node while the classifier sees "pod scope" and
allows the call.
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Iterator
from typing import Any

from chaos_agent.agent.spec.fault_registry import is_host_scope

from .types import ConfidenceLevel, EffectiveTarget
from .carriers import _FAULT_BINARIES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sentinel scopes — the guard knows these aren't real k8s kinds.
# ---------------------------------------------------------------------------

SCOPE_READONLY = "__readonly__"
SCOPE_BANNED = "__banned__"
SCOPE_UNKNOWN = "__unknown__"
SCOPE_ESCAPE = "__escape__"  # container-escape primitives (nsenter/chroot/unshare)


# ---------------------------------------------------------------------------
# Compliant forms, paired one-to-one with the causes recorded below.
#
# Every rejection must carry BOTH halves: ``reject_detail`` says what went
# wrong, ``reject_suggestion`` says what to do about THAT. The guard only falls
# back to a generic template when neither is recorded — so a cause without its
# own fix silently borrows a fix written for a different cause, and the two then
# contradict each other. task-866648cc is what that costs: a rejection whose
# reason named one subsystem while its suggestion pointed at another, and the
# model spent nine minutes acting on the wrong half.
#
# The split is by WHAT THE MODEL MUST CHANGE, not by subcommand:
#   - a name that does not exist   → change the name (arguments cannot help)
#   - a target that was not stated → add the positional argument
#   - an ambiguous target          → qualify it as <kind>/<name>
# Telling a model to "state the target" when the TOOL NAME is wrong sends it
# back to re-issue the same non-existent call with more arguments — a retry
# loop rather than a repair.
#
# ``SCOPE_UNKNOWN`` never becomes a hard floor (see ``guard_gateway``), so these
# only ever improve the repair hint; they cannot widen what the guard permits.
# For ``SCOPE_BANNED`` the opposite holds — an EMPTY suggestion is load-bearing
# there (it is what reports a boundary rather than a reshapeable call), so bans
# with no drill form deliberately keep none.
# ---------------------------------------------------------------------------

_FIX_UNKNOWN_TOOL = (
    "This is not a tool that exists in this phase — no argument will make it "
    "valid. Re-issue the operation with one of the tools bound for the current "
    "phase."
)
_FIX_UNKNOWN_SUBCOMMAND = (
    "The subcommand NAME is what the guard cannot place, so no argument will "
    "help. Check the real spelling with `--help` in v_args (the live help text "
    "is authoritative — more so than any documentation), then re-issue. If the "
    "intent genuinely has no kubectl form, express it as a blade command."
)
_FIX_UNKNOWN_VOCABULARY = (
    "The name itself is what the guard cannot place — correct it to one of the "
    "accepted values named in the reason, rather than adding more arguments."
)
_FIX_NAME_THE_TARGET = (
    "Add the missing positional argument in the shape the reason quotes. The "
    "guard cannot compare a target it could not parse, so this is a form "
    "issue, not a blocked target — once named, the target will be compared "
    "against the approved one (which is a separate check, and only passes if "
    "it matches)."
)
_FIX_QUALIFY_KIND = (
    "Write the target as '<kind>/<name>' (e.g. 'deployment/myapp') so the kind "
    "is unambiguous, then re-issue."
)
_FIX_STATE_SUBCOMMAND = (
    "Put the kubectl subcommand first, before its flags — e.g. "
    "'get pods -n <ns>', not '-n <ns>' alone."
)
_FIX_ESCAPE_VIA_CARRIER = (
    "This path IS available once expressed correctly: run the host operation "
    "through an approved-node privileged debug pod — `kubectl exec <debug-pod> "
    "-- <host-entry> ...` — and make the mutation self-recover by pairing the "
    "forward command with its own inverse behind a time bound, e.g. "
    "`<mutation> && sleep <N> && <inverse>` or "
    "`<mutation> && systemd-run --on-active=<N>s <inverse>`. The inverse that "
    "counts is family-specific and the guard names it when it rejects; a timer "
    "on its own, with no forward mutation, does not qualify."
)

# Namespaces where ChaosBlade tool pods are deployed. When kubectl exec
# targets a pod in one of these namespaces, the inner blade command is
# a Tier 1 injection — the namespace of the ACTUAL target differs from
# the tool pod's namespace.
TOOL_POD_NAMESPACES: frozenset[str] = frozenset({"chaosblade"})


# ---------------------------------------------------------------------------
# Kind canonicalisation — kubectl accepts singular / plural / short
# forms interchangeably. The guard MUST normalise both sides
# (approved + effective) to the canonical singular form, otherwise
# legitimate same-target calls get rejected for cosmetic mismatch.
# ---------------------------------------------------------------------------

# Maps every accepted spelling (singular/plural/short) to canonical
# singular. Group/version suffixes (``.apps`` / ``.v1.apps``) are
# stripped before lookup so ``deployment.apps`` matches ``deployment``.
KIND_ALIASES: dict[str, str] = {
    # Core
    "pod": "pod", "pods": "pod", "po": "pod",
    # ``container`` is not a real k8s kind, but ChaosBlade uses
    # scope=container for in-container chaos. The container lives
    # inside a pod and the guard tracks pod identity — so canonicalise
    # to "pod". Without this alias, ``blade_create(scope="container")``
    # would fall through to BLADE_TARGET_TO_SCOPE[target] and a
    # container-cpu call would mis-resolve to scope="node" (host CPU)
    # and false-positive as drift.
    "container": "pod", "containers": "pod",
    "node": "node", "nodes": "node", "no": "node",
    "service": "service", "services": "service", "svc": "service",
    "namespace": "namespace", "namespaces": "namespace", "ns": "namespace",
    "configmap": "configmap", "configmaps": "configmap", "cm": "configmap",
    "secret": "secret", "secrets": "secret",
    "persistentvolumeclaim": "pvc", "pvc": "pvc", "pvcs": "pvc",
    "persistentvolume": "pv", "pv": "pv", "pvs": "pv",
    "serviceaccount": "serviceaccount", "serviceaccounts": "serviceaccount", "sa": "serviceaccount",
    "endpoints": "endpoints", "ep": "endpoints",
    "event": "event", "events": "event", "ev": "event",
    # apps/v1
    "deployment": "deployment", "deployments": "deployment", "deploy": "deployment",
    "daemonset": "daemonset", "daemonsets": "daemonset", "ds": "daemonset",
    "statefulset": "statefulset", "statefulsets": "statefulset", "sts": "statefulset",
    "replicaset": "replicaset", "replicasets": "replicaset", "rs": "replicaset",
    "replicationcontroller": "replicationcontroller", "replicationcontrollers": "replicationcontroller", "rc": "replicationcontroller",
    # batch
    "job": "job", "jobs": "job",
    "cronjob": "cronjob", "cronjobs": "cronjob", "cj": "cronjob",
    # networking
    "ingress": "ingress", "ingresses": "ingress", "ing": "ingress",
    "networkpolicy": "networkpolicy", "networkpolicies": "networkpolicy", "netpol": "networkpolicy",
    # autoscaling
    "horizontalpodautoscaler": "hpa", "horizontalpodautoscalers": "hpa", "hpa": "hpa",
    # rbac
    "role": "role", "roles": "role",
    "rolebinding": "rolebinding", "rolebindings": "rolebinding",
    "clusterrole": "clusterrole", "clusterroles": "clusterrole",
    "clusterrolebinding": "clusterrolebinding", "clusterrolebindings": "clusterrolebinding",
    # storage
    "storageclass": "storageclass", "storageclasses": "storageclass", "sc": "storageclass",
    # custom resources — operator may install many; we recognise common ChaosBlade ones explicitly
    "chaosblade": "chaosblade", "chaosblades": "chaosblade",
}


def canonicalise_kind(raw: str) -> str:
    """Normalise a kind string to canonical singular form.

    Strips the ``.group`` / ``.group.version`` suffix kubectl
    sometimes accepts (e.g. ``deployment.apps``). Lowercases. Falls
    back to the input unchanged when no alias is known — caller
    treats unknown kinds as ``__unknown__`` via the guard rather
    than silently coercing.
    """
    if not raw:
        return ""
    # Strip .group / .group.version suffix
    head = raw.split(".", 1)[0].lower().strip()
    return KIND_ALIASES.get(head, head)


# ---------------------------------------------------------------------------
# Namespace parsing — handles all 5 kubectl flag forms.
# ---------------------------------------------------------------------------

_NS_FLAG_LONG = "--namespace"
_NS_FLAG_SHORT = "-n"


def parse_namespace(args: list[str], default: str = "default") -> str:
    """Extract the namespace from a kubectl arg list.

    Handles:
      - ``-n ns``
      - ``-n=ns``
      - ``--namespace ns``
      - ``--namespace=ns``
      - flag in any position (before OR after the subcommand)

    Stops at the ``--`` separator — anything after it belongs to an
    INNER command (``kubectl exec POD -- prog ...``) whose own ``-n``
    flag must not leak into the outer kubectl's namespace inference.

    Returns the explicit namespace, or ``default`` if no flag found.
    The caller should pass ``default=""`` for cluster-scoped
    subcommands (node/cordon/taint/etc) so missing namespace doesn't
    get auto-promoted to "default".
    """
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return default
        # Equals form: -n=ns / --namespace=ns
        if a.startswith(_NS_FLAG_SHORT + "=") or a.startswith(_NS_FLAG_LONG + "="):
            return a.split("=", 1)[1]
        # Spaced form: -n ns / --namespace ns
        if a == _NS_FLAG_SHORT or a == _NS_FLAG_LONG:
            if i + 1 < len(args):
                return args[i + 1]
            return default  # malformed: flag with no value
        i += 1
    return default


# ---------------------------------------------------------------------------
# Label selector parsing — -l / --selector
# ---------------------------------------------------------------------------


def parse_labels(args: list[str]) -> dict[str, str]:
    """Extract the label selector from ``-l``/``--selector``/``--labels`` flags.

    Returns a dict of {key: value}. Operator-style selectors
    (``key!=value``, ``key in (v1,v2)``) are flattened to {key: raw}
    so equality-comparison stays simple — the guard treats any
    non-trivial selector difference as drift anyway.
    Missing flag returns {}.

    Recognises both kubectl flags (``-l``, ``--selector``) and the
    ChaosBlade CLI flag (``--labels``) so that inline ``blade create``
    commands inside ``kubectl exec`` are correctly classified.

    Stops at the ``--`` separator so a ``kubectl exec POD -- prog -l x``
    doesn't leak the inner program's ``-l`` flag into the outer
    kubectl's label-selector inference.
    """
    selector: dict[str, str] = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        raw_selector = ""
        if a in ("-l", "--selector", "--labels"):
            if i + 1 < len(args):
                raw_selector = args[i + 1]
                i += 1
        elif a.startswith("-l=") or a.startswith("--selector=") or a.startswith("--labels="):
            raw_selector = a.split("=", 1)[1]
        if raw_selector:
            for pair in raw_selector.split(","):
                pair = pair.strip()
                # Operator-style (``!=`` / ``>=`` / ``<=`` / ``in`` /
                # ``notin``) preserve verbatim so the guard treats
                # ``app!=demo`` as a single distinguishable selector
                # entry instead of decomposing ``app!`` as the key.
                if ("!=" in pair or ">=" in pair or "<=" in pair
                        or " in " in pair or " notin " in pair):
                    selector[pair] = pair
                elif "=" in pair:
                    k, _, v = pair.partition("=")
                    selector[k.strip()] = v.strip()
                else:
                    # bare key — preserve verbatim
                    selector[pair] = pair
        i += 1
    return selector


# ---------------------------------------------------------------------------
# kubectl subcommand classification — coverage policy as docstring
# section "Coverage policy" above.
# ---------------------------------------------------------------------------

# Read-only kubectl subcommands. ``READONLY`` verdict, no comparison.
READONLY_KUBECTL_SUBS: frozenset[str] = frozenset({
    "get", "describe", "top", "logs", "events", "version",
    "api-resources", "api-versions", "explain", "auth",
    "wait", "diff", "help",
})

# Read-only sub-subcommands of kubectl rollout. ``rollout status`` /
# ``rollout history`` are query-only; the others mutate.
READONLY_ROLLOUT_SUBS: frozenset[str] = frozenset({"status", "history"})

# Read-only sub-subcommands of kubectl config. ``view`` / ``current-context``
# are query-only; the others mutate kubeconfig itself.
READONLY_CONFIG_SUBS: frozenset[str] = frozenset({
    "view", "current-context", "get-contexts", "get-clusters", "get-users",
})

# Explicitly banned kubectl subcommands — too dangerous to classify.
# ``apply -f`` requires reading the YAML to know targets; ``config``
# mutating subs change kubeconfig itself; ``certificate`` issues TLS
# certs.
BANNED_KUBECTL_SUBS: frozenset[str] = frozenset({
    # "apply" removed — handled by _uses_file_input + stdin_data whitelist
    "certificate",  # CSR approval — outside chaos scope
})

# Resources allowed to be created via kubectl apply/create -f with stdin_data.
# Only low-risk resources that don't run workloads. Workload resources
# (Deployment, DaemonSet, Pod, Job, etc.) are NOT allowed.
ALLOWED_MANIFEST_KINDS: frozenset[str] = frozenset({
    "persistentvolumeclaim", "pvc",
    "persistentvolume", "pv",
    "configmap", "secret", "namespace",
})


def _allowed_manifest_kinds_text() -> str:
    """The manifest whitelist, rendered for a rejection message.

    Derived from :data:`ALLOWED_MANIFEST_KINDS` rather than written out again,
    so a rejection can never advertise a stale list. Stating it matters: the
    classifier owns this set, and "only whitelisted kinds are allowed" without
    naming them leaves the model to guess (the same failure that made a
    ``kubectl label`` rejection unactionable in task-c758cdbd).
    """
    return ", ".join(sorted(ALLOWED_MANIFEST_KINDS))

# Destructive kubectl subcommands we DO classify. Each maps to a
# function below that parses its specific arg shape.
# Invariant (test_kubectl_verb_consistency): every write verb in
# ``K8sNativeProvider.inject_kubectl_subcommands`` (kubectl-native injection
# carriers) must appear here, so no injection verb can slip past destructive
# classification. These sets are otherwise intentionally distinct — this is a
# safety-classification set, not the provider's injection-detection vocabulary.
DESTRUCTIVE_KUBECTL_SUBS: frozenset[str] = frozenset({
    "exec", "scale", "cordon", "uncordon", "drain", "taint",
    "patch", "set", "delete", "edit", "replace", "run",
    "label", "annotate", "autoscale", "expose", "debug",
    "attach", "port-forward", "proxy", "cp", "create", "rollout",
    "apply",
})

# ``kubectl set`` sub-resources — the FIELD being written, which ``set`` puts in
# its FIRST positional (``kubectl set image deploy/x c=img``). Every other write
# verb names the resource there instead.
#
# Stripping this token is not cosmetic. Without it the generic resource
# classifier read ``image`` as the resource kind, ``_is_known_kind`` rejected it,
# and the call became ``SCOPE_UNKNOWN`` → ``REJECT_UNKNOWN`` — so NO ``kubectl
# set`` call could ever execute, even though ``set`` is in BOTH
# ``ToolGuard.KUBECTL_ALLOWED_SUBCOMMANDS`` (gate ① runs it) and
# ``K8sNativeProvider.inject_kubectl_subcommands`` (the provider declares it an
# injection carrier). That combination is exactly the
# "unexecutable-by-construction" shape the whitelist's own docstring warns about:
# the drill step can never be satisfied and the self-check keeps asking the model
# to redo an action the guard will refuse again.
#
# Verified against the cluster: ``kubectl set image <deploy> <container>=<img>
# --dry-run=client -o name`` exits 0 and resolves the target, so kubectl accepts
# the form the guard was refusing.
_KUBECTL_SET_SUBRESOURCES: frozenset[str] = frozenset({
    "image", "env", "resources", "serviceaccount", "sa", "subject", "selector",
})


# ChaosBlade ``--target`` to k8s scope mapping. Used to detect whether
# a blade_create call's k8s effect is on a pod, node, or unknown.
# Pod-attached resources (container, jvm, mysql in pod) all resolve
# to scope=pod. Host-level chaos (cpu/mem/disk/network without k8s
# prefix) resolves to scope=node.
BLADE_TARGET_TO_SCOPE: dict[str, str] = {
    "pod": "pod",
    "node": "node",
    "container": "pod",  # container belongs to a pod
    # In-pod middleware / runtime fault targets
    "jvm": "pod",
    "mysql": "pod",
    "redis": "pod",
    "kafka": "pod",
    "rocketmq": "pod",
    "nginx": "pod",
    # Host-level chaos (no k8s prefix, blade run on host)
    "cpu": "node",
    "mem": "node",
    "memory": "node",
    "disk": "node",
    "network": "node",
    "process": "node",
    "file": "node",
    "script": "node",
    "time": "node",
    "kernel": "node",
}


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def infer_effective_target(
    tool_name: str,
    tool_args: dict[str, Any] | str | list[str] | None,
    *,
    skill_script_allowed: bool = False,
) -> EffectiveTarget:
    """Top-level classifier — produce an EffectiveTarget for one tool_call.

    Args:
        tool_name: LangChain tool name (e.g. ``blade_create``,
            ``kubectl``, ``_execute_skill_script``,
            ``read_knowledge_resource``).
        tool_args: The tool's parsed arguments. Shape depends on tool:
            - ``blade_create``: dict with scope/target/action/namespace/names/labels
            - ``kubectl``: dict with ``command`` (list[str]) OR ``args``
              (str shell-quoted) OR list[str] directly
            - ``_execute_skill_script``: dict with script path / args
            - others: depends; classifier returns READONLY for known
              read-only tools and UNKNOWN for everything else.
        skill_script_allowed: Whether the operator has opted into
            allowing ``_execute_skill_script`` (default False = banned).
            Tied to ``settings.skill_script_default_allow`` at the
            caller side.

    Returns:
        EffectiveTarget — see ``types.EffectiveTarget`` for fields.
        Sentinel scopes ``__readonly__`` / ``__banned__`` /
        ``__unknown__`` signal special verdicts to the guard.
    """
    raw_command = _format_raw_command(tool_name, tool_args)

    # Known read-only tools. Guard maps these to READONLY verdict.
    # ``read_file`` / ``save_fault_plan`` touch the local FS only (not the
    # cluster) — safe for both phases. ``host_read`` is read-only BY
    # ENFORCEMENT (its own classifier rejects any mutating command).
    # NOTE: ``kubectl_read`` is intentionally NOT in this short-circuit — it now
    # accepts ``exec``/``debug``, so it is routed through ``_classify_kubectl``
    # below where its inner command is classified (read-only inner → READONLY;
    # mutating inner → pod/escape, which the screeners reject).
    if tool_name in ("blade_help", "blade_status", "blade_query_k8s",
                     "read_knowledge_resource", "read_skill_resource",
                     "activate_skill", "submit_fault_intent",
                     "read_file", "save_fault_plan",
                     "finish_planning", "propose_plan_change",
                     "submit_verification", "submit_recover_verification",
                     "host_read",
                     "request_replan",
                     "time_wait",
                     # Progress ledger write: a pure control-signal note with no
                     # cluster side effect (touches no fault target), same class
                     # as request_replan / time_wait.
                     "update_progress",
                     # ChaosBlade Python agent PRECONDITION management. Neither
                     # touches a fault target: ``prepare`` only registers the
                     # in-process agent's port with the blade CLI and ``revoke``
                     # deregisters it. The fault itself is created by
                     # ``blade_python_create`` (classified below) and removed by
                     # ``blade_destroy``.
                     "blade_python_prepare", "blade_python_revoke"):
        return EffectiveTarget(
            scope=SCOPE_READONLY,
            namespace="",
            raw_command=raw_command,
        )

    # Skill script — banned by default; opt-in flag flips it to a
    # READONLY pass-through. Reasoning:
    #   - Default ``skill_script_default_allow=False`` returns BANNED
    #     so the screener blocks the call in enforcing mode.
    #   - When the operator flips the flag to True, they have decided
    #     the bundled skill scripts are trusted. We can't inspect the
    #     script's effect on k8s resources, so we treat the call as
    #     READONLY for guard purposes — pass-through with an INFO log
    #     for audit. (Previous behaviour returned UNKNOWN which the
    #     guard still rejected, making the flag a no-op.)
    if tool_name in ("_execute_skill_script", "execute_skill_script"):
        if not skill_script_allowed:
            return EffectiveTarget(
                scope=SCOPE_BANNED,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.HIGH,
                reject_detail=(
                    "skill-script execution is disabled "
                    "(skill_script_default_allow=false); its effect on cluster "
                    "resources cannot be inspected"
                ),
                reject_suggestion=(
                    "Express the drill with the kubectl / blade tools instead — "
                    "the guard can classify their targets and compare them "
                    "against the approved one. Enabling the flag is an operator "
                    "decision that accepts an unclassifiable call, not "
                    "something to work around here."
                ),
            )
        return EffectiveTarget(
            scope=SCOPE_READONLY,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
        )

    if tool_name == "blade_create":
        return _classify_blade_create(_coerce_args_dict(tool_args), raw_command)

    if tool_name == "blade_python_create":
        return _classify_blade_python_create(
            _coerce_args_dict(tool_args), raw_command,
        )

    if tool_name == "host_inject":
        return _classify_host_inject(_coerce_args_dict(tool_args), raw_command)

    if tool_name in ("kubectl", "kubectl_read"):
        raw_args = _coerce_args_dict(tool_args) if isinstance(tool_args, dict) else None
        return _classify_kubectl(
            _coerce_args_list(tool_args), raw_command, raw_args=raw_args,
        )

    # Unknown tool — default-deny. Forces operator to add explicit
    # classification rather than silently allowing new tools.
    return EffectiveTarget(
        scope=SCOPE_UNKNOWN,
        namespace="",
        raw_command=raw_command,
        confidence=ConfidenceLevel.UNKNOWN,
        reject_detail=(
            f"unrecognized tool '{tool_name}' (default-deny; add explicit "
            "classification)"
        ),
            reject_suggestion=_FIX_UNKNOWN_TOOL,
    )


# ---------------------------------------------------------------------------
# blade_create classifier
# ---------------------------------------------------------------------------


def _classify_blade_create(args: dict[str, Any], raw_command: str) -> EffectiveTarget:
    """Classify a ``blade_create`` tool_call.

    Schema (approximate, matches ChaosBlade k8s plugin):
        scope: "pod" / "node" / "container" (sometimes the blade_target)
        target: ChaosBlade --target (cpu/mem/network/jvm/...)
        action: ChaosBlade action (fullload/burn/loss/...)
        namespace: pod namespace
        names: list[str] of pod / node names
        labels: dict label selector
    """
    blade_target = str(args.get("target") or args.get("blade_target") or "").lower()
    blade_action = str(args.get("action") or args.get("blade_action") or "").lower()
    raw_scope = str(args.get("scope") or args.get("blade_scope") or "").lower()

    # Host scope (bare-metal / VM faults) — identity is the host name, not a
    # k8s namespace/labels selector. Recognised explicitly so host carriers
    # don't fall through to the k8s scope resolution below and mis-resolve
    # to scope=node. Deeper host-drift comparison is layered on in P1/P2.
    if is_host_scope(raw_scope):
        host_names_raw = args.get("names") or args.get("host_name") or []
        if isinstance(host_names_raw, str):
            host_names_raw = [n.strip() for n in host_names_raw.split(",") if n.strip()]
        host_names = tuple(str(n) for n in host_names_raw if n)
        host_name = host_names[0] if host_names else str(args.get("host_name") or "")
        return EffectiveTarget(
            scope="host",
            namespace="",
            names=host_names,
            host_name=host_name,
            blade_target=blade_target,
            blade_action=blade_action,
            confidence=ConfidenceLevel.HIGH if host_name else ConfidenceLevel.LOW,
            raw_command=raw_command,
        )

    # Resolve k8s scope: prefer explicit ``scope`` field if it
    # canonicalises to a known kind; otherwise fall back to
    # blade_target → scope mapping.
    scope = canonicalise_kind(raw_scope) if raw_scope else ""
    if not scope or scope not in {"pod", "node"}:
        scope = BLADE_TARGET_TO_SCOPE.get(blade_target, SCOPE_UNKNOWN)

    namespace = str(args.get("namespace") or "").strip()
    # Cluster-scoped resources (node) keep namespace=""; namespace-scoped
    # default to "default" if absent.
    if not namespace and scope != "node":
        namespace = "default"

    names_raw = args.get("names") or []
    if isinstance(names_raw, str):
        names_raw = [n.strip() for n in names_raw.split(",") if n.strip()]
    names = tuple(str(n) for n in names_raw if n)

    labels_raw = args.get("labels") or {}
    if isinstance(labels_raw, str):
        labels_raw = _parse_label_string(labels_raw)
    labels = {str(k): str(v) for k, v in (labels_raw or {}).items()}

    confidence = ConfidenceLevel.HIGH if (names or labels) else ConfidenceLevel.LOW

    return EffectiveTarget(
        scope=scope,
        namespace=namespace,
        names=names,
        labels=labels,
        blade_target=blade_target,
        blade_action=blade_action,
        confidence=confidence,
        raw_command=raw_command,
    )


def _classify_blade_python_create(
    args: dict[str, Any], raw_command: str,
) -> EffectiveTarget:
    """Classify a ``blade_python_create`` tool_call.

    ``blade_python_create`` injects an in-process method fault into a running
    Python application (``blade create python <target> <action>``). Identity is
    the application process reached through the host channel, not a k8s
    namespace/selector, so the scope is the python fault scope and namespace is
    empty — mirroring ``_classify_host_inject``.

    Classified by its OWN tool name rather than falling through to
    ``_classify_blade_create``: that classifier would resolve ``target=redis``
    via ``BLADE_TARGET_TO_SCOPE`` to ``scope=pod``, which is a DIFFERENT
    capability profile from the approved python scope and would make the guard
    reject every in-process injection as cross-profile drift.

    ``blade_target`` / ``blade_action`` are still populated so the guard's
    fault-TYPE lock keeps pinning "which client, which fault verb".
    """
    from chaos_agent.agent.spec.fault_registry import python_scopes

    blade_target = str(args.get("target") or args.get("blade_target") or "").lower()
    blade_action = str(args.get("action") or args.get("blade_action") or "")
    # The scope name is registry-derived (declared by the python fault family).
    # Prefer an explicit ``scope`` arg when present; otherwise fall back to the
    # family's scope. Never leave it empty: an empty scope is not a sentinel, so
    # the guard would resolve it to the default (k8s) profile and reject the call
    # as cross-profile drift.
    _scopes = sorted(python_scopes())
    scope = str(args.get("scope") or "") or (_scopes[0] if _scopes else "")
    # Name the MISSING argument. Without this the guard can only report
    # "classifier confidence=unknown", which tells the model that something was
    # unparseable but not what — and it has no way to guess that the fault type
    # is what the target lock needs.
    missing = [
        name for name, value in (("target", blade_target), ("action", blade_action))
        if not value
    ]
    return EffectiveTarget(
        scope=scope,
        namespace="",
        host_name="",
        blade_target=blade_target,
        blade_action=blade_action,
        confidence=(
            ConfidenceLevel.HIGH if (blade_target and blade_action)
            else ConfidenceLevel.UNKNOWN
        ),
        raw_command=raw_command,
        reject_detail=(
            f"the python-app call is missing {' and '.join(missing)}, so the "
            "fault TYPE cannot be pinned against the approved one"
            if missing else ""
        ),
    )


def _classify_host_inject(args: dict[str, Any], raw_command: str) -> EffectiveTarget:
    """Classify a ``host_inject`` tool_call.

    ``host_inject`` runs ONE raw host-native fault command (``iptables`` /
    ``tc`` / ``stress-ng`` / ``dd`` / ``kill`` …) over the host transport.
    Identity is the host itself, not a k8s namespace/selector, so
    ``scope="host"``. The fault family is derived from the command via
    ``classify_host_operation`` so the guard's ``blade_target`` lock still
    pins the fault TYPE (``network`` → ``process`` is drift), while the
    k8s namespace/names/labels checks are skipped for host scope.
    """
    from chaos_agent.agent.target_guard.carriers import classify_host_operation

    command = str(args.get("command") or "")
    family = classify_host_operation(command)
    return EffectiveTarget(
        scope="host",
        namespace="",
        host_name="",
        blade_target=family,
        confidence=ConfidenceLevel.HIGH if command else ConfidenceLevel.UNKNOWN,
        raw_command=raw_command,
        # Say WHICH argument is missing — see the note in the python-app
        # classifier above.
        reject_detail=(
            "" if command
            else "host_inject was called with an empty 'command', so there is "
                 "no host operation to classify"
        ),
    )


def _parse_label_string(s: str) -> dict[str, str]:
    """Parse ``k1=v1,k2=v2`` into a dict. Tolerates whitespace."""
    out: dict[str, str] = {}
    for pair in s.split(","):
        pair = pair.strip()
        if "=" in pair:
            k, _, v = pair.partition("=")
            out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------------------
# kubectl classifier
# ---------------------------------------------------------------------------


def _classify_kubectl(
    args: list[str], raw_command: str,
    *, raw_args: dict | None = None,
) -> EffectiveTarget:
    """Dispatch on kubectl subcommand."""
    if not args:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
            reject_detail="the kubectl call carries no subcommand at all",
            reject_suggestion=_FIX_STATE_SUBCOMMAND,
        )

    # Skip leading global flags (--kubeconfig=..., --context=...,
    # --namespace=... when used before the subcommand) to find the
    # actual verb.
    sub_idx = _find_subcommand_index(args)
    if sub_idx is None:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                "no kubectl subcommand was found — every token parsed as a "
                "global flag or a flag's value"
            ),
                reject_suggestion=_FIX_STATE_SUBCOMMAND,
        )
    sub = args[sub_idx]
    rest = args[sub_idx + 1:]

    # Propagate any pre-subcommand ``--namespace`` flag into ``rest``
    # so sub-classifiers (which only see ``rest``) pick it up. Skip if
    # ``rest`` already has its own ``-n`` / ``--namespace``.
    #
    # PREPEND (not append) — for ``kubectl exec POD -- prog`` the rest
    # contains ``--`` and anything appended lands in the inner exec
    # payload where parse_namespace stops scanning. Prepending puts
    # the global ns at the head, before any subcommand args and well
    # before any ``--`` separator.
    pre = args[:sub_idx]
    global_ns = parse_namespace(pre, default="")
    if global_ns and not _rest_has_namespace(rest):
        rest = ["-n", global_ns] + list(rest)

    # Bans first — short-circuit before any parsing.
    if sub in BANNED_KUBECTL_SUBS:
        return EffectiveTarget(
            scope=SCOPE_BANNED, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
            reject_detail=(
                f"kubectl subcommand '{sub}' is explicitly banned "
                "(too dangerous to classify)"
            ),
            # No reject_suggestion ON PURPOSE. The only member of
            # BANNED_KUBECTL_SUBS is ``certificate`` (CSR approval), which has
            # no drill form at all — an empty suggestion is what tells
            # guard_gateway to report this as a boundary rather than a
            # reshapeable call. Adding a placeholder here would invent a way
            # forward that does not exist.
        )

    # Stdin/-f file inputs: when stdin_data is provided AND the YAML
    # contains only whitelisted resource kinds, allow the operation.
    # Otherwise ban — content from -f <file> is not visible to us.
    if sub in ("apply", "create", "replace", "patch", "set", "delete", "edit"):
        if _uses_file_input(rest):
            stdin_data = (raw_args or {}).get("stdin_data", "") if raw_args else ""
            if stdin_data:
                return _classify_kubectl_stdin_manifest(
                    stdin_data, rest, raw_command,
                )
            return EffectiveTarget(
                scope=SCOPE_BANNED, namespace="",
                raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
                reject_detail=(
                    f"kubectl {sub} -f reads a file whose contents are not "
                    "visible to the guard"
                ),
                reject_suggestion=(
                    "Pass the manifest via stdin_data instead, containing only "
                    f"these kinds: {_allowed_manifest_kinds_text()}."
                ),
            )

    # Read-only — no comparison needed.
    if sub in READONLY_KUBECTL_SUBS:
        return EffectiveTarget(
            scope=SCOPE_READONLY, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
        )

    # Any subcommand invoked with -h/--help is a help request — prints
    # usage text and never mutates state.
    if _has_help_flag(rest):
        return EffectiveTarget(
            scope=SCOPE_READONLY, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
        )

    # rollout has both read-only and destructive sub-subs.
    if sub == "rollout":
        if rest and rest[0] in READONLY_ROLLOUT_SUBS:
            return EffectiveTarget(
                scope=SCOPE_READONLY, namespace="",
                raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
            )
        # rollout restart/undo/pause/resume — destructive, affects a
        # deployment/sts/ds. Classify as destructive with the
        # rollout's target resource.
        return _classify_kubectl_rollout(rest, raw_command)

    # config has both query and write sub-subs.
    if sub == "config":
        if rest and rest[0] in READONLY_CONFIG_SUBS:
            return EffectiveTarget(
                scope=SCOPE_READONLY, namespace="",
                raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
            )
        # Writes to kubeconfig — banned outright.
        return EffectiveTarget(
            scope=SCOPE_BANNED, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
            reject_detail=(
                "kubectl config writes to the kubeconfig, which is outside the "
                "target-scoped operation model"
            ),
            reject_suggestion=(
                "A kubeconfig write changes which cluster EVERY later call "
                "targets. Pass --context / --kubeconfig on the individual call "
                "instead; 'kubectl config view' stays available for inspection."
            ),
        )

    # Dispatch on destructive sub
    if sub == "exec":
        return _classify_kubectl_exec(rest, raw_command)
    if sub == "debug":
        return _classify_kubectl_debug(rest, raw_command)
    if sub == "scale":
        return _classify_kubectl_resource(rest, raw_command, default_kind=None)
    if sub in ("cordon", "uncordon", "drain"):
        return _classify_kubectl_node_op(rest, raw_command)
    if sub == "taint":
        return _classify_kubectl_taint(rest, raw_command)
    # ``set`` before the generic group: its first positional is the FIELD
    # (image / env / …), not the resource, so it needs that token stripped first.
    if sub == "set":
        return _classify_kubectl_set(rest, raw_command)
    if sub in ("patch", "delete", "edit", "replace", "label", "annotate", "autoscale"):
        return _classify_kubectl_resource(rest, raw_command, default_kind=None)
    if sub == "run":
        return _classify_kubectl_run(rest, raw_command)
    if sub == "expose":
        return _classify_kubectl_resource(rest, raw_command, default_kind=None)
    if sub == "attach":
        return _classify_kubectl_resource(rest, raw_command, default_kind="pod")
    if sub == "cp":
        return _classify_kubectl_cp(rest, raw_command)
    if sub == "port-forward":
        return _classify_kubectl_resource(rest, raw_command, default_kind="pod")
    if sub == "proxy":
        # Proxy creates a local-only tunnel; treat as banned because
        # it's outside the target-scoped operation model.
        return EffectiveTarget(
            scope=SCOPE_BANNED, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
            reject_detail=(
                "kubectl proxy opens a local tunnel outside the target-scoped "
                "operation model"
            ),
            reject_suggestion=(
                "No tunnel is needed: the kubectl subcommands already carry the "
                "connection settings. Query the API directly with "
                "get / describe / logs, or enter a workload with exec."
            ),
        )
    if sub == "create":
        # create RESOURCE name (without -f) — limited use, classify
        # by resource kind.
        return _classify_kubectl_resource(rest, raw_command, default_kind=None)

    if sub == "apply":
        # apply without -f (already handled above) — classify by resource.
        return _classify_kubectl_resource(rest, raw_command, default_kind=None)

    # Anything else: unknown subcommand → default-deny.
    return EffectiveTarget(
        scope=SCOPE_UNKNOWN, namespace="",
        raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
        reject_detail=f"unknown kubectl subcommand '{sub}'",
        reject_suggestion=_FIX_UNKNOWN_SUBCOMMAND,
    )


def _find_subcommand_index(args: list[str]) -> int | None:
    """Skip leading global flags to find the kubectl subcommand index.

    Global flags (kubectl --help shows ~40) start with -- or -, and
    most take a value. We skip both flag-only (``--v=4``) and
    flag+value (``--context my-ctx`` / ``--kubeconfig ~/.kube/x``)
    forms. The subcommand is the first non-flag arg.

    The 1-vs-2 token decision is delegated to ``_is_valueless_flag`` so this
    and ``_list_positionals`` cannot disagree about a flag's arity.
    """
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("-"):
            return i
        i += 1 if _is_valueless_flag(a) else 2
    return None


# kubectl flags that DON'T take a value. Used by ``_first_positional``
# and ``_find_subcommand_index`` to decide whether to skip 1 token
# (boolean flag) or 2 (flag + value). Without this list, ``kubectl
# delete --all pod -n ns`` would parse as ``--all`` consuming ``pod``
# and lose the actual positional.
#
# Includes both global flags and the most common subcommand-level
# boolean flags. Not exhaustive — uncommon boolean flags fall through
# to the 2-token assumption (worst case: we mis-skip one positional
# and return UNKNOWN, which the screener default-denies in enforcing
# mode rather than letting a wrong call through).
_BOOLEAN_FLAGS: frozenset[str] = frozenset({
    # Help / verbose
    "-h", "--help", "-v", "--version",
    "-W", "--warnings-as-errors",
    "-q", "--quiet",
    # All-namespaces / all
    "-A", "--all-namespaces", "--all",
    # Recursive
    "-R", "--recursive",
    # Force / safety
    "--force", "--ignore-not-found", "--prune",
    "--insecure-skip-tls-verify",
    # Watch
    "-w", "--watch", "--watch-only",
    # Output formatting
    "--show-labels", "--show-kind", "--no-headers",
    "--server-side", "--client",
    # Misc
    "--include-uninitialized", "--keep-annotations",
    "--validate", "--save-config",
    "--rm",  # kubectl run --rm (delete on exit)
    "-i", "--stdin", "-t", "--tty",  # kubectl exec / run
    "--allow-missing-template-keys",
    "--overwrite",  # kubectl label / annotate
    "--local",  # kubectl set ... --local
})


def _is_valueless_flag(token: str) -> bool:
    """Whether *token* is a flag that consumes NO following token.

    Two shapes qualify:
      - ``--flag=value`` / ``-n=ns`` — the value is glued on,
      - an exact member of ``_BOOLEAN_FLAGS``.

    Anything else is assumed to take a value (skip 2). That assumption is
    deliberately conservative: mis-skipping loses a positional and lands in
    ``SCOPE_UNKNOWN``, which the screener default-denies, rather than letting a
    call through against an unverified target.

    Exists as a named function, rather than inline in each caller, because
    ``_find_subcommand_index`` and ``_iter_positionals`` both need the answer and
    used to carry separate copies of it — they can no longer disagree about a
    flag's arity.

    NOT handled on purpose: stacked short-flag CLUSTERS (``-it`` == ``-i -t``).
    A rule admitting all-boolean clusters was written and then reverted. It
    worked — 292561 exhaustive argv combinations showed no other behaviour
    change, and the only kubectl forms it altered (``-vi 5``, which kubectl
    itself rejects with ``invalid argument``) were already invalid. It was
    dropped because the two skill cases that motivated it (``kubectl debug -it
    ... -- tc -Version``) were the real defect: a TTY is meaningless for an
    agent, and those docs were corrected instead. With no evidence the model
    produces ``-it`` on its own — zero occurrences across the recorded session
    logs — the rule was carrying a parsing special case for a hypothetical.

    Consequence to keep in mind: ``kubectl exec -it <pod> ...`` is refused, since
    ``-it`` swallows the pod name. It is refused WITH a reason now
    (``_classify_kubectl_exec`` names the missing pod), so the model can see the
    shape it needs rather than only that something failed.
    """
    if "=" in token:
        return True
    return token in _BOOLEAN_FLAGS


def _rest_has_namespace(rest: list[str]) -> bool:
    """True if ``rest`` already carries a ``-n`` / ``--namespace`` flag.

    Used by ``_classify_kubectl`` to decide whether to inject the
    pre-subcommand global namespace. We don't want to clobber an
    explicit per-subcommand ns with a global one.

    Stops scanning at the ``--`` separator — anything after it is the
    INNER command of ``kubectl exec`` (or similar) and its ``-n`` would
    bind to the inner program's namespace flag, not kubectl's outer ns.
    Without this stop, a ``kubectl exec POD -- prog -n inner`` call
    would falsely report that the OUTER kubectl carries a namespace,
    suppressing global-ns propagation.
    """
    for a in rest:
        if a == "--":
            return False
        if a in ("-n", "--namespace"):
            return True
        if a.startswith("-n=") or a.startswith("--namespace="):
            return True
    return False


def _uses_file_input(args: list[str]) -> bool:
    """Return True if any ``-f`` / ``--filename`` flag is present.

    Stdin (``-f -``) and URL inputs are indistinguishable from local
    files at this layer — we ban them all because the content is not
    in the tool_call arg list.
    """
    for i, a in enumerate(args):
        if a in ("-f", "--filename"):
            return True
        if a.startswith("-f=") or a.startswith("--filename="):
            return True
    return False


def _extract_all_kinds_from_yaml(yaml_str: str) -> list[str]:
    """Extract ALL 'kind' fields from YAML (handles multi-document ``---``)."""
    return re.findall(r"^kind:\s*(\S+)", yaml_str, re.MULTILINE)


def _extract_name_from_yaml(yaml_str: str) -> str:
    """Extract first ``metadata.name`` from YAML string."""
    m = re.search(r"^\s+name:\s*(\S+)", yaml_str, re.MULTILINE)
    return m.group(1) if m else ""


def _extract_namespace_from_yaml(yaml_str: str) -> str:
    """Extract first ``metadata.namespace`` from YAML string."""
    m = re.search(r"^\s+namespace:\s*(\S+)", yaml_str, re.MULTILINE)
    return m.group(1) if m else ""


def _classify_kubectl_stdin_manifest(
    stdin_data: str,
    rest: list[str],
    raw_command: str,
) -> EffectiveTarget:
    """Classify ``kubectl apply/create -f -`` with inline YAML via stdin_data.

    Multi-document safety: ALL ``kind`` values must be in
    ``ALLOWED_MANIFEST_KINDS``. A single non-whitelisted kind causes
    the entire call to be BANNED.
    """
    kinds = _extract_all_kinds_from_yaml(stdin_data)
    if not kinds:
        return EffectiveTarget(
            scope=SCOPE_BANNED, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
            reject_detail=(
                "the -f/stdin manifest has no recognizable 'kind'; its effect "
                "cannot be verified"
            ),
            reject_suggestion=(
                "Declare an explicit 'kind:' in the manifest. Accepted kinds: "
                f"{_allowed_manifest_kinds_text()}."
            ),
        )
    if not all(k.lower() in ALLOWED_MANIFEST_KINDS for k in kinds):
        return EffectiveTarget(
            scope=SCOPE_BANNED, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
            reject_detail=(
                "the manifest contains a non-whitelisted resource kind "
                f"({', '.join(kinds)})"
            ),
            reject_suggestion=(
                f"Accepted kinds: {_allowed_manifest_kinds_text()}. Workload "
                "kinds (Deployment / DaemonSet / Pod / Job / …) are refused "
                "because they start containers whose blast radius the guard "
                "cannot scope — inject into a workload that already exists "
                "instead of creating one."
            ),
        )
    namespace = parse_namespace(rest, default="")
    if not namespace:
        namespace = _extract_namespace_from_yaml(stdin_data)
    name = _extract_name_from_yaml(stdin_data)
    return EffectiveTarget(
        scope=canonicalise_kind(kinds[0]),
        namespace=namespace,
        names=(name,) if name else (),
        confidence=ConfidenceLevel.HIGH,
        raw_command=raw_command,
    )


# ---------------------------------------------------------------------------
# kubectl exec — recursive into inner command
# ---------------------------------------------------------------------------


def _classify_kubectl_exec(args: list[str], raw_command: str) -> EffectiveTarget:
    """Classify ``kubectl exec POD [-c CONTAINER] [-n NS] -- INNER``.

    The effective target of an exec is whatever INNER would act on.
    For most shell commands this is "the pod itself" (scope=pod).
    For nested blade or kubectl calls we recurse.
    """
    ns = parse_namespace(args, default="default")
    pod_name = _first_positional(args)
    if not pod_name:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                "kubectl exec names no pod — the guard cannot tell WHICH pod "
                "would be entered, so it cannot compare it to the approved one"
            ),
                reject_suggestion=_FIX_NAME_THE_TARGET,
        )

    inner = _extract_after_double_dash(args)
    if not inner:
        # Pure stdio attach — acts on the pod. Vehicle identity is decided
        # DATA-side by the screener (state + live discovery), never from
        # the pod name here.
        return EffectiveTarget(
            scope="pod", namespace=ns, names=(pod_name,),
            raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
        )

    # Help request in inner command → read-only regardless of what
    # program runs (blade -h, blade create k8s pod-network drop -h, etc.)
    if _has_help_flag(inner):
        return EffectiveTarget(
            scope=SCOPE_READONLY, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
        )

    # Recursive: inner is ``blade ...``
    if inner[0] == "blade":
        return _classify_inline_blade(inner, raw_command, fallback_ns=ns, fallback_pod=pod_name)

    # Recursive: inner is ``kubectl ...``
    if inner[0] == "kubectl":
        nested = _classify_kubectl(inner[1:], raw_command)
        # Inherit the outer pod's namespace if the nested call has
        # nothing — kubectl-inside-pod usually inherits ambient.
        if nested.namespace == "default" and ns != "default":
            return EffectiveTarget(
                scope=nested.scope, namespace=ns, names=nested.names,
                labels=nested.labels, blade_target=nested.blade_target,
                blade_action=nested.blade_action,
                confidence=ConfidenceLevel.LOW,  # nested = less certain
                raw_command=raw_command,
            )
        return nested

    # Container escape attempts via nsenter/chroot/unshare — these
    # pivot the mount/PID namespace to the host. Default-deny, but
    # distinguish from SCOPE_UNKNOWN so the guard can tell the LLM the
    # *real* reason (security policy, not "unrecognised command").
    #
    # A single ``sh -c "<script>"`` wrapper must not hide the escape
    # primitive: peek one layer deeper before deciding, otherwise a
    # wrapped ``chroot`` would be misread as a benign pod command.
    escape_probe = inner
    if inner[0] in ("sh", "bash", "ash", "dash", "/bin/sh", "/bin/bash") and "-c" in inner:
        c_idx = inner.index("-c")
        if c_idx + 1 < len(inner):
            try:
                nested_tokens = shlex.split(inner[c_idx + 1])
            except ValueError:
                nested_tokens = []
            if nested_tokens:
                escape_probe = nested_tokens
    if escape_probe[0] in ("nsenter", "chroot", "unshare"):
        # A READ-ONLY probe through the escape primitive is not a mutation:
        # from a privileged debug pod, ``chroot /host cat /etc/os-release`` is
        # the only way to inspect the node, and Phase 1 must be able to verify
        # host preconditions before committing to a plan. Delegate to the shared
        # read-only judge, which unwraps the primitive and rules on the command
        # actually being run (so ``chroot /host iptables -A ...`` still lands in
        # SCOPE_ESCAPE below). Mirrors the read-only exemption the fault-binary
        # branch already grants.
        from chaos_agent.tools.readonly import is_readonly_inner_tokens

        if not is_readonly_inner_tokens(inner):
            return EffectiveTarget(
                scope=SCOPE_ESCAPE, namespace="",
                raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
                reject_detail=(
                    f"the exec runs a host-escape primitive ('{escape_probe[0]}'); "
                    "it must go through an approved, current, privileged debug pod "
                    "on the approved node and be self-recovering"
                ),
                    reject_suggestion=_FIX_ESCAPE_VIA_CARRIER,
            )

    # Fault-binary mutations (iptables/nft/tc/stress/dd/etc) inside a
    # kubectl exec.
    #
    # The escape branch above already consumed every command that reaches the
    # HOST: ``chroot`` / ``nsenter`` / ``unshare`` (including one ``sh -c``
    # wrapper). Anything still here runs in the target pod's OWN namespaces,
    # and that containment is enforced by the kernel, not by convention.
    # Measured on the test cluster: two pods of the same Deployment reported
    # ``/proc/self/ns/net`` as ``net:[4026532579]`` and ``net:[4026532741]`` —
    # distinct from each other and from the host's, and each pod saw only
    # ``lo`` plus its own ``eth0@ifN`` veth end. So ``tc qdisc add dev eth0``
    # inside such an exec can only shape that pod's interface.
    #
    # This branch used to fire on the binary name alone, which is why it read
    # ``tc`` as a host mutation and rejected the documented pod-level form
    # (task-866648cc: ``kubectl exec <pod> -- tc qdisc add dev eth0 root netem
    # loss 100%`` → REJECT_BANNED). The comment already scoped the rule to
    # hostNetwork pods; the check never implemented it, so eight skill cases
    # whose injection step is ``kubectl exec ... tc netem ...`` were
    # unexecutable as written.
    #
    # hostNetwork is deliberately NOT consulted here: this classifier is
    # static (no cluster access), and the case it would catch — a fault binary
    # in a hostNetwork pod — is a genuine gap that belongs to a layer that can
    # read pod spec. Guessing from a pod name would fail both ways.
    _READONLY_SUBS = {
        "iptables": {"-L", "-S", "--list", "--list-rules"},
        "ip6tables": {"-L", "-S", "--list", "--list-rules"},
        "nft": {"list"},
    }
    # ``-Version`` is tc's own spelling for a version query (iproute2 uses it
    # instead of the conventional ``--version``), and four skill cases probe
    # tool availability with ``tc -Version`` before injecting. Without it the
    # probe classifies as a pod mutation — harmless to execute, but it would
    # consume a blast-radius comparison for what is only a capability check.
    _READONLY_FLAGS = {"--help", "-h", "--version", "-V", "-Version", "version"}
    binary = escape_probe[0].rsplit("/", 1)[-1]
    if binary in _FAULT_BINARIES:
        probe_args = escape_probe[1:]
        is_readonly_probe = bool(probe_args) and (
            probe_args[0] in _READONLY_FLAGS
            or probe_args[0] in _READONLY_SUBS.get(binary, set())
        )
        if not is_readonly_probe:
            # A pod-scoped mutation: same shape the guard already accepts for
            # any other pod-level fault, so identity/blast-radius comparison
            # applies normally instead of the escape path's carrier
            # requirement. ``fault_binary_mutation`` marks the shape so the
            # screener's vehicle exemption does NOT swallow it: inside a
            # privileged / hostNetwork tool pod the same binary shapes the
            # HOST, which the static classifier cannot rule out — keep the
            # identity review.
            return EffectiveTarget(
                scope="pod", namespace=ns, names=(pod_name,),
                raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
                fault_binary_mutation=True,
            )

    # Read-only probe (cat/ls/df/ps, iptables -L, ip addr show, ...) — a
    # non-mutating inspection of the pod. Classify as READONLY so the phase-1 /
    # intent / verify screeners pass it without a target comparison (reads do
    # not drift). Reached only AFTER the escape / mutating-fault-binary checks
    # above, so ``iptables -A`` / ``chroot`` / ``stress`` never land here — the
    # shared classifier returns False for them and this branch is skipped.
    from chaos_agent.tools.readonly import is_readonly_inner_tokens
    if is_readonly_inner_tokens(inner):
        return EffectiveTarget(
            scope=SCOPE_READONLY, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
        )

    # Plain shell command (rm/kill/etc) — acts on the pod's own
    # filesystem/process space. scope=pod is correct.
    #
    # Vehicle identity (exec into the task's own injection machinery) is
    # resolved DATA-side by the screener — task-registered artifacts and
    # live label-selector discovery — not from the pod name here. The
    # fault-binary mutation branch above deliberately keeps identity review
    # via ``fault_binary_mutation``; inner commands here were already
    # screened by the escape/banned/readonly checks above.
    return EffectiveTarget(
        scope="pod", namespace=ns, names=(pod_name,),
        raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
    )


def _classify_inline_blade(
    inner: list[str], raw_command: str,
    *, fallback_ns: str, fallback_pod: str,
) -> EffectiveTarget:
    """Parse ``blade create k8s pod-cpu fullload --names X -n ns ...``.

    Distinct from ``_classify_blade_create`` (which parses dict args
    from the LangChain tool_call). Here we parse the CLI tokens.
    """
    if len(inner) < 2 or inner[0] != "blade" or inner[1] != "create":
        # Non-create blade commands (status/destroy/query/version/prepare/revoke)
        # don't target new k8s resources — guard drift comparison not applicable.
        return EffectiveTarget(
            scope=SCOPE_READONLY, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
        )

    # blade create [k8s] <target>-<sub> <action> [flags]
    rest = inner[2:]
    is_k8s = len(rest) > 0 and rest[0] == "k8s"
    if is_k8s:
        rest = rest[1:]

    # Next token is something like "pod-cpu" / "node-mem" / "pod-network"
    blade_subtype = rest[0] if rest else ""
    rest = rest[1:] if rest else []
    # Split "pod-cpu" → scope_hint="pod", target_hint="cpu"
    scope_hint = ""
    target_hint = blade_subtype
    if "-" in blade_subtype:
        scope_hint, _, target_hint = blade_subtype.partition("-")

    blade_action = rest[0] if rest else ""

    # Parse flags inside the inner cmd
    ns = parse_namespace(rest, default="")
    names = _parse_blade_names(rest)
    labels = parse_labels(rest)
    node_name = _parse_flag_value(rest, "--node")

    # Resolve scope
    if is_k8s and scope_hint:
        scope = canonicalise_kind(scope_hint) or scope_hint
    else:
        scope = BLADE_TARGET_TO_SCOPE.get(target_hint, scope_hint or "pod")

    # Tier 1 detection: outer exec into a tool pod namespace + inner
    # blade k8s command without explicit --namespace. Blade v1.8.0
    # rejects --namespace for some subcommands (e.g. pod-network), so
    # the agent legitimately omits it.
    is_tier1 = (
        is_k8s
        and fallback_ns in TOOL_POD_NAMESPACES
        and not ns  # no explicit --namespace in inner blade args
    )

    # Cluster-scoped resources don't carry namespace
    if scope == "node":
        effective_ns = ""
    elif ns:
        effective_ns = ns
    elif is_tier1:
        effective_ns = ""
    else:
        effective_ns = "default"

    # Resolve names
    if scope == "node" and node_name:
        effective_names: tuple[str, ...] = (node_name,)
    elif names:
        effective_names = names
    elif labels:
        effective_names = ()
    elif fallback_pod and scope == "pod":
        # Inside ``kubectl exec POD -- blade create k8s pod-cpu ...``
        # if no --names given, it implicitly targets the host pod.
        effective_names = (fallback_pod,)
    else:
        effective_names = ()

    return EffectiveTarget(
        scope=scope,
        namespace=effective_ns,
        names=effective_names,
        labels=labels,
        blade_target=target_hint,
        blade_action=blade_action,
        confidence=ConfidenceLevel.HIGH,
        raw_command=raw_command,
        is_tier1_exec=is_tier1,
    )


def _parse_blade_names(args: list[str]) -> tuple[str, ...]:
    """Parse blade's ``--names X,Y,Z`` into a name tuple."""
    raw = _parse_flag_value(args, "--names")
    if not raw:
        return ()
    return tuple(n.strip() for n in raw.split(",") if n.strip())


def _parse_flag_value(args: list[str], flag: str) -> str:
    """Generic ``--flag value`` / ``--flag=value`` parser."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
        i += 1
    return ""


# ---------------------------------------------------------------------------
# Other kubectl subcommand classifiers
# ---------------------------------------------------------------------------


def _classify_kubectl_debug(args: list[str], raw_command: str) -> EffectiveTarget:
    """``kubectl debug node/NODE`` or ``kubectl debug POD``.

    Both creates a debug pod that EXECUTES against the target. The
    target itself is what matters (the node or the pod being
    debugged), not the ephemeral debug pod.
    """
    first = _first_positional(args)
    if not first:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                "kubectl debug names no target — expected a pod name or "
                "'node/<node-name>' as the first positional argument"
            ),
                reject_suggestion=_FIX_NAME_THE_TARGET,
        )
    kind, name = _split_kind_name(first)
    canonical = canonicalise_kind(kind) if kind else "pod"
    ns = parse_namespace(args, default="" if canonical == "node" else "default")
    return EffectiveTarget(
        scope=canonical, namespace=ns, names=(name,),
        raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
    )


def _classify_kubectl_node_op(args: list[str], raw_command: str) -> EffectiveTarget:
    """``kubectl cordon NODE`` / ``uncordon NODE`` / ``drain NODE``."""
    node = _first_positional(args)
    if not node:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                "the node-maintenance command (cordon / uncordon / drain) "
                "names no node"
            ),
                reject_suggestion=_FIX_NAME_THE_TARGET,
        )
    return EffectiveTarget(
        scope="node", namespace="", names=(node,),
        raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
    )


def _classify_kubectl_taint(args: list[str], raw_command: str) -> EffectiveTarget:
    """``kubectl taint nodes NODE key=val:Effect``."""
    # First positional is typically "nodes"; second is the node name.
    pos = _list_positionals(args)
    if len(pos) >= 2 and canonicalise_kind(pos[0]) == "node":
        return EffectiveTarget(
            scope="node", namespace="", names=(pos[1],),
            raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
        )
    return EffectiveTarget(
        scope=SCOPE_UNKNOWN, namespace="",
        raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
        reject_detail=(
            "kubectl taint could not be read as 'nodes <node> "
            "<key>=<value>:<Effect>' — the node name is missing"
        ),
            reject_suggestion=_FIX_NAME_THE_TARGET,
    )


def _classify_kubectl_set(args: list[str], raw_command: str) -> EffectiveTarget:
    """``kubectl set <sub-resource> KIND/NAME ...`` — strip the field, then reuse.

    ``set`` is the only whitelisted write verb whose first positional is the
    FIELD being written (``image`` / ``env`` / ``resources`` / …) rather than the
    resource. Once that token is removed the remainder has the same shape every
    other write verb has, so the generic resource classifier handles it — no
    duplicate target-parsing logic.

    ``_first_positional_index`` (not ``_list_positionals``) is used because the
    token has to be REMOVED, and it can sit after flags: both
    ``set image -n ns deploy/x c=i`` and ``set -n ns image deploy/x c=i`` are
    accepted by kubectl.
    """
    expected = ", ".join(sorted(_KUBECTL_SET_SUBRESOURCES))
    idx = _first_positional_index(args)
    if idx is None:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                "kubectl set names neither a sub-resource nor a target "
                f"(expected 'set <{expected}> <kind>/<name> ...')"
            ),
                reject_suggestion=_FIX_NAME_THE_TARGET,
        )
    subresource = args[idx]
    if subresource not in _KUBECTL_SET_SUBRESOURCES:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                f"'{subresource}' is not a kubectl set sub-resource "
                f"(expected one of: {expected})"
            ),
                reject_suggestion=_FIX_UNKNOWN_VOCABULARY,
        )
    # Drop the sub-resource; everything else (flags, kind/name) is untouched.
    remainder = args[:idx] + args[idx + 1:]
    return _classify_kubectl_resource(remainder, raw_command, default_kind=None)


def _classify_kubectl_resource(
    args: list[str], raw_command: str,
    *, default_kind: str | None,
) -> EffectiveTarget:
    """Generic ``kubectl <verb> KIND/NAME`` or ``kubectl <verb> KIND NAME``.

    Used for scale / patch / set / delete / edit / replace / label /
    annotate / autoscale / expose / attach / port-forward / create.

    Handles three positional shapes:
      - ``KIND/NAME`` — slash-joined (e.g. ``scale deploy/myapp``)
      - ``KIND NAME`` — two positionals (e.g. ``scale deployment myapp``)
      - ``NAME`` — bare name with ``default_kind`` filled in (e.g.
        ``attach POD`` where caller passes ``default_kind="pod"``)
    """
    positionals = _list_positionals(args)
    if not positionals:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                "the command names no resource — expected '<kind>/<name>' or "
                "'<kind> <name>' as a positional argument"
            ),
                reject_suggestion=_FIX_NAME_THE_TARGET,
        )

    first = positionals[0]
    kind, name = _split_kind_name(first)

    if not kind:
        # First positional is either a bare kind ("deployment") or a
        # bare name. Disambiguation rules:
        #   1. If the caller supplied ``default_kind`` AND there's
        #      only one positional, prefer the name interpretation
        #      (``attach POD`` where POD might literally be named
        #      "pod" or "deploy"). Without this rule, a pod whose
        #      name collides with a kind keyword gets misclassified.
        #   2. Otherwise if ``first`` matches a known kind, use it
        #      as kind and pull name from positionals[1] (``scale
        #      deployment myapp`` form).
        #   3. Otherwise fall back to ``default_kind`` with ``first``
        #      as the name.
        #   4. If none of the above resolve, return UNKNOWN.
        if default_kind and len(positionals) == 1:
            kind = default_kind
            name = first
        elif _is_known_kind(first):
            kind = first
            name = positionals[1] if len(positionals) >= 2 else ""
        elif default_kind:
            kind = default_kind
            name = first
        else:
            return EffectiveTarget(
                scope=SCOPE_UNKNOWN, namespace="",
                raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
                reject_detail=(
                    f"'{first}' was read as neither a resource kind nor a "
                    "name — write the target as '<kind>/<name>' (e.g. "
                    "'deployment/myapp') so the kind is unambiguous"
                ),
                    reject_suggestion=_FIX_QUALIFY_KIND,
            )
    elif not name and len(positionals) >= 2:
        # Slash form with empty name half — fall back to next positional
        name = positionals[1]

    canonical = canonicalise_kind(kind) if kind else default_kind or ""
    if not canonical:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                f"resource kind '{kind}' is not one the guard recognises, so "
                "it cannot be compared against the approved target's scope"
            ),
                reject_suggestion=_FIX_UNKNOWN_VOCABULARY,
        )

    # Cluster-scoped resources (node/pv/namespace/cluster*role*) skip ns
    cluster_scoped = canonical in ("node", "pv", "namespace",
                                    "clusterrole", "clusterrolebinding",
                                    "storageclass")
    ns = parse_namespace(args, default="" if cluster_scoped else "default")
    labels = parse_labels(args)
    names: tuple[str, ...] = (name,) if name else ()

    return EffectiveTarget(
        scope=canonical, namespace=ns, names=names, labels=labels,
        raw_command=raw_command,
        confidence=ConfidenceLevel.HIGH if name or labels else ConfidenceLevel.LOW,
    )


def _is_known_kind(token: str) -> bool:
    """True iff ``token`` is a known kubectl kind (any spelling)."""
    if not token:
        return False
    head = token.split(".", 1)[0].lower().strip()
    return head in KIND_ALIASES


def _classify_kubectl_run(args: list[str], raw_command: str) -> EffectiveTarget:
    """``kubectl run NAME --image=...`` — creates a new pod."""
    name = _first_positional(args)
    if not name:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
            reject_detail="kubectl run names no pod to create",
            reject_suggestion=_FIX_NAME_THE_TARGET,
        )
    ns = parse_namespace(args, default="default")
    return EffectiveTarget(
        scope="pod", namespace=ns, names=(name,),
        raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
    )


def _classify_kubectl_rollout(args: list[str], raw_command: str) -> EffectiveTarget:
    """``kubectl rollout restart deploy/X`` etc."""
    if not args:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                "kubectl rollout names neither an action nor a target "
                "(expected 'rollout <restart|undo|pause|resume> <kind>/<name>')"
            ),
                reject_suggestion=_FIX_NAME_THE_TARGET,
        )
    # args[0] is the sub-sub (restart/undo/pause/resume); rest is the
    # target resource.
    return _classify_kubectl_resource(args[1:], raw_command, default_kind=None)


def _classify_kubectl_cp(args: list[str], raw_command: str) -> EffectiveTarget:
    """``kubectl cp POD:/src /local`` or reverse.

    Either direction reads/writes the pod's filesystem — scope=pod.
    The pod identity is the part before/after the ``:`` in one of the
    positional args.
    """
    for a in args:
        if a.startswith("-"):
            continue
        if ":" in a:
            pod_part = a.split(":", 1)[0]
            # Pod can be "namespace/pod" or just "pod"
            if "/" in pod_part:
                ns, _, name = pod_part.partition("/")
                return EffectiveTarget(
                    scope="pod", namespace=ns, names=(name,),
                    raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
                )
            ns = parse_namespace(args, default="default")
            return EffectiveTarget(
                scope="pod", namespace=ns, names=(pod_part,),
                raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
            )
    return EffectiveTarget(
        scope=SCOPE_UNKNOWN, namespace="",
        raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
        reject_detail=(
            "kubectl cp names no pod — one side of the copy must be "
            "'<pod>:<path>' (or '<namespace>/<pod>:<path>')"
        ),
            reject_suggestion=_FIX_NAME_THE_TARGET,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_positional(args: list[str]) -> str:
    """First non-flag arg in a kubectl-subcommand-rest list.

    Skips ``--flag=value``, known boolean flags (``--all`` / ``-A`` /
    ``--force`` / ...), and assumes any other ``-x`` / ``--x`` takes
    a value (skip 2 tokens). Returns "" if no positional found.

    Conservative on unknown flags: assumes they take a value so we
    don't accidentally treat ``ns`` in ``-n ns`` as a positional.
    The trade-off is that an unknown boolean flag can cause us to
    miss a real positional — but in that case the classifier falls
    back to UNKNOWN, which the screener default-denies, instead of
    silently letting a wrong call through.
    """
    return next((value for _, value in _iter_positionals(args)), "")


def _list_positionals(args: list[str]) -> list[str]:
    """Every non-flag positional in a kubectl-subcommand-rest, in order."""
    return [value for _, value in _iter_positionals(args)]


def _first_positional_index(args: list[str]) -> int | None:
    """Index of the first non-flag positional, or ``None`` if there is none.

    Returns the POSITION rather than the value so a caller can REMOVE that
    token. ``kubectl set`` needs this: its first positional is the field being
    set, not the resource, and it has to be stripped before the generic resource
    classifier can read the target.
    """
    return next((index for index, _ in _iter_positionals(args)), None)


def _iter_positionals(args: list[str]) -> Iterator[tuple[int, str]]:
    """Yield ``(index, value)`` for each non-flag positional.

    The SINGLE definition of the three rules that separate a target from the
    noise around it: where the flag/positional boundary is, how many tokens a
    flag consumes (delegated to ``_is_valueless_flag``), and that ``--`` ends the
    outer command — everything past it is an inner ``exec`` payload and must not
    be read as an outer positional.

    ``_first_positional`` / ``_list_positionals`` / ``_first_positional_index``
    are all views onto this one walk, so they cannot disagree about any of the
    three. An earlier version of ``_first_positional_index`` carried its own copy
    of the loop, which left the ``--`` stop duplicated with no mechanism keeping
    the copies in step.
    """
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--":
            return
        if not token.startswith("-"):
            yield i, token
            i += 1
        else:
            i += 1 if _is_valueless_flag(token) else 2


def _split_kind_name(token: str) -> tuple[str, str]:
    """Split ``kind/name`` into (kind, name). ``name`` alone → ("", name)."""
    if "/" in token:
        kind, _, name = token.partition("/")
        return kind, name
    return "", token


def _extract_after_double_dash(args: list[str]) -> list[str]:
    """Return args after the ``--`` separator, or [] if none."""
    try:
        idx = args.index("--")
    except ValueError:
        return []
    return args[idx + 1:]


def _has_help_flag(args: list[str]) -> bool:
    """True if ``-h`` or ``--help`` appears anywhere in *args*.

    Used to short-circuit classification: any command invoked with a
    help flag only prints usage text and never mutates state.
    """
    return "-h" in args or "--help" in args


def _coerce_args_dict(tool_args: Any) -> dict[str, Any]:
    """Best-effort coerce of various tool_args shapes into a dict."""
    if isinstance(tool_args, dict):
        return tool_args
    return {}


def _coerce_args_list(tool_args: Any) -> list[str]:
    """Best-effort coerce of kubectl tool_args into a list[str].

    Recognises the actual production shape of ``chaos_agent.tools.kubectl``
    (``{subcommand: str, v_args: str, kubeconfig?: str, context?: str,
    cluster?: str, task_id?: str}``) — without this branch, every real
    kubectl tool_call would coerce to ``[]`` and classify as UNKNOWN.

    Also accepts legacy shapes for back-compat with synthetic test
    fixtures and future re-binding of kubectl as a list-arg tool:
        - list[str] directly
        - dict with ``command`` / ``args`` / ``argv`` / ``cmd`` key
        - str shell-quoted

    Ignores ``kubeconfig`` / ``context`` / ``cluster`` because they
    select the cluster, not the target resource — the guard's job is
    target identity, not cluster identity.
    """
    if isinstance(tool_args, list):
        return [str(x) for x in tool_args]
    if isinstance(tool_args, dict):
        # Production shape — subcommand + v_args
        if "subcommand" in tool_args:
            out: list[str] = [str(tool_args.get("subcommand") or "")]
            v_args = tool_args.get("v_args") or ""
            if v_args:
                try:
                    out.extend(shlex.split(str(v_args)))
                except ValueError:
                    out.extend(str(v_args).split())
            return [x for x in out if x]
        # Legacy / synthetic shapes
        for key in ("command", "args", "argv", "cmd"):
            v = tool_args.get(key)
            if isinstance(v, list):
                return [str(x) for x in v]
            if isinstance(v, str):
                try:
                    return shlex.split(v)
                except ValueError:
                    return v.split()
    if isinstance(tool_args, str):
        try:
            return shlex.split(tool_args)
        except ValueError:
            return tool_args.split()
    return []


def _format_raw_command(tool_name: str, tool_args: Any) -> str:
    """Build a short, audit-friendly representation of the tool call."""
    if isinstance(tool_args, dict):
        parts = [f"{k}={v!r}" for k, v in tool_args.items()]
        return f"{tool_name}({', '.join(parts)})"
    if isinstance(tool_args, list):
        return f"{tool_name}({' '.join(str(x) for x in tool_args)})"
    if isinstance(tool_args, str):
        return f"{tool_name}({tool_args})"
    return f"{tool_name}(?)"


__all__ = [
    "BLADE_TARGET_TO_SCOPE",
    "BANNED_KUBECTL_SUBS",
    "DESTRUCTIVE_KUBECTL_SUBS",
    "KIND_ALIASES",
    "READONLY_KUBECTL_SUBS",
    "SCOPE_BANNED",
    "SCOPE_READONLY",
    "SCOPE_UNKNOWN",
    "canonicalise_kind",
    "infer_effective_target",
    "parse_labels",
    "parse_namespace",
]
