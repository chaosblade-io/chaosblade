"""L4 adapter: TestTask ↔ AgentState conversions.

Handles inbound (TestTask → inject graph initial_state) and
outbound (graph final state → TaskResult) transformations.
"""

from __future__ import annotations

import logging
import re
import uuid

from chaos_agent.agent.intent_handoff import (
    build_pipeline_handoff_from_intent_state,
    is_previous_intent_residue,
    spec_relevance_tokens,
)
from chaos_agent.agent.state_mgmt.state_builders import build_inject_initial_state
from chaos_agent.l4.error_mapping import _extract_error
from chaos_agent.l4.schemas import L4TaskResult, L4TestTask

logger = logging.getLogger(__name__)

# Observation-failure marker vocabulary — the SAME markers the verifier
# prompt's evidence boundary uses (auth-class vs transient-class). Single
# source of truth: when the prompt vocabulary changes, change it here too.
# The status codes match on word boundaries: plain substring matching would
# let "24013ms" (transient timing noise) contain "401" and misclassify it
# as auth, steering the operator toward credentials instead of the network.
_AUTH_MARKERS = ("forbidden", "unauthorized")
_AUTH_STATUS_RE = re.compile(r"\b(?:401|403)\b")
_TRANSIENT_MARKERS = ("timeout", "timed out", "connection", "transport")


def _classify_observation_error(text: str) -> str:
    """Classify an observation-failure text as auth / transient / unknown."""
    t = (text or "").lower()
    if any(m in t for m in _AUTH_MARKERS) or _AUTH_STATUS_RE.search(t):
        return "auth"
    if any(m in t for m in _TRANSIENT_MARKERS):
        return "transient"
    return "unknown"


def _collect_observation_failures(verification: dict | None) -> list[dict] | None:
    """Aggregate the observation-failure log from a verification verdict.

    Sources: checklist items (``status == "skipped"`` marks the channel as
    unavailable; evidence text carrying error markers marks a failed probe)
    and warnings. Items are keyed by (channel, error_class) with a count —
    ``channel`` names come from the checklist's own step numbering, no new
    naming scheme is invented. Returns None when nothing failed (no
    placeholder entries).
    """
    if not isinstance(verification, dict):
        return None
    failures: dict[tuple[str, str], int] = {}

    def _record(channel: str, text: str) -> None:
        key = (channel, _classify_observation_error(text))
        failures[key] = failures.get(key, 0) + 1

    checklist = verification.get("checklist")
    items = checklist.get("items") if isinstance(checklist, dict) else checklist
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            evidence = item.get("evidence")
            evidence_text = evidence if isinstance(evidence, str) else ""
            if item.get("status") == "skipped":
                _record(f"step-{item.get('step', '?')}", evidence_text or "skipped")
            elif evidence_text and _classify_observation_error(evidence_text) != "unknown":
                _record(f"step-{item.get('step', '?')}", evidence_text)
    warnings = verification.get("warnings")
    if isinstance(warnings, list):
        for w in warnings:
            if isinstance(w, str) and _classify_observation_error(w) != "unknown":
                _record("warnings", w)
    if not failures:
        return None
    return [
        {"channel": ch, "error_class": ec, "count": n}
        for (ch, ec), n in sorted(failures.items())
    ]


def test_task_to_initial_state(task: L4TestTask) -> dict:
    """Convert L4 TestTask into inject graph initial_state dict.

    Reads fault parameters from ``payload["fault_intent"]``.
    This is produced by the platform's ``run_chaos_inject`` via
    ``FaultSpec.to_intent_dict()``.

    Fail-closed: required fields (target / action / scope / namespace) must
    be non-empty; otherwise raise ``ValueError`` so the caller (platform
    ``run_chaos_inject`` tool) returns a clear MISSING_REQUIRED_ARGS error
    instead of silently launching the inject pipeline with an empty
    fault_spec (which previously caused the agent_loop to spin in a
    "tell me what fault you want" ReAct loop until recursion_limit).
    """
    payload = task.payload or {}
    fi = payload.get("fault_intent")
    if not isinstance(fi, dict):
        raise ValueError(
            "L4 adapter: payload must include payload['fault_intent'] "
            "(dict with scope/target/action/namespace). "
            f"Got payload keys={list(payload.keys())}."
        )

    fault_target = fi.get("target", "")
    fault_action = fi.get("action", "")
    scope = fi.get("scope", "")
    namespace = fi.get("namespace", "")

    # namespace 仅对 **namespace-scoped** 故障必填。cluster-scoped（host / node /
    # python / pv / …）天然没有 namespace，无条件必填会把正常的主机故障
    # 直接 fail-closed 拦下（实测：host-cpu-fullload 报
    # "missing required field(s): ['namespace']"）。此前未暴露，是因为平台
    # 协调器 LLM 会自己瞎填 namespace="default" 去满足校验。
    # 判据复用 fault_registry 的单一真源（CLI / REST 层已在用同一个），
    # 新增无 namespace 的 scope 时无需再改这里。
    from chaos_agent.agent.spec.fault_registry import aggregate_cluster_scoped

    _required = [
        ("target", fault_target),
        ("action", fault_action),
        ("scope", scope),
    ]
    if scope not in aggregate_cluster_scoped():
        _required.append(("namespace", namespace))

    _missing = [name for name, val in _required if not val]
    if _missing:
        raise ValueError(
            "L4 adapter: fault_intent missing required field(s): "
            f"{_missing}. Got fault_intent keys={list(fi.keys())}. "
            "Required = target / action / scope / namespace (per "
            "FaultSpec.to_intent_dict())."
        )

    # Validate the explicit transport channel override (mirrors the REST
    # InjectRequest validator).  The L4 AgentCard advertises this enum in
    # FAULT_PAYLOAD_SCHEMA, but jsonschema is never enforced programmatically,
    # so validate here — fail-closed with a clear error rather than letting a
    # bad value crash deep inside execute_via_transport → resolve().
    _conn_mode = payload.get("kube_connection_mode", "")
    if _conn_mode not in ("", "kubeconfig", "kubewiz_k8s", "kubewiz_host", "ssh"):
        raise ValueError(
            f"L4 adapter: invalid kube_connection_mode {_conn_mode!r}; "
            "allowed = '' / kubeconfig / kubewiz_k8s / kubewiz_host / ssh."
        )

    fault_spec_dict = {
        "namespace": namespace,
        "scope": scope,
        "names": fi.get("names", []),
        "labels": fi.get("labels", {}),
        "fault_target": fault_target,
        "fault_action": fault_action,
        "params": fi.get("params", {}),
        # Single canonical duration key (l4-contract-faithfulness):
        # ``duration_seconds`` — the key emitted by to_intent_dict() and
        # the only legal duration channel. The retired ``duration``
        # alias has no reader here.
        "duration_seconds": fi.get("duration_seconds", 300),
        "source": "l4_sdk",
        "user_description": fi.get("user_description") or task.intent,
    }
    # L4 SDK skips intent_clarification / batch_setup, so it must still
    # explicitly mark the operation as an inject for save_memory/postmortem.
    return build_inject_initial_state(
        task_id=task.task_id,
        fault_spec=fault_spec_dict,
        confirmed_intent="inject",
        needs_confirmation=False,
        interaction_mode="l4",  # Avoid CLI auto-reject in confirmation_gate
        kubeconfig=payload.get("kubeconfig", ""),
        kube_context=payload.get("kube_context", ""),
        kubewiz_cluster_uuid=payload.get("kubewiz_cluster_uuid", ""),
        kubewiz_profile=payload.get("kubewiz_profile", ""),
        kube_connection_mode=_conn_mode,
        host_name=payload.get("host_name", ""),
        ssh_host=payload.get("ssh_host", ""),
        ssh_user=payload.get("ssh_user", ""),
        ssh_key_path=payload.get("ssh_key_path", ""),
        ssh_port=payload.get("ssh_port"),
        messages=[],
        tenant_id=payload.get("tenant_id", ""),
        # str() normalization: platform sides (tools_l4 / tools_chaos) carry
        # workspace_id as a UUID OBJECT for their own token-attribution
        # consumers; state/SQL want the plain string form.
        workspace_id=str(payload.get("workspace_id") or ""),
    )


def state_to_task_result(
    values: dict, task_id: str, trajectory_id: str = ""
) -> L4TaskResult:
    """Extract TaskResult from graph final state.

    Reuses build_status_data() to avoid reinventing field assembly.
    """
    from chaos_agent.agent.state import (
        TaskState,
        build_status_data,
        infer_task_state,
    )

    task_state = infer_task_state(values)
    status_data = build_status_data(task_id, values)

    # 透出 LLM token 消耗：从 observability tracer 取 trace 汇总，让平台
    # 大盘可以记录每次混沌实验的 token 用量。L4TaskResult 没有专属字段，
    # 借 extras 字典传出（dict 形态，平台侧用 .get 读）。
    token_usage_dict: dict | None = None
    try:
        from chaos_agent.observability.tracer import _traces
        _trace = _traces.get(task_id)
        if _trace is not None:
            _ti = int(getattr(_trace, "total_token_input", 0) or 0)
            _to = int(getattr(_trace, "total_token_output", 0) or 0)
            _calls = int(getattr(_trace, "total_llm_calls", 0) or 0)
            if _ti or _to or _calls:
                token_usage_dict = {
                    "prompt_tokens": _ti,
                    "completion_tokens": _to,
                    "total_tokens": _ti + _to,
                    "call_count": _calls or 1,
                }
    except Exception:
        token_usage_dict = None

    # Keys derive from the TaskState legislation (round-15): the map is
    # total over the closed set minus "cancelled" (default → failed).
    status_map = {
        TaskState.INJECTED.value: "passed",
        TaskState.RECOVERED.value: "passed",
        TaskState.PARTIAL_RECOVERED.value: "degraded",
        TaskState.FAILED.value: "failed",
        TaskState.REJECTED.value: "failed",
        # Verification ran but produced no conclusion (evidence unavailable):
        # "completed with reservations" — not passed (no evidence of success),
        # not failed (no counter-evidence either). error stays None: there is
        # nothing to report as an error.
        TaskState.UNVERIFIED.value: "degraded",
        TaskState.INJECTING.value: "degraded",
        TaskState.RECOVERING.value: "degraded",
        TaskState.COMPLETED.value: "passed",
    }
    status = status_map.get(task_state, "failed")

    error = None
    if status == "failed":
        error = _extract_error(values, task_state)

    from chaos_agent.agent.result.operation_outcome import (
        read_inject_verification,
        read_operation_outcome,
    )

    verification = (
        status_data["verification"]
        if "verification" in status_data
        else read_inject_verification(values)
    )
    outcome = read_operation_outcome(values)

    experiment_uid_out = (
        status_data.get("experiment_uid")
        or values.get("experiment_uid")
        or ""
    )

    return L4TaskResult(
        task_id=task_id,
        status=status,
        trajectory_id=trajectory_id,
        summary=status_data.get("fault_type", "") + " · " + task_state,
        error=error,
        # First-class verification fields (D6): the machine-readable answer
        # to "how do you know" — extras["verification"] stays as the mirror
        # for legacy readers during the transition.
        verification=verification,
        observation_failures=_collect_observation_failures(verification),
        extras={
            "experiment_uid": experiment_uid_out,
            "verification": verification,
            "safety": values.get("safety_status"),
            "task_state": task_state,
            "phase": status_data.get("phase"),
            "duration_ms": status_data.get("duration_ms"),
            # Surface the LLM-generated postmortem (path/markdown/summary)
            # written by save_memory node so SDK callers can render it.
            "postmortem": outcome.postmortem,
            "token_usage": token_usage_dict,
            **{
                k: v
                for k, v in status_data.items()
                if k not in ("task_id", "stage", "status")
            },
        },
    )


def build_recover_initial_state(inject_values: dict, inject_task_id: str) -> dict:
    """Build recover graph initial_state from inject graph final state.

    Mirrors server/routes/recover.py: reads inject checkpoint
    but does NOT copy full state (prevents causal chain illusion).
    """
    from chaos_agent.agent.state_mgmt.recovery_state import build_recover_initial_from_checkpoint

    return build_recover_initial_from_checkpoint(
        inject_values,
        inject_task_id,
        record_task_id=f"recover-{inject_task_id}",
    )


def make_trajectory_id(task_id: str) -> str:
    """Generate a trajectory_id. Format: traj-{task_id}-{short_uuid}."""
    short = uuid.uuid4().hex[:8]
    return f"traj-{task_id}-{short}"


async def attach_intent_handoff(
    initial_state: dict, pool, intent_thread_id: str
) -> dict:
    """Bridge intent-dialogue evidence into an L4 inject task's initial state.

    The platform's three-step protocol (clarify → step(approved) → invoke)
    runs the intent dialogue and the inject pipeline as two independent SDK
    calls. The approval path already harvests ``probe_snapshot`` /
    ``progress_ledger`` / ``handoff_summary`` into the Intent Graph
    checkpoint (``_commit_inject_handoff``); this function reads that
    checkpoint back at invoke time so the pipeline starts with the same
    intent-time evidence a TUI dispatch would carry — same extraction
    function (``build_pipeline_handoff_from_intent_state``), same
    cross-intent residue guards — one source of truth, no fourth copy.

    ``intent_thread_id`` must carry the ``chaos-`` prefix — the SDK's own
    convention for intent-dialogue threads (``_async_step`` uses the same
    discriminator). Anything else means "no dialogue source declared"
    (REST direct calls, older platforms) and skips the bridge silently.

    Enhancement-only / fail-open: a missing checkpoint, an unreadable
    state, or a guard rejection degrades to the cold-start initial_state
    — a new failure mode here must never block the injection itself.
    """
    if not intent_thread_id or not intent_thread_id.startswith("chaos-"):
        return initial_state
    try:
        config = {"configurable": {"thread_id": intent_thread_id}}
        existing = await pool.intent_graph.aget_state(config)
        values = existing.values if existing else None
        if not values:
            logger.debug(
                "attach_intent_handoff: no checkpoint for thread %s; "
                "cold-start initial_state kept",
                intent_thread_id,
            )
            return initial_state

        handoff = build_pipeline_handoff_from_intent_state(
            values,
            operation="inject",
            task_id=initial_state.get("task_id", ""),
            default_tui_session_id="",
        )

        # Residue guard, anchored on the TASK-side spec: the checkpoint's
        # evidence may belong to a previously approved intent about a
        # different target (the clarify loop resets fault_spec each turn,
        # but a fresh clarify is not guaranteed between approvals). The
        # task payload's fault_intent is the only authoritative statement
        # of what THIS run targets. Same guards as _commit_inject_handoff;
        # snapshot and ledger are judged independently.
        from chaos_agent.agent.spec.fault_spec import read_fault_spec

        task_spec = read_fault_spec({"fault_spec": initial_state.get("fault_spec")})
        tokens = spec_relevance_tokens(task_spec) if task_spec is not None else []

        # Identity guard for the seed summary: the summary carries the
        # APPROVED intent's identity line ("Fault: … → scope/target/action
        # @ ns"). TUI needs no such guard — approval and dispatch happen in
        # the same conversational turn — but the decoupled platform timing
        # allows "approve A, then direct-inject B" in one session, and an
        # A-flavoured summary seeding B's pipeline is stale intent identity,
        # not conversation context. Two complementary judgements, because
        # the checkpoint's fault_spec and handoff_summary have different
        # lifetimes — a later clarify turn resets fault_spec (interaction.py
        # reset dicts) but NOT handoff_summary:
        #   spec alive  → identity quadruple (scope/namespace/fault_target/
        #                 fault_action). params are excluded because the
        #                 platform legitimately injects defaults (timeout)
        #                 into the payload after approval.
        #   spec absent → word-boundary residue against the task's tokens
        #                 (same guard class as snapshot/ledger). A summary
        #                 never carries names, so only the namespace token
        #                 can hit: this narrows the stale window to
        #                 same-ns-different-target after a clarify reset —
        #                 accepted residue (design D3).
        # A missing task-side spec skips both → fail-open (pre-change
        # behaviour is the floor).
        summary = handoff.handoff_summary
        if summary and task_spec is not None:
            if handoff.fault_spec is not None:
                ckpt_spec = read_fault_spec({"fault_spec": handoff.fault_spec})
                if ckpt_spec is not None and (
                    ckpt_spec.scope,
                    ckpt_spec.namespace,
                    ckpt_spec.fault_target,
                    ckpt_spec.fault_action,
                ) != (
                    task_spec.scope,
                    task_spec.namespace,
                    task_spec.fault_target,
                    task_spec.fault_action,
                ):
                    logger.debug(
                        "attach_intent_handoff: handoff_summary from thread %s "
                        "describes a different approved intent; dropped",
                        intent_thread_id,
                    )
                    summary = ""
            elif tokens and is_previous_intent_residue([summary], tokens):
                logger.debug(
                    "attach_intent_handoff: handoff_summary from thread %s "
                    "names none of this task's target tokens; dropped",
                    intent_thread_id,
                )
                summary = ""

        snapshot = handoff.probe_snapshot
        if snapshot is not None and tokens:
            facts = [
                entry.get("fact")
                for entry in snapshot.get("facts", [])
                if isinstance(entry, dict)
            ]
            if is_previous_intent_residue(facts, tokens):
                logger.debug(
                    "attach_intent_handoff: probe_snapshot from thread %s "
                    "names none of this task's target tokens; dropped",
                    intent_thread_id,
                )
                snapshot = None

        ledger = handoff.progress_ledger
        if ledger is not None and tokens:
            ledger_state = ledger.get("state")
            established = (
                ledger_state.get("established_facts")
                if isinstance(ledger_state, dict)
                else None
            )
            if is_previous_intent_residue(list(established or []), tokens):
                logger.debug(
                    "attach_intent_handoff: progress_ledger from thread %s "
                    "names none of this task's target tokens; dropped",
                    intent_thread_id,
                )
                ledger = None

        # build_pipeline_handoff_from_intent_state already deep-copied both
        # payloads out of the checkpoint, so assigning them straight into the
        # initial_state keeps the pipeline decoupled from the dialog thread.
        if snapshot is not None:
            initial_state["probe_snapshot"] = snapshot
        if ledger is not None:
            initial_state["progress_ledger"] = ledger

        if summary:
            from langchain_core.messages import SystemMessage

            seed = SystemMessage(content=summary)
            initial_state["messages"] = [
                seed,
                *list(initial_state.get("messages") or []),
            ]

        return initial_state
    except Exception:
        logger.debug(
            "attach_intent_handoff failed for thread %s; cold-start "
            "initial_state kept",
            intent_thread_id,
            exc_info=True,
        )
        return initial_state
