"""Lazy, process-wide graph pool used by the L4 adapter."""

from __future__ import annotations

import asyncio
import threading


class _ChaosAgentPool:
    """Holds compiled inject/recover graphs.

    Sync entry (CLI): uses MemorySaver (pure-dict, no IO/loop binding;
    safe for ``asyncio.run()`` which creates a fresh loop each call).

    Async entry (platform): passes ``checkpointer=None`` so factory
    uses AsyncSqliteSaver — persistent across service restarts.
    """

    inject_graph = None
    intent_graph = None  # Intent Graph (dialogue layer: intent_clarification + intent_confirm)
    recover_graph = None
    skill_registry = None
    _initialized = False
    _init_lock = threading.Lock()
    # asyncio.Lock is created lazily on first async init call (cannot be
    # created at class-definition time — needs a running event loop).
    _async_init_lock: "asyncio.Lock | None" = None

    def _build_graphs_sync(self) -> dict:
        """Build skill registry + return create_agent kwargs (loop-agnostic).

        Extracted so both sync and async init paths share the heavy
        registry-loading logic without duplication.
        """
        from chaos_agent.skills.loader import get_skills_dir
        from chaos_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        skills_dir = get_skills_dir()
        if skills_dir.exists():
            registry.load_from_directory(skills_dir)
        return {"registry": registry}

    def _commit(self, agents: dict, registry) -> None:
        """Atomic commit of compiled graphs onto the class."""
        cls = type(self)
        cls.inject_graph = agents["pipeline"]
        cls.intent_graph = agents["intent"]
        cls.recover_graph = agents["recover"]
        cls.skill_registry = registry
        cls._initialized = True

    def ensure_initialized(self) -> None:
        """Sync init path — used by CLI and any sync caller.

        Uses ``asyncio.run()`` to drive the async ``create_agent``. Safe
        only when the calling thread has NO running event loop.
        """
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            from chaos_agent.agent.factory import create_agent

            ctx = self._build_graphs_sync()
            registry = ctx["registry"]
            # None → factory uses AsyncSqliteSaver (persistent).
            agents = asyncio.run(create_agent(registry, checkpointer=None))
            self._commit(agents, registry)

    async def async_ensure_initialized(self) -> None:
        """Async init path — used by platform (stays in main loop).

        Uses ``asyncio.Lock`` for coroutine-safe single init. Sync
        ``threading.Lock`` would block the loop if another coroutine
        also tries to init.
        """
        if self._initialized:
            return
        cls = type(self)
        if cls._async_init_lock is None:
            cls._async_init_lock = asyncio.Lock()
        async with cls._async_init_lock:
            if self._initialized:
                return
            from chaos_agent.agent.factory import create_agent

            ctx = self._build_graphs_sync()
            registry = ctx["registry"]
            # Pass None → factory uses AsyncSqliteSaver (persistent across restarts).
            # MemorySaver was previously used here but caused thread state loss on
            # service restart, breaking multi-turn clarify conversations.
            agents = await create_agent(registry, checkpointer=None)
            self._commit(agents, registry)

