"""``/api/v1/config`` — TS TUI configuration read/write endpoints.

The Python TUI manipulates ``~/.blade-ai/config.json`` directly via
``ConfigStore`` (``chaos_agent/tui/config_store.py``). The TS TUI runs
out-of-process and has no filesystem access to the user's home, so the
server proxies the same operations.

Surface mirrors Python's ``/config`` slash:
  - ``GET  /api/v1/config``        — read the display dict (api key masked).
  - ``POST /api/v1/config/{key}``  — set ``key`` to the value in the body.
  - ``DELETE /api/v1/config/{key}``— unset ``key`` (revert to default).

Why a write whitelist:
  ConfigStore.set will happily write any key to the JSON file — including
  ``llm_api_key``, ``tasks_pg_dsn``, and other secrets / DSNs. Exposing
  the unfiltered surface over HTTP would let a malformed request leak
  or rotate credentials. We allow only the fields a user might
  reasonably tweak from a TUI session — model knobs, timeouts, replan
  budgets, k8s targeting. Secrets and connection strings stay out.
"""

from __future__ import annotations

import logging

from fastapi import Body, Request

from chaos_agent.models.schemas import JSONEnvelope, ResponseCode
from chaos_agent.server.routes import config_router
from chaos_agent.tui.config_store import ConfigStore

logger = logging.getLogger(__name__)


# Whitelist of keys writable via the HTTP API. Strict opt-in so adding
# a new sensitive setting never accidentally becomes remotely writable.
# Mirrors the keys the Python TUI's ``/config`` users actually edit
# (model knobs, timeouts, replan budgets, k8s targets).
_WRITABLE_KEYS: frozenset[str] = frozenset(
    {
        # Model + LLM knobs.
        "model_name",
        "api_base_url",
        "llm_temperature",
        "llm_max_retries",
        "llm_enable_thinking",
        "verifier_json_mode",
        # User-facing behaviour.
        "confirmation_required",
        "self_evolution",
        "log_level",
        # K8s targeting.
        "kubeconfig_path",
        "kube_context",
        # Timeouts.
        "timeout_blade",
        "timeout_kubectl",
        "timeout_kubectl_exec",
        "llm_connect_timeout",
        "llm_read_timeout",
        "timeout_default",
        # Loop / recursion budgets.
        "max_agent_loop",
        "max_execute_loop",
        "max_verifier_loop",
        "max_recover_verifier_loop",
        "recursion_limit",
        # Replan tuning.
        "max_replan_count",
        "replan_auto_trigger",
        "replan_reset_execute_count",
        # Loop detection.
        "loop_detection_window",
        "loop_detection_threshold",
        "idle_turn_threshold",
    }
)


def _store() -> ConfigStore:
    """Single-shot factory. The store is stateless beyond the path it
    locks on, so making one per request is cheap and avoids a
    process-global that the test suite would have to monkey-patch."""
    return ConfigStore()


@config_router.get("")
async def read_config(req: Request):
    """Return the masked display config + the resolved file path.

    ``api_key`` is asterisked by ``get_display_dict``; any new field
    added there inherits the same masking. The path is included so
    ``/config path`` on the TS side renders without a second call.
    """
    store = _store()
    return JSONEnvelope.ok(
        data={
            "config": store.get_display_dict(),
            "config_path": store.path,
        },
        request_id=getattr(req.state, "request_id", ""),
    )


@config_router.post("/{key}")
async def write_config(
    key: str,
    req: Request,
    payload: dict = Body(...),
):
    """Set ``key`` to ``payload['value']``. Whitelist gated.

    The body is intentionally a dict instead of a typed schema —
    coercion lives in ``ConfigStore._coerce`` (string → bool/int/float
    based on the per-key sets). The TS client always sends strings;
    let the canonical coercion path own the cast.
    """
    if key not in _WRITABLE_KEYS:
        # 422 reads as "well-formed request, semantically rejected"
        # which fits a whitelist refusal better than a 403 (auth) or
        # 400 (malformed). Body-level validation errors elsewhere in
        # the API also use this code.
        return JSONEnvelope.fail(
            code=ResponseCode.INVALID_PARAMS,
            message=f"key '{key}' is not writable via the HTTP API",
            request_id=getattr(req.state, "request_id", ""),
        )
    raw_value = payload.get("value")
    if raw_value is None:
        return JSONEnvelope.fail(
            code=ResponseCode.INVALID_PARAMS,
            message="body must include a 'value' field",
            request_id=getattr(req.state, "request_id", ""),
        )
    # ConfigStore.set accepts strings only — same contract as the
    # Python TUI's ``/config set`` slash.
    value_str = raw_value if isinstance(raw_value, str) else str(raw_value)
    store = _store()
    try:
        is_hot = store.set(key, value_str)
    except (ValueError, TypeError) as e:
        # Coercion failure (e.g. ``set max_replan_count abc``).
        return JSONEnvelope.fail(
            code=ResponseCode.INVALID_PARAMS,
            message=f"value for '{key}' is not the expected type: {e}",
            request_id=getattr(req.state, "request_id", ""),
        )
    except Exception as e:  # pragma: no cover — disk / lock failures
        logger.exception("config write failed for key=%s", key)
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message=f"failed to write config: {e}",
            request_id=getattr(req.state, "request_id", ""),
        )

    # If the just-written key is LLM-bound (model_name / api_base_url /
    # llm_api_key / etc.) the in-process agents need rebuilding so the
    # next /turn picks up the change. ``ConfigStore`` itself classifies
    # those keys as "cold" because the LLM client captures them at
    # construct time; ``maybe_rebuild_agents`` is what flips them to
    # effectively-hot for HTTP callers. Non-LLM keys (timeouts, replan
    # budgets, …) are no-ops here.
    from chaos_agent.server.agent_runtime import (
        LLM_BOUND_KEYS,
        maybe_rebuild_agents,
    )

    rebuild_error = await maybe_rebuild_agents(req.app, [key])

    # Read back the canonical value so the TS side renders what
    # actually landed on disk (including type coercion).
    coerced = store.get(key)
    # ``hot_reload`` is the USER-FACING field: True when the change
    # is effective without a restart. Two paths:
    #   · ``is_hot`` (ConfigStore says settings.reload covers it)
    #   · the key is LLM-bound AND ``maybe_rebuild_agents`` succeeded
    # Critically NOT "rebuild_error is None" alone — the helper
    # returns None when it short-circuits on non-LLM-bound keys, so
    # using None-as-success would silently mark a hypothetical
    # future non-LLM cold key (e.g. a db path added to the whitelist)
    # as hot when in fact it needs a restart.
    effective = is_hot or (key in LLM_BOUND_KEYS and rebuild_error is None)
    return JSONEnvelope.ok(
        data={
            "key": key,
            "value": coerced,
            "hot_reload": effective,
            "rebuild_error": rebuild_error,
        },
        request_id=getattr(req.state, "request_id", ""),
    )


@config_router.delete("/{key}")
async def unset_config(key: str, req: Request):
    """Remove ``key`` so the next read falls back to the default.

    Same whitelist as POST — ``DELETE /api/v1/config/llm_api_key``
    would otherwise let a caller silently rotate credentials by
    forcing a re-read of the env var.
    """
    if key not in _WRITABLE_KEYS:
        return JSONEnvelope.fail(
            code=ResponseCode.INVALID_PARAMS,
            message=f"key '{key}' is not writable via the HTTP API",
            request_id=getattr(req.state, "request_id", ""),
        )
    store = _store()
    try:
        was_present = store.unset(key)
    except Exception as e:  # pragma: no cover
        logger.exception("config unset failed for key=%s", key)
        return JSONEnvelope.fail(
            code=ResponseCode.INTERNAL_ERROR,
            message=f"failed to unset config: {e}",
            request_id=getattr(req.state, "request_id", ""),
        )

    # Same agent-rebuild contract as POST: an unset on an LLM-bound
    # key reverts settings to the code default (e.g. unset
    # ``model_name`` → ``qwen3.6-max-preview``), but the captured-at-
    # startup ``ChatOpenAI`` still holds the previously-set value.
    # Without this rebuild, the user gets a `hot_reload=false` envelope
    # AND a stale in-process client — POST handles that, DELETE used
    # to drop the rebuild on the floor.
    from chaos_agent.server.agent_runtime import (
        LLM_BOUND_KEYS,
        maybe_rebuild_agents,
    )

    rebuild_error = await maybe_rebuild_agents(req.app, [key])
    effective = (
        not ConfigStore.is_cold_key(key)
        or (key in LLM_BOUND_KEYS and rebuild_error is None)
    )
    return JSONEnvelope.ok(
        data={
            "key": key,
            "was_present": was_present,
            "hot_reload": effective,
            "rebuild_error": rebuild_error,
        },
        request_id=getattr(req.state, "request_id", ""),
    )
