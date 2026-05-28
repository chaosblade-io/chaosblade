"""MCP client: one connection to one external MCP server.

Owns the session lifecycle + a per-session asyncio lock that
serialises tool calls (MCP JSON-RPC is order-sensitive — single
in-flight request per session). If a single server needs high
concurrency, the right fix is multiple sessions / connection pool,
not unlocking; Phase 1 sticks with single-session per server.

Transport-agnostic interface: stdio (child process via mcp SDK's
``stdio_client``) or HTTP/SSE (via ``sse_client``). Both expose the
same ``ClientSession``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

from chaos_agent.mcp.config import McpServerConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpToolDescriptor:
    """Subset of mcp.types.Tool that the adapter needs."""
    name: str
    description: str
    input_schema: dict[str, Any]


class McpClient:
    """One server connection, owns lifecycle + concurrency lock."""

    def __init__(self, config: McpServerConfig):
        self._config = config
        self._lock = asyncio.Lock()
        self._session: ClientSession | None = None
        # AsyncExitStack holds the transport context managers
        # (stdio_client / sse_client + ClientSession) so disconnect
        # can unwind them in reverse order.
        self._stack: contextlib.AsyncExitStack | None = None

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def attach_to(self) -> tuple[str, ...]:
        return self._config.attach_to

    @property
    def timeout_seconds(self) -> int:
        return self._config.timeout_seconds

    async def connect(self) -> None:
        """Open transport + ClientSession + run initialize handshake.

        Raises whatever mcp SDK raises on failure; caller (manager)
        wraps in try/except and per-server timeout.
        """
        stack = contextlib.AsyncExitStack()
        try:
            if self._config.transport == "stdio":
                params = StdioServerParameters(
                    command=self._config.command or "",
                    args=list(self._config.args),
                    env=self._config.env or None,
                    cwd=self._config.cwd,
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            else:  # http
                read, write = await stack.enter_async_context(
                    sse_client(self._config.url or "", headers=self._config.headers or None)
                )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._session = session
            self._stack = stack
        except BaseException:
            # Roll back partial connect so we don't leak fds / child procs.
            with contextlib.suppress(Exception):
                await stack.aclose()
            raise

    async def list_tools(self) -> list[McpToolDescriptor]:
        """Discover tools available on the server.

        Called once at connect time by McpManager. Re-calling is safe
        (returns the server's current view) but no caller relies on a
        cached value, so no caching here.
        """
        if self._session is None:
            raise RuntimeError(f"client '{self.name}' not connected")
        async with self._lock:
            result = await self._session.list_tools()
        return [
            McpToolDescriptor(
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.inputSchema or {}),
            )
            for tool in result.tools
        ]

    async def call_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        """Invoke a tool by its MCP-side name (no server prefix).

        Returns the flattened text content. Image / resource_link
        parts are replaced with placeholders (ToolMessage is str-typed).
        Caller wraps in timeout; this method itself doesn't time out.
        """
        if self._session is None:
            raise RuntimeError(f"client '{self.name}' not connected")
        async with self._lock:
            result = await self._session.call_tool(tool_name, arguments=args)

        # Flatten content list to text. mcp.types.TextContent has .text;
        # ImageContent has .data (binary) — replace with placeholder.
        # ResourceLinkContent has .uri.
        parts: list[str] = []
        for content in (result.content or []):
            ctype = getattr(content, "type", "")
            if ctype == "text":
                parts.append(getattr(content, "text", ""))
            elif ctype == "image":
                parts.append("[image omitted]")
            elif ctype == "resource_link":
                uri = getattr(content, "uri", "")
                parts.append(f"[resource: {uri}]")
            else:
                parts.append(f"[unsupported content type: {ctype}]")

        text = "\n".join(parts)
        if getattr(result, "isError", False):
            text = f"[tool error] {text}" if text else "[tool error] (no message)"
        return text

    async def disconnect(self) -> None:
        """Close session + reap child process. Idempotent."""
        if self._stack is None:
            return
        try:
            await self._stack.aclose()
        except Exception as e:
            logger.warning("client '%s' disconnect error (continuing): %s", self.name, e)
        finally:
            self._stack = None
            self._session = None
