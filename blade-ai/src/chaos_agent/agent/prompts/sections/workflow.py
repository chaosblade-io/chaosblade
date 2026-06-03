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
- **Ambiguous**: Cross-validate with different commands. "No signal" ≠ "no fault" until timing is accounted for."""


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


def get_workflow_section() -> str:
    """Workflow phases section — path-guided ("how to succeed") not禁令式.

    Rewrite goal (post task-ce9647931ce1 incident): the prior wording was a
    禁令 ("Do NOT inject faults in Phase 1, no `kubectl exec ... blade create`,
    ...") that the LLM ignored under execution pressure — it撞 the
    blade_create black-list, saw the error message list ``kubectl`` as an
    alternative, and ran ``kubectl exec ... blade create`` instead. The new
    text shifts emphasis to **the success path** (what to do, then stop),
    explicitly lists Phase 1's read-only tool surface, and explains that
    mutation tools will be bound automatically once the plan is approved.

    Keeps the Analyze / Activate / Verify verbs frozen by
    ``tests/test_agent/test_prompts.py``.
    """
    return """## Workflow
You operate in TWO phases — the system transitions automatically.

### Phase 1 (current): Planning — read-only by enforcement

Your output contract this turn:
1. **Analyze** the FAULT INTENT in your first human message → fault type,
   target (namespace / resource / names), parameters.
2. **Activate** the matching skill ONCE via `activate_skill` (do NOT re-activate).
3. **Verify** the target exists with read-only kubectl (`get` / `describe`)
   before any plan decision.
   - If `-l <label>` returns empty, drop the label, list by name, then inspect
     `.metadata.labels` to discover the real key (e.g. `app.kubernetes.io/name`).
4. **Read** skill resources / knowledge docs to determine the correct blade
   command and flags. Treat the templates inside as RECIPES for Phase 2 —
   do not execute them here.
   - Resource-mapping heuristics (e.g. `--path` → imagefs vs nodefs for
     node-disk fill) are guidance only — verify on the live node with
     read-only kubectl `top` / `describe` before concluding.
5. **Assess complexity** (optional save_fault_plan):
   - Simple (single target, single fault, trivial rollback): skip plan, go
     to step 6.
   - Complex (multi-target, multi-step, cascading, large blast radius,
     non-trivial rollback): call `save_fault_plan` with a markdown plan
     containing the standard sections (Task Summary / Execution Steps /
     Expected Impact / Rollback and Recovery / Verification Methods). Pass
     the `task_id` from the user's conversation.
5b. **Reject if warranted**: If the request violates safety red lines, has no
    matching use-case in the catalogue, or the target is infeasible, call
    `finish_planning(summary="<what you found>", rejected=True,
    rejection_reason="<specific reason>")`. The system will end the run
    cleanly without any cluster changes. Do NOT output a free-text refusal
    without calling `finish_planning`.
6. **End Phase 1** by calling `finish_planning`:
   - `finish_planning(summary="...")` → proceed to safety check and execution.
   - `finish_planning(summary="...", rejected=True, rejection_reason="...")` →
     reject the request (the system ends cleanly).
   When proceeding (not rejecting), you MUST include `blast_radius_scope` —
   the system uses it for safety assessment. Classification rules:
   - `"target-only"` — only the declared target is mutated (e.g. patch one
     deployment, blade_create on specific pods).
   - `"namespace-wide"` — other resources in the target namespace are affected
     (e.g. delete all pods in a namespace).
   - `"cluster-wide"` — resources outside the target namespace are mutated
     (e.g. taint/cordon nodes, modify cluster-level resources). This triggers
     elevated safety review.
   Also include `blast_radius_detail` describing the mutation scope (e.g.
   "Will taint 3 nodes where target pods run").
   If you read multiple skill case files during planning, you MUST include
   `skill_case_resource` with the resource_path of the chosen case (e.g.
   `skill_case_resource="references/catalogue/Pod_镜像拉取失败/Pod_镜像拉取失败_镜像不存在或标签错误.md"`).
   This tells the system which case to use for verification.
   This is your output contract. Do NOT end Phase 1 by emitting free text
   without calling `finish_planning`.

### Phase 1 tools (read-only — runtime enforced)

Available: `activate_skill`, `read_skill_resource`, `read_knowledge_resource`,
`read_file`, kubectl (`get`/`describe`/`top`/`logs`), `blade_status`,
`save_fault_plan`, `finish_planning`.

NOT available: `blade_create`, `blade_destroy`, full kubectl (`exec`/`delete`/
`patch`/`scale`/...), `execute_skill_script`. Calling them returns
`Error: phase1_readonly_violation`. Mutation tools are bound in Phase 2
automatically after `finish_planning` → safety_check → user approval."""


def get_nl_mode_section() -> str:
    """Natural language mode section."""
    return """## Natural Language Mode
When the user provides a natural language description instead of structured parameters:
1. **Extract** fault_type, target (namespace, resource_type, names), and params from the description
2. **Activate** the matching skill based on the extracted fault_type
3. **Verify** the target exists using kubectl tools before injection
4. **Execute** following the skill instructions

If the description is ambiguous, ask for clarification rather than guessing."""


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
        parts.append(f"**Existing blade experiments (partial success)**: {', '.join(existing_uids)}")
        parts.append("Decide whether to destroy existing experiments or build on top of them.")
    else:
        parts.append("No blade experiments were successfully created.")

    failed_calls = replan_context.get("failed_tool_calls", [])
    if failed_calls:
        parts.append("\n### Failed Tool Calls")
        for fc in failed_calls:
            parts.append(f"- `{fc.get('name', '?')}` args={fc.get('args', {})} → {fc.get('error', '?')}")

    if replan_history:
        parts.append("\n### Previous Replan Attempts (DO NOT repeat these approaches)")
        for entry in replan_history:
            parts.append(f"- Attempt {entry.get('attempt', '?')}: {entry.get('action_taken', '?')} — {entry.get('original_error', '?')}")

    parts.extend([
        "\n### Replan Instructions",
        "1. Re-verify the target using available tools (kubectl, blade_status, etc.)",
        "2. Use read_skill_resource to re-read the skill for correct parameters",
        "3. **Runtime overrides documentation**: If the error indicates a rejected "
        "parameter/command/syntax, do NOT include it in your corrected plan. "
        "Plan a verification step first: run the tool's help/usage command "
        "to discover its actual interface before calling it.",
        "4. Generate a CORRECTED plan — do NOT repeat the same approach that failed",
        "5. When ready, output a summary. The system will route to safety check before execution.",
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

    # Add alternative injection approaches when blade_create failed
    blade_create_failed = "blade_create" in (ctx_failed_tools or [])
    if blade_create_failed:
        parts.extend([
            "",
            "### Alternative Injection Approaches (PLANNING ONLY — do NOT execute in Phase 1)",
            "blade_create failed on the host (likely due to an incompatible blade version or missing CLI). "
            "Consider these alternatives when generating your corrected plan:",
            "",
            "1. **kubectl exec into tool pod** — preserves blade_uid for automatic recovery via blade_destroy",
            "2. **kubectl-native injection** (scale/cordon/patch/taint) — no blade_uid; manual rollback required; Layer 2 will verify fault effect",
            "3. **Adjust blade parameters** — check `blade create k8s <scenario> -h` for supported flags in Phase 2",
            "",
            "See Important Guidelines → Injection Method Switching for detailed constraints on method selection.",
        ])

    parts.extend([
        "",
        "### Plan Change (fault type switch)",
        "If after analyzing the Phase 2 failure you determine the original fault type "
        "is fundamentally not viable on this target (e.g., iptables not installed for "
        "network-drop, insufficient disk space for disk-fill), you may propose an "
        "alternative fault type using `propose_plan_change`. The user will see a "
        "comparison card and approve or reject the change.",
        "",
        "Do NOT use propose_plan_change for parameter adjustments — those can be "
        "handled within the same fault type. Only use it when the fault TYPE "
        "(scope/target/action) must change.",
    ])

    return "\n".join(parts)


def get_replan_directive_for_execution() -> str:
    """Replan directive for Phase 2's prompt — tells LLM about [REPLAN] mechanism."""
    return f"""### Replan Mechanism
If you encounter an error that you CANNOT resolve with the available Phase 2 tools (blade_create, blade_status, kubectl, execute_skill_script):
1. Output `{REPLAN_MARKER}` followed by a detailed description of the problem
2. Include what you tried, what failed, and what information or approach might help
3. The system will route back to Phase 1, which has richer tools (read_skill_resource, blade_destroy, save_fault_plan) for investigation and re-planning
4. Only use {REPLAN_MARKER} when you have genuinely exhausted Phase 2 capabilities — do NOT use it for transient errors that can be retried"""
