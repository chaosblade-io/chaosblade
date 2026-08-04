"""Nodes that speak to the user directly must also write to the session file.

Most turns end with a streamed LLM reply, and ``intent_clarification`` persists
that itself. A few nodes instead author their own text — a recovery lookup
result, a refusal to open the approval gate — and for those the write is the
node's own responsibility:

- ``intent_clarification`` rebuilds its persist list from scratch each turn and
  only back-fills ``ToolMessage`` from history, so an ``AIMessage`` another node
  left in state is never picked up later.
- the turn-level fallback (``session_finalizer``) reaches the session file only
  when the caller passes a ``tui_session_store``. The CLI runner does; the server
  route does not, and its flush is additionally gated on an operational
  ``task_id`` that a pure dialogue turn never has.

So without an explicit write the text shows in the TUI and is gone from disk —
measured on sess_5f082a560921, whose rejected round is missing entirely.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.nodes.planning import intent_clarification as ic
from chaos_agent.agent.nodes.planning import intent_confirm as icf
from chaos_agent.agent.nodes.recover import recover_handler as rh
from chaos_agent.memory.tui_session_store import (
    TuiSessionStore,
    get_global_tui_session_store,
    set_global_tui_session_store,
)

_ACTIVE = {
    "task_id": "task-abc123",
    "fault_type": "pod-cpu",
    "target": {"namespace": "prod"},
    "blade_uid": "uid-1",
}


@pytest.fixture
def session():
    """A real store on a temp dir, registered as the global singleton."""
    previous = get_global_tui_session_store()
    with tempfile.TemporaryDirectory() as tmp:
        store = TuiSessionStore(Path(tmp))
        set_global_tui_session_store(store)
        sid = "sess-under-test"
        store.create(sid)
        try:
            yield store, sid
        finally:
            set_global_tui_session_store(previous)


def _disk_text(store: TuiSessionStore, sid: str) -> str:
    return "\n".join(str(m.get("content", "")) for m in store.read_dialogue(sid))


def _task_store(tasks: list):
    async def _get():
        class _Store:
            async def query_active(self, tenant_id=""):
                return tasks

            async def get(self, tid):
                return next((t for t in tasks if t["task_id"] == tid), None)

        return _Store()

    return _get


@pytest.mark.parametrize(
    "tasks, expected",
    [
        ([], "no active fault-injection experiments"),
        ([_ACTIVE], "Found 1 active experiment"),
        ([_ACTIVE, dict(_ACTIVE, task_id="task-def456")], "Found multiple active experiments"),
    ],
    ids=["none-active", "one-auto-selected", "several-to-choose"],
)
async def test_recover_lookup_result_reaches_disk(session, tasks, expected):
    store, sid = session
    with patch.object(rh, "get_task_store", _task_store(tasks)), \
            patch("chaos_agent.agent.dispatch.dispatch_node_message", AsyncMock()):
        out = await rh.recover_handler({"tui_session_id": sid})

    assert expected in str(out["messages"][0].content)      # graph state
    assert expected in _disk_text(store, sid)               # session file


async def test_recover_lookup_failure_reaches_disk(session):
    """The error path matters most: it is the one an operator comes back to read."""
    store, sid = session

    async def _boom():
        raise RuntimeError("task store down")

    with patch.object(rh, "get_task_store", _boom), \
            patch("chaos_agent.agent.dispatch.dispatch_node_message", AsyncMock()):
        out = await rh.recover_handler({"tui_session_id": sid})

    assert "task store down" in str(out["messages"][0].content)
    assert "task store down" in _disk_text(store, sid)


async def test_incomplete_spec_refusal_reaches_disk(session):
    store, sid = session
    out = await icf.intent_confirm({"tui_session_id": sid, "fault_spec": None})

    assert out["confirmed_intent"] is None
    assert "missing or unsupported scope" in str(out["messages"][0].content)
    assert "missing or unsupported scope" in _disk_text(store, sid)


async def test_persistence_failure_never_breaks_the_turn(session):
    """An audit write must not fail a turn whose verdict is already decided."""
    store, sid = session

    def _boom(*a, **k):
        raise OSError("disk full")

    with patch.object(rh, "get_task_store", _task_store([])), \
            patch("chaos_agent.agent.dispatch.dispatch_node_message", AsyncMock()), \
            patch.object(store, "append_dialogue", _boom):
        out = await rh.recover_handler({"tui_session_id": sid})

    assert "no active fault-injection experiments" in str(out["messages"][0].content)


async def test_no_session_id_is_silent(session):
    """Non-TUI callers (CLI, API) have no session file and must not error."""
    with patch.object(rh, "get_task_store", _task_store([])), \
            patch("chaos_agent.agent.dispatch.dispatch_node_message", AsyncMock()):
        out = await rh.recover_handler({})

    assert out["operation"] == "recover"
    assert "no active fault-injection experiments" in str(out["messages"][0].content)


# ── intent_clarification's two "chat" exits ───────────────────────────────────
# These looked safe: ``confirmed_intent="chat"`` skips the mid-conversation
# append and takes the full finalize path, which flushes state messages. But that
# path is reachable on the server only behind an operational ``task_id``, and a
# chat turn never allocates one — ``_allocate_operation_task_id`` runs for inject,
# batch and recover only, and the real session sess_5f082a560921 ended with
# ``task_ids: []``. So the last thing the user is told was lost there too.


async def test_dialogue_limit_goodbye_reaches_disk(session):
    store, sid = session
    node = ic.make_intent_clarification(llm=object(), tools=[], hook=None, registry=None)

    out = await node({
        "tui_session_id": sid,
        "dialogue_round": ic.MAX_DIALOGUE_ROUNDS,
        "messages": [],
        "confirmed_intent": None,
    })

    assert out["confirmed_intent"] == "chat"
    assert "goodbye" in str(out["messages"][0].content)
    assert "goodbye" in _disk_text(store, sid)


async def test_llm_failure_apology_reaches_disk(session):
    """The apology is the only record that a turn died on an upstream error."""
    store, sid = session

    class _BoomLLM:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            raise RuntimeError("upstream 503")

    node = ic.make_intent_clarification(llm=_BoomLLM(), tools=[], hook=None, registry=None)
    out = await node({
        "tui_session_id": sid,
        "dialogue_round": 1,
        "messages": [],
        "confirmed_intent": None,
    })

    assert out["confirmed_intent"] == "chat"
    assert "ran into a problem" in str(out["messages"][0].content)
    assert "ran into a problem" in _disk_text(store, sid)
