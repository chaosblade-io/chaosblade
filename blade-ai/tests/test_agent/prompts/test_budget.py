"""Prompt-budget behavior keeps whole typed segments intact."""

from chaos_agent.agent.prompts.assembly import PromptSegment, assemble_prompt


def test_prompt_budget_keeps_contracts_and_drops_optional_segments():
    result = assemble_prompt([
        PromptSegment("head", "HEAD", "invariant", True, "test"),
        PromptSegment("optional", "x" * 600, "optional", False, "test"),
        PromptSegment("tail", "TAIL", "contract", True, "test"),
    ], 300)

    assert result.startswith("HEAD")
    assert result.endswith("TAIL")
    assert "x" * 600 not in result
