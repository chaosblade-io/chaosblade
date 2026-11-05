"""Host-profile diagnostic delivery.

When a fault targets a bare host (``scope == "host"`` / host channel) there is
no cluster tool pod to ``kubectl exec`` into — the diagnostic must run directly
on the host. ``_run_host_diagnostic`` dispatches a read-only shell diagnostic
through the transport layer (mirroring the ``baseline_capture._exec_host_simple``
pattern with ``skip_guard=True``).

It is the sanctioned host-delivery seam: the ``host_http_probe`` tool wraps it
to run a bounded read-only GET directly on the target host, reusing this single
implementation rather than re-deriving the transport call.
"""

from __future__ import annotations

import logging
import shlex

from chaos_agent.config.settings import settings
from chaos_agent.transports import (
    PROFILE_HOST,
    execute_via_transport,
    resolve_transport_target,
)

logger = logging.getLogger(__name__)


async def _run_host_diagnostic(command: str, state: dict | None, task_id: str):
    """Run one read-only diagnostic verbatim on the target host."""
    # Single authoritative seam (ground-truth-single-authority D2): a raw
    # ``from_state(state)`` orphan reads the truthy ssh_port placeholder and
    # masks settings.ssh_port — the same B3 class as the verify supplement.
    target = resolve_transport_target(state or {})
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    return await execute_via_transport(
        argv, target,
        timeout=settings.command_timeout,
        task_id=task_id,
        skip_guard=True,
        # Host-shaped by construction; a cluster channel would answer from
        # the platform executor instead of the target machine.
        expect_profile=PROFILE_HOST,
        source="verify-host",
    )
