"""MCP manager: owns the lifecycle of all configured servers.

Parallel connect with per-server independent timeout — one slow /
broken server NEVER blocks the others or the rest of startup.
Failed servers are dropped with a log warning; successful ones are
kept and their tools surfaced through ``tools_for_phase``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from chaos_agent.mcp.adapter import make_langchain_tool
from chaos_agent.mcp.client import McpClient
from chaos_agent.mcp.config import McpServerConfig, load_mcp_config
from chaos_agent.mcp.effect import resolve_tool_effect
from chaos_agent.mcp.registry import McpToolRegistry

if TYPE_CHECKING:
    from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)

# Force-kill timeout for stdio child processes if graceful close
# doesn't complete in time. Avoids orphan child processes lingering
# after blade-ai-server shutdown.
_DISCONNECT_TIMEOUT_SECONDS = 5.0


class McpManager:
    """Owns all McpClient instances and their tools."""

    def __init__(self, configs: list[McpServerConfig] | None = None):
        # Inject configs (mainly for tests); production calls
        # load_mcp_config() inside connect_all if omitted.
        self._configs = configs
        self._clients: list[McpClient] = []
        # client → list of LangChain tools (already adapted)
        self._tools_by_client: dict[str, list[StructuredTool]] = {}

    async def connect_all(self, connect_timeout_seconds: int = 10) -> None:
        """Connect to all enabled servers in parallel.

        Failures are isolated per-server: one bad server doesn't
        prevent the others from coming up. After this returns,
        ``self._clients`` contains only successfully-connected ones.
        """
        if self._configs is None:
            self._configs = load_mcp_config()
        if not self._configs:
            logger.info("no MCP servers configured")
            return

        async def _connect_one(cfg: McpServerConfig) -> McpClient | None:
            client = McpClient(cfg)
            try:
                await asyncio.wait_for(client.connect(), timeout=connect_timeout_seconds)
                tools = await asyncio.wait_for(
                    client.list_tools(), timeout=connect_timeout_seconds
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP server '%s' connect/list_tools timed out after %ds; skipping",
                    cfg.name, connect_timeout_seconds,
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return None
            except Exception as e:
                logger.warning(
                    "MCP server '%s' failed to connect (skipping): %s",
                    cfg.name, e,
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return None

            # Adapt each MCP tool to a LangChain StructuredTool. Adapter
            # failures (e.g. bad JSON Schema) are per-tool: log warning,
            # skip that tool, keep the rest.
            adapted: list[StructuredTool] = []
            # Advisory read/write labels (posture B): operator override
            # wins, else the server's own ToolAnnotations, else
            # unspecified. Recorded here, surfaced in the tool
            # description; NEVER gates execution.
            effect_records: list[tuple[str, str, str]] = []
            for descriptor in tools:
                effect, source = resolve_tool_effect(
                    cfg.tool_effects.get(descriptor.name), descriptor.annotations
                )
                effect_records.append((descriptor.name, effect, source))
                try:
                    adapted.append(
                        make_langchain_tool(
                            client, descriptor, cfg.timeout_seconds, effect
                        )
                    )
                except Exception as e:
                    logger.warning(
                        "MCP server '%s' tool '%s' adapter failed (skipping): %s",
                        cfg.name, descriptor.name, e,
                    )
            self._tools_by_client[cfg.name] = adapted
            # Advisory-label integrity (posture B): warn on ``tool_effects``
            # keys that matched NO real tool. The VALUE axis is validated at
            # config load (config.py); the KEY axis can only be checked
            # here, where the server's actual tool names are known. Without
            # this, a typo'd tool name (valid value, wrong key) makes the
            # operator's override silently no-op — the mirror image of the
            # silent config error we removed on the value side. Advisory
            # only: warn, never gate; the real tools are unaffected.
            known_names = {d.name for d in tools}
            orphans = sorted(set(cfg.tool_effects) - known_names)
            if orphans:
                logger.warning(
                    "MCP server '%s' tool_effects key(s) %s matched no tool "
                    "(server exposes: %s); these overrides are ignored",
                    cfg.name, orphans, sorted(known_names) or "none",
                )
            # Register each adapted tool with the guard-classification
            # registry so the target_guard classifier recognises it instead
            # of default-denying it as UNKNOWN. Posture A: the classifier
            # passes a registered MCP tool through as READONLY (best-effort;
            # the operator owns the wiring). Keyed by the LangChain full
            # name the LLM will actually call it by.
            for adapted_tool in adapted:
                McpToolRegistry.register(adapted_tool.name, cfg.attach_to)
            logger.info(
                "MCP connected: %s (%d tools, attach_to=%s)",
                cfg.name, len(adapted), list(cfg.attach_to),
            )
            logger.info(
                "MCP tool effects (server=%s): %s",
                cfg.name,
                ", ".join(f"{n}={e}({s})" for n, e, s in effect_records) or "none",
            )
            return client

        results = await asyncio.gather(
            *(_connect_one(c) for c in self._configs),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, BaseException):
                logger.warning("MCP connect_all gather raised: %s", r)
            elif r is not None:
                self._clients.append(r)

    def tools_for_phase(self, phase: str) -> list["StructuredTool"]:
        """Return the union of tools from all clients whose attach_to
        includes the given phase. Order: by server insertion order,
        then by tool order within each server."""
        out: list[StructuredTool] = []
        for client in self._clients:
            if phase in client.attach_to:
                out.extend(self._tools_by_client.get(client.name, []))
        return out

    async def disconnect_all(self) -> None:
        """Close all clients in parallel; force-kill stragglers."""
        if not self._clients:
            return

        async def _disconnect_one(client: McpClient) -> None:
            try:
                await asyncio.wait_for(
                    client.disconnect(), timeout=_DISCONNECT_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP client '%s' disconnect timed out (%ds); forcing close",
                    client.name, _DISCONNECT_TIMEOUT_SECONDS,
                )
            except Exception as e:
                logger.warning("MCP client '%s' disconnect error: %s", client.name, e)

        await asyncio.gather(
            *(_disconnect_one(c) for c in self._clients),
            return_exceptions=True,
        )
        self._clients = []
        self._tools_by_client = {}
        # Drop the guard-classification entries too, so a tool from a
        # server that is no longer connected can never be classified.
        McpToolRegistry.clear()
