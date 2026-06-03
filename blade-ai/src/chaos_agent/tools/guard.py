"""Tool Guard: command execution safety.

Enforces a whitelist of allowed commands, kubectl subcommand
restrictions, and a parameter blacklist to prevent dangerous operations.

E11 — host_part regex was replaced by AST-level parsing via
``guard_parser.parse_command``. Behaviour:
  1. Binary whitelist (unchanged).
  2. kubectl/blade subcommand whitelist (subcommand extracted by parser
     instead of an inline while-loop).
  3. Token-level checks on ``ParsedCommand.host_relevant_tokens()``
     only — no more ``" ".join(cmd)`` cross-token false positives.
     Two checks per host token:
       a. Solo shell-metachar (``SUSPICIOUS_SOLO_TOKENS``) — ``|``
          ``;`` ``&`` ``>`` ``<`` etc.
       b. Regex blacklist (``PARAM_BLACKLIST_PATTERNS``).
     Data payload flag values (``-p`` ``--patch`` ``--from-literal``
     ``-l`` ``--field-selector`` …) and container_command (after
     ``--`` for ``kubectl exec/run/attach/debug``) are excluded from
     BOTH checks — they are not shell tokens on the host (subprocess
     uses ``shell=False``), so a stray ``|`` in those positions is at
     worst a no-op, never a host-injection.
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from chaos_agent.tools.guard_parser import (
    SUSPICIOUS_SOLO_TOKENS,
    parse_command,
)
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
        "apply",
        "create",
        "rollout",
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

        # 2. AST-level parse — single source of structure for the rest
        # of the checks (subcommand identification + payload/container
        # exclusion). Pure function, never raises.
        parsed = parse_command(cmd)

        # 3. kubectl subcommand whitelist (from parsed.subcommand)
        if binary == "kubectl" and parsed.subcommand:
            if parsed.subcommand not in self.kubectl_subcommands:
                return False, f"kubectl subcommand not allowed: {parsed.subcommand}"

        # 4 + 5. Token-level checks (SUSPICIOUS_SOLO_TOKENS + regex
        # blacklist) on host-relevant tokens only. Excludes
        # data_payload_values (JSON/YAML/selector strings) and
        # container_command (tokens after ``--`` for exec/run/attach/debug).
        #
        # Why container_command is exempt from the solo-token check:
        # the runtime uses subprocess with shell=False, so a stray ``|``
        # after ``--`` cannot form a pipeline on the host — kubectl just
        # forwards it as a literal argv to the container's process. The
        # LLM may emit ``kubectl exec pod -- ps aux | grep mem`` thinking
        # it produces a host-side pipe; in our exec-form runtime that
        # results in a no-op (extra argv ignored), but it is NOT a
        # security issue. Real pipe semantics inside the container
        # require ``kubectl exec pod -- sh -c "ps aux | grep mem"``,
        # whose ``|`` sits inside a quoted token and was never caught
        # by the solo-token check anyway.
        host_tokens = parsed.host_relevant_tokens()
        for token in host_tokens:
            if token in SUSPICIOUS_SOLO_TOKENS:
                return False, "Dangerous pattern detected in command"
            for pattern in self._compiled_patterns:
                if pattern.search(token):
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
