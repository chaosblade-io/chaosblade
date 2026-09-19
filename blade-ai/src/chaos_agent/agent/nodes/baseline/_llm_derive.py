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

from chaos_agent.agent.nodes.baseline._baseline_profiles import (
    build_baseline_system_prompt,
    validate_command,
)
from chaos_agent.agent.nodes.baseline._commands import BaselineCommand
from chaos_agent.transports import PROFILE_HOST
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
        return _validate_and_filter_commands(commands, profile)
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
    "replace": [BaselineCommand, ...]}``. Entries without a ``verdict``
    field default to ``replace`` (legacy LLM output shape, and the old
    behavior of "every failure gets a corrected replacement").

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
        "purpose>\"} — when it is a TRUE failure (wrong flags, wrong path, "
        "wrong resource, wrong label selector), using the ACTUAL resource "
        "values above.\n"
        "Rules:\n"
        "- Fix ONLY the listed non-zero/empty commands; do NOT add new "
        "observation dimensions or regenerate succeeded ones.\n"
        "- Pod names seen in failed commands or their error output may belong "
        "to an ALREADY-DELETED carrier — between retries the runtime replaces "
        "debug pods, so a literal pod name from earlier output is stale. Emit "
        "the ``{debug_pod}`` placeholder (resolved at execution time); never "
        "a literal debug pod name.\n"
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
            _parse_llm_json_output(raw), failed_observations, profile,
        )
    except Exception as e:
        logger.warning("LLM baseline retry failed: %s", e)
        return {"expected": [], "replace": []}


def _split_retry_decisions(
    decisions: list[dict],
    failed_observations: list[dict],
    profile: str,
) -> dict:
    """Split retry LLM output into semantic verdicts.

    Entries pair positionally with ``failed_observations`` (the prompt asks
    for exactly one entry per command). An entry without a ``verdict``
    field defaults to ``replace`` — the legacy output shape where every
    failure got a corrected replacement — so old-format LLM output still
    behaves exactly like the pre-verdict retry.
    """
    expected: list[tuple[dict, str]] = []
    replace_raw: list[dict] = []
    for entry, obs in zip(decisions, failed_observations):
        verdict = entry.get("verdict")
        if verdict == "expected_absence":
            expected.append((obs, str(entry.get("reason", ""))[:500]))
        elif verdict == "replace" or verdict is None:
            replace_raw.append({
                "description": entry.get("description", ""),
                "command": entry.get("command", ""),
                "mode": entry.get("mode", ""),
            })
        # Unknown verdicts are dropped: the loop re-collects the unjudged
        # observation next round (it is still non-zero and unmarked).
    return {
        "expected": expected,
        "replace": _validate_and_filter_commands(replace_raw, profile),
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


def _validate_and_filter_commands(
    commands: list[dict], profile: str,
) -> list[BaselineCommand]:
    """Validate and filter LLM-generated commands for *profile*.

    Each element is expected as ``{"description", "command", "mode"}``.
    Safety is delegated to ``validate_command`` (per-profile whitelist +
    shell-metachar rejection). ``mode`` is normalized:
      * host  → always "simple" (no debug pod on a bare host)
      * k8s   → auto-correct to "debug_two_step" when ``{debug_pod}`` present
    """
    result: list[BaselineCommand] = []
    for cmd in commands:
        if not isinstance(cmd, dict):
            continue
        command = (cmd.get("command") or "").strip()
        if not command:
            continue
        if not validate_command(command, profile):
            logger.warning(
                "LLM baseline: rejected command %r (profile=%s)",
                command, profile,
            )
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

        result.append(BaselineCommand(
            description=cmd.get("description", ""),
            command=command,
            mode=mode,
        ))

    return result


__all__ = [
    "_LLM_BASELINE_MAX_RETRIES",
    "_llm_derive_baseline_commands",
    "_llm_retry_failed_commands",
    "_parse_llm_json_output",
    "_validate_and_filter_commands",
]
