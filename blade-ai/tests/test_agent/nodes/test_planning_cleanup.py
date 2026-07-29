"""Tests for planning-phase capability-probe debug pod cleanup."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes.planning import _planning_cleanup
from chaos_agent.agent.nodes.planning.extract_planning_metadata import (
    extract_planning_metadata,
)
from chaos_agent.agent.nodes.planning.plan_builder import make_plan_builder


def _spy_cleanup(monkeypatch):
    """Patch the shared cleanup helper with a call-recording async spy.

    Both extract_planning_metadata and the plan_builder wrapper import the
    helper lazily from ``_planning_cleanup``, so patching the source module
    attribute intercepts every call site.
    """
    calls: list = []

    async def _spy(state):
        calls.append(state)
        return {}

    monkeypatch.setattr(
        "chaos_agent.agent.nodes.planning._planning_cleanup.cleanup_planning_debug_pods",
        _spy,
    )
    return calls


def _debug_tool_message(pod: str, ns: str, name: str = "kubectl_read") -> ToolMessage:
    meta = '{"name":"%s","namespace":"%s","uid":"u-%s","node":"n1"}' % (pod, ns, pod)
    return ToolMessage(
        content=(
            f"Creating debugging pod {pod} with container debugger on node n1.\n"
            f"[debug-pod-meta: {meta}]\n[debug-pod-ns: {ns}]"
        ),
        name=name,
        tool_call_id=f"tc-{pod}",
    )


@pytest.mark.asyncio
async def test_cleanup_deletes_probe_pod_and_records(monkeypatch):
    """A kubectl_read debug probe pod in history is deleted and recorded."""
    deleted: list[tuple[str, str]] = []

    async def _fake_delete(pod_name, kubeconfig, task_id, namespace=""):
        deleted.append((pod_name, namespace))
        return "confirmed"

    monkeypatch.setattr(
        "chaos_agent.agent.nodes.execute._debug_pod.delete_debug_pod", _fake_delete,
    )
    monkeypatch.setattr(
        "chaos_agent.agent.nodes.execute._kubeconfig_inject._resolve_kubeconfig",
        lambda state: "/kc",
    )

    state = {
        "task_id": "task-probe",
        "messages": [_debug_tool_message("node-debugger-abc", "default")],
    }
    update = await _planning_cleanup.cleanup_planning_debug_pods(state)

    assert deleted == [("node-debugger-abc", "default")]
    assert update.get("cleaned_debug_pods") == ["node-debugger-abc"]


@pytest.mark.asyncio
async def test_cleanup_skips_already_cleaned(monkeypatch):
    """Pods already in cleaned_debug_pods are not re-deleted (idempotent)."""
    deleted: list[str] = []

    async def _fake_delete(pod_name, kubeconfig, task_id, namespace=""):
        deleted.append(pod_name)
        return "confirmed"

    monkeypatch.setattr(
        "chaos_agent.agent.nodes.execute._debug_pod.delete_debug_pod", _fake_delete,
    )
    monkeypatch.setattr(
        "chaos_agent.agent.nodes.execute._kubeconfig_inject._resolve_kubeconfig",
        lambda state: "/kc",
    )

    state = {
        "task_id": "task-probe",
        "messages": [_debug_tool_message("node-debugger-abc", "default")],
        "cleaned_debug_pods": ["node-debugger-abc"],
    }
    update = await _planning_cleanup.cleanup_planning_debug_pods(state)

    assert deleted == []


@pytest.mark.asyncio
async def test_cleanup_no_probe_pods_is_noop(monkeypatch):
    """No debug pods in history → empty update, no deletes."""
    async def _fail_delete(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("delete_debug_pod should not be called")

    monkeypatch.setattr(
        "chaos_agent.agent.nodes.execute._debug_pod.delete_debug_pod", _fail_delete,
    )
    monkeypatch.setattr(
        "chaos_agent.agent.nodes.execute._kubeconfig_inject._resolve_kubeconfig",
        lambda state: "/kc",
    )

    state = {
        "task_id": "task-probe",
        "messages": [AIMessage(content="just planning text, no tools")],
    }
    update = await _planning_cleanup.cleanup_planning_debug_pods(state)

    assert "cleaned_debug_pods" not in update


# ---------------------------------------------------------------------------
# Wiring: the two planning exits actually invoke the cleanup helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_metadata_proceed_invokes_cleanup(monkeypatch):
    """Sink A: the proceed exit of extract_planning_metadata calls cleanup."""
    calls = _spy_cleanup(monkeypatch)
    state = {
        "task_id": "t",
        "skill_case_content": "already loaded",
        "messages": [
            ToolMessage(
                content="Planning finalized. Summary: my plan",
                name="finish_planning",
                tool_call_id="tc_fin",
            ),
        ],
    }
    await extract_planning_metadata(state)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_extract_metadata_nudge_skips_cleanup(monkeypatch):
    """Nudge/loop-back path returns before cleanup (planning continues)."""
    calls = _spy_cleanup(monkeypatch)
    # finish_planning rejected WITHOUT browsing catalogue -> nudge back to
    # agent_loop, early return BEFORE the cleanup call.
    state = {
        "task_id": "t",
        "messages": [
            ToolMessage(
                content="Planning rejected. Reason: no match",
                name="finish_planning",
                tool_call_id="tc_rej",
            ),
        ],
    }
    result = await extract_planning_metadata(state)
    assert result.get("planning_rejected") is True
    assert len(calls) == 0


@pytest.mark.asyncio
async def test_plan_builder_terminal_invokes_cleanup(monkeypatch):
    """Sink B: a plan_builder turn that ENDS the loop (no tool_calls) calls
    cleanup. llm=None makes the inner node return a plain no-tool message."""
    calls = _spy_cleanup(monkeypatch)
    node = make_plan_builder(llm=None, tools=[], hook=None, registry=None)
    result = await node({"task_id": "t", "messages": [HumanMessage(content="hi")]})
    # inner returned a plain AIMessage (no tool_calls) -> wrapper cleaned up
    assert len(calls) == 1
    assert result.get("messages")
