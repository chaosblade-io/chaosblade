"""Structured contract for requesting a return from execution to planning."""

from __future__ import annotations

import json
import re

from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

REPLAN_REQUEST_OPEN = "<replan_request>"
REPLAN_REQUEST_CLOSE = "</replan_request>"
_REQUEST_RE = re.compile(
    rf"{re.escape(REPLAN_REQUEST_OPEN)}\s*(\{{.*?\}})\s*{re.escape(REPLAN_REQUEST_CLOSE)}",
    re.DOTALL,
)


def _scalar_to_str(value: object) -> object:
    """Stringify a JSON scalar so ``list[str]`` validation accepts it.

    Models sometimes emit numeric evidence (e.g. ``[500, 502]`` HTTP codes),
    which pydantic's ``list[str]`` rejects — dropping the whole replan signal.
    Coerce int/float (bool is an int subclass) to ``str``; leave real strings
    and complex values (dict/list) untouched for pydantic to validate/reject.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return value


def _coerce_str_list(value: object) -> object:
    """Coerce an LLM-supplied value into a ``list`` for pydantic validation.

    qwen and other models frequently emit list fields as a JSON-encoded string
    (e.g. ``'["a", "b"]'``) or a bare scalar string, which pydantic rejects with
    ``Input should be a valid list``. Normalize:
      - ``None`` → ``[]`` (behave as empty)
      - ``list`` → element scalars stringified (pydantic validates the rest)
      - ``str``  → ``json.loads`` if it parses to a list (scalars stringified);
                   otherwise wrap the original string as a single-element list
                   (empty → ``[]``)
      - any other scalar → wrap as ``[str(value)]``

    Shared by ``ReplanRequest``'s field validator and reusable by any other
    structured tool whose LLM-facing list fields hit the same coercion gap.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [_scalar_to_str(v) for v in value]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return [value]
        if isinstance(parsed, list):
            return [_scalar_to_str(v) for v in parsed]
        return [value]
    return [_scalar_to_str(value)]


class ReplanRequest(BaseModel):
    """Evidence a planner needs to reassess an approved execution plan.

    This is intentionally a decision record, not hidden model reasoning.  It
    tells the graph which assumption was invalidated and whether the next plan
    must go back through target/risk confirmation.
    """

    kind: Literal["feasibility", "target", "safety", "verification"]
    decision: Literal["plan_invalid", "needs_investigation"]
    invalidated_assumption: str = Field(min_length=1, max_length=2000)
    observed_evidence: list[str] = Field(default_factory=list, max_length=20)
    # Optional semantic hints supplied by the model.  Internal tool-call IDs
    # are resolved from the runtime message history when building replan_context;
    # the model must not be asked to reproduce opaque framework identifiers.
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    affected_step: str = Field(min_length=1, max_length=500)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=20)
    changes_target_or_risk: bool = False

    @field_validator(
        "observed_evidence", "evidence_refs", "unresolved_questions", mode="before"
    )
    @classmethod
    def _coerce_list_fields(cls, value: object) -> object:
        # Models often send these as a JSON string or bare scalar; normalize
        # to a list before element validation. See ``_coerce_str_list``.
        return _coerce_str_list(value)

    def as_context(self) -> dict:
        return {
            "kind": self.kind,
            "decision": self.decision,
            "invalidated_assumption": self.invalidated_assumption,
            "observed_evidence": self.observed_evidence,
            "evidence_refs": self.evidence_refs,
            "affected_step": self.affected_step,
            "unresolved_questions": self.unresolved_questions,
            "changes_target_or_risk": self.changes_target_or_risk,
        }


def replan_request_format() -> str:
    """Return the exact public wire format emitted by execution prompts."""
    example = ReplanRequest(
        kind="feasibility",
        decision="plan_invalid",
        invalidated_assumption="the selected capability is available on the target",
        observed_evidence=["the tool result reports the selected capability is unavailable"],
        evidence_refs=[],
        affected_step="inject fault",
        unresolved_questions=["which supported capability can achieve the approved effect?"],
        changes_target_or_risk=False,
    )
    return f"{REPLAN_REQUEST_OPEN}{example.model_dump_json()}{REPLAN_REQUEST_CLOSE}"


def parse_replan_request(content: object) -> ReplanRequest | None:
    """Parse the explicit structured replan request wire format."""
    if not isinstance(content, str):
        return None

    match = _REQUEST_RE.search(content)
    if match:
        try:
            payload = json.loads(match.group(1))
            return ReplanRequest.model_validate(payload)
        except (json.JSONDecodeError, ValueError):
            return None

    return None


REQUEST_REPLAN_TOOL_NAME = "request_replan"


@tool
def request_replan(
    kind: str,
    decision: str,
    invalidated_assumption: str,
    affected_step: str,
    observed_evidence: list | str | None = None,
    evidence_refs: list | str | None = None,
    unresolved_questions: list | str | None = None,
    changes_target_or_risk: bool = False,
) -> str:
    """Declare that the APPROVED GOAL cannot be reached and execution must
    return to planning (Phase 1). Executor ONLY.

    The approved goal is the fault EFFECT on the approved TARGET within the
    approved SAFETY boundary. Replan ONLY when that goal is unreachable — no
    action still available to you could advance it. While ANY alternative
    approach remains within the approved boundary, take it and keep executing;
    do NOT replan. A single tool or command failing is evidence about that one
    attempt, never by itself a verdict that the goal is infeasible. Planning
    re-derives the plan; the confirmed target and fault type are preserved
    unless you explicitly flag a boundary change.

    When to use:
      - You have exhausted the alternatives available to you and the approved
        goal cannot be reached as planned.
      - Runtime evidence requires a genuinely different planning decision.

    Inputs:
      - kind: "feasibility" | "target" | "safety" | "verification".
      - decision: "plan_invalid" (return to planning now — goal unreachable) |
          "needs_investigation" (keep executing; an alternative is still
          available — does NOT replan).
      - invalidated_assumption: the plan assumption the evidence contradicts.
      - affected_step: the plan step that can no longer proceed as written.
      - observed_evidence: evidence strings supporting the decision.
      - evidence_refs: optional semantic hints; do NOT reproduce opaque
          framework tool-call IDs.
      - unresolved_questions: what the next plan must answer.
      - changes_target_or_risk: true ONLY if the next plan must change WHAT is
          attacked (target / fault type) or the safety boundary — forces user
          re-confirmation.

    Output: confirmation of the request, or "Error:" prefix.

    Side effects: None directly — the system returns to Phase 1 only for
    decision=plan_invalid; the FaultSpec contract is unchanged.

    Constraints (MUST READ before calling):
      - A single tool/command error is NOT sufficient grounds: try another
        approach within the approved boundary first.
      - Use ``needs_investigation``, not ``plan_invalid``, whenever any
        alternative that could still advance the approved goal remains available
        in this loop.
      - A documented alternative injection METHOD that achieves the same fault
        effect on the same target is an alternative WITHIN the approved boundary
        — switch to it and keep executing (clean up any residue first); do NOT
        replan. The tool/method is NOT part of the approved boundary — only the
        fault effect, target, and safety limits are. "A fundamentally different
        method" is therefore NOT grounds for ``plan_invalid``.
    """
    return "Replan request recorded."
