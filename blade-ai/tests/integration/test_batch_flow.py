"""Integration test: batch execution loop-back flow.

Uses a real compiled LangGraph with InMemorySaver checkpointer to test
the full batch lifecycle: plan_builder → batch_setup → safety_check →
confirmation_gate(interrupt) → execute → verifier → save_memory →
batch_next → [loop | END].

Nodes that require external resources (LLM, kubectl, blade) are replaced
with lightweight stubs. The test verifies:
  1. Normal 3-fault batch: all approved, 3 results collected
  2. Reject middle fault: fault 1 rejected, faults 0 and 2 succeed
  3. Disconnect recovery: resume from checkpoint after interruption
  4. Single-fault plan_builder: normalized to 1-element batch
  5. State isolation: messages cleared between faults
"""

import asyncio
from typing import Optional
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, RemoveMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command, interrupt

from chaos_agent.agent.nodes.batch_next import batch_next
from chaos_agent.agent.nodes.batch_setup import batch_setup
from chaos_agent.agent.router import (
    route_after_batch_next,
    route_after_save_memory,
)
from chaos_agent.agent.state import AgentState, infer_task_state


# ---------------------------------------------------------------------------
# Stub nodes — minimal implementations that exercise the batch loop
# ---------------------------------------------------------------------------

_DEFAULT_FAULTS = [
    {"scope": "pod", "target": "cpu", "action": "fullload",
     "namespace": "test-ns", "names": ["pod-a"]},
    {"scope": "pod", "target": "mem", "action": "load",
     "namespace": "test-ns", "names": ["pod-b"]},
    {"scope": "pod", "target": "network", "action": "delay",
     "namespace": "test-ns", "names": ["pod-c"]},
]



async def stub_agent_loop(state: AgentState) -> dict:
    """Simulate agent_loop: activate skill + produce plan text."""
    spec_dict = state.get("fault_spec") or {}
    scope = spec_dict.get("scope", "")
    target = spec_dict.get("blade_target", "")
    action = spec_dict.get("blade_action", "")
    return {
        "skill_name": "k8s-chaos-skills",
        "plan": f"Inject {scope}-{target}-{action}",
        "messages": [AIMessage(content=f"Plan: {scope}-{target}-{action}")],
        "agent_loop_count": state.get("agent_loop_count", 0) + 1,
    }


async def stub_safety_check(state: AgentState) -> dict:
    return {"safety_status": "safe"}


async def stub_confirmation_gate(state: AgentState) -> dict:
    answer = interrupt({"type": "confirm", "task_id": state.get("task_id", "")})
    if answer == "rejected":
        return {"safety_status": "rejected", "safety_reason": "User rejected"}
    return {}


async def stub_execute(state: AgentState) -> dict:
    if state.get("safety_status") == "rejected":
        return {}
    idx = state.get("current_fault_index", 0)
    return {
        "blade_uid": f"blade-uid-{idx}",
        "messages": [AIMessage(content=f"Injected fault {idx}")],
    }


async def stub_save_memory(state: AgentState) -> dict:
    return {"finished_at": "2026-06-05T12:00:00+08:00"}


async def stub_reject(state: AgentState) -> dict:
    return {
        "error": state.get("safety_reason", "rejected"),
        "finished_at": "2026-06-05T12:00:00+08:00",
    }


def route_after_safety(state):
    if state.get("safety_status") == "rejected":
        return "reject"
    return "confirmation_gate"


def route_after_confirm(state):
    if state.get("safety_status") == "rejected":
        return "reject"
    return "execute"


# ---------------------------------------------------------------------------
# Graph builder — minimal batch pipeline
# ---------------------------------------------------------------------------

def build_test_batch_graph():
    """Build a minimal graph that exercises the batch loop-back pattern."""
    graph = StateGraph(AgentState)

    graph.add_node("batch_setup", batch_setup)
    graph.add_node("batch_next", batch_next)
    graph.add_node("agent_loop", stub_agent_loop)
    graph.add_node("safety_check", stub_safety_check)
    graph.add_node("confirmation_gate", stub_confirmation_gate)
    graph.add_node("execute", stub_execute)
    graph.add_node("save_memory", stub_save_memory)
    graph.add_node("reject", stub_reject)

    graph.set_entry_point("batch_setup")
    graph.add_edge("batch_setup", "agent_loop")
    graph.add_edge("agent_loop", "safety_check")
    graph.add_conditional_edges(
        "safety_check", route_after_safety,
        {"confirmation_gate": "confirmation_gate", "reject": "reject"},
    )
    graph.add_conditional_edges(
        "confirmation_gate", route_after_confirm,
        {"execute": "execute", "reject": "reject"},
    )
    graph.add_edge("execute", "save_memory")
    graph.add_conditional_edges(
        "save_memory", route_after_save_memory,
        {"batch_next": "batch_next", END: END},
    )
    graph.add_conditional_edges(
        "batch_next", route_after_batch_next,
        {"batch_setup": "batch_setup", END: END},
    )
    graph.add_conditional_edges(
        "reject", route_after_save_memory,
        {"batch_next": "batch_next", END: END},
    )

    return graph


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBatchFlow:

    @pytest.fixture
    def graph(self):
        checkpointer = MemorySaver()
        g = build_test_batch_graph()
        return g.compile(checkpointer=checkpointer), checkpointer

    def _make_graph(self):
        checkpointer = MemorySaver()
        g = build_test_batch_graph()
        return g.compile(checkpointer=checkpointer), checkpointer

    @pytest.mark.asyncio
    async def test_three_faults_all_approved(self, graph):
        """Normal batch: 3 faults, all approved → 3 results in batch_results."""
        compiled, _ = graph
        config = {"configurable": {"thread_id": "test-3-approved"}}
        init = {
            "messages": [], "current_fault_index": 0, "batch_results": [],
            "batch_submit_args": {
                "faults": _DEFAULT_FAULTS,
                "execution_order": "serial", "interval_seconds": 0,
            },
        }

        # Run until first interrupt (fault 0 confirmation_gate)
        await compiled.ainvoke(init, config)

        # Approve all 3 faults
        for i in range(3):
            state = await compiled.aget_state(config)
            assert state.next, f"Expected interrupt at fault {i}"
            await compiled.ainvoke(Command(resume="approved"), config)

        state = await compiled.aget_state(config)
        assert not state.next, "Graph should be done"
        results = state.values.get("batch_results", [])
        assert len(results) == 3
        for i, r in enumerate(results):
            assert r["blade_uid"] == f"blade-uid-{i}"
            assert r["task_state"] == "injecting"  # stub doesn't set verification
            assert r["task_id"].startswith("task-")

    @pytest.mark.asyncio
    async def test_reject_middle_fault(self, graph):
        """Fault 1 rejected: faults 0 and 2 succeed, fault 1 has error."""
        compiled, _ = graph
        config = {"configurable": {"thread_id": "test-reject-mid"}}
        init = {
            "messages": [], "current_fault_index": 0, "batch_results": [],
            "batch_submit_args": {
                "faults": _DEFAULT_FAULTS,
                "execution_order": "serial", "interval_seconds": 0,
            },
        }

        await compiled.ainvoke(init, config)

        # Fault 0: approve
        await compiled.ainvoke(Command(resume="approved"), config)
        # Fault 1: reject
        await compiled.ainvoke(Command(resume="rejected"), config)
        # Fault 2: approve
        await compiled.ainvoke(Command(resume="approved"), config)

        state = await compiled.aget_state(config)
        assert not state.next
        results = state.values.get("batch_results", [])
        assert len(results) == 3
        assert results[0]["blade_uid"] == "blade-uid-0"
        assert results[1]["blade_uid"] is None
        assert results[1]["error"] is not None
        assert results[2]["blade_uid"] == "blade-uid-2"

    @pytest.mark.asyncio
    async def test_disconnect_recovery(self, graph):
        """Disconnect after fault 0 completes, reconnect and resume."""
        compiled, checkpointer = graph
        config = {"configurable": {"thread_id": "test-disconnect"}}
        init = {
            "messages": [], "current_fault_index": 0, "batch_results": [],
            "batch_submit_args": {
                "faults": _DEFAULT_FAULTS,
                "execution_order": "serial", "interval_seconds": 0,
            },
        }

        # Run and approve fault 0
        await compiled.ainvoke(init, config)
        await compiled.ainvoke(Command(resume="approved"), config)

        # Verify paused at fault 1's confirmation
        state = await compiled.aget_state(config)
        assert state.next
        assert state.values.get("current_fault_index") == 1
        assert len(state.values.get("batch_results", [])) == 1

        # "Disconnect": create new compiled graph with same checkpointer
        compiled2 = build_test_batch_graph().compile(checkpointer=checkpointer)
        state2 = await compiled2.aget_state(config)
        assert state2.next, "Should still be paused after reconnect"
        assert state2.values.get("current_fault_index") == 1

        # Resume: approve faults 1 and 2
        await compiled2.ainvoke(Command(resume="approved"), config)
        await compiled2.ainvoke(Command(resume="approved"), config)

        state_final = await compiled2.aget_state(config)
        assert not state_final.next
        results = state_final.values.get("batch_results", [])
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_single_fault_normalized(self):
        """Single-fault batch: 1 fault in batch_submit_args."""
        single_faults = [
            {"scope": "pod", "target": "cpu", "action": "fullload",
             "namespace": "test-ns", "names": ["pod-x"]},
        ]
        compiled, _ = self._make_graph()
        config = {"configurable": {"thread_id": "test-single"}}
        init = {
            "messages": [], "current_fault_index": 0, "batch_results": [],
            "batch_submit_args": {
                "faults": single_faults,
                "execution_order": "serial", "interval_seconds": 0,
            },
        }

        await compiled.ainvoke(init, config)
        await compiled.ainvoke(Command(resume="approved"), config)

        state = await compiled.aget_state(config)
        assert not state.next
        results = state.values.get("batch_results", [])
        assert len(results) == 1
        assert results[0]["blade_uid"] == "blade-uid-0"

    @pytest.mark.asyncio
    async def test_messages_isolated_between_faults(self, graph):
        """Messages are cleared by RemoveMessage between fault iterations."""
        compiled, _ = graph
        config = {"configurable": {"thread_id": "test-isolation"}}
        init = {
            "messages": [], "current_fault_index": 0, "batch_results": [],
            "batch_submit_args": {
                "faults": _DEFAULT_FAULTS,
                "execution_order": "serial", "interval_seconds": 0,
            },
        }

        # Run until fault 0 confirmation
        await compiled.ainvoke(init, config)
        await compiled.ainvoke(Command(resume="approved"), config)

        # Paused at fault 1: check messages don't contain fault 0's content
        state = await compiled.aget_state(config)
        messages = state.values.get("messages", [])
        contents = [m.content for m in messages if hasattr(m, "content")]
        # Should NOT contain fault 0's "Injected fault 0" message
        assert not any("Injected fault 0" in c for c in contents), \
            f"Fault 0 messages leaked into fault 1: {contents}"
