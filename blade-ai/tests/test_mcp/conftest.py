"""Shared fixtures for tests/test_mcp.

``McpToolRegistry`` is a process-global class-level dict (mirroring
``FaultProviderRegistry``). Several manager tests call ``connect_all``
without a matching ``disconnect_all``, so registered tool names would
otherwise leak into later test directories (e.g. test_target_guard, whose
classifier consults the same registry) and make the suite order-dependent.
This autouse fixture resets that global after every test in this package so
MCP tests are good citizens and cannot pollute downstream suites.
"""

import pytest

from chaos_agent.mcp.registry import McpToolRegistry


@pytest.fixture(autouse=True)
def _clear_mcp_registry():
    yield
    McpToolRegistry.clear()
