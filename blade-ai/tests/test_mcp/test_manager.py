"""Tests for chaos_agent.mcp.manager — multi-server lifecycle + per-phase filter."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from chaos_agent.mcp.config import McpServerConfig
from chaos_agent.mcp.manager import McpManager


def _stub_descriptor(name="t", schema=None):
    from chaos_agent.mcp.client import McpToolDescriptor
    return McpToolDescriptor(
        name=name, description="", input_schema=schema or {"type": "object"},
    )


def _stub_config(name, attach_to=("phase1",), transport="stdio"):
    return McpServerConfig(
        name=name,
        transport=transport,
        command="x" if transport == "stdio" else None,
        args=(),
        env={},
        cwd=None,
        url=None if transport == "stdio" else "http://x",
        headers={},
        attach_to=attach_to,
        enabled=True,
        timeout_seconds=30,
    )


def _make_fake_client(name, attach_to, tool_names=("a", "b")):
    """Build a mock that mimics McpClient without touching the SDK."""
    client = MagicMock()
    client.name = name
    client.attach_to = attach_to
    client.timeout_seconds = 30
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_tools = AsyncMock(
        return_value=[_stub_descriptor(n) for n in tool_names]
    )
    client.call_tool = AsyncMock(return_value="result")
    return client


class TestConnectAll:
    @pytest.mark.asyncio
    async def test_empty_configs_noop(self):
        mgr = McpManager(configs=[])
        await mgr.connect_all()
        assert mgr.tools_for_phase("phase1") == []

    @pytest.mark.asyncio
    async def test_filter_by_attach_to(self, monkeypatch):
        cfgs = [
            _stub_config("a", attach_to=("phase1",)),
            _stub_config("b", attach_to=("phase2",)),
            _stub_config("c", attach_to=("phase1", "verifier")),
        ]
        mgr = McpManager(configs=cfgs)

        def _fake_client_factory(cfg):
            return _make_fake_client(cfg.name, cfg.attach_to)

        monkeypatch.setattr("chaos_agent.mcp.manager.McpClient", _fake_client_factory)

        await mgr.connect_all()
        phase1 = mgr.tools_for_phase("phase1")
        phase2 = mgr.tools_for_phase("phase2")
        verifier = mgr.tools_for_phase("verifier")

        # Each client has 2 tools; check that filtering by phase works
        names_phase1 = sorted(t.name for t in phase1)
        # Server 'a' (2 tools) + Server 'c' (2 tools) = 4 phase1 tools
        assert len(names_phase1) == 4
        assert all(n.startswith(("a__", "c__")) for n in names_phase1)
        # Phase 2 only has server 'b' (2 tools)
        assert len(phase2) == 2
        assert all(t.name.startswith("b__") for t in phase2)
        # Verifier only has server 'c' (2 tools)
        assert len(verifier) == 2

    @pytest.mark.asyncio
    async def test_failed_connect_isolated(self, monkeypatch):
        """One server failing must NOT prevent other servers from
        loading. Isolation is critical for E9."""
        cfgs = [
            _stub_config("good", attach_to=("phase1",)),
            _stub_config("bad", attach_to=("phase1",)),
        ]
        mgr = McpManager(configs=cfgs)

        def _factory(cfg):
            c = _make_fake_client(cfg.name, cfg.attach_to)
            if cfg.name == "bad":
                c.connect = AsyncMock(side_effect=RuntimeError("nope"))
            return c

        monkeypatch.setattr("chaos_agent.mcp.manager.McpClient", _factory)

        await mgr.connect_all()
        phase1 = mgr.tools_for_phase("phase1")
        assert all(t.name.startswith("good__") for t in phase1)
        assert len(phase1) == 2  # good has 2 tools

    @pytest.mark.asyncio
    async def test_failed_list_tools_isolated_and_disconnected(self, monkeypatch):
        """Regression: connect succeeds but list_tools raises. The
        broken server must:
          (a) NOT contribute tools to other phases
          (b) Get disconnect() called so stdio child / HTTP session
              doesn't leak
          (c) NOT prevent the good server from loading
        """
        cfgs = [
            _stub_config("good", attach_to=("phase1",)),
            _stub_config("broken_list", attach_to=("phase1",)),
        ]
        mgr = McpManager(configs=cfgs)

        disconnect_calls: list[str] = []

        def _factory(cfg):
            c = _make_fake_client(cfg.name, cfg.attach_to)
            if cfg.name == "broken_list":
                c.list_tools = AsyncMock(side_effect=RuntimeError("server bug"))
            original_disc = c.disconnect

            async def _track_disc():
                disconnect_calls.append(c.name)
                await original_disc()
            c.disconnect = _track_disc
            return c

        monkeypatch.setattr("chaos_agent.mcp.manager.McpClient", _factory)

        await mgr.connect_all()
        phase1 = mgr.tools_for_phase("phase1")
        # Only 'good' contributes
        assert all(t.name.startswith("good__") for t in phase1)
        assert len(phase1) == 2
        # Critical: broken server's disconnect must have been called
        # so its connection doesn't leak (the resource was opened
        # successfully — connect() succeeded — even if list_tools failed)
        assert "broken_list" in disconnect_calls

    @pytest.mark.asyncio
    async def test_disconnect_all_calls_each_client(self, monkeypatch):
        cfgs = [_stub_config("a"), _stub_config("b")]
        mgr = McpManager(configs=cfgs)

        clients_seen = []

        def _factory(cfg):
            c = _make_fake_client(cfg.name, cfg.attach_to)
            clients_seen.append(c)
            return c

        monkeypatch.setattr("chaos_agent.mcp.manager.McpClient", _factory)

        await mgr.connect_all()
        await mgr.disconnect_all()
        for c in clients_seen:
            c.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_default_attach_to_empty_means_no_phase(self, monkeypatch):
        """E9 default-secure: attach_to=() means tool surfaces to no phase."""
        cfgs = [_stub_config("a", attach_to=())]
        mgr = McpManager(configs=cfgs)
        monkeypatch.setattr(
            "chaos_agent.mcp.manager.McpClient",
            lambda cfg: _make_fake_client(cfg.name, cfg.attach_to),
        )
        await mgr.connect_all()
        for phase in ("clarification", "phase1", "phase2", "verifier"):
            assert mgr.tools_for_phase(phase) == []
