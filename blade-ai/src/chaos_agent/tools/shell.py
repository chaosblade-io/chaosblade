"""Safe async subprocess runner with ToolGuard integration and per-tool timeout."""

import asyncio
import logging
import time
from typing import Optional

from chaos_agent.config.settings import settings
from chaos_agent.errors import ToolGuardError, ToolTimeoutError
from chaos_agent.memory.session_store import get_global_session_store
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
    StatusEvent,
    StatusPhase,
)
from chaos_agent.tools.guard import CommandResult, ToolGuard

logger = logging.getLogger(__name__)

# Module-level ToolGuard instance
_tool_guard: Optional[ToolGuard] = None


def get_tool_guard() -> ToolGuard:
    """Get or create the singleton ToolGuard instance."""
    global _tool_guard
    if _tool_guard is None:
        _tool_guard = ToolGuard()
    return _tool_guard


def _persist_to_session(
    task_id: str,
    cmd_str: str,
    source_name: str,
    exit_code: int,
    duration_ms: float,
    stdout_preview: str,
    stderr: str = "",
) -> None:
    """Fire-and-forget: record command execution details to SessionStore.

    Ensures that CLI-visible command information (full command line,
    exit_code, duration_ms, stdout) is also persisted in the session
    JSON file for post-hoc analysis.
    """
    if not task_id:
        return
    try:
        _ss = get_global_session_store()
        if _ss:
            detail = {
                "command": cmd_str,
                "exit_code": exit_code,
                "duration_ms": round(duration_ms, 1),
                "stdout_preview": stdout_preview,
                "source": source_name,
            }
            if exit_code != 0 and stderr:
                detail["stderr"] = stderr[:500]
            _ss.append_raw_message(task_id, {
                "type": "tool_execution",
                "content": f"[shell] {cmd_str}",
                "detail": detail,
            })
    except Exception:
        logger.debug("SessionStore write failed for task %s", task_id)


async def run_command(
    cmd: list[str],
    timeout: Optional[int] = None,
    task_id: str = "",
    skip_guard: bool = False,
    env_override: Optional[dict[str, str]] = None,
    source: Optional[str] = None,
) -> CommandResult:
    """Execute a command safely via async subprocess.

    Args:
        cmd: Command and arguments as a list. Never uses shell=True.
        timeout: Per-command timeout in seconds. Falls back to settings.timeout_default.
        task_id: Task ID for audit logging and status tracking.
        skip_guard: Skip ToolGuard check (for internal use only).
        env_override: If provided, merge these env vars into the subprocess environment.
                      Useful for commands that don't support certain flags (e.g. blade status
                      in v1.8.0 lacks --kubeconfig, so KUBECONFIG must be passed via env).
        source: Override the status tracker source name. Defaults to cmd[0].
                Use a descriptive name (e.g. "conflict-check") for programmatic
                pre-checks to distinguish them from LLM-initiated tool calls.

    Returns:
        CommandResult with exit_code, stdout, stderr, duration_ms.

    Raises:
        ToolGuardError: If the command is blocked by ToolGuard.
        ToolTimeoutError: If the command times out.
    """
    if not skip_guard:
        guard = get_tool_guard()
        allowed, reason = guard.check(cmd)
        if not allowed:
            raise ToolGuardError(reason)

    # Emit status event for command execution (use emit() instead of
    # start()/complete()/fail() to avoid polluting the parent node's
    # _current_source and _start_time.  run_command is a sub-step — it
    # should not override the caller's tracker context.)
    cmd_str = " ".join(cmd)
    source_name = source or (cmd[0] if cmd else "unknown")
    tracker = get_tracker(task_id) if task_id else None
    if tracker:
        tracker.emit(StatusEvent(
            task_id=tracker.task_id,
            phase=StatusPhase.STARTED,
            category=StatusCategory.TOOL,
            source=source_name,
            message=f"Executing shell: {cmd_str}",
            detail={"command": cmd_str},
        ))

    cmd_timeout = timeout or settings.timeout_default
    start_time = time.monotonic()

    try:
        import os
        sub_env = None
        if env_override:
            sub_env = {**os.environ, **env_override}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=sub_env,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=cmd_timeout,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        if tracker:
            tracker.emit(StatusEvent(
                task_id=tracker.task_id,
                phase=StatusPhase.FAILED,
                category=StatusCategory.TOOL,
                source=source_name,
                message=f"Command timed out after {cmd_timeout}s: {cmd_str}",
                duration_ms=(time.monotonic() - start_time) * 1000,
                detail={"exit_code": -1, "timeout": cmd_timeout},
            ))
        _persist_to_session(
            task_id=task_id,
            cmd_str=cmd_str,
            source_name=source_name,
            exit_code=-1,
            duration_ms=(time.monotonic() - start_time) * 1000,
            stdout_preview="",
            stderr=f"Command timed out after {cmd_timeout}s",
        )
        raise ToolTimeoutError(
            f"Command timed out after {cmd_timeout}s: {' '.join(cmd)}"
        )

    duration_ms = (time.monotonic() - start_time) * 1000
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    result = CommandResult(
        exit_code=proc.returncode or 0,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
    )

    # Emit completion status (emit() to avoid state pollution)
    if tracker:
        stdout_preview = stdout[:500] if stdout else ""
        if result.exit_code == 0:
            tracker.emit(StatusEvent(
                task_id=tracker.task_id,
                phase=StatusPhase.COMPLETED,
                category=StatusCategory.TOOL,
                source=source_name,
                message=f"Shell completed: {cmd_str} ({duration_ms:.0f}ms)",
                duration_ms=duration_ms,
                detail={"exit_code": result.exit_code, "duration_ms": duration_ms, "stdout_preview": stdout_preview},
            ))
        else:
            tracker.emit(StatusEvent(
                task_id=tracker.task_id,
                phase=StatusPhase.FAILED,
                category=StatusCategory.TOOL,
                source=source_name,
                message=f"Shell failed: {cmd_str} (exit={result.exit_code})",
                duration_ms=duration_ms,
                detail={"exit_code": result.exit_code, "stderr": stderr[:200], "stdout_preview": stdout_preview},
            ))

    # Persist command execution to SessionStore (CLI → session JSON observability bridge)
    _persist_to_session(
        task_id=task_id,
        cmd_str=cmd_str,
        source_name=source_name,
        exit_code=result.exit_code,
        duration_ms=duration_ms,
        stdout_preview=stdout[:2000] if stdout else "",
        stderr=stderr,
    )

    # Audit log
    if not skip_guard:
        guard = get_tool_guard()
        guard.audit_log(cmd, result, task_id)

    return result
