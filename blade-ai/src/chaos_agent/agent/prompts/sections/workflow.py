"""Workflow sections: two-phase workflow, NL mode, verification strategy, replan."""

from chaos_agent.agent.prompts.constants import REPLAN_MARKER


# ---------------------------------------------------------------------------
# Reusable verification sub-sections (shared with verification.py)
# ---------------------------------------------------------------------------


def get_fault_effect_delay_section() -> str:
    """Fault effect delay awareness — shared by inject and verifier prompts."""
    return """### Fault Effect Delay
Fault injection is NOT instantaneous. After the injection command reports Success:
- The actual fault effect may take **5-30 seconds** to become observable
- The injection tool needs to deploy and start the fault process on the target
- Kubernetes metrics-server has its own sampling interval (typically 15-30s)"""


def get_multi_iteration_section() -> str:
    """Multi-iteration verification pattern — shared by inject and verifier prompts."""
    return """### Multi-Iteration Verification Pattern
1. **Iteration 1**: Run initial checks (kubectl top, kubectl describe)
2. **Iteration 2**: If iteration 1 showed no effect, re-check key indicators
3. **Iteration 3+**: Consolidate findings. Only conclude "not in effect" after 2+ checks"""


def get_minimal_container_section() -> str:
    """Minimal container handling guidance — shared by inject and verifier prompts."""
    return """### Minimal Container Handling
Some container images lack common utilities (top, ps, netstat, etc.):
- If `kubectl exec` returns "command not found", do NOT retry similar commands
- Switch to `kubectl describe` for Pod-level signals (restart count, conditions, events)
- Use `kubectl get -o json` for structured data when exec is unavailable"""


def get_verification_method_priority_section() -> str:
    """Verification method priority — shared by inject and verifier prompts."""
    return """### Verification Method Priority
1. Skill-provided injection verification instructions (highest confidence)
2. Fault-specific patterns from domain knowledge (e.g., CPU stress → kubectl top)
3. General health checks (kubectl describe, events, conditions)"""


def get_handling_ambiguous_results_section() -> str:
    """Decision heuristic for handling ambiguous verification results."""
    return """### Handling Ambiguous Results
When tool output contradicts expectations:
1. Consider timing — metrics may not reflect the fault yet (wait 15-30s and re-check)
2. Cross-validate with a different command — if kubectl top shows no change, check kubectl describe for condition changes
3. Never infer from absence — "no signal" is not "no fault" until timing is accounted for"""


def get_verification_method_reasoning_section() -> str:
    """Decision heuristic for choosing verification method based on fault type."""
    return """### Verification Method Selection Reasoning
Beyond the priority order, choose your verification method based on the fault type:
- CPU/Memory stress → kubectl top (quantitative metrics) + kubectl describe (conditions)
- Network delay/loss → kubectl exec connectivity test (application impact) + kubectl describe (events)
- Pod kill/crash → kubectl get pods (restart count) + kubectl describe (events/OOMKilled)
- Disk fill → kubectl exec df -h (filesystem) + kubectl describe node (DiskPressure condition)
- Node-level faults → kubectl describe node (conditions) + cross-namespace pod status check
If the skill provides specific verification instructions, they OVERRIDE these general patterns"""


def get_evidence_sufficiency_section() -> str:
    """Decision heuristic for evidence sufficiency in verification."""
    return """### Evidence Sufficiency
Sufficient evidence requires:
1. At least 2 independent data points confirming the same conclusion
2. Data from different verification layers (e.g., metrics + events, not just two metrics calls)
3. Timing accounted for — if all evidence is from a single point in time, wait and re-check
A single positive data point is a hint, not a conclusion"""


def get_verification_heuristics_compact_section() -> str:
    """Compact merged section — replaces 5 separate sections for verifier prompt.

    Combines: fault delay, minimal container, method priority, method
    reasoning, evidence sufficiency, and ambiguous results into ONE
    concise section (~800 chars). Detailed content is available via
    read_knowledge_resource('verification-heuristics.md') on demand.

    Design rationale: 5 separate sections (~2,600 chars / ~650 tokens)
    occupied the middle of the verifier system prompt — a Lost-in-the-Middle
    high-risk zone. Merging them into one compact section reduces middle-area
    noise while preserving the essential rules. The LLM can load detailed
    guidance on demand via knowledge documents.
    """
    return """## Verification Heuristics (compact — see knowledge docs for details)

- **Delay**: Fault effects take 5-30s to appear. Do NOT conclude "not in effect" from a single observation — re-check after delay.
- **Minimal container**: If `kubectl exec` returns "command not found", switch to `kubectl describe` or `kubectl get -o json`. Do NOT retry similar commands.
- **Method priority**: Skill instructions > knowledge patterns > general health checks. CPU/Memory → kubectl top + describe; Network → connectivity test; Disk → df -h + describe node; Pod kill → get pods + describe.
- **Evidence**: Need 2+ independent data points from different verification layers. Single data point = hint, not conclusion.
- **Ambiguous**: Cross-validate with different commands. "No signal" ≠ "no fault" until timing is accounted for.
- **Transient faults**: Some faults produce cyclic/short-lived effects. If ANY observation shows a clear change from baseline, mark 'passed', NOT 'recovered_before_observation'. Only use 'recovered_before_observation' when NO observation at ANY point showed fault effects.
- **RestartCount**: Compare current restartCount with pre-injection baseline. Only a NEW restart (count > baseline) indicates restart during injection window."""


# ---------------------------------------------------------------------------
# Composite section (backward compatible)
# ---------------------------------------------------------------------------


def get_verification_strategy_section(brief: bool = False) -> str:
    """Verification strategy and delay awareness section.

    Composes from reusable sub-sections so that both inject and verifier
    prompts can share the same content without copy-paste duplication.

    Args:
        brief: When True, return a compact ≤10-line principle version that
            keeps the ``"verification"`` keyword. The verifier prompt should
            still pass ``brief=False`` to receive full heuristics; inject
            Phase 1 only needs the principles to draft a plan's Verification
            Methods section. Full heuristics are sourced on demand from the
            ``verification-heuristics`` knowledge doc.
    """
    if brief:
        return """## Verification Strategy (Principles)
- Fault effects are NOT instantaneous — wait 5-30 s after blade Success before observing.
- Use multi-iteration verification: 2+ checks before concluding "no effect".
- Cross-validate across layers (metrics + events), not two reads of the same metric.
- If a tool is unavailable inside the container (top/ps missing), switch method (kubectl describe / get -o json) — do NOT retry the same command.
- Skill-provided verification overrides general heuristics.

For the full heuristic catalogue (method-by-fault-type mapping, evidence sufficiency, ambiguous-result handling), call ``read_knowledge_resource('verification-heuristics.md')``."""

    parts = [
        "## Verification Strategy",
        "",
        get_fault_effect_delay_section(),
        "",
        get_multi_iteration_section(),
        "",
        get_minimal_container_section(),
        "",
        get_verification_method_priority_section(),
        "",
        get_verification_method_reasoning_section(),
        "",
        get_evidence_sufficiency_section(),
        "",
        get_handling_ambiguous_results_section(),
    ]
    return "\n".join(parts)


def get_core_principles_section() -> str:
    """Core anti-hallucination principles — primacy zone anchor.

    Concise form of Workflow's Ground Truth, placed at the prompt beginning
    for U-shaped attention. The full version with rationale lives in
    Workflow's Ground Truth subsection; REMEMBER at the end reinforces
    these same rules (recency zone).
    """
    return """# Core Principles
- FAULT INTENT parameters are UNVERIFIED — verify with tools before trusting them
- When tool output contradicts FAULT INTENT or documentation, the TOOL is correct
- Before calling finish_planning, you MUST cite tool output that proves the target exists"""


def get_remember_section() -> str:
    """REMEMBER segment — recency zone anchor for U-shaped attention.

    Reinforces the anti-hallucination principles from Core Principles and
    Workflow Ground Truth, plus one workflow rule about propose_plan_change.
    """
    return """# REMEMBER
- FAULT INTENT parameters are UNVERIFIED — verify with tools before trusting them
- When tool output contradicts FAULT INTENT or documentation, the TOOL is correct
- Before calling finish_planning, you MUST cite tool output that proves the target exists
- Do NOT use propose_plan_change for parameter fixes — only for fault TYPE changes"""


def get_executor_core_principles_section() -> str:
    """Core execution principles — primacy zone anchor for Phase 2.

    Phase 2's root cause: the LLM's tool interface knowledge from docs
    (skill case, knowledge docs) is UNVERIFIED. The tool's runtime behavior
    (help output, error messages) is the ground truth.

    Mirrors Phase 1's get_core_principles_section() pattern: same root
    principle (tool is ground truth), applied to the execution context.
    Three rules form a complete loop — before calling (discover),
    during failure (adapt), after completion (stop).

    The 'stop' rule is step-aware: a fault injection may consist of
    multiple atomic steps (e.g., kubectl patch → kubectl delete → observe).
    A single step's success is progress, not completion. The LLM must
    continue calling tools until ALL steps are done, then STOP and let
    the verifier handle verification.
    """
    return """# Core Principles
- Tool interface knowledge from docs is UNVERIFIED — discover the actual interface from the tool itself
- When a tool returns error, the TOOL is right — adapt immediately, do not retry or re-plan
- When ALL injection steps are complete, STOP — do not verify or recover (verification is automatic)"""


def get_executor_remember_section() -> str:
    """REMEMBER segment — recency zone anchor for Phase 2 U-shaped attention.

    Reinforces the same three rules from executor Core Principles, plus
    one replan escape rule. Must stay verbatim aligned with Core Principles
    for U-shaped attention integrity.
    """
    return """# REMEMBER
- Tool interface knowledge from docs is UNVERIFIED — discover the actual interface from the tool itself
- When a tool returns error, the TOOL is right — adapt immediately, do not retry or re-plan
- When ALL injection steps are complete, STOP — do not verify or recover (verification is automatic)
- If all injection methods fail, output [REPLAN] — do not retry exhausted approaches"""


def get_workflow_section() -> str:
    """Workflow phases section — tool-agnostic, verification as structural backbone.

    Design principles:
    1. Ground Truth at top — establishes fact priority (tool > FAULT INTENT > docs).
    2. Verification (Step 3) is the structural centerpiece, not buried in a list.
    3. Tool-agnostic — no external CLI tool names (blade/kubectl) in principle
       sections. Concrete tool names live only in the Tools section.
       Internal framework APIs (activate_skill, finish_planning, etc.) keep
       their names — they are the agent's own interface.

    Keeps the Analyze / Activate / Verify verbs frozen by
    ``tests/test_agent/test_prompts.py``.
    """
    return """## Workflow
You operate in TWO phases — the system transitions automatically.

### Phase 1 (current): Planning — read-only by enforcement

### Ground Truth
FAULT INTENT parameters are UNVERIFIED. When tool output contradicts them,
the TOOL is correct — use verified values, not original ones.
The same applies to documentation: when a tool's runtime behavior contradicts
skill docs or knowledge docs, the tool is right. Adapt to what the tool
actually does.

### Steps
1. **Analyze** the FAULT INTENT → fault type, target (namespace / resource /
   names), parameters. Note: these are UNVERIFIED — do not trust them yet.
2. **Activate** the matching skill ONCE via `activate_skill` (do NOT re-activate).
3. **Verify** the target exists with read-only cluster query tools — THE
   critical step for plan reliability:
   - Query the target by the provided identifier (labels, names, etc.)
   - If the query returns empty → the identifier is WRONG. List resources
     by name, inspect their metadata to discover the correct identifier.
   - Before proceeding, state: "Verified: N resources match <key>=<value>"
   - You MUST be able to cite the specific tool output that proves the
     target exists. If you cannot cite evidence, do NOT proceed.
4. **Read** skill resources / knowledge docs to determine the correct
   injection command and parameters. Treat templates as RECIPES for
   Phase 2 — do not execute them here.
5. **Assess complexity** (optional `save_fault_plan`):
   - Simple (single target, single fault, trivial rollback): skip plan,
     go to step 6.
   - Complex (multi-target, multi-step, cascading, large blast radius):
     call `save_fault_plan` with a markdown plan (Task Summary /
     Execution Steps / Expected Impact / Rollback and Recovery /
     Verification Methods). Pass the `task_id` from the user's conversation.
   - When writing Verification Methods: fault effects are NOT instantaneous
     (may take 5-30s to propagate). Plan for multi-iteration verification
     (2+ checks before concluding "no effect").
5b. **Reject ONLY when technically impossible**: You may ONLY call
   `finish_planning(rejected=True, ...)` when the request **cannot be done**:
   target does not exist after verification, no matching use-case after
   browsing the catalogue, or the injection method is fundamentally
   unsupported on this target.
   In ALL other cases — including safety concerns or blast-radius warnings
   — you MUST complete planning normally (`rejected=False`) and include
   your concerns in the `summary`. The system handles risk decisions via
   `safety_check` → `confirmation_gate`.
   When rejecting, provide 2-4 actionable alternatives against the same target
   (numbered list, each with fault type + brief description + risk level).
6. **End Phase 1** by calling `finish_planning` with VERIFIED parameters:
   - `finish_planning(summary="...")` → proceed to safety check and execution.
   - `finish_planning(summary="...", rejected=True, rejection_reason="...")` →
     reject the request (the system ends cleanly).
   When proceeding, you MUST include:
   - `blast_radius_scope`: `"target-only"` | `"namespace-wide"` |
     `"cluster-wide"` (cluster-wide triggers elevated safety review)
   - `blast_radius_detail`: specific resources affected
   - `skill_case_resource`: resource_path of chosen case (if multiple were read)
   Do NOT end Phase 1 without calling `finish_planning`.

### Phase 2 (automatic): Execution — mutation tools bound after approval.
Phase 1 is read-only. Mutation tools are bound automatically in Phase 2
after `finish_planning` → safety_check → user approval. See Tool Usage
Guidelines for available tools."""



def get_replan_section(replan_context: dict | None = None, replan_history: list | None = None) -> str:
    """Replan mode section — injected when Phase 2 error triggers replan."""
    if not replan_context:
        return ""
    parts = [
        "## Replan Mode — Phase 2 Execution Failed",
        "You are re-entering Phase 1 because Phase 2 execution encountered an error.",
        f"**Error Summary**: {replan_context.get('error_summary', 'Unknown')}",
        f"**Failed at iteration**: {replan_context.get('iteration_at_failure', '?')}",
    ]
    existing_uids = replan_context.get("existing_blade_uids", [])
    if existing_uids:
        parts.append(f"**Existing experiments (partial success)**: {', '.join(existing_uids)}")
        parts.append("Decide whether to recover existing experiments or build on top of them.")
    else:
        parts.append("No experiments were successfully created.")

    failed_calls = replan_context.get("failed_tool_calls", [])
    if failed_calls:
        parts.append("\n### Failure Chain (chronological — analyze the FULL chain)")
        for i, fc in enumerate(failed_calls, 1):
            parts.append(f"{i}. `{fc.get('name', '?')}` args={fc.get('args', {})}")
            parts.append(f"   → {fc.get('error', '?')}")
        parts.append("")
        parts.append("Look for the ROOT CAUSE at the beginning of the chain,")
        parts.append("not just the last error. The last error is often a symptom.")

    if replan_history:
        parts.append("\n### Previous Replan Attempts (DO NOT repeat these approaches)")
        for entry in replan_history:
            parts.append(f"- Attempt {entry.get('attempt', '?')}: {entry.get('action_taken', '?')} — {entry.get('original_error', '?')}")

    parts.extend([
        "\n### Replan Instructions",
        "1. Re-verify the target using available read-only tools",
        "2. Re-read the skill case to confirm correct injection parameters",
        "3. **Runtime overrides documentation**: If the error indicates a rejected "
        "parameter/command/syntax, do NOT include it in your corrected plan. "
        "Plan a verification step: run the tool's help/usage command to discover "
        "its actual interface before calling it.",
        "4. Generate a CORRECTED plan — do NOT repeat the approach that failed",
        "5. When ready, call `finish_planning`. The system routes to safety check before execution.",
    ])

    # Inject rejected params prohibition
    rejected = replan_context.get("rejected_params", [])
    if rejected:
        parts.append("\n### REJECTED PARAMETERS — DO NOT USE")
        parts.append(f"The tool rejected: {', '.join(f'`{p}`' for p in rejected)}")
        parts.append("These do NOT exist in the current tool version.")
        parts.append("Your corrected plan MUST NOT include any of them.")

    # Inject tool-specific verification suggestions
    ctx_failed_tools = replan_context.get("failed_tool_names", [])
    if ctx_failed_tools:
        from chaos_agent.agent.nodes.react_helpers import suggest_verify_command
        parts.append("\n### VERIFY BEFORE RETRY")
        for t in ctx_failed_tools:
            parts.append(f"- `{t}`: {suggest_verify_command(t)}")

    # Error classification decision tree — tool-agnostic
    if ctx_failed_tools:
        parts.extend([
            "",
            "### Analyzing the Failure",
            "Before deciding your approach, analyze WHY the failure occurred:",
            "",
            "- **Parameter error** (target not found, identifier mismatch):",
            "  Fix the parameters and retry with the SAME fault type.",
            "  Do NOT use propose_plan_change.",
            "",
            "- **Environment error** (tool not available, dependency missing):",
            "  Consider alternative injection methods from the skill case.",
            "  You MAY use propose_plan_change if the fault type is",
            "  fundamentally not viable on this target.",
            "",
            "- **Execution error** (tool ran but injection failed):",
            "  Re-read the skill case, verify parameters, and retry.",
            "  Only escalate to plan change if repeated attempts fail.",
            "",
            "Check the failure chain above to determine which category applies.",
        ])

    parts.extend([
        "",
        "### Plan Change (fault type switch)",
        "If after analyzing the failure you determine the original fault type "
        "is fundamentally not viable on this target, you may propose an "
        "alternative fault type using `propose_plan_change`. The user will see "
        "a comparison card and approve or reject the change.",
        "",
        "Do NOT use propose_plan_change for parameter adjustments — those can "
        "be handled within the same fault type. Only use it when the fault TYPE "
        "(scope/target/action) must change.",
    ])

    return "\n".join(parts)


def get_replan_directive_for_execution() -> str:
    """Replan directive for Phase 2's prompt — tells LLM about [REPLAN] mechanism.

    This section is tool-agnostic — it describes the replan mechanism
    (output [REPLAN] marker) without listing specific tool names. The
    concrete Phase 2 tools are listed in the Tools section and execution
    directives, not here.
    """
    return f"""### Replan Mechanism
If you encounter an error that you CANNOT resolve with the available Phase 2 tools:
1. Output `{REPLAN_MARKER}` followed by a detailed description of the problem
2. Include what you tried, what failed, and what information or approach might help
3. The system will route back to Phase 1, which has richer read-only tools for investigation and re-planning
4. Only use {REPLAN_MARKER} when you have genuinely exhausted Phase 2 capabilities — do NOT use it for transient errors that can be retried"""
