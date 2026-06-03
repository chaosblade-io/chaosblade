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
"""

from langchain_core.tools import tool

SUBMIT_VERIFICATION_TOOL_NAME = "submit_verification"


@tool
def submit_verification(
    overall: str,
    layer2_status: str,
    layer2_details: str = "",
    primary_evidence_observed: bool = False,
    baseline_used: bool = False,
    checklist: list = None,
    warnings: list = None,
    chosen_candidate: int = 0,
) -> str:
    """Verifier ONLY. Submit the FINAL verification verdict and end verification.

    Call this as your LAST action once you have gathered enough evidence.
    Do NOT also emit a free-text VERIFICATION_RESULT — this structured call
    IS the verdict. Cleanup of debug pods is handled automatically; you do
    not need to delete them yourself.

    Inputs:
      - overall: "verified" | "partial" | "unverified"
          verified = fault effect directly confirmed against baseline;
          partial = injected but effect only partially/indirectly confirmed;
          unverified = could not confirm the fault effect.
      - layer2_status: "passed" | "failed" | "partial" | "skipped" |
          "recovered_before_observation"  (fault-specific effect observable?)
      - layer2_details: one-line evidence summary.
      - primary_evidence_observed: true ONLY if you directly observed the
          fault's PRIMARY effect (not just a side effect). "verified"
          requires this to be true.
      - baseline_used: true if you compared observations against the
          pre-injection baseline provided in the context.
      - checklist: list of {"step": int, "status":
          "passed|failed|skipped|recovered_before_observation", "evidence": str},
          one entry per verification step from the skill case.
      - warnings: optional list of warning strings.
      - chosen_candidate: when multiple skill case candidates were
          provided, set this to the candidate number you chose (e.g. 2
          for Candidate 2). Leave 0 for single-candidate cases.

    Output: confirmation string (the verdict is taken from these args).
    """
    return "Verification verdict recorded."


SUBMIT_RECOVER_VERIFICATION_TOOL_NAME = "submit_recover_verification"


@tool
def submit_recover_verification(
    overall: str,
    layer2_status: str,
    layer2_details: str = "",
    baseline_used: bool = False,
    checklist: list = None,
    warnings: list = None,
) -> str:
    """Recover verifier ONLY. Submit the FINAL recovery verdict and end verification.

    Call this as your LAST action once you have verified (via kubectl) the
    CURRENT post-recovery state. Do NOT also emit a free-text
    RECOVERY_VERIFICATION_RESULT — this structured call IS the verdict.
    Cleanup of debug pods is handled automatically.

    Inputs:
      - overall: "recovered" | "partial" | "unrecovered"
          recovered = fault effect fully removed; partial = mostly removed;
          unrecovered = fault effect still present.
      - layer2_status: "passed" | "failed" | "partial" | "skipped"
          (passed = recovery confirmed; failed = fault still active).
      - layer2_details: one-line evidence summary.
      - baseline_used: true if you compared against the pre-injection baseline.
      - checklist: list of {"step": int, "status":
          "passed|failed|skipped|partial", "evidence": str}, one per recovery
          verification step.
      - warnings: optional list of warning strings.

    Output: confirmation string (the verdict is taken from these args).
    """
    return "Recovery verdict recorded."
