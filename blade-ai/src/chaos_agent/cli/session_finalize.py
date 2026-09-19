"""Session finalization and auto-rollback utilities for CLI / TUI."""

from __future__ import annotations

import logging

from chaos_agent.memory.session_finalizer import (
    finalize_inject_session as _finalize_inject_session,  # noqa: F401  (re-exported: cli.runner imports it from here)
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
    """Attempt to roll back orphaned faults after inject failure.

    Domain-aligned cleanup (round-30): the LIVE criterion judges the
    PLURAL liability set (any live sibling licenses the rollback), so
    the ACTION must cover the same set. An experiment-kind fault
    dispatches the registry's liability sweep (every UID in owned −
    retired − proven-death), not the singular attribution slot — a
    composite create (``blade create A && blade create B``) parks the
    FIRST birth in the slot, and when that birth dies while the sibling
    stays live, the slot-valued dispatch destroyed the corpse and
    reported success while the live sibling left the failing task
    orphaned (round-30 K1'; gate plural, action singular — the r29 fix
    made the mismatch visible, it did not create it). The sweep IS the
    live-set-driven destroy: gate and action are one, an empty live set
    renders "" (a destroyed task rolls back nothing, the r29 corpse
    protection preserved through the sweep's own live filter).

    Native handles keep the singular dispatch: their criterion domain
    (committed single handle, no death oracle — round-25) and action
    domain were never split.
    """
    try:
        current_state = await graph.aget_state(config)
        if current_state and current_state.values:
            values = current_state.values
            from chaos_agent.agent.state import materialize_fault_handle

            handle = materialize_fault_handle(values)
            if handle:
                from chaos_agent.agent.providers import FaultProviderRegistry

                if FaultProviderRegistry.is_experiment_handle(handle):
                    retired_new, failures = (
                        await FaultProviderRegistry.sweep_live_liabilities(
                            values
                        )
                    )
                    parts = []
                    if retired_new:
                        parts.append(
                            "auto-rolled back experiment_uids="
                            + ", ".join(retired_new)
                        )
                    if failures:
                        # Un-recovered liabilities with the reason the sweep
                        # surfaced (e.g. the in-cluster delivery guidance) —
                        # an honest "still owed" beats the old suffix that
                        # reported the slot uid as rolled back.
                        parts.append(
                            "rollback INCOMPLETE: " + "; ".join(failures)
                        )
                    return f" ({'; '.join(parts)})" if parts else ""
                # Native carrier: committed-True by design (no death oracle,
                # round-25) — the conservative singular undo is the whole
                # domain, and the live predicate would answer True for every
                # native handle anyway.
                #
                # Config domain (round-32, K1c-b): the explicit-signature
                # dispatch site passes the RESOLVED value (state > spec >
                # settings), never the bare state read — the same rule the
                # sweep now enforces internally. Today's native carriers
                # (host_shell, k8s_native) are no-op rollbacks, so this is
                # defensive alignment: a future native carrier with a real
                # undo inherits the injection chain's cluster, not blade's
                # own default.
                from chaos_agent.agent.kubeconfig import resolve_kubeconfig

                logger.warning(
                    "Auto-rollback: dispatching fault handle %s after inject failure",
                    handle,
                )
                return await FaultProviderRegistry.rollback_handle(
                    handle, kubeconfig=resolve_kubeconfig(values),
                )
    except Exception as rb_err:  # noqa: BLE001
        logger.error("Auto-rollback failed: %s", rb_err)
        return f" (rollback FAILED: {rb_err})"
    return ""
