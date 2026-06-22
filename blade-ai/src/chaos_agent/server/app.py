"""FastAPI application factory with lifespan, graceful shutdown, and middleware."""

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from chaos_agent.agent.factory import create_agent
from chaos_agent.agent.prompts.knowledge_watcher import KnowledgeWatcher
from chaos_agent.config.settings import settings
from chaos_agent.memory.tui_session_store import (
    TuiSessionStore,
    get_global_tui_session_store,
    set_global_tui_session_store,
)
from chaos_agent.models.schemas import JSONEnvelope
from chaos_agent.server.middleware import (
    ProtocolVersionMiddleware,
    RequestIDMiddleware,
    TimingMiddleware,
)
from chaos_agent.server.routes import config as _config  # noqa: F401 - registers /api/v1/config
from chaos_agent.server.routes import (
    health_router,
    recover_router,
)
from chaos_agent.server.routes import interrupt as _interrupt  # noqa: F401 - registers /interrupt + /cancel
from chaos_agent.server.routes import memory as _memory  # noqa: F401 - registers /api/v1/memory
from chaos_agent.server.routes import model as _model  # noqa: F401 - Phase 3c.1 model select
from chaos_agent.server.routes import preflight as _preflight  # noqa: F401 - registers /api/v1/preflight
from chaos_agent.server.routes import skills_admin as _skills_admin  # noqa: F401 - Phase 3b skills mgmt
from chaos_agent.server.routes import turn as _turn  # noqa: F401 - registers /turn handler
from chaos_agent.server.routes.confirm import confirm_router
from chaos_agent.server.routes.inject import inject_router
from chaos_agent.server.routes.inject_stream import inject_stream  # noqa: F401 - registers route
from chaos_agent.server.routes.list_skills import skills_router
from chaos_agent.server.routes.metric import metric_router
from chaos_agent.server.routes.prometheus import prometheus_router
from chaos_agent.server.routes.recordings import recordings_router
from chaos_agent.server.routes.recover_stream import recover_stream  # noqa: F401 - registers route
from chaos_agent.server.routes.sessions import sessions_router
from chaos_agent.server.routes.status_stream import status_stream  # noqa: F401 - registers route
from chaos_agent.skills.loader import get_skills_dir
from chaos_agent.skills.prerequisites import PrerequisitesChecker
from chaos_agent.skills.registry import SkillRegistry
from chaos_agent.skills.watcher import SkillWatcher
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


class TaskTracker:
    """Track active tasks for graceful shutdown."""

    def __init__(self, drain_timeout: int = 30):
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._shutting_down = False
        self._drain_timeout = drain_timeout

    def register(self, task_id: str, task: asyncio.Task) -> None:
        self._active_tasks[task_id] = task

    def unregister(self, task_id: str) -> None:
        self._active_tasks.pop(task_id, None)

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    async def drain(self) -> None:
        """Wait for all active tasks to complete or timeout."""
        self._shutting_down = True
        if not self._active_tasks:
            return

        logger.info(f"Draining {len(self._active_tasks)} active tasks...")
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._active_tasks.values(), return_exceptions=True),
                timeout=self._drain_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Drain timeout, {len(self._active_tasks)} tasks still running"
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown logic."""
    # --- Startup ---
    logger.info("Starting Blade AI Server...")

    # Initialize skill registry
    registry = SkillRegistry()
    registry.load_from_directory(get_skills_dir())
    app.state.skill_registry = registry

    # Hot-reload watchers. Both are no-ops if watchdog isn't installed
    # or the target dir doesn't exist (logged as a single WARN line).
    # SkillWatcher rebuilds the SkillRegistry on SKILL.md / script
    # changes; KnowledgeWatcher rebuilds the knowledge_registry cache
    # on .md changes under src/chaos_agent/knowledge/. Both stop in
    # the shutdown block below.
    skill_watcher = SkillWatcher(get_skills_dir(), registry)
    skill_watcher.start()
    app.state.skill_watcher = skill_watcher

    knowledge_watcher = KnowledgeWatcher()
    knowledge_watcher.start()
    app.state.knowledge_watcher = knowledge_watcher

    # Check prerequisites
    prereq_checker = PrerequisitesChecker()
    await prereq_checker.check_startup_prerequisites(registry)

    # E9 — MCP client manager. Connect to external MCP servers in
    # parallel with per-server timeout; failure isolation guarantees
    # a single bad server doesn't block startup. Tools are surfaced
    # to the agent via per-phase allowlist (attach_to) inside
    # create_agent below.
    mcp_manager = None
    if settings.mcp_enabled:
        from chaos_agent.mcp.manager import McpManager
        mcp_manager = McpManager()
        try:
            await mcp_manager.connect_all(
                connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
            )
        except Exception as e:
            logger.warning(f"MCP startup failed (continuing without MCP): {e}")
            mcp_manager = None
    app.state.mcp_manager = mcp_manager

    # First-run gate: skip create_agent when essential LLM config is
    # missing. ``make_llm()`` calls ``ChatOpenAI(api_key=...)`` which
    # raises ``OpenAIError`` immediately on an empty api_key — that
    # would crash the lifespan startup and the backend would never
    # become reachable. But the TS TUI's BootRunner needs the backend
    # to be alive to call ``/api/v1/wizard/needs-setup`` and render
    # the setup wizard. The wizard's ``/save`` endpoint calls
    # ``maybe_rebuild_agents`` afterwards which builds the graph
    # against the now-complete config — so we just defer construction
    # until then.
    from chaos_agent.config.wizard_validators import (
        missing_essential_config,
    )
    _missing = missing_essential_config()
    if _missing:
        logger.warning(
            "Essential LLM config missing (%s); skipping agent "
            "creation. Wizard /save will build agents once config "
            "is complete.",
            ", ".join(_missing),
        )
        agents: dict = {}
        app.state.agents = None
        app.state.checkpointer = None
    else:
        agents = await create_agent(registry, mcp_manager=mcp_manager)
        app.state.agents = agents
        app.state.checkpointer = agents.get("checkpointer")

    # Initialize task tracker for graceful shutdown
    task_tracker = TaskTracker()
    app.state.task_tracker = task_tracker

    # Initialize the per-process TuiSessionStore singleton — mirrors
    # what the legacy Python TUI does in ``tui/app.py:199-200``. Without
    # this the global is None, ``intent_clarification`` node's calls to
    # ``get_global_tui_session_store().append_dialogue(...)`` silently
    # no-op, and the TS TUI's session files / dialogue history never
    # land on disk under ``~/.blade-ai/memory/sessions/``. Same store
    # class, same path, so a session created via this server is readable
    # by Python TUI tooling that already understands the format.
    tui_session_store = TuiSessionStore(
        settings.resolved_memory_dir / "sessions"
    )
    set_global_tui_session_store(tui_session_store)
    app.state.tui_session_store = tui_session_store

    logger.info(
        f"Blade AI Server ready - {len(registry)} skills loaded"
    )

    yield

    # --- Shutdown ---
    logger.info("Graceful shutdown initiated...")

    # Stop hot-reload watchers FIRST — before drain — so a SKILL.md /
    # knowledge .md edit happening during the drain window can't fire
    # a registry reload while an in-flight task is still using it.
    # Each stop is guarded: a failed stop must not block the rest of
    # shutdown.
    for attr in ("skill_watcher", "knowledge_watcher"):
        w = getattr(app.state, attr, None)
        if w is not None:
            try:
                w.stop()
            except Exception as e:
                logger.warning(f"Failed to stop {attr}: {e}")

    await task_tracker.drain()

    # Finalize any TUI sessions that didn't get a clean DELETE — a TS
    # TUI killed by SIGKILL would have left ``.jsonl`` increments
    # without a matching ``.json`` snapshot. Iterate over whatever's in
    # the in-memory active dict (matches Python TUI's exit-time
    # ``tui_session_store.finalize(state.tui_session_id)`` in
    # tui/app.py:416) and mark each ``aborted`` so a later reader can
    # tell apart "user said /exit" (status=completed) from "server died
    # mid-session" (status=aborted).
    try:
        store = get_global_tui_session_store()
        if store is not None:
            for sid in store.list_active():
                try:
                    store.finalize(sid, status="aborted")
                except Exception as e:
                    logger.warning(f"Failed to finalize TUI session {sid}: {e}")
    except Exception as e:
        logger.warning(f"TUI session sweep failed: {e}")

    # Finalize orphaned task sessions (SessionStore) — same pattern as
    # TUI sessions above.  After drain() completes, any task still in
    # _active_sessions was interrupted (timeout or unfinished stream).
    _current_agents = getattr(app.state, "agents", None) or {}
    try:
        _task_ss = _current_agents.get("session_store")
        if _task_ss:
            for tid in list(_task_ss._active_sessions):
                try:
                    _task_ss.finalize_session(tid, remaining_messages=[], status="aborted")
                except Exception as e:
                    logger.warning(f"Failed to finalize task session {tid}: {e}")
    except Exception as e:
        logger.warning(f"Task session sweep failed: {e}")

    # Close checkpointer connection. Read from app.state (not the
    # local ``agents`` captured at startup) so that a wizard /save
    # which rebuilt the graph mid-lifecycle gets its checkpointer
    # cleaned up too — otherwise the new aiosqlite fd leaks.
    conn = _current_agents.get("checkpointer_conn")
    if conn is not None:
        try:
            await conn.close()
            logger.info("Checkpointer connection closed")
        except Exception as e:
            logger.warning(f"Failed to close checkpointer connection: {e}")

    # Close checkpointer
    checkpointer = _current_agents.get("checkpointer")
    if checkpointer and hasattr(checkpointer, "close"):
        await checkpointer.close()

    # Close the catalog-generator LLM client (lazy-built on the first
    # ``/api/v1/skills`` request, cached on app.state.catalog_llm). The
    # ``ChatOpenAI`` wraps an ``openai.AsyncOpenAI`` whose underlying
    # ``httpx`` pool holds sockets / file descriptors until close — leak
    # those on every server cycle and a busy CI host eventually starves.
    # Gracefully absent on a process that never served /experiments, so
    # we ``getattr`` defensively.
    catalog_llm = getattr(app.state, "catalog_llm", None)
    if catalog_llm is not None:
        # ``async_client`` is langchain-openai's attribute for the
        # underlying ``openai.AsyncOpenAI``; ``close()`` on that closes
        # the httpx pool. Guarded by hasattr in case a future
        # langchain-openai version renames the attribute — we'd rather
        # log a warning than crash shutdown.
        try:
            inner = getattr(catalog_llm, "async_client", None)
            if inner is not None and hasattr(inner, "close"):
                await inner.close()
            logger.info("Catalog LLM client closed")
        except Exception as e:
            logger.warning(f"Failed to close catalog LLM client: {e}")

    # Flush and shutdown OTel GenAI export (no-op if not initialized)
    try:
        from chaos_agent.observability.otel_genai import shutdown_otel_genai
        shutdown_otel_genai()
    except Exception as e:
        logger.warning(f"OTel GenAI shutdown failed: {e}")

    # E9 — disconnect MCP clients + reap stdio child processes
    _mcp = getattr(app.state, "mcp_manager", None)
    if _mcp is not None:
        try:
            await _mcp.disconnect_all()
        except Exception as e:
            logger.warning(f"MCP disconnect failed: {e}")

    # Close TaskStore backend
    try:
        from chaos_agent.persistence.task_store import reset_task_store
        await reset_task_store()
    except Exception as e:
        logger.warning(f"Failed to reset TaskStore: {e}")

    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    from chaos_agent import __version__ as _pkg_ver

    app = FastAPI(
        title="Chaos Engineering Agent",
        version=_pkg_ver,
        lifespan=lifespan,
    )

    # Add middleware (Starlette runs these in reverse order of registration,
    # i.e. ProtocolVersionMiddleware sees the response *first* on the way
    # out — order doesn't matter here since each only adds headers, but
    # keep the version stamp closest to the response for readability).
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(TimingMiddleware)
    app.add_middleware(ProtocolVersionMiddleware)

    # Register routers
    app.include_router(inject_router)
    app.include_router(recover_router)
    app.include_router(metric_router)
    app.include_router(prometheus_router)
    app.include_router(skills_router)
    app.include_router(confirm_router)
    app.include_router(sessions_router)
    app.include_router(recordings_router)
    # Phase 3a control-plane endpoints — config (read/write), memory
    # (inspect/clear). Compact lives on sessions_router because it's
    # session-scoped (``/sessions/{sid}/compact``).
    from chaos_agent.server.routes import config_router, memory_router

    app.include_router(config_router)
    app.include_router(memory_router)
    # Phase 3c.1 — model selection. Hot-swap currently degrades to
    # "restart needed" because the LLM is captured at startup; the
    # endpoint surface is built so a future LLM-rebuild path can
    # flip ``restart_required: false`` without changing the API.
    from chaos_agent.server.routes import model_router

    app.include_router(model_router)

    # Phase 4 — TS Ink onboarding wizard surface. Validators delegate
    # to ``chaos_agent.config.wizard_validators`` (shared with the
    # legacy Python Rich wizard); persistence reuses ConfigStore so
    # the wizard's save matches ``/api/v1/config`` semantics.
    from chaos_agent.server.routes import wizard_router

    # Import for side-effect: registers the @wizard_router.* handlers.
    import chaos_agent.server.routes.wizard  # noqa: F401

    app.include_router(wizard_router)

    # Health and version endpoints
    @health_router.get("/api/v1/health")
    async def health():
        return {"status": "ok", "timestamp": now_iso()}

    @health_router.get("/api/v1/version")
    async def version():
        return JSONEnvelope.ok(
            data={
                "version": _pkg_ver,
                "supported_fault_count": 0,
            },
        )

    app.include_router(health_router)

    return app


def run_server(
    host: str | None = None,
    port: int | None = None,
    ready_stdout: bool = False,
) -> None:
    """Entry point for the blade-ai-server command.

    Optional arguments support the TS TUI's embedded-server mode:
      - ``port=0`` lets the OS allocate a free port (we resolve it before
        uvicorn starts so the chosen port can be advertised).
      - ``ready_stdout=True`` prints a single ``BLADE_AI_READY port=N``
        line to stdout once the chosen port is bound, so the TS CLI can
        ``readline`` it and connect. Anything before the line is allowed
        to be log noise; the TS side scans line-by-line.
    """
    import socket

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    resolved_host = host if host is not None else settings.server_host
    resolved_port = port if port is not None else settings.server_port

    # Pre-bind to discover the port when port=0. We close immediately
    # and let uvicorn re-bind; on Linux/macOS this is reliable enough
    # for our use (TS spawn → readline → connect, no race in practice).
    if resolved_port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((resolved_host, 0))
            resolved_port = s.getsockname()[1]

    if ready_stdout:
        # The TS server-process.ts watches for this exact prefix.
        # Use stdout (not stderr) and flush so the line is delivered
        # immediately even when stdout is line-buffered.
        print(f"BLADE_AI_READY port={resolved_port}", flush=True)

    uvicorn.run(
        create_app(),
        host=resolved_host,
        port=resolved_port,
        log_level="warning",
    )


def _cli() -> None:
    """argparse entry for ``python -m chaos_agent.server.app``.

    Used by the TS TUI's embedded-server spawn. Kept separate from
    ``run_server`` so the existing ``blade-ai-server`` script entry
    (which calls run_server() with no args) is unaffected.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="chaos_agent.server.app")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--ready-stdout", action="store_true")
    args = parser.parse_args()

    run_server(host=args.host, port=args.port, ready_stdout=args.ready_stdout)


if __name__ == "__main__":
    _cli()
