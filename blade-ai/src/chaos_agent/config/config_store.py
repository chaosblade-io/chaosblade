"""Configuration store — the write half of the ``~/.blade-ai/config.json`` scheme.

``settings.py`` is the *reader* (pydantic ``BaseSettings`` + ``JsonConfigSettingsSource``);
this module is the matching *writer*, relocated into the ``config`` package so all
config responsibility lives in one place.

Design (the "new scheme"):
- Single source of truth for the path — reuse ``settings._active_config_file()``
  instead of hardcoding the expanduser call, so reader and writer can never
  disagree, and both honour the ``BLADE_AI_CONFIG_DIR`` sandbox override.
- Atomic write via temp file + ``os.replace`` — a config writer must never leave a
  truncated JSON behind on crash. This is cheap and non-negotiable.
- No ``fcntl`` file-locking — the previous POSIX-only lock layer was dropped; the TUI
  is single-writer per process and atomic replace already prevents torn writes.
- Hot/cold key classification — hot keys take effect after ``settings.reload()``;
  cold keys (paths, DB backends, LLM-bound values captured at startup) require a TUI
  restart, and ``set()`` returns ``False`` so the caller can warn the user.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from chaos_agent.config.settings import _active_config_file

logger = logging.getLogger(__name__)

# Keys where a string value should be cast to bool.
_BOOL_KEYS = {
    "confirmation_required",
    "self_evolution",
    "save_system_message",
    "verifier_json_mode",
    "llm_enable_thinking",
    "retry_jitter",
    "replan_auto_trigger",
    "replan_reset_execute_count",
}
# Keys where a string value should be cast to int.
_INT_KEYS = {
    "llm_max_retries",
    "server_port",
    "command_timeout",
    "experiment_timeout",
    "max_agent_loop",
    "max_execute_loop",
    "max_verifier_loop",
    "max_recover_verifier_loop",
    "max_recover_layer1_iterations",
    "recursion_limit",
    "loop_detection_window",
    "loop_detection_turns",
    "stagnation_frequency_ceiling",
    "loop_detection_threshold",
    "idle_turn_threshold",
    "max_replan_count",
    "max_verify_replan_count",
    "retry_max_retries",
    "context_max_tokens",
    "kubectl_max_output_bytes",
    "skill_script_max_output",
    "timeout_blade",
    "timeout_kubectl",
    "timeout_kubectl_exec",
    "llm_connect_timeout",
    "llm_read_timeout",
    "timeout_default",
    "timeout_skill_script",
}
# Float keys.
_FLOAT_KEYS = {
    "llm_temperature",
    "retry_base_delay",
    "retry_max_delay",
    "retry_exponential_base",
    "context_compact_ratio",
}

# Cold keys require a TUI restart to take effect (DB connections, paths
# captured at startup, plugin scan results).
#
# LLM-bound keys are also classified cold: ``make_llm()`` runs once at
# startup and the resulting client is captured in ``app.state.agents``
# / ``runner._llm`` references that ``settings.reload()`` does NOT
# observe. Without this, ``/config set model_name foo`` writes the new
# value to disk and reports ``hot_reload=true`` while the running graph
# happily keeps using the previous LLM — a bug-shaped UX. Until the
# server grows a real LLM-rebuild path, classifying these as cold is
# the honest answer to the user.
_COLD_KEYS = frozenset({
    "tasks_db_backend",
    "tasks_db_path",
    "tasks_pg_dsn",
    "checkpoint_db_path",
    "memory_dir",
    "skills_dir",
    # LLM-bound — captured by ``make_llm()`` at startup, no live
    # rebuild path. Server side reflects this via ``hot_reload=False``
    # in the ``/config`` POST envelope so the TS TUI prints the
    # "restart required" tail.
    "model_name",
    "api_base_url",
    "llm_api_key",
    "llm_max_retries",
    "llm_temperature",
    "llm_enable_thinking",
    "verifier_json_mode",
    # LLM timeout (httpx.Timeout) is baked into the ChatOpenAI client at
    # make_llm() time, so changing it needs a restart like the others.
    "llm_connect_timeout",
    "llm_read_timeout",
})


class ConfigStore:
    """Atomic read/write for ``~/.blade-ai/config.json``."""

    def __init__(self, config_path: str | None = None) -> None:
        # Resolve at construction time (not import) so a BLADE_AI_CONFIG_DIR
        # redirect set before the store is created takes effect — this is
        # what keeps the wizard /save tests from writing the real config.
        self._path = config_path or str(_active_config_file())

    @property
    def path(self) -> str:
        return self._path

    # ── reads ────────────────────────────────────────────────────

    def read_all(self) -> dict[str, Any]:
        """Read the full config dict from disk."""
        if not os.path.isfile(self._path):
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Failed to read config: %s", e)
            return {}

    def get(self, key: str, default: Any = None) -> Any:
        """Return the raw value for ``key``, or ``default`` if absent."""
        return self.read_all().get(key, default)

    def get_display_dict(self) -> dict[str, str]:
        """Return config suitable for display (API key masked)."""
        from chaos_agent.config.settings import settings as s

        return {
            "llm_api_key": "*" * 8 if s.llm_api_key else "(not configured)",
            "model_name": s.model_name,
            "api_base_url": s.api_base_url,
            "kubeconfig_path": s.kubeconfig_path or "(default)",
            "kube_context": s.kube_context or "(auto-detected)",
            "confirmation_required": str(s.confirmation_required),
            "skills_dir": str(s.skills_dir),
            "memory_dir": str(s.memory_dir),
            "log_level": s.log_level,
        }

    # ── writes ───────────────────────────────────────────────────

    def set(self, key: str, value: str) -> bool:
        """Set ``key=value``. Returns True if the key is hot-reloadable.

        Caller may show a "restart required" notice when False is returned.
        """
        existing = self.read_all()
        existing[key] = self._coerce(key, value)
        self._write_atomic(existing)
        self._reload_settings()
        return key not in _COLD_KEYS

    def unset(self, key: str) -> bool:
        """Remove ``key`` from the config. Returns True if the key was present."""
        existing = self.read_all()
        if key not in existing:
            return False
        existing.pop(key, None)
        self._write_atomic(existing)
        self._reload_settings()
        return True

    def set_many(self, updates: dict[str, Any]) -> None:
        """Set multiple keys at once. Caller already passes typed values."""
        existing = self.read_all()
        existing.update(updates)
        self._write_atomic(existing)
        self._reload_settings()

    @staticmethod
    def is_cold_key(key: str) -> bool:
        """True if changing ``key`` requires a TUI restart."""
        return key in _COLD_KEYS

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _coerce(key: str, value: str) -> Any:
        if key in _BOOL_KEYS:
            return value.lower() in ("true", "1", "yes", "on")
        if key in _INT_KEYS:
            return int(value)
        if key in _FLOAT_KEYS:
            return float(value)
        return value

    def _write_atomic(self, data: dict) -> None:
        """Write atomically via tmp + rename so a crash can't truncate the config."""
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, self._path)

    @staticmethod
    def _reload_settings() -> None:
        try:
            from chaos_agent.config.settings import settings
            settings.reload()
        except Exception as e:
            logger.error(f"Failed to reload settings: {e}")
