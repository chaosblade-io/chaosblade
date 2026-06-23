"""Durable operation summaries written back to dialogue memory.

These summaries are not UI rendering artifacts.  They are compact memory
records that tell the intent graph what actually happened in a completed
inject, batch inject, or recover operation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from chaos_agent.agent.fault_spec import fault_type_from_state, read_fault_spec
from chaos_agent.agent.operation_outcome import (
    build_verification_simple,
    read_inject_verification,
)
from chaos_agent.agent.state import extract_ui_diagnostics, infer_task_state


POST_OPERATION_FRESHNESS_NOTE = (
    "后续目标建议: 本概要及更早历史中的资源名仅作历史上下文；"
    "若要复用这些目标，必须重新 kubectl 验证当前存在性。"
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
        f"类型: {fault_type} | 目标: {target_text}",
        f"结果: {task_state} | blade_uid: {blade_uid}",
    ]
    verification_line = _format_verification_line("验证", verification)
    if verification_line:
        parts.append(verification_line)
    if diagnostics.get("side_effects_summary"):
        parts.append(f"副作用: {diagnostics['side_effects_summary']}")
    if diagnostics.get("failure_reason"):
        parts.append(f"失败原因: {diagnostics['failure_reason']}")
    parts.append(POST_OPERATION_FRESHNESS_NOTE)
    return OperationSummary(kind="inject", text="\n".join(parts))


def build_task_summary_text(state_values: Mapping[str, Any] | None, task_id: str) -> str:
    return build_task_summary(state_values, task_id).text


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
        "操作: batch_inject",
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
            parts.append(f"     失败原因: {failure_reason}")

    if batch_pm_path:
        parts.append(f"批量分析报告: {batch_pm_path}")
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
        f"类型: {fault_type} | 目标: {target_text}",
        f"结果: {task_state} | blade_uid: {blade_uid}",
    ]
    verification_line = _format_verification_line(
        "恢复验证",
        verification if isinstance(verification, Mapping) else None,
    )
    if not verification_line and isinstance(verification, Mapping):
        verification_line = (
            "恢复验证: "
            f"{verification.get('level', '?')} "
            f"(L1={verification.get('layer1', {}).get('status', '?')}, "
            f"L2={verification.get('layer2', {}).get('status', '?')})"
        )
    if verification_line:
        parts.append(verification_line)
    if data.get("error"):
        parts.append(f"失败原因: {data['error']}")
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
