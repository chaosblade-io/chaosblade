"""MCP client: one connection to one external MCP server.

Owns the session lifecycle + a per-session asyncio lock that
serialises tool calls (MCP JSON-RPC is order-sensitive — single
in-flight request per session). If a single server needs high
concurrency, the right fix is multiple sessions / connection pool,
not unlocking; Phase 1 sticks with single-session per server.

Transport-agnostic interface: stdio (child process via mcp SDK's
``stdio_client``), Streamable HTTP (``streamablehttp_client``, the
modern MCP transport that ``transport="http"`` maps to — matching
Claude/Cursor semantics), or legacy HTTP+SSE (``sse_client``, for
``transport="sse"`` to reach older servers). All expose the same
``ClientSession``.
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
from mcp.client.streamable_http import streamablehttp_client

from chaos_agent.mcp.config import McpServerConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpToolDescriptor:
    """Subset of mcp.types.Tool that the adapter needs.

    ``annotations`` carries the server's self-declared MCP
    ``ToolAnnotations`` (``readOnlyHint`` / ``destructiveHint`` / ...) as
    a plain dict, or ``None`` when the server sent none. Advisory only:
    ``effect.resolve_tool_effect`` reads it to label the tool for the
    LLM; it never gates execution.
    """
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] | None = None


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
        # Owner-task lifecycle. The mcp SDK clients are anyio
        # structured-concurrency contexts (task-affine cancel scopes): the
        # task that ENTERS the stack must be the one that EXITS it. A
        # dedicated owner task enters + exits; every other task only uses
        # the session streams (call_tool) and signals stop to close.
        self._owner: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._ready: asyncio.Event | None = None
        self._connect_error: BaseException | None = None

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

        The enter/exit of the anyio-backed transport stack is confined to
        a dedicated owner task (``_owner_loop``) so the cancel-scope
        task-affinity rule holds no matter which task calls connect or
        disconnect. Raises whatever mcp SDK raises on failure; caller
        (manager) wraps in try/except and per-server timeout.
        """
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._connect_error = None
        self._owner = asyncio.create_task(
            self._owner_loop(), name=f"mcp-owner-{self.name}"
        )
        await self._ready.wait()
        if self._connect_error is not None:
            raise self._connect_error

    async def _enter_transport(self, stack: contextlib.AsyncExitStack):
        """Enter the transport CM on ``stack``; return ``(read, write)``."""
        if self._config.transport == "stdio":
            params = StdioServerParameters(
                command=self._config.command or "",
                args=list(self._config.args),
                env=self._config.env or None,
                cwd=self._config.cwd,
            )
            return await stack.enter_async_context(stdio_client(params))
        if self._config.transport == "sse":
            # Legacy HTTP+SSE transport. Deprecated in MCP spec
            # 2025-03-26 (superseded by Streamable HTTP) but kept so
            # blade-ai can still reach older SSE-only servers.
            return await stack.enter_async_context(
                sse_client(self._config.url or "", headers=self._config.headers or None)
            )
        # http → Streamable HTTP (modern MCP)
        read, write, _ = await stack.enter_async_context(
            streamablehttp_client(
                self._config.url or "", headers=self._config.headers or None
            )
        )
        return read, write

    async def _owner_loop(self) -> None:
        """Enter the stack, hold it until stopped, exit in THIS task.

        Confining both enter and exit to this single task satisfies
        anyio's cancel-scope task-affinity rule regardless of which tasks
        call connect()/disconnect()/call_tool(). EVERY exit path — the
        stop signal, a handshake failure, or cancellation of the owner
        task itself (host crash / event-loop teardown orphaning it when
        nobody called disconnect()) — unwinds the stack inside this
        task, so fds / child processes / anyio cancel scopes are never
        left to the loop's finalizer as shutdown noise.
        """
        stack = contextlib.AsyncExitStack()
        try:
            read, write = await self._enter_transport(stack)
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException as e:
            # Handshake failed — roll back here (same task) so we don't
            # leak fds / child procs, then surface the error to connect().
            self._connect_error = e
            with contextlib.suppress(Exception):
                await stack.aclose()
            self._ready.set()
            return
        self._session = session
        self._stack = stack
        self._ready.set()
        try:
            await self._stop.wait()
        finally:
            # finally, not except: a CANCELLED owner (host crash / loop
            # teardown — nobody called disconnect()) must still unwind
            # the stack in THIS task. Pre-fix, the CancelledError flew
            # straight out of _stop.wait(), leaving the anyio cancel
            # scopes to the loop's finalizer. Single-cancellation
            # semantics let this await run to completion; the error
            # resumes propagating only after the finally completes.
            try:
                await stack.aclose()
            except Exception as e:
                logger.warning(
                    "client '%s' disconnect error (continuing): %s", self.name, e
                )
            finally:
                self._stack = None
                self._session = None

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
                annotations=(
                    tool.annotations.model_dump(exclude_none=True)
                    if getattr(tool, "annotations", None) is not None
                    else None
                ),
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
        """Signal the owner task to close and wait for it. Idempotent.

        Never closes the stack from the caller's task — that would
        violate anyio cancel-scope task affinity. The owner task exits
        the stack in its own task once ``_stop`` is set.
        """
        owner = self._owner
        if owner is None:
            return
        self._owner = None
        if self._stop is not None:
            self._stop.set()
        try:
            await asyncio.wait_for(owner, timeout=self._config.timeout_seconds)
        except asyncio.TimeoutError:
            # Owner stuck (e.g. mid-handshake): cancel it; cancellation
            # unwinds the enter inside the owner task, still same-task.
            owner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await owner
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("client '%s' disconnect error (continuing): %s", self.name, e)
        finally:
            self._stack = None
            self._session = None
