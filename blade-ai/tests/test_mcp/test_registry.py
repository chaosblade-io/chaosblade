"""Tests for chaos_agent.mcp.registry + its wiring in McpManager.

The registry is the seam that lets the target_guard classifier recognise
operator-attached MCP tools instead of default-denying them as UNKNOWN.
These tests pin (a) the registry's own contract and (b) that McpManager
populates it on connect and clears it on disconnect — the lifecycle parity
that keeps a stale entry from ever classifying a disconnected server's tool.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from chaos_agent.mcp.config import McpServerConfig
from chaos_agent.mcp.manager import McpManager
from chaos_agent.mcp.registry import McpToolRegistry


@pytest.fixture(autouse=True)
def _clean_registry():
    """Process-global state — isolate every test."""
    McpToolRegistry.clear()
    yield
    McpToolRegistry.clear()


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


class TestRegistryContract:
    def test_register_then_get(self):
        McpToolRegistry.register("srv__tool", ("phase1", "verifier"))
        assert McpToolRegistry.get("srv__tool") == frozenset({"phase1", "verifier"})

    def test_get_unknown_returns_none(self):
        assert McpToolRegistry.get("never_registered") is None

    def test_register_empty_name_ignored(self):
        McpToolRegistry.register("", ("phase1",))
        assert McpToolRegistry.get("") is None

    def test_register_is_idempotent_by_name(self):
        McpToolRegistry.register("srv__tool", ("phase1",))
        McpToolRegistry.register("srv__tool", ("phase2",))
        assert McpToolRegistry.get("srv__tool") == frozenset({"phase2"})

    def test_clear_empties(self):
        McpToolRegistry.register("srv__tool", ("phase1",))
        McpToolRegistry.clear()
        assert McpToolRegistry.get("srv__tool") is None

    def test_snapshot_is_a_copy(self):
        McpToolRegistry.register("srv__tool", ("phase1",))
        snap = McpToolRegistry.snapshot()
        snap["srv__tool"] = frozenset({"phase2"})
        snap["injected"] = frozenset()
        # Live registry unaffected by mutating the snapshot.
        assert McpToolRegistry.get("srv__tool") == frozenset({"phase1"})
        assert McpToolRegistry.get("injected") is None


class TestManagerWiring:
    @pytest.mark.asyncio
    async def test_connect_all_populates_registry(self, monkeypatch):
        cfgs = [
            _stub_config("obs", attach_to=("phase1", "verifier"), ),
        ]
        mgr = McpManager(configs=cfgs)
        monkeypatch.setattr(
            "chaos_agent.mcp.manager.McpClient",
            lambda cfg: _make_fake_client(cfg.name, cfg.attach_to, ("q1", "q2")),
        )
        await mgr.connect_all()

        # Adapted full names are {server}__{tool}; each carries the server's
        # attach_to so the classifier can derive the READONLY verdict.
        assert McpToolRegistry.get("obs__q1") == frozenset({"phase1", "verifier"})
        assert McpToolRegistry.get("obs__q2") == frozenset({"phase1", "verifier"})

    @pytest.mark.asyncio
    async def test_disconnect_all_clears_registry(self, monkeypatch):
        cfgs = [_stub_config("obs", attach_to=("phase1",))]
        mgr = McpManager(configs=cfgs)
        monkeypatch.setattr(
            "chaos_agent.mcp.manager.McpClient",
            lambda cfg: _make_fake_client(cfg.name, cfg.attach_to),
        )
        await mgr.connect_all()
        assert McpToolRegistry.get("obs__a") is not None
        await mgr.disconnect_all()
        assert McpToolRegistry.get("obs__a") is None

    @pytest.mark.asyncio
    async def test_failed_server_not_registered(self, monkeypatch):
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

        assert McpToolRegistry.get("good__a") is not None
        # The failed server never adapted tools → never registered.
        assert McpToolRegistry.get("bad__a") is None

    @pytest.mark.asyncio
    async def test_phase2_attachment_recorded_verbatim(self, monkeypatch):
        """Posture A: a phase2 attachment is recorded as-is; the classifier
        (not the registry) decides the verdict + audit log."""
        cfgs = [_stub_config("mut", attach_to=("phase2",))]
        mgr = McpManager(configs=cfgs)
        monkeypatch.setattr(
            "chaos_agent.mcp.manager.McpClient",
            lambda cfg: _make_fake_client(cfg.name, cfg.attach_to, ("apply",)),
        )
        await mgr.connect_all()
        assert McpToolRegistry.get("mut__apply") == frozenset({"phase2"})
