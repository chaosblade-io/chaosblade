"""submit_verification control-signal tool (Scheme B).

Mirrors the agent_loop ``finish_planning`` pattern: the verifier LLM calls
``submit_verification`` to end the verification ReAct loop and hand a
STRUCTURED verdict to the ``finalize_verification`` node. Going through a
real tool (ToolNode) keeps the message history well-formed (every tool_call
gets a ToolMessage) and decouples "I'm done" from "no tool_calls" — so a
verdict bundled with a cleanup tool_call no longer forces the LLM to repeat
its verdict on a second turn.

The tool body is a no-op confirmation; the verdict lives in the call's
ARGS, read by ``finalize_verification`` from the AIMessage.

Vocabulary single-sourcing (B76 round-14): every word list taught below
is DERIVED from the verdict enums at import time. The enum is the only
place a vocabulary literal may live — before this, the two docstrings
hand-copied subsets that had already drifted from each other and from
the legislation (each taught a different 5-of-6 layer2 set; the recover
checklist line taught 4 of 7 item words). Gloss maps are keyed by enum
member, so adding a member without its gloss raises KeyError at import —
teaching cannot silently miss a legislated word.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from langchain_core.tools import tool
from pydantic import BeforeValidator

from chaos_agent.agent.result.verdict import (
    ChecklistItemStatus,
    InjectVerdict,
    Layer2Status,
    RecoverVerdict,
    ResidualAttribution,
)

logger = logging.getLogger(__name__)

SUBMIT_VERIFICATION_TOOL_NAME = "submit_verification"


def _coerce_list_arg(v):
    """Parse a JSON-stringified ``list`` argument into a real list.

    Some models JSON-stringify structured tool args before serialising the
    tool_call — a known qwen-class quirk handled the same way for
    ``update_progress`` (``tools/progress._coerce_json_arg``) and
    ``submit_fault_intent``. Without it the ``list`` annotation rejects the call
    at the ``@tool`` boundary with "Input should be a valid list". Live run
    inject-6129a6da (Case #81): the verifier's first ``submit_verification``
    passed ``checklist`` as a JSON string with a leading newline, was rejected,
    and burned a round-trip re-submitting the identical verdict as a real array.

    Anything already a list, or that cannot be parsed into one, is returned
    untouched so pydantic still reports a GENUINE type error rather than this
    helper masking one.
    """
    if not isinstance(v, str):
        return v
    s = v.strip()
    if not s:
        return None
    try:
        parsed = json.loads(s)
    except (ValueError, TypeError):
        logger.debug("submit_verification: list arg is not parseable JSON: %r", s[:120])
        return v
    if isinstance(parsed, list):
        return parsed
    logger.debug(
        "submit_verification: list arg parsed to %s, expected list",
        type(parsed).__name__,
    )
    return v


_INJECT_OVERALL_GLOSSES = {
    InjectVerdict.VERIFIED: "effect directly confirmed against baseline",
    InjectVerdict.PARTIAL: "injected, effect only partially/indirectly confirmed",
    InjectVerdict.UNVERIFIED: "could not confirm the fault effect",
}

_INJECT_OVERALL_LINE = " |\n          ".join(
    f'"{m.value}" ({_INJECT_OVERALL_GLOSSES[m]})' for m in InjectVerdict
)

_SUBMIT_VERIFICATION_DOC = f"""Verifier ONLY. Submit the FINAL verdict and end verification.

Call as your LAST action; no free-text VERIFICATION_RESULT — this call IS the
verdict.

Inputs:
  - overall: {_INJECT_OVERALL_LINE}.
  - layer2_status: "{"|".join(m.value for m in Layer2Status)}"
      (fault effect observable?).
  - layer2_details: one-line evidence.
  - primary_evidence_observed: true ONLY if you directly observed the fault's
      PRIMARY effect (not a side effect); "verified" requires it.
  - baseline_used: compared vs pre-injection baseline.
  - checklist: [{{"step": int, "status":
      "{"|".join(m.value for m in ChecklistItemStatus)}",
      "evidence": str}}], per skill-case step.
  - metric_claims: optional [{{"metric","baseline","post","kind?","source?"}}]
      cross-checked vs timeline.
  - warnings: optional strings.
  - chosen_candidate: chosen index (multi-candidate); else 0.

Consistency contract (enforced on submit — inconsistent DOWNGRADED):
  - "verified" + primary_evidence_observed=false → "partial";
    "verified" + layer2_status="failed" → "unverified";
    layer2_status="partial" → overall "partial".
  - layer2_status="passed" + a "failed" item → DOWNGRADED to "partial";
    mark a non-real-failure step "expected"|"partial"|"recovered_before_observation",
    not "failed".

Output: confirmation string."""


@tool(SUBMIT_VERIFICATION_TOOL_NAME, description=_SUBMIT_VERIFICATION_DOC)
def submit_verification(
    overall: str,
    layer2_status: str,
    layer2_details: str = "",
    primary_evidence_observed: bool = False,
    baseline_used: bool = False,
    checklist: Annotated[list, BeforeValidator(_coerce_list_arg)] = None,
    metric_claims: Annotated[list, BeforeValidator(_coerce_list_arg)] = None,
    warnings: Annotated[list, BeforeValidator(_coerce_list_arg)] = None,
    chosen_candidate: int = 0,
) -> str:
    """No-op confirmation — the verdict is taken from the call args by
    ``finalize_verification`` (description above is enum-derived)."""
    return "Verification verdict recorded."


SUBMIT_RECOVER_VERIFICATION_TOOL_NAME = "submit_recover_verification"

_SUBMIT_RECOVER_VERIFICATION_DOC = f"""Recover verifier ONLY. Submit the FINAL recovery verdict and end
verification. Call as your LAST action after observing the CURRENT
post-recovery state — this call IS the verdict (no free-text
RECOVERY_VERIFICATION_RESULT).

Inputs:
  - overall: {" | ".join(f'"{m.value}"' for m in RecoverVerdict)}
      ("unverified" = you could NOT observe the post-recovery state —
      evidence unavailable, NOT evidence the fault persists)
  - layer2_status: {" | ".join(f'"{m.value}"' for m in Layer2Status)}
      (passed = fault effect absent; a converging tail does not block it)
  - layer2_details: one-line evidence summary.
  - baseline_used: compared against the pre-injection baseline.
  - residual_attribution: {" | ".join(f'"{m.value}"' for m in ResidualAttribution)}
      ("recovered" + fault-attributed residuals: downgraded).
  - checklist: [{{"step": int, "status": "{"|".join(m.value for m in ChecklistItemStatus)}",
      "evidence": str}}], one per verification step.
  - warnings: optional warning strings.

Output: confirmation string (the verdict is taken from these args)."""


@tool(
    SUBMIT_RECOVER_VERIFICATION_TOOL_NAME,
    description=_SUBMIT_RECOVER_VERIFICATION_DOC,
)
def submit_recover_verification(
    overall: str,
    layer2_status: str,
    layer2_details: str = "",
    baseline_used: bool = False,
    residual_attribution: str = "none",
    checklist: Annotated[list, BeforeValidator(_coerce_list_arg)] = None,
    warnings: Annotated[list, BeforeValidator(_coerce_list_arg)] = None,
) -> str:
    """No-op confirmation — the verdict is taken from the call args by
    ``finalize_recover_verification`` (description above is enum-derived)."""
    return "Recovery verdict recorded."
