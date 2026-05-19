"""Structured domain event dispatching for blade-ai graph nodes.

Uses ``langchain_core.callbacks.adispatch_custom_event`` to emit events
that flow through ``astream_events(v2)`` as ``on_custom_event`` type.
These replace the fragile ``on_chain_start/on_chain_end`` parsing that
depends on non-public LangGraph internal event format (``langsmith:nodes:``
tags and ``metadata["langgraph_node"]``).

Event names:
- ``phase_started``   — node entry, carries ``{node, phase}``
- ``phase_completed`` — node exit,  carries ``{node, phase}``

Consumed by ``parse_stream_event()`` in ``streaming.py`` which maps them
to ``StreamEvent(type="node_start"|"node_end")``.  Downstream EventBridge
and PhaseTimelineRenderer are unchanged — they already handle these types.
"""

from __future__ import annotations

import logging
from typing import Callable, Awaitable

from langchain_core.callbacks import adispatch_custom_event
from langgraph.errors import GraphInterrupt

from chaos_agent.agent.state import AgentState

logger = logging.getLogger(__name__)


async def dispatch_phase_started(node: str, phase: str) -> None:
    """Emit a phase_started domain event (node entry)."""
    await adispatch_custom_event("phase_started", {"node": node, "phase": phase})


async def dispatch_phase_completed(node: str, phase: str) -> None:
    """Emit a phase_completed domain event (node exit)."""
    await adispatch_custom_event("phase_completed", {"node": node, "phase": phase})


def with_phase_events(
    node_name: str,
    phase: str,
    node_fn: Callable[[AgentState], Awaitable[dict]],
) -> Callable[[AgentState], Awaitable[dict]]:
    """Wrap an async node function to emit phase_started/phase_completed events.

    Applied in ``graph.py`` at node registration — zero changes to node code.
    Handles ``GraphInterrupt`` (from ``interrupt()``) by NOT dispatching
    ``phase_completed`` on interrupt: the node hasn't finished, it's paused.
    On resume the wrapper fires ``phase_started`` again (LangGraph re-invokes
    the node), which is a harmless duplicate the TUI already handles.

    Parameters
    ----------
    node_name : str
        Must match the name used in ``graph.add_node(node_name, ...)``.
    phase : str
        The 5-stage stepper phase: ``"intent"`` | ``"safety"`` | ``"inject"``
        | ``"verify"`` | ``"recovery"``.
    node_fn : async callable
        The original node function ``(state: AgentState) -> dict``.
    """
    async def wrapped(state: AgentState) -> dict:
        # Phase events are cosmetic (TUI stepper), not functional.
        # If dispatch fails (e.g., no runnable context in tests), the node
        # must still execute its business logic.
        try:
            await dispatch_phase_started(node_name, phase)
        except Exception:
            logger.debug("dispatch_phase_started failed for %s (non-critical)", node_name)
        completed = False
        try:
            result = await node_fn(state)
            completed = True
            return result
        except GraphInterrupt:
            # interrupt() pauses the node — don't mark it as completed.
            # On resume, LangGraph re-invokes this wrapper from scratch,
            # firing phase_started again (harmless duplicate).
            raise
        finally:
            if completed:
                try:
                    await dispatch_phase_completed(node_name, phase)
                except Exception:
                    logger.debug(
                        "dispatch_phase_completed failed for %s (non-critical)",
                        node_name,
                    )
    return wrapped