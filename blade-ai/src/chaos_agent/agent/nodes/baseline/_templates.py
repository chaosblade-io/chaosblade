"""Baseline template-variable resolution + coverage / evidence-supplement helpers.

Split out of ``baseline_capture.py`` (Phase 2 module split). Resolves the
``{node_name}`` / ``{pod_name}`` / ``{namespace}`` / ``{label_selector}`` /
``{debug_pod}`` / ``{target_pod}`` / ``{target_node}`` template variables in
registry / fallback ``BaselineCommand`` entries into concrete, executable
command dicts (with multi-target sampling),
reports how much of a multi-target baseline was actually observed, and derives
deterministic evidence-gap supplement commands. Depends only on the command
data layer (``_commands``), the evidence contract, the fault spec, and the
channel profile constants — never on ``baseline_capture`` — so there is no
import cycle.
"""

from __future__ import annotations

import logging
import re

from chaos_agent.agent.evidence import EvidenceProfile, host_evidence_supplements
from chaos_agent.agent.nodes.baseline._commands import (
    BaselineCommand,
    _normalize_debug_namespace,
)
from chaos_agent.agent.state import AgentState
from chaos_agent.transports import (
    PROFILE_HOST,
    PROFILE_K8S,
    profile_of,
    resolve_channel_name,
)

logger = logging.getLogger(__name__)

# Payload-placeholder dialects that share the brace glyph with our template
# variables but belong to the COMMAND PAYLOAD's own grammar, not ours. They
# are literal runtime syntax (curl ``-w`` format, POSIX shell parameter
# expansion), must survive template resolution verbatim, and must never be
# mistaken for unresolved template variables. See the grammar-domain
# separation comment at the detection site (Case #58).
_PAYLOAD_PLACEHOLDER_RE = re.compile(r"(?:%|\$)\{[a-z_]+\}")


# When a fault targets multiple resources (e.g. 44 nodes in an AZ-wide
# network partition), expanding per-name baseline commands for ALL targets
# would flood the API server.  Cap at a representative sample — the
# verifier compares before/after on the SAME resource, so a sample is
# sufficient for causation attribution.  The first target is always
# included (it is the primary injection target); remaining slots are
# evenly spaced across the full list to avoid clustering.
_BASELINE_NAME_EXPANSION_CAP = 10


# scope -> per-name baseline template variable. A new scope registers its
# variable here instead of adding another branch in _resolve_templates;
# scopes absent from the map fall back to "{pod_name}".
_SCOPE_NAME_VAR: dict[str, str] = {
    "node": "{node_name}",
    "host": "{host_name}",
    "pod": "{pod_name}",
}


def _k8s_parse(command: str) -> tuple[str, str]:
    """Split a full ``kubectl <sub> <args...>`` command into (subcommand, v_args).

    The leading ``kubectl `` is stripped; the first remaining token is the
    subcommand and the rest is the argument string that the existing exec
    helpers (``build_kubectl_cmd``) consume unchanged. Non-kubectl input
    (defensive) yields the whole string as v_args with an empty subcommand.
    """
    s = command.strip()
    if s.startswith("kubectl "):
        s = s[len("kubectl "):].strip()
    parts = s.split(None, 1)
    subcommand = parts[0] if parts else ""
    v_args = parts[1] if len(parts) > 1 else ""
    return subcommand, v_args


def _resolve_one_baseline(
    cmd: BaselineCommand,
    name: str,
    scope: str,
    namespace: str,
    label_selector: str,
    profile: str,
    target_pod: str = "",
    target_node: str = "",
) -> dict:
    """Resolve a single BaselineCommand with the given target name.

    Shared by single-target and multi-target paths so the variable
    replacement, mode auto-correction, and unknown-var detection
    logic stays in one place.

    For ``profile == "k8s"`` the resolved full command is additionally
    parsed back into ``subcommand`` + ``v_args`` so the existing kubectl
    exec helpers keep working unchanged. For ``profile == "host"`` the
    command is a plain shell diagnostic (usually variable-free) and is
    executed verbatim via the transport layer.

    ``target_pod`` (baseline-observation-contract change): the literal pod
    name pre-resolved by ``baseline_capture._resolve_target_pod`` for
    exec-class probes. Empty string means "not resolved" — commands that
    reference ``{target_pod}`` under an empty value are marked
    ``_unresolved`` and skipped by the viability gate (fail-open to the
    existing absence mechanism, never silently substituted with the
    workload name). Distinct from ``{pod_name}`` which resolves to
    ``spec.names[0]`` (a DEPLOYMENT name under workload scope — kubectl
    exec's bare-name form only accepts pod names).
    """
    command = cmd.command
    unresolved = False

    # ``node_name`` is the NODE an observation is anchored on — not whatever
    # ``spec.names[0]`` happens to be. Under node scope the two coincide;
    # under pod/workload scope ``name`` is the victim POD and the node must
    # come from the pre-resolved ``target_node`` fact (``_resolve_target_node``,
    # isomorphic to ``{target_pod}``'s ``_resolve_target_pod``).
    #
    # Case #67 (W-67-1): this was an unconditional ``node_name = name`` whose
    # comment claimed "only used when template has {node_name}" — but
    # ``_node_name`` is ALSO read unconditionally by the debug_two_step path
    # (_executors: pre-create debug pods, the pod cache lookup, and both exec
    # helpers), so a pod-scope run issued ``kubectl debug node/<POD-NAME>``,
    # got ``nodes "<POD-NAME>" not found``, and retried TEN times rotating the
    # image — a retry dimension that cannot fix a wrong node name. Two single
    # sources already in this file stated the correct rule and went
    # unconsulted: the ``_SCOPE_NAME_VAR`` registry (pod scope → ``{pod_name}``,
    # never ``{node_name}``) and ``_evidence_supplement_commands``'s
    # ``"{node_name}" if spec.scope == "node"`` guard.
    #
    # Fail-open: an unresolved ``target_node`` leaves this empty, so commands
    # carrying ``{node_name}`` are marked ``_unresolved`` and skipped by the
    # viability gate — visible absence, never a silent pod-name substitution.
    node_name = name if scope == "node" else (target_node or "")
    pod_name = name if scope not in ("node", "host") else ""
    host_name = name if scope == "host" else ""

    if "{namespace}" in command:
        if namespace:
            command = command.replace("{namespace}", namespace)
        else:
            unresolved = True
    if "{node_name}" in command:
        if node_name:
            command = command.replace("{node_name}", node_name)
        else:
            unresolved = True
    if "{host_name}" in command:
        if host_name:
            command = command.replace("{host_name}", host_name)
        else:
            unresolved = True
    if "{pod_name}" in command:
        if pod_name:
            command = command.replace("{pod_name}", pod_name)
        else:
            unresolved = True
    if "{label_selector}" in command:
        if label_selector:
            command = command.replace("{label_selector}", label_selector)
        else:
            unresolved = True
    if "{target_pod}" in command:
        if target_pod:
            command = command.replace("{target_pod}", target_pod)
        else:
            # Fail-open: pre-resolution did not yield a literal pod name
            # (workload-scope selector query failed / returned empty, or
            # the caller did not prewarm). Mark unresolved so the viability
            # gate skips this observation instead of executing a command
            # with the literal ``{target_pod}`` text (kubectl would reject
            # it with an obscure error, and the retry LLM would then have
            # to guess what went wrong).
            unresolved = True
    if "{target_node}" in command:
        if target_node:
            command = command.replace("{target_node}", target_node)
        else:
            # Fail-open, same contract as {target_pod}: the node was not
            # pre-resolvable (non-applicable scope, or the nodeName query
            # failed / returned empty). Mark unresolved so the viability gate
            # skips the observation instead of executing a command with the
            # literal ``{target_node}`` text — and, critically, instead of
            # letting the debug_two_step path build a carrier against a name
            # that was never a node.
            unresolved = True
    # {debug_pod} is resolved later in the debug_two_step execution path.

    # Deep defense: auto-correct mode if {debug_pod} present but mode is wrong.
    mode = cmd.mode
    if "{debug_pod}" in command and mode != "debug_two_step":
        logger.warning(
            "Deep defense: auto-correcting mode from '%s' to "
            "'debug_two_step' in _resolve_templates for: %s",
            mode, cmd.description,
        )
        mode = "debug_two_step"

    # k8s: derive subcommand + v_args from the resolved command so the
    # kubectl exec helpers (which read cmd_info["subcommand"/"v_args"])
    # keep working. host: the command runs verbatim, no split needed.
    if profile == PROFILE_HOST:
        subcommand, v_args = "", ""
    else:
        subcommand, v_args = _k8s_parse(command)
        # Normalize namespace for debug_two_step commands (operates on the
        # kubectl-side args, before ``--``).
        if mode == "debug_two_step":
            v_args = _normalize_debug_namespace(v_args)
            command = f"kubectl {subcommand} {v_args}".strip()

    # Detect unknown template variables left after known-variable replacement.
    # Grammar-domain separation (Case #58 inject-3dae7b4f): the template
    # variable namespace (``{pod_name}`` & co, our registry dialect) and the
    # PAYLOAD placeholder dialect (curl's ``-w '%{time_connect}'``, shell's
    # ``${var}``) are two different languages that happen to share the brace
    # glyph. A curl timing probe is a fully concrete LLM-derived command —
    # ``%{time_connect}`` is literal syntax curl expands at run time, not an
    # unresolved template. The old single-regex scan treated it as an unknown
    # variable, killed the command at the viability gate, and the llm strategy
    # degraded to partial (4/5) — the network primary metric never had any
    # production path (llm killed here, registry partial on empty labels,
    # fallback carries no network command). Strip payload placeholders BEFORE
    # scanning for template variables; ``command`` itself keeps the original
    # text (only the detection input is masked).
    if not unresolved:
        detection_text = _PAYLOAD_PLACEHOLDER_RE.sub(" ", command)
        remaining_vars = re.findall(r'\{([a-z_]+)\}', detection_text)
        # Exemption list (mechanism-shared with {debug_pod}, memory a98c6772):
        # placeholders resolved LATER in the execution path must survive the
        # unknown-var scan. {debug_pod} is resolved by _exec_debug_two_step;
        # {target_pod} is resolved by pre-execution substitution above (any
        # surviving occurrence here means the value was empty, already
        # caught by the unresolved flag). {target_node} is substituted above
        # under the same pre-resolution contract.
        unknown_vars = [
            v for v in remaining_vars
            if v not in ("debug_pod", "target_pod", "target_node")
        ]
        if unknown_vars:
            logger.warning(
                "Unknown template variable(s) in baseline command '%s': %s",
                cmd.description, unknown_vars,
            )
            unresolved = True

    return {
        "description": cmd.description,
        "profile": profile,
        "command": command,
        "subcommand": subcommand,
        "v_args": v_args,
        "mode": mode,
        "_unresolved": unresolved,
        "_node_name": node_name,
        "_extractors": cmd.extractors,
        # The declared observation DIMENSION travels with the resolved command
        # so it can be stamped onto the observation (see
        # ``_executors._execute_observations``). ``class_value`` is validated
        # against the command's FORM at derive time; carrying it forward is
        # what lets the retry path check CONTINUITY — a replacement may repair
        # the command, not redefine what was being measured. ``None`` for
        # registry-sourced commands (form trusted by construction), which is
        # also the signal for the retry gate to fall back to the LLM's own
        # declaration instead of pinning.
        "_class": cmd.class_value,
    }


def _resolve_templates(
    commands: list[BaselineCommand],
    state: AgentState,
    profile: str | None = None,
    target_pod: str | None = None,
    target_node: str | None = None,
) -> list[dict]:
    """Resolve template variables in BaselineCommand list.

    Returns list of dicts with resolved values. Unresolvable commands
    are marked with _unresolved=True so _execute_observations can skip them.

    ``profile`` (k8s/host) selects the execution semantics. When omitted it
    is derived from the connection channel; the node passes its already-
    computed profile so the value has a single source per invocation.

    ``target_pod`` (baseline-observation-contract change): the literal pod
    name pre-resolved by ``_resolve_target_pod`` in baseline_capture, or
    ``None`` when unavailable (workload-scope selector query failed / not
    applicable scope). Threaded through to ``_resolve_one_baseline`` for
    ``{target_pod}`` substitution.

    ``target_node`` (Case #67 / W-67-1): the literal NODE name pre-resolved by
    ``_resolve_target_node``, or ``None`` when unavailable. Under node scope it
    equals ``spec.names[0]``; under pod/workload scope it is the victim's host
    node (``.spec.nodeName``), which never appears in ``spec.names``. Feeds both
    ``{target_node}`` substitution and the ``_node_name`` field the
    debug_two_step path builds its carrier against — the same fact, one source,
    so the carrier can no longer be aimed at a pod name.

    Multi-target expansion: when the fault spec contains more than one
    target name (e.g. AZ-wide network partition with 44 nodes), each
    per-name template command is expanded into one entry per name, up
    to ``_BASELINE_NAME_EXPANSION_CAP``.  Commands without name-specific
    variables (e.g. ``kubectl get nodes``) are not expanded.
    """
    from chaos_agent.agent.spec.fault_spec import read_fault_spec

    if profile is None:
        profile = profile_of(resolve_channel_name(state))
    _tp = target_pod or ""
    _tn = target_node or ""
    spec = read_fault_spec(state)
    if spec is None:
        return [
            _resolve_one_baseline(cmd, "", "", "", "", profile, _tp, _tn)
            for cmd in commands
        ]

    namespace = spec.namespace
    names = list(spec.names)
    labels_dict = spec.labels
    # kubectl 必须用 ``-l key=value`` 形式才能把字符串识别为 label selector；
    label_selector = (
        "-l " + ",".join(f"{k}={v}" for k, v in labels_dict.items())
        if labels_dict
        else ""
    )

    is_multi_target = len(names) > 1
    # scope -> the per-name template variable it fills. Registry-style map so a
    # new scope only adds an entry here (not another if/elif); unknown scopes
    # fall back to the pod variable, matching the historical default branch.
    name_var = _SCOPE_NAME_VAR.get(spec.scope, "{pod_name}")
    if is_multi_target:
        capped = len(names) > _BASELINE_NAME_EXPANSION_CAP
        if capped:
            # Evenly-spaced sample: always include first and last,
            # fill with evenly distributed indices in between.
            n = len(names)
            cap = _BASELINE_NAME_EXPANSION_CAP
            indices = sorted(set(
                int(i * (n - 1) / (cap - 1)) for i in range(cap)
            ))
            expansion_names = [names[i] for i in indices]
        else:
            expansion_names = names
    else:
        capped = False
        expansion_names = names[:1]

    resolved = []
    for cmd in commands:
        if is_multi_target and name_var in cmd.command:
            for i, name in enumerate(expansion_names):
                entry = _resolve_one_baseline(
                    cmd, name, spec.scope, namespace, label_selector, profile,
                    _tp, _tn,
                )
                entry["description"] = f"{cmd.description} ({name})"
                entry["_target_name"] = name
                entry["_target_sampled"] = capped
                if capped and i == 0:
                    entry["description"] += (
                        f" [sampled {len(expansion_names)}/{len(names)}]"
                    )
                resolved.append(entry)
        else:
            name = names[0] if names else ""
            entry = _resolve_one_baseline(
                cmd, name, spec.scope, namespace, label_selector, profile,
                _tp, _tn,
            )
            resolved.append(entry)

    return resolved


def _target_coverage(
    spec,
    resolved: list[dict],
    successful_observations: list[dict],
) -> dict:
    """Describe how much of a multi-target baseline was actually observed.

    Evidence-profile completeness answers whether the *kind* of evidence is
    sufficient.  It cannot answer whether all nodes in an AZ-wide experiment
    were represented.  Keep those concerns separate so a representative
    sample never appears as full target coverage.
    """
    requested_names = list(spec.names) if spec else []
    if not requested_names:
        return {
            "applicable": False,
            "collection_mode": "not_applicable",
            "requested_count": 0,
            "observed_count": 0,
            "complete": True,
        }

    observation_text = "\n".join(
        " ".join(
            str(observation.get(key, "") or "")
            for key in ("description", "command", "stdout", "stderr")
        ).lower()
        for observation in successful_observations
    )
    observed_names = [
        name for name in requested_names
        if re.search(
            rf"(?<![A-Za-z0-9_.-]){re.escape(name)}(?![A-Za-z0-9_.-])",
            observation_text,
            flags=re.IGNORECASE,
        )
    ]
    planned_names = list(dict.fromkeys(
        entry.get("_target_name", "") for entry in resolved
        if entry.get("_target_name")
    ))
    sampled = any(entry.get("_target_sampled") for entry in resolved)
    if sampled:
        collection_mode = "targeted_sample"
    elif planned_names:
        collection_mode = "targeted_full"
    else:
        collection_mode = "aggregate_or_llm"

    missing_names = [name for name in requested_names if name not in observed_names]
    return {
        "applicable": True,
        "collection_mode": collection_mode,
        "requested_count": len(requested_names),
        "planned_count": len(planned_names),
        "planned_names": planned_names,
        "observed_count": len(observed_names),
        "observed_names": observed_names,
        "missing_count": len(missing_names),
        "missing_names": missing_names,
        "complete": not missing_names,
    }


def _evidence_supplement_commands(
    profile: str,
    spec,
    resolved: list[dict],
) -> list[BaselineCommand]:
    """Return safe, deterministic commands for evidence-profile gaps.

    This is deliberately narrow: it fills identity/cross-observation gaps only
    when the target locator is already known.  It never guesses a resource or
    manufactures a target-specific primary metric for an unsupported scope.
    """
    coverage = EvidenceProfile.for_fault(spec, profile).coverage(resolved)
    missing = set(coverage.missing)
    if not missing:
        return []

    existing_commands = " ".join(item.get("command", "") for item in resolved).lower()
    supplements: list[BaselineCommand] = []
    if profile == PROFILE_HOST:
        # host identity + cross-metric probes come from the shared contract in
        # evidence.py so baseline and verification anchor evidence identically.
        for description, argv in host_evidence_supplements(
            spec.fault_target if spec else "", missing, existing_commands,
        ):
            supplements.append(BaselineCommand(description, " ".join(argv)))
        return supplements

    if profile != PROFILE_K8S or spec is None or not spec.names:
        return supplements

    # ``container`` is a ChaosBlade injection scope, not a Kubernetes API
    # resource. Its target identity and conditions are observed through the
    # owning Pod. Other registered scopes retain their Kubernetes resource.
    resource = {
        "node": "node",
        "container": "pod",
    }.get(spec.scope, spec.scope)
    name_var = "{node_name}" if spec.scope == "node" else "{pod_name}"
    namespace = "" if spec.scope == "node" else " -n {namespace}"
    if "target_identity" in missing:
        supplements.append(BaselineCommand(
            "Target identity", f"kubectl get {resource} {name_var}{namespace}",
        ))
    if "independent_cross_metric" in missing:
        # Machine judgement needs machine-readable output (live case
        # inject-3dae7b4f): ``describe`` prints per-container blocks under
        # EVERY section, and once a pod-scoped ``kubectl debug`` has ever
        # run against the target, its permanently-accumulating ``Ephemeral
        # Containers:`` section made the last-wins describe parser read a
        # dead debugger's RestartCount 0 / container ID as the pod's
        # baseline (business truth: RestartCount 329). ``get -o json`` is
        # the API server's authoritative shape and feeds the structured
        # first-container parser instead; ``--show-managed-fields=false``
        # drops the tens-of-KB managedFields blob the same pod carries.
        supplements.append(BaselineCommand(
            "Target conditions",
            f"kubectl get {resource} {name_var}{namespace} -o json"
            " --show-managed-fields=false",
        ))
    return supplements


__all__ = [
    "_BASELINE_NAME_EXPANSION_CAP",
    "_SCOPE_NAME_VAR",
    "_k8s_parse",
    "_resolve_one_baseline",
    "_resolve_templates",
    "_target_coverage",
    "_evidence_supplement_commands",
]
