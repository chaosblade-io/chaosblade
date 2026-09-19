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
from chaos_agent.agent.prompts.sections import (
    get_executor_remember_section,
    get_intent_reminder_section,
    get_remember_section,
    get_verifier_remember_section,
)
from chaos_agent.agent.prompts.sections.plan_builder import (
    get_plan_builder_critical_rules_reminder_section,
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
    """U-shaped attention: REMEMBER must occupy the true end of the Phase 1
    prompt — AFTER every dynamic section, not just above the cache boundary.

    Regression: remember used to sit in the stable section list, so the
    ever-present fault contract (plus replan context / runtime env / ledger)
    pushed it out of the recency zone on the most common Phase 1 paths,
    while the sibling builders' docstrings claimed "REMEMBER at END" for it.
    """

    def test_remember_trails_all_dynamic_sections(self):
        prompt = build_inject_system_prompt(
            skill_catalog="x",
            env_info={"blade_version": "1.7.0"},
            fault_spec={
                "scope": "pod", "fault_target": "cpu", "fault_action": "fullload",
                "namespace": "demo", "names": ["p0"], "params": {},
            },
            replan_context={"error": "boom", "failed_node": "execute_loop"},
        )
        remember_idx = prompt.index("# REMEMBER")
        for dynamic_marker in (
            CACHE_BOUNDARY.strip(),
            "## Environment",
            "Reviewed FaultSpec",
            "Planning Contract Declaration",
        ):
            assert remember_idx > prompt.index(dynamic_marker), (
                f"# REMEMBER must come after {dynamic_marker!r} so the "
                "recency anchor is never displaced by dynamic content"
            )
        # And nothing at all may follow it.
        assert remember_idx == prompt.rindex("# REMEMBER")
        assert "# REMEMBER" in prompt[-4000:]

    def test_remember_still_last_without_dynamic_sections(self):
        prompt = build_inject_system_prompt(skill_catalog="x")
        assert prompt.rstrip().endswith(
            get_remember_section().strip()
        ) or "# REMEMBER" in prompt[-3000:]


class TestSiblingBuildersRememberRecency:
    """Machine-enforce the "REMEMBER at END" docstring contract for the
    sibling builders.

    The inject-builder regression (remember pushed out of the recency zone
    by dynamic sections appended after it) survived because this contract
    was only a docstring claim. These guards pin it down for the builders
    that currently hold it — execute, verifier, intent, plan_builder — on
    their MAXIMAL dynamic paths, which are the ones where a future
    late-appended section would silently displace the anchor.
    """

    LEDGER = "## Progress Ledger\n- b-anchor: plan X\n- log: step1 done"

    def test_execute_remember_trails_all_dynamic_sections(self):
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
        remember_idx = prompt.index("# REMEMBER")
        for dynamic_marker in (
            "## Environment",
            "the approved plan body",
            "## EXECUTION PHASE DIRECTIVES",
            # NOTE: "## Progress Ledger" is intentionally absent here — Unit A
            # (context-cache-prefix-stability task 2.1) moved the execute ledger
            # OUT of the system prompt head onto the message tail, so it is no
            # longer a dynamic head section that could displace REMEMBER.
        ):
            assert remember_idx > prompt.index(dynamic_marker), (
                f"# REMEMBER must come after {dynamic_marker!r}"
            )
        assert remember_idx == prompt.rindex("# REMEMBER")
        assert prompt.rstrip().endswith(get_executor_remember_section().strip())
        # The ledger must NOT ride the execute head anymore (it rides the tail).
        assert "## Progress Ledger" not in prompt

    def test_verifier_head_omits_ledger_and_remember_trails(self):
        # Unit A (context-cache-prefix-stability task 2.4): the verify ledger
        # moved OUT of build_verifier_prompt's head onto the message tail, so the
        # head no longer carries it (passing the kwarg is now a no-op). REMEMBER
        # still trails every dynamic head section.
        prompt = build_verifier_prompt(progress_ledger_section=self.LEDGER)
        assert "## Progress Ledger" not in prompt
        remember_idx = prompt.index("# REMEMBER")
        assert remember_idx == prompt.rindex("# REMEMBER")
        assert prompt.rstrip().endswith(get_verifier_remember_section().strip())

    def test_intent_remember_trails_dynamic_completeness(self):
        prompt = build_intent_clarification_prompt(
            fault_spec={"scope": "pod", "fault_target": "cpu"},
            skill_catalog="x",
        )
        remember_idx = prompt.index("# REMEMBER")
        # The reviewed-contract snapshot is the dynamic section appended
        # after the cache boundary — the anchor must still trail it.
        assert remember_idx > prompt.index('"scope": "pod"')
        assert remember_idx > prompt.index(CACHE_BOUNDARY.strip())
        assert remember_idx == prompt.rindex("# REMEMBER")
        assert prompt.rstrip().endswith(get_intent_reminder_section().strip())

    def test_plan_builder_reminder_trails_progress(self):
        prompt = build_plan_builder_prompt(
            collected_faults=[
                {"scope": "pod", "target": "cpu", "action": "fullload"},
            ],
            skill_catalog="x",
        )
        reminder_idx = prompt.index("## Reminder")
        assert reminder_idx > prompt.index(
            "## Collected Parameters (confirmed by user)"
        )
        assert reminder_idx > prompt.index(CACHE_BOUNDARY.strip())
        assert reminder_idx == prompt.rindex("## Reminder")
        assert prompt.rstrip().endswith(
            get_plan_builder_critical_rules_reminder_section().strip()
        )


class TestInjectSlimmedSections:
    def test_role_section_content(self):
        prompt = build_inject_system_prompt(skill_catalog="x")
        # Role section preserves "Chaos Engineering Agent" and Safety Rules.
        assert "What You Can Do" not in prompt
        assert "Chaos Engineering Agent" in prompt
        assert "Safety Rules" in prompt

    def test_uses_hard_only_safety(self):
        prompt = build_inject_system_prompt(skill_catalog="x")
        # Hard Rules + Caution Compliance kept; long-tail Advisory / Decision dropped.
        assert "### Hard Rules" in prompt
        assert "Caution Rule Compliance" in prompt
        assert "### Decision Framework" not in prompt
        assert "### Advisory Rules" not in prompt

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

    def test_keeps_hard_safety_rules(self):
        prompt = build_execute_system_prompt(skill_catalog="x")
        # Executor still bound by Hard Rules + Caution Compliance.
        assert "### Hard Rules" in prompt
        assert "target blacklist" in prompt

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
