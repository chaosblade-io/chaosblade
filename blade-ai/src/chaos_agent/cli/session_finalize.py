"""Session finalization and auto-rollback utilities for CLI / TUI."""

from __future__ import annotations

import logging

from chaos_agent.memory.session_finalizer import (
    finalize_inject_session as _finalize_inject_session,
)

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
