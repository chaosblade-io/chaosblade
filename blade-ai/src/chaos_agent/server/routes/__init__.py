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
# writes ``model_name`` via ConfigStore + rebuilds the in-process
# agents via ``server/agent_runtime.maybe_rebuild_agents``. Common
# case: ``restart_required: false`` (rebuild succeeded, next /turn
# uses the new model). Falls back to ``restart_required: true``
# only on rebuild failure, with the underlying error surfaced in
# ``rebuild_error``.
model_router = APIRouter(prefix="/api/v1/model", tags=["model"])
# Phase 4: Onboarding wizard surface. The TS Ink wizard (UI) calls
# these endpoints to validate user input + persist the final config.
# Business logic lives in ``chaos_agent.config.wizard_validators``;
# this router is a thin HTTP adapter that returns ValidationResult
# verbatim. Persistence reuses ``ConfigStore.save_to_file``.
wizard_router = APIRouter(prefix="/api/v1/wizard", tags=["wizard"])
