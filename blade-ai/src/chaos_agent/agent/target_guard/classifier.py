"""Classify a tool_call into the resource it would actually act on.

Companion to ``guard.py`` — the classifier produces an
``EffectiveTarget``; the guard compares that to ``ApprovedTarget``
and emits a ``GuardDecision``.

Since phase-7 T5 this module is the GENERIC layer of the target-guard
classification: the top-level entry point (:func:`infer_effective_target`)
plus the cross-carrier shared helpers (kind canonicalisation, namespace /
label-selector parsing). Every carrier's OWN tool vocabulary — the
``blade_create`` dict-arg classifier and inline ``kubectl exec ... blade``
CLI parser (``providers/chaosblade/provider.py``), the kubectl command-line family
(``providers/k8s_native/classifier.py``), the python-agent and host-shell
classifiers — is enacted through ``FaultProviderRegistry.classify_tool_target``
so this layer holds no carrier tool-name branch or carrier vocabulary table.

Verdict coverage policy (unchanged semantics, wherever the classifier runs):

  - **READONLY**: known read-only tools (this layer's generic table, or a
    provider claiming its own read-only tools). Sentinel
    ``scope="__readonly__"`` — no comparison needed.
  - **BANNED**: calls outside the target-scoped operation model (e.g.
    ``_execute_skill_script`` without the operator opt-in). Sentinel
    ``scope="__banned__"``.
  - **UNKNOWN**: anything else — unrecognised tool name, unrecognised
    subcommand, malformed args. Sentinel ``scope="__unknown__"`` so the
    guard can emit ``REJECT_UNKNOWN``.
"""

from __future__ import annotations

import logging
from typing import Any

from .types import (
    ConfidenceLevel,
    EffectiveTarget,
    SCOPE_BANNED,
    SCOPE_ESCAPE,
    SCOPE_READONLY,
    SCOPE_UNKNOWN,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sentinel scopes — the guard knows these aren't real k8s kinds. Canonical
# home is types.py since phase-7 T5 (see the migration note there); imported
# above for the constructions below and re-exported for the guard-side
# consumers that historically imported them from this module.
# ---------------------------------------------------------------------------


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
    # Admission-control drill target (ResourceQuota-exceeded → Pod Pending).
    # Without these aliases ``kubectl create/delete quota`` reads the first
    # positional as neither kind nor name and collapses to __unknown__ —
    # the drill step becomes unexecutable-by-construction.
    "resourcequota": "resourcequota", "resourcequotas": "resourcequota", "quota": "resourcequota", "quotas": "resourcequota",
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

#: Kinds whose objects live OUTSIDE any namespace — the SINGLE SOURCE of
#: the namespace topology (R21/G-5). Previously this six-kind set was
#: copied inline in drift_policy (``CLUSTER_SCOPED_KINDS``), the k8s-native
#: classifier's resource branch, and the carrier-family registry; each copy
#: evolved alone and the carrier-family copy drifted into G-5's
#: self-referential contradiction (cluster members registered under the
#: carrier ns while their own cleanup audit entries carry no ``-n``).
#: Agent-tree consumers must import from here — the spec layer
#: (``spec/fault_registry.py``'s ``aggregate_cluster_scoped``) keeps its own
#: declaration on purpose: it sits BELOW the agent layer and must not
#: import up; the two sets are cross-referenced by comment, not by code.
CLUSTER_SCOPED_KINDS: frozenset[str] = frozenset({
    "node", "pv", "namespace", "clusterrole",
    "clusterrolebinding", "storageclass",
})


def is_cluster_scoped_kind(canonical: str) -> bool:
    """True when ``canonical`` is a cluster-scoped kind (already
    canonicalised — feed it through :func:`canonicalise_kind` first if
    the input is raw).

    Cluster-scoped objects live in NO namespace: an ``-n`` flag on their
    commands is noise kubectl silently ignores, so consumers comparing
    namespaces must treat the dimension as ABSENT for these kinds
    (see ``execution_artifacts._ns_matches`` for the matching rule and
    ``drift_policy`` for the drift-check twin of this rule).
    """
    return canonical in CLUSTER_SCOPED_KINDS



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
# pflag-normalised flag parsing — the shared v_args expansion layer.
#
# Every kubectl-arg consumer (namespace, selector, filename, widening
# gate) used to hand-roll its own token matching, and each hand-rolled
# copy had the same blind spots: combined shorthand bundles
# (``-nprod`` = namespace "prod", ``-An prod`` = ``-A -n prod``) and
# bundle-internal value absorption. Probes G2b/G8 showed those blind
# spots are not merely cosmetic: a guard that anchors on ``default``
# while kubectl executes ``-nprod`` passes the drift check (route=pass)
# — so this layer exists ONCE and every consumer reads from it.
# ---------------------------------------------------------------------------

# Shorthands that CONSUME a value inside a single-dash token (pflag
# semantics: everything after the shorthand in the token — or the next
# arg if the token ends there — becomes its VALUE, so no later
# character in that token is a shorthand). Enumerated from the
# kubectl v1.34.1 helps of the seven -f subs: f filename, k kustomize,
# o output, l selector, p patch, plus the global n namespace
# (probe-verified live: ``get pods -nApp`` parses as namespace "App").
_KUBECTL_VALUE_SHORTHANDS: frozenset[str] = frozenset("fnlkop")

# Normalisation of the shorthands this classifier consumes. Characters
# not in the map keep a ``-<char>`` placeholder name (nothing consumes
# them; the placeholder just keeps bundle walking total).
_KUBECTL_SHORT_TO_LONG: dict[str, str] = {
    "A": "--all-namespaces",
    "n": "--namespace",
    "f": "--filename",
    "k": "--kustomize",
    "l": "--selector",
    "o": "--output",
    "p": "--patch",
}

# Long flags known to consume a SEPARATED next-token value. Long flags
# outside this set are treated as boolean (value None) — the arity of
# unknown long flags cannot be guessed, and none of the consumers below
# anchor on an unknown long flag's value.
_KUBECTL_LONG_VALUE_FLAGS: frozenset[str] = frozenset({
    "--namespace", "--filename", "--kustomize", "--selector",
    "--output", "--patch", "--prune-allowlist", "--labels",
})


def iter_flag_assignments(args: list[str]) -> list[tuple[str, str | None, str]]:
    """Normalise a kubectl arg tail into ``(long_flag, value, origin)`` triples.

    Handles, per live pflag semantics (probe-verified against
    kubectl v1.34.1): separated values (``-n prod`` / ``--namespace
    prod``), ``=`` forms (``-n=prod`` / ``--namespace=prod``), combined
    shorthand bundles (``-An prod`` = ``-A -n prod``; ``-nprod`` =
    namespace "prod"; ``-nw`` = namespace "w"), and ``=`` attached to a
    boolean shorthand (``-A=false``). Scanning stops at a bare ``--``
    separator — anything after it is the inner command of ``kubectl
    exec`` and must not leak into outer flag inference. Positional
    arguments are skipped (not emitted).

    A value-absorbing shorthand swallows the rest of its token or the
    next arg, so ``-nApp`` yields ``--namespace`` = "App" with NO
    all-namespaces flag — value-interior capitals are not shorthands.
    Unknown long flags yield ``(flag, None)`` (boolean assumption).
    """
    out: list[tuple[str, str | None, str]] = []
    i = 0
    n = len(args)
    while i < n:
        a = args[i]
        if a == "--":
            break
        if a.startswith("--"):
            name, _sep, inline = a.partition("=")
            if _sep:
                out.append((name, inline, a))
            elif name in _KUBECTL_LONG_VALUE_FLAGS and i + 1 < n:
                out.append((name, args[i + 1], a))
                i += 2
                continue
            else:
                out.append((name, None, a))
            i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            chars = a[1:]
            j = 0
            consumed_next = False
            while j < len(chars):
                ch = chars[j]
                long_name = _KUBECTL_SHORT_TO_LONG.get(ch, f"-{ch}")
                if chars[j + 1 : j + 2] == "=":
                    # ``-A=false`` / ``-n=prod``: '=' hands the rest of
                    # the token to THIS shorthand (boolean or not).
                    out.append((long_name, chars[j + 2 :], a))
                    break
                if ch in _KUBECTL_VALUE_SHORTHANDS:
                    inline = chars[j + 1 :]
                    if inline:
                        out.append((long_name, inline, a))
                    elif i + 1 < n:
                        # pflag consumes the next arg UNCONDITIONALLY as
                        # this shorthand's value — even when it is
                        # flag-shaped (``-n -A`` = namespace "-A"; probe:
                        # ``get pods -n -A`` → "No resources found in -A
                        # namespace.") or the ``--`` separator
                        # (``-l --`` = selector "--"). The consumed token
                        # must NOT be re-scanned: re-scanning conjures
                        # phantom flags (``-n -A … -f -`` read as if it
                        # carried --all-namespaces — a false widening
                        # reject) or prematurely stops at an absorbed
                        # ``--``, hiding every later flag.
                        out.append((long_name, args[i + 1], a))
                        consumed_next = True
                    else:
                        # malformed trailing value flag — pflag errors
                        # client-side; emit an empty value so consumers
                        # treat the assignment as absent.
                        out.append((long_name, "", a))
                    break  # value absorbed: the token ends here
                # Boolean shorthand (or unknown char — kubectl itself
                # rejects unknown shorthands, so failing to interpret
                # one stays fail-closed): keep walking the bundle.
                out.append((long_name, None, a))
                j += 1
            i += 2 if consumed_next else 1
            continue
        i += 1
    return out


def namespace_values(args: list[str]) -> list[str]:
    """All explicit ``--namespace`` values in *args* (pflag-normalised).

    Used by the k8s-native classifier's namespace-consistency gate:
    kubectl's pflag lets a LATER ``-n`` silently override an earlier
    one, so a call carrying two different namespaces cannot be anchored
    to a single one for the drift check.
    """
    return [
        str(v)
        for name, v, _origin in iter_flag_assignments(args)
        if name == "--namespace" and v
    ]


# ---------------------------------------------------------------------------
# Namespace parsing — handles all kubectl flag forms.
# ---------------------------------------------------------------------------


def parse_namespace(args: list[str], default: str = "default") -> str:
    """Extract the namespace from a kubectl arg list.

    Handles every spelling pflag accepts, via ``iter_flag_assignments``:
      - ``-n ns`` / ``-n=ns`` / ``--namespace ns`` / ``--namespace=ns``
      - combined shorthand bundles: ``-nprod`` (value glued on),
        ``-An prod`` (``-A -n prod`` — the trailing ``-n`` absorbs the
        NEXT arg), ``-n=prod`` (``=`` form on a shorthand)
    - flag in any position (before OR after the subcommand)

    Stops at the ``--`` separator — anything after it belongs to an
    INNER command (``kubectl exec POD -- prog ...``) whose own ``-n``
    flag must not leak into the outer kubectl's namespace inference.

    Returns the FIRST explicit namespace, or ``default`` if none. When
    a call carries several DISTINCT namespaces the k8s-native
    classifier's consistency gate rejects it before this "first wins"
    choice can matter. The caller should pass ``default=""`` for
    cluster-scoped subcommands (node/cordon/taint/etc) so missing
    namespace doesn't get auto-promoted to "default".
    """
    for name, value, _origin in iter_flag_assignments(args):
        if name == "--namespace" and value:
            return str(value)
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
    for name, value, _origin in iter_flag_assignments(args):
        if name not in ("--selector", "--labels") or not value:
            continue
        for pair in str(value).split(","):
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
    return selector


def _classify_mcp_tool(
    tool_name: str, attach_to: frozenset[str], raw_command: str,
) -> EffectiveTarget:
    """Classify an operator-attached MCP tool (posture A — trust ``attach_to``).

    The guard cannot infer a k8s target from arbitrary MCP arguments, and
    MCP servers are installed and phase-attached by the operator, so the
    guard does NOT police that wiring: a registered MCP tool is passed
    through as READONLY. ``SCOPE_READONLY`` short-circuits the drift
    comparison, so this never contributes to the approved write-set either
    (the classifier is shared with write-set projection — a READONLY verdict
    is excluded there, exactly as for every other read-only tool).

    A ``phase2`` (mutating-phase) attachment is logged at WARNING because
    there the pass-through bypasses target-drift checking: the operator has
    put a tool the guard cannot verify into the one phase that mutates, and
    owns that decision. Read-only-phase attachments log at INFO.
    """
    if "phase2" in attach_to:
        logger.warning(
            "target_guard: MCP tool %r is attached to phase2 (mutating) — "
            "classified READONLY pass-through; its cluster target cannot be "
            "verified, so target-drift does not apply. The operator owns this "
            "wiring (attach_to=%s).",
            tool_name, sorted(attach_to),
        )
    else:
        logger.info(
            "target_guard: MCP tool %r classified READONLY (attach_to=%s)",
            tool_name, sorted(attach_to),
        )
    return EffectiveTarget(
        scope=SCOPE_READONLY,
        namespace="",
        raw_command=raw_command,
        confidence=ConfidenceLevel.HIGH,
    )


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
    # cluster) — safe for both phases. Carrier-owned read-only tools
    # (blade_help / blade_status / blade_query_k8s / blade_python_prepare /
    # blade_python_revoke / host_read) are claimed by their providers'
    # ``classify_tool_target`` hooks via the registry dispatch below
    # (phase-7 T5) — the vocabulary lives with its carrier.
    # NOTE: ``kubectl`` / ``kubectl_read`` are intentionally NOT in this
    # short-circuit — they are claimed by the k8s-native provider's hook,
    # whose classifier rules on the inner command too (``exec``/``debug``
    # with a read-only inner → READONLY; mutating inner → pod/escape,
    # which the screeners reject).
    if tool_name in ("read_knowledge_resource", "read_skill_resource",
                     "activate_skill", "submit_fault_intent",
                     "read_file", "save_fault_plan",
                     "finish_planning", "propose_plan_change",
                     "submit_verification", "submit_recover_verification",
                     "request_replan",
                     "time_wait",
                     # Progress ledger write: a pure control-signal note with no
                     # cluster side effect (touches no fault target), same class
                     # as request_replan / time_wait. ``finish_execution`` is the
                     # terminal form of the same class (B81: it was added with the
                     # #39 clean-exit fix — tool + factory binding + prompt
                     # teaching — but this whitelist never heard of it, so the
                     # guard answered the prompt-taught STOP call with
                     # REJECT_UNKNOWN and the model had to improvise a
                     # update_progress phase-write downgrade).
                     "update_progress", "finish_execution"):
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

    # Carrier dispatch (phase-7 T5): each provider classifies its OWN tools
    # (injection, read-only, and embedded deliveries riding another carrier's
    # tool) through the registry — this generic layer holds no carrier
    # tool-name branch or carrier vocabulary table. Channel-independent: the
    # guard rules on the tool_call itself.
    from chaos_agent.agent.providers.registry import FaultProviderRegistry

    classified = FaultProviderRegistry.classify_tool_target(
        tool_name, tool_args, raw_command,
    )
    if classified is not None:
        return classified

    # MCP tool seam (posture A): the generic layer holds no MCP tool-name
    # branch — it asks the MCP registry whether this is an operator-attached
    # MCP tool. Registered → best-effort READONLY pass-through (the guard
    # cannot infer a k8s target from arbitrary MCP args; the operator owns
    # the wiring). Unregistered (MCP disabled / not connected / a direct
    # unit test) → fall through to the default-deny below, byte-identical to
    # the pre-MCP behaviour. Lazy import mirrors the provider-registry
    # dispatch above and keeps target_guard free of a hard mcp dependency.
    from chaos_agent.mcp.registry import McpToolRegistry

    mcp_attach = McpToolRegistry.get(tool_name)
    if mcp_attach is not None:
        return _classify_mcp_tool(tool_name, mcp_attach, raw_command)

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
# Carrier classifier migrations (phase-7 T5) — every carrier's tool
# classification now lives in its provider domain, enacted through the
# registry dispatch in :func:`infer_effective_target`:
#   - blade_create (dict args) + inline ``kubectl exec ... blade`` CLI
#     parser + BLADE_TARGET_TO_SCOPE → providers/chaosblade/provider.py
#   - kubectl command-line family (60+ subcommands, vocab constants,
#     escape/vehicle/facts logic) → providers/k8s_native/classifier.py
#   - blade_python_create → providers/chaosblade/python_provider.py
#   - host_inject → providers/host_shell/provider.py
# Pure moves — behaviour is byte-identical (the guard is a security layer).
# ---------------------------------------------------------------------------








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
    "CLUSTER_SCOPED_KINDS",
    "KIND_ALIASES",
    "SCOPE_BANNED",
    "SCOPE_ESCAPE",
    "SCOPE_READONLY",
    "SCOPE_UNKNOWN",
    "canonicalise_kind",
    "infer_effective_target",
    "is_cluster_scoped_kind",
    "parse_labels",
    "parse_namespace",
]
