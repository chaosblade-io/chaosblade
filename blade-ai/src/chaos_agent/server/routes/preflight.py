"""GET /api/v1/preflight — boot-time environment self-check for the TS TUI.

The TS TUI's boot screen needs:
  - runtime metadata (kubeconfig path, namespace, model_name) that the
    server side knows but the TS side has no other way to read
  - check results to render the "环境自检 N/M 通过" card

Wraps ``chaos_agent.preflight.run_tui_checks()`` so both the legacy
Python TUI and the new TS TUI run the same live checks.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException, Request

from chaos_agent.models.schemas import JSONEnvelope
from chaos_agent.server.routes import health_router

logger = logging.getLogger(__name__)

# Overall budget for the entire preflight pipeline. Each individual
# kubectl check has its own per-check timeout (~15s), and they run in
# parallel via asyncio.gather, so the worst case without this wrapper
# is ~15s. The TUI boot screen makes the user stare at a black terminal
# during this window — bound the total to something snappier.
#
# Half of the per-check timeout gives slow-but-healthy clusters time to
# answer while still keeping the perceived boot latency under 8s. If
# the budget blows, asyncio.wait_for raises and the endpoint returns
# empty checks; cli.tsx renders the doctor card as "unavailable" and
# the user can re-run /doctor on a per-need basis.
_PREFLIGHT_BUDGET_S = 8.0


@health_router.get("/api/v1/preflight")
async def preflight(req: Request):
    """Run the TUI preflight bundle and emit runtime info."""
    from chaos_agent.config.settings import settings
    from chaos_agent.preflight import expand_kubeconfig_path, run_tui_checks

    try:
        results = await asyncio.wait_for(
            run_tui_checks(), timeout=_PREFLIGHT_BUDGET_S
        )
    except asyncio.TimeoutError:
        # Returning 200 with results=[] would land as "0/0 passed" in
        # the TUI's boot doctor card — a confidently-wrong report. 504
        # routes the TS client to its ``preflight unavailable`` branch
        # so the user gets accurate "couldn't check" UX and a hint to
        # re-run /doctor once the cluster wakes up.
        logger.warning(f"preflight exceeded {_PREFLIGHT_BUDGET_S}s budget")
        raise HTTPException(
            status_code=504,
            detail=f"preflight exceeded {_PREFLIGHT_BUDGET_S}s budget",
        )
    except Exception as e:  # pragma: no cover — defensive
        logger.warning(f"preflight aborted: {e}")
        results = []

    kubeconfig = ""
    try:
        kubeconfig = expand_kubeconfig_path(settings.kubeconfig_path) or ""
    except Exception:
        kubeconfig = settings.kubeconfig_path or ""

    # namespace is per-session, not a global setting — the TUI already
    # has it in its store from createSession; we don't echo it here.
    ctx_max_tokens, _ = settings.resolve_context_budget(settings.model_name)

    return JSONEnvelope.ok(
        data={
            "kubeconfig": kubeconfig,
            "model_name": settings.model_name or "",
            "context_max_tokens": ctx_max_tokens,
            "passed_count": sum(1 for c in results if c.passed),
            "total_count": len(results),
            "checks": [
                {
                    "name": c.name,
                    "severity": c.severity,
                    "passed": c.passed,
                    "message": c.message,
                    "fix": c.fix,
                }
                for c in results
            ],
        },
        request_id=getattr(req.state, "request_id", ""),
    )
