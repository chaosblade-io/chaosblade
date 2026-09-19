"""Contract tests for POST /api/v1/sessions/{sid}/resume — the
server-side rehydrate of the /resume takeover.

Locks down:
  - in-memory ``SessionStore._items[sid]`` rebuilt from the disk JSON:
    conversation_thread_id restored (dialogue continuity), task_ids
    restored, ``first_turn_done=True`` (no first-turn placeholder)
  - legacy session files without a thread field resume with an EMPTY
    binding (turn.py mints + backfills a fresh thread on the next turn)
  - idempotency: a second call overwrites from disk, same result
  - a finalized (non-active) session file is flipped back to "active"
    on disk — WITHOUT finalize()'s side effects (no finished_at stamp,
    no events-jsonl deletion)
  - missing disk record → TASK_NOT_FOUND fail envelope

Plus POST /api/v1/sessions (create) at the HTTP route level: the
in-memory record's conversation_thread_id must land on disk at create
time — the restart-amnesia regression guard (see
TestCreateSessionPersistsThread).
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chaos_agent.config.settings import settings
from chaos_agent.memory.tui_session_store import (
    TuiSessionStore,
    get_global_tui_session_store,
    set_global_tui_session_store,
)
from chaos_agent.models.schemas import ResponseCode
from chaos_agent.server.routes.sessions import _GLOBAL_STORE, sessions_router


@pytest.fixture
def test_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "memory_dir", tmp_path / "memory")
    memory = tmp_path / "memory"
    previous = get_global_tui_session_store()
    store = TuiSessionStore(memory / "sessions")
    set_global_tui_session_store(store)
    app = FastAPI()
    app.include_router(sessions_router)
    try:
        yield TestClient(app)
    finally:
        _GLOBAL_STORE._items.clear()
        set_global_tui_session_store(previous)


def _session_json_path(sid: str):
    store = get_global_tui_session_store()
    return store.session_dir / f"{sid}.json"


class TestResumeRehydrate:
    def test_rebuilds_in_memory_entry_with_thread_and_tasks(
        self, test_client
    ):
        store = get_global_tui_session_store()
        store.create(
            "sess-r1",
            cluster_name="prod",
            namespace="ns-a",
            conversation_thread_id="conv-abc123",
        )
        store.add_task("sess-r1", "task-9")

        resp = test_client.post("/api/v1/sessions/sess-r1/resume")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["tui_session_id"] == "sess-r1"
        assert data["conversation_thread_id"] == "conv-abc123"
        assert data["resumed"] is True

        entry = _GLOBAL_STORE._items.get("sess-r1")
        assert entry is not None
        assert entry["conversation_thread_id"] == "conv-abc123"
        assert entry["task_ids"] == ["task-9"]
        assert entry["first_turn_done"] is True
        assert entry["cluster"] == "prod"
        assert entry["namespace"] == "ns-a"

    def test_legacy_session_without_thread_resumes_empty(
        self, test_client
    ):
        store = get_global_tui_session_store()
        store.create("sess-legacy")  # pre-thread schema equivalent
        # Strip the field to simulate a file from before the feature.
        p = _session_json_path("sess-legacy")
        disk = json.loads(p.read_text(encoding="utf-8"))
        disk.pop("conversation_thread_id", None)
        p.write_text(json.dumps(disk), encoding="utf-8")

        resp = test_client.post("/api/v1/sessions/sess-legacy/resume")
        data = resp.json()["data"]
        # Empty binding — turn.py's next-turn mint + backfill path.
        assert data["conversation_thread_id"] == ""
        assert _GLOBAL_STORE._items["sess-legacy"][
            "conversation_thread_id"
        ] == ""

    def test_idempotent_overwrites_from_disk(self, test_client):
        store = get_global_tui_session_store()
        store.create(
            "sess-again", conversation_thread_id="conv-first"
        )
        first = test_client.post("/api/v1/sessions/sess-again/resume")
        assert first.json()["data"]["conversation_thread_id"] == "conv-first"
        # Mutate the disk binding between calls.
        store.update_thread_id("sess-again", "conv-second")
        second = test_client.post("/api/v1/sessions/sess-again/resume")
        assert second.json()["data"]["conversation_thread_id"] == "conv-second"
        # Overwrite, not duplicate keys.
        assert _GLOBAL_STORE._items["sess-again"][
            "conversation_thread_id"
        ] == "conv-second"

    def test_missing_disk_record_is_task_not_found(self, test_client):
        resp = test_client.post("/api/v1/sessions/sess-nope/resume")
        assert resp.status_code == 200
        env = resp.json()
        assert env["code"] == 2001
        assert "sess-nope" in env["message"]
        assert _GLOBAL_STORE._items.get("sess-nope") is None

    def test_finalized_session_reactivated_without_finalize_side_effects(
        self, test_client
    ):
        store = get_global_tui_session_store()
        store.create("sess-done", conversation_thread_id="conv-x")
        # A terminal status on disk, as finalize() would leave it.
        store.update_status("sess-done", "completed")

        resp = test_client.post("/api/v1/sessions/sess-done/resume")
        assert resp.json()["code"] == 0

        disk = json.loads(
            _session_json_path("sess-done").read_text(encoding="utf-8")
        )
        assert disk["status"] == "active"
        # finalize() would have stamped finished_at AND dropped the
        # events jsonl — update_status must do neither.
        assert disk.get("finished_at") in (None, "", "")
        events = (
            store.session_dir.parent / "tui" / "sess-done.events.jsonl"
        )
        # No events file was ever created in this test; reactivation
        # must not attempt to delete one (and must not create one).
        assert not events.exists()

    def test_active_session_status_untouched(self, test_client):
        store = get_global_tui_session_store()
        store.create("sess-live", conversation_thread_id="conv-y")
        before = _session_json_path("sess-live").read_text(encoding="utf-8")
        test_client.post("/api/v1/sessions/sess-live/resume")
        after = _session_json_path("sess-live").read_text(encoding="utf-8")
        # No rewrite for an already-active record.
        assert before == after


class TestResumeSessionIdValidation:
    """Path-param whitelist parity with the memory routes.

    The resume route composes the sid into filesystem paths
    (``tui_store.read`` → ``_file_path``, ``update_status``) — a
    traversal payload like ``../../x`` must bounce BEFORE any read.
    The rule lives in one place (``tui_session_store.SESSION_ID_PATTERN``)
    and both route surfaces import it, so this also guards against the
    two surfaces drifting apart.
    """

    @pytest.mark.parametrize(
        "bad_sid",
        ["has.dot", "has%20space", "a" * 129],
    )
    def test_malformed_ids_rejected(self, test_client, bad_sid):
        # Payloads that DO reach the handler (no route-level path
        # splitting) must bounce at the whitelist with INVALID_PARAMS.
        resp = test_client.post(f"/api/v1/sessions/{bad_sid}/resume")
        assert resp.status_code == 200  # fail envelope, not HTTP error
        env = resp.json()
        assert env["status"] == "fail"
        assert env["code"] == ResponseCode.INVALID_PARAMS
        assert "invalid tui_session_id" in env["message"]
        # Bounced BEFORE any disk access — nothing entered the store.
        assert bad_sid not in _GLOBAL_STORE._items

    @pytest.mark.parametrize(
        "traversal_sid",
        ["..%2Fescape", "..%2F..%2Fetc", "../escape", "sub/dir"],
    )
    def test_traversal_ids_never_reach_disk(self, test_client, traversal_sid):
        """Traversal payloads are stopped by EITHER fence, and the test
        must not depend on which one fires — the URL layer's decoding
        timing differs between TestClient and a real uvicorn deploy
        (Starlette matches the decoded path here, so ``%2F`` splits
        the segment and 404s; a deploy that passes the raw path would
        deliver the decoded sid to the handler, where the whitelist
        bounces it). Either way: no session_dir file may appear."""
        store = get_global_tui_session_store()
        before = sorted(p.name for p in store.session_dir.iterdir())
        resp = test_client.post(f"/api/v1/sessions/{traversal_sid}/resume")
        after = sorted(p.name for p in store.session_dir.iterdir())
        assert before == after
        if resp.status_code == 200:
            # Reached the handler → the whitelist must have bounced it.
            env = resp.json()
            assert env["status"] == "fail"
            assert env["code"] == ResponseCode.INVALID_PARAMS
        else:
            # Stopped at the route layer (404) — equally protected.
            assert resp.status_code == 404

    def test_session_dir_untouched_by_rejected_id(self, test_client):
        """The rejection happens before ``read()`` composes a path:
        no session_dir file may be created by a rejected attempt."""
        store = get_global_tui_session_store()
        before = sorted(p.name for p in store.session_dir.iterdir())
        test_client.post("/api/v1/sessions/..%2F..%2Fetc%2Fpasswd/resume")
        after = sorted(p.name for p in store.session_dir.iterdir())
        assert before == after

    def test_valid_shape_accepted(self, test_client):
        # The whitelist's generous superset: alnum, dash, underscore,
        # dot-free, ≤128 chars — same ids the memory routes accept.
        store = get_global_tui_session_store()
        store.create("sess_ok-123", conversation_thread_id="conv-ok")
        resp = test_client.post("/api/v1/sessions/sess_ok-123/resume")
        assert resp.json()["code"] == 0
        assert _GLOBAL_STORE._items["sess_ok-123"][
            "conversation_thread_id"
        ] == "conv-ok"


class TestCreateSessionPersistsThread:
    """POST /api/v1/sessions at the HTTP ROUTE level — the create path
    must land the in-memory conversation_thread_id on disk AT CREATE
    TIME.

    Regression guard for the restart-amnesia bug: the route used to
    call ``TuiSessionStore.create()`` without the thread binding, so
    the disk JSON kept the empty-string default. A server restart +
    ``/resume`` then minted a FRESH thread and the agent forgot the
    whole conversation even though its checkpoints were still alive in
    checkpoints.db. The store-level tests in test_tui_session_store.py
    only prove ``create()`` CAN persist the field — they cannot catch
    the route forgetting to pass it, which is exactly what happened.
    """

    def test_create_route_persists_thread_binding(
        self, test_client, monkeypatch
    ):
        # Stub the DB mirror: the route treats task-store failure as
        # non-fatal, and a real store would touch a live sqlite path
        # outside the tmp_path sandbox.
        async def _no_task_store():
            raise RuntimeError("stubbed out for test")

        monkeypatch.setattr(
            "chaos_agent.persistence.task_store.get_task_store",
            _no_task_store,
        )

        resp = test_client.post(
            "/api/v1/sessions",
            json={"cluster": "prod", "namespace": "ns-a"},
        )
        assert resp.status_code == 200
        sid = resp.json()["session_id"]

        in_mem = _GLOBAL_STORE._items[sid]
        assert in_mem["conversation_thread_id"].startswith("conv-")

        disk = json.loads(
            _session_json_path(sid).read_text(encoding="utf-8")
        )
        # Non-empty AND matching the in-memory binding — the whole
        # point of the fix.
        assert disk["conversation_thread_id"] == (
            in_mem["conversation_thread_id"]
        )
