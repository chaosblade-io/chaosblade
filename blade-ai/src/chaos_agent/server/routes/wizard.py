"""``/api/v1/wizard`` — onboarding wizard endpoints for the TS Ink UI.

Architecture:
  · The TS wizard (Ink) is a pure UI layer. It does NOT import openai,
    kubectl, or any business logic.
  · This router exposes the validation + persistence surface the wizard
    needs over HTTP, with all logic delegated to
    ``chaos_agent.config.wizard_validators`` (single source of truth
    shared with the legacy Python Rich wizard) and
    ``chaos_agent.config.config_store.ConfigStore`` (existing canonical
    config writer).

Endpoints:
  ·  GET  /api/v1/wizard/model-presets        — recommended LLM list
  ·  POST /api/v1/wizard/validate/url         — shape-check api_base_url
  ·  POST /api/v1/wizard/validate/api-key     — live models.list() probe
  ·  POST /api/v1/wizard/validate/kubeconfig  — path check + ctx discovery
  ·  POST /api/v1/wizard/save                 — write the assembled
                                                config dict to disk

Why a separate wizard router (vs reusing ``/api/v1/config``):
  ``/api/v1/config`` is per-key set/get and assumes the keys already
  exist. The wizard's payloads are validation requests (URL + key +
  model triples that need a *live* test), so they don't map cleanly
  onto per-key writes. The save endpoint at the end IS a series of
  per-key writes — but bundling them in one call avoids 6 round-trips
  and lets the server stop on the first failure.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import Body, Request

from chaos_agent.config import wizard_validators
from chaos_agent.config.settings import settings
from chaos_agent.models.schemas import JSONEnvelope, ResponseCode
from chaos_agent.server.routes import wizard_router
from chaos_agent.config.config_store import ConfigStore

logger = logging.getLogger(__name__)


# Same whitelist as ``/api/v1/config`` — wizard-driven saves must not
# be a back-door around the per-key write guard. Kept inline (not
# imported from config.py) so the two routes evolve independently.
_SAVABLE_KEYS: frozenset[str] = frozenset(
    {
        "model_name",
        "api_base_url",
        "llm_api_key",  # wizard explicitly writes this; the per-key
                       # route refuses it on purpose. The wizard path
                       # is the canonical entry for first-run secret
                       # capture.
        "github_token",  # same rationale: optional wizard
                       # step, secret, doubles as the issue-report
                       # on/off switch.
        "kubeconfig_path",
        "kube_context",
        "confirmation_required",
    }
)


# First-tier discoverability seeds (product decision 2026-08): the
# wizard historically persisted only the 7 user-filled keys above, so
# users never learned these safety/context knobs exist. After every
# successful save, keys listed here are written with their Settings
# defaults — but ONLY when absent, so re-running the wizard never
# overwrites a value the user already customized. Deliberately NOT
# seeded: llm_enable_thinking — thinking must stay on by default for
# injection-command quality, so the toggle is not user-discoverable.
# ``model_budgets`` is handled separately at seed time (personalized
# with the user's configured model — see /save below), not here.
_WIZARD_SEED_DEFAULTS: dict[str, Any] = {
    "context_max_tokens": 100_000,
    "context_compact_ratio": 0.85,
    "llm_thinking_format": "auto",
    "safety_blacklist_namespaces": "",
    "experiment_timeout": 300,
    "confirmation_required": False,
    "kube_connection_mode": "kubeconfig",
    "max_agent_loop": 100,
}


def _store() -> ConfigStore:
    """Fresh ConfigStore — same pattern as routes/config.py."""
    return ConfigStore()


def seed_missing_defaults(store: ConfigStore) -> list[str]:
    """Absent-only seed shared by wizard ``/save`` and boot backfill.

    Writes the first-tier defaults (plus a personalized ``model_budgets``
    entry) for keys still ABSENT from the file and returns the keys
    actually written. A value the user saved earlier — this run or a
    previous one — always wins over the seed.
    """
    current = store.read_all()
    # A non-dict file means corrupt / foreign content — seeding must
    # never touch it (the boot-time caller pre-checks health; /save
    # inherits the gate from this shared guard).
    if not isinstance(current, dict):
        return []
    seeded = [key for key in _WIZARD_SEED_DEFAULTS if key not in current]
    updates: dict[str, Any] = {
        key: _WIZARD_SEED_DEFAULTS[key] for key in seeded
    }

    # Personalized model_budgets: instead of a static example, seed an
    # entry keyed by the user's own configured model, valued via the
    # same resolver the runtime uses (built-in table when the model is
    # known, global fallback otherwise). Users see their model name
    # with real numbers — edit-in-place, no format to guess.
    model_name = str(current.get("model_name") or "").strip()
    if "model_budgets" not in current and model_name:
        max_tokens, ratio = settings.resolve_context_budget(model_name)
        updates["model_budgets"] = {
            model_name: {"max_tokens": max_tokens, "compact_ratio": ratio},
        }
        seeded.append("model_budgets")

    if updates:
        store.set_many(updates)
    return seeded


def backfill_seed_defaults() -> tuple[list[str], list[str]]:
    """Boot-time silent backfill for PRE-EXISTING config files.

    Old installs upgraded past the seed feature never see the wizard
    again (essential fields complete → the needs-setup gate stays
    false), so their config.json would never gain the first-tier keys.
    This closes that gap at server startup — only when the wizard will
    NOT fire; a wizard-bound boot gets its seeds from ``/save`` and
    must not be raced.

    Returns ``(seeded, materialized)``: keys written with curated
    defaults, and essential keys materialized from the environment.

    Skip conditions (any one → return ``([], [])`` without writing):
    1. config file corrupt — the boot error page owns that file
    2. config file absent — pure-env-var users get no file fabricated
    3. essential fields missing — the wizard fires and ``/save`` seeds
    """
    if wizard_validators.check_config_file_health():
        return [], []
    store = _store()
    if not os.path.isfile(store.path):
        return [], []
    if wizard_validators.missing_essential_config():
        return [], []

    # Env materialization: a user configured via env vars has a file
    # that LACKS the essential keys even though the values are live at
    # runtime (env > file precedence). Seed them from the environment
    # — never with defaults — so (a) the file states what actually
    # runs and (b) the personalized model_budgets entry below keys on
    # the model that is genuinely in the file instead of dangling on
    # an env-only value. Absent-only: file values are never clobbered,
    # and env always retains its higher runtime precedence anyway.
    materialized: list[str] = []
    current = store.read_all()
    for field_name, env_name in wizard_validators.ESSENTIAL_CONFIG_FIELDS:
        env_val = (os.environ.get(env_name) or "").strip()
        if env_val and field_name not in current:
            store.set(field_name, env_val)
            materialized.append(field_name)

    return seed_missing_defaults(store), materialized


# ── Read-only ──────────────────────────────────────────────────────────


@wizard_router.get("/needs-setup")
async def needs_setup(req: Request):
    """Determine whether the onboarding wizard should fire.

    Three possible outcomes:

    1. **File absent** (first-time user) → ``needs_setup=true``,
       ``missing=[...]``, ``config_error=null`` → wizard fires.
    2. **File present & valid JSON but missing essential keys** →
       ``needs_setup=true``, ``missing=[...]``, ``config_error=null``
       → wizard fires.
    3. **File present but corrupt / unparseable** →
       ``needs_setup=false``, ``missing=[]``, ``config_error="..."``
       → front-end shows a startup-failure page with the parse error.
       We return ``needs_setup=false`` so the UI neither enters the
       wizard nor starts a session; instead it renders the config_error
       to the user.

    Liveness (does the URL actually answer?) is intentionally NOT
    checked here — that's the runtime self-check's job and the wizard's
    per-step validators. Boot stays fast.
    """
    config_error = wizard_validators.check_config_file_health()
    if config_error:
        return JSONEnvelope.ok(
            data={"needs_setup": False, "missing": [], "config_error": config_error},
            request_id=getattr(req.state, "request_id", ""),
        )
    missing = wizard_validators.missing_essential_config()
    return JSONEnvelope.ok(
        data={"needs_setup": len(missing) > 0, "missing": missing, "config_error": None},
        request_id=getattr(req.state, "request_id", ""),
    )


@wizard_router.get("/model-presets")
async def get_model_presets(req: Request):
    """Return the wizard's recommended-models radio list.

    The TS wizard renders these as the first 5 options of the model
    step; "[F] custom" is added by the TS side as a UI affordance.
    Pulling the list at runtime means adding/removing a recommended
    model is a Python-only change — no TS release needed.
    """
    return JSONEnvelope.ok(
        data={"presets": wizard_validators.get_model_presets()},
        request_id=getattr(req.state, "request_id", ""),
    )


# ── Validators ─────────────────────────────────────────────────────────


@wizard_router.post("/validate/url")
async def validate_url(req: Request, payload: dict = Body(...)):
    """Shape-check the api_base_url. No network call."""
    url = payload.get("url")
    if url is not None and not isinstance(url, str):
        return JSONEnvelope.fail(
            code=ResponseCode.INVALID_PARAMS,
            message="'url' must be a string",
            request_id=getattr(req.state, "request_id", ""),
        )
    result = await wizard_validators.validate_api_url(url)
    return JSONEnvelope.ok(
        data=result.to_dict(),
        request_id=getattr(req.state, "request_id", ""),
    )


@wizard_router.post("/validate/api-key")
async def validate_api_key(req: Request, payload: dict = Body(...)):
    """Live probe — runs models.list() against the supplied base_url.

    Payload: ``{api_key: str, base_url: str, model?: str}``.
    The ``model`` field, when provided, flips ``metadata.has_target``
    based on whether the endpoint's model list contains it. Lets the
    TS wizard show "supports your chosen model" inline.
    """
    api_key = payload.get("api_key")
    base_url = payload.get("base_url")
    model = payload.get("model")
    for name, val in (("api_key", api_key), ("base_url", base_url)):
        if val is not None and not isinstance(val, str):
            return JSONEnvelope.fail(
                code=ResponseCode.INVALID_PARAMS,
                message=f"'{name}' must be a string",
                request_id=getattr(req.state, "request_id", ""),
            )
    if model is not None and not isinstance(model, str):
        return JSONEnvelope.fail(
            code=ResponseCode.INVALID_PARAMS,
            message="'model' must be a string when provided",
            request_id=getattr(req.state, "request_id", ""),
        )
    result = await wizard_validators.validate_api_key(
        api_key=api_key, base_url=base_url, model=model,
    )
    return JSONEnvelope.ok(
        data=result.to_dict(),
        request_id=getattr(req.state, "request_id", ""),
    )


@wizard_router.post("/validate/kubeconfig")
async def validate_kubeconfig(req: Request, payload: dict = Body(...)):
    """Path existence + kube-context discovery.

    Discovered contexts come back in ``metadata.contexts`` so the TS
    wizard can populate the next radio step without a separate call.
    """
    path = payload.get("path")
    if path is not None and not isinstance(path, str):
        return JSONEnvelope.fail(
            code=ResponseCode.INVALID_PARAMS,
            message="'path' must be a string",
            request_id=getattr(req.state, "request_id", ""),
        )
    result = await wizard_validators.validate_kubeconfig(path)
    return JSONEnvelope.ok(
        data=result.to_dict(),
        request_id=getattr(req.state, "request_id", ""),
    )


# ── Persistence ────────────────────────────────────────────────────────


@wizard_router.post("/save")
async def save_config(req: Request, payload: dict = Body(...)):
    """Persist the wizard's accumulated config dict to disk.

    Payload: ``{config: {key: value, ...}}``. Keys outside
    ``_SAVABLE_KEYS`` are silently ignored — keeps a malformed TS
    payload from poisoning the file. Type coercion is delegated to
    ``ConfigStore.set`` (string → bool/int/float per its per-key sets).

    Returns ``{saved_keys: [...], saved_path: "..."}`` on success.
    """
    config = payload.get("config")
    if not isinstance(config, dict):
        return JSONEnvelope.fail(
            code=ResponseCode.INVALID_PARAMS,
            message="body must include a 'config' dict",
            request_id=getattr(req.state, "request_id", ""),
        )

    store = _store()
    saved: list[str] = []
    errors: dict[str, str] = {}
    for key, raw_value in config.items():
        if key not in _SAVABLE_KEYS:
            continue
        if raw_value is None or raw_value == "":
            # Skip empty values rather than overwriting with empty
            # strings — the legacy Python wizard treats empty as
            # "leave unchanged" and we match that semantic so a user
            # who skips an optional field doesn't lose an existing
            # value.
            continue
        value_str = raw_value if isinstance(raw_value, str) else str(raw_value)
        try:
            store.set(key, value_str)
            saved.append(key)
        except (ValueError, TypeError) as e:
            errors[key] = str(e)
        except Exception as e:  # pragma: no cover — disk / lock failures
            logger.exception("wizard save failed for key=%s", key)
            errors[key] = f"{type(e).__name__}: {e}"

    if errors and not saved:
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message=f"all writes failed: {errors}",
            request_id=getattr(req.state, "request_id", ""),
        )

    # Discoverability seed: write curated first-tier defaults for keys
    # still ABSENT from the file. Absent-only by design — a value the
    # user just saved above (or customized in a previous run) always
    # wins over the seed. Shared with the boot-time backfill so both
    # paths seed identically (see ``seed_missing_defaults``).
    seeded = seed_missing_defaults(store)

    # Rebuild agents so the just-saved LLM-bound keys take effect on
    # the next /turn — see ``server/agent_runtime`` for the rationale.
    # Wizard flow runs at boot, BEFORE /turn is reachable, so no race.
    from chaos_agent.server.agent_runtime import maybe_rebuild_agents

    rebuild_error = await maybe_rebuild_agents(req.app, saved)

    return JSONEnvelope.ok(
        data={
            "saved_keys": saved,
            "seeded_keys": seeded,  # discoverability defaults added
            "saved_path": store.path,
            "errors": errors,  # partial-failure surface
            "agent_rebuild_error": rebuild_error,
        },
        request_id=getattr(req.state, "request_id", ""),
    )
