"""L4ResilienceAgent — the main L4 adapter for blade-ai.

Implements the L4 lifecycle (prepare/execute/cleanup/cancel) by
wrapping blade-ai's LangGraph inject/recover graphs and driving
runtime.step() via astream_events phase event interception.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

from chaos_agent.l4.constants import DEFAULT_CARD_DECISION_TIMEOUT_S
from chaos_agent.l4.execution import _L4ExecutionMixin
from chaos_agent.l4.interaction import _L4InteractionMixin
from chaos_agent.l4.pool import _ChaosAgentPool
from chaos_agent.l4.recovery import _L4RecoveryMixin
from chaos_agent.l4.schemas import L4TaskResult


_logging_configured = False
_log_dir: "Path | None" = None


def _setup_logging() -> None:
    """Configure file-based logging for L4 SDK mode (idempotent).

    The handler is set up once per process, but the log directory is
    re-created on every call via ``ensure_log_dir()`` so that deleting
    the folder at runtime does not permanently silence file logging.
    """
    global _logging_configured, _log_dir
    if _logging_configured:
        return
    _logging_configured = True

    from chaos_agent.config.settings import settings

    _log_dir = settings.resolved_memory_dir / "logs"
    try:
        _log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    log_path = _log_dir / "l4.log"
    try:
        handler = RotatingFileHandler(
            log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
    except Exception:
        return

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.addHandler(handler)
    level_name = (settings.log_level or "INFO").upper()
    root.setLevel(getattr(logging, level_name, logging.INFO))

    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def ensure_log_dir() -> None:
    """Re-create the log directory if it was removed at runtime."""
    if _log_dir is not None:
        try:
            _log_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


class L4ResilienceAgent(
    _L4ExecutionMixin,
    _L4RecoveryMixin,
    _L4InteractionMixin,
):
    """blade-ai L4 adapter layer.

    Does NOT inherit BaseTestAgent (avoids circular dependency).
    Implements the same method signatures; ai-testing-platform's
    ResilienceAgent delegates to this object via composition.
    """

    def __init__(self) -> None:
        self._pool: _ChaosAgentPool | None = None
        self._cancel_event = threading.Event()
        self._completed: dict[str, L4TaskResult] = {}
        self._state_transitions_buffer: list[dict] = []

    # --- Lifecycle ---

    def prepare(self, runtime, task) -> None:
        """Pre-check: initialize graph pool, validate K8s/ChaosBlade."""
        self._ensure_pool()

    async def async_prepare(self, runtime, task) -> None:
        """Async pre-check: initialize graph pool (stays in caller's loop)."""
        await self._async_ensure_pool()

    def execute(self, runtime, task) -> L4TaskResult:
        """Main entry: TestTask → inject graph → TaskResult.

        B3 idempotent: same task_id returns cached result.
        """
        if task.task_id in self._completed:
            return self._completed[task.task_id]
        self._state_transitions_buffer = []
        # _ensure_pool() MUST be called in sync context (before asyncio.run)
        # because ensure_initialized() internally uses asyncio.run(create_agent(...))
        pool = self._ensure_pool()
        result = asyncio.run(self._async_execute(pool, runtime, task))
        if result.status in ("passed", "failed", "cancelled", "degraded"):
            self._completed[task.task_id] = result
            # FIFO eviction: drop oldest-inserted entry when over capacity.
            # B3 idempotent cache; repeated task_id is rare in production.
            if len(self._completed) > 100:
                oldest_inserted = next(iter(self._completed))
                del self._completed[oldest_inserted]
        return result

    def recover(self, runtime, task) -> L4TaskResult:
        """Public entry for explicit fault recovery.

        ``task.payload`` must contain ``inject_task_id`` — the task_id of
        the inject execution whose fault we want to recover.

        Returns L4TaskResult with status in (recovered, partial_recovered, failed).
        """
        pool = self._ensure_pool()
        result = asyncio.run(self._async_recover_explicit(pool, runtime, task))
        return result

    async def async_execute(self, runtime, task) -> L4TaskResult:
        """Async main entry (stays in caller's loop).

        Same semantics as ``execute()`` but without ``asyncio.run()``.
        """
        if task.task_id in self._completed:
            return self._completed[task.task_id]
        self._state_transitions_buffer = []
        pool = await self._async_ensure_pool()
        result = await self._async_execute(pool, runtime, task)
        if result.status in ("passed", "failed", "cancelled", "degraded"):
            self._completed[task.task_id] = result
            if len(self._completed) > 100:
                oldest_inserted = next(iter(self._completed))
                del self._completed[oldest_inserted]
        return result

    async def async_recover(self, runtime, task) -> L4TaskResult:
        """Async public entry for explicit fault recovery (stays in caller's loop)."""
        pool = await self._async_ensure_pool()
        return await self._async_recover_explicit(pool, runtime, task)

    def cleanup(self, runtime, task) -> None:
        """Clean up per-task state."""
        self._cancel_event.clear()

    def request_cancel(self) -> None:
        """Cooperative cancel: set event, 3s guarantee."""
        self._cancel_event.set()

    def is_cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    # --- Internal ---

    def _ensure_pool(self) -> _ChaosAgentPool:
        """Lazy-init graph pool. Compiles inject/recover graphs on first call."""
        if self._pool is None:
            _setup_logging()
            self._pool = _ChaosAgentPool()
        else:
            ensure_log_dir()
        self._pool.ensure_initialized()
        return self._pool

    async def _async_ensure_pool(self) -> _ChaosAgentPool:
        """Async lazy-init. For callers already in a running event loop."""
        if self._pool is None:
            _setup_logging()
            self._pool = _ChaosAgentPool()
        else:
            ensure_log_dir()
        await self._pool.async_ensure_initialized()
        return self._pool
