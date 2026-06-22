"""Session finalization and auto-rollback utilities for CLI / TUI."""

from __future__ import annotations

import logging

from chaos_agent.models.schemas import build_inject_envelope

logger = logging.getLogger(__name__)


def _format_error(e: Exception) -> tuple[int, str]:
    """Format an exception into (error_code, message) with type info.

    - ChaosAgentError subclasses: use their built-in error_code
    - Other exceptions: code 4001 with type name prefix for debuggability
    """
    from chaos_agent.errors import ChaosAgentError

    if isinstance(e, ChaosAgentError):
        return e.error_code, f"{type(e).__name__}: {e}"
    return 4001, f"{type(e).__name__}: {e}"


async def auto_rollback(graph, config) -> str:
    """Attempt to destroy an orphaned blade experiment after inject failure.

    Returns a human-readable status suffix (e.g. " (auto-rolled back blade_uid=...)").
    Returns empty string when no rollback was needed.
    """
    try:
        current_state = await graph.aget_state(config)
        if current_state and current_state.values:
            blade_uid = current_state.values.get("blade_uid", "")
            kubeconfig = current_state.values.get("kubeconfig", "")
            if blade_uid:
                logger.warning(
                    "Auto-rollback: destroying blade experiment %s after inject failure",
                    blade_uid,
                )
                from chaos_agent.tools.blade import blade_destroy
                destroy_result = await blade_destroy.ainvoke(
                    {"uid": blade_uid, "kubeconfig": kubeconfig}
                )
                logger.info("Auto-rollback result: %s", destroy_result)
                return f" (auto-rolled back blade_uid={blade_uid})"
    except Exception as rb_err:
        logger.error("Auto-rollback failed: %s", rb_err)
        return f" (rollback FAILED: {rb_err})"
    return ""


async def _finalize_inject_session(
    session_store,
    graph_or_agent,
    config,
    session_id: str,
    kwargs: dict | None = None,
    is_open_conversation: bool | None = None,
    error_log_level: str = "warning",
    precomputed_values: dict | None = None,
    tui_session_store=None,
) -> None:
    """Finalize an inject-type session by reading final graph state and
    persisting the result envelope.

    Shared across ``inject_stream``, ``inject``, and ``converse_stream``
    to eliminate duplicated finalization logic.

    Parameters
    ----------
    session_store : SessionStore
        The active session store.
    graph_or_agent : CompiledGraph | dict
        Object with ``aget_state(config)`` method.
        Ignored when ``precomputed_values`` is provided.
    config : RunnableConfig
        LangGraph config dict containing ``thread_id`` and ``configurable``.
    session_id : str
        Task/thread identifier used as the session key.
    kwargs : dict | None
        Original inject kwargs (unused after Phase D unification, kept
        for API compatibility).
    is_open_conversation : bool | None
        If True: mid-conversation turn — append messages but keep session
        alive.  If False/None: final turn — call ``finalize_session``.
    error_log_level : str
        Log level for the outer catch-all exception.
    precomputed_values : dict | None
        Final graph-state ``values`` dict, if already fetched by the caller.
    tui_session_store : TuiSessionStore | None
        Caller's TUI session store for mid-conversation dialogue routing.
    """
    if not session_store:
        return

    try:
        remaining = []
        values_fin = {}

        try:
            if precomputed_values:
                values_fin = precomputed_values
            else:
                final_graph_state = await graph_or_agent.aget_state(config)
                if final_graph_state and final_graph_state.values:
                    values_fin = final_graph_state.values
            remaining = values_fin.get("messages", []) if values_fin else []
        except Exception:
            pass

        if is_open_conversation is True:
            try:
                tui_ses_id = values_fin.get("tui_session_id", "") if values_fin else ""
                if tui_session_store and tui_ses_id:
                    tui_session_store.append_dialogue(tui_ses_id, remaining)
                else:
                    session_store.append_messages(session_id, remaining)
            except Exception:
                logger.debug(
                    "Mid-conversation append failed for %s",
                    session_id,
                    exc_info=True,
                )
            return

        from chaos_agent.server.routes.turn_result import build_inject_data_from_state
        _data = build_inject_data_from_state(values_fin, session_id) if values_fin else {
            "task_id": session_id, "task_state": "unknown", "error": "",
        }
        session_store.finalize_session(
            session_id,
            remaining_messages=remaining,
            result_summary=build_inject_envelope(
                _data, _data.get("task_state", "unknown"), _data.get("error", ""),
            ),
            status="completed",
        )
    except Exception:
        _log = logger.warning if error_log_level == "warning" else logger.debug
        _log("Failed to finalize session for %s", session_id, exc_info=True)
