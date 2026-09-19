"""kubectl command-line target classification — the k8s-native carrier's
guard-side domain knowledge.

Migrated from ``target_guard/classifier.py`` (phase-7 T5) — pure move,
behaviour byte-identical (the guard is a security layer). kubectl alone has
60+ subcommands with non-uniform argument shapes; that vocabulary is this
carrier's, so it lives here beside its provider. Consumed by
``K8sNativeProvider.classify_tool_target`` — and, through the registry
dispatch, by every guard-side caller of ``infer_effective_target``.

Import-cycle NOTE: this module is imported lazily (from the k8s-native
provider's ``classify_tool_target`` hook), never at provider-registration
time, so its module-level imports may safely reach ``target_guard`` — by
the time it is first imported the target_guard package is fully
initialised. See the NOTE in chaosblade.py for the registration-time
constraint it stays clear of.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterator
from typing import Any

import yaml

from chaos_agent.agent.target_guard.carriers import _FAULT_BINARIES
from chaos_agent.agent.target_guard.classifier import (
    KIND_ALIASES,
    canonicalise_kind,
    is_cluster_scoped_kind,
    iter_flag_assignments,
    namespace_values,
    parse_labels,
    parse_namespace,
)
from chaos_agent.agent.target_guard.types import (
    SCOPE_BANNED,
    SCOPE_ESCAPE,
    SCOPE_READONLY,
    SCOPE_UNKNOWN,
    ConfidenceLevel,
    EffectiveTarget,
)


# ---------------------------------------------------------------------------
# Repair-hint constants, paired one-to-one with the reject causes below —
# see the pairing rationale in target_guard/classifier.py (task-866648cc).
# ---------------------------------------------------------------------------

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
_FIX_EXEC_USE_SEPARATOR = (
    "kubectl requires `--` before an exec/debug inner command: write it as "
    "`POD [flags] -- COMMAND` (the command-mode form without the separator is "
    "refused by kubectl itself: \"exec [POD] [COMMAND] is not supported "
    "anymore\"). If the trailing tokens are flag VALUES, write the flag and "
    "its value together (`--image=busybox`, `-n default`) so the entry and its "
    "flags stay the whole command line."
)
_FIX_EXEC_ONE_ENTRY = (
    "The separator closes the ENTRY: write `POD [flags] -- COMMAND` with "
    "exactly one positional before the `--`. Tokens between the entry and "
    "the separator are refused because their effect is client-dependent — "
    "some kubectl versions run them as the command head, others drop them — "
    "so the guard cannot say what would execute. If the extra token was a "
    "flag VALUE, glue it to its flag (`--image=busybox`, `-n default`)."
)
_FIX_DEBUG_ONE_TARGET = (
    "One target per call: `debug POD [flags] -- COMMAND` (or `debug "
    "node/<node> ... -- COMMAND`). Additional positionals are not extra "
    "flags — kubectl debug resolves each one as a SEPARATE target, creating "
    "a privileged pod / ephemeral container per target, so the guard cannot "
    "compare them against the single approved target."
)


# Read-only kubectl subcommands. ``READONLY`` verdict, no comparison.
READONLY_KUBECTL_SUBS: frozenset[str] = frozenset(
    {
        "get",
        "describe",
        "top",
        "logs",
        "events",
        "version",
        "api-resources",
        "api-versions",
        "explain",
        "auth",
        "wait",
        "diff",
        "help",
    }
)

# Read-only sub-subcommands of kubectl rollout. ``rollout status`` /
# ``rollout history`` are query-only; the others mutate.
READONLY_ROLLOUT_SUBS: frozenset[str] = frozenset({"status", "history"})

# Read-only sub-subcommands of kubectl config. ``view`` / ``current-context``
# are query-only; the others mutate kubeconfig itself.
READONLY_CONFIG_SUBS: frozenset[str] = frozenset(
    {
        "view",
        "current-context",
        "get-contexts",
        "get-clusters",
        "get-users",
    }
)

# Explicitly banned kubectl subcommands — too dangerous to classify.
# ``apply -f`` requires reading the YAML to know targets; ``config``
# mutating subs change kubeconfig itself; ``certificate`` issues TLS
# certs.
BANNED_KUBECTL_SUBS: frozenset[str] = frozenset(
    {
        # "apply" removed — handled by _uses_file_input + stdin_data whitelist
        "certificate",  # CSR approval — outside chaos scope
    }
)

# Resources allowed to be created via kubectl apply/create -f with stdin_data.
# Only low-risk resources that don't run workloads. Workload resources
# (Deployment, DaemonSet, Pod, Job, etc.) are NOT allowed.
#
# ``faultdrill`` (openspec faultdrill-cr-channel): the CR CHANNEL's own
# instance object — a namespaced, low-risk recipe record (the injected
# fault lives in the swapped Secret / patched Deployment, not in the CR).
# The whitelist entry is a LOWERCASED literal of
# ``providers/faultdrill/crd.py:CRD_KIND`` — cross-carrier import would
# couple the two subpackages, so the drift hazard is pinned by test
# (``CRD_KIND.lower() in ALLOWED_MANIFEST_KINDS``) instead. The CRD
# definition object itself (CustomResourceDefinition kind) is
# deliberately NOT here: it installs programmatically (D2 — the LLM
# face never sees an admissible CRD install).
ALLOWED_MANIFEST_KINDS: frozenset[str] = frozenset(
    {
        "persistentvolumeclaim",
        "pvc",
        "persistentvolume",
        "pv",
        "configmap",
        "secret",
        "namespace",
        "faultdrill",
    }
)

# Workload kinds — every kind that starts containers — on the IMPERATIVE
# create channel (``kubectl create KIND NAME --image=...`` without -f).
# Canonicalised names, the same vocabulary ``_classify_kubectl_resource``
# emits in ``scope``. The manifest channel whitelists the NON-workload
# kinds above and admits exactly one workload shape (the drill-target
# Deployment contract); the imperative channel carries no manifest for
# any contract to inspect, so every workload kind on it is banned
# wholesale (see the ``sub == "create"`` branch of the dispatcher).
# kubectl's imperative create today only builds deployment/job/cronjob
# shapes, but the set is deliberately the full workload family: a kubectl
# extension or a kind-alias spelling must land in the ban, not in a gap.
_IMPERATIVE_WORKLOAD_KINDS: frozenset[str] = frozenset(
    {
        "pod",
        "deployment",
        "daemonset",
        "statefulset",
        "replicaset",
        "replicationcontroller",
        "job",
        "cronjob",
    }
)


# W-55-6: kubectl imperative-create subtype grammar. ``create service`` and
# ``create secret`` take a SUBTYPE positional before the NAME (``create
# service clusterip NAME``, ``create secret tls NAME``). The subtype is
# grammar, not the resource name — the generic positional reader only models
# ``KIND NAME``, so without stripping the subtype it reads the subtype AS the
# name and drops the real one. Keyed by canonical kind; values are the kubectl
# subtype tokens (lowercased).
_CREATE_SUBTYPES: dict[str, frozenset[str]] = {
    "service": frozenset({"clusterip", "nodeport", "loadbalancer", "externalname"}),
    "secret": frozenset({"generic", "docker-registry", "tls"}),
}


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
DESTRUCTIVE_KUBECTL_SUBS: frozenset[str] = frozenset(
    {
        "exec",
        "scale",
        "cordon",
        "uncordon",
        "drain",
        "taint",
        "patch",
        "set",
        "delete",
        "edit",
        "replace",
        "run",
        "label",
        "annotate",
        "autoscale",
        "expose",
        "debug",
        "attach",
        "port-forward",
        "proxy",
        "cp",
        "create",
        "rollout",
        "apply",
    }
)

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
_KUBECTL_SET_SUBRESOURCES: frozenset[str] = frozenset(
    {
        "image",
        "env",
        "resources",
        "serviceaccount",
        "sa",
        "subject",
        "selector",
    }
)


def _classify_kubectl(
    args: list[str],
    raw_command: str,
    *,
    raw_args: dict | None = None,
    _cmdline_raw: str | None = None,
) -> EffectiveTarget:
    """Dispatch on kubectl subcommand.

    ``_cmdline_raw`` (facts engine only): the raw text of THIS level's
    command line, threaded down by a parent ``kubectl exec`` recursion so
    a nested exec branch judges its OWN inner command rather than the
    outer line's. ``None`` → the exec branch falls back to the call's
    ``v_args`` string when present, else stays on the token path.
    ``raw_command`` is NOT usable for this: it is the audit-facing
    ``_format_raw_command`` rendering (``kubectl(subcommand=..., ...)``),
    not a shell line.
    """
    if not args:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.UNKNOWN,
            reject_detail="the kubectl call carries no subcommand at all",
            reject_suggestion=_FIX_STATE_SUBCOMMAND,
        )

    # Skip leading global flags (--kubeconfig=..., --context=...,
    # --namespace=... when used before the subcommand) to find the
    # actual verb.
    sub_idx = _find_subcommand_index(args)
    if sub_idx is None:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                "no kubectl subcommand was found — every token parsed as a "
                "global flag or a flag's value"
            ),
            reject_suggestion=_FIX_STATE_SUBCOMMAND,
        )
    sub = args[sub_idx]
    rest = args[sub_idx + 1 :]

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

    # Namespace-consistency gate — kubectl's pflag lets a LATER ``-n``
    # silently override an earlier one (probe: ``get pods -n default
    # -n kube-system`` returns kube-system pods). A call carrying two
    # DISTINCT namespaces cannot be anchored to a single one: the
    # guard would judge the first while kubectl executes the last
    # (probe G6: ``-n prod pod mypod -nother`` classified against prod
    # and passed while kubectl would have run in "other"). Form issue,
    # not a mechanism ban — the compliant form exists (drop one).
    ns_seen = namespace_values(rest)
    if len(set(ns_seen)) > 1:
        return EffectiveTarget(
            scope=SCOPE_BANNED,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
            reject_detail=(
                "conflicting --namespace values in one call ("
                + ", ".join(ns_seen)
                + "): kubectl lets the LAST one silently win, so no single "
                "namespace can be anchored for the drift check"
            ),
            reject_suggestion=(
                "Re-issue the call with exactly one namespace flag (one "
                "-n / --namespace, any spelling form)."
            ),
        )

    # Bans first — short-circuit before any parsing.
    if sub in BANNED_KUBECTL_SUBS:
        return EffectiveTarget(
            scope=SCOPE_BANNED,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
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
        if _malformed_stdin_data(raw_args):
            # The tool schema declares stdin_data: str, but the model emits
            # tool_call args as JSON — a manifest can arrive structured
            # (dict/list), which crashed the classifier with a TypeError
            # inside re.findall instead of fail-closing (probe I2).
            got = type((raw_args or {}).get("stdin_data")).__name__
            return EffectiveTarget(
                scope=SCOPE_UNKNOWN,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.HIGH,
                reject_detail=(
                    f"kubectl {sub}: stdin_data must be a YAML text string, "
                    f"got {got} — a structured manifest cannot be classified"
                ),
                reject_suggestion=(
                    "Re-send the manifest as a single YAML string in the "
                    "stdin_data field (with v_args '-f -')."
                ),
            )
        if _uses_kustomize_input(rest):
            # Kustomize channel (probe KUSTO): ``-k <dir>`` makes kubectl
            # BUILD the manifests from a directory the guard cannot see
            # (apply/delete/replace/create all execute the built objects,
            # live-verified on kubectl v1.34.1) — the same invisibility
            # class as ``-f <file>``, one legislation for both. Judged
            # BEFORE the -f branch so ``-k dir -f -`` (kubectl itself
            # rejects the combo today) also lands here: one predicate,
            # every spelling (``-k``, ``-k=dir``, ``-Rk dir``,
            # ``--kustomize dir``).
            return EffectiveTarget(
                scope=SCOPE_BANNED,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.HIGH,
                reject_detail=(
                    f"kubectl {sub} -k reads a kustomization DIRECTORY "
                    "whose built content is not visible to the guard"
                ),
                reject_suggestion=(
                    "Render the kustomization locally (kubectl kustomize "
                    "<dir>) and pass the resulting manifest via stdin_data "
                    "with '-f -', containing only these kinds: "
                    f"{_allowed_manifest_kinds_text()}."
                ),
            )
        if _uses_file_input(rest):
            stdin_data = (raw_args or {}).get("stdin_data", "") if raw_args else ""
            if stdin_data:
                if _stdin_filename_flag(rest):
                    return _classify_kubectl_stdin_manifest(
                        stdin_data,
                        rest,
                        raw_command,
                        sub,
                    )
                # Decoy routing (probe: ``-f /tmp/evil.yaml`` plus a
                # COMPLIANT stdin manifest classified as compliant):
                # kubectl reads the file and ignores stdin, so judging
                # the stdin manifest would bless a call that executes
                # different content.
                return EffectiveTarget(
                    scope=SCOPE_BANNED,
                    namespace="",
                    raw_command=raw_command,
                    confidence=ConfidenceLevel.HIGH,
                    reject_detail=(
                        f"kubectl {sub} carries BOTH '-f <file-or-url>' and "
                        "stdin_data: kubectl reads the file, so the manifest "
                        "the guard can see (stdin_data) is NOT the one that "
                        "gets executed"
                    ),
                    reject_suggestion=(
                        "Re-issue with '-f -' so kubectl reads the manifest "
                        "from stdin_data — then the classified manifest IS "
                        "the executed manifest."
                    ),
                )
            return EffectiveTarget(
                scope=SCOPE_BANNED,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.HIGH,
                reject_detail=(
                    f"kubectl {sub} -f reads a file whose contents are not "
                    "visible to the guard"
                ),
                reject_suggestion=(
                    "Pass the manifest via stdin_data instead, containing only "
                    f"these kinds: {_allowed_manifest_kinds_text()}."
                ),
            )
        # No -f: diagnose before the generic positional fallback, whose
        # "add a <kind>/<name> positional" hint is only true for verbs
        # that accept one. Two shapes have a different REAL cause:
        stdin_data = (raw_args or {}).get("stdin_data", "") if raw_args else ""
        if stdin_data:
            # The caller meant to feed the manifest through stdin but
            # dropped the flag that makes kubectl read stdin.
            return EffectiveTarget(
                scope=SCOPE_UNKNOWN,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.HIGH,
                reject_detail=(
                    f"kubectl {sub} carries a manifest in stdin_data but no "
                    "'-f -' flag, so kubectl would not read stdin and the "
                    "guard cannot see what gets applied"
                ),
                reject_suggestion=(
                    "Add the missing '-f -' flag (e.g. v_args=\"-f -\") so "
                    "kubectl reads the manifest from stdin_data; the guard "
                    "then classifies the manifest itself."
                ),
            )
        if sub in ("apply", "replace"):
            # apply/replace have no positional-resource form at all — the
            # generic hint would steer the model into a command kubectl
            # itself rejects.
            return EffectiveTarget(
                scope=SCOPE_UNKNOWN,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.HIGH,
                reject_detail=(
                    f"kubectl {sub} has no positional-resource form; it "
                    "needs a manifest source, and none was given"
                ),
                reject_suggestion=(
                    'Pass the manifest via stdin_data with v_args="-f -", '
                    "containing only these kinds: "
                    f"{_allowed_manifest_kinds_text()}."
                ),
            )

    # Read-only — no comparison needed.
    if sub in READONLY_KUBECTL_SUBS:
        return EffectiveTarget(
            scope=SCOPE_READONLY,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
        )

    # Any subcommand invoked with -h/--help is a help request — prints
    # usage text and never mutates state.
    if _has_help_flag(rest):
        return EffectiveTarget(
            scope=SCOPE_READONLY,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
        )

    # rollout has both read-only and destructive sub-subs.
    if sub == "rollout":
        if rest and rest[0] in READONLY_ROLLOUT_SUBS:
            return EffectiveTarget(
                scope=SCOPE_READONLY,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.HIGH,
            )
        # rollout restart/undo/pause/resume — destructive, affects a
        # deployment/sts/ds. Classify as destructive with the
        # rollout's target resource.
        return _classify_kubectl_rollout(rest, raw_command)

    # config has both query and write sub-subs.
    if sub == "config":
        if rest and rest[0] in READONLY_CONFIG_SUBS:
            return EffectiveTarget(
                scope=SCOPE_READONLY,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.HIGH,
            )
        # Writes to kubeconfig — banned outright.
        return EffectiveTarget(
            scope=SCOPE_BANNED,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
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
        # Face-4 facts line: the exec branch judges the RAW inner text.
        # Production tool calls carry it as ``v_args`` (``POD -- INNER``);
        # a nested exec recursion receives the peeled inner line from its
        # parent. Synthetic arg shapes (list / command-string) have no raw
        # text to recover — their tokens ARE the argv, no quoting remains
        # to misread — so they stay on the token path under either engine.
        cmdline = _cmdline_raw
        if cmdline is None and raw_args is not None:
            v_args = raw_args.get("v_args")
            if isinstance(v_args, str) and v_args.strip():
                cmdline = v_args
        return _classify_kubectl_exec(rest, raw_command, _cmdline_raw=cmdline)
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
            scope=SCOPE_BANNED,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
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
        # W-55-6: strip the imperative-create subtype positional (``create
        # service clusterip NAME`` / ``create secret tls NAME``) before the
        # generic reader — the subtype is grammar, not the resource name.
        # Removed BY INDEX (the second positional's own slot from the shared
        # ``_iter_positionals`` walk), not by value — ``list.remove(token)``
        # would strip the first string match, so a flag value that happens to
        # equal the subtype (``-n clusterip``) would be removed instead.
        # raw_command is left untouched (audit fidelity).
        _rest = rest
        _pos_idx = list(_iter_positionals(rest))
        if len(_pos_idx) >= 2:
            _subs = _CREATE_SUBTYPES.get(canonicalise_kind(_pos_idx[0][1]))
            if _subs and _pos_idx[1][1].lower() in _subs:
                _rest = list(rest)
                del _rest[_pos_idx[1][0]]
        eff = _classify_kubectl_resource(_rest, raw_command, default_kind=None)
        # Workload kinds are a MECHANISM ban on the IMPERATIVE channel
        # (code review 2026-09-11, probe-verified): imperative create
        # succeeds iff the object is ABSENT — exactly the drill-target
        # staging scenario — yet carries no manifest the shape contract
        # (T1-T5: single doc, one container, no privilege surface, image
        # allow-set, volume kinds) could ever see, and nothing registers
        # the created workload on the cleanup chain. The same
        # create-succeeds-iff-absent logic that justified narrowing the
        # manifest channel to apply/create cuts the other way here: the
        # ONLY compliant staging form is the manifest contract, so the
        # imperative form has no legitimate use. Non-workload kinds
        # (namespace/secret/configmap/quota — the ALLOWED_MANIFEST set's
        # siblings) keep the generic classification below.
        if eff.scope in _IMPERATIVE_WORKLOAD_KINDS:
            return EffectiveTarget(
                scope=SCOPE_BANNED,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.HIGH,
                mechanism_banned=True,
                reject_detail=(
                    f"imperative 'kubectl create {eff.scope}' starts a "
                    f"{eff.scope} whose shape the guard cannot verify "
                    "(image, command, lifetime) and whose cleanup the task "
                    "cannot track"
                ),
                reject_suggestion=(
                    "Stage the drill target in-band via the manifest channel: "
                    "'kubectl apply -f -' with stdin_data under the "
                    "drill-target contract (single Deployment document, "
                    "metadata.name = the approved target name, exactly one "
                    "container under spec.template.spec with no "
                    "initContainers, no host* / privileged / capabilities / "
                    "hostPath, an image from the carrier allow-set, "
                    "persistentVolumeClaim/configMap/secret volumes only) — "
                    "or inject into a workload that already exists."
                ),
            )
        return eff

    if sub == "apply":
        # apply without -f AND without stdin_data (diagnosed above) — a
        # stray positional still gets the generic resource classification
        # so a wrong-but-parseable shape reports the parse result rather
        # than a dead end.
        return _classify_kubectl_resource(rest, raw_command, default_kind=None)

    # Anything else: unknown subcommand → default-deny.
    return EffectiveTarget(
        scope=SCOPE_UNKNOWN,
        namespace="",
        raw_command=raw_command,
        confidence=ConfidenceLevel.UNKNOWN,
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
_BOOLEAN_FLAGS: frozenset[str] = frozenset(
    {
        # Help / verbose
        "-h",
        "--help",
        "-v",
        "--version",
        "-W",
        "--warnings-as-errors",
        "-q",
        "--quiet",
        # All-namespaces / all
        "-A",
        "--all-namespaces",
        "--all",
        # Recursive
        "-R",
        "--recursive",
        # Force / safety
        "--force",
        "--ignore-not-found",
        "--prune",
        "--insecure-skip-tls-verify",
        # Watch
        "-w",
        "--watch",
        "--watch-only",
        # Output formatting
        "--show-labels",
        "--show-kind",
        "--no-headers",
        "--server-side",
        "--client",
        # Misc
        "--include-uninitialized",
        "--keep-annotations",
        "--validate",
        "--save-config",
        "--rm",  # kubectl run --rm (delete on exit)
        "-i",
        "--stdin",
        "-t",
        "--tty",  # kubectl exec / run
        "--allow-missing-template-keys",
        "--overwrite",  # kubectl label / annotate
        "--local",  # kubectl set ... --local
    }
)


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
    explicit per-subcommand ns with a global one. Reads through
    ``iter_flag_assignments`` so combined shorthand bundles
    (``-nprod``) count as carrying a namespace too.

    Stops scanning at the ``--`` separator — anything after it is the
    INNER command of ``kubectl exec`` (or similar) and its ``-n`` would
    bind to the inner program's namespace flag, not kubectl's outer ns.
    Without this stop, a ``kubectl exec POD -- prog -n inner`` call
    would falsely report that the OUTER kubectl carries a namespace,
    suppressing global-ns propagation.
    """
    return any(
        name == "--namespace" for name, _value, _origin in iter_flag_assignments(rest)
    )


def _uses_file_input(args: list[str]) -> bool:
    """Return True if any ``-f`` / ``--filename`` flag is present.

    Reads through ``iter_flag_assignments`` so every pflag spelling
    counts: separated (``-f -``), ``=`` forms, and bundle-interior
    absorption (``-f-`` = stdin, ``-fdir/x.yaml``, ``-Rf x.yaml`` —
    the trailing ``f`` absorbs the next arg).

    Stdin (``-f -``) and URL inputs are indistinguishable from local
    files at this layer — the caller distinguishes stdin via
    ``_stdin_filename_flag``; everything else is banned because the
    content is not in the tool_call arg list.
    """
    return any(
        name == "--filename" for name, _value, _origin in iter_flag_assignments(args)
    )


def _uses_kustomize_input(args: list[str]) -> bool:
    """True if a ``-k`` / ``--kustomize`` flag is present.

    The kustomize channel reads manifests from a DIRECTORY the guard
    cannot see (kustomization.yaml + resources + patches build at
    apply time) — the same invisibility class as ``-f <file>``: any
    identity the guard classified would describe content kubectl does
    not execute. Reads through ``iter_flag_assignments`` so bundled
    (``-Rk``), glued (``-k=dir``) and long (``--kustomize dir``)
    spellings all count (probe KUSTO, kubectl v1.34.1 live:
    apply/delete/replace/create all execute the built objects).
    """
    return any(
        name == "--kustomize"
        for name, _value, _origin in iter_flag_assignments(args)
    )


def _stdin_filename_flag(args: list[str]) -> bool:
    """True when EVERY ``-f`` / ``--filename`` value is exactly ``-``.

    ``--filename`` is a REPEATABLE pflag (StringArray): ``-f - -f
    x.yaml`` makes kubectl apply BOTH the stdin manifest AND the file
    (probe I1c/I1d: dry-run shows both objects created, in either
    flag order). Judging only the FIRST value let the stdin+file mix
    through the stdin channel — the guard would audit the compliant
    stdin manifest while kubectl also executes the invisible file
    content (the P8 decoy class reached through the repetition
    dimension). Every filename value must therefore be ``-``; any
    non-"-" value routes the call to the decoy/file ban instead.
    """
    saw_filename = False
    for name, value, _origin in iter_flag_assignments(args):
        if name == "--filename":
            saw_filename = True
            if value != "-":
                return False
    return saw_filename


def _malformed_stdin_data(raw_args: dict[str, Any] | None) -> bool:
    """True when stdin_data is present but not a string.

    The tool schema declares ``stdin_data: str``, but the model emits
    tool_call arguments as JSON — a manifest can arrive structured
    (dict / list), which would crash the classifier with a TypeError
    inside ``re.findall`` instead of fail-closing (probe I2: a dict
    stdin_data raised straight out of ``infer_effective_target``).
    """
    if raw_args is None or "stdin_data" not in raw_args:
        return False
    return not isinstance(raw_args.get("stdin_data"), str)


def _stdin_manifest_docs(yaml_str: str) -> list[dict[str, Any]] | None:
    """Structurally parse every document of a stdin manifest.

    Text regexes and the YAML/JSON parser disagree on legal spellings:
    a quoted key (``"kind": ClusterRole``) and a whole JSON document
    are invisible to ``^kind:`` yet fully executed by kubectl (probe
    J1/J2b: dry-run created the ConfigMap AND the ClusterRole while
    the guard saw only the ConfigMap); a pure-JSON manifest is legal
    kubectl input the regex could not see at all (probe J2). Parsing
    is the only layer kubectl itself consults, so the guard must
    parse too. Returns None on a YAML error (callers fail-closed);
    non-mapping documents are dropped — kubectl rejects them itself
    (probe J4: 'invalid object to validate').
    """
    try:
        docs = [d for d in yaml.safe_load_all(yaml_str) if d is not None]
    except yaml.YAMLError:
        return None
    return [d for d in docs if isinstance(d, dict)]


def _extract_all_kinds_from_yaml(yaml_str: str) -> list[str]:
    """Extract ALL 'kind' fields from YAML (handles multi-document ``---``)."""
    docs = _stdin_manifest_docs(yaml_str)
    if docs is None:
        return []
    kinds: list[str] = []
    for doc in docs:
        kind = doc.get("kind")
        if isinstance(kind, str) and kind:
            kinds.append(kind)
    return kinds


# Flags that widen a stdin-manifest call's EFFECT beyond the manifest text.
# ``apply --prune`` deletes live resources absent from the manifest
# (officially those created by apply/create --save-config — in a
# declaratively-managed cluster that is nearly everything), ``--all``
# widens the operand set to every resource of the kind, and
# ``-A`` / ``--all-namespaces`` widen across namespaces. The guard's
# visibility boundary IS the manifest text: with one of these flags the
# approved call (create the manifest's objects) and the executed call
# (create AND delete a set the guard never saw) are not the same thing,
# and the deletion bypasses every identity anchor. Third-round review
# finding (2026-09-11, probe-verified P5/P6/P7 — all three stdin
# channels passed ``--prune`` through); user ruling: ban them at the
# SHARED manifest entry so every channel (drill-target contract /
# occupant pod / generic kind whitelist) is covered by one gate. A FORM
# issue, not a mechanism ban — the compliant form exists (drop the flag,
# re-send the same manifest); an explicit ``=false`` off-form is inert
# and passes.


def _stdin_manifest_widening_flag(rest: list[str]) -> str:
    """Return the first range-widening flag token in ``rest``, or "".

    Boolean flags accept an explicit ``=false`` off-form (inert — the
    call's effect stays inside the manifest text; pflag supports
    ``-A=false``, probe-verified). ``--prune-allowlist`` takes a LIST
    value and exists only to steer ``--prune``, so any form of it
    counts. Reads through ``iter_flag_assignments`` so pflag shorthand
    bundles are judged positionally: a capital ``A`` at a shorthand
    position (``-An prod`` = ``-A -n prod``) counts as the
    all-namespaces widening, while an ``A`` inside a VALUE (``-nApp`` =
    namespace "App", ``-lapp=App`` = selector) is inert — which plain
    substring matching cannot tell apart (round-4 probes F1a/F1b/F1d:
    the interim fix false-rejected all three while letting ``-nProd``
    through — same shape, opposite verdicts).
    """
    for name, value, origin in iter_flag_assignments(rest):
        if name == "--prune-allowlist":
            return origin
        if name in ("--prune", "--all", "--all-namespaces"):
            if value is None:
                return origin  # bare flag: pflag defaults it to true
            if str(value).strip().lower() not in ("false", "0", "f"):
                # truthy, or unparseable — kubectl itself rejects
                # unparseable values client-side, so failing closed here
                # costs nothing
                return origin
            # explicit off-form (``-A=false`` / ``--prune=0``): inert
    return ""


def _classify_kubectl_stdin_manifest(
    stdin_data: str,
    rest: list[str],
    raw_command: str,
    sub: str,
) -> EffectiveTarget:
    """Classify ``kubectl <mutating-sub> -f -`` with inline YAML via stdin_data.

    Multi-document safety: ALL ``kind`` values must be in
    ``ALLOWED_MANIFEST_KINDS``. A single non-whitelisted kind causes
    the entire call to be BANNED. FaultDrill documents are additionally
    SINGLE-document (P7, handle integrity: one apply = one CR = one
    recovery handle). ``sub`` additionally gates the drill-target
    Deployment branch (staging channels apply/create only).
    Range-widening flags (--prune / --all / -A / …) are refused at this
    entry — see ``_stdin_manifest_widening_flag``.
    """
    widening = _stdin_manifest_widening_flag(rest)
    if widening:
        return EffectiveTarget(
            scope=SCOPE_BANNED,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
            reject_detail=(
                f"'{widening}' widens the call's effect beyond the manifest "
                "text (--prune deletes live resources absent from the "
                "manifest; --all / -A widen the operand set), and the guard "
                "can only see the manifest — the flag-driven part would "
                "bypass every identity anchor"
            ),
            reject_suggestion=(
                "Re-issue the call WITHOUT the flag, applying the manifest "
                "text as-is; deleting pre-existing resources is a separate, "
                "individually-approved call."
            ),
        )
    kinds = _extract_all_kinds_from_yaml(stdin_data)
    if not kinds:
        return EffectiveTarget(
            scope=SCOPE_BANNED,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
            reject_detail=(
                "the -f/stdin manifest has no recognizable 'kind'; its effect "
                "cannot be verified"
            ),
            reject_suggestion=(
                "Declare an explicit 'kind:' in the manifest. Accepted kinds: "
                f"{_allowed_manifest_kinds_text()}."
            ),
        )
    lower_kinds = [k.lower() for k in kinds]
    if all(k in ALLOWED_MANIFEST_KINDS for k in lower_kinds):
        # Anchor EVERY document, not just the first: kubectl creates every
        # document (probe K1: a two-doc all-whitelisted apply created
        # ok-cm@default AND evil-cm@kube-system while the guard anchored
        # only doc 1 and ALLOWed — doc 2 rode along with no name/ns anchor
        # at all). kubectl resolves each doc's namespace individually
        # (probe K-K1), and rejects an explicit -n that conflicts with a
        # doc's own namespace (probe K-K2/K-K8) — so conflicting doc
        # namespaces can never be anchored by one value: form-issue them,
        # same legislation as the --namespace consistency gate.
        # Kubectl creates every document as its OWN kind while the
        # approval's scope anchors exactly ONE (probe M3: a two-doc
        # ConfigMap+Secret apply under a namespace-wide configmap approval
        # ALLOWed and the dry run created both — the Secret document rode
        # along with no scope anchor at all). Same legislation as the
        # mixed-namespace gate below: no single scope value can anchor a
        # mixed-kind apply, form-issue it. Kind identity is compared
        # lower-cased (kubectl itself rejects non-canonical spellings like
        # ``kind: CONFIGMAP``, probe M5 — the mapper has no such kind — so
        # case-merging is fail-closed on the executor side).
        if len(set(lower_kinds)) > 1:
            return EffectiveTarget(
                scope=SCOPE_BANNED,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.HIGH,
                reject_detail=(
                    "the -f/stdin manifest mixes kinds ("
                    + ", ".join(kinds)
                    + "): kubectl creates every document as its own kind "
                    "while the approval's scope anchors exactly one, so a "
                    "document of an unapproved kind rides along with no "
                    "scope anchor"
                ),
                reject_suggestion=(
                    "Split the manifest into one kind per apply, or keep "
                    "every document the same kind as the approved scope."
                ),
            )
        # FaultDrill documents are SINGLE-document by legislation (P7,
        # second-round adversarial review of faultdrill-cr-channel): the
        # carrier's recovery handle references exactly ONE ns/name
        # (``build_handle_from_messages`` hydrates the first document),
        # so a second FaultDrill document in the same apply injects a
        # second fault whose CR no handle ever references — leaked,
        # unrecoverable through the reconcile path. The general branch
        # anchors every document's NAME (``names`` tuple), but the
        # faultdrill handle layer cannot consume a tuple; same
        # single-document shape the pod-occupant and drill-target
        # contracts legislate below.
        if lower_kinds[0] == "faultdrill" and len(kinds) > 1:
            return EffectiveTarget(
                scope=SCOPE_BANNED,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.HIGH,
                reject_detail=(
                    "the -f/stdin manifest carries more than one FaultDrill "
                    "document: each CR injects its OWN fault, but the "
                    "channel's recovery handle references exactly one "
                    "ns/name — a second CR's fault would leak with no "
                    "recovery path"
                ),
                reject_suggestion=(
                    "Apply one FaultDrill CR per apply call; carry the "
                    "second fault in a separate, individually-approved "
                    "apply."
                ),
            )
        docs = _stdin_manifest_docs(stdin_data) or []
        names: list[str] = []
        doc_namespaces: list[str] = []
        doc_labels: list[dict[str, str]] = []
        for d in docs:
            meta = d.get("metadata") if isinstance(d, dict) else None
            meta = meta if isinstance(meta, dict) else {}
            n = meta.get("name")
            if isinstance(n, str) and n:
                names.append(n)
            ns = meta.get("namespace")
            if isinstance(ns, str) and ns:
                doc_namespaces.append(ns)
            raw_labels = meta.get("labels")
            if isinstance(raw_labels, dict):
                doc_labels.append({str(k): str(v) for k, v in raw_labels.items()})
            else:
                doc_labels.append({})
        namespace = parse_namespace(rest, default="")
        if not namespace:
            if len(set(doc_namespaces)) > 1:
                return EffectiveTarget(
                    scope=SCOPE_BANNED,
                    namespace="",
                    raw_command=raw_command,
                    confidence=ConfidenceLevel.HIGH,
                    reject_detail=(
                        "the -f/stdin manifest mixes namespaces ("
                        + ", ".join(doc_namespaces)
                        + "): kubectl creates each document in its own "
                        "namespace, so no single namespace can be anchored "
                        "for the drift check"
                    ),
                    reject_suggestion=(
                        "Split the manifest into one namespace per apply, or "
                        "align every document's metadata.namespace."
                    ),
                )
            # Explicit/implicit mix (probe NS-MIX): a doc WITHOUT a
            # namespace lands in "default" while its explicit siblings
            # land in their own — kubectl writes two namespaces, but
            # doc_namespaces only records the explicit ones, so the
            # anchor below would over-claim the implicit docs into the
            # explicit namespace and the per-name reconciliation would
            # happily pass an in-contract check for an out-of-contract
            # (default) write. Same legislation as the mixed-namespace
            # gate above: form-issue it. A manifest whose explicit
            # namespaces are ALL "default" stays legal — the implicit
            # docs land in "default" too, so one value still anchors.
            implicit_docs = len(docs) - len(doc_namespaces)
            if (
                doc_namespaces
                and implicit_docs
                and set(doc_namespaces) != {"default"}
            ):
                return EffectiveTarget(
                    scope=SCOPE_BANNED,
                    namespace="",
                    raw_command=raw_command,
                    confidence=ConfidenceLevel.HIGH,
                    reject_detail=(
                        "the -f/stdin manifest mixes documents with an "
                        "explicit namespace ("
                        + ", ".join(doc_namespaces)
                        + ") and documents without one: kubectl creates "
                        "the namespace-less documents in \"default\" while "
                        "the explicit ones land elsewhere, so no single "
                        "namespace can be anchored for the drift check"
                    ),
                    reject_suggestion=(
                        "Give every document the same metadata.namespace, or "
                        "pass --namespace explicitly so the namespace-less "
                        "documents inherit it."
                    ),
                )
            namespace = doc_namespaces[0] if doc_namespaces else ""
        # Labels anchor (probe M4): this branch never extracted manifest
        # labels, so a label-only approval rejected a fully-compliant
        # whitelisted apply while the SAME shape on the deployment
        # contract branch passed — two branches of one channel, two
        # verdicts. Multi-document semantics: the INTERSECTION of every
        # document's labels, so the guard's superset check
        # (effective ⊇ approved) passes exactly when EVERY created
        # object carries the approved labels — a document without them
        # empties the intersection and the call stays unanchored
        # (fail-closed), rather than one labelled document vouching for
        # an unlabelled sibling (fail-open). Single document: the
        # intersection is that document's labels, matching the
        # deployment branch.
        labels: dict[str, str] = dict(doc_labels[0]) if doc_labels else {}
        for other in doc_labels[1:]:
            labels = {k: v for k, v in labels.items() if other.get(k) == v}
        return EffectiveTarget(
            scope=canonicalise_kind(kinds[0]),
            namespace=namespace,
            names=tuple(names),
            labels=labels,
            confidence=ConfidenceLevel.HIGH,
            raw_command=raw_command,
        )
    # Drill occupancy vehicle: a SINGLE Pod document may pass when it
    # satisfies the occupant contract (behaviourless sleep pod holding a PVC).
    # Multi-document manifests mixing a Pod with anything else stay refused —
    # the contract must see the whole effect, and a side document hides it.
    if lower_kinds == ["pod"]:
        return _classify_vehicle_pod_manifest(stdin_data, rest, raw_command)
    # Drill target: a SINGLE Deployment document may pass when it satisfies
    # the drill-target contract — the victim workload the task stages in-band
    # when the approved target does not exist yet. Multi-document manifests
    # stay refused (a side document hides part of the effect from the
    # contract), same single-document policy as the occupant branch above.
    # The staging channels are apply/create ONLY (code review 2026-09-11,
    # probe-verified): kubectl replace 404s on an absent object, so it can
    # never stage a new target — through the manifest channel it admits only
    # re-shaping a PRE-EXISTING deployment, which would register it as
    # task-owned and put a persistent workload on the cleanup (delete) chain;
    # delete/patch/set/edit -f serve no staging purpose either and keep the
    # standing workload-kind mechanism ban. The pod occupant branch keeps the
    # wider dispatch: an occupant is a bounded-lifetime disposable vehicle
    # (activeDeadlineSeconds self-expiry), so tracking-and-cleaning any
    # occupant-shaped execution is the correct outcome there.
    if lower_kinds == ["deployment"] and sub in ("apply", "create"):
        return _classify_drill_target_manifest(stdin_data, rest, raw_command)
    if "pod" in lower_kinds:
        return EffectiveTarget(
            scope=SCOPE_BANNED,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
            reject_detail=(
                "the manifest mixes a Pod with other documents "
                f"({', '.join(kinds)}); an occupant pod must be applied alone"
            ),
            reject_suggestion=(
                "Apply the occupant pod as the ONLY document in stdin_data; "
                "apply any whitelisted resources "
                f"({_allowed_manifest_kinds_text()}) in separate calls."
            ),
        )
    # Reshape distinction (inject-cc2d5080): a manifest whose EVERY kind is
    # carried by the imperative create channel (the recovery-carrier RBAC
    # family, :data:`_RECOVERY_CARRIER_CREATE_KINDS`) is a FORM rejection,
    # not a mechanism ban — the same objects pass the guard as separate
    # ``kubectl create sa <name>`` calls, so the model must be told that
    # reshape exists. In that task the mislabel rendered "no reshape of
    # this call will pass", which steered the model off its approved
    # carrier-stacking path and into a forced un-armed injection.
    from chaos_agent.agent.execution_artifacts import (
        _RECOVERY_CARRIER_CREATE_KINDS,
    )

    if lower_kinds and all(
        k in _RECOVERY_CARRIER_CREATE_KINDS for k in lower_kinds
    ):
        return EffectiveTarget(
            scope=SCOPE_BANNED,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
            reject_detail=(
                "the manifest contains resource kinds the manifest channel "
                f"does not whitelist ({', '.join(kinds)})"
            ),
            reject_suggestion=(
                "The manifest whitelist covers only "
                f"{_allowed_manifest_kinds_text()}; RBAC objects travel on "
                "the IMPERATIVE channel instead — re-issue each object as "
                "its own call ('kubectl create sa <name> -n <ns>', "
                "'kubectl create clusterrole <name> ...'), one object per "
                "call."
            ),
        )
    return EffectiveTarget(
        scope=SCOPE_BANNED,
        namespace="",
        raw_command=raw_command,
        confidence=ConfidenceLevel.HIGH,
        # A workload kind is a MECHANISM ban, not a reshape-and-retry: no
        # compliant form of this apply passes, so "adjust and retry" would
        # only loop the model through doomed variants. The screener turns
        # this into replan guidance.
        mechanism_banned=True,
        reject_detail=(
            "the manifest contains a non-whitelisted resource kind "
            f"({', '.join(kinds)})"
        ),
        reject_suggestion=(
            f"Accepted kinds: {_allowed_manifest_kinds_text()} — plus a "
            "single-document Deployment under the drill-target contract "
            "(metadata.name = the approved target name, exactly one "
            "container under spec.template.spec with no initContainers, no "
            "host* / privileged / capabilities / hostPath, an image from "
            "the carrier allow-set, persistentVolumeClaim/configMap/secret "
            "volumes only), applied alone. Other workload kinds (DaemonSet / "
            "StatefulSet / Job / …) start containers whose blast radius the "
            "guard cannot scope — inject into a workload that already "
            "exists instead of creating one."
        ),
    )


# ---------------------------------------------------------------------------
# Drill occupancy vehicle contract (manifest channel)
#
# A resource-occupancy drill reproduces faults whose mechanism is "the target
# cannot acquire a scarce resource": the canonical case is an RWO cloud disk
# already attached elsewhere — a re-created target pod stalls in
# ContainerCreating on the multi-attach conflict. The only way to stage that
# is a transient pod that CLAIMS the same PVC, so the blanket workload-create
# ban carves out exactly that shape, and nothing else. The contract makes the
# exception verifiable from the manifest alone:
#
#   C1 behaviourless — every container command is a pure keep-alive sleep and
#      there are no initContainers: the occupant holds the resource and does
#      nothing with it;
#   C2 no privilege surface — no hostNetwork/hostPID/hostIPC, no privileged
#      or capabilities;
#   C3 volumes — ONLY persistentVolumeClaim volumes, at least one (the
#      occupancy target); nothing else mounts;
#   C4 bounded lifetime — ``activeDeadlineSeconds`` in (0, 3600]: the pod
#      self-destructs even if the task dies before recovery runs.
#
# Deliberately NO drill-marker label constraint: the drill must be
# indistinguishable from a real incident, and a recognisable marker label on
# the occupant gives the game away to anyone inspecting the cluster. Vehicle
# identity is tracked TASK-side (execution_artifacts registration + the
# screener's exemption/cleanup), never via a cluster-visible label.
#
# A contract violation is a FORM issue (mechanism_banned=False): a compliant
# occupant DOES exist, and the rejection lists exactly which constraints to
# fix. That is what keeps the model reshaping toward the contract instead of
# looping through doomed variants (task-190c94e8).
# ---------------------------------------------------------------------------

_MAX_OCCUPANT_DEADLINE_SECONDS = 3600
_SHELL_SLEEP_RE = re.compile(r"^(?:exec\s+)?sleep(?:\s+[1-9][0-9]*)?$")


def _is_sleep_only_command(command: Any) -> bool:
    """Whether a manifest container ``command`` is a pure keep-alive sleep.

    Modelled on ``tools.kubectl._is_keepalive_sleep`` (re-declared here
    rather than imported — the guard layer does not depend on tools), but
    STRICTER: a bare ``sleep`` with no duration and ``sh -c`` wrapping a
    bare ``sleep`` are refused here, because an occupant must bound its own
    runtime in addition to ``activeDeadlineSeconds``. Accepted: ``sleep N``
    (N a positive integer), an absolute-path sleep binary with the same
    argument shape, and a shell wrapping NOTHING BUT ``sleep N``. Anything
    composite is behaviour and fails the contract.
    """
    if not isinstance(command, (list, tuple)) or not command:
        return False
    cmd = [str(c) for c in command]
    base = cmd[0].rsplit("/", 1)[-1]
    if base == "sleep":
        if len(cmd) == 1:
            return True
        return len(cmd) == 2 and re.fullmatch(r"[1-9][0-9]*", cmd[1]) is not None
    if base in ("sh", "bash") and "-c" in cmd:
        idx = cmd.index("-c")
        script = cmd[idx + 1].strip() if idx + 1 < len(cmd) else ""
        return bool(_SHELL_SLEEP_RE.fullmatch(script))
    return False


def _occupant_contract_violations(doc: dict) -> list[str]:
    """Validate one parsed Pod document against the occupant contract.

    Returns the list of violated constraints (empty = compliant). Every
    entry names the constraint and the fix, so the rejection text can be
    surfaced verbatim as the actionable suggestion.
    """
    violations: list[str] = []
    meta = doc.get("metadata") or {}
    spec = doc.get("spec") or {}
    if not isinstance(meta, dict):
        meta = {}
    if not isinstance(spec, dict) or not spec:
        return ["the pod has no spec — declare spec.containers with a sleep command"]

    # Explicit identity: the vehicle is tracked TASK-side by this exact name
    # (registration, drift exemption, cleanup all key on it). A missing name or
    # generateName would leave the created pod untrackable — and uncleanable.
    if not str(meta.get("name") or ""):
        violations.append(
            "metadata.name must be declared explicitly (generateName is not "
            "allowed — the occupant's identity must be fixed up front)"
        )

    # C4 bounded lifetime
    deadline = spec.get("activeDeadlineSeconds")
    if (
        not isinstance(deadline, int)
        or isinstance(deadline, bool)
        or not 0 < deadline <= _MAX_OCCUPANT_DEADLINE_SECONDS
    ):
        violations.append(
            f"spec.activeDeadlineSeconds must be an integer in "
            f"(0, {_MAX_OCCUPANT_DEADLINE_SECONDS}] so the occupant "
            "self-destructs even if recovery never runs"
        )

    # C1 behaviourless
    if spec.get("initContainers"):
        violations.append("initContainers are not allowed on an occupant pod")
    containers = spec.get("containers")
    if not isinstance(containers, list) or not containers:
        violations.append("spec.containers must declare at least one container")
        containers = []
    for i, c in enumerate(containers):
        cname = str(c.get("name") or f"#{i}") if isinstance(c, dict) else f"#{i}"
        if not isinstance(c, dict):
            violations.append(f"container {cname} is not a mapping")
            continue
        if not _is_sleep_only_command(c.get("command")):
            violations.append(
                f"container '{cname}' command must be a pure keep-alive sleep, "
                'e.g. command: ["sleep", "3600"]'
            )
        if c.get("args"):
            violations.append(f"container '{cname}' must not declare args")
        # C2 privilege surface (container level)
        sec = c.get("securityContext") or {}
        if isinstance(sec, dict) and (sec.get("privileged") or sec.get("capabilities")):
            violations.append(
                f"container '{cname}' securityContext must not set "
                "privileged or capabilities"
            )

    # C2 privilege surface (pod level)
    for flag in ("hostNetwork", "hostPID", "hostIPC"):
        if spec.get(flag):
            violations.append(f"spec.{flag} must not be set on an occupant pod")

    # C3 volumes — PVC only, at least one
    volumes = spec.get("volumes")
    pvc_count = 0
    for v in volumes if isinstance(volumes, list) else []:
        vname = str(v.get("name") or "?") if isinstance(v, dict) else "?"
        if not isinstance(v, dict):
            violations.append(
                f"volume '{vname}' must be a persistentVolumeClaim volume — occupants mount nothing else"
            )
            continue
        pvc = v.get("persistentVolumeClaim")
        if isinstance(pvc, dict) and str(pvc.get("claimName") or ""):
            pvc_count += 1
        elif isinstance(pvc, dict):
            violations.append(
                f"volume '{vname}' persistentVolumeClaim must declare claimName "
                "(an unnamed claim cannot be anchored against the approval)"
            )
        else:
            violations.append(
                f"volume '{vname}' must be a persistentVolumeClaim volume — "
                "occupants mount nothing else"
            )
    if not pvc_count:
        violations.append(
            "the occupant must claim at least one persistentVolumeClaim volume "
            "(the occupancy target)"
        )
    return violations


def _extract_occupant_claims(spec: dict) -> tuple[str, ...]:
    """PVC claim names referenced by an occupant pod spec."""
    claims: set[str] = set()
    volumes = spec.get("volumes")
    for v in volumes if isinstance(volumes, list) else []:
        if not isinstance(v, dict):
            continue
        pvc = v.get("persistentVolumeClaim")
        if isinstance(pvc, dict) and pvc.get("claimName"):
            claims.add(str(pvc["claimName"]))
    return tuple(sorted(claims))


def _classify_vehicle_pod_manifest(
    stdin_data: str,
    rest: list[str],
    raw_command: str,
) -> EffectiveTarget:
    """Classify a single-Pod apply against the occupant contract.

    Compliant manifests classify as a normal scope=pod creation PLUS
    ``is_vehicle_manifest`` / ``occupant_claims`` — the screener validates
    the claims against the frozen approval and registers the occupant as a
    task vehicle. Contract violations are a reshapeable form issue.
    """
    try:
        docs = [d for d in yaml.safe_load_all(stdin_data) if d]
    except yaml.YAMLError as exc:
        return EffectiveTarget(
            scope=SCOPE_BANNED,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
            reject_detail=f"the occupant manifest does not parse as YAML: {exc}",
            reject_suggestion="Fix the YAML syntax and re-apply the occupant pod.",
        )
    # Single-document policy: the contract must see the WHOLE effect. A side
    # document — even one with no ``kind:`` that the kinds regex cannot see —
    # hides part of the apply from this check.
    if len(docs) != 1:
        return EffectiveTarget(
            scope=SCOPE_BANNED,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
            reject_detail=(
                f"the occupant manifest carries {len(docs)} documents; an "
                "occupant pod must be applied alone"
            ),
            reject_suggestion="Apply the occupant pod as the ONLY document in stdin_data.",
        )
    doc = docs[0] if docs else {}
    if not isinstance(doc, dict):
        doc = {}
    violations = _occupant_contract_violations(doc)
    if violations:
        return EffectiveTarget(
            scope=SCOPE_BANNED,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
            reject_detail=(
                "the occupant pod violates the drill vehicle contract: "
                + "; ".join(violations)
            ),
            # B48 (case #33, pending): the occupant contract is the ONLY
            # Pod-creation carve-out, so observation / probe carriers for
            # non-occupancy drills have no legal create path, and this
            # suggestion ("re-apply the SAME manifest") invites compliance
            # toward PVC-holding even when the drill is not resource
            # occupancy. A scenario-diversion sentence was tried and
            # reverted (2026-09-10, user ruling: point patch, not a general
            # solution). The general fix is a second exemption shape
            # (PVC-less, bounded-lifetime carrier) — awaiting a real-drill
            # need before expanding the write set.
            reject_suggestion=(
                "A behaviourless occupant pod IS permitted — fix the listed "
                "constraints and re-apply the SAME manifest: sleep-only "
                "command, no initContainers, no host* / privileged / "
                "capabilities, only persistentVolumeClaim volumes, and "
                "activeDeadlineSeconds <= 3600."
            ),
        )
    meta = doc.get("metadata") or {}
    spec = doc.get("spec") or {}
    name = str(meta.get("name") or "")
    namespace = parse_namespace(rest, default="")
    if not namespace:
        namespace = str(meta.get("namespace") or "")
    labels: dict[str, str] = {}
    raw_labels = meta.get("labels")
    if isinstance(raw_labels, dict):
        labels = {str(k): str(v) for k, v in raw_labels.items()}
    return EffectiveTarget(
        scope="pod",
        namespace=namespace,
        names=(name,) if name else (),
        labels=labels,
        confidence=ConfidenceLevel.HIGH,
        raw_command=raw_command,
        is_vehicle_manifest=True,
        occupant_claims=_extract_occupant_claims(spec),
    )


# ---------------------------------------------------------------------------
# Drill-target contract (manifest channel)
#
# A drill whose approved target does not exist yet (deleted out-of-band, or
# never staged) needs the task to CREATE the victim workload itself — case
# #38: the dedicated PVC-mounting target was gone and the first planning
# round looped 30 minutes against the blanket workload-create ban. The
# carve-out mirrors the occupant contract's verifiable-from-the-manifest
# discipline, with the deployment-appropriate differences:
#
#   T2 one container (no initContainers) — the container surface, and so
#      the blast radius, stays enumerable; unlike an occupant the command
#      is NOT constrained (a victim workload may do real work — that is
#      what gets faulted);
#   T3 no privilege surface — host* flags, privileged/capabilities
#      securityContext, hostPath volumes;
#   T4 image ∈ the carrier allow-set (configured ∪ auto-discovered);
#   T5 volumes limited to persistentVolumeClaim/configMap/secret — data
#      volumes only (user ruling 2026-09-11: all three kinds, once —
#      configMap- and secret-mounting targets are real workload shapes and
#      PVC-only would make the victim unrealistic).
#
# Identity is NOT part of this gate: the manifest's metadata.name must be
# declared, but whether it equals the approved target name is the
# SCREENER's ordinary drift question — the drill target's name IS the
# approved identity (unlike an occupant's generated name, which can never
# match). Deployment has no activeDeadlineSeconds, so lifetime is governed
# task-side: the screener registers the ALLOW as an ``occupant_deployment``
# vehicle artifact whose cleanup chain deletes it.
#
# NOTE the path trap: unlike a Pod manifest (fields at spec.* directly), a
# Deployment keeps the whole container surface inside spec.template.spec —
# a checker that reads the pod-level paths instead silently misses every
# constraint below.
# ---------------------------------------------------------------------------

_DRILL_TARGET_VOLUME_KINDS: frozenset[str] = frozenset(
    {"persistentVolumeClaim", "configMap", "secret"}
)


def _drill_target_violations(doc: dict) -> list[str]:
    """Validate one parsed Deployment document against the drill-target contract.

    Returns the list of violated constraints (empty = compliant). Every
    entry names the constraint and the fix, so the rejection text can be
    surfaced verbatim as the actionable suggestion.
    """
    violations: list[str] = []
    meta = doc.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}

    # Explicit identity: the manifest name is what the screener anchors the
    # ALLOW on (it must equal the approved target name) and what the cleanup
    # chain deletes. generateName would leave the workload untrackable — and
    # uncleanable.
    if not str(meta.get("name") or ""):
        violations.append(
            "metadata.name must be declared explicitly (generateName is not "
            "allowed — the drill target's identity must be fixed up front)"
        )

    spec = doc.get("spec") or {}
    if not isinstance(spec, dict):
        spec = {}
    template = spec.get("template") or {}
    if not isinstance(template, dict):
        template = {}
    pod_spec = template.get("spec") or {}
    if not isinstance(pod_spec, dict) or not pod_spec:
        violations.append(
            "spec.template.spec must be declared — a Deployment keeps its "
            "container, volumes and host flags under spec.template.spec (the "
            "pod template), not under spec directly"
        )
        pod_spec = pod_spec if isinstance(pod_spec, dict) else {}

    # T2 — exactly one container and no initContainers.
    if pod_spec.get("initContainers"):
        violations.append(
            "spec.template.spec.initContainers are not allowed on a drill "
            "target — the container surface must be exactly one container"
        )
    containers = pod_spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        violations.append(
            "spec.template.spec.containers must declare exactly one container"
        )
        containers = []
    for i, c in enumerate(containers):
        cname = str(c.get("name") or f"#{i}") if isinstance(c, dict) else f"#{i}"
        if not isinstance(c, dict):
            violations.append(f"container {cname} is not a mapping")
            continue
        # T3 container-level privilege surface
        sec = c.get("securityContext") or {}
        if isinstance(sec, dict) and (sec.get("privileged") or sec.get("capabilities")):
            violations.append(
                f"container '{cname}' securityContext must not set privileged "
                "or capabilities"
            )
        # T4 image allow-set — same set as recovery carriers (configured ∪
        # auto-discovered healthy DaemonSet images).
        image = str(c.get("image") or "")
        if image not in _recovery_carrier_allowed_images():
            violations.append(_recovery_carrier_image_hint(image))

    # T3 pod-level privilege surface
    for flag in ("hostNetwork", "hostPID", "hostIPC"):
        if pod_spec.get(flag):
            violations.append(
                f"spec.template.spec.{flag} must not be set on a drill target"
            )

    # T3/T5 volumes — hostPath is a privilege surface; the data-volume
    # kinds are persistentVolumeClaim / configMap / secret. No minimum
    # count: a drill target does not need any volume.
    volumes = pod_spec.get("volumes")
    for v in volumes if isinstance(volumes, list) else []:
        vname = str(v.get("name") or "?") if isinstance(v, dict) else "?"
        if not isinstance(v, dict):
            violations.append(f"volume '{vname}' is not a mapping")
            continue
        if v.get("hostPath") is not None:
            violations.append(
                f"volume '{vname}' must not be a hostPath volume — hostPath "
                "is a host privilege surface"
            )
            continue
        if not any(v.get(k) is not None for k in _DRILL_TARGET_VOLUME_KINDS):
            violations.append(
                f"volume '{vname}' must be a persistentVolumeClaim, configMap "
                "or secret volume — drill targets mount nothing else"
            )
    return violations


def _classify_drill_target_manifest(
    stdin_data: str,
    rest: list[str],
    raw_command: str,
) -> EffectiveTarget:
    """Classify a single-Deployment apply/create against the drill-target
    contract.

    A compliant manifest classifies as a normal scope=deployment creation
    PLUS ``is_drill_target_manifest`` — the screener anchors it on the
    approved target name through the ORDINARY drift net and registers the
    ALLOW as an ``occupant_deployment`` vehicle artifact. Contract
    violations are a reshapeable form issue.
    """
    try:
        docs = [d for d in yaml.safe_load_all(stdin_data) if d]
    except yaml.YAMLError as exc:
        return EffectiveTarget(
            scope=SCOPE_BANNED,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
            reject_detail=(
                f"the drill-target manifest does not parse as YAML: {exc}"
            ),
            reject_suggestion=(
                "Fix the YAML syntax and re-apply the drill-target deployment."
            ),
        )
    # Single-document policy: the contract must see the WHOLE effect. A side
    # document — even one with no ``kind:`` that the kinds regex cannot see —
    # hides part of the apply from this check. (The dispatcher's
    # ``lower_kinds == ["deployment"]`` already implies a single document;
    # this check stays as defence-in-depth, same as the occupant branch.)
    if len(docs) != 1:
        return EffectiveTarget(
            scope=SCOPE_BANNED,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
            reject_detail=(
                f"the drill-target manifest carries {len(docs)} documents; a "
                "drill-target deployment must be applied alone"
            ),
            reject_suggestion=(
                "Apply the drill-target deployment as the ONLY document in "
                "stdin_data."
            ),
        )
    doc = docs[0] if docs else {}
    if not isinstance(doc, dict):
        doc = {}
    violations = _drill_target_violations(doc)
    if violations:
        return EffectiveTarget(
            scope=SCOPE_BANNED,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
            reject_detail=(
                "the drill-target deployment violates the drill target "
                "contract: " + "; ".join(violations)
            ),
            reject_suggestion=(
                "A dedicated drill-target Deployment IS permitted in-band — "
                "fix the listed constraints and re-apply the SAME manifest: "
                "metadata.name equal to the approved target name, exactly one "
                "container under spec.template.spec (no initContainers), no "
                "host* / privileged / capabilities / hostPath, an image from "
                "the carrier allow-set, and only persistentVolumeClaim / "
                "configMap / secret volumes."
            ),
        )
    meta = doc.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    name = str(meta.get("name") or "")
    namespace = parse_namespace(rest, default="")
    if not namespace:
        namespace = str(meta.get("namespace") or "")
    labels: dict[str, str] = {}
    raw_labels = meta.get("labels")
    if isinstance(raw_labels, dict):
        labels = {str(k): str(v) for k, v in raw_labels.items()}
    return EffectiveTarget(
        scope="deployment",
        namespace=namespace,
        names=(name,) if name else (),
        labels=labels,
        confidence=ConfidenceLevel.HIGH,
        raw_command=raw_command,
        is_drill_target_manifest=True,
    )


# ---------------------------------------------------------------------------
# kubectl exec — recursive into inner command
# ---------------------------------------------------------------------------


def _peek_escape_tokens_facts(cmdline_raw: str) -> list[str] | None:
    """Facts peel for the escape-primitive peek (design 4.6, face 4).

    Returns the inner command's first-segment argv rendered with
    ``word_token`` — one ``sh -c`` layer unwrapped via the loose
    peek-routing variant — or ``None`` when no confident peel exists, in
    which case the caller keeps the unpeeled tokens (the legacy
    ``except ValueError`` fallback semantics, preserved).

    The peek is ROUTING, not judging: peel uncertainty fails open here
    (keep the unpeeled tokens) because the unpeeled shape is then ruled
    on by the read-only judge below, which fails closed. Only the first
    segment is rendered — a multi-segment inner is rejected by the
    read-only judge on either engine, so peek detail past segment one is
    inert.
    """
    from chaos_agent.bashfacts import (
        Budget,
        ScriptFacts,
        parse_script,
        unwrap_shell_invocation,
        word_token,
    )
    from chaos_agent.tools._readonly_facts import inner_raw_after_double_dash

    inner_raw, _ = inner_raw_after_double_dash(cmdline_raw)
    if inner_raw is None or not inner_raw.strip():
        return None
    budget = Budget()
    root = parse_script(inner_raw, budget=budget)
    if root.errors or not root.segments:
        return None
    first = root.segments[0].command
    if isinstance(first, ScriptFacts):
        return None
    words = ([first.name] if first.name is not None else []) + list(first.args)
    nested = unwrap_shell_invocation(words, budget=budget)
    if nested is not None:
        if nested.errors or not nested.segments:
            return None
        nested_first = nested.segments[0].command
        if isinstance(nested_first, ScriptFacts):
            return None
        words = ([nested_first.name] if nested_first.name is not None else []) + list(
            nested_first.args
        )
    if not words:
        return None
    return [word_token(w) for w in words]


def _fault_binary_in_payload_segments(
    facts_line: str | None, inner: list[str],
) -> bool:
    """True when any command SEGMENT's head is a fault binary (R25/G-9).

    The escape probe in ``_classify_kubectl_exec`` is a single-layer
    peek at the FIRST command's head: a compound payload whose head is
    innocent (``sh -c 'blade destroy x; stress-ng'``, ``cat f; iptables
    -A``) hid every fault binary past the ``;``, so the branch's
    ``fault_binary_mutation`` flag never fired and the vehicle/
    machinery exemptions (both of which withhold on that flag) would
    swallow a real fault binary — the R22 declaration domain (“a fault
    binary inside the carrier keeps identity review”) silently diverged
    from the implementation's traversal domain.

    The judgement reuses the carriers-shared segment parser
    (``exec_command_segments`` — the same single source the blade
    carrier's own payload classifier rides): the raw inner line when
    facts text exists, else the delivered payload argv (the parser's
    token form — same separator/wrapper/script expansion). The
    ``_FAULT_BINARIES`` set is the one the head path already consults.
    """
    from chaos_agent.agent.providers.message_scanning import (
        exec_command_segments,
    )

    command: object = facts_line if facts_line is not None else inner
    return any(
        seg and seg[0].rsplit("/", 1)[-1] in _FAULT_BINARIES
        for seg in exec_command_segments(command)
    )


#: Host-escape primitives, basename form — the same trio the head-only
#: escape probe below legislates (R26/G-10 segments the check the same
#: way G-9 segmented the fault-binary one).
_ESCAPE_PRIMITIVES = frozenset({"nsenter", "chroot", "unshare"})


def _escape_primitive_in_payload_segments(
    facts_line: str | None, inner: list[str],
) -> str | None:
    """The escape primitive heading any command SEGMENT (R26/G-10).

    The escape branch's single-layer peek reads only the FIRST
    command's head, so a payload with an innocent head and an escape
    primitive riding past a ``;``/``&&`` (``sh -c 'cat /etc/hosts;
    nsenter -t 1 -m sh'``) never reached the SCOPE_ESCAPE legislation:
    it classified as a plain pod mutation, and when the exec target IS
    the approved pod the identity match passed the whole chain
    end-to-end (screener probe: route=pass for the compound while every
    direct/wrapped form was REJECT_BANNED) — the branch's own comment
    promise ("a single ``sh -c`` wrapper must not hide the escape
    primitive") was already broken by the compound form. Same parser,
    same single source as the G-9 fault-binary twin, but the
    chroot-KEEPING projection (``chroot_delegation=False``): the
    default "what actually runs" delegation (``chroot /host bash`` →
    ``[bash]``) would erase the very primitive being legislated from
    a delegated tail segment.

    Returns the CAUGHT primitive's name (R27/G-11c) so the reject
    message can name the primitive the legislation actually caught —
    the head-only probe names the innocent head ('cat') for a compound
    payload, and the message is the model's repair guidance.
    """
    from chaos_agent.agent.providers.message_scanning import (
        exec_command_segments,
    )

    command: object = facts_line if facts_line is not None else inner
    for seg in exec_command_segments(command, chroot_delegation=False):
        if seg:
            head = seg[0].rsplit("/", 1)[-1]
            if head in _ESCAPE_PRIMITIVES:
                return head
    return None


def _classify_kubectl_exec(
    args: list[str],
    raw_command: str,
    *,
    _cmdline_raw: str | None = None,
) -> EffectiveTarget:
    """Classify ``kubectl exec POD [-c CONTAINER] [-n NS] -- INNER``.

    The effective target of an exec is whatever INNER would act on.
    For most shell commands this is "the pod itself" (scope=pod).
    For nested blade or kubectl calls we recurse.

    ``_cmdline_raw``: raw text of THIS level's command line — the call's
    ``v_args`` at top level, or the peeled inner line threaded down by
    the parent exec recursion. ``None`` (synthetic arg shapes carry no
    raw text) → the token fallback runs instead.
    """
    ns = parse_namespace(args, default="default")
    pod_name = _first_positional(args)
    if not pod_name:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                "kubectl exec names no pod — the guard cannot tell WHICH pod "
                "would be entered, so it cannot compare it to the approved one"
            ),
            reject_suggestion=_FIX_NAME_THE_TARGET,
        )

    # R45: the separator must be pflag's own and the entry stretch may hold
    # exactly ONE positional. ``args.index("--")`` (the legacy slice) is
    # blind to a ``--`` that a value-taking flag swallowed as its value, and
    # it never asked what else sits before the boundary. The shared
    # flag-aware walk decides both questions so this face and the facts
    # judge cannot drift apart.
    from chaos_agent.tools._readonly_facts import exec_separator_shape

    positionals, after = exec_separator_shape(args)
    if after is not None and len(positionals) > 1:
        extras = positionals[1:]
        shown = " ".join(extras[:8]) + (" ..." if len(extras) > 8 else "")
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                f"the exec entry is followed by extra tokens ('{shown}') "
                "before the '--' separator — kubectl reads only the FIRST "
                "positional as the entry, and what happens to the rest is "
                "client-dependent (current clients drop them, older ones run "
                "them as the command head), so the guard cannot tell what "
                "this call would do"
            ),
            reject_suggestion=_FIX_EXEC_ONE_ENTRY,
        )
    inner = list(after) if after is not None else []
    if not inner:
        # No ``--`` at all is TWO shapes, not one (R44). ``POD [flags]``
        # — bare entry / attach — runs nothing: it acts on the pod, and
        # vehicle identity is decided DATA-side by the screener (state +
        # live discovery), never from the pod name here. ``POD COMMAND``
        # instead CARRIES a command that this entry-only reading never
        # handed to any judge: kubectl refuses the separator-less form
        # outright, so the call cannot run — and reading it as "attach"
        # would name a call whose command is invisible ("the shell did
        # not run it" is the CLIENT's behaviour, not this judge's
        # guarantee). The shared walker (single source, the same one the
        # read-only judge uses at the tool layer) names the trailing
        # command; refuse with the fix path.
        from chaos_agent.tools._readonly_facts import (
            exec_command_without_double_dash,
        )

        trailing = exec_command_without_double_dash(args)
        if trailing is not None:
            shown = " ".join(trailing[:8]) + (" ..." if len(trailing) > 8 else "")
            return EffectiveTarget(
                scope=SCOPE_UNKNOWN,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.UNKNOWN,
                reject_detail=(
                    f"the exec entry is followed by a command ('{shown}') "
                    "written without the '--' separator, so the guard cannot "
                    "tell what this call would do"
                ),
                reject_suggestion=_FIX_EXEC_USE_SEPARATOR,
            )
        return EffectiveTarget(
            scope="pod",
            namespace=ns,
            names=(pod_name,),
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
        )

    # Facts engine (design 4.6, face 4): the raw text of THIS level's
    # command line — ``v_args`` for a production-shaped call, or the peeled
    # inner line threaded down by a parent exec recursion. ``raw_command``
    # is NOT usable here (audit-facing ``_format_raw_command`` rendering,
    # not a shell line). No raw text (synthetic arg shapes) → ``_cmdline_raw``
    # itself is None → the token path below runs unchanged: those tokens are
    # already the definitive argv, with no quoting left to misread.
    facts_line: str | None = _cmdline_raw

    # Help request in inner command → read-only regardless of what
    # program runs (blade -h, blade create k8s pod-network drop -h, etc.)
    if _has_help_flag(inner):
        return EffectiveTarget(
            scope=SCOPE_READONLY,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
        )

    # Recursive: inner is ``blade ...``
    if inner[0] == "blade":
        # Domain-routing seam (phase-14 G3): the inline-blade CLI parser
        # belongs to the ChaosBlade carrier domain — reached through the
        # registry (the providers package's vertical routing point), not
        # a cross-carrier import.
        from chaos_agent.agent.providers.registry import (
            FaultProviderRegistry,
        )

        return FaultProviderRegistry.classify_inline_blade_command(
            inner, raw_command, fallback_ns=ns, fallback_pod=pod_name
        )

    # Recursive: inner is ``kubectl ...``
    if inner[0] == "kubectl":
        nested_cmdline = None
        if facts_line is not None:
            # Hand the nested level its OWN raw command line: peel this
            # level's ``--`` and pass the raw remainder down. A locate
            # failure leaves None → the nested level has no raw text and
            # stays on the token path (those tokens are the delivered
            # argv), whose misjudgement is over-strict (fail-closed),
            # never an escape-miss.
            from chaos_agent.tools._readonly_facts import (
                inner_raw_after_double_dash,
            )

            nested_cmdline, _ = inner_raw_after_double_dash(facts_line)
        nested = _classify_kubectl(
            inner[1:],
            raw_command,
            _cmdline_raw=nested_cmdline,
        )
        # Inherit the outer pod's namespace if the nested call has
        # nothing — kubectl-inside-pod usually inherits ambient.
        if nested.namespace == "default" and ns != "default":
            return EffectiveTarget(
                scope=nested.scope,
                namespace=ns,
                names=nested.names,
                labels=nested.labels,
                fault_target=nested.fault_target,
                fault_action=nested.fault_action,
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
    if facts_line is not None:
        probed = _peek_escape_tokens_facts(facts_line)
        if probed:
            escape_probe = probed
    elif (
        inner[0] in ("sh", "bash", "ash", "dash", "/bin/sh", "/bin/bash")
        and "-c" in inner
    ):
        c_idx = inner.index("-c")
        if c_idx + 1 < len(inner):
            try:
                nested_tokens = shlex.split(inner[c_idx + 1])
            except ValueError:
                nested_tokens = []
            if nested_tokens:
                escape_probe = nested_tokens
    escape_segment_hit = _escape_primitive_in_payload_segments(facts_line, inner)
    if (
        escape_probe[0] in ("nsenter", "chroot", "unshare")
        # R26/G-10: the head-only peek cannot see an escape primitive
        # riding PAST a ``;``/``&&`` inside a script or wrapper — check
        # at SEGMENT level so the compound forms reach this branch's
        # readonly ruling (the shared judge is already segment-level,
        # B46: an all-readonly compound stays READONLY, an escape stage
        # that mutates lands in SCOPE_ESCAPE).
        or escape_segment_hit is not None
    ):
        # R27/G-11c: the reject message names the primitive the branch
        # actually CAUGHT — for a compound payload the head-only probe
        # holds the innocent head, and the message is the model's repair
        # guidance (naming 'cat' sends the repair in the wrong direction).
        escape_trigger = (
            escape_probe[0]
            if escape_probe[0] in ("nsenter", "chroot", "unshare")
            else escape_segment_hit
        )
        # A READ-ONLY probe through the escape primitive is not a mutation:
        # from a privileged debug pod, ``chroot /host cat /etc/os-release`` is
        # the only way to inspect the node, and Phase 1 must be able to verify
        # host preconditions before committing to a plan. Delegate to the shared
        # read-only judge, which unwraps the primitive and rules on the command
        # actually being run (so ``chroot /host iptables -A ...`` still lands in
        # SCOPE_ESCAPE below). Mirrors the read-only exemption the fault-binary
        # branch already grants.
        from chaos_agent.tools.readonly import (
            is_readonly_inner_tokens,
            kubectl_exec_rejection_reason,
        )

        if facts_line is not None:
            # Facts judge: rules on the raw inner text sliced after this
            # level's ``--`` (the kubectl-exec prefix before it is inert),
            # so quoted literals and structure are classified exactly.
            escape_readonly = kubectl_exec_rejection_reason(facts_line) is None
        else:
            escape_readonly = is_readonly_inner_tokens(inner)
        if not escape_readonly:
            return EffectiveTarget(
                scope=SCOPE_ESCAPE,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.UNKNOWN,
                reject_detail=(
                    f"the exec runs a host-escape primitive "
                    f"('{escape_trigger}'); "
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
    # Delegate the read-only probe ruling to the SHARED judge — the same
    # one the tool layer enforces on kubectl_read exec/debug inners
    # (``tc qdisc show``, ``iptables -t nat -L``, ...). The private
    # first-token vocabulary here lagged the judge: ``tc qdisc show``
    # classifies as a pod mutation while the tool layer happily runs it,
    # so the verify-phase screener refused a probe the model could never
    # route around (the judge is the single source of truth for what a
    # fault binary's read-only surface is).
    from chaos_agent.tools.readonly import (
        kubectl_exec_rejection_reason,
        readonly_inner_tokens_reason,
    )

    binary = escape_probe[0].rsplit("/", 1)[-1]
    fault_binary_hit = binary in _FAULT_BINARIES
    if not fault_binary_hit:
        # R25/G-9: the head-only peek cannot see a fault binary riding
        # PAST a ``;``/``&&`` inside a script or wrapper — judge at
        # SEGMENT level so the compound forms keep the marker this
        # branch exists to set.
        fault_binary_hit = _fault_binary_in_payload_segments(
            facts_line, inner,
        )
    if fault_binary_hit:
        if facts_line is not None:
            fb_probe_reason = kubectl_exec_rejection_reason(facts_line)
        else:
            fb_probe_reason = readonly_inner_tokens_reason(escape_probe)
        if fb_probe_reason is None:
            return EffectiveTarget(
                scope=SCOPE_READONLY,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.HIGH,
            )
        # A pod-scoped mutation: same shape the guard already accepts for
        # any other pod-level fault, so identity/blast-radius comparison
        # applies normally instead of the escape path's carrier
        # requirement. ``fault_binary_mutation`` marks the shape so the
        # screener's vehicle exemption does NOT swallow it: inside a
        # privileged / hostNetwork tool pod the same binary shapes the
        # HOST, which the static classifier cannot rule out — keep the
        # identity review.
        return EffectiveTarget(
            scope="pod",
            namespace=ns,
            names=(pod_name,),
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
            fault_binary_mutation=True,
        )

    # Read-only probe (cat/ls/df/ps, iptables -L, ip addr show, ...) — a
    # non-mutating inspection of the pod. Classify as READONLY so the phase-1 /
    # intent / verify screeners pass it without a target comparison (reads do
    # not drift). Reached only AFTER the escape / mutating-fault-binary checks
    # above, so ``iptables -A`` / ``chroot`` / ``stress`` never land here — the
    # shared classifier returns False for them and this branch is skipped.
    if facts_line is not None:
        # Same raw-inner facts judgment as the escape branch above; the
        # token fallback below only runs when no raw text exists at all.
        probe_reason = kubectl_exec_rejection_reason(facts_line)
    else:
        probe_reason = readonly_inner_tokens_reason(inner)
    if probe_reason is None:
        return EffectiveTarget(
            scope=SCOPE_READONLY,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
        )

    # Plain shell command (rm/kill/etc) or a MALFORMED probe (shell control
    # operators, unknown binary) — acts on the pod's own filesystem/process
    # space, so scope=pod is correct for Phase 2. The read-only phase screeners
    # additionally need the verdict's CAUSE, so carry the reason the shared
    # judge actually reached — never flatten it to a boolean and let the
    # screeners re-invent a generic template.
    #
    # Vehicle identity (exec into the task's own injection machinery) is
    # resolved DATA-side by the screener — task-registered artifacts and
    # live label-selector discovery — not from the pod name here. The
    # fault-binary mutation branch above deliberately keeps identity review
    # via ``fault_binary_mutation``; inner commands here were already
    # screened by the escape/banned/readonly checks above.
    return EffectiveTarget(
        scope="pod",
        namespace=ns,
        names=(pod_name,),
        raw_command=raw_command,
        confidence=ConfidenceLevel.HIGH,
        readonly_probe_reason=probe_reason,
    )


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
            scope=SCOPE_UNKNOWN,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                "kubectl debug names no target — expected a pod name or "
                "'node/<node-name>' as the first positional argument"
            ),
            reject_suggestion=_FIX_NAME_THE_TARGET,
        )
    # R45: kubectl debug resolves EVERY positional as a separate target
    # (v1.34 ``o.TargetNames = args[:argsLen]`` → ``ResourceNames("pods",
    # o.TargetNames...)`` + a per-info Visit that creates a privileged pod
    # per node / patches an ephemeral container per pod). Reading only the
    # FIRST positional let extra names ride the approved one into the
    # cluster. The walk is flag-aware, so a flag's value (``--image ubuntu``)
    # never counts as one.
    from chaos_agent.tools._readonly_facts import exec_separator_shape

    positionals, _after = exec_separator_shape(args)
    if len(positionals) > 1:
        extras = positionals[1:]
        shown = " ".join(extras[:8]) + (" ..." if len(extras) > 8 else "")
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                f"kubectl debug reads EVERY positional as a separate target "
                f"— '{shown}' beyond the first would be debugged too, "
                "creating a privileged pod / ephemeral container OUTSIDE "
                "the single approved target"
            ),
            reject_suggestion=_FIX_DEBUG_ONE_TARGET,
        )
    kind, name = _split_kind_name(first)
    canonical = canonicalise_kind(kind) if kind else "pod"
    ns = parse_namespace(args, default="" if canonical == "node" else "default")
    return EffectiveTarget(
        scope=canonical,
        namespace=ns,
        names=(name,),
        raw_command=raw_command,
        confidence=ConfidenceLevel.HIGH,
    )


def _classify_kubectl_node_op(args: list[str], raw_command: str) -> EffectiveTarget:
    """``kubectl cordon NODE`` / ``uncordon NODE`` / ``drain NODE``."""
    node = _first_positional(args)
    if not node:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                "the node-maintenance command (cordon / uncordon / drain) names no node"
            ),
            reject_suggestion=_FIX_NAME_THE_TARGET,
        )
    return EffectiveTarget(
        scope="node",
        namespace="",
        names=(node,),
        raw_command=raw_command,
        confidence=ConfidenceLevel.HIGH,
    )


def _classify_kubectl_taint(args: list[str], raw_command: str) -> EffectiveTarget:
    """``kubectl taint nodes NODE key=val:Effect``."""
    # First positional is typically "nodes"; second is the node name.
    pos = _list_positionals(args)
    if len(pos) >= 2 and canonicalise_kind(pos[0]) == "node":
        return EffectiveTarget(
            scope="node",
            namespace="",
            names=(pos[1],),
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
        )
    return EffectiveTarget(
        scope=SCOPE_UNKNOWN,
        namespace="",
        raw_command=raw_command,
        confidence=ConfidenceLevel.UNKNOWN,
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
            scope=SCOPE_UNKNOWN,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                "kubectl set names neither a sub-resource nor a target "
                f"(expected 'set <{expected}> <kind>/<name> ...')"
            ),
            reject_suggestion=_FIX_NAME_THE_TARGET,
        )
    subresource = args[idx]
    if subresource not in _KUBECTL_SET_SUBRESOURCES:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                f"'{subresource}' is not a kubectl set sub-resource "
                f"(expected one of: {expected})"
            ),
            reject_suggestion=_FIX_UNKNOWN_VOCABULARY,
        )
    # Drop the sub-resource; everything else (flags, kind/name) is untouched.
    remainder = args[:idx] + args[idx + 1 :]
    return _classify_kubectl_resource(remainder, raw_command, default_kind=None)


def _classify_kubectl_resource(
    args: list[str],
    raw_command: str,
    *,
    default_kind: str | None,
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
            scope=SCOPE_UNKNOWN,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.UNKNOWN,
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
                scope=SCOPE_UNKNOWN,
                namespace="",
                raw_command=raw_command,
                confidence=ConfidenceLevel.UNKNOWN,
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
            scope=SCOPE_UNKNOWN,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.UNKNOWN,
            reject_detail=(
                f"resource kind '{kind}' is not one the guard recognises, so "
                "it cannot be compared against the approved target's scope"
            ),
            reject_suggestion=_FIX_UNKNOWN_VOCABULARY,
        )

    # Cluster-scoped resources (node/pv/namespace/cluster*role*) skip ns
    # — topology single-sourced via is_cluster_scoped_kind (R21/G-5:
    # this branch previously carried its own inline copy of the set).
    cluster_scoped = is_cluster_scoped_kind(canonical)
    ns = parse_namespace(args, default="" if cluster_scoped else "default")
    labels = parse_labels(args)
    names: tuple[str, ...] = (name,) if name else ()

    return EffectiveTarget(
        scope=canonical,
        namespace=ns,
        names=names,
        labels=labels,
        raw_command=raw_command,
        confidence=ConfidenceLevel.HIGH if name or labels else ConfidenceLevel.LOW,
    )


def _is_known_kind(token: str) -> bool:
    """True iff ``token`` is a known kubectl kind (any spelling)."""
    if not token:
        return False
    head = token.split(".", 1)[0].lower().strip()
    return head in KIND_ALIASES


def _imperative_workload_create_kind(args: list[str]) -> str | None:
    """The canonical workload kind an imperative create would start, if any.

    ``args`` is the tail AFTER the ``create`` verb. Mirrors the positional
    reading of ``_classify_kubectl_resource`` (``KIND NAME`` and the
    ``KIND/NAME`` slash form) so the ToolGuard dispatch check and the
    dispatcher's ``sub == "create"`` branch below share one vocabulary —
    :data:`_IMPERATIVE_WORKLOAD_KINDS` — instead of two hand-kept lists.
    """
    first = _first_positional(args)
    if not first:
        return None
    kind, _name = _split_kind_name(first)
    if not kind:
        kind = first if _is_known_kind(first) else ""
    if not kind:
        return None
    canonical = canonicalise_kind(kind)
    return canonical if canonical in _IMPERATIVE_WORKLOAD_KINDS else None


def _recovery_carrier_allowed_images() -> frozenset[str]:
    """The recovery-carrier image whitelist: configured ∪ auto-discovered.

    ``settings.recovery_carrier_allowed_images`` is the manual fallback
    (env/config). ``settings.recovery_carrier_discovered_images`` is
    populated at task start by the preplan probe (healthy DaemonSet
    images — node-cached, no pull needed), so restricted-network clusters
    work without manual configuration.

    Kept a function (not a module constant) so tests and operators can
    reconfigure either setting at runtime and the classifier honours the
    change — same discipline as the manifest whitelist's
    ``_allowed_manifest_kinds_text``.
    """
    from chaos_agent.config.settings import settings

    raw = str(settings.recovery_carrier_allowed_images or "")
    discovered = str(settings.recovery_carrier_discovered_images or "")
    return frozenset(
        img.strip()
        for img in f"{raw},{discovered}".split(",")
        if img.strip()
    )


def _recovery_carrier_image_hint(image: str) -> str:
    """One-line actionable hint for an off-allowlist carrier image."""
    from chaos_agent.config.settings import settings

    allowed = str(settings.recovery_carrier_allowed_images or "")
    discovered = str(settings.recovery_carrier_discovered_images or "")
    parts = [f"image {image!r} not in the carrier image allowlist"]
    if discovered:
        parts.append(f"auto-discovered this task: {discovered}")
    else:
        parts.append(
            "auto-discovery found nothing (no healthy DaemonSet images or "
            "probe not run)"
        )
    parts.append(
        "manual fallback: settings key recovery_carrier_allowed_images / "
        "env BLADE_AI_RECOVERY_CARRIER_ALLOWED_IMAGES"
    )
    if allowed:
        parts.append(f"configured: {allowed}")
    return "; ".join(parts)


def _recovery_carrier_shape_failure(args: list[str], name: str) -> str | None:
    """First failing SHAPE condition, human-readable — or ``None`` when OK.

    Single source of truth for the shape predicate: the boolean wrapper
    below delegates here, so the guard's rejection feedback and the
    classifier verdict can never drift apart (run8 lesson: an image
    allowlist miss surfaced as an opaque REJECT_DRIFT with no reason).
    """
    from chaos_agent.config.settings import settings

    prefix = str(settings.recovery_carrier_name_prefix or "drill-rc-")
    if not prefix or not name.startswith(prefix):
        return f"name {name!r} lacks the carrier prefix {prefix!r}"
    # Flag whitelist (condition 5). Values taken as separate tokens
    # (``--image x``) or inline (``--image=x``); ``-n x`` is the only
    # short form admitted.
    value_flags = {"--image", "--restart", "--overrides", "-n", "--namespace"}
    bare_flags = {"--command"}
    separator = "--"
    separator_index = args.index(separator) if separator in args else len(args)
    for index, token in enumerate(args):
        if index >= separator_index:
            break
        if token == separator:
            break
        if token.startswith("-"):
            head = token.split("=", 1)[0]
            if head in bare_flags and "=" not in token:
                continue
            if head in value_flags:
                # Inline form carries its own value; separate form takes
                # the next token — validated as a whole token, not a flag.
                if "=" not in token and index + 1 >= separator_index:
                    return f"flag {token!r} has no value before '--'"
                continue
            return (
                f"flag {token!r} is outside the carrier flag whitelist "
                "(only -n/--namespace, --image, --restart, --command, "
                "--overrides admitted)"
            )
    # ``--restart=Never`` (both spellings; anything else keeps the default
    # Always policy and the pod becomes an immortal restart loop).
    restart = _option_pair_value(args, "--restart")
    if restart != "Never":
        return (
            f"--restart must be Never (got {restart!r}) — no crash-restart "
            "loops, the pod's terminal phase is its lifecycle truth"
        )
    # Condition 3 requires the EXPLICIT ``--command`` flag (design D7):
    # without it ``--``-args feed the image's default entrypoint (busybox
    # ``sh sleep N`` errors out), so the shape is not the prescribed
    # skeleton. Bare flag only — ``--command=true`` is not a shape the
    # standard prescribes, and fail-closed keeps it out.
    if "--command" not in args[:separator_index]:
        return "missing the explicit --command flag (skeleton must be sleep-only)"
    image = _option_pair_value(args, "--image")
    if not image:
        return "missing --image (the carrier image allowlist applies to it)"
    if image not in _recovery_carrier_allowed_images():
        return _recovery_carrier_image_hint(image)
    # Sleep-only skeleton: everything after ``--`` must be ``sleep N``.
    if separator_index >= len(args):
        return "missing '--' sleep skeleton arguments"
    inner = args[separator_index + 1:]
    if len(inner) != 2 or inner[0] != "sleep":
        return (
            "the '--' payload must be exactly ``sleep N`` (sleep-only "
            "self-expiring skeleton)"
        )
    try:
        sleep_seconds = int(inner[1])
    except ValueError:
        return f"sleep bound {inner[1]!r} is not an integer"
    if not 0 < sleep_seconds <= int(settings.recovery_carrier_max_sleep_seconds):
        return (
            f"sleep bound {sleep_seconds} outside 1.."
            f"{settings.recovery_carrier_max_sleep_seconds}"
        )
    return _recovery_carrier_overrides_failure(args)


def _is_recovery_carrier_run(args: list[str], name: str) -> bool:
    """Whether a ``kubectl run`` matches the recovery-carrier pod SHAPE.

    Five conditions, all required (design D7 — fail closed on any miss):
      1. task-side carrier name prefix (auxiliary signal — the SECURITY
         boundary is the in-net pod secondary scope + task registration,
         never the name alone);
      2. ``--restart=Never`` (no crash-restart loop: the pod's terminal
         phase is the truth of its lifecycle);
      3. an explicit sleep-only skeleton command (``--command -- sleep N``
         with N bounded by ``recovery_carrier_max_sleep_seconds``) — the
         carrier must self-expire even if every cleanup path dies;
      4. ``--image`` inside the effective allowlist (configured ∪
         auto-discovered — see ``_recovery_carrier_allowed_images``);
      5. a FLAG WHITELIST, not a blacklist. ``kubectl run`` accepts many
         flags the five conditions never inspect (``--serviceaccount``,
         ``--env``, ``--nodename``, ``--schedule``, ...). Checking only the
         ones we DO inspect lets any other flag ride the carrier gate —
         ``--serviceaccount`` smuggles an arbitrary SA past the overrides
         white-list, ``--schedule`` turns the run into a CronJob. So the
         shape admits ONLY ``-n/--namespace``, ``--image``, ``--restart``,
         ``--command``, ``--overrides`` (the latter still payload-checked
         below), plus the ``--`` separator; any other flag fails closed.

    Thin boolean wrapper over ``_recovery_carrier_shape_failure`` (the
    single implementation — keeps verdict and diagnostics in lockstep).
    """
    return _recovery_carrier_shape_failure(args, name) is None


def _recovery_carrier_overrides_failure(args: list[str]) -> str | None:
    """Overrides payload check (SHAPE condition 5b) — failure reason or None.

    ``--overrides`` patches the raw Pod spec, which could smuggle
    hostNetwork / privileged / hostPath past the shape check. Fail closed
    on any overrides payload except the TWO documented scheduling keys:
    ``{"spec": {"serviceAccountName": "<str>",
                "tolerations": [<scheduling-only>]}}``. Tolerations are
    POD SCHEDULING match rules — they admit the carrier onto tainted
    nodes (enterprise clusters commonly taint EVERY node) but grant no
    runtime privilege whatsoever. Structurally validated: each entry's
    keys ⊆ {key, operator, value, effect}, key is a non-empty string
    (an empty toleration matches EVERY taint — no free pass), operator
    ∈ {Exists, Equal}, effect ∈ the three legal values.
    """
    overrides = _option_pair_value(args, "--overrides")
    if not overrides:
        return None
    try:
        doc = json.loads(overrides)
    except (TypeError, ValueError):
        return "--overrides payload is not valid JSON"
    if not isinstance(doc, dict) or set(doc) != {"spec"}:
        return (
            "--overrides must contain exactly the top-level key 'spec' "
            "(serviceAccountName + tolerations only)"
        )
    spec = doc["spec"]
    if not isinstance(spec, dict) or not spec:
        return "--overrides spec must be a non-empty object"
    if not set(spec) <= {"serviceAccountName", "tolerations"}:
        return (
            "--overrides spec admits only serviceAccountName and "
            "tolerations (scheduling keys; no hostNetwork/privileged/"
            "hostPath)"
        )
    service_account = spec.get("serviceAccountName")
    if service_account is not None and (
        not isinstance(service_account, str) or not service_account
    ):
        return "--overrides serviceAccountName must be a non-empty string"
    if "tolerations" in spec and not _valid_carrier_tolerations(
        spec["tolerations"],
    ):
        return (
            "--overrides tolerations must be scheduling-only entries "
            "(keys ⊆ key/operator/value/effect, non-empty key, operator "
            "Exists|Equal, effect NoSchedule|PreferNoSchedule|NoExecute)"
        )
    return None


_TOLERATION_KEYS = {"key", "operator", "value", "effect"}
_TOLERATION_OPERATORS = {"Exists", "Equal"}
_TOLERATION_EFFECTS = {
    "NoSchedule",
    "NoExecute",
    "PreferNoSchedule",
}


def _valid_carrier_tolerations(value: object) -> bool:
    """Structural white-list for the carrier overrides tolerations array."""
    if not isinstance(value, list) or not value:
        return False
    for toleration in value:
        if not isinstance(toleration, dict):
            return False
        if not set(toleration) <= _TOLERATION_KEYS:
            return False
        # A toleration without a key matches EVERY taint (an empty entry
        # matches all) — the carrier must name the taints it tolerates.
        key = toleration.get("key")
        if not isinstance(key, str) or not key:
            return False
        operator = toleration.get("operator")
        if operator is not None and (
            not isinstance(operator, str)
            or operator not in _TOLERATION_OPERATORS
        ):
            return False
        toleration_value = toleration.get("value")
        if toleration_value is not None and not isinstance(
            toleration_value, str,
        ):
            return False
        effect = toleration.get("effect")
        if effect is not None and (
            not isinstance(effect, str) or effect not in _TOLERATION_EFFECTS
        ):
            return False
    return True


def _option_pair_value(args: list[str], option: str) -> str:
    """Value of ``option value`` / ``option=value`` in argv ("" when absent)."""
    for index, token in enumerate(args):
        if token == option and index + 1 < len(args):
            return args[index + 1]
        if token.startswith(f"{option}="):
            return token.split("=", 1)[1]
    return ""


def _classify_kubectl_run(args: list[str], raw_command: str) -> EffectiveTarget:
    """``kubectl run NAME --image=...`` — creates a new pod."""
    name = _first_positional(args)
    if not name:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.UNKNOWN,
            reject_detail="kubectl run names no pod to create",
            reject_suggestion=_FIX_NAME_THE_TARGET,
        )
    ns = parse_namespace(args, default="default")
    # Recovery-carrier shape first: a compliant run classifies as the
    # vehicle marker the screener registers (task-side artifact + in-net
    # pod secondary scope). Any other run keeps the plain pod scope and
    # faces the ordinary drift review — behaviour identical to before
    # this branch existed.
    if _is_recovery_carrier_run(args, name):
        return EffectiveTarget(
            scope="pod",
            namespace=ns,
            names=(name,),
            raw_command=raw_command,
            confidence=ConfidenceLevel.HIGH,
            is_recovery_carrier=True,
        )
    return EffectiveTarget(
        scope="pod",
        namespace=ns,
        names=(name,),
        raw_command=raw_command,
        confidence=ConfidenceLevel.HIGH,
    )


def _classify_kubectl_rollout(args: list[str], raw_command: str) -> EffectiveTarget:
    """``kubectl rollout restart deploy/X`` etc."""
    if not args:
        return EffectiveTarget(
            scope=SCOPE_UNKNOWN,
            namespace="",
            raw_command=raw_command,
            confidence=ConfidenceLevel.UNKNOWN,
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
                    scope="pod",
                    namespace=ns,
                    names=(name,),
                    raw_command=raw_command,
                    confidence=ConfidenceLevel.HIGH,
                )
            ns = parse_namespace(args, default="default")
            return EffectiveTarget(
                scope="pod",
                namespace=ns,
                names=(pod_part,),
                raw_command=raw_command,
                confidence=ConfidenceLevel.HIGH,
            )
    return EffectiveTarget(
        scope=SCOPE_UNKNOWN,
        namespace="",
        raw_command=raw_command,
        confidence=ConfidenceLevel.UNKNOWN,
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


def _has_help_flag(args: list[str]) -> bool:
    """True if ``-h`` or ``--help`` appears anywhere in *args*.

    Used to short-circuit classification: any command invoked with a
    help flag only prints usage text and never mutates state.
    """
    return "-h" in args or "--help" in args


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
