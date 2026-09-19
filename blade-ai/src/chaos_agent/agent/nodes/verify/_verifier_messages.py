"""Verifier messages domain: synthetic message construction for Layer 2.

Extracted from verifier.py to isolate the messages construction logic
(baseline ToolMessages and the full Layer 2 prompt builder) from the
verifier node entry points and orchestration code.
"""

import logging
import time

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes.baseline._commands import (
    _is_empty_observation,
    _is_observation_success,
)
from chaos_agent.agent.nodes.verify._verifier_hints import (
    _extract_baseline_key_metrics,
    _BASELINE_INTEGRITY_PROMPT,
    _get_fault_verification_hints,
)
# Phase-4 T6: verdict-direct (was a forward through the _verifier_layer1
# shim pre-cleanup).
from chaos_agent.agent.result.verdict import ChecklistItemStatus, Layer1Result
from chaos_agent.agent.prompts.reminder import wrap_system_reminder
from chaos_agent.agent.nodes.verify._verifier_layer2_parse import (
    _extract_verification_step_descriptions,
    _has_injection_verification_section,
)
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.utils.message_integrity import apply_synthetic_pair_gate
from chaos_agent.utils.truncation import build_truncation_notice

logger = logging.getLogger(__name__)

# Item-status teaching prose derives from the legislation enum (B76
# round-14 root-cause fix): teaching, parsing and clamping share ONE
# vocabulary — this line can never drift from the regex or the clamps.
_ITEM_STATUS_PROSE = ", ".join(m.value for m in ChecklistItemStatus)
_BASELINE_TOOL_CALL_ID = "baseline_collector"
_METRICS_TOOL_CALL_ID = "baseline_collector_metrics"

# Aggregate set of all synthetic tool_call_ids used for state persistence.
_SYNTHETIC_TOOL_CALL_IDS = frozenset({
    _BASELINE_TOOL_CALL_ID,
    _METRICS_TOOL_CALL_ID,
})

# STABLE langchain message ids for the synthetic pairs (not auto-generated
# UUIDs). The dedup gate below rebuilds the pair set whenever state's copy is
# damaged, and rebuilds reach state through ``extract_synthetic_messages`` →
# ``add_messages``. That reducer REPLACES a message whose id already exists and
# APPENDS one whose id is new — so with fresh UUIDs every rebuild would leave
# the damaged copy in state, the gate would fire again next turn, and state
# would grow by a full pair set per turn. Stable ids make the rebuild
# idempotent: one copy per role, no matter how often it runs.
_BASELINE_MSG_ID_CALLER = "synthetic:verifier:baseline:caller"
_BASELINE_MSG_ID_RESULT = "synthetic:verifier:baseline:result"
_METRICS_MSG_ID_CALLER = "synthetic:verifier:baseline_metrics:caller"
_METRICS_MSG_ID_RESULT = "synthetic:verifier:baseline_metrics:result"

# All four stable message ids of the synthetic baseline pair set, exported for
# the replan-seam lifecycle cleanup (``reset_attribution_state`` removes these
# messages at every replan seam so the next verification cycle rebuilds the
# pair from the CURRENT ``baseline_data`` — see change
# ``stale-baseline-pair-seam-cleanup``). Single construction source: the seam
# imports this frozenset instead of copying string literals, so an id rename
# cannot silently disarm the cleanup.
BASELINE_PAIR_MESSAGE_IDS = frozenset({
    _BASELINE_MSG_ID_CALLER,
    _BASELINE_MSG_ID_RESULT,
    _METRICS_MSG_ID_CALLER,
    _METRICS_MSG_ID_RESULT,
})

# Marker for the main verifier context HumanMessage — used to identify
# the ephemeral HumanMessage that should be persisted to AgentState on the
# cycle's first turn so it remains visible on subsequent iterations.
_VERIFIER_CONTEXT_KWARGS_KEY = "_verifier_main_context"

# What each kind of observation MEANS for the verdict — evidence
# semantics, not behavioural rules: the model derives how to judge from
# what the evidence says, never from commanded procedures or count caps
# (verifier-effect-decides; review rounds 2-3 fixed the register: v1 had
# numeric budgets, v2 had imperative rules, v3 states semantics only).
# Module-level so judgement tests can anchor the wording.
_EVIDENCE_SEMANTICS_PROMPT = (
    "**EVIDENCE SEMANTICS (CRITICAL)**: what each observation means.\n"
    "- Qualitative faults (process gone / NotReady / unreachable): the "
    "observation is binary — a clear one settles the step.\n"
    "- Quantitative faults: the injected value (e.g. --mem-percent 80) is "
    "a mechanism parameter, not a measurement promise. The effect "
    "evidence is a significant change from baseline — or, with no "
    "baseline, significant deviation from the expected healthy state; a "
    "gap vs the injected value is magnitude information for Warnings.\n"
    "- Mechanism evidence (experiment registration, tool receipts, rule "
    "snapshots, unit liveness) speaks for the mechanism, not the "
    "outcome — field-proven: tools have reported Success while "
    "delivering no fault. When the effect is missing, it points to the "
    "failed layer (command not issued / mechanism not alive / payload "
    "spinning / effect not propagated).\n"
    "- Derived status labels (e.g. CrashLoopBackOff) render underlying "
    "quantities — restarts climbing, back-off events; the quantities are "
    "the evidence, the label their display form.\n"
    "- Fault effects propagate with physical delay (heartbeat grace, "
    "probe periods, image pull); an observation that predates "
    "propagation says nothing yet about the outcome.\n\n"
)


def _verification_cycle_needs_context(state: AgentState) -> bool:
    """True when the CURRENT verification cycle has no main-context message yet.

    Position-based detection, aligned with execute_loop's Phase-2 kickoff:
    a phase transition is a fact about the message sequence (was the
    transition message emitted for THIS cycle?), never about loop counters —
    counting couples injection to seam-side counter resets and silently
    breaks when a reset is missed, conditional, or a resume lands mid-cycle.

    The current cycle is the slice at/after ``attribution_epoch_index``:
    every replan seam re-bases the boundary via ``reset_attribution_state``,
    so a context message from a PRE-replan cycle sits before the boundary and
    re-arms the injection for the new cycle — no counter involvement. With
    no boundary set (first cycle / flows that never rebase) the whole
    history is the current cycle.
    """
    messages = state.get("messages", [])
    boundary = state.get("attribution_epoch_index")
    try:
        start = int(boundary) if boundary else 0
    except (TypeError, ValueError):
        start = 0
    if start > len(messages):
        start = 0
    return not any(
        isinstance(m, HumanMessage)
        and getattr(m, "additional_kwargs", {}).get(_VERIFIER_CONTEXT_KWARGS_KEY)
        for m in messages[start:]
    )

# ---------------------------------------------------------------------------
# Baseline data → synthetic ToolMessage injection
#
# Baseline data was previously injected as a plain-text section inside the
# verifier HumanMessage.  LLMs treated it as "external reference material"
# and often ignored it (confirmation bias: baseline contradicts the conclusion
# the LLM already formed → LLM discards baseline and declares BaselineUsed=false).
#
# By converting baseline data into synthetic AIMessage+ToolMessage pairs,
# the LLM perceives it as "tool call results I already obtained" — creating
# a causal narrative ("I ran kubectl before injection → got these numbers →
# now I must compare") instead of "someone gave me a data section".
#
# Academic basis:
#   - Lost in the Middle (Liu 2023): position > role for attention; early
#     placement avoids "middle invisibility"
#   - TIM-PRM (Kuang 2024): independent tool queries eliminate confirmation
#     bias by decoupling evidence acquisition from reasoning chain
#   - VERITAS (Xu 2025): LLMs don't inherently trust ToolMessage more than
#     HumanMessage — the causal narrative framing is what matters, not the
#     message role per se.
# ---------------------------------------------------------------------------


def _build_baseline_tool_messages(
    baseline: dict,
    fault_target: str,
    fault_action: str,
    injection_parsed: dict | None = None,
) -> list:
    """Build synthetic AIMessage + ToolMessage pairs for pre-injection baseline data.

    Converts the baseline_data dict (from baseline_capture node) into one or
    two synthetic tool call result pairs that inject baseline observations and
    extracted key metrics into the LLM's context as "already obtained" tool
    results, with causal narrative framing.

    Returns:
        List of [AIMessage, ToolMessage] pairs (2–4 messages total).
        Empty list if baseline has no usable data.
    """
    # Blob gate: usable when there are value-carrying successes OR any
    # expected-absence observations (#31 residue pre-checks / #16 fix B
    # planned creations — a pure pre-check baseline has success_count 0
    # yet its absences are exactly what verify must compare against).
    if not baseline or (
        baseline.get("success_count", 0) <= 0
        and not any(
            o.get("expected_absence") for o in baseline.get("observations", [])
        )
    ):
        return []

    captured_at = baseline.get("captured_at", "unknown time")
    source = baseline.get("source", "unknown")
    observations = baseline.get("observations", [])

    # ── Pair 1: Raw baseline observations ──
    # Each successful observation (exit_code=0, has stdout) becomes part
    # of the tool result content, with causal narrative framing.
    #
    # Two-layer governance boundary (why a per-obs pre-cut lives here
    # instead of leaving it all to the compactor):
    # * this layer is the OBS-GRANULARITY GATE: N observations are fused
    #   into ONE synthetic ToolMessage — the compactor's smart stripper
    #   is structurally blind to fused markdown, so a full dump would be
    #   head-cut to just the first obs's head; the per-obs budget keeps
    #   every obs's head (the delta-comparison values) in the context;
    # * the compactor is the BETWEEN-TURN rolling governor for whatever
    #   this injection leaves in context.
    # The truncation notice follows the shared contract (kind=
    # baseline-evidence): marker + honest size + "full observation
    # preserved in state.baseline_data" — the LLM knows the full evidence
    # exists instead of hitting a zero-information dead end.
    obs_lines = []
    for obs in observations:
        # Expected-absence observations (#16 fix B / #31): the absence IS
        # the baseline value — pre-injection "ConfigMap not created yet"
        # (machine-marked planned creation) or "residue file does not
        # exist" (LLM-judged pre-check). They must NOT be filtered like
        # failures: the post-injection comparison for these dimensions is
        # "absent → present", and hiding the absence would leave the LLM
        # comparing against nothing. Rendered as existence baselines with
        # their reason, distinct from value-carrying observations.
        if obs.get("expected_absence"):
            desc = obs.get("description", "unknown metric")
            cmd = obs.get("command", "")
            _ev = (obs.get("stdout") or obs.get("stderr") or "").strip()
            _ev_preview = _ev[:400] if _ev else "(no output — resource absent)"
            obs_lines.append(
                f"### {desc} — PRE-INJECTION ABSENCE (existence baseline)\n"
                f"Command: `{cmd}`\n"
                f"Note: {obs['expected_absence']}\n"
                f"```\n{_ev_preview}\n```"
            )
            continue
        # Empty observations (#16 fix C): exit 0 but nothing matched — "No
        # resources found" or an empty items list is non-empty TEXT, so the
        # old ``not obs.get("stdout")`` guard let it through and framed it
        # as an authoritative comparison value. An empty observation is
        # not a baseline; keep it out of the blob (the honest header count
        # below still reports it, and B-side planned-creation notes live in
        # the baseline receipt).
        if (
            obs.get("exit_code") != 0
            or not obs.get("stdout")
            or _is_empty_observation(obs)
        ):
            continue
        desc = obs.get("description", "unknown metric")
        cmd = obs.get("command", "")
        output = obs["stdout"][:1500]
        if len(obs["stdout"]) > 1500:
            output += build_truncation_notice(
                "baseline-evidence", len(obs["stdout"]), unit="characters",
            )
        obs_lines.append(
            f"### {desc}\n"
            f"Command: `{cmd}`\n"
            f"```\n{output}\n```"
        )

    if not obs_lines:
        return []

    # Honest quality header (#16 fix C): the raw "N/M succeeded" counts
    # executions, not observations. When some of the successes are empty
    # spins, the LLM must know that the usable comparison set is smaller —
    # otherwise it anchors its delta comparisons on nothing. Legacy
    # baselines (pre-fix) lack the split fields; recompute from the list.
    _valid = baseline.get("valid_count")
    _empty = baseline.get("empty_count")
    if _valid is None or _empty is None:
        _valid = sum(
            1 for obs in observations
            if (_is_observation_success(obs)
                and not _is_empty_observation(obs))
            or obs.get("expected_absence")
        )
        _empty = sum(
            1 for obs in observations
            if _is_observation_success(obs)
            and _is_empty_observation(obs)
            and not obs.get("expected_absence")
        )
    # total_count fallback mirrors _verifier_shared: the count in hand
    # is the honest denominator for legacy baselines (pre-total_count
    # persistence) — a 0 fallback rendered "N/0" receipts (#13/#10 audits).
    _total = baseline.get("total_count", len(observations))
    _quality = (
        f"{baseline.get('success_count', 0)}/"
        f"{_total} succeeded"
        if _empty <= 0
        else (
            f"{baseline.get('success_count', 0)}/"
            f"{_total} succeeded "
            f"({_valid} valid + {_empty} empty — empty observations captured "
            f"no value and are NOT usable as comparison baselines)"
        )
    )
    raw_content = (
        f"Pre-injection baseline collected at {captured_at} "
        f"(strategy: {source}, {_quality}).\n\n"
        f"These metrics were captured BEFORE fault injection using the same "
        f"kubectl commands you would use for verification. "
        f"This is your authoritative reference — compare every post-injection "
        f"observation against these values using delta format: "
        f"\"baseline: X → current: Y (ΔZ)\".\n\n"
        + "\n\n".join(obs_lines)
    )

    ai_msg_1 = AIMessage(
        content="",
        id=_BASELINE_MSG_ID_CALLER,
        tool_calls=[{
            "name": "baseline_collector",
            "args": {"phase": "pre-injection", "target": "all_metrics"},
            "id": _BASELINE_TOOL_CALL_ID,
            "type": "tool_call",
        }],
    )
    tool_msg_1 = ToolMessage(
        content=raw_content,
        id=_BASELINE_MSG_ID_RESULT,
        tool_call_id=_BASELINE_TOOL_CALL_ID,
        name="baseline_collector",
    )

    # ── Pair 2: Extracted key metrics + comparison semantics ──
    # Pre-extracted metrics so LLM doesn't need to parse raw kubectl output
    # to find key numbers.  Includes mandatory comparison format instructions.
    key_metrics = _extract_baseline_key_metrics(baseline, fault_target, fault_action)
    metrics_parts = []
    if key_metrics:
        metrics_lines = "\n".join(f"- {k}: {v}" for k, v in key_metrics.items())
        metrics_parts.append(
            f"### Baseline Key Metrics (extracted — compare against these)\n"
            f"{metrics_lines}"
        )

    # Comparison semantics — the causal narrative that motivates baseline usage
    semantics = (
        "### Baseline Comparison Rules\n"
        "You now have PRE-INJECTION baseline data (captured BEFORE the fault was injected). "
        "This is MORE RELIABLE than your first post-injection observation because the "
        "baseline values are guaranteed to be unaffected by the fault.\n"
        "Rules:\n"
        "- Compare post-injection metrics against the pre-injection baseline above, "
        "NOT against your first post-injection check.\n"
        "- A significant change from baseline (e.g., disk 10%→13%, CPU 100m→800m, "
        "RestartCount 7→8) is STRONG evidence the fault is in effect.\n"
        "- If metrics are SIMILAR to baseline, the fault may not be working.\n\n"
        "**FORMAT REQUIREMENT (when Pre-Injection Baseline is available)**:\n"
        "For steps that decide whether the injection took effect, include baseline "
        "comparison in the evidence in the format:\n"
        "  \"baseline: <metric from above> → post-injection: <metric you observe NOW> (Δ<change>)\"\n"
        "Steps without a baseline comparison weaken their own evidence. Propagated-effect "
        "steps (OOM, latency, business impact) may omit it — mark them "
        "'expected'/'not_applicable' when no observation is in hand.\n"
        "In your VERIFICATION_RESULT, set BaselineUsed: true.\n"
    )
    metrics_parts.append(semantics)

    if not metrics_parts:
        # UNREACHABLE today: ``semantics`` is appended unconditionally above, so
        # ``metrics_parts`` is never empty. Kept as a guard, but note the
        # contract it would break — the dedup gate in ``_build_layer2_messages``
        # requires EVERY id in ``_SYNTHETIC_TOOL_CALL_IDS`` to be paired, so
        # emitting only pair 1 would make the gate rebuild on every turn.
        # Reviving this branch means narrowing the required-id set with it.
        return [ai_msg_1, tool_msg_1]

    metrics_content = "\n\n".join(metrics_parts)

    # Second synthetic pair for structured metrics + semantics
    ai_msg_2 = AIMessage(
        content="",
        id=_METRICS_MSG_ID_CALLER,
        tool_calls=[{
            "name": "baseline_collector",
            "args": {"phase": "pre-injection", "target": "key_metrics_summary"},
            "id": _METRICS_TOOL_CALL_ID,
            "type": "tool_call",
        }],
    )
    tool_msg_2 = ToolMessage(
        content=metrics_content,
        id=_METRICS_MSG_ID_RESULT,
        tool_call_id=_METRICS_TOOL_CALL_ID,
        name="baseline_collector",
    )

    return [ai_msg_1, tool_msg_1, ai_msg_2, tool_msg_2]








# ---------------------------------------------------------------------------
# Refactor 7: 提取 Layer 2 prompt 构建为独立函数
# 原因: prompt 拼接 + 收敛提示逻辑与 LLM 调用逻辑混在一起
# 做法: 独立函数，输入结构化参数，输出 HumanMessage 列表
# ---------------------------------------------------------------------------


def _build_convergence_hint(count: int) -> str:
    """Build a convergence hint string based on iteration count.

    3-tier system (matching execute_loop pattern):
    - Tier 1: soft warning when iterations are running low
    - Tier 2: urgent warning on second-to-last iteration
    - Empty string when no hint is needed
    """
    remaining = settings.max_verifier_loop - count
    if settings.max_verifier_loop - 3 <= count < settings.max_verifier_loop - 1:
        return (
            f"\n\n**Iteration Progress**: You are on iteration {count} of max {settings.max_verifier_loop} "
            f"({remaining} remaining). "
            f"If you have gathered enough evidence, call submit_verification now. "
            f"If you need more data, focus on the most critical checks only."
        )
    if count >= settings.max_verifier_loop - 1:
        return (
            f"\n\n**VERIFICATION DEADLINE**: This is iteration {count} of max {settings.max_verifier_loop} — "
            f"your SECOND-TO-LAST iteration.\n"
            f"Based on ALL evidence gathered so far:\n"
            f"  - If you have sufficient data, call submit_verification NOW.\n"
            f"  - If you need ONE more check, do it now — but you MUST conclude on the next iteration.\n\n"
            f"Your Overall conclusion must be one of:\n"
            f"  - **verified**: Fault effect is confirmed present on the target\n"
            f"  - **partial**: Some evidence supports the fault, but not fully confirmed\n"
            f"  - **unverified**: Fault effect could NOT be confirmed despite checks\n"
        )
    return ""


def build_recovery_timer_reminder(
    state: AgentState,
    *,
    now: float | None = None,
) -> str:
    """Render the armed recovery timer's remaining time for the verifier.

    The verifier's three context channels (message history, progress ledger,
    system prompt) carry no timestamps, and the LLM has no wall clock of its
    own — so a persistence check used to reverse-engineer the fire moment
    from cluster-side pod ages (Case #46 R2: "roughly T+120s since arm",
    derived through two unknown delays, while a persistence wait could
    silently straddle the fire). The exact deadline already sits on the
    recovery-carrier artifact (``recovery_deadline_epoch``, written at arming
    time by ``_mark_bounded_host_recovery``); this renders REMAINING seconds
    rather than the fire timestamp because a moment value would still force
    the model to supply its own "now", which it does not have.

    Returns ``""`` when no armed carrier carries a numeric deadline (the
    ChaosBlade path times out inside the experiment, never here).
    """
    artifacts = state.get("execution_artifacts") or []
    armed: list[tuple[float, dict]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("status") != "recovery_armed":
            continue
        deadline = artifact.get("recovery_deadline_epoch")
        if isinstance(deadline, (int, float)):
            armed.append((float(deadline), artifact))
    if not armed:
        return ""
    current = time.time() if now is None else float(now)

    def _window_text(artifact: dict) -> str:
        window = artifact.get("recovery_timeout_seconds")
        return f" (window {window}s)" if isinstance(window, (int, float)) else ""

    future = [(d, a) for d, a in armed if d > current]
    if future:
        # The NEXT fire is the planning anchor when several carriers are armed.
        deadline, artifact = min(future, key=lambda pair: pair[0])
        seconds = max(0, int(deadline - current))
        return (
            f"**RECOVERY TIMER (system-computed, authoritative)**: the recovery "
            f"carrier's self-recovery timer{_window_text(artifact)} fires in "
            f"~{seconds}s. Evidence semantics: any probe taken after that moment "
            f"can no longer serve as persistence evidence; any wait longer "
            f"than ~{seconds}s ends after the fire."
        )
    # No future fire left: report the most recent one. Post-fire wording must
    # stay NEUTRAL about recovery outcome: fire proves the actions were
    # TRIGGERED, never that they converged (Case #46 R1: fired on schedule,
    # then hung 12 minutes on the OrderedReady wedge). A still-present fault
    # signature after the fire is recovery-NOT-converged evidence — optimism
    # like "recovery at or near completion" would launder exactly that
    # finding, and the verdict must rest on pre-fire evidence.
    deadline, artifact = max(armed, key=lambda pair: pair[0])
    return (
        f"**RECOVERY TIMER (system-computed, authoritative)**: the recovery "
        f"carrier's self-recovery timer{_window_text(artifact)} fired "
        f"~{int(current - deadline)}s ago — the recovery actions were TRIGGERED, "
        f"not necessarily completed. Evidence semantics: fault signatures "
        f"observed from now on are not persistence evidence; a still-present "
        f"signature means recovery has NOT converged (record it); the verdict "
        f"rests on pre-fire evidence."
    )


def _build_layer2_messages(
    state: AgentState,
    layer1: Layer1Result,
    experiment_uid: str,
    skill_name: str,
    kubeconfig: str,
    count: int,
    tool_pod_name: str | None = None,
    new_cycle: bool | None = None,
) -> list:
    """Build messages for Layer 2 LLM invocation.

    On the FIRST TURN OF THE CURRENT CYCLE, injects the full Layer 1
    context — detected POSITIONALLY (no marker-tagged context message in
    the current epoch) rather than by ``count == 1``, so a replan that
    re-bases the attribution epoch re-arms the injection even if the loop
    counter were not reset. On subsequent iterations approaching the limit,
    injects a convergence hint.
    """
    messages = list(state.get("messages", []))
    convergence_hint = _build_convergence_hint(count)

    # ── Position-optimized synthetic message injection ──
    # Baseline ToolMessages before the HumanMessage or convergence hint
    # (Lost in the Middle: early placement gets higher attention).
    # Inject on EVERY iteration, not just the cycle's first turn, because
    # they are NOT persisted in AgentState.messages by default
    # (result_update only contains the LLM's response). Without this, LLM
    # loses baseline data on later turns. When state already holds a COMPLETE,
    # correctly ordered pair set (persisted from the cycle's first turn via
    # result_update), skip to avoid duplication; when any pair is missing,
    # half-present or duplicated, rebuild the whole set (gate below).
    # State-derived variables needed by _build_baseline_tool_messages.
    from chaos_agent.agent.spec.fault_spec import read_fault_spec as _rfs_vm
    _spec_vm = _rfs_vm(state)
    _fault_target = _spec_vm.fault_target if _spec_vm else ""
    _fault_action = _spec_vm.fault_action if _spec_vm else ""
    _injection_parsed = state.get("injection_parsed_params") or {}

    _baseline = state.get("baseline_data")
    if _baseline and _baseline.get("success_count", 0) > 0:
        # PAIR-aware dedup over the whole synthetic id set — not a single-id
        # ToolMessage probe. The old gate asked only "is a ToolMessage with
        # ``baseline_collector`` already in history?", which is blind to:
        #   * the AI caller gone but its ToolMessage surviving → the gate
        #     reports "injected", no rebuild runs, so no caller is ever
        #     supplied and the orphan ships with nothing to answer;
        #   * damage confined to the METRICS pair → that id was never probed;
        #   * a duplicated or reversed pair → both illegal, both invisible.
        #
        # The gate mechanics (diagnose → drop every stale fragment → rebuild →
        # log at the level the cause deserves) are shared with recover_verify
        # and documented on apply_synthetic_pair_gate. What stays here is the
        # part specific to this node: two non-convergent flavours of damage are
        # measured, tolerated and pinned by tests — a REVERSED pair, where the
        # id-based merge pins the surviving result before its rebuilt caller
        # forever (INFO), and a legacy DUPLICATE fragment whose message id
        # predates the stable-id fix and can never be replaced in place
        # (WARNING, and honest: state really does answer one tool_call twice).
        # Neither leaks: the shipped sequence is intact and protocol-legal
        # every turn. See message_integrity's CONTRACT BOUNDARY, plus
        # TestSyntheticPairDedup and TestRecoverPairGate.
        messages = apply_synthetic_pair_gate(
            messages,
            _SYNTHETIC_TOOL_CALL_IDS,
            lambda: _build_baseline_tool_messages(
                _baseline, _fault_target, _fault_action, _injection_parsed,
            ),
            phase="verify",
        )
    if new_cycle is None:
        new_cycle = _verification_cycle_needs_context(state)
    if new_cycle:
        context = _build_first_iteration_context(
            state, layer1, experiment_uid, skill_name, kubeconfig,
            tool_pod_name, convergence_hint,
        )
        messages.append(HumanMessage(
            content=context,
            additional_kwargs={_VERIFIER_CONTEXT_KWARGS_KEY: True},
        ))
    elif convergence_hint:
        # Subsequent iterations approaching limit: inject convergence nudge
        messages.append(HumanMessage(content=wrap_system_reminder(convergence_hint)))

    # Recovery-timer visibility (Case #46): the verifier LLM has no wall clock
    # and its context channels carry no timestamps, so the armed self-recovery
    # timer's fire moment had to be reverse-engineered from pod ages — a
    # persistence-check wait could silently straddle the fire. Rendered fresh
    # on EVERY iteration (this builder runs once per graph re-entry) and NOT
    # persisted to state (extract_persistent_hm only takes the kwargs-tagged
    # context message; extract_synthetic_messages only takes tool pairs), so
    # the remaining seconds never go stale and never accumulate.
    timer_reminder = build_recovery_timer_reminder(state)
    if timer_reminder:
        messages.append(HumanMessage(content=wrap_system_reminder(timer_reminder)))

    # Final-iteration conclusion prompt (tools will be unbound at this count).
    # Skipped when verifier_json_mode is on: verifier.py appends the JSON
    # schema reminder at the same count and forces response_format=json_object
    # — injecting both would give the model two mutually exclusive format
    # contracts in one call. The JSON reminder is the single format source.
    if count >= settings.max_verifier_loop and not settings.verifier_json_mode:
        messages.append(HumanMessage(content=wrap_system_reminder(
            f"**FINAL VERIFICATION ITERATION**: This is iteration {count} of max {settings.max_verifier_loop}. "
            f"NO more iterations available. Tools are no longer available.\n"
            f"You MUST provide your final verification conclusion NOW in this EXACT format:\n\n"
            f"VERIFICATION_CHECKLIST:\n"
            f"- Step 1: passed/failed/skipped — brief evidence\n"
            f"- Step 2: passed/failed/skipped — brief evidence\n"
            f"- ...\n\n"
            f"VERIFICATION_RESULT:\n"
            f"- Layer1 (experiment status): passed/failed/skipped\n"
            f"- Layer2 (fault-specific): passed/failed/skipped - evidence summary\n"
            f"- Overall: verified/partial/unverified\n"
            f"- BaselineUsed: true/false (whether pre-injection baseline was compared in evidence)\n"
            f"- Warnings: any warnings, or \"none\"\n\n"
            f"Layer 2 Status Definitions: 'passed' = fault effect IS observable (injection WORKED); "
            f"'failed' = fault effect is NOT observable (injection may not have worked); "
            f"'skipped' = could not verify.\n"
            f"Do NOT conclude 'failed' if evidence shows the fault IS in effect.\n\n"
            f"If you cannot determine the result, set Overall to \"unverified\" and explain why in Layer2 details."
        )))

    return messages

def _build_first_iteration_context(
    state: AgentState,
    layer1: Layer1Result,
    experiment_uid: str,
    skill_name: str,
    kubeconfig: str,
    tool_pod_name: str | None,
    convergence_hint: str,
) -> str:
    """Build the full Layer 2 context string for the first verification iteration.

    Assembles Layer 1 results, fault metadata, baseline references,
    skill-case verification strategy, and behavioral rules into a single
    context string for the HumanMessage.
    """
    from chaos_agent.agent.spec.fault_spec import read_fault_spec as _rfs_vm
    _spec_vm = _rfs_vm(state)
    _params = dict(_spec_vm.params) if _spec_vm else {}
    _fault_target = _spec_vm.fault_target if _spec_vm else ""
    _fault_action = _spec_vm.fault_action if _spec_vm else ""

    # First iteration: inject full Layer 1 context
    target = {
        "namespace": _spec_vm.namespace if _spec_vm else "",
        "names": list(_spec_vm.names) if _spec_vm else [],
        "labels": dict(_spec_vm.labels) if _spec_vm else {},
        "resource_type": _spec_vm.scope if _spec_vm else "",
    }
    params = _params
    injection_method = state.get("injection_method")
    fault_scope = _spec_vm.scope if _spec_vm else ""
    fault_target = _fault_target
    fault_action = _fault_action

    # Build Layer 1 context section (adapted for skipped vs passed)
    _is_self_destructive = (
        layer1.status == "skipped"
        and "self-destructive" in (layer1.details or "").lower()
    )
    if _is_self_destructive:
        layer1_context = (
            "## Layer 1 Result (SKIPPED — self-destructive fault)\n"
            f"Layer 1 tool check unreachable: {layer1.details}\n\n"
            "## WARNING: Target node is NotReady\n"
            "The target node lost connectivity — this is likely the "
            "injection effect itself (e.g. containerd/kubelet stopped). "
            "Your verification MUST use commands that go through the "
            "API server (not the node):\n"
            "- `kubectl get nodes` — confirm node NotReady\n"
            "- `kubectl get pods` — check for ContainerCreating / Terminating\n"
            "- `kubectl describe pod` — check Events for runtime errors\n"
            "- `kubectl get events` — check for node-level events\n"
            "Do NOT use `kubectl exec` into pods on the affected node "
            "(it will fail because the node is unreachable).\n\n"
        )
        layer2_instruction = (
            "This is a self-destructive fault: the injection destroyed "
            "the node's communication channel. Verify the EFFECT of the "
            "fault (node NotReady, pods in abnormal state) rather than "
            "the injection mechanism (Layer 1 tool check).\n"
        )
    elif layer1.status == "skipped":
        layer1_context = (
            "## Layer 1 Result\n"
            "Layer 1 skipped: no experiment UID for this fault. "
            "Proceed directly to Layer 2 verification.\n\n"
        )
        layer2_instruction = (
            "This fault was injected natively (no experiment carrier). "
            "Perform Layer 2 verification: use the available observation "
            "tools to verify the fault is actually in effect on the target.\n"
        )
    else:
        # Round-29: the anchor line names the uid the poll ACTUALLY
        # verified — ``layer1.experiments`` carries the anchor entry the
        # plural poll chose (a dead dispatch slot handed the role to the
        # first survivor); the caller's ``experiment_uid`` stays the
        # fallback for the legacy single-value shape.
        _anchor_uid = next(
            (e.uid for e in layer1.experiments if e.is_anchor), experiment_uid,
        )
        layer1_context = (
            f"## Layer 1 Result (already completed)\n"
            f"Layer 1 for experiment {_anchor_uid}: {layer1.status}\n"
            f"Details: {layer1.raw_output[:500]}\n\n"
        )
        # Round-29 K1: sibling evidence renders in its OWN bounded
        # section — the anchor's 500-char window is never shared with
        # the siblings (the r28 string-append starved past the window;
        # each sibling now gets one bounded line, status first).
        _sibling_entries = [e for e in layer1.experiments if not e.is_anchor]
        if _sibling_entries:
            _sib_lines = ["## Sibling Experiments (also live, Layer-1 polled)\n"]
            for _se in _sibling_entries:
                _se_det = (_se.details or "")[:200]
                _sib_lines.append(
                    f"- {_se.uid}: {_se.status}"
                    + (f" - {_se_det}" if _se_det else "") + "\n"
                )
            _sib_lines.append(
                "\nThese experiments are ALSO live for this task. Factor "
                "their status into the task-level verification verdict.\n\n"
            )
            layer1_context += "".join(_sib_lines)
        if layer1.expired:
            layer2_instruction = (
                "Layer 1 shows the experiment has EXPIRED (status: Destroyed/Revoked). "
                "The fault was injected but has already timed out. "
                "You should still perform Layer 2 verification to confirm whether "
                "any residual effects remain, but expect that fault effects have dissipated. "
                "If no fault effects are observable, conclude Layer 2 as "
                "'recovered_before_observation' and add a Warning about the short duration.\n"
            )
        else:
            layer2_instruction = (
                "Layer 1 is PASSED. Now perform Layer 2 verification: "
                "use the available observation tools to verify the fault "
                "is actually in effect on the target.\n"
            )

    # Resource coverage from blade_query_k8s (if available)
    coverage_context = ""
    if layer1.affected_count > 0:
        coverage_context = "\n## Resource Coverage (from blade_query_k8s)\n"
        coverage_context += f"Affected resources: {layer1.affected_count}\n"
        if layer1.resource_statuses:
            coverage_context += "Per-resource details:\n"
            for rs in layer1.resource_statuses:
                identifier = rs.get("identifier", rs.get("id", "?"))
                state_val = rs.get("state", "?")
                success = rs.get("success", "?")
                coverage_context += f"  - {identifier}: {state_val} (success={success})\n"
        target_names = target.get("names", [])
        if target_names:
            coverage_context += f"Target resources from context: {target_names}\n"
        coverage_context += (
            "\nCompare affected_count against the expected number of target resources. "
            "If fewer resources are affected than expected, the fault injection has "
            "INCOMPLETE COVERAGE. Investigate and report this in VERIFICATION_RESULT Warnings.\n"
        )

    # Build fault metadata section
    fault_metadata = ""
    if fault_scope or fault_target or fault_action or injection_method:
        parts = []
        if fault_scope:
            parts.append(f"Scope: {fault_scope}")
        if fault_target:
            parts.append(f"Target: {fault_target}")
        if fault_action:
            parts.append(f"Action: {fault_action}")
        if injection_method:
            parts.append(f"Injection method: {injection_method}")
        fault_metadata = " | ".join(parts)

    from chaos_agent.transports import is_kubewiz_channel
    from chaos_agent.transports.registry import is_host_scope_channel
    _is_kubewiz = is_kubewiz_channel()
    # The programmatic post-check blocks below (disk fill / disk burn) describe
    # K8s CRD-mode concepts (container overlay, imagefs vs nodefs, tool pod) and
    # must NEVER surface for host-channel verification. Guard explicitly instead
    # of relying on the post-check artifacts merely happening to be k8s-only.
    _is_host_channel = is_host_scope_channel(state)
    context = (
        f"{layer1_context}"
        f"{coverage_context}"
        f"## Fault Context\n"
        f"Skill: {skill_name}\n"
        f"Target namespace: {target.get('namespace', '')}\n"
        f"Target names: {target.get('names', [])}\n"
        f"Blade params: {params}\n"
        f"Kubeconfig: {'(kubewiz)' if _is_kubewiz else (kubeconfig or '(default)')}\n"
    )
    # task-29848471: if the target names contain a transient injection
    # vehicle (debug/tool pod), the L2 model must be told explicitly not
    # to verify against it — the write-side defenses stop NEW vehicles
    # from entering the spec, but a polluted name already in state still
    # reaches this prompt. Point the model at the approved anchor instead.
    from chaos_agent.agent.execution_artifacts import is_vehicle_name
    _vehicle_hits = [
        n for n in (target.get("names") or []) if is_vehicle_name(n, state)
    ]
    if _vehicle_hits:
        _anchor = state.get("approved_target") or {}
        _anchor_names = list(_anchor.get("resolved_names") or _anchor.get("names") or [])
        _anchor_labels = _anchor.get("labels") or {}
        _anchor_desc = (
            f"names {_anchor_names}" if _anchor_names
            else (f"labels {_anchor_labels}" if _anchor_labels else "the approved target")
        )
        logger.warning(
            "verifier Fault Context contains vehicle name(s) %s; "
            "emitting anchor warning (anchor=%s)", _vehicle_hits, _anchor_desc,
        )
        context += (
            f"⚠ VEHICLE WARNING: {_vehicle_hits} in the target names above "
            "are TRANSIENT injection vehicles (debug/tool pods created to "
            "carry commands), NOT fault targets. Do NOT verify fault effects "
            f"against them. Verify ONLY the approved anchor: {_anchor_desc}.\n"
        )
    # Structured key parameters from parsed flags (e.g. path, percent, size)
    injection_parsed = state.get("injection_parsed_params") or {}
    if injection_parsed:
        context += f"Injection key parameters: {injection_parsed}\n"
    if fault_metadata:
        context += f"{fault_metadata}\n"
    # Timeout note: explicitly declared short timeouts run verbatim, so a
    # sub-300s experiment may genuinely have timed out — informational note
    _timeout_val = injection_parsed.get("timeout")
    if _timeout_val:
        try:
            _timeout_sec = int(str(_timeout_val).strip())
            if _timeout_sec < 300:
                context += (
                    f"ℹ Duration note: --timeout {_timeout_sec}s. "
                    f"If fault effects are not observable, consider that the fault "
                    f"may have timed out rather than failed.\n"
                )
        except (ValueError, TypeError):
            pass
    # Baseline data is now injected as synthetic AIMessage+ToolMessage pairs
    # (via _build_baseline_tool_messages) BEFORE the main HumanMessage,
    # instead of as a plain-text section inside HumanMessage.
    # This creates a "tool call result" narrative that makes the LLM
    # perceive baseline as "already obtained evidence" rather than
    # "external reference material" — reducing confirmation bias.
    #
    # The fallback "no usable data" note remains in HumanMessage because
    # there's nothing to convert to ToolMessage format.
    baseline = state.get("baseline_data")
    if not (baseline and baseline.get("success_count", 0) > 0):
        # Fallback: baseline capture produced no usable data
        context += (
            "\n## Baseline Data Note\n"
            "Baseline capture was attempted but produced no usable data. "
            "You MUST be cautious when interpreting absolute metric values — "
            "high resource usage does NOT necessarily mean the fault is in effect. "
            "Cross-validate with multiple independent data points "
            "(metrics + events + conditions) before concluding 'verified'.\n"
        )
    # Tool pod context: provide accurate information about tool pod capabilities
    if fault_scope == "node" and tool_pod_name:
        # The tool pod namespace is deployment-specific (task-e9bae269: the
        # pods lived in `default`, not `chaosblade`). It is never recorded in
        # state, so never assert one — instruct the LLM to resolve it first.
        # Tool-agnostic per the abstraction boundary: concrete command forms
        # (CRD status queries, exec patterns) live in knowledge docs.
        context += (
            f"\n## Available Tool Pod\n"
            f"A tool pod is available for cluster-level operations:\n"
            f"- Pod name: `{tool_pod_name}`\n"
            f"- Namespace: unknown — identify it across all namespaces before exec "
            f"(the namespace is deployment-specific; never assume it)\n"
            f"- Capabilities: injection-tool commands (status/destroy), cluster API checks (describe/top/get), "
            f"and host-level checks via `/host/...` (the tool pod typically mounts the host root).\n"
            f"- For CRD-mode disk fill, the fill file IS in the container overlay — "
            f"checking it inside this pod is the PRIMARY verification method.\n"
            f"- For host filesystem verification (e.g., `/proc/loadavg`, `/var/log`), "
            f"prefix paths with `/host` (try `/host/proc/loadavg` first; if missing, "
            f"fall back to bare `/proc/loadavg`). If this tool pod is unavailable or "
            f"lacks /host access, fall back to a node debug pod (busybox image) "
            f"and exec into it.\n"
            f"- **UID Dual Mapping**: The experiment UID ({experiment_uid}) is the CRD resource name. "
            f"Inside the tool pod, the injection tool's local status subcommand searches the LOCAL "
            f"experiment database and typically returns 'record not found' for an experiment "
            f"created through the cluster API — NEVER use it for this check (it causes a "
            f"false-negative Layer 2 conclusion). Query the experiment CRD through the cluster "
            f"API instead (discover the experiment resource kind via the cluster query "
            f"tool itself; knowledge docs provide reference forms).\n"
        )
    # Programmatic post-check: injection engine already verified the fill effect
    # during injection. This is authoritative — present it BEFORE verification
    # instructions so the LLM can use it as primary evidence.
    _post_check = state.get("disk_fill_post_check") or params.get("disk_fill_post_check")
    if not _is_host_channel and _post_check and isinstance(_post_check, dict):
        _fill_found = _post_check.get("fill_file_found", False)
        _target_pod = _post_check.get("target_pod", "unknown")
        _ls_out = _post_check.get("ls_output", "")
        _df_out = _post_check.get("df_output", "")
        context += (
            f"\n## Injection Engine Post-Check (already executed)\n"
            f"The injection engine programmatically verified the fill effect on "
            f"target node via tool pod `{_target_pod}`:\n"
        )
        if _fill_found:
            context += (
                f"- **Fill file FOUND** in container overlay — injection is WORKING.\n"
                f"- `ls` output:\n```\n{_ls_out[:300]}\n```\n"
            )
        else:
            context += (
                f"- **Fill file NOT found** in container overlay.\n"
                f"- `ls` output:\n```\n{_ls_out[:300]}\n```\n"
            )
        if _df_out:
            context += (
                f"- `df -h` output:\n```\n{_df_out[:300]}\n```\n"
            )
        context += (
            "Use this as PRIMARY evidence. If fill file was found, you have direct "
            "proof the fault is in effect. If not found, the fault may not have "
            "worked — cross-validate with other checks.\n"
        )
    # Programmatic post-check: injection engine already verified the burn I/O effect
    # during injection. This is authoritative — present it BEFORE verification
    # instructions so the LLM can use it as primary evidence.
    _burn_check = state.get("disk_burn_post_check") or params.get("disk_burn_post_check")
    if not _is_host_channel and _burn_check and isinstance(_burn_check, dict):
        _burn_detected = _burn_check.get("burn_io_detected", False)
        _active_parts = _burn_check.get("active_partitions", [])
        _burn_target_pod = _burn_check.get("target_pod", "unknown")
        _burn_node = _burn_check.get("node", "unknown")
        _burn_scope = _burn_check.get("scope", "node")
        if _burn_scope == "pod":
            _scope_desc = (
                f"target pod's node `{_burn_node}` via pod `{_burn_target_pod}`"
            )
        else:
            _scope_desc = (
                f"target node `{_burn_node}` via tool pod `{_burn_target_pod}`"
            )
        if _burn_detected:
            _parts_str = ", ".join(
                f"{p['name']}: ~{p['write_throughput_mb_s']} MB/s"
                for p in _active_parts[:5]
            )
            context += (
                f"\n## Disk Burn I/O Pre-Check (AUTHORITATIVE)\n"
                f"The injection engine programmatically verified the burn I/O effect on "
                f"{_scope_desc}.\n"
                f"Programmatic check confirmed: disk burn I/O is ACTIVE.\n"
                f"Write throughput on partition(s): {_parts_str}\n"
                f"This is DEFINITIVE evidence that the disk burn fault is in effect — "
                f"the dd processes are actively writing to the container overlay.\n"
                f"The I/O appears on the overlay's backing partition (typically imagefs, "
                f"e.g. /dev/vdb), NOT on the nodefs partition (e.g. /dev/vda3) where "
                f"/host/tmp resides. This is expected: CRD-mode burn writes to the "
                f"container overlay, not the host filesystem.\n"
                f"DO NOT conclude \"failed\" based on /host/tmp/ having no burn files "
                f"or nodefs (vda3) showing no I/O — the burn is on a DIFFERENT partition.\n"
                f"The measured write throughput above is the effect evidence "
                f"for the disk I/O verification step.\n"
            )
        else:
            context += (
                f"\n## Disk Burn I/O Pre-Check\n"
                f"The injection engine programmatically verified the burn I/O effect on "
                f"{_scope_desc}.\n"
                f"Burn I/O NOT detected on any partition. "
                f"Active partitions: {_active_parts[:5] or 'none with measurable I/O'}\n"
                f"The fault may not be in effect despite blade query reporting Success. "
                f"Cross-validate with other checks (ps | grep dd, iostat).\n"
            )
    context += (
        "\n## Injection Verification Instructions\n"
    )
    # Skill use-case content: PRIMARY AUTHORITY for verification
    skill_case = state.get("skill_case_content", "")
    if skill_case:
        context += (
            f"The following skill use-case defines how to verify this fault. "
            f"You MUST follow its verification approach as the primary reference.\n\n"
            f"<skill-case>\n{skill_case}\n</skill-case>\n\n"
        )
    # Planner's environment-adapted verification strategy (saved plan):
    # probed facts and anticipated negatives override the generic
    # skill-case steps when they conflict. Absent for simple tasks.
    plan_verification = state.get("plan_verification", "") or ""
    if plan_verification:
        context += (
            "### Planner's Verification Strategy (environment-adapted)\n"
            "The Phase 1 planner probed THIS environment and adapted the "
            "verification strategy below. Where it conflicts with the skill "
            "case's generic steps, the planner's environment-specific "
            "conclusions prevail on environment facts (e.g. an anticipated "
            "negative result) — never by lengthening a wait or slowing a "
            "criterion. The skill case still defines which steps to verify; "
            "the adaptation stays within the case's verification steps "
            "(no extra observation rounds or repeat windows).\n\n"
            f"<planner-verification>\n{plan_verification}\n</planner-verification>\n\n"
        )
    if skill_case:
        # Four-tier verification mode based on skill case structure:
        # Mode 0 (Multi-candidate): multiple candidates → LLM chooses
        # Mode 1 (Template): 注入验证 has parseable numbered/bullet steps → pre-filled checklist
        # Mode 2 (Guided):  注入验证 exists but unparseable (prose only) → point LLM to prose
        # Mode 3 (Free):    no 注入验证 → current generic guidance
        is_multi_candidate = "--- Candidate" in skill_case

        if is_multi_candidate:
            # ═══ Mode 0: MULTI-CANDIDATE — LLM picks the right one ═══
            context += (
                f"### Verification Strategy (Multi-candidate)\n"
                f"Multiple skill cases are provided above. You MUST:\n"
                f"1. Read ALL candidates carefully\n"
                f"2. Choose the ONE most relevant to the actual fault "
                f"(scope={fault_scope}, target={fault_target}, "
                f"action={fault_action})\n"
                f"3. State which candidate you chose and why "
                f"(one sentence)\n"
                f"4. Follow THAT candidate's **注入验证** steps as your "
                f"verification checklist. If it has no 注入验证 section, "
                f"design your own checklist based on the candidate's "
                f"content. Do NOT mix content from different candidates\n"
                f"5. Output a VERIFICATION_CHECKLIST section with each "
                f"step and its result before VERIFICATION_RESULT\n\n"
                f"Rules:\n"
                f"1. Replace [status] with: {_ITEM_STATUS_PROSE}\n"
                f"2. After [status], write \" — \" followed by brief "
                f"evidence\n"
                f"3. If a step cannot be executed, mark as skipped "
                f"with reason. Steps observing the injection itself "
                f"taking effect decide the verdict. Propagated effects "
                f"(OOM, latency, business impact) are NOT verdict "
                f"criteria: record evidence already in hand, otherwise "
                f"mark 'expected'/'not_applicable' — never wait, retry "
                f"or sample for them\n"
                f"4. **MANDATORY OUTPUT**: You MUST output a "
                f"'VERIFICATION_CHECKLIST:' section BEFORE your final "
                f"'VERIFICATION_RESULT:' section.\n"
                f"5. **chosen_candidate**: When calling "
                f"`submit_verification`, you MUST set "
                f"`chosen_candidate` to the candidate number you chose "
                f"(e.g. `chosen_candidate=2` for Candidate 2).\n"
            )
        else:
            step_descs = _extract_verification_step_descriptions(skill_case)
            has_section = _has_injection_verification_section(skill_case)

        if not is_multi_candidate and step_descs:
            # ═══ Mode 1: TEMPLATE — structured steps, single-tier verdict ═══
            # Verifier answers ONE claim: did the injection take effect
            # on the target. Steps observing that decide the verdict.
            # Steps describing propagated effects (OOM, latency, business
            # impact) are NOT verdict criteria — evidence already in hand
            # is recorded faithfully, never waited or sampled for.
            template_lines = []
            for i, desc in enumerate(step_descs, start=1):
                template_lines.append(
                    f"- Step {i}: [status] — {desc}"
                )
            template_str = "\n".join(template_lines)
            context += (
                f"### Verification Strategy (Template)\n"
                f"Steps from the skill case:\n"
                f"{template_str}\n\n"
                f"Step handling:\n"
                f"- Steps observing the injection itself taking effect on "
                f"the target decide the verdict — they are your priority.\n"
                f"- Steps describing propagated effects (OOM/eviction, "
                f"latency, business impact) are NOT verdict criteria: if "
                f"evidence is already in hand, record it faithfully "
                f"(absence included); otherwise mark 'expected' or "
                f"'not_applicable' — NEVER add waiting, retries or extra "
                f"sampling to observe them. If the step's target cannot "
                f"be instantiated here (placeholder like '应用 A'), mark "
                f"it 'not_applicable' with the reason; do NOT fabricate "
                f"a target.\n\n"
                f"Rules:\n"
                f"1. Line format: `Step N: <status> — <evidence>`,\n"
                f"   keeping the case's step numbering.\n"
                f"2. <status> ∈ {_ITEM_STATUS_PROSE}.\n"
                f"3. Every status needs evidence; for injection-effect "
                f"steps `expected` without an observation is invalid — \n"
                f"   use `skipped` if unchecked.\n"
                f"4. ANSWER all {len(step_descs)} steps — any status with a\n"
                f"   reason counts; silent omission is the only violation.\n"
                f"5. Verdict: 'passed' requires ALL injection-effect steps\n"
                f"   passed; a failed injection-effect step means the fault\n"
                f"   may not be in effect ('failed'/'partial').\n"
                f"6. Different method than specified? Note '(deviation: <why>)'.\n"
            )
        elif not is_multi_candidate and has_section:
            # ═══ Mode 2: GUIDED — 注入验证 exists but unparseable ═══
            context += (
                "### Verification Strategy (Guided)\n"
                "The skill case's **注入验证** section above contains "
                "verification guidance in prose format (no numbered steps). "
                "Read the section carefully and extract the verification "
                "intent. Design your own numbered VERIFICATION_CHECKLIST "
                "based on the checks described in the prose.\n\n"
                "Rules:\n"
                "1. Each checklist item must map to a distinct check "
                "described in the 注入验证 section\n"
                "2. Mark steps you cannot execute as: "
                "\"Step N: skipped — <reason>\". If a step's target cannot "
                "be instantiated in this environment (placeholder like "
                "'应用 A', unnamed service), mark it \"Step N: not_applicable — "
                "<reason>\" instead of fabricating a target\n"
                "3. Do NOT add checks that are not mentioned in the skill case\n"
                "4. Verdict principle: steps observing the injection itself "
                "taking effect decide the verdict. Propagated effects (OOM, "
                "latency, business impact) are NOT verdict criteria: record "
                "evidence already in hand, otherwise mark "
                "'expected'/'not_applicable' — never wait, retry or sample "
                "for them\n"
                "5. **Programmatic note**: Step coverage validation is "
                "DISABLED for this mode — we trust your extraction\n"
                "6. **MANDATORY OUTPUT**: You MUST output a "
                "'VERIFICATION_CHECKLIST:' section BEFORE your final "
                "'VERIFICATION_RESULT:' section.\n"
            )
        elif not is_multi_candidate:
            # ═══ Mode 3: FREE — no 注入验证 section at all ═══
            context += (
                "### Verification Strategy:\n"
                "1. The skill case has no 注入验证 section — design your own "
                "verification checklist for this fault type. The decisive "
                "check is whether the fault's expected EFFECT is observable "
                "on the target. You MUST execute EVERY step you list — do "
                "not skip any step.\n"
                "2. If a step cannot be executed (e.g., no Ingress configured "
                "in this cluster), you MUST explicitly note: '[SKIPPED] "
                "Step N: <reason>'. Do NOT silently omit steps.\n"
                "3. Before your final conclusion, output a **Verification "
                "Checklist** listing each step and its result:\n"
                "   - Step 1: passed/failed/skipped/expected/not_applicable — brief evidence\n"
                "   - Step 2: passed/failed/skipped/expected/not_applicable — brief evidence\n"
                "   - ...\n"
                "4. Verdict principle: steps observing the injection itself "
                "taking effect decide the verdict — ALL pass → 'passed'; ANY "
                "fails → 'failed'; decisive steps skipped without alternatives "
                "→ 'partial'. Propagated effects (OOM, latency, business "
                "impact) are NOT verdict criteria: record evidence already "
                "in hand, otherwise mark 'expected'/'not_applicable' — never "
                "wait, retry or sample for them.\n"
                "5. **MANDATORY OUTPUT**: You MUST output a "
                "'VERIFICATION_CHECKLIST:' section BEFORE your final "
                "'VERIFICATION_RESULT:' section. This checklist will be "
                "parsed programmatically. Without it, your verification will "
                "be flagged as potentially incomplete and may be downgraded "
                "from 'verified' to 'partial'.\n"
            )
        pass  # NEGATIVE EVIDENCE moved to core behavioral rules (outside if/else)
        context += (
            "**Checklist Status Choice**:\n"
            "- The checklist reports OBSERVED FACTS, not predictions.\n"
            "- Steps observing the injection effect: did you perform the "
            "check? Yes → 'passed'/'failed' by what you OBSERVED; No → "
            "'skipped'.\n"
            "- 'expected': the phenomenon is absent and the actual "
            "injection parameters make that the anticipated outcome. For "
            "propagated-effect steps (OOM, latency, business impact) this "
            "is valid WITHOUT observation; for injection-effect steps it "
            "requires the observation.\n"
            "- 'not_applicable': the step's target cannot exist in this "
            "environment (placeholder target, unnamed service). Do NOT "
            "fabricate a target.\n"
            "- Timing uncertainty belongs in Warnings, not in checklist status.\n"
            "- 'recovered_before_observation': the fault was transient and had dissipated "
            "by the time you checked — distinct from 'failed' (checked, fault absent).\n\n"
        )
        # ChaosBlade-specific: layer boundary
        if experiment_uid:
            context += (
                "Note — Layer boundary: VERIFICATION_CHECKLIST must ONLY contain Layer 2 "
                "checks (observable fault effects). Do NOT include Layer 1 items "
                "(Layer 1 tool check, experiment registration).\n\n"
            )
    else:
        context += (
            "No skill use-case content is available. Design verification based on "
            "the fault-specific hints below and your expertise.\n"
            "Before your final conclusion, output a **Verification Checklist** listing "
            "each check and its result:\n"
            "   - Check 1: passed/failed/skipped — brief evidence\n"
            "   - ...\n"
            "**WARNING**: Without skill guidance, verification may be incomplete. "
            "At minimum, verify the fault effect is observable on the target.\n"
            "**Knowledge docs**: Check the Domain Knowledge Index for documents whose "
            "\"When to read\" field covers your current scenario (e.g., verification "
            "strategies, kubectl field reference). Use `read_knowledge_resource` to "
            "load them before designing your verification plan.\n\n"
        )
    # ── Core behavioral rules (always added, positioned early) ──
    context += (
        f"{layer2_instruction}"
    )
    context += (
        "**NEGATIVE EVIDENCE ENUMERATION (CRITICAL)**:\n"
        "Before concluding Layer2 'passed', you MUST include a 'Negative Evidence' section "
        "in your reasoning that explicitly lists EVERY observation contradicting or weakening "
        "the conclusion that the fault is in effect. For each item, either:\n"
        "(a) Dismiss it with factual basis (not speculation), or\n"
        "(b) Accept it as valid counter-evidence.\n"
        "If ANY element of the effect claim (a significant change observed, "
        "attributable to the injection) is demonstrably NOT met, you MUST "
        "conclude Layer2 as 'partial' or 'failed' — NOT 'passed'. "
        "(Absence of propagated effects (OOM, latency, business impact) is "
        "NOT counter-evidence against the fault being in effect.)\n\n"
    )
    context += _EVIDENCE_SEMANTICS_PROMPT
    # Add fault-specific verification hints when metadata is available
    verification_hints = _get_fault_verification_hints(
        fault_scope, fault_target, fault_action,
        parsed_flags=injection_parsed,
    )
    if verification_hints:
        context += (
            f"\n### Fault-Specific Verification Hints\n"
            f"{verification_hints}\n\n"
        )
    # Per-injection-method verifier note, owned by the resolved backend provider
    # (kubectl_exec BusyBox reference, kubectl-native note, host-native note).
    from chaos_agent.agent.providers import FaultProviderRegistry

    _method_provider = FaultProviderRegistry.resolve_by_method(injection_method)
    method_note = (
        _method_provider.verify_prompt_note(
            injection_method, injection_pod_name=tool_pod_name
        )
        if _method_provider is not None
        else ""
    )
    if method_note:
        context += method_note + "\n"
    # ── Conditional rules (only when relevant) ──
    if baseline and baseline.get("success_count", 0) > 0:
        context += f"{_BASELINE_INTEGRITY_PROMPT}\n\n"
    if experiment_uid:
        context += (
            "**Layer 1 Limitation**: Layer 1 only checks whether the fault experiment "
            "is registered. It does NOT verify that the fault effect is observable. "
            "Your Layer 2 verification is the ONLY way to confirm the fault is working.\n\n"
        )
    # Default minimal-container note when no backend contributed a method note
    # (host_blade delivery or undetected): a kubectl exec into a minimal image
    # may lack common shell utilities.
    if not method_note:
        context += (
            "**NOTE**: Some minimal container images lack common shell utilities (top, ps, netstat, etc.). "
            "If a container exec check returns empty output or \"command not found\", do NOT retry — "
            "use a cluster API describe-style check instead.\n\n"
        )
    # ── Always-on helpers ──
    context += (
        f"### Test Pod Namespace Selection\n"
        f"When you need a Running application pod for verification tests "
        f"(DNS resolution, network connectivity, service calls), do NOT "
        f"only search the `default` namespace. Search in the TARGET "
        f"namespace first ({target.get('namespace', '') or 'see Fault Context'}), "
        f"then other namespaces with workloads. For cluster-wide faults "
        f"(DNS, node-level), any Running pod with shell access can serve "
        f"as a test target.\n\n"
    )
    if fault_scope == "node":
        context += (
            "### Debug Pod Cleanup\n"
            "If you create any pods during verification (e.g., temporary test pods), "
            "you MUST delete them before finishing verification. Add cleanup as a "
            "final step in your checklist.\n"
            "Note: framework-managed host-access pods (the carrier tool pods "
            "surfaced in the hints above) are DaemonSet-managed and MUST NOT be "
            "deleted.\n"
        )
    context += convergence_hint
    return context




