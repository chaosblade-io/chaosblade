"""State update helpers for structured failure reporting.

Centralizes failure-state construction so call-sites don't hand-build
dicts with inconsistent field combinations.
"""

from __future__ import annotations

from chaos_agent.agent.verdict import FailureCategory, FailureDetail


def fail_state(
    category: FailureCategory,
    context: str = "",
    messages: list | None = None,
    alternatives: str = "",
    llm_analysis: str = "",
) -> dict:
    """Build a state update dict for a failed task.

    Returns keys: ``failure_detail`` (structured dict) and ``error``
    (short string for logs/UI fallback).

    If *llm_analysis* is provided directly it takes precedence over
    running ``extract_llm_diagnosis`` on *messages*.
    """
    if not llm_analysis:
        from chaos_agent.errors import extract_llm_diagnosis
        llm_analysis = extract_llm_diagnosis(messages) if messages else ""

    detail = FailureDetail(
        category=category,
        context=context,
        llm_analysis=llm_analysis,
        alternatives=alternatives,
    )
    return {
        "failure_detail": detail.model_dump(),
        "error": detail.to_reason_string(),
    }
