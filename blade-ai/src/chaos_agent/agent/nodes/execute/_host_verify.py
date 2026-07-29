"""Host-profile diagnostic sampling.

When a fault targets a bare host (``scope == "host"`` / host channel) there is
no cluster tool pod to ``kubectl exec`` into — the diagnostic must run directly
on the host. ``_run_host_diagnostic`` dispatches a read-only shell diagnostic
through the transport layer (mirroring the ``baseline_capture._exec_host_simple``
pattern with ``skip_guard=True``).

Post-injection effect judgment (fill markers, diskstats deltas) lives in
``_effect_checks``; the per-scope sampling mechanism — including the host path,
which wraps this helper — lives in ``_effect_channels.HostSampleChannel``.
"""

from __future__ import annotations

import logging
import shlex

from chaos_agent.config.settings import settings
from chaos_agent.transports import (
    PROFILE_HOST,
    TransportTarget,
    execute_via_transport,
)

logger = logging.getLogger(__name__)


async def _run_host_diagnostic(command: str, state: dict | None, task_id: str):
    """Run one read-only diagnostic verbatim on the target host."""
    target = TransportTarget.from_state(state or {})
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
