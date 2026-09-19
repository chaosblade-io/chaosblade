"""Transient transport error classification + bounded retry policy.

Live-probed against the kubewiz platform (2026-07 ~ 2026-09, clusters
c62735cce / cb7d0de0 / c63aa73): the ``wiz task exec`` dispatch layer
intermittently fails BEFORE the command ever runs, surfacing as
``No executor available for cluster: ...`` or ``executor ... heartbeat
is stale``. Both are dispatch-level failures — the command never reached
an executor, so retrying is semantically safe (no partial side effects
on the cluster) and empirically effective for the flapping form
(2026-08-25 probe: 8/8 green after a 45s backoff).

Two failure forms share these signatures (diagnosis memory, 2026-07/08):
- flapping: executor heartbeat thread briefly wedged; retry heals it.
- zombie: executor instance is dead but its registration lingers in the
  scheduling pool; retrying burns the budget for nothing — the exhaustion
  hint tells the model to stop retrying and report the channel instead.

The retry lives HERE (transport layer), not in the LLM loop: without it
each transient error costs a full inference round (~60s + tokens) just
for the model to re-issue the identical command (#49: 23 occurrences in
a 22-minute active window; #51: 11; #50: 10).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Dispatch-level failure signatures, probed live. An RBAC 403/Forbidden is
# deliberately NOT here — that is a deterministic rejection and retrying
# can never change the outcome.
_TRANSIENT_SIGNATURES: tuple[str, ...] = (
    "no executor available",
    "heartbeat is stale",
)


def is_transient_transport_error(message: str) -> bool:
    """True when ``message`` carries a transient dispatch-layer signature."""
    lowered = (message or "").lower()
    return any(signature in lowered for signature in _TRANSIENT_SIGNATURES)


def transient_retry_delays() -> list[float]:
    """Delays (seconds) before each transport-layer retry attempt.

    ``base * 2**attempt`` (default 30s -> 60s), brackets the live-probed
    ~45s self-heal window of the flapping form; worst-case added wall
    clock ~90s, far below the LLM-loop cost it replaces.
    """
    from chaos_agent.config.settings import settings

    base = settings.transport_transient_retry_base_delay
    return [
        base * (2 ** attempt)
        for attempt in range(settings.transport_transient_retry_max)
    ]


def transient_exhaustion_hint() -> str:
    """Appended to stderr when retries are exhausted, so the model sees
    this is NOT a first failure and further identical retries are wasted —
    the zombie form needs platform-side registration cleanup, not retries.
    """
    return (
        " [transport] transient dispatch error persisted after automatic"
        " retries — likely a zombie executor registration; do NOT retry"
        " the same command unchanged, report the channel to the platform"
    )
