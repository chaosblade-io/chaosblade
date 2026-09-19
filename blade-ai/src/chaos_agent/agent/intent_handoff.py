"""Intent Graph → Pipeline Graph handoff helpers.

The Intent Graph may keep rich dialogue history, but once an inject or batch
operation is dispatched, executable one-shot payload fields must be cleared so
the next conversational turn does not re-launch a stale intent.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


DISPATCHED_OPERATION_CLEAR_UPDATE: dict[str, Any] = {
    "confirmed_intent": None,
    "batch_submit_args": None,
    "fault_spec": None,
    "handoff_summary": None,
    "intent_reasoning": None,
    "intent_confidence": 0.0,
    "clarification_round": 0,
    # Cross-graph bridge payload (tier1-speedup): dispatched once, then stale.
    # The pipeline graph received its own deepcopy at dispatch, so clearing
    # here never touches the running pipeline.
    "progress_ledger": None,
    "probe_snapshot": None,
}


@dataclass(frozen=True)
class PipelineHandoff:
    """Resolved data needed to start a Pipeline Graph from IntentState."""

    operation: str
    task_id: str
    tui_session_id: str
    handoff_summary: str
    fault_spec: dict | None = None
    batch_submit_args: dict | None = None
    # Cross-graph bridge (tier1-speedup): intent-time evidence that must reach
    # the pipeline graph's AgentState — ``progress_ledger`` (facts the model
    # recorded during clarification via ``update_progress``) and
    # ``probe_snapshot`` (the timestamped per-fact record harvested at
    # ``intent_confirm`` approval). Both are None on direct (no-intent-graph)
    # entry paths, which is the pre-change rendering baseline.
    progress_ledger: dict | None = None
    probe_snapshot: dict | None = None


def clear_dispatched_operation_payload_update() -> dict[str, Any]:
    """Return the IntentState update that clears dispatched one-shot payload."""

    return deepcopy(DISPATCHED_OPERATION_CLEAR_UPDATE)


def detect_dispatchable_operation(
    intent_state: dict,
    *,
    has_pending_interrupt: bool = False,
) -> str | None:
    """Return the operation ready for Pipeline dispatch, if any."""

    if has_pending_interrupt:
        return None

    confirmed = intent_state.get("confirmed_intent")
    if confirmed == "batch_inject" and intent_state.get("batch_submit_args"):
        return "batch_inject"
    if confirmed == "inject" and intent_state.get("fault_spec"):
        return "inject"
    return None


def build_pipeline_handoff_from_intent_state(
    intent_state: dict,
    *,
    operation: str,
    task_id: str,
    default_tui_session_id: str = "",
) -> PipelineHandoff:
    """Extract immutable handoff data from IntentState for Pipeline startup."""

    if operation not in ("inject", "batch_inject"):
        raise ValueError(f"Unsupported pipeline handoff operation: {operation}")

    batch_submit_args = (
        deepcopy(intent_state.get("batch_submit_args"))
        if operation == "batch_inject"
        else None
    )
    return PipelineHandoff(
        operation=operation,
        task_id=task_id,
        tui_session_id=(intent_state.get("tui_session_id") or default_tui_session_id or ""),
        handoff_summary=str(intent_state.get("handoff_summary") or ""),
        fault_spec=deepcopy(intent_state.get("fault_spec")),
        batch_submit_args=batch_submit_args,
        progress_ledger=deepcopy(intent_state.get("progress_ledger")),
        probe_snapshot=deepcopy(intent_state.get("probe_snapshot")),
    )


def word_contains(haystack: str, token: str) -> bool:
    """Whole-token containment (``drill-target`` must NOT match
    ``drill-target-2``). Delimiters allow the hyphens/dots K8s names
    are made of, so only an exact standalone occurrence counts.
    """
    return re.search(rf"(?<![\w.-]){re.escape(token)}(?![\w.-])", haystack) is not None


def spec_relevance_tokens(spec) -> list[str]:
    """Tokens identifying THIS intent's target: names / param values /
    namespace (word-boundary matched via ``word_contains``).

    Single source for ALL cross-intent guards (snapshot primary source,
    the ledger handoff in ``intent_confirm._commit_inject_handoff``, and
    the L4 invoke-side bridge ``l4/adapter.attach_intent_handoff``) —
    they must agree on what counts as "about this intent".
    """
    names = [n for n in (getattr(spec, "names", ()) or ()) if isinstance(n, str) and n.strip()]
    params = getattr(spec, "params", None) or {}
    param_values = [
        v.strip() for v in params.values()
        if isinstance(v, str) and len(v.strip()) >= 3 and not v.strip().isdigit()
    ]
    ns = (getattr(spec, "namespace", "") or "").strip()
    return [t for t in (*names, *param_values, ns) if t]


def is_previous_intent_residue(batch: list, tokens: list[str]) -> bool:
    """Whole-BATCH cross-intent check: a batch naming none of THIS intent's
    tokens belongs to a previous intent about a different target.

    Whole-batch rather than per-entry — a causal-insight fact need not name
    the target itself as long as its batch does (the model records identity
    facts and insights together). An empty batch or an empty token set
    cannot discriminate, so neither counts as residue (keep as-is). All
    entries are strings by construction (``_latest_established_facts``
    filters; the ledger's merge bounds values), but coerce defensively.
    """
    if not batch or not tokens:
        return False
    blob = "\n".join(str(f) for f in batch)
    return not any(word_contains(blob, t) for t in tokens)


__all__ = [
    "DISPATCHED_OPERATION_CLEAR_UPDATE",
    "PipelineHandoff",
    "build_pipeline_handoff_from_intent_state",
    "clear_dispatched_operation_payload_update",
    "detect_dispatchable_operation",
    "is_previous_intent_residue",
    "spec_relevance_tokens",
    "word_contains",
]
