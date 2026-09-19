"""Tests for system prompt templates."""

from chaos_agent.agent.prompts import (
    build_inject_system_prompt,
    build_intent_clarification_prompt,
    get_role_section,
    get_core_principles_section,
    get_remember_section,
    get_executor_core_principles_section,
    get_executor_remember_section,
    get_workflow_section,
    get_safety_section,
    get_tools_section,
    get_guidelines_section,
    get_env_section,
)
from chaos_agent.agent.prompts.sections.workflow import (
    get_verification_heuristics_compact_section,
)
from chaos_agent.agent.prompts.sections.verification import (
    get_verifier_core_principles_section,
    get_verifier_remember_section,
)
from chaos_agent.agent.prompts.sections.recovery import (
    get_recover_core_principles_section,
    get_recover_remember_section,
)
from chaos_agent.agent.prompts.sections.plan_builder import (
    get_plan_builder_critical_rules_section,
    get_plan_builder_critical_rules_reminder_section,
)
from chaos_agent.agent.prompts.reminder import PARALLELIZE_PRINCIPLE
from chaos_agent.agent.prompts.sections.execution import (
    _execution_steps_only,
    get_execution_directives_section,
)
from chaos_agent.agent.prompts.modes import PromptMode
from chaos_agent.agent.prompts.sections.intent import (
    get_intent_role_section,
    get_intent_priorities_section,
    get_intent_dialogue_routing_section,
    get_intent_parameter_model_section,
    get_intent_inject_flow_section,
    get_intent_recover_flow_section,
    get_intent_batch_flow_section,
    get_intent_operation_freshness_section,
    get_intent_tools_section,
    get_intent_output_section,
    get_intent_completeness_section,
    get_intent_reminder_section,
)


class TestSectionFunctions:
    """Test individual section functions (迁移点 1/2/3/4)."""

    def test_role_section_not_empty(self):
        assert len(get_role_section()) > 0
        assert "Chaos Engineering Agent" in get_role_section()

    def test_workflow_section_contains_phases(self):
        section = get_workflow_section()
        assert "Phase 1" in section
        assert "Phase 2" in section
        assert "activate_skill" in section

    def test_workflow_section_grounds_target_before_planning(self):
        # Single profile-agnostic text: target grounding is enforced for every
        # environment via bound read-only tools, not a per-profile branch.
        section = get_workflow_section()
        assert "target authority" in section
        assert "read-only tools" in section
        assert "finish_planning" in section

    def test_workflow_plan_contract_keeps_execution_steps_mutation_only(self):
        # Postmortem (inject-3a745506): the planner copied the case's
        # observation step into "Execution Steps" and Phase 2 executed it
        # literally — a 90s+ wait consumed the fault window; "2+ checks"
        # generalized into "≥2 次" and forced the verifier into an empty
        # second window. The contract keeps observation in Verification
        # Methods and licenses repeated sampling only for ruling an effect OUT.
        section = get_workflow_section()
        assert "MUTATION steps only" in section
        assert "don't invent observation rounds" in section
        assert "rule an effect OUT" in section

    def test_workflow_plan_contract_bounds_verification_time_annotations(self):
        # Postmortem (inject-6001154d): the planner parameterized the case's
        # "confirm within the window" into "mid-window ~3-5 minutes" and the
        # verifier burned the window waiting for a status label after the
        # physical quantity was already proven; a recovery-period wait also
        # leaked into Verification Methods as a 60s pre-recovery hold.
        # Time annotations are upper bounds; recovery waits belong to
        # Rollback and Recovery.
        section = get_workflow_section()
        assert "upper bounds, not quotas" in section
        assert "satisfied at any point inside its window" in section
        assert '"Rollback and Recovery"' in section
        assert "the verifier does not wait for recovery" in section

    def test_workflow_duration_teaching_claims_no_floor_raise(self):
        # inject-aac02265 (#35 round audit): the duration teaching claimed
        # "values below the 600s safety floor are raised to the floor" —
        # doubly false. ensure_min_duration honours an explicitly declared
        # duration verbatim (floor 300s applies only to the UNSPECIFIED
        # default path), and the floor was never 600s post-adjustment.
        # The false claim cost a full 121.7s planning turn: the LLM
        # believed its honest ~120s declaration would be inflated, so it
        # second-guessed itself across three reasoning passes and finally
        # declared 600 "to match what will actually be applied". The
        # purge directive: no surface may teach raise-the-floor semantics.
        section = get_workflow_section()
        assert "raised to the floor" not in section
        assert "safety floor" not in section
        assert "600s" not in section
        # The surviving teaching: declare the window the plan actually uses.
        assert "Declare the window the plan actually" in section

    def test_workflow_finish_planning_identity_declaration_teaching(self):
        # B83/B84 (#49 post-mortem): step 6 MUST list the fault-identity
        # triple (absence reads as "optional" and burns a nudge round on
        # every CLI NL run) — but the CONTRACT lives in the tool schema
        # alone (same pointer shape as blast_radius_scope). The prompt
        # keeps one generalized principle only: identity comes from what
        # the plan attacks, never from the case menu.
        section = get_workflow_section()
        assert "fault-identity" in section
        assert "never from the skill-case document" in section
        assert "`finish_planning` tool schema" in section

    def test_execution_directives_skip_planned_observation_steps(self):
        # Legacy or malformed plans may still carry observation steps; the
        # executor must treat them as verification work and skip them rather
        # than executing them (inject-3a745506).
        section = get_execution_directives_section()
        assert "verification work" in section
        assert "skip it" in section

    def test_verification_heuristics_encodes_timeout_discipline(self):
        # Postmortem lesson (task-e951696f) generalized: a timeout/failed
        # observation is never proof of success, and partial coverage must not
        # be generalized to the whole. Lives as a GENERAL heuristic (not a
        # blade_scope branch), so it applies to every fault type.
        section = get_verification_heuristics_compact_section()
        assert "Timeout ≠ signal" in section
        assert "Count & coverage" in section
        assert "never tally timeouts" in section
        assert "partial N/total" in section

    def test_verification_heuristics_encodes_method_not_target(self):
        # Aligns the verifier with intent's `# Reflection`: after repeated
        # identical failures, suspect your own method (not the target) and
        # broaden-then-narrow rather than re-running the same command. Lives as
        # a GENERAL heuristic, not a per-fault branch.
        section = get_verification_heuristics_compact_section()
        assert "Method, not target" in section
        assert "broaden" in section

    def test_recover_core_principles_encodes_method_switch(self):
        # Recovery verifier previously lacked the "switch method, don't retry
        # the same command" discipline that the inject verifier already has.
        # This closes that gap using the same wording family.
        from chaos_agent.agent.prompts.sections.recovery import (
            build_recover_verifier_system_prompt,
        )

        prompt = build_recover_verifier_system_prompt()
        assert "suspect your METHOD" in prompt
        assert "switch to structured status" in prompt
        assert "never silently omit" in prompt

    def test_safety_section_contains_rules(self):
        section = get_safety_section()
        assert "target blacklist" in section
        assert "NEVER" in section
        assert "ALWAYS" in section

    def test_tools_section_contains_priority(self):
        """迁移点 3: 工具使用规范"""
        section = get_tools_section(phase=1)
        assert "Tool Selection Priority" in section
        assert "Parallel Calls" in section
        assert "Avoid Redundancy" in section
        assert "read_skill_resource" in section
        # Timeout Protection removed: default timeout is a program guarantee
        # visible in the injection tool's own schema/docstring.
        assert "Timeout Protection" not in section

    def test_phase1_parallel_authorization_covers_all_independent_calls(self):
        # inject-aac02265 (#35): planning spent 262s on three SERIAL
        # bookkeeping turns (ledger → plan file → exit signal) because the
        # Phase 1 parallel authorization was scoped to "read-only query
        # calls" only — every non-query call was implicitly serialized.
        # The dependency definition line already constrains what counts as
        # independent; the read-only qualifier only excluded calls that
        # were safe to batch.
        #
        # 2026-09-11 slimming: the SAME-turn directive itself moved out of
        # the middle-zone Parallel Calls block — the canonical
        # PARALLELIZE_PRINCIPLE (Core Principles + REMEMBER, pinned by
        # test_parallelize_principle_single_sourced_in_every_node) is the
        # only directive copy; the block keeps only what the principle does
        # NOT say: the dependency criteria (and, in Phase 2, the mutation
        # receipt sequencing). This test pins the slimmed shape: no
        # read-only re-scoping of Phase 1, and Phase 2 mutation receipts
        # stay sequenced.
        phase1 = get_tools_section(phase=1)
        assert "read-only query calls" not in phase1
        assert '"Dependent" means one call\'s arguments require another call\'s result' in phase1
        phase2 = get_tools_section(phase=2)
        assert "Mutation calls stay sequenced by their receipts" in phase2

    def test_parallelize_principle_single_sourced_in_every_node(self):
        # B53 follow-up (inject-0cac8c21 #35 / inject-172a2765 #26): the
        # MAY-form authorization above was consumed probabilistically at
        # best — the record-keeping turns stayed serial (262s/309s) in both
        # regressions while #36 batched 2/3 on the same prompt. The fix is
        # a single canonical principle (reminder.PARALLELIZE_PRINCIPLE:
        # parallelize when you can; serialize on dependency, safety
        # ordering, or doubt) rendered verbatim in every LLM node's primacy AND
        # recency zones — a principle travels across faces; a scoped
        # permission does not. This test pins the single-sourcing: every
        # zone must carry the constant verbatim, so the wording can never
        # drift per node.
        zones = {
            "planner Core Principles": get_core_principles_section(),
            "planner REMEMBER": get_remember_section(),
            "executor Core Principles": get_executor_core_principles_section(),
            "executor REMEMBER": get_executor_remember_section(),
            "verifier Core Principles": get_verifier_core_principles_section(),
            "verifier REMEMBER": get_verifier_remember_section(),
            "recover Core Principles": get_recover_core_principles_section(),
            "recover REMEMBER": get_recover_remember_section(),
            "intent REMEMBER": get_intent_reminder_section(),
            "plan-builder critical rules (guided)":
                get_plan_builder_critical_rules_section(),
            "plan-builder critical rules (expert)":
                get_plan_builder_critical_rules_section(mode="expert"),
            "plan-builder reminder (guided)":
                get_plan_builder_critical_rules_reminder_section(),
            "plan-builder reminder (expert)":
                get_plan_builder_critical_rules_reminder_section(mode="expert"),
        }
        for zone_name, zone in zones.items():
            assert PARALLELIZE_PRINCIPLE in zone, zone_name

    def test_guidelines_section_contains_follow_instructions(self):
        section = get_guidelines_section(phase=2)
        assert "Skill-case methods come first" in section
        # Runtime Feedback Priority removed from guidelines: executor Core
        # Principles + REMEMBER carry the same principle in the
        # primacy/recency zones; the middle copy added nothing.
        assert "Runtime Feedback Priority" not in section

    def test_core_principles_section_content(self):
        section = get_core_principles_section()
        assert "# Core Principles" in section
        assert "FAULT INTENT parameters are UNVERIFIED" in section
        assert "TOOL is correct" in section
        assert "finish_planning" in section

    def test_remember_section_content(self):
        section = get_remember_section()
        assert "# REMEMBER" in section
        assert "FAULT INTENT parameters are UNVERIFIED" in section
        assert "TOOL is correct" in section
        assert "finish_planning" in section
        assert "propose_plan_change" in section

    def test_core_principles_and_remember_are_aligned(self):
        """REMEMBER must reinforce the same rules as Core Principles (U-shaped attention)."""
        core = get_core_principles_section()
        remember = get_remember_section()
        # Each Core Principles rule must appear verbatim in REMEMBER
        for line in core.splitlines():
            if line.startswith("- "):
                assert line in remember, (
                    f"Core Principles rule not found in REMEMBER: {line!r}"
                )

    def test_feasibility_probing_covers_host_level_dependencies(self):
        """Viability probing must obligate deriving the mechanism's full
        dependency set and probing it wherever it lives (container, host, or
        cluster components), anchored on the substrate principle. Regression
        for task-5193538b: every container-side precondition passed, but the
        node kernel lacked netem support — the plan's feasibility was built
        on documentation, not runtime evidence. The wording is deliberately a
        derivation principle, not an enumerated checklist: enumerated
        directions go stale; the mechanism's dependency set is derived per
        task and its concrete preconditions live in the skill case.
        """
        for section in (
            get_core_principles_section(),
            get_remember_section(),
        ):
            assert "derive the fault mechanism's dependency set" in section
        workflow = get_workflow_section()
        assert "derive what the fault mechanism depends on to work" in workflow
        assert "only as viable as the substrate it" in workflow
        assert "kernel or operator capability the mechanism needs" in workflow

    def test_executor_core_principles_section_content(self):
        section = get_executor_core_principles_section()
        assert "# Core Principles" in section
        assert "UNVERIFIED" in section
        assert "runtime evidence" in section
        assert "adaptively" in section
        assert "do not retry or re-plan" not in section
        assert "STOP" in section

    def test_executor_remember_section_content(self):
        section = get_executor_remember_section()
        assert "# REMEMBER" in section
        assert "UNVERIFIED" in section
        assert "runtime evidence" in section
        assert "adaptively" in section
        assert "STOP" in section
        # request_replan must be presented as a normal tool call, never a
        # printed JSON/text marker (the text form induced verbalized
        # "[Tool call: request_replan]" output that matched no channel).
        assert "request_replan" in section
        assert "<replan_request>" not in section

    def test_executor_remember_rejects_replan_text_marker(self):
        """The recency-zone mirror must carry the same anti-text-marker
        contract as Core Principles — alignment tests only check shared
        bullets, so the REMEMBER-only replan escape rule needs its own
        negative guard against regression to the printed-marker form."""
        section = get_executor_remember_section()
        assert "request_replan" in section
        assert "<replan_request>" not in section
        assert "never describe it in prose" in section

    def test_executor_core_principles_and_remember_are_aligned(self):
        """REMEMBER must reinforce the same rules as executor Core Principles."""
        core = get_executor_core_principles_section()
        remember = get_executor_remember_section()
        for line in core.splitlines():
            if line.startswith("- "):
                assert line in remember, (
                    f"Executor Core Principles rule not found in REMEMBER: {line!r}"
                )

    def test_env_section_format(self):
        section = get_env_section({"blade_version": "1.7.0", "k8s_available": True})
        assert "## Environment" in section
        assert "blade_version: 1.7.0" in section
        assert "k8s_available: True" in section


class TestBuildInjectSystemPrompt:
    """Test the prompt assembler (迁移点 1)."""

    def test_basic_assembly(self):
        prompt = build_inject_system_prompt(skill_catalog="- pod-kill: Pod kill skill")
        assert "Chaos Engineering Agent" in prompt
        assert "pod-kill" in prompt
        assert "Skill Index" in prompt

    def test_contains_all_sections(self):
        prompt = build_inject_system_prompt(skill_catalog="test-skill")
        # All major section headers should be present
        assert "Workflow" in prompt
        assert "Safety Rules" in prompt
        assert "Tool Usage Guidelines" in prompt
        assert "Important Guidelines" in prompt
        # REMEMBER segment (U-shaped recency zone)
        assert "# REMEMBER" in prompt
        # Removed from Phase 1 (tool-agnostic redesign):
        # Communication Style and K8s Cluster Connection are Phase 2 only
        assert "Communication Style" not in prompt
        assert "K8s Cluster Connection" not in prompt

    def test_with_env_info(self):
        """迁移点 7: 环境信息注入"""
        prompt = build_inject_system_prompt(
            skill_catalog="test-skill",
            env_info={"blade_version": "1.7.0", "k8s_available": True},
        )
        assert "## Environment" in prompt
        assert "blade_version: 1.7.0" in prompt
        # Environment section should appear early (after role section)
        role_pos = prompt.find("Chaos Engineering Agent")
        env_pos = prompt.find("## Environment")
        assert env_pos > role_pos

    def test_without_env_info_no_environment_section(self):
        prompt = build_inject_system_prompt(skill_catalog="test-skill")
        assert "## Environment" not in prompt

    def test_no_empty_sections(self):
        """All sections should produce non-empty output."""
        prompt = build_inject_system_prompt(skill_catalog="test-skill")
        # No double newlines from empty sections
        assert "\n\n\n\n" not in prompt


class TestIntentClarificationSectionFunctions:
    """Test intent clarification section functions — English, U-shaped."""

    def test_role_section_english(self):
        section = get_intent_role_section()
        assert "Blade AI" in section
        assert "chaos engineering" in section
        # Three intent types present (lowercase in role section)
        assert "inject" in section
        assert "batch" in section
        assert "recover" in section

    def test_role_section_keeps_identity_product_facing(self):
        """Role must NOT name the three operating roles.

        Whatever is written into §1 Role gets recited back to users
        verbatim when they ask "你是谁" — observed in sess_c9aba7fa6492,
        where the model answered with a numbered recitation of
        follower/prober/guide straight off the prompt. The three roles
        are internal behaviour rules and live in §2 Truthfulness; Role
        stays pure product positioning so self-introduction has no
        internal instruction structure to leak.
        """
        for semantic_only in (False, True):
            section = get_intent_role_section(semantic_only=semantic_only)
            assert "Blade AI" in section
            assert "professional" in section
            # the operating roles must not appear in identity
            assert "follower of the user's intent" not in section
            assert "prober of the real environment" not in section
            assert "guide when the two disagree" not in section
            assert "three roles" not in section

    def test_priorities_section_has_3_priorities(self):
        section = get_intent_priorities_section()
        assert "Three Priorities" in section
        assert "Truthfulness" in section
        assert "Proactiveness" in section
        assert "Convergence" in section

    def test_dialogue_routing_section_has_routes(self):
        section = get_intent_dialogue_routing_section()
        assert "Dialogue Routing" in section
        assert "Recover" in section
        assert "Batch" in section
        assert "Pure text response" in section

    def test_parameter_model_section(self):
        section = get_intent_parameter_model_section()
        assert "scope" in section
        assert "target" in section
        assert "action" in section
        assert "target identity fields" in section

    def test_parameter_model_requires_duration(self):
        """Duration is a mandatory presentation item of the intent summary.

        Guards the duration contract: the model must surface a duration
        (user's value or the system recommended default, stating which),
        and must never smuggle it into ``params`` as ``timeout`` — that
        key is rejected by the submission chain.
        """
        section = get_intent_parameter_model_section()
        assert "duration" in section
        assert "system recommended default" in section
        assert "duration_seconds" in section
        assert "rejected" in section
        # sess_67b835f8977c: intensity got declared in the submit summary,
        # the window never was — the user approved a duration they never saw.
        assert "nobody approved" in section
        assert "states the window" in section

    def test_parameter_model_states_params_provenance_rule(self):
        """Intent accuracy: the probed environment is the ONLY authority.

        Regression guard for the sess_6645c3eaa130 incident, which proved
        both failure modes at once: the intent node copied a skill-case
        example value (``port: 8080``) into ``params`` AND never probed
        the target's actual probe config — the submitted value had no
        ground truth at all, yet downstream preserved it as the
        user-approved contract and the planner called it "the user's
        specified port". Hence the rule, stated once in §2 Truthfulness:
        environment probe results are the only authority; the user's
        words are direction, not data (a user-stated value probing
        cannot verify must be deferred to the environment); case
        examples are templates; an unprobed environment-bound value is
        never submitted. §4 keeps a pointer only.
        """
        for semantic_only in (False, True):
            section = get_intent_priorities_section(semantic_only=semantic_only)
            assert "intent accuracy is non-negotiable" in section
            assert "ONLY authority" in section
            # user words demoted to direction — probing follows them
            assert "direction, " in section and "not data" in section
            # on conflict: NEVER auto-substitute and submit — guide the
            # user with an environment-verified recommendation and let
            # them choose (follower AND guide, not a silent executor)
            assert "NEVER submit" in section
            assert "recommend the environment-verified alternative" in section
            assert "let the user choose" in section
            # case examples demoted to templates
            assert "templates" in section
            # probe-before-submit obligation
            assert "probe it first, or omit it" in section
            # the consequence that makes the rule matter
            assert "user-approved" in section
        # §4 points at the rule instead of restating it
        params_section = get_intent_parameter_model_section()
        assert "Truthfulness rule above" in params_section
        assert "non-negotiable" not in params_section

    def test_inject_flow_section(self):
        section = get_intent_inject_flow_section()
        assert "Inject Flow" in section
        assert "submit_fault_intent" in section
        assert "Probe" in section
        assert "Recommend" in section

    def test_inject_flow_outcome_vs_means_teaches_criteria_not_verdicts(self):
        """sess_9d6b3bbfe54f: the user's "make the component down" (an
        OUTCOME) was silently single-picked into "Pod deleted" out of five
        realizing forms — the user had to redirect to process-kill by hand.
        The rule must teach the ranking CRITERIA and the presentation shape
        (top-ranked primary + alternatives with observable differences);
        WHICH form wins stays the model's judgement from case facts, so the
        agent layer must never bake in a domain verdict.

        intent-outcome-to-means: the rule now also routes through the
        knowledge doc's question chain (what must break → which families →
        four-axis comparison) and adds the sibling behavioural rules —
        list-before-probe, the pre-recommendation
        query_active_experiments compound-state check, and explicit
        handling of partial answers — all sourced from trace
        sess_4b696f566f23's FM1/FM2/FM4/FM5.
        """
        for kwargs in ({}, {"semantic_only": True}):
            section = get_intent_inject_flow_section(**kwargs)
            assert "Outcome vs means" in section
            # routes through the methodology doc's question chain
            assert "outcome-to-means.md" in section
            assert "question chain" in section
            assert "fault families" in section
            # realism-first ranking: a drill rehearses what production
            # actually hits, not the fastest trigger
            assert "how likely each means is to occur in the real world" in section
            assert "how certainly it achieves the named outcome" in section
            assert "Never silently" in section
            # behavioural siblings (trace sess_4b696f566f23)
            assert "Means before carrier probe" in section
            assert "query_active_experiments" in section
            # sess_67b835f8977c: the compound warning fired at recommendation
            # time but vanished from the submit summary — the approval
            # decision was made without the composition in view
            assert "submit summary" in section
            assert "Partial answers" in section
            # no domain winner hardcoded in the agent layer (phrase-level:
            # bare "kill" collides with "skill/case")
            assert "pod deleted" not in section.lower()
            assert "process kill" not in section.lower()

    def test_inject_flow_warns_on_channel_mismatch_but_never_blocks(self):
        """One-line heads-up only — enforcement lives in the submit gate.

        The prompt must not ask the model to withhold submit_fault_intent on its
        own reading of the profile: that judgement can be wrong (incomplete skill
        descriptions, hallucination) and would leave the user no override. The
        code-side gate in ``intent_clarification`` decides, using
        ``family_for_scope`` + ``profile_of``.
        """
        section = get_intent_inject_flow_section(semantic_only=True)
        assert "Capability Profile" in section
        assert "submit tool enforces this" in section
        # must NOT instruct the model to refuse on its own
        assert "Never withhold" not in section
        assert "do not submit" not in section.lower()

    def test_inject_flow_refers_to_profile_by_name_not_position(self):
        """The Capability Profile section is assembled AFTER Inject Flow.

        So the rule must not say "above" — that would point the model at
        something not yet in the prompt.
        """
        section = get_intent_inject_flow_section(semantic_only=True)
        idx = section.index("Capability Profile")
        window = section[max(0, idx - 60):idx + 60]
        assert "above" not in window, "profile referenced by position, not name"

    def test_recover_flow_section(self):
        section = get_intent_recover_flow_section()
        assert "Recover Flow" in section
        assert "recover_task" in section
        assert "task_id" in section

    def test_batch_flow_section(self):
        section = get_intent_batch_flow_section()
        assert "Batch Boundary" in section
        assert "independent" in section
        assert 'execution_order="serial"' in section
        assert "Do not manufacture a batch" in section
        assert "submit_batch_intent" in section

    def test_operation_freshness_section(self):
        section = get_intent_operation_freshness_section()
        assert "Operation Freshness" in section
        assert "stale" in section
        assert "re-query" in section

    def test_tools_section_has_categories(self):
        section = get_intent_tools_section()
        assert "Probe" in section
        assert "Submit" in section
        assert "Route" in section
        assert "bound to you" in section

    def test_output_section(self):
        section = get_intent_output_section()
        assert "Chinese" in section
        assert "blade-fault-proposal" in section
        assert "FaultSpec" in section
        # Revision-free replay contract: the model replays execution fields
        # only; the server-owned revision must never surface as a replay
        # instruction (strict revision replay caused same-turn trailer+submit
        # rejections — the model cannot observe the revision its own trailer
        # just advanced).
        assert "execution" in section and "fields exactly" in section
        assert "carry its exact revision" not in section
        assert "fault_revision" not in section

    def test_fault_contract_includes_current_fault_spec(self):
        section = get_intent_completeness_section({
            "revision": 1,
            "objective": "test",
            "scope": "pod", "fault_target": "network", "fault_action": "drop",
            "namespace": "default", "names": [], "labels": {}, "params": {},
            "boundaries": [], "constraints": [], "assumptions": [],
        })
        assert "Reviewed FaultSpec" in section
        assert '"objective": "test"' in section
        # The server-owned revision is hidden from the contract view so the
        # model can never carry or quote it.
        assert '"revision"' not in section

    def test_fault_contract_shows_partial_collection_state(self):
        section = get_intent_completeness_section({"scope": "pod"})
        assert '"scope": "pod"' in section

    def test_fault_contract_is_present_without_previous_state(self):
        section = get_intent_completeness_section(None)
        assert "No FaultSpec has been collected yet" in section

    def test_reminder_section_recaps_rules(self):
        section = get_intent_reminder_section()
        assert "REMEMBER" in section
        assert "target authority" in section
        assert "submit" in section
        assert "Probe" in section


class TestBuildIntentClarificationPrompt:
    """Test intent clarification prompt builder — U-shaped assembly."""

    def test_basic_assembly(self):
        prompt = build_intent_clarification_prompt()
        assert "Blade AI" in prompt
        assert "Three Priorities" in prompt
        assert "REMEMBER" in prompt

    def test_u_shaped_structure(self):
        """Priorities at beginning + reminder at end."""
        prompt = build_intent_clarification_prompt()
        # Priorities near beginning (primacy zone)
        priorities_pos = prompt.find("Three Priorities")
        # REMEMBER near end (recency zone)
        reminder_pos = prompt.find("# REMEMBER")
        assert priorities_pos > 0
        assert reminder_pos > 0
        assert reminder_pos > priorities_pos
        # Reminder should be in the last 20% of the prompt
        assert reminder_pos > len(prompt) * 0.8

    def test_cache_boundary_present(self):
        """CACHE_BOUNDARY separates stable from dynamic sections."""
        prompt = build_intent_clarification_prompt()
        assert "BLADE_AI_CACHE_BOUNDARY" in prompt

    def test_with_fault_spec(self):
        """The reviewed FaultSpec is injected below the cache boundary."""
        prompt = build_intent_clarification_prompt(
            fault_spec={
                "revision": 1,
                "objective": "network isolation",
                "scope": "pod", "fault_target": "network", "fault_action": "drop",
                "namespace": "default", "names": [], "labels": {}, "params": {},
                "boundaries": [], "constraints": [], "assumptions": [],
            },
        )
        assert "Reviewed FaultSpec" in prompt
        assert '"objective": "network isolation"' in prompt

    def test_without_fault_spec_uses_empty_contract(self):
        """The protocol always exposes the current contract state to the model."""
        prompt = build_intent_clarification_prompt()
        assert "No FaultSpec has been collected yet" in prompt

    def test_inject_flow_in_assembled_prompt(self):
        prompt = build_intent_clarification_prompt()
        assert "Inject Flow" in prompt
        assert "Recover Flow" in prompt
        assert "Batch Flow" in prompt

    def test_prompt_mode_intent(self):
        """PromptMode.INTENT routes to build_intent_clarification_prompt."""
        from chaos_agent.agent.prompts.builders import build_system_prompt
        prompt = build_system_prompt(PromptMode.INTENT)
        assert "Blade AI" in prompt
        assert "Three Priorities" in prompt

    def test_semantic_intent_prompt_without_profile_omits_capability_section(self):
        # Previously named ``..._without_transport_profile``. The semantic
        # intent stage no longer *always* hides the capability profile — it
        # now states a KNOWN channel as a fact so a host-channel environment
        # stops loading k8s-chaos-skills. What stays invariant is that the
        # full skill catalog is always shown (nothing is filtered), and when
        # no profile is resolvable the section is omitted (degrades to the
        # original behaviour). See the two tests below for the profile-aware
        # and unknown-channel paths.
        prompt = build_intent_clarification_prompt(
            skill_catalog="- k8s: pod cpu pressure\n- host: cpu pressure",
            semantic_only=True,
        )

        assert "`k8s`: pod cpu pressure" in prompt
        assert "`host`: cpu pressure" in prompt
        # Judge by the fragment's own body, not by the words "Capability
        # Profile" — the Inject Flow self-check rule refers to that section by
        # name, so the bare title appears even when no fragment is emitted.
        assert "## Capability Profile" not in prompt
        assert "You are operating" not in prompt
        assert "kubectl_read" not in prompt
        assert "full fault catalog" in prompt

    def test_semantic_intent_prompt_states_known_profile_without_filtering(self):
        # A resolved profile is stated as a fact, but the catalog is NOT
        # filtered — both skills remain visible. This is "inform, don't
        # restrict": it fixes skill mis-selection without reintroducing the
        # rejected hard-routing.
        prompt = build_intent_clarification_prompt(
            skill_catalog="- k8s: pod cpu pressure\n- host: cpu pressure",
            semantic_only=True,
            profile="host",
        )

        assert "Capability Profile" in prompt
        assert "configured host" in prompt
        # catalog still complete
        assert "`k8s`: pod cpu pressure" in prompt
        assert "`host`: cpu pressure" in prompt

    def test_semantic_intent_prompt_unknown_profile_omits_discouraging_wording(self):
        # An unresolvable channel must NOT surface the "environment is
        # unsupported, do not attempt injection" fragment — that would be
        # strictly worse than the pre-fix behaviour. It degrades to no
        # capability section instead.
        prompt = build_intent_clarification_prompt(
            skill_catalog="- k8s: pod cpu pressure\n- host: cpu pressure",
            semantic_only=True,
            profile="unknown",
        )

        assert "## Capability Profile" not in prompt
        assert "You are operating" not in prompt
        assert "unsupported" not in prompt
        assert "Do not attempt injection" not in prompt
        # catalog still complete
        assert "`k8s`: pod cpu pressure" in prompt
        assert "`host`: cpu pressure" in prompt


class TestExecutorEffectObservationBoundary:
    """inject-9bf2dddd: after a successful injection the executor spent 571s
    watching the fault effect (waits + repeated sampling + stability checks)
    and ate the fault's active window, leaving the verifier ~29s. The cause
    was prompt-level: no positive exit criterion (the plan's 'Verification
    Methods' numbers became the model's exit gate) plus wording that eroded
    the receipt's authority ('do not treat a single command result as the
    final verdict'). These tests freeze the fix: the receipt is the
    executor's completion proof, effect observation belongs to verification,
    and the plan is structurally sliced to its mutation steps.
    """

    def test_receipt_completion_rule_in_core_principles(self):
        section = get_executor_core_principles_section()
        # Receipt = proof a step was ISSUED (single source for the receipt
        # concept); the STOP rule stays step-aware ("ALL steps"), so
        # multi-step / hybrid injections never stop after the first receipt.
        assert "A step is complete when its mutation is ISSUED" in section
        assert "When ALL steps are issued, STOP" in section
        assert "do not wait for, sample, or stabilize the fault effect" in section
        # the replan channel is named so the right-to-switch-method survives
        assert "returns to you through replan" in section

    def test_receipt_completion_rule_in_remember(self):
        # U-shaped attention: the recency anchor must carry the same rule.
        section = get_executor_remember_section()
        assert "A step is complete when its mutation is ISSUED" in section

    def test_receipt_authority_wording_not_eroded(self):
        # The old phrasing taught the model that its receipt cannot be
        # trusted as proof of the real-world effect — driving post-injection
        # effect observation. It must be gone from the Phase 2 tools section,
        # which returns to its original read-only-context focus.
        tools = get_tools_section(phase=2)
        assert "final verdict on the real-world effect" not in tools
        assert "verification and recovery lifecycle" in tools

    def test_plan_sliced_to_execution_steps(self):
        plan = (
            "# Some fault\n\n"
            "## Execution Steps\n"
            "1. issue the mutation\n\n"
            "## Verification Methods\n"
            "observe 2 samples at 30s intervals until ~80%\n\n"
            "## Rollback and Recovery\n"
            "destroy the experiment\n"
        )
        directives = get_execution_directives_section(plan=plan, plan_path="/tmp/p.md")
        assert "1. issue the mutation" in directives
        assert "observe 2 samples" not in directives
        assert "destroy the experiment" not in directives
        # the textual ban is replaced by structural isolation
        assert "do NOT execute them" not in directives

    def test_plan_without_header_falls_back_to_full_text(self):
        assert _execution_steps_only("the approved plan body") == "the approved plan body"

    def test_slice_keeps_steps_drops_later_sections(self):
        plan = (
            "## Execution Steps\n"
            "1. step one\n"
            "2. step two\n"
            "## Expected Impact\n"
            "memory ~80%\n"
        )
        sliced = _execution_steps_only(plan)
        assert "step one" in sliced and "step two" in sliced
        assert "memory ~80%" not in sliced

    def test_planned_wait_step_exemption(self):
        # inject-aac02265 (#35): the executor burned a full 27s turn
        # reconciling the observation ban against a wait step the plan
        # itself declared (taint → 30s propagation → untaint). The ban
        # targets ADDED observations; a plan-declared wait sequences the
        # mutations and must be executed literally. The exemption is
        # scoped to plan-declared steps only — both bans stay intact.
        section = get_execution_directives_section()
        assert (
            "A wait or pause the plan declares as a step is executed as "
            "written, not skipped." in " ".join(section.split())
        )
        # the two bans the exemption carves around are untouched
        assert "verification work — skip it" in section
        assert "Do not add effect observations" in section


class TestExecutorPlanContractDiscipline:
    """tier1-speedup unit 3 — the executor's first-round budget was burned
    re-deriving already-frozen commands (task inject-8b757abb: 286s before
    the first tool call). The pre-change "current hypothesis, not a script"
    wording licensed that re-derivation. These tests freeze the contract
    framing and the discipline clauses that forbid it.
    """

    def test_contract_framing_replaces_hypothesis_wording(self):
        section = get_execution_directives_section()
        assert "contract, not a hypothesis" in section
        # The old licensing phrase must be gone from the orchestration section
        assert "current hypothesis, not a script" not in section

    def test_discipline_clauses_present_with_plan(self):
        plan = "## Execution Steps\n1. issue the mutation\n"
        section = get_execution_directives_section(plan=plan)
        # (1) written order
        assert "in their written order" in section
        # (2) exactly one read-only existence check before the first mutation
        assert "exactly one read-only call" in section
        assert "still exists" in section
        # (3) re-derivation ban
        assert "Do NOT re-derive command texts" in section
        assert "re-evaluate quoting or" in section
        # (4) tool-error correction is legitimate; twice = replan
        assert "legitimate path" in section
        assert "failing twice means replan" in section

    def test_plan_conditioned_clauses_absent_without_plan(self):
        section = get_execution_directives_section()
        # No frozen plan → no plan-conditioned discipline; nothing to forbid
        # re-deriving.
        assert "Do NOT re-derive command texts" not in section
        assert "in their written order" not in section


class TestCrChannelRoutingGuide:
    """openspec faultdrill-cr-channel D3 source 2 — the Workflow routing
    guide that teaches the planner to read the skill case's
    ``recovery_channel`` declaration. Gated by the builder on
    (faultdrill_enabled ∧ K8s profile): the dark-launch window keeps the
    section byte-identical to pre-change."""

    def test_dark_launch_section_is_byte_identical(self):
        default = get_workflow_section()
        assert default == get_workflow_section(include_cr_channel_routing=False)
        assert "4b." not in default
        assert "recovery_channel" not in default
        assert "FaultDrill" not in default

    def test_guide_splices_cleanly_at_the_step_boundary(self):
        # Enabled output minus the guide is EXACTLY the pre-change section:
        # the guide inserts between step 4 and step 5 and disturbs nothing
        # else — no renumbering, no rewording of neighbouring steps.
        default = get_workflow_section()
        on = get_workflow_section(include_cr_channel_routing=True)
        i = on.index("4b. **Recovery-channel routing**")
        j = on.index("5. **Assess complexity**")
        assert on[:i] + on[j:] == default

    def test_guide_teaches_the_d3_route(self):
        on = get_workflow_section(include_cr_channel_routing=True)
        i = on.index("4b. **Recovery-channel routing**")
        j = on.index("5. **Assess complexity**")
        guide = " ".join(on[i:j].split())
        # Declaration routes: apiserver-write → the FaultDrill CR channel.
        assert "recovery_channel: apiserver-write" in guide
        assert "FaultDrill custom resource" in guide
        # Recovery single-source: the channel arms its own guards — no SOP
        # stacking (the reconciler race guard, same legislation as 2.2).
        assert "landing readback and reconciler it arms itself" in guide
        assert "do NOT stack a recovery-carrier SOP" in guide
        # No declaration → documented path unchanged (M3 not landed yet:
        # zero behavior change until cases carry the field).
        assert "keep their documented path unchanged" in guide
        # CRD-unavailable degradation completes at the plan layer.
        assert "pre-task environment probes message" in guide
        assert "recovery-carrier SOP form directly" in guide
        assert "no CR attempt round" in guide

    def test_builder_gate_combines_flag_with_profile(self):
        # The builder gate (not the section) combines the caller's feature
        # flag with the K8s profile: host/unknown channels never see the
        # guide even with the flag on; the prompts layer reads no settings.
        p_on = build_inject_system_prompt("catalog", profile="k8s", cr_channel_enabled=True)
        assert "4b. **Recovery-channel routing**" in p_on
        p_off = build_inject_system_prompt("catalog", profile="k8s")
        assert "4b." not in p_off
        p_host = build_inject_system_prompt("catalog", profile="host", cr_channel_enabled=True)
        assert "4b." not in p_host
        p_unknown = build_inject_system_prompt("catalog", profile="unknown", cr_channel_enabled=True)
        assert "4b." not in p_unknown
