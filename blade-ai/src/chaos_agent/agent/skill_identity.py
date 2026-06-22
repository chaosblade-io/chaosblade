"""Semantic accessors for the active skill identity.

Historically ``state["skill_name"]`` was used for two different ideas:

* the activated skill id used to load skill resources and prompts;
* a display-friendly fault type fallback such as ``pod-cpu-fullload``.

The first meaning remains ``skill_name`` for compatibility with persisted
tasks and tool calls.  Reporting code should use
``chaos_agent.agent.fault_spec.fault_type_from_state`` instead.
"""

from __future__ import annotations

from typing import Any, Mapping


def read_active_skill_name(state: Mapping[str, Any] | None) -> str:
    """Return the activated skill id stored on AgentState."""

    if not isinstance(state, Mapping):
        return ""
    value = state.get("skill_name") or ""
    return str(value)


def has_active_skill(state: Mapping[str, Any] | None) -> bool:
    """Whether the graph has activated a skill for execution."""

    return bool(read_active_skill_name(state))


__all__ = ["has_active_skill", "read_active_skill_name"]
