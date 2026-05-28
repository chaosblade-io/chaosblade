"""mcp.json parser + validator.

Reads ``~/.blade-ai/mcp.json`` (Claude Desktop format + blade-ai
extensions), interpolates ``${ENV_VAR}`` references in env/headers,
returns a list of validated ``McpServerConfig`` instances.

Returns empty list when the file is missing or ``mcp_enabled=False``
in settings — callers treat that as "no MCP servers", not as an
error.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


class McpConfigError(ValueError):
    """Raised when mcp.json fails validation (bad shape, missing env, ...)."""


_ALLOWED_PHASES: frozenset[str] = frozenset({
    "clarification", "phase1", "phase2", "verifier",
})
_ALLOWED_TRANSPORTS: frozenset[str] = frozenset({"stdio", "http"})

# ${VAR} or ${VAR_NAME_123}
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    transport: str                                # "stdio" | "http"
    command: str | None
    args: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None                        # Claude Desktop compat
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    attach_to: tuple[str, ...] = ()
    enabled: bool = True
    timeout_seconds: int = 30


def _interpolate(value: str, env_overrides: dict[str, str]) -> str:
    """Resolve ``${VAR}`` references against env_overrides ∪ os.environ.

    env_overrides wins on conflict. A MISSING variable raises
    ``McpConfigError`` rather than silent substitution — a missing
    auth token must fail loudly at load time, not be silently set to
    ``""`` which would let the request go out unauthenticated.
    """
    def _replace(match: re.Match) -> str:
        var = match.group(1)
        if var in env_overrides:
            return env_overrides[var]
        if var in os.environ:
            return os.environ[var]
        raise McpConfigError(
            f"environment variable ${{{var}}} referenced in mcp.json "
            f"is not defined; refusing silent fallback to empty string"
        )

    return _ENV_REF_RE.sub(_replace, value)


def _parse_one(name: str, raw: dict) -> McpServerConfig:
    if not isinstance(raw, dict):
        raise McpConfigError(f"server '{name}' must be a JSON object")

    transport = str(raw.get("transport", "stdio")).lower()
    if transport not in _ALLOWED_TRANSPORTS:
        raise McpConfigError(
            f"server '{name}' transport='{transport}' not in {sorted(_ALLOWED_TRANSPORTS)}"
        )

    enabled = bool(raw.get("enabled", True))
    timeout = int(raw.get("timeout_seconds", 30))

    attach_to_raw = raw.get("attach_to", [])
    if not isinstance(attach_to_raw, list):
        raise McpConfigError(f"server '{name}' attach_to must be a list")
    for phase in attach_to_raw:
        if phase not in _ALLOWED_PHASES:
            raise McpConfigError(
                f"server '{name}' attach_to entry '{phase}' not in {sorted(_ALLOWED_PHASES)}"
            )
    attach_to = tuple(attach_to_raw)

    env_raw = raw.get("env", {}) or {}
    if not isinstance(env_raw, dict):
        raise McpConfigError(f"server '{name}' env must be a dict")
    env = {str(k): _interpolate(str(v), {}) for k, v in env_raw.items()}

    headers_raw = raw.get("headers", {}) or {}
    if not isinstance(headers_raw, dict):
        raise McpConfigError(f"server '{name}' headers must be a dict")
    headers = {str(k): _interpolate(str(v), env) for k, v in headers_raw.items()}

    if transport == "stdio":
        command = raw.get("command")
        if not command:
            raise McpConfigError(f"stdio server '{name}' requires 'command'")
        args_raw = raw.get("args", []) or []
        if not isinstance(args_raw, list):
            raise McpConfigError(f"server '{name}' args must be a list")
        # All user-supplied string fields support ``${ENV_VAR}`` so the
        # surface is consistent. Without interpolating ``command`` and
        # ``cwd`` here, a config like ``"command": "${NODE_PATH}/npx"``
        # would try to literally exec the unresolved string and fail
        # with a confusing FileNotFoundError instead of the loud
        # McpConfigError that env-var resolution gives.
        command = _interpolate(str(command), env)
        args = tuple(_interpolate(str(a), env) for a in args_raw)
        cwd_raw = raw.get("cwd")
        cwd = _interpolate(str(cwd_raw), env) if cwd_raw else None
        return McpServerConfig(
            name=name, transport=transport,
            command=command, args=args, env=env, cwd=cwd,
            url=None, headers={},
            attach_to=attach_to, enabled=enabled, timeout_seconds=timeout,
        )
    else:  # http
        url = raw.get("url")
        if not url:
            raise McpConfigError(f"http server '{name}' requires 'url'")
        return McpServerConfig(
            name=name, transport=transport,
            command=None, args=(), env={}, cwd=None,
            url=_interpolate(str(url), env), headers=headers,
            attach_to=attach_to, enabled=enabled, timeout_seconds=timeout,
        )


def load_mcp_config(path: Path | None = None) -> list[McpServerConfig]:
    """Load + validate mcp.json. Returns enabled servers only.

    Missing file → empty list (treated as "no MCP" by callers,
    not as an error). Malformed JSON / invalid shape → raises.
    """
    if path is None:
        path = Path.home() / ".blade-ai" / "mcp.json"

    if not path.exists():
        logger.info("mcp.json not found at %s — MCP disabled", path)
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise McpConfigError(f"failed to read {path}: {e}") from e

    if not isinstance(raw, dict):
        raise McpConfigError(f"{path} root must be a JSON object")

    servers_raw = raw.get("mcpServers", {})
    if not isinstance(servers_raw, dict):
        raise McpConfigError(f"{path}: 'mcpServers' must be a JSON object")

    configs: list[McpServerConfig] = []
    for name, server_raw in servers_raw.items():
        try:
            cfg = _parse_one(str(name), server_raw)
        except McpConfigError as e:
            logger.warning("mcp.json: skipping invalid server '%s': %s", name, e)
            continue
        if not cfg.enabled:
            logger.debug("mcp.json: server '%s' disabled, skipping", name)
            continue
        configs.append(cfg)

    return configs
