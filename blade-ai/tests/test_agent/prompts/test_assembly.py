from chaos_agent.agent.prompts.assembly import PromptSegment, assemble_prompt


def _segment(name: str, content: str, priority: str, **kwargs) -> PromptSegment:
    return PromptSegment(
        name=name,
        content=content,
        priority=priority,  # type: ignore[arg-type]
        cacheable=True,
        source="test",
        **kwargs,
    )


def test_invariant_and_contract_survive_a_tight_budget():
    prompt = assemble_prompt(
        [
            _segment("safety", "do not bypass the confirmation gate", "invariant"),
            _segment("context", "runtime context that does not fit", "context"),
            _segment("schema", "return a structured result", "contract"),
        ],
        budget=20,
    )

    assert "do not bypass" in prompt
    assert "structured result" in prompt
    assert "runtime context" not in prompt


def test_optional_segment_is_omitted_as_a_whole_unit():
    optional = "optional history must never be partially rendered"
    prompt = assemble_prompt(
        [
            _segment("core", "safety contract", "invariant"),
            _segment("history", optional, "optional"),
        ],
        budget=len("safety contract") + 2,
    )

    assert prompt == "safety contract"
    assert optional not in prompt


def test_segment_max_chars_omits_non_contractual_material_instead_of_slicing():
    prompt = assemble_prompt(
        [
            _segment("core", "required safety", "invariant"),
            _segment("knowledge", "a very long optional knowledge section", "optional", max_chars=8),
        ],
        budget=500,
    )

    assert prompt == "required safety"


def test_context_is_retained_before_optional_material_under_budget_pressure():
    prompt = assemble_prompt(
        [
            _segment("optional-history", "optional-history", "optional"),
            _segment("runtime-context", "runtime-context", "context"),
            _segment("contract", "required-contract", "contract"),
        ],
        budget=len("runtime-context\n\nrequired-contract"),
    )

    assert "runtime-context" in prompt
    assert "required-contract" in prompt
    assert "optional-history" not in prompt
