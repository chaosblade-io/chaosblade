"""MCP tool guard-classification registry.

Mirrors the ``FaultProviderRegistry.classify_tool_target`` seam: the
generic target-guard classifier (``infer_effective_target``) holds no MCP
tool-name branch and no MCP vocabulary table. Instead it dispatches here
to learn (a) whether a tool name is an operator-attached MCP tool and
(b) which phases its server attached it to.

Populated by :class:`~chaos_agent.mcp.manager.McpManager` when tools are
adapted at connect time; cleared on ``disconnect_all``. Keyed by the
adapted LangChain tool's full name (``{server}__{tool}``, produced by
``adapter._safe_tool_name``) which is unique per ``(server, tool)`` pair,
so concurrent tasks sharing one process read a stable, race-free map
(writes happen only at connect / disconnect).

Design posture (A — trust ``attach_to``): MCP servers are
operator-installed (``~/.blade-ai/mcp.json``) and operator-attached to
phases. The guard cannot infer a k8s target from arbitrary MCP arguments,
so it does NOT try to police the operator's own wiring — a registered MCP
tool is classified READONLY (best-effort pass-through) and the safety of
that wiring is the operator's responsibility. An EMPTY registry (MCP
disabled, server failed to connect, or a direct classifier unit test)
leaves the classifier's default-deny behaviour byte-identical, so this
seam is a pure addition.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

logger = logging.getLogger(__name__)


class McpToolRegistry:
    """Process-global map of MCP tool full-name → attached phases.

    Class-method singleton (same shape as ``FaultProviderRegistry``): the
    classifier is a pure module function with no handle on the manager,
    so the attach metadata lives in a module-level registry it can consult
    without a signature change at any screener call site.
    """

    _attach_by_name: dict[str, frozenset[str]] = {}

    @classmethod
    def register(cls, tool_name: str, attach_to: Iterable[str]) -> None:
        """Record one MCP tool's attached phases (idempotent by name)."""
        if not tool_name:
            return
        cls._attach_by_name[tool_name] = frozenset(attach_to or ())

    @classmethod
    def get(cls, tool_name: str) -> frozenset[str] | None:
        """Return the attached phases for *tool_name*, or ``None`` when the
        name is not a registered MCP tool (the classifier then falls through
        to its pre-existing default-deny path)."""
        return cls._attach_by_name.get(tool_name)

    @classmethod
    def clear(cls) -> None:
        """Drop every entry — called on ``disconnect_all`` so a stale map
        can never classify a tool from a server that is no longer connected."""
        cls._attach_by_name.clear()

    @classmethod
    def snapshot(cls) -> dict[str, frozenset[str]]:
        """Shallow copy for audit / tests (never hand out the live map)."""
        return dict(cls._attach_by_name)


__all__ = ["McpToolRegistry"]
