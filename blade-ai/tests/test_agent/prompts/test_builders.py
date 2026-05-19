"""Tests for the Phase 4 builder slimming behavior.

Covers:
* ``input_is_nl`` kwarg is accepted and does not break assembly,
* execute builder no longer pulls in the full verification-strategy or
  failure-modes catalogues (those moved to knowledge docs).
"""

from chaos_agent.agent.prompts.builders import (
    build_execute_system_prompt,
    build_inject_system_prompt,
)
from chaos_agent.agent.prompts.constants import CACHE_BOUNDARY


class TestInjectInputIsNlKwarg:
    def test_accepts_input_is_nl_true(self):
        # Should not raise; NL Mode section stays present unconditionally.
        prompt = build_inject_system_prompt(skill_catalog="x", input_is_nl=True)
        assert "Natural Language Mode" in prompt

    def test_accepts_input_is_nl_false(self):
        prompt = build_inject_system_prompt(skill_catalog="x", input_is_nl=False)
        assert "Natural Language Mode" in prompt


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


class TestInjectSlimmedSections:
    def test_uses_brief_role_section(self):
        prompt = build_inject_system_prompt(skill_catalog="x")
        # Brief role drops the "What You Can Do" subsection but preserves
        # "Chaos Engineering Agent" and the kube-system safety token.
        assert "What You Can Do" not in prompt
        assert "Chaos Engineering Agent" in prompt
        assert "kube-system" in prompt

    def test_uses_hard_only_safety(self):
        prompt = build_inject_system_prompt(skill_catalog="x")
        # Hard Rules + Caution Compliance kept; long-tail Advisory / Decision dropped.
        assert "### Hard Rules" in prompt
        assert "Caution Rule Compliance" in prompt
        assert "### Decision Framework" not in prompt
        assert "### Advisory Rules" not in prompt

    def test_uses_brief_verification_strategy(self):
        prompt = build_inject_system_prompt(skill_catalog="x")
        # Brief variant collapses the Strategy header and removes per-fault recipes.
        assert "Verification Strategy (Principles)" in prompt
        assert "### Verification Method Selection Reasoning" not in prompt

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
        assert "kube-system" in prompt

    def test_keeps_method_switching_block(self):
        prompt = build_execute_system_prompt(skill_catalog="x")
        # Executor needs method switching guidance when blade_create fails.
        assert "Injection Method Switching" in prompt

    def test_keeps_execution_directives(self):
        prompt = build_execute_system_prompt(skill_catalog="x")
        assert "EXECUTION PHASE DIRECTIVES" in prompt
