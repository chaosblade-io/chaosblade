"""CLI command: blade-ai config - Manage unified configuration."""

import typer

from chaos_agent.cli.config_manager import (
    SENSITIVE_KEYS,
    get_config,
    list_config,
    set_config,
    LOCAL,
    SERVER,
)
from chaos_agent.cli.output import format_output


def _mask_value(key: str, value: object) -> object:
    """Mask sensitive values for display."""
    if key in SENSITIVE_KEYS and isinstance(value, str) and len(value) > 4:
        return value[:4] + "*" * (len(value) - 4)
    return value


def _parse_value(key: str, raw: str) -> object:
    """Parse a string value into the appropriate Python type based on key."""
    # Boolean keys
    if key in ("confirmation_required", "retry_jitter"):
        return raw.lower() in ("true", "1", "yes")
    # Integer keys
    if key in (
        "server_port",
        "command_timeout",
        "llm_max_retries",
        "timeout_blade",
        "timeout_kubectl",
        "timeout_kubectl_exec",
        "timeout_llm",
        "timeout_default",
        "max_agent_loop",
        "max_execute_loop",
        "recursion_limit",
        "retry_max_retries",
    ):
        try:
            return int(raw)
        except ValueError:
            return raw
    # Float keys
    if key in ("llm_temperature", "retry_base_delay", "retry_max_delay", "retry_exponential_base"):
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw


def config_command(
    action: str = typer.Argument(
        "list",
        help="Action: 'list' | 'get' | 'set'",
    ),
    key: str = typer.Argument(
        None,
        help="Config key name (required for get/set)",
    ),
    value: str = typer.Argument(
        None,
        help="Config value (required for set)",
    ),
    extra: str = typer.Argument(
        None,
        help="Extra value (used when key=mode and value=server, this is the server URL)",
    ),
    output: str = typer.Option("json", "--output", "-o", help="Output format: json|yaml"),
):
    """Manage configuration: view and set all config values.

    Examples:
      blade-ai config                        # List all config
      blade-ai config list                   # List all config
      blade-ai config get mode               # Get a single config value
      blade-ai config get llm_api_key        # Get API key (masked)
      blade-ai config set mode local         # Switch to local mode
      blade-ai config set mode server http://host:8089  # Switch to server mode
      blade-ai config set llm_api_key sk-xxx # Set API key
      blade-ai config set model_name glm-5.1 # Set model name
    """
    if action == "list":
        data = list_config()
        masked = {k: _mask_value(k, v) for k, v in data.items()}
        result = {"code": 0, "message": "success", "data": masked}

    elif action == "get":
        if not key:
            result = {"code": 1001, "message": "Key is required for 'get'. Usage: blade-ai config get <key>", "data": None}
        else:
            val = get_config(key)
            if val is None:
                result = {"code": 1002, "message": f"Unknown config key: {key}", "data": None}
            else:
                result = {"code": 0, "message": "success", "data": {key: _mask_value(key, val)}}

    elif action == "set":
        if not key:
            result = {"code": 1001, "message": "Key is required for 'set'. Usage: blade-ai config set <key> <value>", "data": None}
        elif key == "mode":
            # Special handling for mode: set mode local | set mode server <url>
            mode_val = value or LOCAL
            if mode_val == LOCAL:
                set_config("mode", LOCAL)
                set_config("server_url", None)
                result = {"code": 0, "message": "success", "data": {"mode": LOCAL, "server_url": None}}
            elif mode_val == SERVER:
                if not extra:
                    result = {"code": 1001, "message": "Server URL is required. Usage: blade-ai config set mode server <url>", "data": None}
                else:
                    set_config("mode", SERVER)
                    set_config("server_url", extra.rstrip("/"))
                    result = {"code": 0, "message": "success", "data": {"mode": SERVER, "server_url": extra.rstrip("/")}}
            else:
                result = {"code": 1001, "message": f"Unknown mode '{mode_val}'. Use: local | server", "data": None}
        else:
            parsed = _parse_value(key, value) if value is not None else value
            set_config(key, parsed)
            result = {"code": 0, "message": "success", "data": {key: _mask_value(key, parsed)}}

    else:
        result = {"code": 1001, "message": f"Unknown action '{action}'. Use: list | get | set", "data": None}

    typer.echo(format_output(result, output))
