"""Generic orchestration for injection detection shared by the execute loop.

This module is the GENERIC half of injection detection: issue-time method
attribution (registry-dispatched), drill-step text extraction /
observation-step filtering, and the high-tolerance step self-check
assembly. Every carrier VOCABULARY (kubectl write verbs, host injection
binaries, blade success shapes) lives on the providers — consumed via the
registry-dispatched hooks (``scan_step_actions`` / ``was_injection_attempted``,
phase-8 Form B).
This module must not import a concrete provider module (AST-audited by
``tests/test_agent/test_generic_layer_import_audit.py``).
"""

import logging
import re

logger = logging.getLogger(__name__)


def classify_issue_time_method(
    tool_name: str, tool_args: dict, *, is_host: bool
) -> str | None:
    """Map a SINGLE freshly-issued tool_call to the injection_method it enacts.

    Direction B: ``injection_method`` is recorded at the moment the injection is
    ISSUED (from the AIMessage tool_call), not reverse-reconstructed from the
    (possibly severed / truncated) message history later. The carrier shapes
    are owned by the backend providers — dispatched through
    :meth:`FaultProviderRegistry.issue_time_method` (phase-7 T4) so this
    generic layer never names a carrier-specific tool, subcommand vocabulary,
    or method string.

    Returns the method, or ``None`` when the call is not an injection (read-only
    probe, verification, or an unrelated tool). The caller decides the
    commit policy: native methods (``kubectl_native`` / ``host_native``) have no
    experiment UID so the attempt IS the injection and can be recorded at issue
    time; the experiment methods (``host_blade`` / ``kubectl_exec``) are
    classified here for completeness but the execute node defers committing them
    to the ``experiment_uid`` path (proof the ChaosBlade experiment succeeded), so a
    failed blade attempt followed by a kubectl-native fallback is not
    mis-recorded.
    """
    from chaos_agent.agent.providers.registry import FaultProviderRegistry

    if not isinstance(tool_args, dict):
        return None
    return FaultProviderRegistry.issue_time_method(
        tool_name, tool_args, is_host=is_host
    )


def _was_kubectl_injection_attempted(messages: list) -> bool:
    """Check if kubectl write operations were used for fault injection.

    Thin compatibility wrapper kept for the tests' import path (the 13-case
    suite in ``test_verifier.py``; no src caller remains — the verify /
    recover chains read the attempt state off the durable attribution).
    Dispatches through the registry to the owning backend's
    ``was_injection_attempted`` hook (phase-8 T3) — the kubectl write-op
    vocabulary is the backend's, not this generic module's. Returns
    ``False`` when the owning backend is not registered.
    """
    from chaos_agent.agent.providers.registry import FaultProviderRegistry

    provider = FaultProviderRegistry.resolve_by_method("kubectl_native")
    if provider is None:
        return False
    return bool(provider.was_injection_attempted(messages))


# ---------------------------------------------------------------------------
# Injection step self-check (heuristic)
# ---------------------------------------------------------------------------


def _extract_drill_steps(skill_case: str) -> list[str]:
    """Extract 演练步骤 from skill case content.

    Returns the text of each numbered step.
    """
    if "演练步骤" not in skill_case:
        return []
    start = skill_case.index("演练步骤")
    remainder = skill_case[start:]
    header_end = remainder.find('\n')
    if header_end < 0:
        return []
    body = remainder[header_end:]
    next_section = re.search(r'\n\*\*[^*]+\*\*', body)
    section = body[:next_section.start()] if next_section else body
    steps = re.findall(r'^\s*\d+\.\s+(.+)', section, re.MULTILINE)
    return [s.split('\n')[0].strip() for s in steps if s.strip()]


# Markers that mean a command NEVER reached the target (pre-execution
# rejection) now live in ``providers/chaosblade/detection.py`` (``PRE_EXEC_REJECTION_MARKERS``
# / ``reached_target``) — shared by the per-provider executed-action scans.


# Leading-intent markers for a BASELINE / OBSERVATION step, which merely NAMES
# a verb or binary instead of injecting. Requiring those as injection actions is
# a false positive — most visible on the host side, whose vocabulary is binary
# names that read and write under the SAME name (``systemctl status`` vs
# ``systemctl stop``, ``date`` vs ``date -s``), unlike the write-only kubectl
# verbs. Matched only at the START of the step: a step's opening clause declares
# its purpose, so an action step that merely MENTIONS observation later
# ("删除该节点上的 Pod，观察是否被重建") is correctly kept as an action.
_READONLY_STEP_PREFIXES = (
    "记录",
    "查看",
    "观察",
    "确认",
    "检查",
    "获取",
    "统计",
    "采集",
    "record",
    "observe",
    "inspect",
    "check",
    "verify",
    "baseline",
    "capture",
)


def _injection_intent_steps(steps: list[str]) -> list[str]:
    """Drop baseline / observation steps before extracting REQUIRED actions.

    A read-only step (``确认目标服务当前状态：systemctl status <svc>``) names a
    binary without injecting anything; treating it as a required injection
    action is the host-side analogue of the label-vs-patch false positive.
    Applied to both backends — harmless for kubectl (write-only verbs) and
    decisive for host. Never returns more steps than it was given.

    Only the step's LEADING intent is inspected, so a genuine action step that
    also mentions observing the outcome keeps contributing its verb.
    """
    kept: list[str] = []
    for step in steps:
        head = step.lstrip(" \t-*。.、：:").lower()
        if any(head.startswith(p) for p in _READONLY_STEP_PREFIXES):
            continue
        kept.append(step)
    return kept


def build_injection_step_selfcheck(
    skill_case: str,
    messages: list,
    injection_method: str | None,
    *,
    is_teardown=None,
) -> str | None:
    """HIGH-TOLERANCE step-skip detection → SOFT, one-shot reminder, or ``None``.

    Third condition of the multi-step self-check (after the ``is_multi_step``
    switch and "scenario has >= 2 drill steps"): a fault-tolerant judgement of
    whether an injection action documented in the skill case looks NOT-yet
    -performed. Because string mapping is imprecise, it errs toward
    UNDER-reporting (executed counts any attempted action incl. timeouts; only
    genuinely-absent actions are flagged) and the returned message is a soft
    heuristic asking the LLM to RECONSIDER — the LLM may still conclude if it
    judges the injection complete. Returns ``None`` (no reminder) when the
    scenario is single-step, the backend does not claim the step self-check,
    no required action is recognised, or nothing looks missing.

    Backend-aware vocabulary dispatch (phase-8 Form B): the backend resolved
    from ``injection_method`` owns the token vocabulary via its optional
    ``scan_step_actions`` hook — kubectl write verbs for kubectl_native,
    host injection binaries for host_native; experiment-UID carriers do not
    claim the hook (completion is judged by the experiment evidence chain,
    the UID — the D4 narrowing, pinned by golden tests).

    Teardown≠step-credit (O-3, P3): thread the ``is_teardown`` matcher
    (``execution_artifacts.make_teardown_matcher``) and a
    registered-vehicle teardown delete's receipt credits NO step verb —
    at call granularity, so a MIXED batch (teardown delete + read-only
    call) no longer keeps the whole message's teardown credit. ``None``
    (the default) is RAW credit (test fixtures).
    """
    if not skill_case:
        return None
    steps = _extract_drill_steps(skill_case)
    if len(steps) < 2:
        return None

    # REQUIRED actions come from injection steps only — a baseline/observation
    # step that merely names a verb or binary is not an injection action. The
    # full step list is still shown to the LLM below for context.
    action_steps = _injection_intent_steps(steps)

    # Vocabulary dispatch: the resolved backend owns the token vocabulary.
    # Optional hook — a backend that does not claim the step self-check
    # contributes nothing, so the check is skipped (getattr-skip also covers
    # third parties that omit the hook entirely).
    from chaos_agent.agent.providers.registry import FaultProviderRegistry

    provider = (
        FaultProviderRegistry.resolve_by_method(injection_method)
        if injection_method
        else None
    )
    scan_hook = getattr(provider, "scan_step_actions", None) if provider else None
    if scan_hook is None:
        return None
    scan = scan_hook(action_steps, messages, is_teardown=is_teardown)
    if scan is None:
        return None
    required = scan.required
    executed = scan.executed

    if not required:
        return None
    missing = {tok: desc for tok, desc in required.items() if tok not in executed}
    if not missing:
        return None

    lines = [
        "[Step self-check] This multi-step skill case has an injection action "
        "that may not have been performed yet. Steps outlined:",
    ]
    for i, step in enumerate(steps, 1):
        lines.append(f"{i}. {step}")
    lines.append(
        "\nPossibly not yet performed: "
        + ", ".join(f"{tok} ({desc})" for tok, desc in missing.items())
    )
    lines.append(
        "\nReconsider (this check is heuristic and may be inaccurate): if you "
        "have ALREADY performed the actions needed for the fault effect — a tool "
        "may have timed out but still applied — STOP calling tools and let "
        "verification confirm it. Do NOT repeat actions already done, and do NOT "
        "loop deleting/observing to watch the effect (observation is the "
        "verification phase's job). If an action was genuinely SKIPPED, do it now."
    )
    return "\n".join(lines)
