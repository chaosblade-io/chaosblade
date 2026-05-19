"""``/api/v1/model`` — list candidate LLMs + switch the active one.

Surface mirrors Python TUI's ``/model`` slash:
  - ``GET  /api/v1/model``   → ``{active, candidates}`` so the TS TUI
                                can show a list with a marker on the
                                currently-selected entry.
  - ``POST /api/v1/model``   → ``{model_name}`` body; persists the new
                                value via ConfigStore. ``model_name`` is
                                a cold key (see Phase 3a's
                                ``_COLD_KEYS`` expansion), so the
                                response always carries
                                ``restart_required: true`` and the TS
                                handler renders that explicitly.

Why no live hot-swap:
  ``make_llm()`` runs once at startup and the resulting client is
  baked into every compiled LangGraph node. Replacing ``settings
  .model_name`` and rebuilding ``app.state.agents["llm"]`` would
  miss the closure references inside the inject / recover graphs —
  the running process keeps using the old client. Until the agent
  factory grows a swappable-LLM indirection, the honest answer is
  "writes saved, restart to apply".

Candidates list:
  Hardcoded curated set covering the providers we routinely see
  (Qwen / OpenAI / DeepSeek / Anthropic). The list is advisory —
  ``POST`` accepts any non-empty string so users with private
  deployments can point at their own model identifiers without
  patching this file. The ``custom`` field on each candidate
  signals to the renderer "this is one we tested" vs literal
  passthrough.
"""

from __future__ import annotations

import logging

from fastapi import Body, Request

from chaos_agent.models.schemas import JSONEnvelope, ResponseCode
from chaos_agent.server.routes import model_router

logger = logging.getLogger(__name__)


# Curated list. Order matters — UI renders top-to-bottom, so we lead
# with what most operators will pick first. Each entry is just a
# string id; descriptions / pricing belong in product docs, not in
# the hot path of an LLM-backed chaos tool.
_CANDIDATES: list[dict] = [
    {"id": "qwen3.6-max-preview", "provider": "qwen", "note": "current default"},
    {"id": "qwen-max", "provider": "qwen"},
    {"id": "qwen-plus", "provider": "qwen"},
    {"id": "qwen-turbo", "provider": "qwen"},
    {"id": "qwen-long", "provider": "qwen"},
    {"id": "deepseek-chat", "provider": "deepseek"},
    {"id": "deepseek-reasoner", "provider": "deepseek"},
    {"id": "gpt-4o", "provider": "openai"},
    {"id": "gpt-4o-mini", "provider": "openai"},
    {"id": "gpt-4-turbo", "provider": "openai"},
    # Claude 4.X family — IDs valid as of 2026-05. Anthropic
    # publishes new dated suffixes occasionally (e.g.
    # ``claude-haiku-4-5-20251001``); the friendly name resolves
    # to the latest snapshot via the OpenAI-compatible proxy that
    # most operators front Anthropic with.
    {"id": "claude-opus-4-7", "provider": "anthropic"},
    {"id": "claude-sonnet-4-6", "provider": "anthropic"},
    {"id": "claude-haiku-4-5", "provider": "anthropic"},
]


@model_router.get("")
async def read_model(req: Request):
    """List the active model + curated candidates.

    Returns the active model name as a separate field so the TS
    handler doesn't have to scan ``candidates`` for a match — and so
    a user-set model that isn't in the curated list still surfaces
    in ``active``.
    """
    from chaos_agent.config.settings import settings as s

    return JSONEnvelope.ok(
        data={
            "active": s.model_name or "",
            "api_base_url": s.api_base_url or "",
            "candidates": _CANDIDATES,
        },
        request_id=getattr(req.state, "request_id", ""),
    )


@model_router.post("")
async def write_model(req: Request, payload: dict = Body(...)):
    """Set ``model_name`` to ``payload['model_name']``.

    Whitelist: any non-empty string. Users with private model gateways
    must be free to point at their own identifiers; rejecting
    out-of-list names would break that flow.

    Persistence: routes through ``ConfigStore.set("model_name", v)``
    — same path the Python TUI's ``/config set model_name v`` uses,
    so ``settings.reload()`` runs identically and any future cold-key
    classification change picks this up automatically.
    """
    from chaos_agent.tui.config_store import ConfigStore

    raw = payload.get("model_name")
    if not isinstance(raw, str) or not raw.strip():
        return JSONEnvelope.fail(
            code=ResponseCode.INVALID_PARAMS,
            message="body must include a non-empty 'model_name' string",
            request_id=getattr(req.state, "request_id", ""),
        )
    name = raw.strip()
    store = ConfigStore()
    try:
        # ``set`` returns True when the key is hot (not cold). For
        # ``model_name`` this is always False because we made it cold
        # in Phase 3a. We call through anyway so the path stays
        # canonical and any later un-cold-ing (when LLM hot-swap
        # lands) takes effect without touching this route.
        is_hot = store.set("model_name", name)
    except Exception as e:  # pragma: no cover — disk / lock failures
        logger.exception("model write failed")
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message=f"failed to write model_name: {e}",
            request_id=getattr(req.state, "request_id", ""),
        )
    return JSONEnvelope.ok(
        data={
            "active": name,
            "restart_required": not is_hot,
            "next_action": (
                "Restart blade-ai-server to load the new model"
                if not is_hot
                else "Hot-swap applied — next turn uses the new model"
            ),
        },
        request_id=getattr(req.state, "request_id", ""),
    )
