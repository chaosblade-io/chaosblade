"""Tests for the Phase 4 builder slimming behavior.

Covers:
* ``input_is_nl`` kwarg is accepted and does not break assembly,
* execute builder no longer pulls in the full verification-strategy or
  failure-modes catalogues (those moved to knowledge docs).
"""

from chaos_agent.agent.prompts.builders import (
    build_execute_system_prompt,
    build_inject_system_prompt,
    build_intent_clarification_prompt,
    build_plan_builder_prompt,
    build_verifier_prompt,
)
from chaos_agent.agent.prompts.constants import CACHE_BOUNDARY
from chaos_agent.agent.prompts.reminder import PARALLELIZE_PRINCIPLE
from chaos_agent.agent.prompts.sections import (
    get_replan_directive_for_execution,
    get_verifier_output_format_section,
)


class TestInjectInputIsNlKwarg:
    def test_accepts_input_is_nl_true(self):
        # Should not raise; NL mode steps are covered by Workflow section.
        prompt = build_inject_system_prompt(skill_catalog="x", input_is_nl=True)
        assert "Workflow" in prompt

    def test_accepts_input_is_nl_false(self):
        prompt = build_inject_system_prompt(skill_catalog="x", input_is_nl=False)
        assert "Workflow" in prompt


class TestInjectCacheBoundary:
    def test_skill_catalog_above_cache_boundary(self):
        """PATD: skill index is now in the stable section (above boundary).

        Previously it was in the dynamic section (below boundary) as part of
        the P2 tool_result injection pattern. PATD eliminates P2 injection
        and moves skill index to stable section for cache efficiency and
        guaranteed visibility across iterations.
        """
        # Use valid catalog format (matching build_catalog_prompt output)
        prompt = build_inject_system_prompt(
            skill_catalog="- my-test-skill: Test skill for cache boundary"
        )
        boundary_idx = prompt.index(CACHE_BOUNDARY.strip())
        skill_idx = prompt.index("my-test-skill")
        assert skill_idx < boundary_idx, (
            "PATD: skill index must sit above cache boundary (stable section) "
            "so it persists across iterations and is never scrolled out of context"
        )

    def test_env_section_below_cache_boundary(self):
        prompt = build_inject_system_prompt(
            skill_catalog="x",
            env_info={"blade_version": "1.7.0"},
        )
        boundary_idx = prompt.index(CACHE_BOUNDARY.strip())
        env_idx = prompt.index("## Environment")
        assert env_idx > boundary_idx


class TestInjectRememberRecency:
    """2026-09-20 skeleton/weight cleanup: the planner prompt no longer
    carries a REMEMBER mirror. Rationale (universal-cognitive-
    architecture OQ3): in the ReAct loop the model's last read is the
    message tail (tool results + progress ledger + corrective hints), so
    a prompt-end restatement never actually held the recency position —
    it duplicated primacy content at 2030 chars / 10.4% of the prompt.

    These guards pin the removal's invariants on the MAXIMAL dynamic
    path (fault contract + replan context + runtime env): no mirror
    anywhere, the mirror's unique rule present in Core Principles, and
    the shared principle single-sided (one occurrence, at primacy).
    """

    def test_planner_prompt_omits_remember_mirror(self):
        prompt = build_inject_system_prompt(
            skill_catalog="x",
            env_info={"blade_version": "1.7.0"},
            fault_spec={
                "scope": "pod", "fault_target": "cpu", "fault_action": "fullload",
                "namespace": "demo", "names": ["p0"], "params": {},
            },
            replan_context={"error": "boom", "failed_node": "execute_loop"},
        )
        assert "# REMEMBER" not in prompt

    def test_mirror_unique_rule_folded_into_core_principles(self):
        # The mirror's one non-duplicated line must survive the removal —
        # Core Principles now carries it (planning's contract-keeper rule).
        prompt = build_inject_system_prompt(skill_catalog="x")
        assert "Preserve the reviewed FaultSpec" in prompt
        assert "propose_plan_change" in prompt

    def test_parallelize_principle_single_sided_in_planner(self):
        # Primacy carries the principle; with the mirror gone the planner
        # prompt must contain it EXACTLY once (a second copy creeping back
        # in is exactly the weight the cleanup removed).
        prompt = build_inject_system_prompt(
            skill_catalog="x", profile="k8s",
        )
        assert prompt.count(PARALLELIZE_PRINCIPLE) == 1

    def test_dynamic_sections_close_the_prompt(self):
        # Without the mirror, the dynamic tail (fault contract / replan)
        # legitimately closes the prompt; the fault contract ends with the
        # propose_plan_change declaration that restates the folded rule
        # exactly where it binds.
        prompt = build_inject_system_prompt(
            skill_catalog="x",
            fault_spec={
                "scope": "pod", "fault_target": "cpu", "fault_action": "fullload",
                "namespace": "demo", "names": ["p0"], "params": {},
            },
        )
        assert "Planning Contract Declaration" in prompt
        assert prompt.rstrip().endswith("full `proposed_fault`.")


class TestSiblingBuildersRememberRecency:
    """Machine-enforce the "REMEMBER at END" docstring contract for the
    sibling builders.

    The inject-builder regression (remember pushed out of the recency zone
    by dynamic sections appended after it) survived because this contract
    was only a docstring claim. These guards pin it down for the builders
    that currently hold it — plan_builder — on their MAXIMAL dynamic
    paths, which are the ones where a future late-appended
    section would silently displace the anchor.
    (intent dropped its REMEMBER mirror in the 2026-09-20 skeleton/weight
    cleanup; its dynamic-path guard is
    ``test_intent_dynamic_completeness_trails_cache_boundary`` below. The
    executor dropped its own mirror in the same-date execute cleanup —
    OQ3 family: recency rides the message tail, so a prompt-end mirror
    never held the position; its close is now pinned on the replan
    contract by ``test_execute_close_is_replan_contract`` below. The
    verifier dropped its mirror in the same-date verifier cleanup
    (pass-4); its close is pinned on the output contract by
    ``test_verifier_close_is_output_contract`` below.)
    """

    LEDGER = "## Progress Ledger\n- b-anchor: plan X\n- log: step1 done"

    def test_execute_close_is_replan_contract(self):
        # 2026-09-20 execute cleanup: the executor's REMEMBER mirror is
        # gone (91% verbatim re-render of Core Principles; its unique
        # replan-escape rule folded into the _EXECUTOR_PRINCIPLES tuple).
        # On the MAXIMAL dynamic path this guard pins the new close: the
        # prompt ends on the replan contract, whose "an actual tool call,
        # never prose" wording re-teaches the folded escape rule exactly
        # where the replan decision is made, trailing every dynamic
        # section that could otherwise displace it.
        prompt = build_execute_system_prompt(
            skill_catalog="x",
            skill_name="k8s-fault",
            plan="the approved plan body",
            plan_path="/tmp/plan.json",
            structured_params_hint="scope=pod, target=cpu, action=fullload",
            user_params_hint='{"percent": 80}',
            env_info={"blade_version": "1.7.0"},
            progress_ledger_section=self.LEDGER,
        )
        assert "# REMEMBER" not in prompt
        directive = get_replan_directive_for_execution().strip()
        replan_idx = prompt.rstrip().rindex(directive)
        for dynamic_marker in (
            "## Environment",
            "the approved plan body",
            "## EXECUTION PHASE DIRECTIVES",
            # NOTE: "## Progress Ledger" is intentionally absent here — Unit A
            # (context-cache-prefix-stability task 2.1) moved the execute ledger
            # OUT of the system prompt head onto the message tail, so it is no
            # longer a dynamic head section that could displace the close.
        ):
            assert replan_idx > prompt.index(dynamic_marker), (
                f"the replan contract must come after {dynamic_marker!r}"
            )
        assert prompt.rstrip().endswith(directive)
        # The ledger must NOT ride the execute head anymore (it rides the tail).
        assert "## Progress Ledger" not in prompt

    def test_verifier_close_is_output_contract(self):
        # 2026-09-20 verifier cleanup (pass-4): the verifier's REMEMBER
        # mirror is gone (OQ3 family — recency rides the message tail;
        # every bullet had a stronger carrier: CP #1-#4 primacy copies,
        # the output contract's submit-only rule, the PARALLELIZE
        # constant in CP). On the MAXIMAL dynamic path this guard pins
        # the new close: the prompt ends on the output contract, trailing
        # every dynamic head section that could otherwise displace it
        # (same shape as the executor's replan-contract close above).
        prompt = build_verifier_prompt(progress_ledger_section=self.LEDGER)
        assert "## Progress Ledger" not in prompt
        assert "# REMEMBER" not in prompt
        contract = get_verifier_output_format_section().strip()
        contract_idx = prompt.rstrip().rindex(contract)
        for dynamic_marker in (
            "## Domain Knowledge",
            "## Fault-Specific Verification",
            "## Capability Profile",
            "## Verification Heuristics",
            # NOTE: "## Progress Ledger" is intentionally absent — Unit A
            # (context-cache-prefix-stability task 2.4) moved the verify
            # ledger onto the message tail, so it is not a dynamic head
            # section that could displace the close.
        ):
            assert contract_idx > prompt.index(dynamic_marker), (
                f"the output contract must come after {dynamic_marker!r}"
            )
        assert prompt.rstrip().endswith(contract)

    def test_intent_dynamic_completeness_trails_cache_boundary(self):
        # The intent REMEMBER mirror was removed (2026-09-20 skeleton/weight
        # cleanup, universal-cognitive-architecture OQ3 — recency rides the
        # transition tail messages). What must survive on the dynamic path:
        # the reviewed-contract snapshot is appended AFTER the cache
        # boundary, so the stable head stays byte-identical across turns.
        prompt = build_intent_clarification_prompt(
            fault_spec={"scope": "pod", "fault_target": "cpu"},
            skill_catalog="x",
        )
        assert "# REMEMBER" not in prompt
        assert prompt.index('"scope": "pod"') > prompt.index(CACHE_BOUNDARY.strip())

    def test_plan_builder_dynamic_tail_trails_cache_boundary(self):
        # The end-of-prompt checklist mirror was removed (2026-09-20 pass-6,
        # OQ3 — the ReAct message tail owns recency, and every checklist
        # bullet restated a rule carried by the bound tool schemas or the
        # critical_rules head). What must survive on the dynamic path: the
        # progress snapshot is appended AFTER the cache boundary, so the
        # stable head stays byte-identical across turns — and with the
        # mirror gone the prompt CLOSES on that dynamic tail.
        prompt = build_plan_builder_prompt(
            collected_faults=[
                {"scope": "pod", "target": "cpu", "action": "fullload"},
            ],
            skill_catalog="x",
        )
        assert "## Reminder" not in prompt
        assert prompt.rstrip().endswith(
            "Do NOT re-ask for parameters already collected above."
        )
        assert prompt.index("Do NOT re-ask for parameters") > prompt.index(
            CACHE_BOUNDARY.strip()
        )

    def test_plan_builder_discover_before_ask_is_single_sourced(self):
        # P3 follow-up (2026-09-20 pass-6): the discover-before-ask teaching
        # used to be restated four times (role Core Principle / critical
        # rules #2 / workflow Stage 1 / output_format). It is now pinned to
        # STRUCTURE, not wording: critical_rules #2 is the single mechanism
        # source; role keeps only the one-line philosophy; Stage 1 keeps only
        # its stage gate (skip discovery when the user already named a
        # target); output_format teaches only the presentation contract. If
        # the mechanism wording reappears anywhere else, a section has
        # regressed to restating critical_rules.
        prompt = build_plan_builder_prompt(planning_mode="guided", skill_catalog="x")
        # Single mechanism carrier (critical_rules #2): the enumeration
        # HOW-TO (filter/group/present) exists exactly once in the prompt.
        assert prompt.count("common prefix") == 1
        assert prompt.count("build options FROM the results") == 1
        # Role is philosophy-only: no option mechanics, no single-click
        # promise (that promise rode the deleted restatement).
        role = prompt[: prompt.index("### Critical Rules")]
        assert "Research before you ask" in role
        assert "present them as concrete options" not in role
        assert "single click" not in prompt
        # Stage 1 is a stage gate, not a how-to.
        stage1 = prompt[prompt.index("Stage 1: TARGET DISCOVERY") : prompt.index("Stage 2:")]
        assert "skip discovery for that field" in stage1
        assert "enumerate candidates" not in stage1


class TestInjectSlimmedSections:
    def test_role_section_content(self):
        prompt = build_inject_system_prompt(skill_catalog="x")
        # Role section preserves "Chaos Engineering Agent"; the Safety
        # Rules section itself is gone from the planner prompt (2026-09-20
        # skeleton/weight cleanup — program guards enforce it), and the
        # role's closing sentence now references the guards generically
        # instead of a section that is no longer rendered.
        assert "What You Can Do" not in prompt
        assert "Chaos Engineering Agent" in prompt
        assert "Safety Rules" not in prompt
        assert "The system's guards reject out-of-bounds actions" in prompt

    def test_planner_prompt_omits_safety_section(self):
        prompt = build_inject_system_prompt(skill_catalog="x")
        # 2026-09-20 skeleton/weight cleanup (user ruling): five of six Hard
        # rules are enforced by code (safety_check node, phase-1 tool
        # binding + screener, target freeze, automatic timeout, automatic
        # conflict detection) and the sixth duplicates Core Principles; the
        # Caution rules ride confirmation_gate + finish_planning required
        # fields. The shared get_safety_section function is untouched —
        # the same-date execute cleanup later dropped the executor's row
        # too (all six rules' executors are code on the execute path), so
        # the function is now builder-consumerless, pinned only by its own
        # direct section tests: test_section_params.py's level-parameterized
        # class and test_prompts.py's content pin (dead-but-exported).
        assert "### Hard Rules" not in prompt
        assert "Caution Rule Compliance" not in prompt
        assert "### Decision Framework" not in prompt
        assert "### Advisory Rules" not in prompt
        assert "target blacklist" not in prompt

    def test_executor_prompt_omits_safety_section(self):
        # 2026-09-20 execute cleanup (compress-all ruling): every Hard
        # Rule's executor is code on the execute path — safety_check is an
        # upstream node the executor never sees, phase-2 tool binding, the
        # tool screener, provider default timeout, conflict resolution
        # before execution — and the Caution rules are confirmation_gate
        # artifacts already user-approved. The residual prompt-side
        # posture is the role's guard-adaptation line. The shared function
        # itself stays pinned by its direct section tests —
        # test_section_params.py and test_prompts.py (dead-but-exported).
        prompt = build_execute_system_prompt(skill_catalog="x")
        assert "### Hard Rules" not in prompt
        assert "target blacklist" not in prompt
        assert "Caution Rule Compliance" not in prompt
        assert "# REMEMBER" not in prompt
        assert "The system's guards reject out-of-bounds actions" in prompt

    def test_verification_merged_into_workflow(self):
        prompt = build_inject_system_prompt(skill_catalog="x")
        # Phase 5: verification strategy section removed from Phase 1 —
        # key principles (delay awareness, evidence sufficiency) merged
        # into Workflow Step 3. The standalone section header is gone.
        assert "Verification Strategy (Principles)" not in prompt
        assert "### Verification Method Selection Reasoning" not in prompt
        # But the delay awareness principle IS present (merged into Workflow)
        assert "NOT instantaneous" in prompt

    def test_drops_failure_modes_section(self):
        prompt = build_inject_system_prompt(skill_catalog="x")
        # The full Failure Modes block (with subsection headers + prose) is gone
        # from the system prompt — content lives in failure-modes.md and is
        # loaded on demand via read_knowledge_resource. The Knowledge Index
        # may still reference the doc title, which is intentional.
        assert "### Partial Injection Failure" not in prompt
        assert "### Cascading Impact" not in prompt
        # Specific in-prompt prose from the dropped block must be absent.
        assert "Do NOT retry failed targets automatically" not in prompt

    def test_drops_method_switching_block(self):
        prompt = build_inject_system_prompt(skill_catalog="x")
        # The actual subsection header should be gone from Phase 1.
        # The phrase may appear in the Domain Knowledge Index pointing to
        # chaosblade-cli.md — check for the header form.
        assert "### Injection Method Switching" not in prompt
        assert "METHOD CONSTRAINT" not in prompt


class TestExecuteSlimmedSections:
    def test_drops_verification_strategy(self):
        prompt = build_execute_system_prompt(skill_catalog="x")
        assert "## Verification Strategy" not in prompt
        assert "Verification Method Selection Reasoning" not in prompt

    def test_drops_failure_modes(self):
        prompt = build_execute_system_prompt(skill_catalog="x")
        # Subsection headers + in-prompt prose must be gone; knowledge
        # index pointer to failure-modes.md may remain.
        assert "### Partial Injection Failure" not in prompt
        assert "### Cascading Impact" not in prompt
        assert "Do NOT retry failed targets automatically" not in prompt

    def test_drops_hard_safety_rules(self):
        prompt = build_execute_system_prompt(skill_catalog="x")
        # 2026-09-20 execute cleanup: the Hard-Rules wall moved to code —
        # full rationale pinned next to the planner-side drop in
        # TestInjectSlimmedSections above; the executor's residual safety
        # posture is the role's guard-adaptation line.
        assert "### Hard Rules" not in prompt
        assert "target blacklist" not in prompt

    def test_keeps_failure_handling_block(self):
        prompt = build_execute_system_prompt(skill_catalog="x")
        # Executor needs general orchestration guidance, without a fixed
        # failure playbook that would pre-empt ReAct's own decisions.
        assert "Execution Orchestration" in prompt
        assert "Treat tool output as runtime evidence" in prompt
        assert "do not retry or re-plan" not in prompt
        assert "plan itself needs to be" in prompt
        assert "genuinely exhausted Phase 2 capabilities" not in prompt
        # Replan is presented purely as the request_replan tool — no printed
        # text marker (which induced verbalized tool-call output).
        assert "request_replan" in prompt
        assert "<replan_request>" not in prompt

    def test_keeps_execution_directives(self):
        prompt = build_execute_system_prompt(skill_catalog="x")
        assert "EXECUTION PHASE DIRECTIVES" in prompt


class TestCaseHandoffReferenceSemantics:
    """The intent dialogue's case pick reaches Phase 1 as a REFERENCE, not
    a directive (user design principle: card approval ≠ careful review, so
    the pick is usually the intent node's own — planning must keep full
    authority over the final case selection, exactly as it independently
    verifies the FaultSpec target).

    Freezes the two-sided fix:
    * the old binding wording ("read that case first and anchor planning",
      "instead of silently switching") is gone — it contradicted the
      reference semantics;
    * the case file path, carried first-hand on ``FaultSpec.
      case_resource_path`` (relative to the skill directory), is surfaced
      so planning can read the case directly instead of browsing the
      skill's resources to locate it.
    """

    _CASE_PATH = (
        "references/catalogue/Node_内存使用率过高/"
        "Node_内存使用率过高_异常进程占用.md"
    )

    _SPEC = {
        "scope": "node", "fault_target": "mem", "fault_action": "load",
        "case_resource_path": _CASE_PATH,
    }

    def test_case_pick_worded_as_reference(self):
        prompt = build_inject_system_prompt(
            skill_catalog="x", fault_spec=dict(self._SPEC),
        )
        assert "a reference, not a directive" in prompt
        assert "the final case selection is yours" in prompt
        # The fallback path is signposted: a case that turns out wrong must
        # send planning back to browsing the skill, not into a dead end.
        # Canonical wording comes from the single source (case_reference).
        assert "browse the active skill with `read_skill_resource`" in prompt
        assert "note the reason in your plan" in prompt
        # Binding-era wording must not survive anywhere in the prompt.
        assert "read that case first and anchor planning" not in prompt
        assert "silently switching to another case" not in prompt

    def test_case_path_surfaced_from_spec(self):
        prompt = build_inject_system_prompt(
            skill_catalog="x", fault_spec=dict(self._SPEC),
        )
        # The path rides on the spec dump and the canonical note fires.
        assert self._CASE_PATH in prompt
        assert "exactly what `read_skill_resource` consumes" in prompt

    def test_no_case_pick_note_without_case_path(self):
        # No case settled in the dialogue → the whole case-pick note is
        # absent; telling planning to "start from it" with no path at all
        # would be self-contradictory.
        spec = dict(self._SPEC)
        spec["case_resource_path"] = ""
        prompt = build_inject_system_prompt(
            skill_catalog="x", fault_spec=spec,
        )
        assert "a reference, not a directive" not in prompt
        assert "Start from it" not in prompt
        assert "exactly what `read_skill_resource` consumes" not in prompt
        assert "browse the active skill" not in prompt


class TestIntentKnowledgeIndexInjection:
    """intent-outcome-to-means: the intent prompt must carry the plan-phase
    knowledge index, or the outcome→means methodology is invisible.

    Trace sess_4b696f566f23: without the index the intent node saw ONLY the
    means-named skill-package index, so an outcome-stated request collapsed
    lexically onto the catalogue entries whose names matched the outcome's
    wording. These tests pin the full chain the fix depends on: the doc
    exists in the registry (auto-discovery), the index row reaches the
    prompt ahead of the Skill Index, and the host exemption holds (host
    intent prompts are contractually free of cluster vocabulary).
    """

    def test_k8s_prompt_carries_plan_knowledge_index_before_skill_index(self):
        prompt = build_intent_clarification_prompt(
            semantic_only=True, profile="k8s"
        )
        ki = prompt.find("Domain Knowledge (on-demand)")
        si = prompt.find("## Skill Index")
        assert ki != -1, (
            "intent prompt lost the knowledge index — outcome-stated "
            "requests fall back to lexical catalogue matching"
        )
        assert si != -1
        assert ki < si, (
            "knowledge index must precede the Skill Index: methodology "
            "sight before means selection"
        )

    def test_index_lists_outcome_to_means_doc(self):
        from chaos_agent.agent.prompts.sections.knowledge_sections import (
            get_knowledge_summary_section,
        )

        plan_index = get_knowledge_summary_section("plan")
        assert "`outcome-to-means.md`" in plan_index, (
            "outcome-to-means.md missing from the plan-phase index — "
            "frontmatter (phases: [plan]) or registry auto-discovery broke"
        )
        # Phase discipline: the doc declares plan only, so the verifier
        # index must not pay its row.
        verify_index = get_knowledge_summary_section("verify")
        assert "`outcome-to-means.md`" not in verify_index

    def test_host_prompt_omits_knowledge_index(self):
        from chaos_agent.transports import PROFILE_HOST

        prompt = build_intent_clarification_prompt(
            semantic_only=True, profile=PROFILE_HOST
        )
        assert "Domain Knowledge (on-demand)" not in prompt, (
            "host intent prompt must stay free of cluster vocabulary — "
            "the index carries k8s-knowledge/kubectl-guide rows"
        )
