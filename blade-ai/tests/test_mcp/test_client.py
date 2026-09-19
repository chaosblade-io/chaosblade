"""Tests for chaos_agent.mcp.client — transport routing.

Pins which mcp-SDK transport context manager ``McpClient.connect()``
selects per configured ``transport``: ``http`` → Streamable HTTP
(modern MCP), ``sse`` → legacy HTTP+SSE, ``stdio`` → child process.
Regression guard for the 2026-09-17 fix where ``http`` was wrongly
routed to the deprecated ``sse_client``, hanging the initialize
handshake against Streamable-HTTP servers.
"""

import asyncio
import contextlib

import pytest

from chaos_agent.mcp import client as client_mod
from chaos_agent.mcp.config import McpServerConfig


class _FakeSession:
    async def initialize(self) -> None:
        pass


def _make_cfg(transport: str) -> McpServerConfig:
    return McpServerConfig(
        name="x",
        transport=transport,
        command="cmd" if transport == "stdio" else None,
        args=(),
        env={},
        cwd=None,
        url=None if transport == "stdio" else "http://example.com/mcp",
        headers={},
        attach_to=(),
        enabled=True,
        timeout_seconds=30,
    )


@pytest.fixture
def routed(monkeypatch):
    """Swap the three SDK transport CMs + ClientSession for recorders."""
    calls = {"stdio": 0, "sse": 0, "streamable": 0}

    @contextlib.asynccontextmanager
    async def fake_stdio(params):
        calls["stdio"] += 1
        yield object(), object()

    @contextlib.asynccontextmanager
    async def fake_sse(url, headers=None):
        calls["sse"] += 1
        yield object(), object()

    @contextlib.asynccontextmanager
    async def fake_streamable(url, headers=None, **kwargs):
        calls["streamable"] += 1
        yield object(), object(), lambda: "sid"

    @contextlib.asynccontextmanager
    async def fake_session(read, write):
        yield _FakeSession()

    monkeypatch.setattr(client_mod, "stdio_client", fake_stdio)
    monkeypatch.setattr(client_mod, "sse_client", fake_sse)
    monkeypatch.setattr(client_mod, "streamablehttp_client", fake_streamable)
    monkeypatch.setattr(client_mod, "ClientSession", fake_session)
    return calls


@pytest.mark.asyncio
async def test_http_routes_to_streamable(routed):
    c = client_mod.McpClient(_make_cfg("http"))
    await c.connect()
    assert routed == {"stdio": 0, "sse": 0, "streamable": 1}
    await c.disconnect()


@pytest.mark.asyncio
async def test_sse_routes_to_legacy_sse(routed):
    c = client_mod.McpClient(_make_cfg("sse"))
    await c.connect()
    assert routed == {"stdio": 0, "sse": 1, "streamable": 0}
    await c.disconnect()


@pytest.mark.asyncio
async def test_stdio_routes_to_stdio(routed):
    c = client_mod.McpClient(_make_cfg("stdio"))
    await c.connect()
    assert routed == {"stdio": 1, "sse": 0, "streamable": 0}
    await c.disconnect()


@pytest.mark.asyncio
async def test_disconnect_stops_owner_cleanly(routed):
    """disconnect() must signal the owner task and let IT exit the stack
    (same-task enter/exit), leaving no dangling owner task."""
    c = client_mod.McpClient(_make_cfg("http"))
    await c.connect()
    assert c._owner is not None
    await c.disconnect()
    assert c._owner is None
    assert c._session is None


@pytest.mark.asyncio
async def test_disconnect_idempotent(routed):
    c = client_mod.McpClient(_make_cfg("http"))
    await c.connect()
    await c.disconnect()
    await c.disconnect()  # second call is a no-op, must not raise


@pytest.mark.asyncio
async def test_owner_cancellation_unwinds_stack_in_owner_task(monkeypatch):
    """Edge A (2026-09-18): a CANCELLED owner task — host crash / loop
    teardown orphaning it, nobody called disconnect() — must still
    unwind the transport stack INSIDE the owner task via finally.
    Pre-fix, the CancelledError flew straight out of ``_stop.wait()``
    leaving the anyio cancel scopes to the loop's finalizer as
    shutdown noise (the noise observed when a probe crashed before
    calling disconnect). Same-task exit is the invariant; the exit
    ORDER is LIFO (session entered last, exits first).
    """
    exits: list[str] = []

    @contextlib.asynccontextmanager
    async def fake_streamable(url, headers=None, **kwargs):
        try:
            yield object(), object(), lambda: "sid"
        finally:
            exits.append("streamable")

    @contextlib.asynccontextmanager
    async def fake_session(read, write):
        try:
            yield _FakeSession()
        finally:
            exits.append("session")

    monkeypatch.setattr(client_mod, "streamablehttp_client", fake_streamable)
    monkeypatch.setattr(client_mod, "ClientSession", fake_session)

    c = client_mod.McpClient(_make_cfg("http"))
    await c.connect()
    owner = c._owner
    assert owner is not None and not owner.done()
    assert exits == []  # both CMs entered, none exited: steady state

    owner.cancel()  # nobody called disconnect() — the owner is orphaned
    with pytest.raises(asyncio.CancelledError):
        await owner

    # The stack unwound INSIDE the owner before the error propagated:
    # LIFO order, both levels exited, and the client reflects closed
    # state instead of leaking it to the loop's finalizer.
    assert exits == ["session", "streamable"]
    assert c._stack is None and c._session is None


class _FakeAnn:
    """Stand-in for mcp ToolAnnotations (pydantic) with model_dump."""

    def __init__(self, **kw):
        self._kw = kw

    def model_dump(self, exclude_none=True):
        return {k: v for k, v in self._kw.items() if v is not None}


class _FakeTool:
    def __init__(self, name, annotations=None):
        self.name = name
        self.description = f"{name} desc"
        self.inputSchema = {"type": "object"}
        self.annotations = annotations


class _FakeListResult:
    def __init__(self, tools):
        self.tools = tools


class _ListingSession:
    def __init__(self, tools):
        self._tools = tools

    async def initialize(self):
        pass

    async def list_tools(self):
        return _FakeListResult(self._tools)


@pytest.mark.asyncio
async def test_list_tools_captures_annotations(monkeypatch):
    """list_tools must carry the server's ToolAnnotations into the
    descriptor (as a plain dict) so effect.resolve_tool_effect can label
    the tool; a tool with no annotations yields None."""
    tools = [
        _FakeTool("cancel", _FakeAnn(destructiveHint=True, title="取消")),
        _FakeTool("get", _FakeAnn(readOnlyHint=True)),
        _FakeTool("ping", None),
    ]

    @contextlib.asynccontextmanager
    async def fake_streamable(url, headers=None, **kwargs):
        yield object(), object(), lambda: "sid"

    @contextlib.asynccontextmanager
    async def fake_session(read, write):
        yield _ListingSession(tools)

    monkeypatch.setattr(client_mod, "streamablehttp_client", fake_streamable)
    monkeypatch.setattr(client_mod, "ClientSession", fake_session)

    c = client_mod.McpClient(_make_cfg("http"))
    await c.connect()
    descriptors = await c.list_tools()
    by_name = {d.name: d for d in descriptors}
    assert by_name["cancel"].annotations == {"destructiveHint": True, "title": "取消"}
    assert by_name["get"].annotations == {"readOnlyHint": True}
    assert by_name["ping"].annotations is None
    await c.disconnect()
