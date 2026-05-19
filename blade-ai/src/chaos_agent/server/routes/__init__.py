"""API routes for the Blade AI Server."""

from fastapi import APIRouter

# Create routers for each command
inject_router = APIRouter(prefix="/api/v1", tags=["inject"])
recover_router = APIRouter(prefix="/api/v1", tags=["recover"])
metric_router = APIRouter(prefix="/api/v1", tags=["metric"])
skills_router = APIRouter(prefix="/api/v1", tags=["skills"])
confirm_router = APIRouter(prefix="/api/v1", tags=["confirm"])
health_router = APIRouter(tags=["health"])
# Phase 3a: TS TUI control-plane endpoints. Each prefix is its own
# router so the OpenAPI tags stay clean and the route file imports
# stay one-purpose.
config_router = APIRouter(prefix="/api/v1/config", tags=["config"])
memory_router = APIRouter(prefix="/api/v1/memory", tags=["memory"])
# Phase 3c.1: model selection. ``GET`` lists candidates, ``POST``
# writes ``model_name`` via ConfigStore. ``model_name`` is a cold
# key (see ``tui/config_store._COLD_KEYS``) so a successful POST
# always reports ``restart_required: true`` — the in-process LLM
# instance was captured at startup and doesn't observe
# settings.reload(). Honest "restart needed" beats a misleading
# silent-no-op.
model_router = APIRouter(prefix="/api/v1/model", tags=["model"])
