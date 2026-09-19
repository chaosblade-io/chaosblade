"""Layer 1 domain for recover verifier: the GENERIC recovery plumbing.

Extracted from recover_verifier.py to isolate the "execute recovery" layer
from the "verify recovery" layer (Layer 2). The ChaosBlade execution domain
(blade_destroy + blade_status and their parsers) physically lives in
``providers/chaosblade/recover.py`` since phase-3 T2; the transitional
aliases this module used to re-export were retired with phase-5.

Symbols (owned here — generic):
  Constants: _RECOVER_BASELINE_TOOL_CALL_ID,
             _RECOVER_SYNTHETIC_TOOL_CALL_IDS, _RECOVER_CONTEXT_KWARGS_KEY
  Dataclass: RecoverLayer1Result
  Functions: _build_recover_baseline_tool_messages,
             _build_layer1_recovery_prompt, _parse_layer1_recovery_result
"""

import logging

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.prompts.reminder import SYSTEM_REMINDER_DECLARATION
from chaos_agent.agent.result.verdict import Layer1Result
from chaos_agent.transports import PROFILE_K8S
from chaos_agent.utils.truncation import build_truncation_notice

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Layer 1 result — reuses verdict.Layer1Result (Pydantic)
# ---------------------------------------------------------------------------

# Backward-compat alias so existing imports don't break.
RecoverLayer1Result = Layer1Result


_RECOVER_BASELINE_TOOL_CALL_ID = "recover_baseline_collector"

# Per-observation render budget: baseline observations are head-capped
# with the shared truncation notice when oversized.
_BASELINE_RENDER_MAX_CHARS = 1500

# NOTE (round-41 retirement): this module used to host a "compactor
# cache restore bridge" — parse `Cache: <path>` out of observation
# stdout, gate on truncation markers, read the file back, and inject it
# as restored baseline evidence. It was retired because no producer
# ever writes compactor notices into baseline observation stdout (the
# baseline executors store raw result.stdout; compactor notices only
# ever live in conversation ToolMessages — a different data path), so
# the bridge's genuine branch was dead code, while its spoof branch
# (a planted `Cache: /etc/passwd` in workload-controlled text echoed by
# kubectl describe) steered unconstrained file reads on the operator
# machine. Absent code cannot be spoofed, bypassed, or regress — the
# honest uniform render below (head cap + shared notice) is the whole
# story for every observation, trusted or not.


# Aggregate set of all synthetic tool_call_ids used for state persistence.
_RECOVER_SYNTHETIC_TOOL_CALL_IDS = frozenset({
    _RECOVER_BASELINE_TOOL_CALL_ID,
})

# STABLE langchain message ids for the synthetic pair — see the matching
# comment in verify/_verifier_messages.py: the dedup gate rebuilds on damage
# and ``add_messages`` replaces by id, so stable ids keep the rebuild
# idempotent instead of appending a duplicate pair set to state every turn.
_RECOVER_BASELINE_MSG_ID_CALLER = "synthetic:recover:baseline:caller"
_RECOVER_BASELINE_MSG_ID_RESULT = "synthetic:recover:baseline:result"

# Marker for the main recover context HumanMessage — used to identify
# the ephemeral HumanMessage that should be persisted to AgentState on
# is_first_layer2 so it remains visible on subsequent iterations.
_RECOVER_CONTEXT_KWARGS_KEY = "_recover_main_context"


def _build_recover_baseline_tool_messages(baseline: dict) -> list:
    """Build synthetic AIMessage + ToolMessage pair for baseline data in recovery verification.

    Mirrors verifier.py's _build_baseline_tool_messages pattern: baseline data
    injected as synthetic tool call results BEFORE the HumanMessage, creating
    a causal narrative ("already obtained baseline") instead of "external reference".

    For recovery verification, the framing emphasizes "back to normal" comparison:
    baseline values are the RECOVERY TARGET — current observations should match these.

    Returns:
        [AIMessage, ToolMessage] pair. Empty list if baseline has no usable data.
    """
    if not baseline or baseline.get("success_count", 0) <= 0:
        return []

    captured_at = baseline.get("captured_at", "unknown time")
    source = baseline.get("source", "unknown")
    observations = baseline.get("observations", [])

    obs_lines = []
    for obs in observations:
        if obs.get("exit_code") != 0 or not obs.get("stdout"):
            continue
        desc = obs.get("description", "unknown metric")
        cmd = obs.get("command", "")
        raw_out = obs["stdout"]
        # Uniform honest render (round-41 retirement, see module NOTE):
        # every observation — with or without any marker-like text —
        # renders head-capped, and the shared notice flags oversize with
        # the honest size and the state-baseline retrieval guidance.
        output = raw_out[:_BASELINE_RENDER_MAX_CHARS]
        if len(raw_out) > _BASELINE_RENDER_MAX_CHARS:
            # Shared contract (kind=baseline-evidence): marker +
            # honest size + retrieval guidance. No cache parsing —
            # observation stdout is workload-echoed text, never a
            # compactor notice.
            output += build_truncation_notice(
                "baseline-evidence", len(raw_out), unit="characters",
            )
        obs_lines.append(
            f"### {desc}\n"
            f"Command: `{cmd}`\n"
            f"```\n{output}\n```"
        )

    if not obs_lines:
        return []

    # Same denominator contract as verify/_verifier_messages: legacy
    # baselines lack total_count — the observation count in hand is the
    # honest fallback (0 fallback rendered "N/0" receipts, #13/#10 audits).
    content = (
        f"Pre-injection baseline collected at {captured_at} "
        f"(strategy: {source}, {baseline.get('success_count', 0)}/"
        f"{baseline.get('total_count', len(observations))} succeeded).\n\n"
        f"These metrics were captured BEFORE fault injection using the same "
        f"observation methods you should use for recovery verification. "
        f"Recovery is confirmed when YOUR CURRENT observations return to "
        f"these baseline levels. Compare using delta format: "
        f"\"baseline: X → current: Y (ΔZ)\". "
        f"If current ≈ baseline → recovery confirmed. "
        f"If current ≠ baseline → fault effect still present.\n\n"
        + "\n\n".join(obs_lines)
    )

    ai_msg = AIMessage(
        content="",
        id=_RECOVER_BASELINE_MSG_ID_CALLER,
        tool_calls=[{
            "name": "recover_baseline_collector",
            "args": {"phase": "pre-injection", "purpose": "recovery_comparison"},
            "id": _RECOVER_BASELINE_TOOL_CALL_ID,
            "type": "tool_call",
        }],
    )
    tool_msg = ToolMessage(
        content=content,
        id=_RECOVER_BASELINE_MSG_ID_RESULT,
        tool_call_id=_RECOVER_BASELINE_TOOL_CALL_ID,
        name="recover_baseline_collector",
    )
    return [ai_msg, tool_msg]


# ---------------------------------------------------------------------------
# Layer 1 (non-ChaosBlade): LLM-driven recovery execution prompt & parser
# ---------------------------------------------------------------------------

def _build_layer1_recovery_prompt(
    *, is_kubectl_blade: bool = False, profile: str = PROFILE_K8S
) -> str:
    """Build the Layer 1 recovery execution system prompt.

    Args:
        is_kubectl_blade: If True, this is a ChaosBlade experiment created via
            kubectl exec into a cluster pod (e.g., otel-c-tool). The recovery
            must use `blade destroy` via kubectl exec, not host blade_destroy.
            If False, this is a true non-ChaosBlade fault (kubectl-native).
        profile: Channel profile ("k8s"|"host"). Selects the environment
            capability fragment appended after the intro, mirroring how the
            inject phases and the Layer-2 recover verifier inject
            ``environment.prompt_fragment(...)``. The generalized (non-kubectl)
            branch stays profile-agnostic; the capability fragment is what
            tells a host recovery executor it has no cluster resource semantics.
    """
    from chaos_agent.agent.environment_profiles import get_environment_profile

    environment = get_environment_profile(profile)
    capability_fragment = (
        environment.prompt_fragment("recover")
        if environment is not None
        else (
            "## Capability Profile\n"
            "The current environment profile is unsupported. Do not attempt "
            "recovery; report the missing environment capability."
        )
    )

    if is_kubectl_blade:
        # U-shaped attention (same architecture as execute_loop / verifiers):
        # critical constraints at BEGINNING (primacy) + REMEMBER at END
        # (recency); procedure details in the middle, where attention dips.
        return f"""You are executing recovery actions for a chaos engineering fault.

Your single objective: restore the target to its pre-fault state.

## CRITICAL CONSTRAINTS
- This fault was injected from INSIDE the cluster — the injection tool ran
  within a tool pod, not on the host. Host-side recovery tools cannot see or
  undo such experiments: the undo action MUST go through the same in-cluster
  channel that performed the injection. DO NOT use host-side destroy/status
  tools for this experiment.
- NEVER assume the tool pod namespace — it is deployment-specific; use only
  the namespace you discover via live cluster queries.
- DO NOT verify the fault has been removed — that is Layer 2's job, not yours.
- DO NOT use interactive commands — they do not work in automation; translate
  them into programmatic equivalents.
- {SYSTEM_REMINDER_DECLARATION}

{capability_fragment}

## How to derive recovery commands
- WHAT to undo comes from the recovery context below: the experiment UID and
  the recorded impact — and the original injection pod, when it is named.
- WHERE to run it comes from live cluster queries — discover the current
  state first; never assume it.
- HOW comes from the tools you actually hold: inspect a tool's own
  help/usage output to confirm the commands and parameters it supports,
  and trust its runtime output. Documentation and conversation history may
  be outdated — the tool's runtime behavior is the ground truth.

## Recovery Procedure
1. Locate a currently running tool pod. If the recovery context below names
   the original injection pod, prefer it, and identify its namespace before
   use (it is deployment-specific — NEVER assume it). Tool pods rotate, and
   the context may name no pod at all — then discover a running one by its
   tool label ACROSS ALL NAMESPACES, using live cluster queries.
2. Inside the located tool pod, in ITS own namespace, run the
   experiment-destroy command for the experiment UID. Confirm the exact
   destroy syntax from the injection tool's own help/usage inside the pod
   before running it.
3. Confirm the destroy output reports success.

## Kubeconfig Requirement
If a kubeconfig path is provided in the recovery context, you MUST pass it to
EVERY cluster tool call. The default kubeconfig cannot access the target
cluster; omitting it connects tool calls to the WRONG cluster.

# REMEMBER
- Undo through the SAME in-cluster channel that performed the injection —
  host-side tools cannot reach cluster-created experiments.
- Discover the tool pod and its namespace with live cluster queries; NEVER
  assume either.
- A tool's own help/usage output and runtime behavior are the ground truth
  over documentation and memory.
- Your job ends when the undo action is confirmed landed at the API layer —
  Layer 2 verifies the recovery outcome.

## Output
After completing the recovery action (or determining it cannot be completed),
output a FINAL summary in this EXACT format:

RECOVERY_EXECUTION_RESULT:
- Status: [success/failed]
- Actions: [summary of actions taken, e.g., "destroyed the experiment via the in-cluster tool pod"]
- Details: [any errors, warnings, or notes]
"""

    return f"""You are executing recovery actions for a chaos engineering fault.

This fault has no experiment carrier. EXECUTE the recovery actions to remove the fault
effect using ONLY the tools bound in the current environment. You are NOT
verifying — Layer 2 owns outcome verification.

{capability_fragment}

## Important Constraints
- Do NOT verify the fault has been removed — that is Layer 2's job, not yours.
- Do NOT invoke tools that are not currently bound (there is no experiment carrier
  to destroy here).
- Do NOT use interactive commands that require a TTY — translate them into
  programmatic equivalents.
- Treat the configured target authority and current tool observations as the
  authority for recovery actions: inspect a tool's own help/usage output to
  confirm what it supports, and trust its runtime output over documentation
  or memory.
- If an action fails, use another supported recovery approach only when new
  evidence justifies it; otherwise report the blocker precisely, and do not
  broaden scope to compensate for an error.
- {SYSTEM_REMINDER_DECLARATION}

## Instructions
1. Use the Recovery Actions and injection context to determine what must be undone.
2. Execute each supported recovery action through the currently bound tools.
3. Preserve the target boundary.
4. A receipt proves the undo was issued, not that it took effect — a
success claim rests on direct evidence the fault cause is revoked or
visibly being removed; immediate evidence, not a wait for recovery.

# REMEMBER
- Execute ONLY through currently bound tools; inspect their help/usage and
  trust their runtime output over documentation or memory.
- Preserve the target boundary — do not broaden scope to compensate for an
  error.
- Your job ends when the recovery actions are confirmed landed at the API
  layer — Layer 2 verifies the recovery outcome.

## Output
After completing ALL recovery actions (or determining they cannot be completed),
output a FINAL summary in this EXACT format:

RECOVERY_EXECUTION_RESULT:
- Status: [success/failed]
- Actions: [summary of actions taken]
- Details: [any errors, warnings, or notes]
"""


def _parse_layer1_recovery_result(text: str) -> RecoverLayer1Result:
    """Parse the LLM's Layer 1 recovery execution result into a RecoverLayer1Result."""
    text_lower = text.lower()

    # Extract status
    if "status: success" in text_lower or "status:  success" in text_lower:
        status = "passed"
    elif "status: failed" in text_lower or "status:  failed" in text_lower:
        status = "failed"
    elif "recovery_execution_result" in text_lower:
        # Has the result block but unclear status — check for positive indicators
        if "error" in text_lower or "failed" in text_lower:
            status = "failed"
        else:
            status = "passed"
    else:
        # No structured output — assume success if tools were used
        status = "passed"

    # Extract details
    details = ""
    for line in text.split("\n"):
        line_lower = line.strip().lower()
        if line_lower.startswith("actions:") or line_lower.startswith("details:"):
            details += line.strip() + "; "
    if not details:
        # Use first 300 chars as fallback
        details = text.strip()[:300]

    raw_output = text.strip()[:500]

    return RecoverLayer1Result(
        status=status,
        details=details.rstrip("; ") if details else "Recovery execution completed",
        raw_output=raw_output,
    )
