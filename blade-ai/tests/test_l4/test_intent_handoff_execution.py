"""Graph-level tests for l4-intent-handoff-parity (tier1 langgraph lesson).

Function-level green does NOT prove the graph boundary is wired right —
tier1-speedup shipped a bridge whose fields langgraph silently dropped
because of a missing state channel. These tests drive the REAL
``_L4ExecutionMixin._async_execute`` entry with a capture inject graph
and assert the pipeline's initial state actually carries the three
bridged fields. A companion test asserts semantic equivalence with the
TUI handoff (same checkpoint data → same evidence fields).
"""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import SystemMessage

from chaos_agent.agent.intent_handoff import build_pipeline_handoff_from_intent_state
from chaos_agent.agent.state_mgmt.state_builders import build_inject_initial_state
from chaos_agent.l4 import adapter as _adapter_mod
from chaos_agent.l4.execution import _L4ExecutionMixin
from chaos_agent.l4.schemas import L4TestTask

# Underscore aliases so pytest does not collect the source function whose
# name starts with test_ (same pattern as test_adapter.py).
attach_intent_handoff = _adapter_mod.attach_intent_handoff
_to_initial_state = _adapter_mod.test_task_to_initial_state


def _fault_intent():
    return {
        "scope": "pod",
        "target": "cpu",
        "action": "fullload",
        "namespace": "cms-demo",
        "names": ["drill-target"],
        "labels": {"app": "myapp"},
        "params": {"cpu-percent": "80"},
        "duration_seconds": 300,
    }


def _task(**payload_extra):
    payload = {
        "fault_intent": _fault_intent(),
        "auto_recover": False,  # focus the test on the inject leg
        "intent_thread_id": "chaos-session-42",
    }
    payload.update(payload_extra)
    return L4TestTask(
        task_id="t-graph-001",
        intent="inject pod cpu fault",
        payload=payload,
    )


def _checkpoint_values():
    """IntentState values as the approved path (_commit_inject_handoff)
    would have checkpointed them."""
    return {
        "confirmed_intent": "inject",
        "tui_session_id": "",
        "handoff_summary": (
            "[Intent Clarification Summary]\nDialogue rounds: 2\n"
            "Confirmed intent: inject\n"
            "Fault: pod-cpu-fullload → pod/cpu/fullload @ cms-demo"
        ),
        "probe_snapshot": {
            "facts": [
                {
                    "fact": "pod drill-target running on node-1",
                    "source_tool": "kubectl_read",
                    "probed_at": "2026-08-27T10:00:00+00:00",
                }
            ]
        },
        "progress_ledger": {
            "state": {"established_facts": ["drill-target has 3 replicas"]},
        },
    }


class _FakeIntentGraph:
    def __init__(self, values=None):
        self._values = values

    async def aget_state(self, config):
        if self._values is None:
            return None
        return SimpleNamespace(values=self._values)


class _CaptureInjectGraph:
    """Records the initial_state the pipeline is invoked with; the astream
    is an empty stream and aget_state reports a benign terminal state."""

    def __init__(self):
        self.captured_input = None

    async def astream_events(self, initial_state, config, version="v2"):
        self.captured_input = initial_state
        return
        yield  # unreachable — makes this an (empty) async generator

    async def aget_state(self, config):
        return SimpleNamespace(values={"task_id": "t-graph-001"}, tasks=[])


class _FakePool:
    def __init__(self, intent_values):
        self.intent_graph = _FakeIntentGraph(intent_values)
        self.inject_graph = _CaptureInjectGraph()
        self.skill_registry = None


class _FakeSessionStore:
    """Records create_session calls; has_active always False so the
    bootstrap's double-invocation guard never suppresses the call under
    test."""

    def __init__(self):
        self.created = []

    def has_active(self, task_id):
        return False

    def create_session(
        self,
        task_id,
        operation="",
        tui_session_id="",
        parent_task_id="",
        baseline_messages=None,
        initial_messages=None,
    ):
        self.created.append(
            {
                "task_id": task_id,
                "operation": operation,
                "initial_messages": initial_messages,
            }
        )


class _ExecutionHost(_L4ExecutionMixin):
    """Minimal host providing the attributes the mixin's closures expect."""

    def __init__(self):
        self._cancel_event = asyncio.Event()


class TestExecutionLevelBridge:
    async def test_pipeline_initial_state_carries_handoff(self):
        """End-to-end through _async_execute: payload declares the dialogue
        source → the inject graph is invoked with snapshot + ledger + seed
        message. This is the graph-boundary assertion the langgraph lesson
        demands — attach() being correct is not enough, the fields must
        actually survive into the invocation."""
        pool = _FakePool(_checkpoint_values())
        host = _ExecutionHost()

        result = await host._async_execute(pool, runtime=None, task=_task())

        captured = pool.inject_graph.captured_input
        assert captured is not None, "inject graph was never invoked"
        assert captured["probe_snapshot"] == _checkpoint_values()["probe_snapshot"]
        assert captured["progress_ledger"] == _checkpoint_values()["progress_ledger"]
        assert captured["messages"], "seed SystemMessage missing"
        assert isinstance(captured["messages"][0], SystemMessage)
        assert captured["messages"][0].content.startswith("[Intent Clarification Summary]")
        # Task identity must stay the payload's, not the dialogue thread's.
        assert captured["task_id"] == "t-graph-001"
        # The execute flow itself must not be disturbed by the bridge.
        assert result is not None

    async def test_no_declaration_cold_start_unchanged(self):
        """Without intent_thread_id the pipeline input is byte-identical to
        the pre-change cold start (no snapshot/ledger keys, empty messages)."""
        pool = _FakePool(_checkpoint_values())  # checkpoint exists, but…
        host = _ExecutionHost()
        task = _task()
        task.payload.pop("intent_thread_id")

        await host._async_execute(pool, runtime=None, task=task)

        captured = pool.inject_graph.captured_input
        assert captured is not None
        assert "probe_snapshot" not in captured
        assert "progress_ledger" not in captured
        assert captured["messages"] == []

    async def test_unreadable_checkpoint_cold_start(self):
        """Declared thread but no checkpoint → silent degrade, inject still
        dispatched with the cold-start state."""
        pool = _FakePool(None)
        host = _ExecutionHost()

        result = await host._async_execute(pool, runtime=None, task=_task())

        captured = pool.inject_graph.captured_input
        assert captured is not None
        assert "probe_snapshot" not in captured
        assert "progress_ledger" not in captured
        assert result is not None


class TestBootstrapParity:
    """Task-file birth parity: the TUI dispatch bootstraps the task file
    with the handoff summary as its FIRST entry (P0-7-6 boundary marker,
    via bootstrap_task_session). The L4 execution bootstrap must do the
    same when the bridge seeded a summary — otherwise the task file is
    born empty and only gains the marker via an in-run flush (or never,
    if the run dies before any memory node)."""

    def _store(self, monkeypatch):
        from chaos_agent.memory import session_store as _ss_mod

        store = _FakeSessionStore()
        monkeypatch.setattr(_ss_mod, "_global_session_store", store)
        return store

    async def test_task_file_born_with_handoff_marker(self, monkeypatch):
        store = self._store(monkeypatch)
        pool = _FakePool(_checkpoint_values())
        host = _ExecutionHost()

        await host._async_execute(pool, runtime=None, task=_task())

        assert len(store.created) == 1, "bootstrap must run exactly once"
        record = store.created[0]
        assert record["task_id"] == "t-graph-001"
        assert record["operation"] == "inject"
        initial = record["initial_messages"]
        assert initial and len(initial) == 1
        assert initial[0].content.startswith("[Intent Clarification Summary]")

    async def test_task_file_cold_start_no_marker_fabricated(self, monkeypatch):
        """No bridge → no seed → the task file is born WITHOUT the marker
        (pre-change behaviour — nothing fabricated)."""
        store = self._store(monkeypatch)
        pool = _FakePool(_checkpoint_values())  # checkpoint exists, but…
        host = _ExecutionHost()
        task = _task()
        task.payload.pop("intent_thread_id")

        await host._async_execute(pool, runtime=None, task=task)

        assert len(store.created) == 1
        assert store.created[0]["initial_messages"] is None


class TestTuiParity:
    async def test_l4_bridge_matches_tui_handoff_semantics(self):
        """Same checkpoint data must yield the same evidence fields as the
        TUI dispatch path (turn_event_stream): TUI consumes
        build_pipeline_handoff_from_intent_state → build_inject_initial_state
        explicit args; L4 consumes attach_intent_handoff. The three evidence
        fields must be equal — one source of truth, two consumers."""
        values = _checkpoint_values()

        # TUI shape (mirrors server/routes/turn_event_stream.py L548-597)
        tui_handoff = build_pipeline_handoff_from_intent_state(
            values, operation="inject", task_id="t-graph-001",
        )
        tui_state = build_inject_initial_state(
            task_id="t-graph-001",
            confirmed_intent="inject",
            fault_spec=tui_handoff.fault_spec,
            messages=[SystemMessage(content=tui_handoff.handoff_summary)],
            progress_ledger=tui_handoff.progress_ledger,
            probe_snapshot=tui_handoff.probe_snapshot,
        )

        # L4 shape: payload conversion + bridge
        l4_state = _to_initial_state(_task())
        l4_state = await attach_intent_handoff(
            l4_state, _FakePool(values), "chaos-session-42",
        )

        assert l4_state["probe_snapshot"] == tui_state["probe_snapshot"]
        assert l4_state["progress_ledger"] == tui_state["progress_ledger"]
        assert l4_state["messages"][0].content == tui_state["messages"][0].content
