"""Safe async subprocess runner with ToolGuard integration and per-tool timeout."""

import asyncio
import logging
import os
import shutil
import signal
import sys
import time
from pathlib import Path
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
from chaos_agent.tools.guard_gateway import get_guard_gateway

# ARMS GenAI registers fork callbacks that emit debug records before exec().
# Production stacks have stalled in RotatingFileHandler on that path. Linux
# subprocesses below avoid fork; this remains defense in depth for third-party
# code that still forks.
for _arms_fork_logger_name in (
    "aliyun.opentelemetry.util.genai._multimodal_processing",
    "aliyun.opentelemetry.util.genai._multimodal_upload.pre_uploader",
):
    logging.getLogger(_arms_fork_logger_name).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Max output limit for communicate() — prevents runaway memory on huge output.
# Not currently enforced (communicate reads until EOF), but kept as reference
# for future output-capping if needed.
_MAX_PIPE_READ = 1024 * 1024  # 1 MB


def _kill_process_group(pgid: int) -> None:
    """Kill an entire process group. No-op if group already exited.

    Refuses ``pgid <= 1``: pgid 0 targets the *caller's own* process group and
    pgid 1 targets init/PID-1's group — SIGKILL to either would take down the
    agent itself, or (as root in a container) the whole session. A legitimately
    spawned child always has pgid > 1 (setsid / ``start_new_session`` makes its
    pgid equal to its own pid), so this guard only ever blocks a bogus value —
    e.g. a mock whose ``.pid`` coerces to 1 in tests, which as root would
    otherwise ``killpg(1, SIGKILL)`` and hang the whole run.
    """
    if pgid <= 1:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _prepare_spawn_command(exec_cmd: list[str]) -> tuple[list[str], dict[str, bool]]:
    """Build a subprocess command that avoids ``fork()`` on Linux.

    CPython 3.12 only selects ``posix_spawn`` when the executable is absolute,
    ``close_fds`` is false, and ``start_new_session`` is false. ``setsid``
    creates the session/process group externally so timeout cleanup can still
    terminate the command and all of its descendants with ``killpg``.
    """
    if not sys.platform.startswith("linux"):
        return exec_cmd, {"start_new_session": True}

    setsid_path = shutil.which("setsid")
    if not setsid_path:
        for candidate in ("/usr/bin/setsid", "/bin/setsid"):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                setsid_path = candidate
                break
    if not setsid_path:
        raise RuntimeError(
            "Linux safe subprocess execution requires the 'setsid' executable"
        )

    # Python-created descriptors are non-inheritable by default (PEP 446).
    # close_fds=False is required for CPython 3.12's posix_spawn fast path.
    return [str(Path(setsid_path).resolve()), *exec_cmd], {
        "close_fds": False,
        "start_new_session": False,
    }


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
                "node": "execute_loop",
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
    stdin_data: str = "",
    channel: str = "",
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
        stdin_data: If non-empty, piped to the subprocess stdin. Used by
                    kubectl apply/create -f - to pass YAML manifests.
        channel: Resolved transport channel name, recorded on status events so
                 the destination of a command is visible even when ``source``
                 carries a semantic label instead of the executed binary.

    Returns:
        CommandResult with exit_code, stdout, stderr, duration_ms.

    Raises:
        ToolGuardError: If the command is blocked by ToolGuard.
        ToolTimeoutError: If the command times out.
    """
    if not skip_guard:
        # Single funnel: command safety → unified GuardFeedback. The rendered
        # text carries the specific rule that fired + the offending token, not
        # an opaque catch-all, so the model can self-correct.
        feedback = get_guard_gateway().check_command(cmd)
        if not feedback.allowed:
            raise ToolGuardError(feedback.render_for_llm())

    # Emit status event for command execution (use emit() instead of
    # start()/complete()/fail() to avoid polluting the parent node's
    # _current_source and _start_time.  run_command is a sub-step — it
    # should not override the caller's tracker context.)
    # Use bare binary name for display; absolute paths leak implementation
    # detail (e.g. bundled blade path) and clutter logs/status events.
    display_cmd = ([Path(cmd[0]).name] + cmd[1:]) if cmd else cmd
    cmd_str = " ".join(display_cmd)
    source_name = source or (Path(cmd[0]).name if cmd else "unknown")
    # ``source`` is a SEMANTIC label the caller chooses ("host-read",
    # "verifier-L1", …) and the TUI renders it, so it must stay as-is. But it
    # then hides the execution facts: task-46317228's ``host_read`` events said
    # "host-read" while ``wiz`` was the binary actually run, against a cluster
    # channel. Carry both facts alongside so the destination is never invisible.
    executed_binary = Path(cmd[0]).name if cmd else ""
    # Carried on the completion / failure / timeout events too, not just the
    # start one: diagnosing a bad run means looking at the FAILURE event, and
    # the destination is exactly what was missing there.
    exec_detail = {"command": cmd_str, "executed_binary": executed_binary}
    if channel:
        exec_detail["channel"] = channel
    tracker = get_tracker(task_id) if task_id else None
    if tracker:
        tracker.emit(StatusEvent(
            task_id=tracker.task_id,
            phase=StatusPhase.STARTED,
            category=StatusCategory.TOOL,
            source=source_name,
            message=f"Executing shell: {cmd_str}",
            detail=dict(exec_detail),
        ))

    cmd_timeout = timeout or settings.timeout_default
    start_time = time.monotonic()

    proc = None
    reap_task = None
    try:
        from chaos_agent.utils.blade_paths import resolve_exec_path

        sub_env = None
        if env_override:
            sub_env = {**os.environ, **env_override}

        # Resolve cmd[0] to an absolute path, then construct a Linux spawn
        # command that satisfies CPython 3.12's posix_spawn conditions. fork()
        # triggers unsafe APM at-fork callbacks in the production worker.
        exec_cmd = ([resolve_exec_path(cmd[0])] + cmd[1:]) if cmd else cmd
        spawn_cmd, spawn_kwargs = _prepare_spawn_command(exec_cmd)

        proc = await asyncio.create_subprocess_exec(
            *spawn_cmd,
            stdin=asyncio.subprocess.PIPE if stdin_data else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=sub_env,
            **spawn_kwargs,
        )

        # Background reaper: once the main process exits, kill its entire
        # process group. This reaps children (e.g. ChaosBlade's background
        # "sleep N; blade destroy" timer) whose inherited pipe FDs would
        # prevent communicate() from seeing EOF.
        async def _reap_children_on_exit():
            await proc.wait()
            _kill_process_group(proc.pid)

        reap_task = asyncio.ensure_future(_reap_children_on_exit())

        # Use communicate() for pipe reading — it correctly handles the
        # pipe/process lifecycle in uvloop. The manual pattern (read tasks
        # + proc.wait + kill_process_group + drain) loses pipe data under
        # uvloop because uvloop may close pipe transports on process exit
        # before StreamReader can consume buffered data.
        #
        # The reap_task above ensures that once blade exits, any timer
        # children holding the pipe are killed immediately, so
        # communicate() gets EOF without waiting for cmd_timeout.
        input_bytes = stdin_data.encode("utf-8") if stdin_data else None
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=input_bytes),
            timeout=cmd_timeout,
        )

    except asyncio.TimeoutError:
        # Kill entire process group (main process + children)
        if proc:
            _kill_process_group(proc.pid)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
        if tracker:
            tracker.emit(StatusEvent(
                task_id=tracker.task_id,
                phase=StatusPhase.FAILED,
                category=StatusCategory.TOOL,
                source=source_name,
                message=f"Command timed out after {cmd_timeout}s: {cmd_str}",
                duration_ms=(time.monotonic() - start_time) * 1000,
                detail={**exec_detail, "exit_code": -1, "timeout": cmd_timeout},
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
            f"Command timed out after {cmd_timeout}s: {cmd_str}"
        )
    finally:
        if reap_task and not reap_task.done():
            reap_task.cancel()
            # Await the cancelled task to ensure it completes before
            # the caller returns. Without this, the task lingers as
            # "pending cancelled" on the event loop. On Python 3.11 +
            # pytest-asyncio the loop-cleanup phase may hang if the
            # task's coroutine raises a non-CancelledError (e.g.
            # TypeError from ``await MagicMock()`` in unit tests where
            # ``proc`` is a MagicMock, not a real Process).
            try:
                await reap_task
            except (asyncio.CancelledError, Exception):
                pass

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
                detail={**exec_detail, "exit_code": result.exit_code, "duration_ms": duration_ms, "stdout_preview": stdout_preview},
            ))
        else:
            tracker.emit(StatusEvent(
                task_id=tracker.task_id,
                phase=StatusPhase.FAILED,
                category=StatusCategory.TOOL,
                source=source_name,
                message=f"Shell failed: {cmd_str} (exit={result.exit_code})",
                duration_ms=duration_ms,
                detail={**exec_detail, "exit_code": result.exit_code, "stderr": stderr[:200], "stdout_preview": stdout_preview},
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
