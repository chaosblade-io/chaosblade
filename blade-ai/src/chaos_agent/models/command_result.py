"""CommandResult dataclass — shared by tools and transports.

This module is intentionally dependency-free (no imports from
``chaos_agent.tools`` or ``chaos_agent.transports``) to break the
circular import that would otherwise arise when transports modules
need ``CommandResult`` for type annotations:

    transports.base → tools.guard → tools.__init__ → tools.blade → transports  ✗

By importing from this neutral module, transports modules avoid
triggering ``tools.__init__`` entirely.
"""

from dataclasses import dataclass


@dataclass
class CommandResult:
    """Result of a command execution."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float = 0.0
