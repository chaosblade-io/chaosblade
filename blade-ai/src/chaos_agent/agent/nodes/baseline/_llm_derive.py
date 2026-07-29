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

logger = logging.getLogger(__name__)


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

    # Build target context so the LLM embeds the correct resource
    # names/namespace/labels directly into each command.
    target_lines = [f"Fault type: {scope}-{target}-{action}", f"Fault scope: {scope}"]
    if namespace:
        target_lines.append(f"Namespace: {namespace}")
    if names:
        target_lines.append(f"Resource names: {', '.join(names[:5])}")
    if labels:
        label_str = ", ".join(f"{k}={v}" for k, v in labels.items())
        target_lines.append(f"Label selector: {label_str}")
    target_context = "\n".join(target_lines)

    human_prompt = (
        f"{target_context}\n\n"
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
    task_id: str = "",
) -> list[BaselineCommand]:
    """Re-derive baseline commands with execution error feedback.

    Called when LLM-generated commands fail at runtime (e.g. bad flags,
    wrong resource type). Feeds the error output back to the LLM so it
    can self-correct. Uses the same channel-assembled System Prompt and
    per-profile validation as the initial derivation.
    """
    if not llm or not failed_observations:
        return []

    error_lines = []
    for obs in failed_observations:
        stderr_preview = (obs.get("stderr") or "")[:1000]
        error_lines.append(
            f"- Purpose: {obs.get('description', '(unknown)')}\n"
            f"  Command: `{obs.get('command', '')}`\n"
            f"  exit_code={obs.get('exit_code')}\n"
            f"  stderr: {stderr_preview}"
        )
    error_feedback = "\n".join(error_lines)

    target_lines = [f"Fault type: {scope}-{target}-{action}", f"Fault scope: {scope}"]
    if namespace:
        target_lines.append(f"Namespace: {namespace}")
    if names:
        target_lines.append(f"Resource names: {', '.join(names[:5])}")
    if labels:
        label_str = ", ".join(f"{k}={v}" for k, v in labels.items())
        target_lines.append(f"Label selector: {label_str}")
    target_context = "\n".join(target_lines)

    _failed_n = len(failed_observations)
    human_prompt = (
        f"{target_context}\n\n"
        f"<skill-case>\n{skill_case_content}\n</skill-case>\n\n"
        f"Exactly {_failed_n} baseline command(s) FAILED during execution. "
        "All OTHER baseline commands SUCCEEDED and are already kept — "
        "do NOT regenerate them.\n\n"
        f"{error_feedback}\n\n"
        "For EACH failed command above, generate ONE corrected replacement "
        "that collects the SAME metric/state (see its Purpose), using the "
        "ACTUAL resource values above. Rules:\n"
        "- Fix ONLY the listed failures; do NOT add new observation dimensions.\n"
        "- Do NOT regenerate commands that already succeeded.\n"
        "- If a failed command cannot be corrected, omit it — prefer fewer "
        "commands over inventing unrelated ones.\n"
        f"- Output AT MOST {_failed_n} corrected command(s) as a JSON list, "
        "no other text."
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
        commands = _parse_llm_json_output(raw)
        return _validate_and_filter_commands(commands, profile)
    except Exception as e:
        logger.warning("LLM baseline retry failed: %s", e)
        return []


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
