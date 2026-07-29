"""End-to-end wiring for the host_shell backend (inject → detect → recover).

Complements the unit-level provider tests by exercising the full host path the
way production does, without a live transport:

1. inject → detect: a successful ``host_inject`` carrier on a resolved host
   channel classifies as ``host_native`` via ``detect_method`` (and NOT on a
   non-host channel).
2. detect → recover: ``HostShellProvider.recover`` is the no-LLM verdict for a
   host-native (no-UID) fault — there is no code-side reverse derivation, so it
   reports ``unrecovered``/Layer-2 skipped. The reverse command lives in the
   skill case and is executed by the LLM recover loop, not here.
3. host_read stays read-only: a mutating command is rejected at the tool gate
   without touching the transport.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import ToolMessage

from chaos_agent.agent.providers import FaultProviderRegistry
from chaos_agent.agent.providers.host_shell import HostShellProvider


@pytest.fixture(autouse=True)
def _isolate_registry():
    FaultProviderRegistry.clear()
    yield
    FaultProviderRegistry.clear()
    FaultProviderRegistry.register_builtins()


def _host_inject_ok(output: str = "filled /var/lib/chaos_fill.img") -> ToolMessage:
    return ToolMessage(content=output, name="host_inject", tool_call_id="h1")


# -- 1. inject → detect ------------------------------------------------------


def test_host_inject_detects_as_host_native_on_host_channel():
    FaultProviderRegistry.register_builtins()
    msgs = [_host_inject_ok()]
    # Only a resolved host channel (is_host=True) with no blade_uid classifies
    # a raw-command carrier as host_native.
    assert FaultProviderRegistry.detect_method(msgs, None, is_host=True) == "host_native"


def test_host_inject_not_host_native_off_host_channel():
    FaultProviderRegistry.register_builtins()
    msgs = [_host_inject_ok()]
    assert FaultProviderRegistry.detect_method(msgs, None, is_host=False) is None


def test_failed_host_inject_is_not_a_carrier():
    FaultProviderRegistry.register_builtins()
    msgs = [ToolMessage(content="Error: host_inject blocked", name="host_inject",
                        tool_call_id="h1")]
    assert FaultProviderRegistry.detect_method(msgs, None, is_host=True) is None


# -- 2. detect → recover (no-LLM verdict, no code-side reversal) -------------


async def test_host_recover_is_no_llm_unrecovered_verdict():
    # host_shell has no blade_uid and no code-side reverse derivation (the
    # reverse command lives in the skill case, executed by the LLM recover
    # loop). The no-LLM recover() therefore reports an honest unrecovered
    # verdict with Layer 1 not-applicable and Layer 2 skipped.
    state = {
        "execution_artifacts": [
            {"type": "host_command",
             "command": "iptables -A INPUT -p tcp --dport 80 -j DROP"},
        ],
    }
    mock_exec = AsyncMock()

    with patch("chaos_agent.transports.execute_via_transport", mock_exec):
        result = await HostShellProvider().recover(state, None, task_id="t1")

    # No transport call — recover() derives no reverse command on its own.
    mock_exec.assert_not_awaited()
    assert result.recovered is False
    assert result.level == "unrecovered"
    assert result.blade_uid == ""
    assert result.layer1["status"] == "skipped"
    assert result.layer2["status"] == "skipped"
    assert result.failure is not None
    assert result.warnings  # non-empty warning that recovery is unverified


async def test_host_recover_carries_blade_uid_through():
    # A blade_uid passed through kwargs is echoed back (defensive: the caller
    # may have recovered it from message history).
    result = await HostShellProvider().recover({}, None, blade_uid="abc123")
    assert result.blade_uid == "abc123"
    assert result.recovered is False


# -- 3. host_read stays read-only --------------------------------------------


async def test_host_read_rejects_mutating_command():
    from chaos_agent.tools.host_cmd import host_read

    # A fault/mutating binary must be rejected by the read-only gate BEFORE any
    # transport call — assert the guidance error, no transport needed.
    out = await host_read.ainvoke({"command": "rm -rf /var/lib/chaos_fill.img"})
    assert out.startswith("Error:")
    assert "read-only diagnostic" in out
    # Guidance still points to host_inject only for genuine fault-injection.
    assert "host_inject" in out
