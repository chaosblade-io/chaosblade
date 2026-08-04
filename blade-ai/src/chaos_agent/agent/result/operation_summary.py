"""Durable operation summaries written back to dialogue memory.

These summaries are not UI rendering artifacts.  They are compact memory
records that tell the intent graph what actually happened in a completed
inject, batch inject, or recover operation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from chaos_agent.agent.spec.fault_spec import fault_type_from_state, read_fault_spec
from chaos_agent.agent.result.operation_outcome import (
    build_verification_simple,
    read_inject_verification,
)
from chaos_agent.agent.state import extract_ui_diagnostics, infer_task_state


POST_OPERATION_FRESHNESS_NOTE = (
    "Advice for follow-up targets: resource names in this summary and in earlier "
    "history are historical context only; to reuse any of these targets, their "
    "current existence MUST be re-verified with kubectl."
)


@dataclass(frozen=True)
class OperationSummary:
    """Textual operation memory plus a small kind discriminator."""

    kind: str
    text: str

    def __bool__(self) -> bool:
        return bool(self.text)


def format_summary_target(target: Any) -> str:
    """Format a result target dict for compact operation summaries."""

    if not isinstance(target, Mapping):
        return ""

    namespace = str(target.get("namespace") or "")
    names = target.get("names") or []
    labels = target.get("labels") or {}

    if isinstance(names, (list, tuple)) and names:
        target_text = ", ".join(str(n) for n in names if n is not None)
    elif isinstance(labels, Mapping) and labels:
        target_text = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    else:
        target_text = ""

    if namespace and target_text:
        return f"{namespace}/{target_text}"
    if namespace:
        return namespace
    return target_text


def _format_state_target(values: Mapping[str, Any]) -> str:
    spec = read_fault_spec(dict(values))
    if spec is None:
        return ""

    target = {
        "namespace": spec.namespace,
        "names": list(spec.names or []),
        "labels": dict(spec.labels or {}),
    }
    return format_summary_target(target)


def _format_verification_line(prefix: str, verification: Mapping[str, Any] | None) -> str:
    if not isinstance(verification, Mapping):
        return ""

    simple = build_verification_simple(dict(verification))
    if not simple:
        return ""

    level = simple.get("level", "?")
    l1 = simple.get("layer1", {}).get("status", "?")
    l2 = simple.get("layer2", {}).get("status", "?")
    return f"{prefix}: {level} (L1={l1}, L2={l2})"


def build_task_summary(state_values: Mapping[str, Any] | None, task_id: str) -> OperationSummary:
    """Build the durable summary written after a single inject operation."""

    values = dict(state_values or {})
    task_state = infer_task_state(values) if values else "unknown"
    fault_type = fault_type_from_state(values) if values else ""
    target_text = _format_state_target(values) if values else ""
    blade_uid = values.get("blade_uid", "")
    verification = read_inject_verification(values)
    diagnostics = extract_ui_diagnostics(values) if values else {}

    parts = [
        f"[Task Summary] task_id={task_id}",
        f"Type: {fault_type} | Target: {target_text}",
        f"Result: {task_state} | blade_uid: {blade_uid}",
    ]
    verification_line = _format_verification_line("Verification", verification)
    if verification_line:
        parts.append(verification_line)
    if diagnostics.get("side_effects_summary"):
        parts.append(f"Side effects: {diagnostics['side_effects_summary']}")
    if diagnostics.get("failure_reason"):
        parts.append(f"Failure reason: {diagnostics['failure_reason']}")
    parts.append(POST_OPERATION_FRESHNESS_NOTE)
    return OperationSummary(kind="inject", text="\n".join(parts))


def build_task_summary_text(state_values: Mapping[str, Any] | None, task_id: str) -> str:
    return build_task_summary(state_values, task_id).text


def append_ledger_process_detail(
    summary_text: str, state_values: Mapping[str, Any] | None,
) -> str:
    """Append the progress ledger's process detail below a summary headline.

    One coherent record, not two overlapping ones: the ledger's established
    facts + milestone log are appended after the (deterministic) summary. The
    ledger's anchor is omitted because the goal is already in the summary, so
    nothing is repeated. Degrades to the plain summary when the ledger is empty.
    Shared by the inject and recover mirror paths.
    """
    from chaos_agent.agent.progress_ledger import render_ledger
    ledger = dict(state_values or {}).get("progress_ledger")
    process_detail = render_ledger(ledger, include_anchor=False)
    if not process_detail:
        return summary_text
    return f"{summary_text}\n\nProgress detail (executor's own record):\n{process_detail}"


def build_operation_record(state_values: Mapping[str, Any] | None, task_id: str) -> str:
    """Compose the SINGLE record mirrored to the intent graph on inject success.

    The deterministic task summary is the authoritative headline (type / target
    / result / verification), and the progress ledger's process detail is
    appended below it via :func:`append_ledger_process_detail`.
    """
    return append_ledger_process_detail(
        build_task_summary_text(state_values, task_id), state_values,
    )


#: Why a turn ended without completing, in the wording shown to the intent graph.
INTERRUPT_CAUSES = {
    "user_cancel": "cancelled by the user mid-operation",
    "confirm_timeout": "confirmation card timed out with no response",
    "disconnected": "client connection dropped",
    "internal_error": "an internal error interrupted this turn",
}
_INTERRUPT_DETAIL_LIMIT = 200


def build_interrupted_record(
    state_values: Mapping[str, Any] | None,
    task_id: str,
    *,
    cause: str,
    error_detail: str = "",
) -> str:
    """Compose the SINGLE record mirrored to intent when a turn is INTERRUPTED.

    Symmetric to :func:`build_operation_record`, but there is no completed
    outcome to summarise — so the progress ledger IS the record: what the
    executor had established and how far it got, plus why it stopped. The
    ledger's own ``status`` markers are preserved, so a finding the executor
    never verified is not presented to the next dialogue turn as fact.

    The closing line is advisory, never imperative: it tells the user what may
    still be live and suggests checking, it does not order a recovery.
    """
    cause_text = INTERRUPT_CAUSES.get(cause, cause or "unknown reason")
    parts = [f"[Task Interrupted] task_id={task_id}", f"Cause: {cause_text}"]

    values = dict(state_values or {})
    # Identify WHAT was interrupted, from state rather than from the ledger. The
    # ledger has no anchor during planning (the FaultSpec is still converging) and
    # the model may not have recorded anything at all — without this the dialogue
    # would learn only that "something was cancelled", and a follow-up like "try
    # again" would have no referent.
    _fault_type = fault_type_from_state(values) if values else ""
    _target = _format_state_target(values) if values else ""
    if _fault_type or _target:
        parts.append(f"Type: {_fault_type} | Target: {_target}")

    if error_detail:
        parts.append(f"Error detail: {error_detail[:_INTERRUPT_DETAIL_LIMIT]}")

    from chaos_agent.agent.progress_ledger import render_ledger
    ledger_text = render_ledger(values.get("progress_ledger"))
    if ledger_text:
        parts.append("")
        parts.append("Progress before the interruption (executor's own record):")
        parts.append(ledger_text)
    else:
        parts.append(
            "The executor left no progress record, so how far it got cannot be "
            "determined."
        )

    # Advisory, not a command: state what may be live and suggest a check.
    if values.get("blade_uid") or values.get("execution_artifacts"):
        parts.append(
            "Note: this operation already made real changes and was not fully "
            "verified, so the target may still be in a faulted state. Checking "
            "its live status first is advisable; if the fault is confirmed to be "
            "still in effect, a recovery can be run."
        )
    parts.append(POST_OPERATION_FRESHNESS_NOTE)
    return "\n".join(parts)


def build_batch_summary(
    batch_results: Sequence[Any] | None,
    batch_pm_path: str = "",
) -> OperationSummary:
    """Build the durable summary written after a batch injection."""

    results = list(batch_results or [])
    if not results:
        return OperationSummary(kind="batch_inject", text="")

    parts = [
        f"[Batch Summary] {len(results)} faults",
        "Operation: batch_inject",
    ]
    for idx, result in enumerate(results):
        if not isinstance(result, Mapping):
            continue
        task_state = str(result.get("task_state") or "unknown")
        ok = task_state in ("injected",)
        target_text = format_summary_target(result.get("target"))
        target_suffix = f" target={target_text}" if target_text else ""
        parts.append(
            f"  {idx + 1}. {result.get('fault_type', '')} "
            f"→ {task_state} "
            f"{'✓' if ok else '✗'} "
            f"(task={result.get('task_id', '')})"
            f"{target_suffix}"
        )
        failure_reason = result.get("failure_reason") or result.get("error")
        if failure_reason:
            parts.append(f"     Failure reason: {failure_reason}")

    if batch_pm_path:
        parts.append(f"Batch analysis report: {batch_pm_path}")
    parts.append(POST_OPERATION_FRESHNESS_NOTE)
    return OperationSummary(kind="batch_inject", text="\n".join(parts))


def build_batch_summary_text(
    batch_results: Sequence[Any] | None,
    batch_pm_path: str = "",
) -> str:
    return build_batch_summary(batch_results, batch_pm_path).text


def build_recover_summary(
    recover_result: Mapping[str, Any] | None,
    parent_task_id: str,
    inject_state_values: Mapping[str, Any] | None,
) -> OperationSummary:
    """Build the durable summary written after a recovery operation."""

    if not isinstance(recover_result, Mapping):
        return OperationSummary(kind="recover", text="")

    data = recover_result.get("data")
    if not isinstance(data, Mapping):
        return OperationSummary(kind="recover", text="")

    inject_values = dict(inject_state_values or {})
    task_id = data.get("task_id") or ""
    task_state = data.get("task_state") or data.get("result") or "unknown"
    fault_type = data.get("fault_type") or fault_type_from_state(inject_values) or "unknown"
    blade_uid = data.get("blade_uid") or inject_values.get("blade_uid", "")
    target_text = format_summary_target(data.get("target")) or _format_state_target(
        inject_values
    )
    verification = data.get("verification")

    parts = [
        f"[Recover Summary] task_id={task_id}",
        f"parent_task_id: {parent_task_id}",
        f"Type: {fault_type} | Target: {target_text}",
        f"Result: {task_state} | blade_uid: {blade_uid}",
    ]
    verification_line = _format_verification_line(
        "Recovery verification",
        verification if isinstance(verification, Mapping) else None,
    )
    if not verification_line and isinstance(verification, Mapping):
        verification_line = (
            "Recovery verification: "
            f"{verification.get('level', '?')} "
            f"(L1={verification.get('layer1', {}).get('status', '?')}, "
            f"L2={verification.get('layer2', {}).get('status', '?')})"
        )
    if verification_line:
        parts.append(verification_line)
    if data.get("error"):
        parts.append(f"Failure reason: {data['error']}")
    parts.append(POST_OPERATION_FRESHNESS_NOTE)
    return OperationSummary(kind="recover", text="\n".join(parts))


def build_recover_summary_text(
    recover_result: Mapping[str, Any] | None,
    parent_task_id: str,
    inject_state_values: Mapping[str, Any] | None,
) -> str:
    return build_recover_summary(
        recover_result,
        parent_task_id,
        inject_state_values,
    ).text
