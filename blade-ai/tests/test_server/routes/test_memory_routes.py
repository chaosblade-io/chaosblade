"""Contract tests for the /resume HTTP surface on the memory router.

``GET /api/v1/memory/resumable`` — sessions that carry an events jsonl
on disk (the picker's data source) and
``GET /api/v1/memory/{sid}/events`` — the full StreamEvent audit trail
(the visual-rebuild source). Locks down:

  - row shape and ordering (mtime desc) of the resumable list
  - ``started_at`` sourced from the session JSON (empty when missing)
  - ``first_input`` head-extraction with the 60-char truncation
  - events read: happy path, corrupt-line skip, missing-file fail
    envelope (TASK_NOT_FOUND — the TUI's hard-error no-fallback hook)
"""

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chaos_agent.config.settings import settings
from chaos_agent.memory.tui_session_store import (
    TuiSessionStore,
    get_global_tui_session_store,
    set_global_tui_session_store,
)


@pytest.fixture
def test_client(tmp_path, monkeypatch):
    """Bare app + memory router; memory_dir redirected to tmp_path.

    The store's events dir is ``session_dir.parent / "tui"`` which
    resolves to the same ``<memory>/tui`` the resumable endpoint
    scans, so hand-written jsonl files are visible through both.
    """
    monkeypatch.setattr(settings, "memory_dir", tmp_path / "memory")
    memory = tmp_path / "memory"
    previous = get_global_tui_session_store()
    store = TuiSessionStore(memory / "sessions")
    set_global_tui_session_store(store)
    # Import order matters: the route decorators live in the ``memory``
    # submodule (the router object itself is defined in the package
    # __init__), so importing it first registers the paths on the
    # shared router BEFORE we hand it to the bare app.
    import chaos_agent.server.routes.memory  # noqa: F401 — side effect
    from chaos_agent.server.routes import memory_router

    app = FastAPI()
    app.include_router(memory_router)
    try:
        yield TestClient(app)
    finally:
        set_global_tui_session_store(previous)


def _events_dir() -> Path:
    return settings.resolved_memory_dir / "tui"


def _write_events(sid: str, records: list[dict]) -> None:
    _events_dir().mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r) for r in records) + "\n"
    (_events_dir() / f"{sid}.events.jsonl").write_text(body, encoding="utf-8")


def _user_input(content: str) -> dict:
    return {
        "ts": "2025-01-01T00:00:00Z",
        "source": "user",
        "task_id": "",
        "event_type": "user_input",
        "data": {"type": "user_input", "content": content},
    }


class TestResumableList:
    def test_empty_when_no_events_dir(self, test_client):
        resp = test_client.get("/api/v1/memory/resumable")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["sessions"] == []
        assert data["total"] == 0

    def test_row_shape_and_totals(self, test_client):
        _write_events("sess-a", [_user_input("hello")])
        resp = test_client.get("/api/v1/memory/resumable")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        row = data["sessions"][0]
        assert row["tui_session_id"] == "sess-a"
        assert row["event_count"] == 1
        assert row["size_bytes"] > 0
        assert row["first_input"] == "hello"
        assert isinstance(row["modified_at"], float)

    def test_sorted_by_mtime_desc(self, test_client):
        _write_events("sess-old", [_user_input("first")])
        # Different mtime: force a later stamp on the second file.
        _write_events("sess-new", [_user_input("second")])
        p_new = _events_dir() / "sess-new.events.jsonl"
        past = 946684800.0  # 2000-01-01
        os.utime(p_new, (past, past))
        resp = test_client.get("/api/v1/memory/resumable")
        ids = [r["tui_session_id"] for r in resp.json()["data"]["sessions"]]
        assert ids == ["sess-old", "sess-new"]

    def test_started_at_from_session_json_and_empty_when_missing(
        self, test_client
    ):
        # One session WITH a session JSON on disk, one without.
        store = get_global_tui_session_store()
        store.create("sess-with-json", conversation_thread_id="conv-1")
        _write_events("sess-with-json", [_user_input("a")])
        _write_events("sess-orphan", [_user_input("b")])
        resp = test_client.get("/api/v1/memory/resumable")
        rows = {
            r["tui_session_id"]: r for r in resp.json()["data"]["sessions"]
        }
        # ``create`` stamps started_at; the orphan falls back to "".
        assert rows["sess-with-json"]["started_at"] != ""
        assert rows["sess-orphan"]["started_at"] == ""

    def test_first_input_reads_first_user_input_only(self, test_client):
        _write_events(
            "sess-multi",
            [
                {
                    "ts": "1",
                    "source": "server",
                    "task_id": "t1",
                    "event_type": "node_message",
                    "data": {"type": "node_message", "content": "noise"},
                },
                _user_input("the real first input"),
                _user_input("later input"),
            ],
        )
        resp = test_client.get("/api/v1/memory/resumable")
        row = resp.json()["data"]["sessions"][0]
        assert row["first_input"] == "the real first input"
        assert row["event_count"] == 3

    def test_first_input_truncated_at_60_chars(self, test_client):
        long_text = "x" * 200
        _write_events("sess-long", [_user_input(long_text)])
        resp = test_client.get("/api/v1/memory/resumable")
        row = resp.json()["data"]["sessions"][0]
        # 60 chars + ellipsis marker (see memory.py _FIRST_INPUT_MAX_CHARS).
        assert len(row["first_input"]) <= 61
        assert row["first_input"].startswith("x")
        assert row["first_input"].endswith("…")

    def test_route_order_literal_resumable_wins(self, test_client):
        # The literal path must not be captured by /{tui_session_id}.
        _write_events("whatever", [_user_input("w")])
        resp = test_client.get("/api/v1/memory/resumable")
        assert resp.status_code == 200
        assert "sessions" in resp.json()["data"]


class TestEventsRead:
    def test_returns_events_and_total(self, test_client):
        records = [
            _user_input("inject cpu"),
            {
                "ts": "2",
                "source": "server",
                "task_id": "t1",
                "event_type": "result",
                "data": {"type": "result", "content": "ok", "task_id": "t1"},
            },
        ]
        _write_events("sess-ok", records)
        resp = test_client.get("/api/v1/memory/sess-ok/events")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 2
        assert data["events"] == records

    def test_corrupt_lines_skipped_not_fatal(self, test_client):
        _events_dir().mkdir(parents=True, exist_ok=True)
        (_events_dir() / "sess-mixed.events.jsonl").write_text(
            json.dumps(_user_input("ok")) + "\n"
            "this is not json\n"
            + json.dumps(_user_input("ok2")) + "\n",
            encoding="utf-8",
        )
        resp = test_client.get("/api/v1/memory/sess-mixed/events")
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 2

    def test_missing_file_is_task_not_found(self, test_client):
        resp = test_client.get("/api/v1/memory/sess-nope/events")
        assert resp.status_code == 200
        env = resp.json()
        assert env["code"] == 2001  # TASK_NOT_FOUND
        assert "sess-nope" in env["message"]

    def test_empty_file_is_task_not_found_too(self, test_client):
        # Missing and empty are the same client behaviour (hard error,
        # no fallback chain) — read_events returns [] for both.
        _events_dir().mkdir(parents=True, exist_ok=True)
        (_events_dir() / "sess-empty.events.jsonl").write_text(
            "", encoding="utf-8"
        )
        resp = test_client.get("/api/v1/memory/sess-empty/events")
        assert resp.json()["code"] == 2001
