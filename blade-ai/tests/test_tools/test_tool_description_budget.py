"""Budget guard for LLM-visible tool schemas (anti-backflow).

Context: the kubectl docstring was once slimmed and its long-tail
examples moved to ``knowledge/kubectl-recipes.md`` ("examples that the
kubectl tool docstring no longer carries inline"). Without a guard, the
docstring grew back from 5179 → 5546 → 6318 chars. This test pins the
budget so any future growth must be a conscious decision, not drift.

Measurement caliber (calibrated 2026-07, reproduces the 12696-tok
baseline of the slimming plan):
  ``json.dumps(convert_to_openai_tool(tool), ensure_ascii=False)`` — the
  FULL schema the LLM sees (description + args). Counted with
  o200k_base, which tracks Qwen2.5 within ±1.4% on these schemas
  (K=qwen/o200k measured 0.979-1.037 across all 22 tools), so the MCP
  warn threshold (500 tok per tool) applies directly. ``transformers``
  + the Qwen2.5 tokenizer stays the arbiter when in doubt.

Rules when a cap trips:
  1. New content is a CLASS-A constraint distilled from a real incident
     (MUST/NOT/refused/auto-stripped) → raise that ONE tool's cap with a
     comment citing the incident, keep the total cap untouched.
  2. New content is a long-tail EXAMPLE / recipe → it belongs in
     ``knowledge/kubectl-recipes.md`` (or the matching knowledge doc),
     with a one-line pointer in the docstring. Do NOT raise the cap.
  3. Anything else → compress or drop; the schema budget is load-bearing
     for prompt-cache and context-window costs.
"""

import json

import pytest
import tiktoken
from langchain_core.utils.function_calling import convert_to_openai_tool

from chaos_agent.agent.nodes.planning.intent_clarification import (
    query_active_experiments,
    recover_task,
    submit_batch_intent,
    submit_fault_intent,
)
from chaos_agent.agent.nodes.verify._verifier_submit import (
    submit_recover_verification,
    submit_verification,
)
from chaos_agent.agent.replan import request_replan
from chaos_agent.tools.blade import (
    blade_create,
    blade_destroy,
    blade_help,
    blade_query_k8s,
    blade_status,
)
from chaos_agent.tools.blade_python import (
    blade_python_create,
    blade_python_prepare,
    blade_python_revoke,
)
from chaos_agent.tools.host_cmd import host_inject, host_read
from chaos_agent.tools.knowledge_reader import read_knowledge_resource
from chaos_agent.tools.kubectl import kubectl, kubectl_read
from chaos_agent.tools.progress import update_progress
from chaos_agent.tools.wait import time_wait

_ENCODING = tiktoken.get_encoding("o200k_base")


def _schema_tokens(tool) -> int:
    """Tokens of the FULL tool schema as serialized to the LLM."""
    schema = json.dumps(convert_to_openai_tool(tool), ensure_ascii=False)
    return len(_ENCODING.encode(schema))


# Per-tool full-schema caps (tokens, o200k_base ≈ Qwen2.5). Target is the
# MCP warn threshold 500; the six tools above it sit at their CLASS-A
# floor — their remaining content is incident-distilled constraints plus
# the only enumeration of valid values, none of which may be dropped
# (slimming-plan hard constraint: "A 类一条不删").
_CAPS: dict[str, tuple[object, int]] = {
    "kubectl": (kubectl, 610),                      # class-A floor (~593 qwen)
    "kubectl_read": (kubectl_read, 570),            # class-A floor (~552 qwen)
    "blade_python_create": (blade_python_create, 605),  # class-A floor (~592)
    "blade_python_prepare": (blade_python_prepare, 500),
    "blade_python_revoke": (blade_python_revoke, 375),
    "submit_fault_intent": (submit_fault_intent, 595),  # class-A floor (~586) + dynamic INTENT_* enums
    "submit_batch_intent": (submit_batch_intent, 380),
    "submit_verification": (submit_verification, 495),
    "submit_recover_verification": (submit_recover_verification, 400),
    "blade_create": (blade_create, 610),            # class-A floor (~597)
    "blade_destroy": (blade_destroy, 365),
    "blade_status": (blade_status, 325),
    "blade_help": (blade_help, 305),
    "blade_query_k8s": (blade_query_k8s, 360),
    "request_replan": (request_replan, 560),        # class-A floor (~550)
    "host_read": (host_read, 510),
    "host_inject": (host_inject, 495),
    "read_knowledge_resource": (read_knowledge_resource, 440),
    "time_wait": (time_wait, 225),
    "query_active_experiments": (query_active_experiments, 220),
    "recover_task": (recover_task, 230),
    "update_progress": (update_progress, 365),
}

# The two flagship tools carry the full five-section structure; long-tail
# examples live in knowledge/kubectl-recipes.md, not inline.
_STRUCTURED_TOOLS = (kubectl, kubectl_read)
_SECTIONS = (
    "When to use:",
    "Inputs:",
    "Output:",
    "Side effects:",
    "Constraints",
)

# Total budget across all tracked schemas (current ~9390). If this trips,
# the growth is aggregate drift — find the culprit via the per-tool caps.
_TOTAL_CAP = 9600


class TestToolDescriptionBudget:
    @pytest.mark.parametrize(
        "name", sorted(_CAPS), ids=lambda n: n
    )
    def test_schema_within_cap(self, name: str) -> None:
        tool, cap = _CAPS[name]
        count = _schema_tokens(tool)
        assert count <= cap, (
            f"{name} full schema grew to {count} tok (cap {cap}; MCP warn "
            "threshold 500). If this is a class-A incident constraint, "
            "raise THIS cap with a comment citing the incident. If it is a "
            "long-tail example, move it to knowledge/ (see "
            "kubectl-recipes.md) and leave a pointer. See "
            "tests/test_tools/test_tool_description_budget.py header."
        )

    @pytest.mark.parametrize(
        "tool", _STRUCTURED_TOOLS, ids=lambda t: t.name
    )
    def test_flagship_tools_keep_five_sections(self, tool) -> None:
        desc = tool.description
        missing = [s for s in _SECTIONS if s not in desc]
        assert not missing, (
            f"{tool.name} lost structural section(s) {missing}. The unified "
            "structure (When to use / Inputs / Output / Side effects / "
            "Constraints) is a hard constraint of the slimming plan — "
            "compress WITHIN sections, never drop the headers."
        )

    def test_total_budget(self) -> None:
        total = sum(_schema_tokens(t) for t, _ in _CAPS.values())
        assert total <= _TOTAL_CAP, (
            f"Total tool-schema budget is {total} tok (cap {_TOTAL_CAP}). "
            "See tests/test_tools/test_tool_description_budget.py header "
            "for the class-A / long-tail / drift triage rules."
        )
