"""ChaosBlade deterministic recovery execution domain.

Physically owned by the provider layer (phase-3 T2): the blade_destroy +
blade_status execution and their output parsers are this carrier's own
knowledge — the generic recover nodes consume them only through the
``FaultProvider.layer1_destroy`` protocol hook. The transitional re-exports
``nodes/recover/_recover_layer1.py`` used to keep were retired with phase-5.

Symbols:
  Constants: DESTROYED_STATES
  Functions: parse_blade_destroy_output, parse_blade_status_destroyed,
             raw_destroy
  Async:     run_layer1_destroy

``recover_layer1_to_dict`` moved to ``result/verdict.py`` as
``layer1_to_dict`` (phase-7 T1 unified address — no re-export kept).
"""

import json
import logging

from .verify import (
    was_blade_create_attempted as _was_blade_create_attempted,
)
from chaos_agent.agent.result.verdict import Layer1Result

logger = logging.getLogger(__name__)

DESTROYED_STATES = frozenset({"Destroyed", "destroyed"})


# ---------------------------------------------------------------------------
# blade_destroy result parsing
# ---------------------------------------------------------------------------

def parse_blade_destroy_output(raw: str) -> tuple[str, str]:
    """Parse blade_destroy JSON output into (status, details)."""
    if not raw or raw.startswith("Error"):
        return "failed", f"blade_destroy returned error: {raw[:200]}"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        if "success" in raw.lower() or "destroy" in raw.lower():
            return "passed", "blade_destroy completed (non-JSON output)"
        return "failed", raw[:200]

    if data.get("success") or data.get("code") == 200:
        return "passed", "blade_destroy: success"
    return "failed", f"blade_destroy failed: {data.get('error', raw[:200])}"


# ---------------------------------------------------------------------------
# blade_status check for Destroyed state
# ---------------------------------------------------------------------------

def parse_blade_status_destroyed(raw: str) -> tuple[str, str]:
    """Check blade_status output confirms experiment is Destroyed."""
    if not raw or not raw.strip():
        return "passed", "blade_status: empty response (experiment not found)"

    # Quick check for NotFound before JSON parsing — kubewiz mode returns
    # error JSON with "not found" when CRD is already deleted.
    if "not found" in raw.lower() or "notfound" in raw.lower():
        return "passed", "blade_status: experiment CRD not found (already destroyed)"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        if any(s in raw for s in DESTROYED_STATES):
            return "passed", "blade_status confirms: Destroyed"
        return "unknown", "Could not parse blade_status output"

    if not (data.get("success") or data.get("code") == 200):
        if data.get("code") == 406:
            return "passed", "blade_status: experiment data not found (already destroyed)"
        # kubewiz mode: blade query k8s returns code 63061 with NotFound error
        error_msg = data.get("error", "")
        if "not found" in error_msg.lower() or "notfound" in error_msg.lower():
            return "passed", "blade_status: experiment CRD not found (already destroyed)"
        return "failed", f"blade_status check failed: {raw[:200]}"

    res = data.get("result", {})
    if not isinstance(res, dict):
        return "passed", "blade_status: Destroyed"

    exp_status = res.get("Status", res.get("status", "")) or res.get("phase", "")
    if exp_status in DESTROYED_STATES:
        return "passed", "blade_status confirms: Destroyed"
    if exp_status in ("Running", "running"):
        return "failed", "blade_status: experiment still Running (destroy may have failed)"
    return "failed", f"blade_status: unexpected status '{exp_status}'"


# ---------------------------------------------------------------------------
# Bare destroy — the Layer-2 still-active retry path (no status verification)
# ---------------------------------------------------------------------------

async def raw_destroy(
    experiment_uid: str, kubeconfig: str, *, state: dict | None = None,
) -> str:
    """Single bare ``blade_destroy`` invocation returning its raw output.

    Used by the recover finalize retry (Layer 2 saw the fault still active):
    unlike :func:`run_layer1_destroy` this runs NO ``blade_status``
    verification — the retry prompt only needs the destroy output."""
    from chaos_agent.agent.providers.chaosblade.cli import blade_destroy

    output = await blade_destroy.ainvoke({"uid": experiment_uid, "kubeconfig": kubeconfig, "state": state})
    return output if isinstance(output, str) else str(output)


# ---------------------------------------------------------------------------
# Layer 1: Execute blade_destroy + verify destroyed
# ---------------------------------------------------------------------------

async def run_layer1_destroy(
    experiment_uid: str, kubeconfig: str, *, messages: list | None = None,
    injection_method: str | None = None,
    state: dict | None = None,
) -> Layer1Result:
    """Execute blade_destroy and verify the experiment is destroyed.

    Step 1: Call blade_destroy
    Step 2: Call blade_status to confirm Destroyed state

    ``injection_method`` is the durable attribution record from state; see
    :func:`chaos_agent.agent.providers.chaosblade.verify.was_blade_create_attempted`
    for why it takes priority over the (possibly compacted) message history.
    """
    if not experiment_uid:
        # Distinguish two scenarios when experiment_uid is empty during recovery:
        # 1. ChaosBlade injection was done but UID unavailable → "failed" (terminal)
        # 2. Non-ChaosBlade fault (kubectl-based) → "skipped" (not terminal, Layer 2 proceeds)
        if messages and _was_blade_create_attempted(messages, injection_method):
            return Layer1Result(
                status="failed",
                details="blade_create was called during injection but no UID available for recovery",
            )
        return Layer1Result(
            status="skipped",
            details="Non-ChaosBlade fault (no experiment_uid), Layer 1 recovery not applicable",
        )

    try:
        from chaos_agent.agent.providers.chaosblade.cli import blade_destroy, blade_status

        # Step 1: Execute blade_destroy
        destroy_output = await blade_destroy.ainvoke(
            {"uid": experiment_uid, "kubeconfig": kubeconfig, "state": state}
        )
        destroy_raw = destroy_output if isinstance(destroy_output, str) else str(destroy_output)
        destroy_status, destroy_details = parse_blade_destroy_output(destroy_raw)

        if destroy_status == "failed":
            # Fallback: destroy failed (e.g. kubewiz guard blocked the delete),
            # but the experiment may already be gone (timeout auto-expiry).
            # Check via blade_status — if CRD is not found, treat as recovered.
            try:
                status_output = await blade_status.ainvoke(
                    {"uid": experiment_uid, "kubeconfig": kubeconfig, "state": state}
                )
                status_raw = status_output if isinstance(status_output, str) else str(status_output)
                if "not found" in status_raw.lower() or "notfound" in status_raw.lower():
                    return Layer1Result(
                        status="passed",
                        details="blade_destroy failed but experiment CRD already removed (timeout auto-expiry)",
                        raw_output=f"destroy: {destroy_raw}\nstatus: {status_raw}",
                    )
                check_status, check_details = parse_blade_status_destroyed(status_raw)
                if check_status == "passed":
                    return Layer1Result(
                        status="passed",
                        details=f"blade_destroy failed but {check_details}",
                        raw_output=f"destroy: {destroy_raw}\nstatus: {status_raw}",
                    )
            except Exception as fallback_err:
                logger.debug(f"destroy-failed fallback status check also failed: {fallback_err}")
            return Layer1Result(
                status="failed",
                details=destroy_details,
                raw_output=destroy_raw,
            )

        # Step 2: Verify via blade_status that experiment is Destroyed
        try:
            status_output = await blade_status.ainvoke(
                {"uid": experiment_uid, "kubeconfig": kubeconfig, "state": state}
            )
            status_raw = status_output if isinstance(status_output, str) else str(status_output)
            check_status, check_details = parse_blade_status_destroyed(status_raw)

            if check_status == "failed":
                return Layer1Result(
                    status="failed",
                    details=f"{destroy_details}, but {check_details}",
                    raw_output=f"destroy: {destroy_raw}\nstatus: {status_raw}",
                )

            combined_details = f"{destroy_details}, {check_details}"
            return Layer1Result(
                status="passed",
                details=combined_details,
                raw_output=f"destroy: {destroy_raw}\nstatus: {status_raw}",
            )
        except Exception as se:
            logger.debug(f"blade_status check failed (non-critical): {se}")
            return Layer1Result(
                status="passed",
                details=f"{destroy_details} (status check unavailable)",
                raw_output=destroy_raw,
            )

    except Exception as e:
        logger.error(f"Recover Layer 1 failed: {e}")
        return Layer1Result(status="error", details=str(e), raw_output=str(e))
