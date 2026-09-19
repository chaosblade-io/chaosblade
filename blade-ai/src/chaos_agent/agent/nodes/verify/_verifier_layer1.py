"""Layer 1 state orchestration for verifier.

Extracted from verifier.py — kept in the nodes layer because the
orchestration consumes node-level facilities (provider registry resolution,
AgentState caching). The Layer-1 EXECUTION domain (blade_status /
blade_query_k8s parsing, the kubectl-exec and host-blade runners, and their
private types/constants) physically lives in
``providers/chaosblade/verify.py`` since phase-4 T4; the transitional
aliases this module used to re-export were retired with phase-5.

Symbols (owned here — state orchestration):
  Functions: run_layer1_for_state, _restore_layer1_from_state
"""

from chaos_agent.agent.state import AgentState
from chaos_agent.agent.result.verdict import ExperimentEvidence, Layer1Result

import logging

logger = logging.getLogger(__name__)


def live_anchor_uid(state: dict, dispatch_uid: str) -> str:
    """Anchor-selection seam — the single source every verdict-side renderer shares.

    Round-29 K2's root fix: the r28 plural poll chose the live anchor
    INSIDE ``run_layer1_for_state`` while the finalize render seam
    (:mod:`_verifier_finalize`) kept reading the dispatch identity's
    never-cleared slot — the seventh private copy of the anchor
    decision, rendering the corpse into the contract field while the
    sibling ran. Both faces now ask this seam.

    Semantics (the r28 legislation, verbatim): a dispatch uid that is
    live (or the ledger-less/empty-live legacy shape) stays the anchor;
    a dead anchor hands the role to the first surviving liability. The
    caller's identity-first attribution is untouched — this seam only
    decides which uid the VERDICT speaks for.
    """
    if not dispatch_uid:
        return ""
    try:
        from chaos_agent.agent.state import live_liability_uids

        live = [u for u in live_liability_uids(state) if u]
    except Exception:
        return dispatch_uid
    if not live or dispatch_uid in live:
        return dispatch_uid
    return live[0]


async def run_layer1_for_state(
    state: dict, experiment_uid: str, kubeconfig: str, *, task_id: str = "",
) -> Layer1Result:
    """Dispatch Layer-1 verification through the resolved FaultProvider seam.

    Resolves the execution backend through the registry's four-level fault
    identity dispatch — the SAME resolution both verifier entries and the
    recover chain use, so every re-dispatch on the same state agrees on the
    same provider (phase-4 T5). UID-bearing claims route to the experiment
    carrier (its host-blade helper decides poll / warning / skipped from
    ``experiment_uid`` + message history and never polls with an empty UID); an
    evidence-less state routes to the UID-less native carrier, whose Layer-1
    verdict is ``skipped`` (the fault effect is checked in Layer 2) —
    mirroring the recover chain's dispatch semantics.

    ``experiment_uid`` is passed explicitly because the caller resolves it from
    the dispatch identity (which may differ from ``state['experiment_uid']``);
    all other inputs (messages, injection_method, injection_pod_name) come
    from ``state``.

    Round-28 K3 — composite-born tasks can hold MULTIPLE live experiments
    while the dispatch anchor is last-write-wins: with ``create A && create
    B`` then ``destroy A``, the anchor slot still names the DEAD A while B
    keeps running, and polling the dead anchor returned blade_status's
    "not found" FAILED as the TASK verdict — a terminal bypass over a task
    whose live liability (B) was never polled once. The plural poll: the
    liability oracle (:func:`live_liability_uids` — the same set the sweep
    and the destroy whitelist consume) names the anchor whenever it is
    still live (single-experiment mainline: the set is the anchor alone,
    one poll, byte-identical); a DEAD anchor hands the anchor role to the
    first surviving experiment (:func:`live_anchor_uid`, the seam the
    finalize renderer shares too — round-29 K2's seventh-copy closure).

    Round-29 root fix — the evidence is now STRUCTURED, not appended:
    every polled experiment (anchor + siblings) lands one
    :class:`ExperimentEvidence` entry in ``result.experiments``; the
    anchor fields stay the anchor's machine verdict alone (the r28
    string-append into ``details``/``raw_output`` is RETIRED — it was
    the single-value pipeline compressing plural evidence back into a
    string, which the Layer-2 window starved, the session renderer
    mis-delivered and the exception path swallowed). A FAILED sibling
    poll becomes an honest ``error`` entry — the same honesty standard
    the anchor always had — instead of a swallowed exception.
    """
    from chaos_agent.agent.providers import FaultProviderRegistry

    provider, _identity = FaultProviderRegistry.resolve_fault_dispatch(state)

    anchor_uid = ""
    siblings: list[str] = []
    if experiment_uid:
        anchor_uid = live_anchor_uid(state, experiment_uid)
        try:
            from chaos_agent.agent.state import live_liability_uids

            live = [u for u in live_liability_uids(state) if u]
        except Exception:
            live = []
        if live:
            siblings = [u for u in live if u != anchor_uid]
        if siblings:
            logger.info(
                "layer1 plural poll: anchor=%s siblings=%s",
                anchor_uid, siblings,
            )

    result = await provider.layer1_verify(
        state, experiment_uid=anchor_uid, kubeconfig=kubeconfig, task_id=task_id,
    )
    if not anchor_uid:
        # UID-less dispatch (native carrier / empty identity): the
        # skipped-or-warning verdict has no experiment evidence to
        # structure — the pre-round-29 result verbatim.
        return result

    evidence = [
        ExperimentEvidence(
            uid=anchor_uid,
            status=result.status,
            details=result.details,
            raw_output=result.raw_output,
            is_anchor=True,
        )
    ]
    for uid in siblings:
        try:
            sibling = await provider.layer1_verify(
                state, experiment_uid=uid, kubeconfig=kubeconfig, task_id=task_id,
            )
        except Exception as exc:
            logger.warning("layer1 sibling poll uid=%s failed: %s", uid, exc)
            evidence.append(ExperimentEvidence(
                uid=uid,
                status="error",
                details=f"sibling poll failed: {exc}",
                is_anchor=False,
            ))
            continue
        evidence.append(ExperimentEvidence(
            uid=uid,
            status=sibling.status,
            details=sibling.details or "",
            raw_output=sibling.raw_output or "",
            is_anchor=False,
        ))
    result.experiments = evidence
    return result


# ---------------------------------------------------------------------------
# Refactor 5: 提取 Layer 1 结果从 state 恢复的逻辑
# 原因: 后续迭代需要复用第一轮的 Layer 1 结果，但之前的实现用
#        f-string 拼凑丢失了 raw_output，LLM 看不到完整上下文
# 做法: 将 raw_output 也存入 verification dict，恢复时完整重建
# ---------------------------------------------------------------------------

def _restore_layer1_from_state(state: AgentState) -> Layer1Result:
    """Restore Layer 1 result from a previous iteration's cache."""
    cache = state.get("inject_layer1_cache") or {}
    return Layer1Result.model_validate(cache) if cache else Layer1Result()
