"""Server-side runtime: rebuild the agent graph after config writes
that touch LLM-bound keys.

``app.state.agents`` is constructed once at FastAPI lifespan startup
with the LLM client captured from whatever ``settings`` was at that
moment. ``ConfigStore.set()`` writes to disk and calls
``settings.reload()`` — but the already-built ``ChatOpenAI`` holds
the api_key / base_url / model_name it was constructed with, not a
live reference. Without rebuilding, ``/turn`` keeps using the old
client even though the user just edited their config.

This module is the single place that does the rebuild dance. Three
routes share it: ``wizard /save``, ``/api/v1/model POST``, and
``/api/v1/config POST``. Before this lived here, two of the three
returned a "restart server" tail because nobody had copy-pasted the
recipe out of the wizard route.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from fastapi import FastAPI

logger = logging.getLogger(__name__)


# Config keys whose change requires rebuilding ``app.state.agents``
# because the LLM client captures them at construction time. Mirrors
# the ``ConfigStore._COLD_KEYS`` subset that's LLM-bound — declared
# independently here because:
#   · From the *user's* perspective these keys are hot (next /turn
#     picks up the change). The ConfigStore classification is the
#     *technical* "captured at startup" fact, separate from the
#     routing layer's "we wrap it transparently" capability.
#   · Importing _COLD_KEYS would couple this module to a frozenset
#     that includes db paths and skills_dir — irrelevant to agent
#     rebuild, would dilute the intent.
LLM_BOUND_KEYS: frozenset[str] = frozenset({
    "llm_api_key",
    "api_base_url",
    "model_name",
    "llm_max_retries",
    "llm_temperature",
    "llm_enable_thinking",
    "verifier_json_mode",
})


async def maybe_rebuild_agents(
    app: FastAPI,
    changed_keys: Iterable[str],
) -> Optional[str]:
    """Rebuild ``app.state.agents`` iff any LLM-bound key changed.

    Returns ``None`` on success (including the "no rebuild needed"
    case), or a stringified error message on failure (logged with
    full traceback at exception level). Callers surface the error
    via their own response envelope.

    Idempotent: ``changed_keys`` can carry any subset of the config
    surface; we filter against ``LLM_BOUND_KEYS`` ourselves and
    silently skip when the intersection is empty.

    Race policy: no in-flight turn protection. The three callers all
    fire from explicit user actions (slash command, wizard step) —
    a concurrent /turn would race the agent swap, tolerable because
    the user just deliberately changed something and expects the
    next turn to behave differently.
    """
    needed = set(changed_keys) & LLM_BOUND_KEYS
    if not needed:
        return None

    registry = getattr(app.state, "skill_registry", None)
    if registry is None:
        # Defensive — production lifespan always installs the
        # registry before serving routes, but tests sometimes spin
        # up a bare app. Skipping is safer than crashing here.
        msg = "skill_registry not on app.state; agent rebuild skipped"
        logger.warning(msg)
        return msg

    try:
        from chaos_agent.agent.factory import create_agent

        new_agents = await create_agent(registry)
        # NOTE: we don't close the previous ``checkpointer_conn`` /
        # ``checkpointer`` here even though we drop the only reference.
        # An in-flight ``/turn`` captured the OLD compiled graph at
        # request start; that graph closes over the OLD checkpointer
        # via LangGraph's compile-time binding. Closing the old conn
        # now would break the running turn's persistence layer
        # mid-stream. Letting GC handle the dropped reference is the
        # safer trade — the leak is bounded (1 sqlite fd per rebuild,
        # typically 0-2 rebuilds per process) and the lifespan
        # shutdown hook closes the LATEST one cleanly on exit.
        app.state.agents = new_agents
        # Keep ``app.state.checkpointer`` in sync — sessions/turn
        # routes read it as a direct alias for convenience and
        # would otherwise hold a stale handle to the previous
        # checkpointer that the new ``agents`` no longer references.
        cp = new_agents.get("checkpointer")
        if cp is not None:
            app.state.checkpointer = cp
        logger.info(
            "Agents rebuilt after config change (keys=%s)",
            sorted(needed),
        )
        return None
    except Exception as e:
        logger.exception("Agent rebuild failed (keys=%s)", sorted(needed))
        return f"{type(e).__name__}: {e}"
