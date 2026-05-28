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
import shlex
from typing import Any

from .types import ConfidenceLevel, EffectiveTarget

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sentinel scopes — the guard knows these aren't real k8s kinds.
# ---------------------------------------------------------------------------

SCOPE_READONLY = "__readonly__"
SCOPE_BANNED = "__banned__"
SCOPE_UNKNOWN = "__unknown__"

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
    """Extract the label selector from a kubectl ``-l`` / ``--selector`` flag.

    Returns a dict of {key: value}. Operator-style selectors
    (``key!=value``, ``key in (v1,v2)``) are flattened to {key: raw}
    so equality-comparison stays simple — the guard treats any
    non-trivial selector difference as drift anyway.
    Missing flag returns {}.

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
        if a in ("-l", "--selector"):
            if i + 1 < len(args):
                raw_selector = args[i + 1]
                i += 1
        elif a.startswith("-l=") or a.startswith("--selector="):
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
    "apply",     # nearly always uses -f, target depends on YAML content
    "certificate",  # CSR approval — outside chaos scope
})

# Destructive kubectl subcommands we DO classify. Each maps to a
# function below that parses its specific arg shape.
DESTRUCTIVE_KUBECTL_SUBS: frozenset[str] = frozenset({
    "exec", "scale", "cordon", "uncordon", "drain", "taint",
    "patch", "set", "delete", "edit", "replace", "run",
    "label", "annotate", "autoscale", "expose", "debug",
    "attach", "port-forward", "proxy", "cp", "create", "rollout",
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
    # ``kubectl_ro`` is the Phase 1 read-only kubectl flavour introduced
    # alongside the phase1_screener (Layer A of the phase 1 readonly
    # plan); its ``subcommand`` Literal is already constrained to the
    # read subset, so by reaching here we already know it's safe.
    # ``read_file`` / ``save_fault_plan`` touch the local FS only (not
    # the cluster) — safe for both phases.
    if tool_name in ("blade_status", "blade_query_k8s",
                     "read_knowledge_resource", "read_skill_resource",
                     "activate_skill", "submit_fault_intent",
                     "kubectl_ro", "read_file", "save_fault_plan",
                     "finish_planning"):
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
            )
        return EffectiveTarget(
            scope=SCOPE_READONLY,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
        )

    if tool_name == "blade_create":
        return _classify_blade_create(_coerce_args_dict(tool_args), raw_command)

    if tool_name == "kubectl":
        return _classify_kubectl(_coerce_args_list(tool_args), raw_command)

    # Unknown tool — default-deny. Forces operator to add explicit
    # classification rather than silently allowing new tools.
    return EffectiveTarget(
        scope=SCOPE_UNKNOWN,
        namespace="",
        raw_command=raw_command,
        confidence=ConfidenceLevel.UNKNOWN,
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


def _classify_kubectl(args: list[str], raw_command: str) -> EffectiveTarget:
    """Dispatch on kubectl subcommand."""
    if not args:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
        )

    # Skip leading global flags (--kubeconfig=..., --context=...,
    # --namespace=... when used before the subcommand) to find the
    # actual verb.
    sub_idx = _find_subcommand_index(args)
    if sub_idx is None:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
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
        )

    # Stdin/-f file inputs cannot be safely classified — content is
    # not in args. Banned for any destructive subcommand that accepts
    # -f (create/replace/patch/set/delete/edit). ``apply`` already
    # banned above.
    if sub in ("create", "replace", "patch", "set", "delete", "edit"):
        if _uses_file_input(rest):
            return EffectiveTarget(
                scope=SCOPE_BANNED, namespace="",
                raw_command=raw_command, confidence=ConfidenceLevel.HIGH,
            )

    # Read-only — no comparison needed.
    if sub in READONLY_KUBECTL_SUBS:
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
    if sub in ("patch", "set", "delete", "edit", "replace", "label", "annotate", "autoscale"):
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
        )
    if sub == "create":
        # create RESOURCE name (without -f) — limited use, classify
        # by resource kind.
        return _classify_kubectl_resource(rest, raw_command, default_kind=None)

    # Anything else: unknown subcommand → default-deny.
    return EffectiveTarget(
        scope=SCOPE_UNKNOWN, namespace="",
        raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
    )


def _find_subcommand_index(args: list[str]) -> int | None:
    """Skip leading global flags to find the kubectl subcommand index.

    Global flags (kubectl --help shows ~40) start with -- or -, and
    most take a value. We skip both flag-only (``--v=4``) and
    flag+value (``--context my-ctx`` / ``--kubeconfig ~/.kube/x``)
    forms. The subcommand is the first non-flag arg.
    """
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("-"):
            return i
        # Equals form: --flag=value or -n=ns — single token, skip 1
        if "=" in a:
            i += 1
            continue
        # Known boolean flag — single token, no value follows
        if a in _BOOLEAN_FLAGS:
            i += 1
            continue
        # Flag+value form: --context X, -n ns, etc — skip 2
        i += 2
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
        )

    inner = _extract_after_double_dash(args)
    if not inner:
        # Pure stdio attach — acts on the pod.
        return EffectiveTarget(
            scope="pod", namespace=ns, names=(pod_name,),
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

    # Container escape attempts via nsenter/chroot — classifier
    # can't reliably know the host they'd land on. Default-deny.
    if inner[0] in ("nsenter", "chroot", "unshare"):
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
        )

    # Plain shell command (rm/kill/stress/iptables/etc) — acts on the
    # pod's own filesystem/process space. scope=pod is correct.
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
        # Not a blade create — could be blade status (read-only inside
        # exec). Treat conservatively as pod-scope.
        return EffectiveTarget(
            scope="pod", namespace=fallback_ns, names=(fallback_pod,),
            raw_command=raw_command, confidence=ConfidenceLevel.LOW,
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
    )


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
            )
    elif not name and len(positionals) >= 2:
        # Slash form with empty name half — fall back to next positional
        name = positionals[1]

    canonical = canonicalise_kind(kind) if kind else default_kind or ""
    if not canonical:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN, namespace="",
            raw_command=raw_command, confidence=ConfidenceLevel.UNKNOWN,
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
    positionals = _list_positionals(args)
    return positionals[0] if positionals else ""


def _list_positionals(args: list[str]) -> list[str]:
    """Return ALL non-flag positionals from a kubectl-subcommand-rest.

    Mirrors ``_first_positional`` but materialises the whole list,
    used by sub-classifiers that need to disambiguate KIND/NAME forms.

    Skips ``--flag=value``, known boolean flags, assumes other flags
    take a value (skip 2). Stops at the ``--`` separator so an inner
    ``exec`` payload's arguments aren't treated as outer positionals.
    """
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if not a.startswith("-"):
            out.append(a)
            i += 1
        elif "=" in a:
            i += 1
        elif a in _BOOLEAN_FLAGS:
            i += 1
        else:
            i += 2  # unknown flag — assume flag+value
    return out


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
