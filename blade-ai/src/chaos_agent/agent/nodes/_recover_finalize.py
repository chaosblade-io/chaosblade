"""finalize_recover_verification node (Scheme B, recover side).

Mirrors finalize_verification for the recover graph. recover_verifier_loop's
Layer 2 is now a pure ReAct step: it gathers post-recovery evidence and, when
done, calls submit_recover_verification (or, as a fallback, emits a
RECOVERY_VERIFICATION_RESULT text). That terminal signal routes here, where
all Layer 2 finalization lives:

  1. Source the verdict — submit_recover_verification args (preferred) or
     parse the last AIMessage's text.
  2. Anti-laziness guard — if NO kubectl verification ran in Layer 2, reject
     and loop back (once) for a real CURRENT-state check.
  3. Baseline-confidence enforcement.
  4. Retry-recovery — if Layer 2 says the fault is STILL active, retry once
     (blade_destroy for ChaosBlade, or a retry prompt for non-ChaosBlade)
     and loop back.
  5. Otherwise set recover_verification → route_after_recover_finalize → END.
  6. Programmatic debug-pod cleanup (moved here from recover_verifier_loop).

route_after_recover_finalize keys on recover_verification being set: present →
END, absent (guard/retry loop-back) → recover_verifier_loop.
"""

import logging

from langchain_core.messages import HumanMessage

from chaos_agent.agent.operation_outcome import write_recover_verification
from chaos_agent.agent.nodes._kubeconfig_inject import _resolve_kubeconfig, sync_kubewiz_runtime
from chaos_agent.agent.nodes._recover_layer1 import (
    RecoverLayer1Result,
    _recover_layer1_to_dict,
)
from chaos_agent.agent.nodes._recover_layer2_parse import _parse_recovery_verification_result
from chaos_agent.agent.nodes._store_sync import sync_to_store
from chaos_agent.agent.nodes._verifier_finalize import _cleanup_debug_pods
from chaos_agent.agent.nodes._verifier_shared import (
    _compute_baseline_confidence,
    extract_submit_args,
    last_ai_text,
)
from chaos_agent.agent.nodes._verifier_submit import SUBMIT_RECOVER_VERIFICATION_TOOL_NAME
from chaos_agent.agent.skill_identity import read_active_skill_name
from chaos_agent.agent.state import AgentState
from chaos_agent.agent.state_helpers import fail_state
from chaos_agent.agent.verdict import FailureCategory
from chaos_agent.config.settings import settings
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)

# Marker phrase used to detect (and avoid re-injecting) the anti-laziness guard.
_GUARD_MARKER = "RECOVERY VERIFICATION GUARD"
# Marker phrase the retry-recovery loop-back uses (mirrors the original).
_RETRY_MARKER = "recovery retry"


def _recover_verification_from_submit_args(args: dict, skill_name: str = "") -> dict:
    """Build a recover verification dict from submit_recover_verification args.

    Produces the SAME shape ``_parse_recovery_verification_result`` yields so
    downstream finalize logic is source-agnostic.
    """
    checklist = args.get("checklist") or []
    if not isinstance(checklist, list):
        checklist = []
    l2_status = args.get("layer2_status", "unknown")
    overall = args.get("overall", "unrecovered")
    if overall not in ("recovered", "partial", "unrecovered"):
        overall = "unrecovered"
    result = {
        "level": overall,
        "layer1": {"status": "unknown", "details": ""},  # overwritten by code later
        "layer2": {"status": l2_status, "details": args.get("layer2_details", "")},
        "warnings": list(args.get("warnings") or []),
        "baseline_used": bool(args.get("baseline_used", False)),
    }
    if checklist:
        result["checklist"] = {
            "items": checklist,
            "total_count": len(checklist),
            "total_executed": len(checklist),
        }
    # Level sync: if Layer 2 not passed, recovery cannot be fully "recovered".
    if l2_status == "failed" and result["level"] == "recovered":
        result["level"] = "unrecovered"
    elif l2_status == "partial" and result["level"] == "recovered":
        result["level"] = "partial"
    return result


def _extract_recover_submit_args(messages: list) -> dict | None:
    """Return args of the most recent submit_recover_verification tool_call, or None."""
    return extract_submit_args(
        messages,
        tool_name=SUBMIT_RECOVER_VERIFICATION_TOOL_NAME,
        guard_markers=(_GUARD_MARKER, _RETRY_MARKER),
    )


_last_ai_text = last_ai_text


def _phrase_in_messages(messages: list, phrase: str) -> bool:
    return any(
        isinstance(m, HumanMessage) and phrase in (getattr(m, "content", "") or "")
        for m in messages
    )


def make_finalize_recover_verification(registry=None):
    """Build the finalize_recover_verification node."""

    async def finalize_recover_verification(state: AgentState) -> dict:
        task_id = state.get("task_id", "")
        skill_name = read_active_skill_name(state)
        blade_uid = state.get("blade_uid", "")
        kubeconfig = _resolve_kubeconfig(state)
        sync_kubewiz_runtime(state)
        count = state.get("verifier_loop_count", 0)
        messages = state.get("messages", [])

        tracker = get_tracker(task_id)
        tracker.start(
            StatusCategory.NODE,
            "finalize_recover_verification",
            "Finalizing recovery verdict",
            {"blade_uid": blade_uid},
        )

        # Restore Layer 1 from cache.
        cache = state.get("recover_layer1_cache") or {}
        layer1 = RecoverLayer1Result(
            status=cache.get("status", "unknown"),
            details=cache.get("details", ""),
            raw_output=cache.get("raw_output", ""),
        )

        # ---- Source the verdict: submit args > text fallback ----
        submit_args = _extract_recover_submit_args(messages)
        if submit_args is not None:
            verification = _recover_verification_from_submit_args(submit_args, skill_name)
        else:
            content = _last_ai_text(messages)
            verification = _parse_recovery_verification_result(content, skill_name=skill_name)

        result_update: dict = {}

        # ---- Anti-laziness guard ----
        # Fire on the FIRST Layer 2 conclusion: if the LLM concluded right after
        # Layer 2 context was built (recover_layer2_first flag set by the loop),
        # it skipped running any kubectl verification of the CURRENT state. Reject
        # once and loop back. Mirrors the original is_first_layer2 guard; the loop
        # clears the flag on re-entry (layer2_context_added is then True), so this
        # fires at most once.
        if state.get("recover_layer2_first"):
            logger.warning(
                "Recover Layer 2 concluded on the first turn without executing kubectl "
                "verification for task %s. Forcing re-verification.", task_id,
            )
            tracker.update(
                "Layer 2 conclusion without verification commands — forcing re-check",
                {"guard": "no_verification_commands"},
            )
            result_update["messages"] = [HumanMessage(content=(
                f"⚠️ {_GUARD_MARKER}: Your recovery verdict was rejected because you did NOT "
                "execute any kubectl verification commands to observe the CURRENT post-recovery "
                "state. Baseline / injection-phase data is NOT current.\n\n"
                "You MUST run kubectl commands now (e.g. kubectl exec to check disk/CPU, "
                "kubectl describe to check pod status), observe the CURRENT state, and only "
                "THEN call submit_recover_verification."
            ))]
            await sync_to_store(state, result_update)
            return result_update

        verification["layer1"] = _recover_layer1_to_dict(layer1)

        # ---- Baseline confidence + enforcement ----
        if "baseline_confidence" not in verification:
            verification["baseline_confidence"] = _compute_baseline_confidence(state)
        if verification.get("baseline_confidence") == "high" and not verification.get("baseline_used"):
            verification.setdefault("warnings", []).append(
                "Pre-injection baseline was available (confidence=high) but LLM did not "
                "perform baseline comparison. Verification relies on absolute thresholds "
                "instead of more reliable before/after delta."
            )

        # ---- Retry-recovery: fault still active → retry once, loop back ----
        _rl1_type = state.get("recover_layer1_type")
        if _rl1_type is None:
            _rl1_type = "deterministic" if blade_uid else "llm_driven"
        _layer1_is_deterministic = _rl1_type == "deterministic"
        l2_status = verification.get("layer2", {}).get("status", "unknown")
        already_retried = _phrase_in_messages(messages, _RETRY_MARKER)
        if l2_status == "failed" and not already_retried and count < settings.max_recover_verifier_loop - 1:
            if blade_uid and _layer1_is_deterministic:
                logger.warning(
                    "Recover Layer 2 detected fault still active for task %s, retrying blade_destroy (uid=%s)",
                    task_id, blade_uid,
                )
                tracker.update("Fault still active, retrying blade_destroy", {"retry": True, "blade_uid": blade_uid})
                try:
                    from chaos_agent.tools.blade import blade_destroy as _blade_destroy
                    retry_output = await _blade_destroy.ainvoke({"uid": blade_uid, "kubeconfig": kubeconfig})
                    retry_raw = retry_output if isinstance(retry_output, str) else str(retry_output)
                    result_update["messages"] = [HumanMessage(content=(
                        f"**{_RETRY_MARKER} executed**\n"
                        f"blade_destroy output: {retry_raw[:500]}\n\n"
                        f"Please verify again whether the fault has been removed, then call "
                        f"submit_recover_verification."
                    ))]
                    await sync_to_store(state, result_update)
                    return result_update
                except Exception as retry_err:
                    logger.warning("blade_destroy retry failed: %s", retry_err)
            else:
                logger.warning(
                    "Recover Layer 2 detected fault still active for task %s, injecting retry prompt (non-ChaosBlade)",
                    task_id,
                )
                tracker.update("Fault still active, injecting recovery retry prompt", {"retry": True})
                result_update["messages"] = [HumanMessage(content=(
                    f"**{_RETRY_MARKER} required**: The fault effect is STILL PRESENT.\n"
                    "Re-attempt recovery using alternative methods (e.g. if kubectl patch failed, "
                    "try kubectl delete --force --grace-period=0). After re-attempting, verify again "
                    "and call submit_recover_verification."
                ))]
                await sync_to_store(state, result_update)
                return result_update

        # ---- Finalize ----
        result = {
            "task_id": task_id,
            "skill": skill_name,
            "blade_uid": blade_uid,
            "recovered": verification["level"] in ("recovered", "partial"),
            "recovery_level": verification["level"],
        }
        result_update = write_recover_verification(
            result_update,
            result=result,
            verification=verification,
            finished_at=now_iso(),
        )

        if not result["recovered"]:
            result_update.update(fail_state(
                FailureCategory.RECOVERY_FAILED,
                f"Layer1={layer1.status}, Layer2={l2_status}, level={verification['level']}",
            ))

        level = verification["level"]
        warnings = verification.get("warnings", [])
        status_msg = f"Recovery verification: {level} (Layer1: {layer1.status}, Layer2: {l2_status})"
        if warnings:
            status_msg += f" (warnings: {len(warnings)})"
        tracker.complete(status_msg)

        # Programmatic debug-pod cleanup (moved here; dedup preserved).
        await _cleanup_debug_pods(state, kubeconfig, task_id, result_update)

        await sync_to_store(state, result_update)
        return result_update

    return finalize_recover_verification
