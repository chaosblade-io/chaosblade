"""Shared writer for operation summaries.

Operation summaries are durable dialogue-memory records.  This module owns the
side effects of appending those records to the Intent Graph and the TUI session
store; summary text construction lives in ``chaos_agent.agent.operation_summary``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from langchain_core.messages import SystemMessage

from chaos_agent.config.settings import settings

logger = logging.getLogger(__name__)

_DEFAULT_TUI_STORE = object()
_RESERVED_STATE_UPDATE_KEYS = frozenset({"messages"})


@dataclass(frozen=True)
class OperationSummaryWriteResult:
    """Best-effort write outcomes for observability and tests."""

    graph_written: bool = False
    tui_dialogue_written: bool = False
    tui_task_indexed: bool = False
    session_task_indexed: bool = False


async def write_operation_summary(
    summary_text: str,
    *,
    intent_graph: Any | None = None,
    thread_id: str = "",
    state_update: Mapping[str, Any] | None = None,
    tui_session_id: str = "",
    tui_session_store: Any | None = _DEFAULT_TUI_STORE,
    session_index_store: Any | None = None,
    task_id: str = "",
    recursion_limit: int | None = None,
    as_node: str = "save_dialogue",
    raise_graph_error: bool = True,
) -> OperationSummaryWriteResult:
    """Persist an operation summary to Intent Graph and TUI session memory.

    Graph writes and TUI writes are deliberately independent: a checkpoint
    failure must not prevent the append-only session audit trail from receiving
    the summary.  When ``raise_graph_error`` is true, the graph exception is
    re-raised after best-effort TUI writes so callers can keep their existing
    logging behavior.
    """

    if not summary_text:
        return OperationSummaryWriteResult()

    extra_update = dict(state_update or {})
    reserved = sorted(_RESERVED_STATE_UPDATE_KEYS.intersection(extra_update))
    if reserved:
        raise ValueError(
            "Operation summary state_update cannot override reserved keys: "
            + ", ".join(reserved)
        )

    summary_msg = SystemMessage(content=summary_text)
    update = {"messages": [summary_msg]}
    update.update(extra_update)

    graph_written = False
    tui_dialogue_written = False
    tui_task_indexed = False
    session_task_indexed = False
    graph_error: Exception | None = None

    if intent_graph is not None and thread_id:
        try:
            await intent_graph.aupdate_state(
                {
                    "configurable": {"thread_id": thread_id},
                    "recursion_limit": (
                        settings.recursion_limit
                        if recursion_limit is None
                        else recursion_limit
                    ),
                },
                update,
                as_node=as_node,
            )
            graph_written = True
        except Exception as exc:
            graph_error = exc
            if not raise_graph_error:
                logger.warning(
                    "Failed to write operation summary to Intent Graph",
                    exc_info=True,
                )

    try:
        store = tui_session_store
        if store is _DEFAULT_TUI_STORE:
            from chaos_agent.memory.tui_session_store import (
                get_global_tui_session_store,
            )

            store = get_global_tui_session_store()
        if store is not None and tui_session_id:
            store.append_dialogue(tui_session_id, [summary_msg])
            tui_dialogue_written = True
            if task_id:
                store.add_task(tui_session_id, task_id)
                tui_task_indexed = True
    except Exception:
        logger.warning("Failed to write operation summary to TUI session", exc_info=True)

    try:
        if session_index_store is not None and tui_session_id and task_id:
            session_index_store.add_task(tui_session_id, task_id)
            session_task_indexed = True
    except Exception:
        logger.warning(
            "Failed to index operation summary task in TUI session",
            exc_info=True,
        )

    if graph_error is not None and raise_graph_error:
        raise graph_error

    return OperationSummaryWriteResult(
        graph_written=graph_written,
        tui_dialogue_written=tui_dialogue_written,
        tui_task_indexed=tui_task_indexed,
        session_task_indexed=session_task_indexed,
    )
