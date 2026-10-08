"""LLM-driven baseline command derivation (the primary baseline strategy).

Split out of ``baseline_capture.py`` (Phase 2 module split): given the full
skill-case content and the concrete target context, ask the LLM to emit
read-only baseline collection commands, parse/validate them, and self-correct
on execution failure. Depends only on the command data layer (``_commands``),
the per-profile prompt/safety layer (``_baseline_profiles``), and the channel
profile constant — never on ``baseline_capture`` — so there is no import cycle.
"""

from __future__ import annotations

import json
import logging
import re
import shlex

from chaos_agent.agent.nodes.baseline._baseline_profiles import (
    build_baseline_system_prompt,
    validate_command_with_reason,
)
from chaos_agent.agent.nodes.baseline._commands import BaselineCommand
from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S
from chaos_agent.utils.truncation import build_truncation_notice, elided_preview

logger = logging.getLogger(__name__)

# Off-graph retry-prompt preview budgets: the preview keeps both ends at
# 400/600 chars, and the truncation notice fires exactly when the preview
# elides (len > HEAD + TAIL). The three literals are ONE contract — a
# notice firing without elision (or vice versa) would be a false alarm /
# a silent cut — so they are derived, never independently re-typed.
_RETRY_PREVIEW_HEAD_CHARS = 400
_RETRY_PREVIEW_TAIL_CHARS = 600


def _record_aux_llm_call(
    task_id: str, purpose: str, *, request: str, response: str,
    reasoning: str = "", duration_ms: int | None = None,
) -> None:
    """Best-effort archival of an off-graph LLM call. Never raises.

    A missing task_id or store just means "not recording" — this is audit, and
    it must not be able to break baseline derivation.
    """
    if not task_id:
        return
    try:
        from chaos_agent.memory.session_store import get_global_session_store
        store = get_global_session_store()
        if store is not None:
            store.record_aux_llm_call(
                task_id, purpose=purpose, request=request, response=response,
                reasoning=reasoning, duration_ms=duration_ms,
            )
    except Exception as e:
        logger.debug("aux LLM call record skipped (%s): %s", purpose, e)


# Pod-owning workload kinds: a fault scoped to one of these targets a
# workload OBJECT, but the runtime state lives in the pods it owns.
_POD_OWNER_SCOPES = frozenset({"deployment", "statefulset", "daemonset", "service"})


# ---------------------------------------------------------------------------
# class ↔ probe-form consistency (baseline-observation-contract, task 4)
# ---------------------------------------------------------------------------
#
# The derive / retry LLM declares an observation-dimension ``class`` per
# command; the program cross-checks that declaration against the command's
# syntactic form BEFORE execution. Case #61 shipped ``kubectl exec -l
# app=<label> -n <ns> -- id`` (a selector form exec cannot honor) as a
# ``container_internal`` probe; the exec-target form gate (task 1) now
# rejects the syntax, and this class gate rejects the intent-vs-shape
# mismatch that let it through in the first place.
#
# Design constraints (see design.md decision 4):
#   * Closed enum, member-level predicates — no aggregate slogans like
#     "must be a kubectl command"; each class names the exact forms that
#     satisfy it, and every other form is a mismatch with a reason.
#   * Derivation is LABEL-ONLY: when the LLM omits ``class`` (or an old
#     output shape predates the field), the program derives a value from
#     the form and stamps it — derivation never rejects. Same fail-open
#     posture as ``_split_retry_decisions``'s verdict default (task 5.2).
#   * Rejection reuses the Case #63 ``validate_command_with_reason``
#     channel: the reason is surfaced as a fact to the verifier and to
#     post-hoc analysis, not just logged.

_CLASS_ENUM = frozenset({
    "container_internal", "api_object", "node_level", "host_level",
})

# kubectl resource tokens that name the NODE kind (not a pod, not a
# workload). ``no`` is the standard short alias (kubectl get no).
_NODE_KIND_TOKENS = frozenset({"node", "nodes", "no"})

# kubectl subcommands that read an API object (as opposed to ``exec``,
# which enters a container's namespaces).
_API_READ_SUBCOMMANDS = frozenset({"get", "describe", "top"})

# Label-selector flags whose VALUE bounds the read. Both the short (``-l``)
# and long (``--selector``) spellings, in space-separated and ``=``-attached
# forms (handled in ``_node_read_unbounded_reason``).
_SELECTOR_FLAGS = frozenset({"-l", "--selector"})

# Refusal reason for the bound gate (fourth dimension of
# ``_validate_and_filter_commands``). Kept as a module constant so the gate
# and its tests quote one string.
_UNBOUND_DESCRIBE_REASON = (
    "``kubectl describe node`` with an existence-only label selector "
    "(``-l <key>`` with no ``=<value>``) describes EVERY node carrying that "
    "label — verbose per-node output for the whole cluster, which floods the "
    "baseline context and is not a node-level observation of the target. Bind "
    "it to the target node: name it (``kubectl describe node <node-name>``) "
    "or use an equality selector (``-l kubernetes.io/hostname=<node-name>``). "
    "To ENUMERATE a subset cheaply use the tabular form ``kubectl get nodes "
    "-l <key>``, which this gate does not touch."
)


def _tokenize_for_class(command: str) -> list[str] | None:
    """Best-effort shlex tokenization for class-shape judgement.

    Returns None when the command is not parseable — the shape gate then
    fails open to "cannot classify" (the validator has already rejected
    unparseable commands upstream, so this branch is defensive).
    """
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _is_node_scoped_api_read(tokens: list[str]) -> bool:
    """True iff the tokens are ``kubectl get|describe|top node[s] ...`` OR
    ``kubectl get pods ... --field-selector spec.nodeName=...`` (the two
    forms the derive prompt teaches for node-level API reads).
    """
    if len(tokens) < 3 or tokens[0] != "kubectl":
        return False
    if tokens[1] not in _API_READ_SUBCOMMANDS:
        return False
    # Direct node-kind target: ``kubectl describe node <name>``.
    if tokens[2] in _NODE_KIND_TOKENS:
        return True
    # Node-scoped pod query: ``kubectl get pods --field-selector
    # spec.nodeName=<n>`` — the field-selector binds the query to a node.
    joined = " ".join(tokens)
    if "spec.nodeName=" in joined:
        return True
    return False


def _derive_command_class_default(
    command: str, mode: str, profile: str,
) -> str | None:
    """Infer the observation-dimension class from the command's form.

    LABEL-ONLY: returns a class tag when the form is unambiguous, None
    when it cannot classify. NEVER rejects (a missing / underivable tag
    is not a validation failure — old outputs and registry templates
    both arrive without one, and refusing them would break the merge
    with pre-existing behaviour).

    Rules (member-level, mirrors the derive-prompt teaching):
      * profile=host → ``host_level`` (only class the host channel can
        produce).
      * ``kubectl exec {debug_pod} ...`` (or ``mode == debug_two_step``)
        → ``node_level`` — the debug pod is the escape hatch into the
        node's namespaces.
      * any other ``kubectl exec ...`` → ``container_internal``.
      * ``kubectl get|describe|top node[s] ...`` or the node-scoped
        field-selector form → ``node_level``.
      * any other ``kubectl get|describe|top ...`` → ``api_object``.
    """
    if profile == PROFILE_HOST:
        return "host_level"
    if profile != PROFILE_K8S:
        return None
    tokens = _tokenize_for_class(command)
    if not tokens or tokens[0] != "kubectl" or len(tokens) < 2:
        return None
    sub = tokens[1]
    if sub == "exec":
        # A debug-pod escape is node_level regardless of the ``mode``
        # field (mode can lag behind the placeholder in stale output).
        if "{debug_pod}" in command or mode == "debug_two_step":
            return "node_level"
        return "container_internal"
    if sub in _API_READ_SUBCOMMANDS:
        if _is_node_scoped_api_read(tokens):
            return "node_level"
        return "api_object"
    return None


def _command_class_shape_reason(
    command: str,
    mode: str,
    declared_class: str,
    profile: str,
) -> str | None:
    """Reason the declared ``class`` mismatches the command's form, or
    None when the declaration is consistent with the shape.

    Member-level mapping (each class names the forms it accepts; every
    other form is a mismatch with a stated reason):
      * ``container_internal`` — k8s profile ONLY, ``kubectl exec`` with
        a non-debug-pod target (``{debug_pod}`` is the node escape, not
        a container probe) and ``mode != debug_two_step``.
      * ``api_object`` — k8s profile ONLY, ``kubectl get|describe|top``
        against a NON-node kind (a node target is ``node_level``).
      * ``node_level`` — k8s profile ONLY, one of:
          - ``kubectl get|describe|top node[s] ...``
          - ``kubectl get pods ... --field-selector spec.nodeName=...``
          - ``kubectl exec {debug_pod} ...`` (equivalently,
            ``mode == debug_two_step``)
      * ``host_level`` — host profile ONLY (any command shape; the
        profile is the discriminator).

    An unknown class value is rejected with the enum surfaced. This
    predicate is self-consistent (returns the same verdict on every
    call for the same input) — no caller-order dependence, per the
    readonly.py predicate discipline.
    """
    if declared_class not in _CLASS_ENUM:
        return (
            f"declared class '{declared_class}' is not in the closed enum "
            f"{sorted(_CLASS_ENUM)}"
        )

    tokens = _tokenize_for_class(command)
    if tokens is None:
        # Unparseable — validate_command_with_reason has already rejected
        # upstream; this branch is defensive and mirrors the fail-open
        # posture of _derive_command_class_default.
        return None

    if profile == PROFILE_HOST:
        if declared_class != "host_level":
            return (
                f"class '{declared_class}' cannot be observed on the host "
                f"profile — host commands observe host_level only"
            )
        return None

    if profile != PROFILE_K8S:
        # Unknown profile — validate_command_with_reason rejects upstream.
        return None

    # k8s profile: host_level is out of domain.
    if declared_class == "host_level":
        return (
            "class 'host_level' cannot be observed on the k8s profile — "
            "use api_object / node_level / container_internal instead"
        )

    if not tokens or tokens[0] != "kubectl" or len(tokens) < 2:
        return (
            f"class '{declared_class}' requires a well-formed kubectl "
            f"command on the k8s profile"
        )
    sub = tokens[1]
    is_debug_exec = (
        sub == "exec" and ("{debug_pod}" in command or mode == "debug_two_step")
    )

    if declared_class == "container_internal":
        if sub != "exec":
            return (
                f"class 'container_internal' requires ``kubectl exec`` form "
                f"(got subcommand '{sub}')"
            )
        if is_debug_exec:
            return (
                "class 'container_internal' cannot target the debug pod — "
                "``{debug_pod}`` escapes into the NODE's namespaces, which "
                "is class 'node_level'"
            )
        return None

    if declared_class == "api_object":
        if sub not in _API_READ_SUBCOMMANDS:
            return (
                f"class 'api_object' requires ``kubectl get|describe|top`` "
                f"form (got subcommand '{sub}')"
            )
        if _is_node_scoped_api_read(tokens):
            return (
                "class 'api_object' does not cover node-scoped reads — a "
                "``kubectl describe node`` / ``--field-selector "
                "spec.nodeName=`` form observes the NODE and belongs to "
                "class 'node_level'"
            )
        return None

    # declared_class == "node_level"
    if is_debug_exec:
        return None
    if _is_node_scoped_api_read(tokens):
        return None
    return (
        "class 'node_level' requires one of: ``kubectl describe/top node``, "
        "``kubectl get pods --field-selector spec.nodeName=<n>``, or "
        f"``kubectl exec {{debug_pod}} ...`` (got '{command[:80]}')"
    )


def _node_read_unbounded_reason(
    command: str, tokens: list[str] | None,
) -> str | None:
    """Reason a ``kubectl describe node`` is bound to no specific node, or
    None when the read is bounded (or is not the gated form).

    A ``describe node`` emits VERBOSE per-node output, so binding it to an
    unbounded node set floods the baseline context. ``-l <key>`` with no
    ``=<value>`` (an existence-only selector) matches every node carrying that
    label — and ``kubernetes.io/hostname`` is on all of them. The bound is
    programmatically decidable, so the pipeline enforces it here instead of
    teaching the model to append ``=value``.

    Live case inject-6ebf341c emitted ``kubectl describe node -l
    kubernetes.io/hostname`` (idx=96); the receipt (idx=109) was 429,784 chars
    — the full ``describe`` of ~40 nodes, the single largest message in the
    task — fed into the derive context and stored whole, the dominant driver
    of that run's context growth.

    SCOPE (deliberately narrow, per the honesty discipline — do not reject
    what the evidence does not show):
      * ONLY ``describe`` is gated. ``get``/``top`` are tabular (one line per
        node) and legitimately use existence-only selectors to ENUMERATE a
        subset — e.g. ``kubectl get nodes -l
        node-role.kubernetes.io/control-plane`` in the AZ-network-partition
        playbook. That is a cheap list, not a flood, so those verbs are exempt.
      * ONLY the node-kind form (``tokens[2]`` in ``_NODE_KIND_TOKENS``).
        Pod/workload reads use label selectors as their normal targeting and
        are out of scope.
      * Equality selectors (``-l key=value``) and positional node names
        (``describe node <name>``) are bounded and pass.
      * Fail-open on unparseable tokens, matching the other class predicates.
    """
    if tokens is None or len(tokens) < 4 or tokens[0] != "kubectl":
        return None
    if tokens[1] != "describe":
        return None
    if tokens[2] not in _NODE_KIND_TOKENS:
        return None
    i = 3
    while i < len(tokens):
        tok = tokens[i]
        if tok in _SELECTOR_FLAGS:
            # space-separated form: ``-l VALUE``
            value = tokens[i + 1] if i + 1 < len(tokens) else ""
            if "=" not in value:
                return _UNBOUND_DESCRIBE_REASON
            i += 2
            continue
        if tok.startswith("--selector="):
            if "=" not in tok.split("=", 1)[1]:
                return _UNBOUND_DESCRIBE_REASON
            i += 1
            continue
        if tok.startswith("-l") and len(tok) > 2:
            # attached short form: ``-lVALUE``
            if "=" not in tok[2:]:
                return _UNBOUND_DESCRIBE_REASON
            i += 1
            continue
        i += 1
    return None


def _build_target_context(
    scope: str,
    target: str,
    action: str,
    namespace: str,
    names: tuple[str, ...],
    labels: dict[str, str] | None,
    pod_selector: dict[str, str] | None,
) -> str:
    """Assemble the target-context block shared by the derive & retry prompts.

    #16 fix A (Identity axiom): the lines carry kind semantics now.
    ``Resource names (kind=...)`` states WHICH kind the names belong to —
    a deployment name is a workload object, not a pod instance, and the
    derive LLM used to treat the two as interchangeable (inventing the
    label ``app=<deployment-name>`` for pod-level queries). The
    authoritative ``Pod label selector`` line (discovered from the
    workload's own ``spec.selector`` by baseline_capture) removes the
    guess entirely.
    """
    lines = [f"Fault type: {scope}-{target}-{action}", f"Fault scope: {scope}"]
    if namespace:
        lines.append(f"Namespace: {namespace}")
    if names:
        lines.append(f"Resource names (kind={scope}): {', '.join(names[:5])}")
    if labels:
        lines.append(
            "Label selector: " + ", ".join(f"{k}={v}" for k, v in labels.items())
        )
    if pod_selector:
        lines.append(
            "Pod label selector (authoritative, read from the "
            f"{scope}'s own spec.selector): "
            + ", ".join(f"{k}={v}" for k, v in pod_selector.items())
        )
    return "\n".join(lines)


def _identity_rules_block(
    scope: str, names: tuple[str, ...], pod_selector: dict[str, str] | None,
) -> str:
    """#16 fix A: identity anchoring rules appended to derive/retry prompts.

    With a discovered selector: use it verbatim, never invent one. Without
    one on a pod-owner scope: forbid guessing (a wrong selector yields an
    empty, useless baseline — R10 replay: 3 of 4 "succeeded" observations
    were exactly this form) and anchor on the workload object instead.
    """
    if pod_selector:
        return (
            "Identity rules: the Pod label selector above was read from the "
            "target workload's own spec — it is the AUTHORITATIVE selector "
            "for its pods. Use it verbatim (``-l k=v``) in every pod-level "
            "query; NEVER invent or guess label keys or values for this "
            "target.\n\n"
        )
    if names and scope in _POD_OWNER_SCOPES:
        return (
            "Identity rules: the resource names above are workload objects, "
            "NOT pod names, and no pod label selector could be resolved for "
            "them. Do NOT guess a label selector for pod-level queries — a "
            "wrong selector yields an empty, useless baseline. Anchor "
            "pod-level state on the workload object itself (describe/get "
            "the named workload; its replica/event status is the valid "
            "baseline) or on objects that name the workload explicitly.\n\n"
        )
    return ""


async def _llm_derive_baseline_commands(
    llm,
    skill_case_content: str,
    scope: str,
    target: str,
    action: str,
    *,
    channel: str = "kubeconfig",
    profile: str = "k8s",
    namespace: str = "",
    names: tuple[str, ...] = (),
    labels: dict[str, str] | None = None,
    pod_selector: dict[str, str] | None = None,
    task_id: str = "",
) -> list[BaselineCommand]:
    """Let LLM derive baseline collection commands from full skill content.

    The SystemMessage is assembled per *channel* (universal core + capability
    fragment) by ``build_baseline_system_prompt``; the HumanMessage carries
    the concrete task context (actual namespace / resource names / labels).
    The LLM emits concrete commands directly (no template variables, except
    ``{debug_pod}`` for k8s node host-level metrics).

    Falls back to empty list on any failure (triggers Registry fallback).
    """
    if not llm or not skill_case_content:
        return []

    # Single-shot structured derivation: reasoning tokens only add latency
    # (264.6s → 3.5s with an explicit disable, bench_thinking.py). Real
    # ChatOpenAI clients get the dialect-correct disable flag; injected
    # test fakes pass through untouched.
    from chaos_agent.agent.factory import with_thinking_disabled
    llm = with_thinking_disabled(llm)

    # Build target context so the LLM embeds the correct resource
    # names/namespace/labels directly into each command (#16 fix A: with
    # kind semantics + the authoritative pod selector when discovered).
    target_context = _build_target_context(
        scope, target, action, namespace, names, labels, pod_selector,
    )
    _identity_rules = _identity_rules_block(scope, names, pod_selector)

    human_prompt = (
        f"{target_context}\n\n"
        f"{_identity_rules}"
        f"<skill-case>\n{skill_case_content}\n</skill-case>\n\n"
        "Based on the skill-case content, reason about what states this fault "
        "will modify. The baseline_facts and symptoms sections describe expected "
        "changes; injection verification provides additional hints. "
        "Generate read-only commands to capture the pre-injection baseline for "
        "each affected state, using the ACTUAL resource values above (embed them "
        "directly — do not emit placeholders).\n"
    )

    try:
        import time as _time

        from langchain_core.messages import SystemMessage as SM, HumanMessage as HM
        _sys = build_baseline_system_prompt(channel)
        _t0 = _time.perf_counter()
        response = await llm.ainvoke([SM(content=_sys), HM(content=human_prompt)])
        _dt_ms = int((_time.perf_counter() - _t0) * 1000)
        raw = response.content if hasattr(response, "content") else str(response)
        # Audit trail: this call is off the main graph and never enters
        # ``messages`` (deliberately — it must not pollute the ReAct context),
        # so record it separately or it is invisible to post-hoc analysis. This
        # is the call that took 73s in task-61915e37 with no way to inspect it.
        _record_aux_llm_call(
            task_id, "baseline_derive",
            request=f"{_sys}\n\n---\n\n{human_prompt}",
            response=raw,
            reasoning=str(getattr(response, "reasoning_content", "") or ""),
            duration_ms=_dt_ms,
        )
        commands = _parse_llm_json_output(raw)
        accepted, rejected = _validate_and_filter_commands(commands, profile)
        _surface_rejected_derivations(task_id, rejected)
        return accepted
    except Exception as e:
        logger.warning(f"LLM baseline derivation failed: {e}")
        return []


_LLM_BASELINE_MAX_RETRIES = 3


async def _llm_retry_failed_commands(
    llm,
    skill_case_content: str,
    scope: str,
    target: str,
    action: str,
    failed_observations: list[dict],
    *,
    channel: str = "kubeconfig",
    profile: str = "k8s",
    namespace: str = "",
    names: tuple[str, ...] = (),
    labels: dict[str, str] | None = None,
    pod_selector: dict[str, str] | None = None,
    task_id: str = "",
    already_tried: tuple[str, ...] = (),
) -> dict:
    """Judge and re-derive failed or empty baseline commands with feedback.

    Called when LLM-generated commands exit non-zero OR complete with
    empty output (#16 fix C — an empty success anchored on nothing: wrong
    selector, wrong name, or an asset the approved plan only creates
    during execute). The core insight (first-principles, from #31): a
    non-zero exit is channel signal, not a semantic verdict —
    existence/residue pre-checks report absence THROUGH
    non-zero exits, and for those the observation IS the baseline value.
    So the retry contract is a per-command *semantic verdict*, not a
    mandatory replacement:

      * ``expected_absence`` — the command is correct and its non-zero
        exit or empty output reports the expected pre-injection absence
        (per the skill case, or because the approved plan creates the
        asset during execute). The observation is kept as-is; retrying it
        can never converge (#31: three identical retries burned ~35s on
        exactly this).
      * ``replace`` — a true failure (wrong flags/path/resource/label
        selector); emit ONE corrected replacement per the same rules as
        before.

    Return contract: ``{"expected": [(obs, reason), ...],
    "replace": [BaselineCommand, ...],
    "rejected_replacements": [(obs, cmd_text, reason), ...]}``.
    Entries without a ``verdict`` field default to ``replace`` (legacy
    LLM output shape, and the old behavior of "every failure gets a
    corrected replacement). ``rejected_replacements`` carries verdict=
    replace entries REFUSED by the double gate (shape + class +
    whitelist); the caller keeps the original obs in the failed set and
    stamps the reason for the next round's feedback loop (task 5.1).

    ``already_tried`` carries the commands earlier retries produced. Each retry
    is an independent call with no memory of the previous one, so without this
    the prompt for retry 2 is byte-identical to retry 1 and the model can only
    resample. task-fc64c982: three retries, the first two both emitting
    ``kubectl exec {debug_pod} -- pidof containerd`` against a node that had no
    debug pod, 71s spent before the third happened to try something else.
    """
    if not llm or not failed_observations:
        return {"expected": [], "replace": []}

    # Same latency rationale as the initial derivation: retries are
    # single-shot structured calls; the error feedback in the prompt does
    # the corrective work, not the reasoning channel.
    from chaos_agent.agent.factory import with_thinking_disabled
    llm = with_thinking_disabled(llm)

    error_lines = []
    for obs in failed_observations:
        # Complete evidence presentation: the kubewiz channel merges
        # stderr into stdout (see _KUBECTL_ERROR_MARKERS), so the
        # absence evidence ("(NotFound)", "No such file") may live in
        # EITHER stream depending on the channel. Show both previews
        # and let the model judge from full evidence — no machine-side
        # pre-digestion of what the non-zero exit "means".
        #
        # Truncation contract (off-graph aux call): this prompt is NOT
        # part of the main message history, so the context compactor
        # never sees it — there is no cache safety net here. The preview
        # keeps BOTH ends (the semantic evidence of an expected_absence
        # verdict may live at either end), and a shared notice
        # (kind=baseline-evidence) points at the full original in
        # state.baseline_data: silent truncation would be the ONLY kind
        # with no retrieval path at all.
        stdout_raw = obs.get("stdout") or ""
        stderr_raw = obs.get("stderr") or ""
        stdout_preview = elided_preview(
            stdout_raw, _RETRY_PREVIEW_HEAD_CHARS, _RETRY_PREVIEW_TAIL_CHARS,
        )
        stderr_preview = elided_preview(
            stderr_raw, _RETRY_PREVIEW_HEAD_CHARS, _RETRY_PREVIEW_TAIL_CHARS,
        )
        if len(stdout_raw) > _RETRY_PREVIEW_HEAD_CHARS + _RETRY_PREVIEW_TAIL_CHARS:
            stdout_preview += build_truncation_notice(
                "baseline-evidence", len(stdout_raw), unit="characters",
            )
        if len(stderr_raw) > _RETRY_PREVIEW_HEAD_CHARS + _RETRY_PREVIEW_TAIL_CHARS:
            stderr_preview += build_truncation_notice(
                "baseline-evidence", len(stderr_raw), unit="characters",
            )
        error_lines.append(
            f"- Purpose: {obs.get('description', '(unknown)')}\n"
            f"  Command: `{obs.get('command', '')}`\n"
            f"  exit_code={obs.get('exit_code')}\n"
            f"  stdout: {stdout_preview or '(empty)'}\n"
            f"  stderr: {stderr_preview or '(empty)'}"
        )
        # Task 5.1 feedback-loop closure: when the previous retry round
        # proposed a replacement that the shape/class gate REFUSED, the
        # refusal reason is stamped onto the obs by baseline_capture.
        # Showing it here means the next round's LLM sees WHY its prior
        # substitution was rejected and can correct the shape instead of
        # re-emitting the same refused form (Case #61's launder-through-
        # retry loop). Absent on the first round and on obs whose
        # replacements passed the gate — no visual noise for the common
        # path.
        _rejection = obs.get("retry_rejection_reason")
        if _rejection:
            error_lines[-1] += (
                f"\n  Previous retry replacement REFUSED by the shape/class "
                f"gate: {_rejection}"
            )
    error_feedback = "\n".join(error_lines)

    target_context = _build_target_context(
        scope, target, action, namespace, names, labels, pod_selector,
    )
    _identity_rules = _identity_rules_block(scope, names, pod_selector)

    _failed_n = len(failed_observations)
    _tried_block = ""
    if already_tried:
        _tried_lines = "\n".join(f"- `{c}`" for c in already_tried)
        _tried_block = (
            "\nAlready attempted in earlier retries and FAILED — do not emit "
            "these again, nor a variant that would fail the same way. If every "
            "approach of one kind has failed (e.g. every command needing a debug "
            f"pod), change approach:\n{_tried_lines}\n"
        )
    human_prompt = (
        f"{target_context}\n\n"
        f"{_identity_rules}"
        f"<skill-case>\n{skill_case_content}\n</skill-case>\n\n"
        f"Exactly {_failed_n} baseline command(s) exited non-zero or "
        "completed with EMPTY output. "
        "All OTHER baseline commands SUCCEEDED and are already kept — "
        "do NOT regenerate them.\n\n"
        "IMPORTANT: a non-zero exit is NOT automatically a failure. "
        "Existence and residue pre-checks report absence THROUGH non-zero "
        "exits (\"No such file or directory\", \"Unit ... could not be "
        "found\", kubectl \"Error from server (NotFound)\") — for those the "
        "observation IS the baseline value: pre-injection absence is "
        "exactly what the post-injection comparison needs, and re-running "
        "the same check can never change it.\n\n"
        "The same judgment applies to EMPTY output (exit 0, \"No resources "
        "found\" or an empty items list): emptiness is EITHER the expected "
        "pre-injection state — e.g. the approved plan creates the asset "
        "during execute, so its pre-injection absence IS the baseline "
        "value — OR a wrong identity (wrong label selector, wrong "
        "resource name) that must be REPLACED with a command targeting "
        "the actual fault target.\n\n"
        f"{error_feedback}\n"
        f"{_tried_block}\n"
        "For EACH failed-or-empty command above, output ONE JSON entry "
        "judging its semantics against the skill case:\n"
        "- {\"verdict\": \"expected_absence\", \"reason\": \"<why the non-zero "
        "exit or empty output is the expected pre-injection form, citing "
        "the skill case>\"} — "
        "when the command is CORRECT and its failure-or-emptiness reports "
        "expected absence (conflicts/target-state pre-checks and "
        "planned-creation assets are the usual cases).\n"
        "- {\"verdict\": \"replace\", \"reason\": \"<what was wrong>\", "
        "\"command\": \"<corrected command>\", \"description\": \"<same "
        "purpose>\", \"class\": \"<container_internal|api_object|"
        "node_level|host_level>\"} — when it is a TRUE failure (wrong "
        "flags, wrong path, wrong resource, wrong label selector), using "
        "the ACTUAL resource values above. The ``class`` field MUST match "
        "the corrected command's form (see the Output Contract in the "
        "system prompt); a mismatch is rejected before execution.\n"
        "Rules:\n"
        "- Fix ONLY the listed non-zero/empty commands; do NOT add new "
        "observation dimensions or regenerate succeeded ones.\n"
        "- The replacement MUST stay in the SAME observation dimension as "
        "the failed command. A ``container_internal`` failure (state "
        "inside the target container) MUST be replaced with another "
        "container-internal probe, NOT with an ``api_object`` probe that "
        "only reads Kubernetes metadata — Case #61 shipped exactly that "
        "regression (a failed container exec replaced with ``kubectl get "
        "pods -o jsonpath=...``), which \"succeeded\" with exit 0 while "
        "the container dimension went unmeasured.\n"
        "- Pod names seen in failed commands or their error output may belong "
        "to an ALREADY-DELETED carrier — between retries the runtime replaces "
        "debug pods, so a literal pod name from earlier output is stale. Emit "
        "the ``{debug_pod}`` placeholder (resolved at execution time); never "
        "a literal debug pod name. The same applies to the fault target's "
        "pod: emit ``{target_pod}`` (resolved at execution time to a pod "
        "owned by the target workload); never a literal target pod name "
        "from earlier output, and NEVER a selector form (``kubectl exec -l "
        "app=... -- <cmd>``) — exec targets ONE specific pod, and the "
        "selector form is rejected by the shape gate.\n"
        "- Do NOT mark a true failure as expected_absence just because "
        "retrying seems futile — if the command itself is wrong, replace "
        "it.\n"
        "- A wrong path also prints \"No such file\": judge from the skill "
        "case's intent, not from the error text alone.\n"
        f"- Output EXACTLY {_failed_n} entries as a JSON list, no other text."
    )

    try:
        import time as _time

        from langchain_core.messages import SystemMessage as SM, HumanMessage as HM
        _sys = build_baseline_system_prompt(channel)
        _t0 = _time.perf_counter()
        response = await llm.ainvoke([SM(content=_sys), HM(content=human_prompt)])
        _dt_ms = int((_time.perf_counter() - _t0) * 1000)
        raw = response.content if hasattr(response, "content") else str(response)
        # Same audit as the primary derive: the retry is a distinct off-graph
        # LLM call and part of what made the baseline phase slow.
        _record_aux_llm_call(
            task_id, "baseline_retry",
            request=f"{_sys}\n\n---\n\n{human_prompt}",
            response=raw,
            reasoning=str(getattr(response, "reasoning_content", "") or ""),
            duration_ms=_dt_ms,
        )
        return _split_retry_decisions(
            _parse_llm_json_output(raw), failed_observations, profile, task_id,
            already_tried=already_tried,
        )
    except Exception as e:
        logger.warning("LLM baseline retry failed: %s", e)
        return {"expected": [], "replace": []}


def _split_retry_decisions(
    decisions: list[dict],
    failed_observations: list[dict],
    profile: str,
    task_id: str = "",
    already_tried: tuple[str, ...] = (),
) -> dict:
    """Split retry LLM output into semantic verdicts.

    Entries pair positionally with ``failed_observations`` (the prompt asks
    for exactly one entry per command). An entry without a ``verdict``
    field defaults to ``replace`` — the legacy output shape where every
    failure got a corrected replacement — so old-format LLM output still
    behaves exactly like the pre-verdict retry.

    Returns a dict with three keys:
      * ``expected``: ``[(obs, reason), ...]`` — verdict=expected_absence.
      * ``replace``: ``[BaselineCommand, ...]`` — verdict=replace that
        passed the same double gate as the primary derive path
        (whitelist + shape gate + class gate via
        ``_validate_and_filter_commands``).
      * ``rejected_replacements``: ``[(obs, cmd_text, reason), ...]`` —
        verdict=replace entries REFUSED by the double gate. Task 5.1: the
        caller (baseline_capture) uses this list to keep the ORIGINAL
        failed observation in the failed set (rather than dropping it on
        the floor as before) and to write ``reason`` back onto the obs so
        the next retry round's error_feedback shows the LLM why its
        proposed replacement was refused. Without this channel, a
        rejected replacement silently deleted the failed observation from
        the merge, letting a half-way substitution (Case #61: a failed
        container-internal exec replaced by an api_object pod listing)
        disappear from view instead of being corrected.

    ``already_tried`` is forwarded to the gate as its fourth dimension. It
    used to reach the LLM only as prompt text, which made "do not emit these
    again" an appeal to model self-discipline rather than an enforced
    constraint — inject-6ebf341c retry 3 re-emitted retry 1's ``kubectl get
    daemonset kube-proxy -n kube-system -o wide`` verbatim, burning an aux call
    and a kubectl round trip, and the DaemonSet dimension stayed missing so the
    receipt read ``5/7`` and confidence settled at ``partial``.
    """
    expected: list[tuple[dict, str]] = []
    # Keep the pairing obs ↔ raw entry so a downstream rejection can be
    # traced back to the observation it was meant to replace. Position-
    # aware filtering (one _validate_and_filter_commands call per entry)
    # is a small cost — retry batches are ≤ a handful of commands — and
    # the alternative (batch call, then reconstruct positions from the
    # rejected command text) is brittle when the LLM emits duplicates.
    replace_raw_pairs: list[tuple[dict, dict, str]] = []
    for entry, obs in zip(decisions, failed_observations):
        verdict = entry.get("verdict")
        if verdict == "expected_absence":
            expected.append((obs, str(entry.get("reason", ""))[:500]))
        elif verdict == "replace" or verdict is None:
            # Dimension preservation (W-67-4). The retry prompt already
            # legislates "do NOT add new observation dimensions", but the
            # class↔form gate below only checks a replacement's declared
            # class against ITS OWN command's shape — it never compares
            # against the class of the observation being replaced. So a
            # replacement refused under one class could be re-submitted
            # verbatim under another and walk straight through.
            #
            # Case #67: ``kubectl exec <victim-pod> -- cat /proc/diskstats``
            # was refused as ``node_level`` (correctly — that class requires a
            # node channel), then accepted one round later as
            # ``container_internal``, and the receipt read ``5/5 commands
            # succeeded`` while the node-level dimension the plan asked for
            # was never measured through a node channel. Case #60 documented
            # the same gap as a deferred boundary; #67 is its field sample.
            #
            # The class a retry may change is the COMMAND, never the
            # dimension. Pin the class (and the description that names it) to
            # the observation being replaced and let the existing form gate
            # judge the result: a replacement that genuinely measures the
            # original dimension passes, one that substitutes a different
            # dimension is refused through ``rejected_replacements`` and
            # stays visible. ``obs["_class"]`` is None for registry-sourced
            # observations and legacy shapes — then the LLM's own declaration
            # stands, exactly as before.
            declared_class = entry.get("class")
            pinned_class = obs.get("_class") or declared_class
            relabel_note = ""
            if (
                pinned_class
                and declared_class
                and declared_class != pinned_class
            ):
                relabel_note = (
                    "the replacement re-declared the observation dimension "
                    f"as '{declared_class}', but a retry may repair the "
                    f"command only — the dimension stays '{pinned_class}' "
                    "(what the plan asked to measure)"
                )
            replace_raw_pairs.append((obs, {
                "description": (
                    obs.get("description") or entry.get("description", "")
                ),
                "command": entry.get("command", ""),
                # Normalize absent/empty mode to "simple" for consistency
                # with the initial derive path (where the LLM emits mode
                # explicitly). The auto-correction in
                # _validate_and_filter_commands handles {debug_pod} →
                # debug_two_step regardless, but a clean default avoids
                # an empty-string mode propagating into BaselineCommand.
                "mode": entry.get("mode") or "simple",
                # ``class`` is optional in the retry schema too — absent
                # means "derive from form" (label-only, never rejects).
                # Present (or pinned) means the shape gate cross-checks it
                # against the corrected command's form; a mismatch is
                # filtered downstream by _validate_and_filter_commands.
                "class": pinned_class,
            }, relabel_note))
        # Unknown verdicts are dropped: the loop re-collects the unjudged
        # observation next round (it is still non-zero and unmarked).

    replace_accepted: list[BaselineCommand] = []
    rejected_replacements: list[tuple[dict, str, str]] = []
    surface_rejected: list[tuple[str, str]] = []
    for obs, raw, relabel_note in replace_raw_pairs:
        accepted_one, rejected_one = _validate_and_filter_commands(
            [raw], profile, already_tried=already_tried,
        )
        if accepted_one:
            if relabel_note:
                # Accepted, but only because the corrected command really does
                # measure the pinned dimension — the LLM had mislabelled it.
                # Log the correction rather than absorbing it silently: the
                # class on the resulting observation is the pinned one, not
                # the one the model declared.
                logger.warning(
                    "LLM baseline retry: %s; kept the pinned class on %r",
                    relabel_note, raw.get("command", "")[:120],
                )
            replace_accepted.extend(accepted_one)
        elif rejected_one:
            _cmd_text, _reason = rejected_one[0]
            if relabel_note:
                # Name BOTH failures. The form mismatch alone reads as "pick a
                # command that matches your declared class", which is exactly
                # the invitation the relabel answered — the next round would
                # relabel again. The dimension clause says what is not up for
                # negotiation, so the feedback loop can actually converge.
                _reason = f"{_reason}; additionally, {relabel_note}"
            rejected_replacements.append((obs, _cmd_text, _reason))
            surface_rejected.append((_cmd_text, _reason))
        else:
            # Garbage output (empty command, malformed entry): route
            # through the rejected channel so the obs STAYS in all_pairs
            # and gets another chance next round. Without this, the obs
            # silently evaporates from the merge (not in success_pairs,
            # not in absence_pairs, not in rejected_pairs, not in the
            # retry zip) and the dimension is permanently lost.
            _cmd_text = (raw.get("command") or "").strip() or "(empty)"
            _reason = "replacement command was empty or malformed"
            rejected_replacements.append((obs, _cmd_text, _reason))
            surface_rejected.append((_cmd_text, _reason))

    # Symmetry with the primary derive path: a retry-round refusal is
    # surfaced the same way (Case #63 audit found this path still dropped
    # rejections on the floor after the primary path was fixed).
    _surface_rejected_derivations(task_id, surface_rejected)
    return {
        "expected": expected,
        "replace": replace_accepted,
        "rejected_replacements": rejected_replacements,
    }


def _parse_llm_json_output(raw: str) -> list[dict]:
    """Robustly parse JSON from LLM output.

    Handles: pure JSON, JSON in markdown code blocks, trailing text.
    """
    if not raw:
        return []

    # Try direct parse
    text = raw.strip()
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code block
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group(1).strip())
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # Try finding first [ ... ] block
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            result = json.loads(text[start:end + 1])
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return []


def _surface_rejected_derivations(
    task_id: str, rejected: list[tuple[str, str]]
) -> None:
    """Record read-only-gate refusals as FACTS in the message archive.

    The verifier's context IS the message stream, so this tells it (and
    any post-hoc reader) which derived probes never ran and why, without
    prescribing any behavior: a write-form metric simply is not
    collectible in this read-only channel, and its anchor belongs to the
    plan's execute phase (Case #63 pattern: the fsync-latency anchor was
    collected at Step 1). Shared by the primary derive path and the retry
    path so a refusal is surfaced identically no matter which round
    produced it. Fire-and-forget: a sync failure must never break
    derivation (same guarantee as sync_node_status_to_session itself).
    """
    if not rejected or not task_id:
        return
    try:
        from chaos_agent.agent.nodes.store._store_sync import (
            sync_node_status_to_session,
        )
        sync_node_status_to_session(
            {"task_id": task_id},
            "baseline_capture",
            "LLM-derived command(s) rejected by the read-only gate: "
            + "; ".join(
                f"'{cmd[:120]}' ({reason})" for cmd, reason in rejected
            ),
            detail={
                "rejected": [
                    {"command": cmd, "reason": reason}
                    for cmd, reason in rejected
                ],
            },
        )
    except Exception as e:
        logger.debug("rejection-record sync skipped: %s", e)


def _command_fingerprint(command: str) -> str:
    """Normalize a command for already-attempted comparison.

    Folds whitespace and the ORDER of independent flags. It never rewrites an
    argument's VALUE: two commands differing in a real argument (another
    namespace, another path) are different observations and must not be treated
    as a repeat. Under-folding is the safe direction here — a false negative
    costs one redundant probe, a false positive permanently loses a baseline
    dimension.

    Positional tokens keep their order; ``--flag value`` and ``--flag=value``
    both canonicalize to ``--flag=value`` and the flag set is sorted, so
    ``-o wide -n kube-system`` and ``-n kube-system -o wide`` fingerprint
    alike. A boolean flag immediately followed by a positional token binds them
    (``-n foo``); that is a mis-grouping, but it is applied identically to both
    sides of every comparison, so the fingerprint stays consistent.
    """
    tokens = command.split()
    positional: list[str] = []
    flags: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-") and tok != "-":
            if "=" in tok:
                flags.append(tok)
            elif i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                flags.append(f"{tok}={tokens[i + 1]}")
                i += 1
            else:
                flags.append(tok)
        else:
            positional.append(tok)
        i += 1
    return " ".join(positional) + " | " + " ".join(sorted(flags))


def _validate_and_filter_commands(
    commands: list[dict],
    profile: str,
    already_tried: tuple[str, ...] = (),
) -> tuple[list[BaselineCommand], list[tuple[str, str]]]:
    """Validate and filter LLM-generated commands for *profile*.

    Each element is expected as ``{"description", "command", "mode"}``
    with an optional ``"class"`` field (baseline-observation-contract).
    Safety is delegated to ``validate_command_with_reason`` (per-profile
    whitelist + shell-metachar rejection — the same single enforcement
    source behind ``validate_command``). ``mode`` is normalized:
      * host  → always "simple" (no debug pod on a bare host)
      * k8s   → auto-correct to "debug_two_step" when ``{debug_pod}`` present

    Class handling (task 4.3, member-level per design.md decision 4):
      * Declared class → cross-checked against the command's syntactic
        form by ``_command_class_shape_reason``; a mismatch is rejected
        with the reason surfaced (same channel as the whitelist
        rejection, per Case #63).
      * Missing / non-string class → derived from the form by
        ``_derive_command_class_default`` and stamped on the accepted
        ``BaselineCommand``. Derivation is LABEL-ONLY and never rejects
        (old outputs and registry templates arrive without the field).

    Attempt gate (fourth dimension, ``already_tried``):
      * A command whose fingerprint matches one an earlier retry already
        produced is rejected, with the refusal routed through the SAME
        ``rejected_replacements`` channel as the other three gates, so the
        original observation stays in the failed set and the reason reaches the
        next round's error_feedback (task 5.1's loop, already built).
      * This is the enforcement half of a constraint that previously existed
        only as prompt text. baseline_capture records tried commands and its
        comment called that a "hard block", but neither gate accepted the list,
        so the block was an instruction to the model rather than a property of
        the pipeline. Empty ``already_tried`` (the primary derive path, which has
        no retry history yet) disables the gate entirely.

    Returns ``(accepted, rejected)`` where *rejected* carries
    ``(command, reason)`` pairs. Case #63 (inject-2ee3bdc7): a correct
    domain probe (``dd ... conv=fsync`` for a write-latency case) was
    refused here and dropped with only a log line — the baseline receipt
    said ``7/7`` with no trace, so "this metric is not collectible in the
    read-only channel" stayed invisible to the verifier and to post-hoc
    analysis. Callers now surface the rejections as facts.
    """
    result: list[BaselineCommand] = []
    rejected: list[tuple[str, str]] = []
    tried_index = {
        _command_fingerprint(c): c for c in already_tried if c and c.strip()
    }
    for cmd in commands:
        if not isinstance(cmd, dict):
            continue
        command = (cmd.get("command") or "").strip()
        if not command:
            continue
        reason = validate_command_with_reason(command, profile)
        if reason is not None:
            logger.warning(
                "LLM baseline: rejected command %r (profile=%s): %s",
                command, profile, reason,
            )
            rejected.append((command, reason))
            continue

        # ── Bound gate ── a ``describe node`` must be bound to a specific
        # node; an existence-only selector matches all of them and floods the
        # context (D8, inject-6ebf341c). Runs right after the whitelist (a
        # command that is not even legal is refused for THAT reason first) and
        # before the attempt gate, so an unbounded read is refused for the
        # actionable shape reason even when it also happens to be a repeat.
        _bound_reason = _node_read_unbounded_reason(
            command, _tokenize_for_class(command),
        )
        if _bound_reason is not None:
            logger.warning(
                "LLM baseline: rejected unbounded node read %r (profile=%s): %s",
                command, profile, _bound_reason,
            )
            rejected.append((command, _bound_reason))
            continue

        # ── Attempt gate ── runs after the whitelist so a command that is not
        # even legal is refused for THAT reason (the more actionable one), and
        # before mode/class normalization, which cannot make a repeat non-repeat.
        if tried_index:
            _fp = _command_fingerprint(command)
            if _fp in tried_index:
                reason = (
                    "this command was already attempted in an earlier retry and "
                    "FAILED; re-emitting it, or a reordering of it, cannot "
                    "produce a different result. Change approach: use a "
                    "different command FORM for the same observation dimension "
                    "(a different channel, or a resource name discovered from "
                    "output you already have), or declare expected_absence if "
                    "the non-zero exit IS the baseline value."
                )
                logger.warning(
                    "LLM baseline: rejected repeat command %r (profile=%s): "
                    "already attempted as %r",
                    command, profile, tried_index[_fp][:120],
                )
                rejected.append((command, reason))
                continue

        mode = cmd.get("mode", "simple")
        if profile == PROFILE_HOST:
            # A bare host has no debug pod; every host diagnostic is simple.
            mode = "simple"
        elif "{debug_pod}" in command and mode != "debug_two_step":
            logger.warning(
                "Auto-correcting mode from '%s' to 'debug_two_step' "
                "for command with {debug_pod}: %s",
                mode, cmd.get("description", ""),
            )
            mode = "debug_two_step"

        # class ↔ form consistency gate (task 4.3). Runs AFTER the
        # mode auto-correction so the debug_two_step signal is already
        # normalized when the class predicate looks at it.
        declared_class = cmd.get("class")
        if declared_class is not None and not isinstance(declared_class, str):
            reason = (
                f"class field must be a string, got {type(declared_class).__name__}"
            )
            logger.warning(
                "LLM baseline: rejected command %r (profile=%s): %s",
                command, profile, reason,
            )
            rejected.append((command, reason))
            continue
        if declared_class:
            reason = _command_class_shape_reason(
                command, mode, declared_class, profile,
            )
            if reason is not None:
                logger.warning(
                    "LLM baseline: rejected command %r (profile=%s, class=%s): %s",
                    command, profile, declared_class, reason,
                )
                rejected.append((command, reason))
                continue
            class_value: str | None = declared_class
        else:
            # No class declared: derive (label-only, never rejects).
            class_value = _derive_command_class_default(command, mode, profile)

        result.append(BaselineCommand(
            description=cmd.get("description", ""),
            command=command,
            mode=mode,
            class_value=class_value,
        ))

    return result, rejected


__all__ = [
    "_LLM_BASELINE_MAX_RETRIES",
    "_llm_derive_baseline_commands",
    "_llm_retry_failed_commands",
    "_parse_llm_json_output",
    "_validate_and_filter_commands",
    "_command_class_shape_reason",
    "_derive_command_class_default",
    "_node_read_unbounded_reason",
]
