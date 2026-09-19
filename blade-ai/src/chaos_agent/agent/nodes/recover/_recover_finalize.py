"""finalize_recover_verification node (Scheme B, recover side).

Mirrors finalize_verification for the recover graph. recover_verifier_loop's
Layer 2 is now a pure ReAct step: it gathers post-recovery evidence and, when
done, calls submit_recover_verification (or, as a fallback, emits a
RECOVERY_VERIFICATION_RESULT text). That terminal signal routes here, where
all Layer 2 finalization lives:

  1. Source the verdict — submit_recover_verification args (preferred) or
     parse the last AIMessage's text.
  2. Anti-laziness guard — if no current-state verification ran in Layer 2, reject
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

from chaos_agent.agent.prompts.reminder import wrap_system_reminder
from chaos_agent.agent.result.operation_outcome import write_recover_verification
from chaos_agent.agent.nodes.execute._kubeconfig_inject import _resolve_kubeconfig, sync_kubewiz_runtime
from chaos_agent.agent.nodes.recover._recover_layer1 import (
    RecoverLayer1Result,
)
# Phase-7 T1: canonical address — the storage-shape helper lives beside the
# Layer1Result data class in result/verdict.py.
from chaos_agent.agent.result.verdict import layer1_to_dict
from chaos_agent.agent.nodes.recover._recover_layer2_parse import _parse_recovery_verification_result
from chaos_agent.agent.nodes.store._store_sync import sync_to_store
from chaos_agent.agent.nodes.verify._verifier_finalize import _cleanup_debug_pods
from chaos_agent.agent.nodes.verify._verifier_shared import (
    _compute_baseline_confidence,
    extract_submit_args,
    last_ai_text,
)
from chaos_agent.agent.nodes.verify._verifier_submit import SUBMIT_RECOVER_VERIFICATION_TOOL_NAME
from chaos_agent.agent.spec.skill_identity import read_active_skill_name
from chaos_agent.agent.state import AgentState, recovery_task_state_from_level
from chaos_agent.agent.state_mgmt.state_helpers import fail_state
from chaos_agent.agent.result.verdict import (
    CHECKLIST_STATUS_VALUES,
    FailureCategory,
    LAYER2_STATUS_VALUES,
    RECOVER_SUCCESS_VALUES,
    RECOVER_VERDICT_VALUES,
    RESIDUAL_ATTRIBUTION_VALUES,
    RecoverVerdict,
    ResidualAttribution,
    WarningCode,
)
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
    raw_l2_status = args.get("layer2_status", "unknown")
    overall = args.get("overall", "unrecovered")
    # "unverified" = recovery unconfirmed (observation channel unavailable) —
    # distinct from "unrecovered" (counter-evidence: fault still present).
    # Both closed sets derive from the legislation enums (B76 round-14
    # root-cause fix): the clamp accepts exactly RecoverVerdict's members,
    # anything else falls back fail-closed — no hand-copied word list here.
    if overall not in RECOVER_VERDICT_VALUES:
        overall = "unrecovered"
    if raw_l2_status not in LAYER2_STATUS_VALUES:
        l2_status = "unknown"
    else:
        l2_status = raw_l2_status
    result = {
        "level": overall,
        "layer1": {"status": "unknown", "details": ""},  # overwritten by code later
        "layer2": {"status": l2_status, "details": args.get("layer2_details", "")},
        "warnings": list(args.get("warnings") or []),
        "baseline_used": bool(args.get("baseline_used", False)),
    }
    if l2_status != raw_l2_status:
        result["warnings"].append(
            f"Layer2 status '{raw_l2_status}' is outside the closed vocabulary; "
            "recorded as 'unknown'."
        )
    if checklist:
        result["checklist"] = {
            "items": checklist,
            "total_count": len(checklist),
            "total_executed": len(checklist),
        }
        # Closed-set violations stay visible instead of silently flowing
        # into the task JSON (items are kept verbatim for audit).
        _outside = sorted({
            c.get("status") for c in checklist
            if isinstance(c, dict) and c.get("status") not in CHECKLIST_STATUS_VALUES
        })
        if _outside:
            result["warnings"].append(
                f"Checklist item statuses outside the closed vocabulary: {_outside}."
            )
    # Attribution first — the Layer-2 attribution contract (see
    # get_recover_delay_section) decides whether a step-level 'partial'
    # aggregate may coexist with a holistic 'recovered' judgement.
    attribution = args.get("residual_attribution")
    if attribution in RESIDUAL_ATTRIBUTION_VALUES:
        result["residual_attribution"] = attribution

    # Level sync: if Layer 2 did not pass, recovery cannot be fully
    # "recovered" — EXCEPT a clean-attribution converging tail: partial
    # step-level facts (convergence still in progress) may coexist with a
    # holistic recovered judgement, per the Checklist=OBSERVED FACTS /
    # Overall=HOLISTIC JUDGMENT separation in the output contract.
    clean_tail = attribution == ResidualAttribution.RECOVERY_PROCESS.value
    if l2_status == "failed" and result["level"] == "recovered":
        result["level"] = "unrecovered"
    elif l2_status == "partial" and result["level"] == "recovered" and not clean_tail:
        result["level"] = "partial"

    # Attribution consistency guard: recovery propagation cost is NOT
    # recovery failure, but fault-attributed (or mixed) residuals contradict
    # a "recovered" verdict — downgrade and flag.
    if result["level"] == "recovered" and attribution in (
        ResidualAttribution.FAULT_RESIDUAL.value,
        ResidualAttribution.MIXED.value,
    ):
        result["level"] = "partial"
        result["warnings"].append(
            f"{WarningCode.RESIDUAL_ATTRIBUTION_CONTRADICTION.value}: verdict "
            "said recovered but residuals were attributed to the fault — "
            "downgraded to partial"
        )
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


def _sweep_exempts_identity_uid(layer1_status) -> bool:
    """Whether the final liability sweep may exempt the identity UID.

    The sweep's ``exclude_uid`` exists to prevent a duplicate destroy of
    the experiment the main Layer-1 flow already owns — a valid exemption
    ONLY while that ownership paid out: ``passed`` (the deterministic
    destroy succeeded, or its not-found fallback verified the death) or
    ``skipped`` (no deterministic destroy applies). Every other status
    (``failed`` / ``error`` / ``unknown`` / ``in_progress``) means the main
    flow did NOT prove the destroy — exempting anyway would orphan the very
    experiment the net exists to catch (B76 review M2, the exempt orphan:
    destroy failed, Layer 2 passed, task closed "recovered" with the
    experiment alive and unwarned four ways). Fail-closed by construction:
    only a proving status exempts.

    Accepts the status as a ``Layer1Status`` enum OR its plain-string
    value. Historical note: under the pre-B76-round-13 ``(str, Enum)``
    base, ``str()`` on a member yielded ``"Layer1Status.PASSED"`` — the
    ``.value`` normalization below was the workaround; the verdict enums
    are now ``StrEnum`` so both arms render "passed", but the explicit
    normalization stays (fail-closed against either input shape, and
    against any future carrier re-introducing a raw-enum path).
    """
    status = str(getattr(layer1_status, "value", layer1_status) or "")
    return status in ("passed", "skipped")


def make_finalize_recover_verification(registry=None):
    """Build the finalize_recover_verification node."""

    async def finalize_recover_verification(state: AgentState) -> dict:
        task_id = state.get("task_id", "")
        skill_name = read_active_skill_name(state)
        # Experiment UID via the carrier-agnostic identity (mirrors the
        # recover entries): dispatch identity first (combo-safe — for a combo
        # task the EXPERIMENT handle outranks the native attribution the
        # plain materialization would return), materialized attribution as
        # fallback.
        from chaos_agent.agent.nodes.recover._recover_verifier_loop import (
            _deterministic_recover_identity,
            _experiment_uid_of,
            _provider_for_recover,
            _resolve_recover_dispatch,
        )
        from chaos_agent.agent.state import materialize_fault_handle

        _, _identity_fr = _resolve_recover_dispatch(state)
        experiment_uid = _experiment_uid_of(_identity_fr) or _experiment_uid_of(
            materialize_fault_handle(state)
        )
        kubeconfig = _resolve_kubeconfig(state)
        sync_kubewiz_runtime(state)
        count = state.get("verifier_loop_count", 0)
        messages = state.get("messages", [])

        tracker = get_tracker(task_id)
        tracker.start(
            StatusCategory.NODE,
            "finalize_recover_verification",
            "Finalizing recovery verdict",
            {"experiment_uid": experiment_uid},
        )

        # Restore Layer 1 from cache.
        cache = state.get("recover_layer1_cache") or {}
        layer1 = RecoverLayer1Result(
            status=cache.get("status", "unknown"),
            details=cache.get("details", ""),
            raw_output=cache.get("raw_output", ""),
        )

        # Defense-in-depth: if the cache is still ``in_progress`` (should
        # not happen — Layer 1 filters out submit_recover_verification so
        # the LLM must output text to complete Layer 1), default to
        # ``unknown`` rather than crashing.
        if layer1.is_in_progress():
            logger.warning(
                "finalize_recover_verification: Layer 1 cache is in_progress "
                "for task %s — this should not happen (submit_recover_verification "
                "is filtered during Layer 1). Defaulting to unknown.",
                task_id,
            )
            layer1 = RecoverLayer1Result(
                status="unknown",
                details="Layer 1 was in progress when finalize was triggered",
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
        # it skipped obtaining any current-state verification evidence. Reject
        # once and loop back. Mirrors the original is_first_layer2 guard; the loop
        # clears the flag on re-entry (layer2_context_added is then True), so this
        # fires at most once.
        if state.get("recover_layer2_first"):
            logger.warning(
                "Recover Layer 2 concluded on the first turn without current-state "
                "verification for task %s. Forcing re-verification.", task_id,
            )
            tracker.update(
                "Layer 2 conclusion without verification commands — forcing re-check",
                {"guard": "no_verification_commands"},
            )
            result_update["messages"] = [HumanMessage(content=wrap_system_reminder(
                f"⚠️ {_GUARD_MARKER}: Your recovery verdict was rejected because you did NOT "
                "execute a bound observation tool to observe the CURRENT post-recovery "
                "state. Baseline / injection-phase data is NOT current.\n\n"
                "Use the currently bound diagnostic tools to observe the CURRENT state, and only "
                "THEN call submit_recover_verification."
            ))]
            await sync_to_store(state, result_update)
            return result_update

        verification["layer1"] = layer1_to_dict(layer1)

        # ---- Residual liability sweep (B76 review G, safety net) ----
        # Destroy every live experiment this task still owes a destroy for,
        # EXCEPT the identity UID — and only while the main Layer-1 flow
        # (and the retry below) still OWNS that destroy: a passed verdict
        # proves the destroy happened (or its not-found fallback verified
        # the death); skipped means no deterministic destroy applies. A
        # failed/error/unknown Layer-1 revokes the exemption (B76 review M2,
        # the exempt orphan): the main flow's destroy did NOT succeed, and an
        # unconditional exemption would let the net skip the one experiment
        # it exists to catch. Structurally un-bypassable net: whichever seam
        # let a superseded experiment survive (approval-time destroy
        # failure, an execute-replan build-on-top, compaction blinding the
        # destroy whitelist), THIS is the last point where the framework
        # still holds the full ownership record. Idempotent across finalize
        # passes — retired UIDs leave the live set — and a no-op for the
        # normal single-experiment task (residuals are empty). Runs BEFORE
        # the retry-recovery check so a retry's re-verification round
        # observes a clean cluster.
        from chaos_agent.agent.providers import FaultProviderRegistry

        _retired_sweep, _sweep_failures = (
            await FaultProviderRegistry.sweep_live_liabilities(
                state,
                exclude_uid=(
                    experiment_uid
                    if _sweep_exempts_identity_uid(layer1.status)
                    else ""
                ),
            )
        )
        if _retired_sweep:
            # write_recover_verification dict-copies result_update, so the
            # retire record survives both the retry loop-back (early return)
            # and the final verdict path.
            result_update["retired_experiment_uids"] = (
                list(state.get("retired_experiment_uids") or []) + _retired_sweep
            )
            logger.info(
                "finalize_recover_verification: residual liability sweep "
                "destroyed %s", _retired_sweep,
            )
            tracker.update(
                "Residual experiment sweep: destroyed "
                f"{', '.join(_retired_sweep)}",
                {"residual_sweep_destroyed": _retired_sweep},
            )
        if _sweep_failures:
            verification.setdefault("warnings", []).append(
                "residual experiment(s) survived the final destroy sweep "
                f"({' | '.join(_sweep_failures)}) — they remain in the "
                "task's liability record; re-run recover or destroy them "
                "manually."
            )

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
            # Same seam-compat inference as the verifier loop's Layer-2
            # entry (D1 / M2 task 2.3): a UID-less deterministic handle
            # (the CR-reference kind) types as "deterministic" too, or
            # legacy checkpoints would mislabel it "llm_driven". The
            # bare-destroy retry below stays UID-only — the CR carrier
            # has no bare destroy form (its retry re-enters the loop and
            # re-runs the provider convergence, which is idempotent).
            _rl1_type = (
                "deterministic"
                if (
                    experiment_uid
                    or _deterministic_recover_identity(state)
                )
                and _provider_for_recover(state).has_deterministic_recover
                else "llm_driven"
            )
        _layer1_is_deterministic = _rl1_type == "deterministic"
        l2_status = verification.get("layer2", {}).get("status", "unknown")
        already_retried = _phrase_in_messages(messages, _RETRY_MARKER)
        if l2_status == "failed" and not already_retried and count < settings.max_recover_verifier_loop - 1:
            if experiment_uid and _layer1_is_deterministic:
                logger.warning(
                    "Recover Layer 2 detected fault still active for task %s, retrying the deterministic destroy (uid=%s)",
                    task_id, experiment_uid,
                )
                tracker.update("Fault still active, retrying the deterministic destroy", {"retry": True, "experiment_uid": experiment_uid})
                try:
                    # Bare retry destroy through the dispatched carrier's
                    # execution domain (no status verification — the prompt
                    # only needs the destroy output).
                    retry_raw = await _provider_for_recover(state).layer1_raw_destroy(
                        experiment_uid, kubeconfig,
                    )
                    result_update["messages"] = [HumanMessage(content=wrap_system_reminder(
                        f"**{_RETRY_MARKER} executed**\n"
                        f"layer-1 destroy output: {retry_raw[:500]}\n\n"
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
                result_update["messages"] = [HumanMessage(content=wrap_system_reminder(
                    f"**{_RETRY_MARKER} required**: The fault effect is STILL PRESENT.\n"
                    "Re-attempt recovery using an alternative currently bound method when the "
                    "evidence supports it. After re-attempting, verify again "
                    "and call submit_recover_verification."
                ))]
                await sync_to_store(state, result_update)
                return result_update

        # ---- Finalize ----
        result = {
            "task_id": task_id,
            "skill": skill_name,
            # External contract key (L4/Web/DB) — permanent compatibility.
            "experiment_uid": experiment_uid,
            "recovered": verification["level"] in RECOVER_SUCCESS_VALUES,
            "recovery_level": verification["level"],
        }

        # Round-32 — a FULL recovery verdict completes the row-level ledger's
        # death wing: the main UID's destroy proof lives in the in-memory
        # ToolMessage history (never persisted), so without this retire the
        # persisted ``owned − retired`` would keep naming it and
        # ``may_carry_live_fault`` would keep the recovered row recoverable
        # forever (a recovered task haunting query_active — the inverse of
        # the K1 blindness, same root: DB-side death evidence was incomplete).
        # DELIBERATELY full-recovery-only: a ``partial`` verdict means at
        # least one experiment may survive — retiring everything would erase
        # exactly the liability the partial verdict still names. And even
        # under a full verdict, the sweep's FAILED residuals keep theirs:
        # a destroy error is LIVE evidence (the warning above says they
        # "remain in the task's liability record") — retiring a proven-live
        # UID here would be the false settle the sweep contract exists to
        # forbid. The verdict settles what it can prove; it never overrides
        # a destroy failure with a recovery claim.
        if verification["level"] == RecoverVerdict.RECOVERED.value:
            _sweep_failed_uids = {
                line.split(":", 1)[0].strip() for line in _sweep_failures
            }
            _owned_proven = [
                uid for uid in (state.get("owned_experiment_uids") or [])
                if uid not in _sweep_failed_uids
            ]
            if _owned_proven:
                result_update["retired_experiment_uids"] = sorted(
                    set(list(state.get("retired_experiment_uids") or [])
                        + _owned_proven)
                )

        result_update = write_recover_verification(
            result_update,
            result=result,
            verification=verification,
            finished_at=now_iso(),
        )

        # fail_state writes a RECOVERY_FAILED diagnostic signal — that directs
        # operators to debug the recovery chain. "unverified" is not a recovery
        # failure: the destroy may well have succeeded, the observation channel
        # was unavailable. The right follow-up is to restore observability and
        # re-confirm, so no failure_reason is recorded (recovered stays False —
        # we never claim recovery without evidence).
        if not result["recovered"] and verification["level"] != "unverified":
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

        # ---- Mark the ORIGINAL inject task as recovered in TaskStore ----
        # The recover flow operates under a new task_id (the recover task).
        # The original inject task is referenced via ``recover_task_id``.
        # Without this update, ``query_active_experiments`` would keep
        # returning the original task because its ``task_state`` is still
        # ``injected``.
        #
        # We directly set ``task_state`` without going through ``upsert``
        # (which would infer state and overwrite ``operation`` / ``result``).
        inject_task_id = state.get("recover_task_id", "")
        if inject_task_id and inject_task_id != task_id:
            # Four states: recovered / partial_recovered / unverified / failed.
            # "unverified" keeps the original task queryable for re-confirmation
            # instead of masquerading as either a recovered or a failed drill.
            # Single-source mapping (round-15 D3): this ternary was the third
            # parallel copy of the recover verdict → task_state truth table.
            inject_state = recovery_task_state_from_level(
                verification["level"],
                recovered=result["recovered"],
                layer1_status=layer1.status,
            )
            try:
                from chaos_agent.persistence.task_store import get_task_store
                _store = await get_task_store()
                # Round-33b single-source: propagate the clearance verdict
                # onto the inject row TOGETHER with the CLEARED word. The
                # verdict above was synced to THIS (recover) row only; a
                # bare word on the inject row left it a CLEARED state with
                # no row-local proof — a permanent "completed-but-
                # uncleared" ghost under the fail-closed predicate.
                await _store.update_task_state(
                    inject_task_id,
                    inject_state,
                    recover_verification=verification,
                )
                logger.info(
                    "finalize_recover_verification: marked original inject task %s "
                    "as %s (recover task %s)",
                    inject_task_id, inject_state, task_id,
                )
            except Exception:
                logger.exception(
                    "finalize_recover_verification: failed to mark original "
                    "inject task %s as recovered", inject_task_id,
                )

        return result_update

    return finalize_recover_verification
