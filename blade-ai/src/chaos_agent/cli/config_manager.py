"""Unified config manager: persistent storage for all CLI configuration.

Configuration is stored in ~/.blade-ai/config.json and persists across CLI invocations.

This replaces the former mode_manager.py (mode.json) and .env file approach.

File format:
{
  "mode": "local" | "server",
  "server_url": "http://host:port",   // only when mode == "server"
  "llm_api_key": "...",
  "model_name": "...",
  ...other settings fields...
}
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.path.expanduser("~/.blade-ai"))
CONFIG_FILE = CONFIG_DIR / "config.json"

# Legacy mode file for migration
MODE_FILE = CONFIG_DIR / "mode.json"

LOCAL = "local"
SERVER = "server"

# Keys that are considered sensitive and should be masked in output
SENSITIVE_KEYS = {"llm_api_key"}

# Default configuration values (user-facing settings only)
DEFAULTS: dict[str, Any] = {
    "mode": LOCAL,
    "server_url": None,
    "llm_api_key": "",
    "model_name": "qwen3.6-max-preview",
    "api_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "llm_temperature": 0.7,
    "llm_max_retries": 3,
    "server_port": 8089,
    "server_host": "0.0.0.0",
    "skills_dir": "~/.blade-ai/skills",
    "memory_dir": "~/.blade-ai/memory",
    "confirmation_required": True,
    "blade_path": "",
    "kubectl_path": "kubectl",
    "kubeconfig_path": "",
    "kube_context": "",
    "command_timeout": 60,
    "log_level": "DEBUG",
    "self_evolution": False,
}


def _migrate_mode_json() -> None:
    """Migrate legacy mode.json to config.json if config.json does not exist yet."""
    if CONFIG_FILE.exists():
        return
    if not MODE_FILE.exists():
        return
    try:
        mode_data = json.loads(MODE_FILE.read_text(encoding="utf-8"))
        config_data = {**DEFAULTS}
        # Merge mode data
        if "mode" in mode_data:
            config_data["mode"] = mode_data["mode"]
        if "server_url" in mode_data:
            config_data["server_url"] = mode_data["server_url"]
        # Write config.json
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(config_data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_FILE)
        # Remove legacy mode.json
        MODE_FILE.unlink()
        logger.info("Migrated mode.json to config.json")
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to migrate mode.json: {e}")


def _read() -> dict:
    """Read the config file. Returns empty dict if not found."""
    _migrate_mode_json()
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read config file: {e}")
        return {}


def _write(data: dict) -> None:
    """Write the config file atomically."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def get_config(key: str) -> Any:
    """Get a single config value. Returns default if key not set."""
    data = _read()
    if key in data:
        return data[key]
    return DEFAULTS.get(key)


def set_config(key: str, value: Any) -> None:
    """Set a single config value (merge semantics: other keys preserved)."""
    data = _read()
    # If there are keys not yet in the file, seed from defaults
    for k, v in DEFAULTS.items():
        if k not in data:
            data[k] = v
    data[key] = value
    _write(data)


def list_config() -> dict:
    """Return all config values, with defaults filling in missing keys."""
    data = {**DEFAULTS, **_read()}
    return data


def get_mode() -> str:
    """Get current execution mode. Defaults to LOCAL."""
    return get_config("mode") or LOCAL


def get_server_url() -> Optional[str]:
    """Get the configured server URL. Returns None if not in server mode or no URL."""
    data = _read()
    if data.get("mode") != SERVER:
        return None
    return data.get("server_url")


def get_backend():
    """Get the appropriate backend (AgentRunner or AgentClient) based on current mode.

    Returns:
        - AgentRunner instance if local mode
        - AgentClient instance if server mode
    """
    mode = get_mode()
    if mode == SERVER:
        from chaos_agent.cli.client import AgentClient

        url = get_server_url()
        if not url:
            raise RuntimeError("Server mode is set but no server URL configured. Run: blade-ai config set mode server <url>")
        return AgentClient(base_url=url)
    else:
        from chaos_agent.cli.runner import AgentRunner

        return AgentRunner()
