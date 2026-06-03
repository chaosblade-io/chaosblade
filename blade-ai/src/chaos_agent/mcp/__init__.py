"""E9 — MCP (Model Context Protocol) client integration.

Connects blade-ai to external MCP servers (filesystem, git, etc.)
and surfaces their tools to the LangGraph agent.

Default-off: ``settings.mcp_enabled=False`` skips all MCP code paths.
Per-server allowlist: each server must declare ``attach_to: [phase]``,
defaulting to ``[]`` (no phase).
"""

from chaos_agent.mcp.config import (
    McpConfigError,
    McpServerConfig,
    load_mcp_config,
)
from chaos_agent.mcp.manager import McpManager

__all__ = [
    "McpConfigError",
    "McpServerConfig",
    "load_mcp_config",
    "McpManager",
]
