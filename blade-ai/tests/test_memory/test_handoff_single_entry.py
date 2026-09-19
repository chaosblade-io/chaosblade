"""The handoff summary must land in the task file once, not twice.

Observed in task-866648cc: ``[Intent Clarification Summary]`` appears at index 0
and again at index 4, 5ms apart. The first carries ``node=intent_clarification``
and no ``id``; the second carries an ``id`` and no ``node``.

They are the same message on two paths. ``runner`` puts it in the graph input,
``create_session`` writes it as the first jsonl entry, and ``memory_hook`` records
the state copy. ``read_session`` already dedups — but ``_message_dedup_key`` is
id-first, and the jsonl copy was written BEFORE ``add_messages`` assigned a UUID.
So one copy keyed on its content and the other on ``id:<uuid>``, and both survived.

Fix: one object with an explicit id, reused for both writes. ``add_messages``
preserves an existing id rather than replacing it, so the two keys match. Same
shape as 1c21325, which added ``id=str(uuid4())`` to HumanMessage in memory_nodes
for the identical reason.
"""

from __future__ import annotations

import uuid

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph.message import add_messages

from chaos_agent.memory.session_store import (
    _message_dedup_key,
    _serialize_message_full,
)

_HANDOFF = (
    "[Intent Clarification Summary]\n"
    "Dialogue rounds: 17\n"
    "Confirmed intent: inject\n"
    "Fault: pod-network-drop → pod/network/drop @ arms-prom"
)


def _key(msg) -> str:
    """Dedup key as ``read_session`` computes it, via the on-disk shape."""
    return _message_dedup_key(_serialize_message_full(msg))


class TestDedupNeedsMatchingIds:
    def test_add_messages_preserves_an_explicit_id(self):
        """The property the whole fix rests on.

        If ``add_messages`` replaced ids, giving the object one up front would
        not help — the state copy would diverge again.
        """
        msg = SystemMessage(content=_HANDOFF, id=str(uuid.uuid4()))
        merged = add_messages([], [msg])[0]
        assert merged.id == msg.id

    def test_add_messages_assigns_an_id_when_absent(self):
        """Why the two copies diverged: only the state copy got a UUID."""
        merged = add_messages([], [SystemMessage(content=_HANDOFF)])[0]
        assert merged.id

    def test_one_object_with_an_id_dedups_to_one_entry(self):
        msg = SystemMessage(content=_HANDOFF, id=str(uuid.uuid4()))
        state_copy = add_messages([], [msg])[0]
        assert _key(msg) == _key(state_copy)

    def test_without_an_id_the_two_copies_do_not_dedup(self):
        """Pins the bug itself, so a regression is visible rather than silent."""
        jsonl_copy = SystemMessage(content=_HANDOFF)
        state_copy = add_messages([], [SystemMessage(content=_HANDOFF)])[0]
        assert _key(jsonl_copy) != _key(state_copy)


class TestDedupKeyContract:
    """``_message_dedup_key`` is id-first; that is the assumption that broke."""

    def test_an_id_wins_over_content(self):
        a = SystemMessage(content="one", id="same")
        b = SystemMessage(content="two — different content", id="same")
        assert _key(a) == _key(b)

    def test_content_is_used_only_without_an_id(self):
        a = SystemMessage(content=_HANDOFF)
        b = SystemMessage(content=_HANDOFF)
        assert _key(a) == _key(b)
        assert not _key(a).startswith("id:")

    def test_different_types_with_equal_content_stay_distinct(self):
        assert _key(SystemMessage(content="x")) != _key(HumanMessage(content="x"))


class TestTurnStreamBuildsOneIdentifiedHandoff:
    """Source-level, because reaching this line needs a live TUI dual-graph run.

    The guard originally pointed at ``AgentRunner.converse_stream`` (the local
    TUI twin). That twin was retired 2026-09-01 — and its retirement exposed
    that the P0-7-6 fix had never reached the server ``/turn`` flow:
    ``_run_inject_pipeline`` in ``server/routes/turn_event_stream.py`` was
    still building TWO bare ``_SM(content=_handoff)`` objects (one for the
    session-store jsonl first entry, one for the pipeline graph input), the
    exact shape this file pins. The fix was ported the same day; this guard
    now watches the server flow, the single TUI conversation entry point
    that remains.
    """

    @staticmethod
    def _turn_stream_source() -> str:
        import inspect

        from chaos_agent.server.routes import turn_event_stream

        return inspect.getsource(turn_event_stream)

    def test_the_handoff_is_constructed_with_an_id(self):
        src = self._turn_stream_source()
        assert "_SM(content=_handoff, id=str(uuid.uuid4()))" in src

    def test_no_second_bare_construction(self):
        """Each write path must reuse the object, not build its own copy.

        Two bare ``_SM(content=_handoff)`` calls produce two distinct ids,
        which defeats the fix just as thoroughly as having none. The exact
        substring (closing paren right after the content arg) matches only
        the bare form, never the id-carrying one.
        """
        src = self._turn_stream_source()
        assert "_SM(content=_handoff)" not in src

    def test_the_graph_input_reuses_the_same_object(self):
        src = self._turn_stream_source()
        assert "messages=[_handoff_msg] if _handoff_msg else []" in src

    def test_it_is_built_before_the_task_id_branch(self):
        """The session bootstrap needs it whether or not a task id exists.

        Building it inside ``if _p_task_id:`` would leave the graph input
        with nothing on the other path; this ordering is load-bearing, not
        cosmetic.
        """
        src = self._turn_stream_source()
        build_at = src.index("_SM(content=_handoff, id=str(uuid.uuid4()))")
        branch_at = src.index("bootstrap_task_session(\n", build_at - 2000)
        assert build_at < branch_at
