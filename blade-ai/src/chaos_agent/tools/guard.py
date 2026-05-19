"""Tool Guard: command execution safety.

Enforces a whitelist of allowed commands, kubectl subcommand restrictions,
and a parameter blacklist to prevent dangerous operations.
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """Result of a command execution."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float = 0.0


class ToolGuard:
    """Security guard for tool command execution."""

    ALLOWED_COMMANDS = {"blade", "kubectl", "df", "ping", "sleep"}

    KUBECTL_ALLOWED_SUBCOMMANDS = {
        "get",
        "describe",
        "delete",
        "exec",
        "logs",
        "top",
        "patch",
        "set",
        "scale",
        "debug",
        "wait",
        "cordon",
        "uncordon",
        "taint",
    }

    PARAM_BLACKLIST_PATTERNS = [
        r"rm\s+-rf",
        r">\s*/dev/",
        r";\s*rm",
        r"\|\s*bash",
        r"\|\s*sh",
        r"`.*`",
        r"\$\(",
    ]

    def __init__(
        self,
        allowed_commands: set[str] | None = None,
        kubectl_subcommands: set[str] | None = None,
        param_blacklist: list[str] | None = None,
    ):
        self.allowed_commands = allowed_commands or self.ALLOWED_COMMANDS
        self.kubectl_subcommands = kubectl_subcommands or self.KUBECTL_ALLOWED_SUBCOMMANDS
        self.param_blacklist = param_blacklist or self.PARAM_BLACKLIST_PATTERNS
        self._compiled_patterns = [re.compile(p) for p in self.param_blacklist]

    def check(self, cmd: list[str]) -> tuple[bool, str]:
        """Check if a command is allowed to execute.

        Returns (is_allowed, reason).
        """
        if not cmd:
            return False, "Empty command"

        binary = Path(cmd[0]).name

        # 1. Command whitelist
        if binary not in self.allowed_commands:
            return False, f"Command not allowed: {binary}"

        # 2. kubectl subcommand check
        if binary == "kubectl" and len(cmd) > 1:
            # Skip global flags and their values to find the actual subcommand.
            # kubectl global flags: --kubeconfig VAL, --context VAL, --cluster VAL,
            # -n VAL, --namespace VAL, --as VAL, --as-group VAL, etc.
            # Strategy: skip any token starting with '-', and the token after it
            # (which is the flag value). Then the first non-skipped token is the subcmd.
            subcmd = None
            i = 1
            while i < len(cmd):
                part = cmd[i]
                if part.startswith("-"):
                    # This is a flag; skip it and its value
                    # Flags with = syntax (e.g. --namespace=default) only consume one token
                    if "=" in part:
                        i += 1
                        continue
                    # Otherwise skip the next token too (it's the value)
                    i += 2
                    continue
                # First non-flag token is the subcommand
                subcmd = part
                break
            if subcmd and subcmd not in self.kubectl_subcommands:
                return False, f"kubectl subcommand not allowed: {subcmd}"

        # 3. Parameter blacklist
        # For kubectl, build host_part excluding data payload values that are
        # not shell-executed and should not be security-checked:
        #   - kubectl exec ... -- <container_cmd>: only check before "--"
        #   - kubectl patch ... -p/--patch <json>: skip the JSON value
        # The cmd list uses exec-form (create_subprocess_exec, no shell),
        # so $() and backticks in data payloads are literal strings, not
        # shell injections.
        if binary == "kubectl":
            host_parts: list[str] = []
            skip_next = False
            for part in cmd:
                if part == "--":
                    break  # Everything after -- runs inside the container
                if skip_next:
                    skip_next = False
                    continue
                # -p / --patch: the value is a JSON/YAML payload, not a shell command
                if part in ("-p", "--patch"):
                    host_parts.append(part)  # Keep the flag name for visibility
                    skip_next = True  # Skip the payload value
                    continue
                # Handle -p=VALUE / --patch=VALUE syntax
                if part.startswith("-p=") or part.startswith("--patch="):
                    flag_name = part.split("=", 1)[0]
                    host_parts.append(flag_name + "=")
                    continue
                host_parts.append(part)
            host_part = " ".join(host_parts)
        else:
            host_part = " ".join(cmd)

        for pattern in self._compiled_patterns:
            if pattern.search(host_part):
                return False, "Dangerous pattern detected in command"

        return True, "OK"

    def audit_log(
        self,
        cmd: list[str],
        result: CommandResult,
        task_id: str = "",
    ) -> None:
        """Record an execution audit log entry."""
        log_entry = {
            "timestamp": now_iso(),
            "task_id": task_id,
            "command": cmd,
            "exit_code": result.exit_code,
            "duration_ms": round(result.duration_ms, 1),
        }
        logger.info(json.dumps(log_entry, ensure_ascii=False))
