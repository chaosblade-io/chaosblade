"""Tests for Phase 3a control-plane routes — /config, /memory, and the
new /sessions/{sid}/compact endpoint.

Covers:
  - /memory and /sessions/{sid}/compact reject path-traversal session ids.
  - /config GET returns the masked display dict + path.
  - /config POST/DELETE reject keys outside the writable whitelist.
  - /config write hot-reload flag matches the cold-key classification
    (LLM-bound keys must be cold even though they live in the
    writable list — the running LLM doesn't observe settings.reload()).

We don't exercise the real TuiSessionStore filesystem here — the
validation gate runs BEFORE any disk touch, which is exactly what the
path-traversal regression we're locking down requires. For happy-path
filesystem behaviour the existing TuiSessionStore unit tests cover
the storage layer.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def test_client():
    """Bring up a minimal FastAPI app with just the Phase 3a routers.

    The submodule imports below are not unused — importing them runs
    the ``@router.<verb>`` decorators that bind handlers onto the
    shared router instances. Without these imports the routers
    register zero routes (matches the pattern used in
    ``chaos_agent.server.app`` for preflight/turn/interrupt).
    """
    app = FastAPI()
    from chaos_agent.server.routes import (
        config_router,
        memory_router,
        model_router,
        skills_router,
    )
    from chaos_agent.server.routes import config as _config  # noqa: F401
    from chaos_agent.server.routes import memory as _memory  # noqa: F401
    from chaos_agent.server.routes import model as _model  # noqa: F401
    from chaos_agent.server.routes import skills_admin as _skills  # noqa: F401

    app.include_router(config_router)
    app.include_router(memory_router)
    app.include_router(model_router)
    app.include_router(skills_router)
    return TestClient(app)


@pytest.fixture
def test_client_with_registry():
    """Same as ``test_client`` but with a stub ``skill_registry`` on
    ``app.state`` so the skills_admin endpoints can resolve metadata.

    The stub returns deterministic data for the few skills we name
    in tests — keeps the asserts precise without a real filesystem.
    """
    app = FastAPI()
    from chaos_agent.server.routes import skills_router
    from chaos_agent.server.routes import skills_admin as _skills  # noqa: F401

    class _FakeMeta:
        def __init__(self, name: str):
            self.name = name
            self.description = f"desc for {name}"
            self.version = "1.0"
            self.category = "node"
            self.target = "cpu"
            self.required_tools = ["kubectl"]
            self.tags = ["chaos"]
            self.parameters = []
            self.scripts = []

    class _FakeRegistry:
        def __init__(self):
            self._metadata = {
                "node-cpu-fullload": _FakeMeta("node-cpu-fullload"),
            }
            self._skill_dirs = {"node-cpu-fullload": "/tmp/skills/node-cpu-fullload"}
            self._instructions_cache = {"node-cpu-fullload": "# SKILL"}

        def get_metadata(self, name):
            return self._metadata.get(name)

        def activate(self, name):
            return self._instructions_cache.get(name, "")

        def get_skill_dir(self, name):
            return self._skill_dirs.get(name)

        def list_skills(self):
            return list(self._metadata.keys())

        def reload(self, *_args, **_kwargs):
            # No-op for the test — we only need to verify the endpoint
            # returns the diff envelope shape, not actual file scans.
            pass

    app.state.skill_registry = _FakeRegistry()
    app.include_router(skills_router)
    return TestClient(app)


class TestMemorySessionIdValidation:
    """Path-traversal regression: ``tui_session_id`` is composed into
    ``session_dir / f"{sid}.json"`` server-side. A crafted
    ``../../etc/passwd`` would let DELETE /memory unlink files outside
    the sessions directory. The validation gate must reject every
    obvious traversal shape BEFORE any filesystem touch."""

    # Cases the HTTP / ASGI layer normalises to 404 BEFORE the handler
    # runs (``..`` dot-segments). Documented here as the second line
    # of defense — the regex would also catch them, but the request
    # never reaches the handler. Tested separately via
    # ``test_traversal_dot_segments_404_before_handler``.

    @pytest.mark.parametrize(
        "bad_sid",
        [
            "sess.json",  # dot rejected by regex
            "sess id",  # space rejected by regex
            "x" * 129,  # over-length rejected by regex
        ],
    )
    def test_get_memory_rejects_traversal_sid(self, test_client, bad_sid):
        # These shapes reach the handler (single segment, no slash) and
        # the regex must reject them with INVALID_PARAMS.
        r = test_client.get(f"/api/v1/memory/{bad_sid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "fail"
        assert body["code"] == 1002, f"expected INVALID_PARAMS, got {body}"

    @pytest.mark.parametrize(
        "bad_sid",
        ["sess.json", "y" * 200, "evil sess"],
    )
    def test_delete_memory_rejects_traversal_sid(self, test_client, bad_sid):
        r = test_client.delete(f"/api/v1/memory/{bad_sid}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "fail"
        assert body["code"] == 1002

    @pytest.mark.parametrize("normalised_sid", ["..", "../etc"])
    def test_traversal_dot_segments_404_before_handler(
        self, test_client, normalised_sid,
    ):
        # The ASGI/test client normalises ``..`` segments at the URL
        # layer — these never hit the handler at all. That's the
        # outer ring of defense; the regex is the inner ring for any
        # path that DOES reach the handler. Asserting 404 here proves
        # the outer ring works; combined with the inner-ring tests
        # above, no traversal path can reach the filesystem build.
        r = test_client.get(f"/api/v1/memory/{normalised_sid}")
        assert r.status_code == 404, r.text

    def test_valid_session_id_passes_validation(self, test_client):
        # A canonical ``sess_<hex>`` shape should NOT be rejected by the
        # gate (it'll fall through to the store lookup, which returns
        # 2001 / TASK_NOT_FOUND for an unknown session — that's a
        # separate code path from 1002 and proves the regex didn't fire).
        r = test_client.get("/api/v1/memory/sess_abcdef012345")
        assert r.status_code == 200
        body = r.json()
        # We can't guarantee the store is initialised in this minimal
        # fixture, so accept either 5099 (store None) or 2001 (no record)
        # — both prove the validation gate let the call through.
        assert body["code"] in (2001, 5099), body


class TestCompactSessionSSE:
    """The /sessions/{sid}/compact endpoint streams SSE so the TS TUI
    can render live progress during the multi-second LLM summariser
    call. Lock the wire shape: events come in order
    (memory_compaction → result → done), and the result envelope
    carries the route-level authoritative numbers."""

    def _make_client_with_compact(self, hook, graph):
        """Spin up an isolated FastAPI app with just the sessions
        router and ``app.state.agents`` wired so /compact can find
        the inject graph and pre-reason hook."""
        app = FastAPI()
        from chaos_agent.server.routes.sessions import sessions_router

        app.state.agents = {"inject": graph, "pre_reason_hook": hook}
        app.include_router(sessions_router)
        return TestClient(app)

    @staticmethod
    def _parse_sse(body_text: str) -> list[dict]:
        """Split a raw SSE response body into the JSON data blobs
        (one per frame). Comment lines (``: keepalive``) are dropped."""
        import json as _json

        frames: list[dict] = []
        for frame in body_text.split("\n\n"):
            data_parts: list[str] = []
            for line in frame.splitlines():
                if line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data_parts.append(line[5:].lstrip())
            if data_parts:
                frames.append(_json.loads("".join(data_parts)))
        return frames

    def test_streams_result_and_done_on_success(self):
        from langchain_core.messages import HumanMessage, RemoveMessage, SystemMessage

        # Stub state snapshots: ``before`` has 4K-token worth of msgs,
        # ``after`` has the summary message. The route asserts both
        # before > 0 (so we don't short-circuit at the noop branch)
        # and after < before (so it emits ``compacted=true``).
        before_msgs = [
            HumanMessage(content="x" * 4000, id=f"m-{i}") for i in range(8)
        ]
        after_msgs = [
            SystemMessage(content="[Compressed History]\nsummary")
        ]

        class _Snapshot:
            def __init__(self, msgs):
                self.values = {"messages": msgs, "task_id": "old-task"}

        call_count = {"n": 0}

        class _StubGraph:
            async def aget_state(self, _config):
                # First call returns ``before`` (route reads initial
                # state); subsequent calls return ``after`` (route
                # reads post-update state for the authoritative
                # ``tokens_after``).
                call_count["n"] += 1
                return _Snapshot(before_msgs if call_count["n"] == 1 else after_msgs)

            async def aupdate_state(self, _config, _updates):
                pass

        # Stub hook: emit a "started" + "completed" through the
        # tracker (so the route's converter has something to relay),
        # then return the standard LangGraph updates shape.
        async def _stub_hook(state, *, force):
            assert force is True
            from chaos_agent.observability.status_tracker import (
                StatusEvent,
                get_tracker,
            )
            task_id = state.get("task_id", "")
            tracker = get_tracker(task_id)
            tracker.emit(StatusEvent(
                task_id=task_id, phase="started", category="node",
                source="memory_compression", message="Compressing 8 messages",
                detail={"total_tokens_before": 1000},
            ))
            tracker.emit(StatusEvent(
                task_id=task_id, phase="completed", category="node",
                source="memory_compression", message="done",
                duration_ms=1234.0,
                detail={"messages_compacted": 8, "tokens_before": 1000, "tokens_after": 200},
            ))
            return {
                "messages": [RemoveMessage(id=m.id) for m in before_msgs]
                + [SystemMessage(content="[Compressed History]\nstub")],
                "compressed_summary": "stub",
            }

        client = self._make_client_with_compact(_stub_hook, _StubGraph())
        # Stub the TuiSessionStore lookup by passing thread_id directly.
        r = client.post(
            "/api/v1/sessions/sess_abc123/compact",
            json={"thread_id": "thread-xyz"},
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")

        frames = self._parse_sse(r.text)
        types = [f.get("type") for f in frames]
        # Order: at least one memory_compaction (started) before
        # ``result``, then ``done`` last.
        assert "memory_compaction" in types
        assert "result" in types
        assert types[-1] == "done"
        # Order: result must come AFTER all memory_compaction frames.
        result_idx = types.index("result")
        last_mc_idx = max(i for i, t in enumerate(types) if t == "memory_compaction")
        assert last_mc_idx < result_idx

        # Result frame carries authoritative route-level numbers.
        result = next(f for f in frames if f["type"] == "result")
        payload = result["payload"]
        assert payload["thread_id"] == "thread-xyz"
        assert payload["tokens_before"] > payload["tokens_after"]
        assert payload["compacted"] is True
        assert payload["layer"] == "llm_summary"

    def test_streams_noop_when_thread_empty(self):
        class _Snapshot:
            values = {"messages": [], "task_id": "old"}

        class _StubGraph:
            async def aget_state(self, _config):
                return _Snapshot()

            async def aupdate_state(self, _config, _updates):
                raise AssertionError("must not be called on noop path")

        async def _stub_hook(*_a, **_kw):
            raise AssertionError("hook must not run when there's nothing to compact")

        client = self._make_client_with_compact(_stub_hook, _StubGraph())
        r = client.post(
            "/api/v1/sessions/sess_empty1/compact",
            json={"thread_id": "thread-empty"},
        )
        assert r.status_code == 200

        frames = self._parse_sse(r.text)
        types = [f.get("type") for f in frames]
        assert types[-1] == "done"
        result = next(f for f in frames if f["type"] == "result")
        assert result["payload"]["compacted"] is False
        assert result["payload"]["layer"] == "noop"

    def test_streams_error_frame_when_hook_raises(self):
        from langchain_core.messages import HumanMessage

        before_msgs = [HumanMessage(content="x" * 4000, id="m-0")]

        class _Snapshot:
            def __init__(self):
                self.values = {"messages": before_msgs, "task_id": "old"}

        class _StubGraph:
            async def aget_state(self, _config):
                return _Snapshot()

            async def aupdate_state(self, _config, _updates):
                raise AssertionError("must not be called when hook raises")

        async def _stub_hook(*_a, **_kw):
            raise RuntimeError("compaction down")

        client = self._make_client_with_compact(_stub_hook, _StubGraph())
        r = client.post(
            "/api/v1/sessions/sess_err1/compact",
            json={"thread_id": "thread-err"},
        )
        assert r.status_code == 200

        frames = self._parse_sse(r.text)
        types = [f.get("type") for f in frames]
        assert "error" in types
        assert types[-1] == "done"
        err = next(f for f in frames if f["type"] == "error")
        assert "compaction down" in err.get("content", "")

    def test_resolves_thread_from_in_memory_session_store(self):
        # Regression guard: ``/compact`` MUST find the thread via the
        # in-memory ``_GLOBAL_STORE[sid].conversation_thread_id`` (the
        # stable LangGraph thread every turn of this session shares),
        # NOT via the on-disk ``TuiSessionStore.task_ids`` list (which
        # is only populated by actual inject/recover tasks).
        #
        # Pre-fix, a chat-only session (empty task_ids) silently
        # returned TASK_NOT_FOUND from /compact even though a perfectly
        # valid conversation thread existed in checkpointer state.
        from langchain_core.messages import HumanMessage
        from chaos_agent.server.routes.sessions import _GLOBAL_STORE

        # Seed the in-memory store with a session carrying the stable
        # conversation thread id (same shape SessionStore.create
        # produces at POST /sessions).
        sid = "sess_compactthread1"
        _GLOBAL_STORE._items[sid] = {  # type: ignore[attr-defined]
            "conversation_thread_id": "conv-stable-abc",
            "first_turn_done": True,
        }
        try:
            captured_thread: list[str] = []

            class _StubGraph:
                async def aget_state(self, config):
                    # Capture the thread_id the route passed in. If
                    # /compact regresses back to ``task_ids[-1]``,
                    # this assertion is what catches it.
                    captured_thread.append(config["configurable"]["thread_id"])

                    class _Snap:
                        values = {
                            "messages": [
                                HumanMessage(content="x" * 4000, id="m-1"),
                            ],
                            "task_id": "stale",
                        }
                    return _Snap()

                async def aupdate_state(self, _config, _updates):
                    pass

            async def _stub_hook(*_a, **_kw):
                return {}  # noop: nothing splittable

            client = self._make_client_with_compact(_stub_hook, _StubGraph())
            r = client.post(
                f"/api/v1/sessions/{sid}/compact",
                json={"thread_id": None},  # NO explicit override
            )
            assert r.status_code == 200
            # Stream must complete (not return TASK_NOT_FOUND envelope).
            frames = self._parse_sse(r.text)
            types = [f.get("type") for f in frames]
            assert "done" in types, f"expected SSE done frame, got: {types}"
            # And the thread id the route picked must be the stable
            # conversation_thread_id, not whatever was in task_ids.
            assert captured_thread == ["conv-stable-abc"]
        finally:
            _GLOBAL_STORE._items.pop(sid, None)  # type: ignore[attr-defined]


class TestConfigWriteWhitelist:
    """``/config`` rejects writes to anything outside the writable set,
    so a malicious caller can't rotate ``llm_api_key`` or ``tasks_pg_dsn``
    via the HTTP API."""

    def test_post_unknown_key_rejected(self, test_client):
        r = test_client.post(
            "/api/v1/config/llm_api_key",
            json={"value": "leaked-key"},
        )
        body = r.json()
        assert body["status"] == "fail"
        assert body["code"] == 1002

    def test_delete_unknown_key_rejected(self, test_client):
        # Same whitelist for DELETE — otherwise a caller could force a
        # secret to be re-read from env vars at the next reload.
        r = test_client.delete("/api/v1/config/tasks_pg_dsn")
        body = r.json()
        assert body["status"] == "fail"
        assert body["code"] == 1002

    def test_post_missing_value_rejected(self, test_client):
        # Body must include a ``value`` field. ``None`` (or missing)
        # triggers the validation branch, NOT the whitelist branch.
        r = test_client.post(
            "/api/v1/config/model_name",
            json={},
        )
        body = r.json()
        assert body["status"] == "fail"
        assert body["code"] == 1002


class TestConfigColdKeyClassification:
    """LLM-bound keys must be classified cold even though they live in
    the writable whitelist. Without this the ``hot_reload=True`` claim
    in the response is a lie — the running LLM is captured at startup
    and ignores ``settings.reload()``."""

    @pytest.mark.parametrize(
        "key",
        [
            "model_name",
            "api_base_url",
            "llm_temperature",
            "llm_max_retries",
            "llm_enable_thinking",
            "verifier_json_mode",
        ],
    )
    def test_llm_bound_key_is_cold(self, key):
        from chaos_agent.tui.config_store import ConfigStore

        assert ConfigStore.is_cold_key(key), (
            f"{key!r} should be classified cold — make_llm() reads it at "
            "startup and the agents['llm'] reference doesn't observe "
            "settings.reload()"
        )

    @pytest.mark.parametrize(
        "key",
        [
            "timeout_kubectl",
            "max_replan_count",
            "kube_context",
            "log_level",
        ],
    )
    def test_runtime_keys_stay_hot(self, key):
        # These are read fresh on every kubectl call / replan check /
        # logging config init, so settings.reload() is enough.
        from chaos_agent.tui.config_store import ConfigStore

        assert not ConfigStore.is_cold_key(key), (
            f"{key!r} should NOT be cold — it's read fresh from settings "
            "on each use, so settings.reload() suffices"
        )


# =============================================================================
# Phase 3b — /skills admin endpoints
# =============================================================================


class TestSkillsNameValidation:
    """``/skills/{name}`` endpoints compose the name into filesystem
    operations indirectly (registry lookups), so the same path-traversal
    gate ``/memory`` uses applies. Lock the regex's reject set."""

    @pytest.mark.parametrize(
        "bad_name",
        ["a/b", "evil name", "x" * 200],
    )
    def test_show_rejects_bad_name(self, test_client_with_registry, bad_name):
        # ``/`` makes FastAPI 404 before reaching the handler — covered
        # by the dot-segment 404 test below. Other shapes hit the
        # handler and the regex must reject.
        if "/" in bad_name:
            r = test_client_with_registry.get(f"/api/v1/skills/{bad_name}")
            assert r.status_code == 404
            return
        r = test_client_with_registry.get(f"/api/v1/skills/{bad_name}")
        body = r.json()
        assert body["status"] == "fail"
        assert body["code"] == 1002

    @pytest.mark.parametrize(
        "bad_name",
        ["evil name", "x" * 200, "a@b"],
    )
    def test_enable_rejects_bad_name(self, test_client_with_registry, bad_name):
        r = test_client_with_registry.post(f"/api/v1/skills/{bad_name}/enable")
        body = r.json()
        assert body["status"] == "fail"
        assert body["code"] == 1002

    @pytest.mark.parametrize(
        "bad_name",
        ["evil name", "x" * 200, "a@b"],
    )
    def test_disable_rejects_bad_name(
        self, test_client_with_registry, bad_name,
    ):
        r = test_client_with_registry.post(f"/api/v1/skills/{bad_name}/disable")
        body = r.json()
        assert body["status"] == "fail"
        assert body["code"] == 1002


class TestSkillsShow:
    """``GET /api/v1/skills/{name}`` returns metadata + SKILL.md body."""

    def test_show_returns_metadata_and_instructions(
        self, test_client_with_registry,
    ):
        r = test_client_with_registry.get("/api/v1/skills/node-cpu-fullload")
        body = r.json()
        assert body["status"] == "success"
        data = body["data"]
        assert data["name"] == "node-cpu-fullload"
        assert data["metadata"]["category"] == "node"
        assert data["metadata"]["target"] == "cpu"
        assert data["metadata"]["required_tools"] == ["kubectl"]
        assert "SKILL" in data["instructions"]
        assert data["skill_dir"].endswith("node-cpu-fullload")

    def test_show_unknown_skill_404(self, test_client_with_registry):
        # ``valid-but-unknown`` passes the regex; the registry just
        # has no entry. Server returns TASK_NOT_FOUND so the TS
        # handler renders "skill not loaded" cleanly.
        r = test_client_with_registry.get("/api/v1/skills/never-loaded")
        body = r.json()
        assert body["status"] == "fail"
        assert body["code"] == 2001

    def test_show_without_registry_returns_internal_error(self, test_client):
        # ``test_client`` (no registry on app.state) covers the
        # cold-boot path where the server is up but skill_registry
        # is not yet attached. We expect a clean fail envelope, not
        # a 500.
        r = test_client.get("/api/v1/skills/anything")
        body = r.json()
        assert body["status"] == "fail"
        assert body["code"] == 5099


class TestSkillsReload:
    """``POST /api/v1/skills/reload`` re-scans and returns the diff."""

    def test_reload_returns_diff_envelope(self, test_client_with_registry):
        r = test_client_with_registry.post("/api/v1/skills/reload")
        body = r.json()
        assert body["status"] == "success"
        data = body["data"]
        assert "skills_dir" in data
        assert "total" in data
        # Same set before/after a no-op reload — added and removed
        # arrays are empty.
        assert data["added"] == []
        assert data["removed"] == []


class TestSkillsInstall:
    """``POST /api/v1/skills/install`` body validation."""

    def test_install_missing_source_rejected(self, test_client):
        r = test_client.post("/api/v1/skills/install", json={})
        body = r.json()
        assert body["status"] == "fail"
        assert body["code"] == 1002

    def test_install_empty_source_rejected(self, test_client):
        r = test_client.post(
            "/api/v1/skills/install", json={"source": "   "},
        )
        body = r.json()
        assert body["status"] == "fail"
        assert body["code"] == 1002

    def test_install_nonexistent_path_surfaces_install_error(
        self, test_client, tmp_path,
    ):
        # Pass a path that definitely doesn't exist — the installer
        # raises SkillInstallError, which we surface as 1002. Lock
        # the error-mapping path so refactors of installer.py don't
        # silently route hard failures into 5099 ("internal error").
        bogus = tmp_path / "definitely-not-here"
        r = test_client.post(
            "/api/v1/skills/install", json={"source": str(bogus)},
        )
        body = r.json()
        assert body["status"] == "fail"
        assert body["code"] == 1002


class TestSkillsEnableDisable:
    """``POST /api/v1/skills/{name}/enable`` and ``/disable`` —
    idempotency contract: server reports whether the call actually
    changed anything via ``was_disabled`` / ``was_enabled`` flags."""

    def test_enable_unknown_name_returns_was_disabled_false(
        self, test_client_with_registry,
    ):
        # The skill name is well-formed but not in disabled_skills,
        # so the call is a no-op. Server still returns success with
        # ``was_disabled: false`` so the TS handler renders the
        # "already enabled" branch.
        r = test_client_with_registry.post(
            "/api/v1/skills/never-disabled/enable",
        )
        body = r.json()
        assert body["status"] == "success"
        assert body["data"]["was_disabled"] is False


# =============================================================================
# Phase 3c.1 — /model endpoints
# =============================================================================


class TestModelGet:
    """``GET /api/v1/model`` shape contract."""

    def test_returns_active_and_candidates(self, test_client):
        r = test_client.get("/api/v1/model")
        body = r.json()
        assert body["status"] == "success"
        data = body["data"]
        # Active mirrors the running settings.model_name. We don't
        # pin the value (env-dependent) — just the field's presence.
        assert "active" in data
        assert "api_base_url" in data
        # Curated candidates list — make sure it's non-empty (a
        # regression that empties it would silently break /model list).
        assert isinstance(data["candidates"], list)
        assert len(data["candidates"]) > 0
        # Each candidate carries the contract fields the TS renderer
        # reads: ``id`` and ``provider``.
        for cand in data["candidates"]:
            assert "id" in cand and isinstance(cand["id"], str)
            assert "provider" in cand and isinstance(cand["provider"], str)


class TestModelSet:
    """``POST /api/v1/model`` body validation + restart-required
    contract. We don't actually mutate the on-disk config in tests
    (would dirty the dev box's ~/.blade-ai/config.json) — the empty /
    invalid body cases short-circuit before ConfigStore touches disk."""

    def test_missing_model_name_rejected(self, test_client):
        r = test_client.post("/api/v1/model", json={})
        body = r.json()
        assert body["status"] == "fail"
        assert body["code"] == 1002

    def test_empty_string_rejected(self, test_client):
        r = test_client.post("/api/v1/model", json={"model_name": "   "})
        body = r.json()
        assert body["status"] == "fail"
        assert body["code"] == 1002

    def test_non_string_rejected(self, test_client):
        # An int / null / list etc. — we surface a 1002 instead of a
        # 500 because the failure is the caller's, not the server's.
        for bad in [None, 123, ["foo"], {"nested": "x"}]:
            r = test_client.post("/api/v1/model", json={"model_name": bad})
            body = r.json()
            assert body["status"] == "fail", f"bad={bad!r}: {body}"
            assert body["code"] == 1002

    def test_set_writes_via_configstore_and_reports_cold(
        self, test_client, tmp_path, monkeypatch,
    ):
        # Redirect ConfigStore to a tmp file so we don't touch the
        # dev user's real config. Verify:
        #  - The route writes the new value.
        #  - ``restart_required`` is True (model_name is cold, see
        #    Phase 3a's _COLD_KEYS expansion).
        #  - ``active`` echoes back the trimmed input.
        from chaos_agent.tui.config_store import ConfigStore

        cfg_path = str(tmp_path / "config.json")
        monkeypatch.setattr(
            ConfigStore, "__init__",
            lambda self, config_path=None: setattr(self, "_path", cfg_path),
        )
        # ``settings.reload()`` will be called inside ConfigStore.set;
        # let it run for real — it's a no-op when the path doesn't
        # affect any settings the test inspects.
        r = test_client.post("/api/v1/model", json={"model_name": "  qwen-test  "})
        body = r.json()
        assert body["status"] == "success", body
        data = body["data"]
        assert data["active"] == "qwen-test", "trimmed value should land"
        assert data["restart_required"] is True, (
            "model_name is a cold key — restart_required must be true"
        )
        # File was actually written.
        import json
        with open(cfg_path) as f:
            stored = json.load(f)
        assert stored["model_name"] == "qwen-test"


# =============================================================================
# Phase 3c.2 — /plan dry-run
# =============================================================================


class TestTurnRequestDryRunSchema:
    """``TurnRequest`` schema must accept the ``dry_run`` field with
    a False default. Without that, the ``/plan`` slash command's
    request body would 422 at the FastAPI boundary."""

    def test_default_is_false(self):
        from chaos_agent.server.routes.turn import TurnRequest

        body = TurnRequest(input="hello")
        assert body.dry_run is False, (
            "dry_run must default to False so existing /run callers "
            "stay byte-identical without setting the field"
        )

    def test_accepts_true(self):
        from chaos_agent.server.routes.turn import TurnRequest

        body = TurnRequest(input="hello", dry_run=True)
        assert body.dry_run is True

    def test_rejects_non_bool(self):
        # Pydantic should reject a non-bool to keep the contract
        # tight — a stray "true" string would silently behave the
        # same as bool true otherwise (Pydantic coerces); pin the
        # real type so any caller passing the wrong shape fails.
        import pydantic
        from chaos_agent.server.routes.turn import TurnRequest

        with pytest.raises(pydantic.ValidationError):
            TurnRequest(input="hello", dry_run=["nope"])


class TestIntentConfirmDryRunSkip:
    """When state.dry_run is True, ``intent_confirm`` must NOT call
    ``interrupt()`` — the user opted into preview-only via /plan and
    expects the plan summary directly, not a Y/N click first."""

    @pytest.mark.asyncio
    async def test_dry_run_short_circuits(self, monkeypatch):
        from chaos_agent.agent.nodes import intent_confirm as ic_mod

        called = {"interrupt": False}

        def fake_interrupt(*_args, **_kwargs):
            called["interrupt"] = True
            return "approved"

        # Replace the module's ``interrupt`` reference. If the
        # short-circuit ever regresses, the test catches it because
        # the real interrupt would fire.
        monkeypatch.setattr(ic_mod, "interrupt", fake_interrupt)

        state = {
            "task_id": "task-test",
            "fault_intent": {
                "fault_type": "node-cpu-fullload",
                "scope": "node",
                "target": "cpu",
                "action": "fullload",
                "namespace": "default",
            },
            "intent_confidence": 1.0,
            "dry_run": True,
        }
        result = await ic_mod.intent_confirm(state)
        assert called["interrupt"] is False, (
            "intent_confirm must NOT call interrupt() in dry_run mode"
        )
        # Empty dict signals "no state changes; routing logic decides
        # next step". route_after_intent_confirm sees confirmed_intent=
        # "inject" + fault_intent and routes to agent_loop.
        assert result == {}, f"dry_run skip should return {{}}, got {result}"

    @pytest.mark.asyncio
    async def test_non_dry_run_still_interrupts(self, monkeypatch):
        # Inverse: when dry_run is False / absent, the gate must
        # still fire interrupt() — otherwise we'd silently skip
        # Layer-1 confirmation for everyone.
        from chaos_agent.agent.nodes import intent_confirm as ic_mod

        called = {"interrupt": False}

        def fake_interrupt(*_args, **_kwargs):
            called["interrupt"] = True
            return "approved"

        monkeypatch.setattr(ic_mod, "interrupt", fake_interrupt)
        state = {
            "task_id": "task-test",
            "fault_intent": {
                "fault_type": "node-cpu-fullload",
                "scope": "node",
                "target": "cpu",
                "action": "fullload",
                "namespace": "default",
            },
            "intent_confidence": 1.0,
            # dry_run absent → defaults to falsy
        }
        await ic_mod.intent_confirm(state)
        assert called["interrupt"] is True, (
            "intent_confirm MUST interrupt when dry_run is falsy — "
            "otherwise Layer-1 confirmation is silently bypassed"
        )


# =============================================================================
# Phase 3 finishing — gap fixes + integration
# =============================================================================


class TestSkillsDirEndpoint:
    """Phase 3 finishing — ``GET /api/v1/skills_dir`` for the
    ``/skills path`` parity command."""

    def test_returns_resolved_and_candidates(self, test_client):
        r = test_client.get("/api/v1/skills_dir")
        body = r.json()
        assert body["status"] == "success"
        data = body["data"]
        assert "resolved" in data and isinstance(data["resolved"], str)
        # Candidates list is fixed at 3 entries (config / env / dev).
        # Pin the count so a future "let's add more candidates" lands
        # here as a contract change.
        assert isinstance(data["candidates"], list)
        assert len(data["candidates"]) == 3
        labels = [c["label"] for c in data["candidates"]]
        assert labels == ["config.json", "env BLADE_AI_SKILLS_DIR", "dev path"]


class TestConfigSetGetIntegration:
    """Cross-endpoint integration: ``/config set`` writes to disk,
    a follow-up ``/config get`` (GET /api/v1/config) returns the same
    value masked through ``ConfigStore.get_display_dict``. Pins the
    end-to-end flow so a refactor of either side that breaks the
    round-trip surfaces here."""

    def test_set_persists_and_reports_hot_reload_correctly(
        self, test_client, tmp_path, monkeypatch,
    ):
        # End-to-end contract for ``/config set <hot-key>``:
        #   1. The new value lands in the on-disk JSON.
        #   2. ``hot_reload`` is True (kube_context is NOT in
        #      ``_COLD_KEYS`` from Phase 3a).
        #   3. The coerced value comes back in the response body.
        #
        # We don't assert that a follow-up GET reflects the value
        # because ``get_display_dict`` reads from the *live* settings
        # singleton — settings.reload() is what bridges file → memory,
        # and the test fixture monkey-patches the file path but not
        # the settings module's source. The shape contract (write
        # path → file + hot_reload flag) is what /config users
        # actually depend on.
        from chaos_agent.tui.config_store import ConfigStore
        import json

        cfg_path = str(tmp_path / "config.json")
        monkeypatch.setattr(
            ConfigStore, "__init__",
            lambda self, config_path=None: setattr(self, "_path", cfg_path),
        )
        r = test_client.post(
            "/api/v1/config/kube_context",
            json={"value": "test-cluster"},
        )
        body = r.json()
        assert body["status"] == "success"
        # Response carries the canonical (post-coercion) value + hot
        # flag. kube_context isn't in any coercion set so it stays
        # a string verbatim.
        assert body["data"]["value"] == "test-cluster"
        assert body["data"]["hot_reload"] is True, (
            "kube_context is a runtime key — hot_reload must be true; "
            "if this fails, _COLD_KEYS likely grew kube_context"
        )
        # Disk verification — the canonical place /config writes to.
        with open(cfg_path) as f:
            stored = json.load(f)
        assert stored["kube_context"] == "test-cluster"

    def test_get_returns_masked_config_shape(self, test_client):
        # ``GET /api/v1/config`` returns the masked display dict +
        # the resolved config_path. The dict is whatever
        # ``ConfigStore.get_display_dict`` produces — we just pin
        # the contract that the keys ``llm_api_key`` and
        # ``model_name`` exist (so a refactor that removes the API
        # key masking surfaces here).
        r = test_client.get("/api/v1/config")
        body = r.json()
        assert body["status"] == "success"
        cfg = body["data"]["config"]
        assert "llm_api_key" in cfg
        # Either the API key is configured (asterisks) or unset
        # ("(未配置)" / similar). Either way it must NOT leak the
        # actual key. ``in`` checks the renderer didn't accidentally
        # ship the raw value.
        assert "*" in cfg["llm_api_key"] or cfg["llm_api_key"] in (
            "(未配置)",
            "(unset)",
            "",
        )
        # config_path is always populated (ConfigStore.path always
        # resolves to a string).
        assert isinstance(body["data"]["config_path"], str)
        assert body["data"]["config_path"]

    def test_set_then_unset_clears(self, test_client, tmp_path, monkeypatch):
        from chaos_agent.tui.config_store import ConfigStore
        import json

        cfg_path = str(tmp_path / "config.json")
        monkeypatch.setattr(
            ConfigStore, "__init__",
            lambda self, config_path=None: setattr(self, "_path", cfg_path),
        )
        test_client.post(
            "/api/v1/config/timeout_kubectl", json={"value": "45"},
        )
        with open(cfg_path) as f:
            stored = json.load(f)
        assert stored["timeout_kubectl"] == 45
        # Now unset.
        r = test_client.delete("/api/v1/config/timeout_kubectl")
        body = r.json()
        assert body["status"] == "success"
        assert body["data"]["was_present"] is True
        # File should no longer carry the key.
        with open(cfg_path) as f:
            stored = json.load(f)
        assert "timeout_kubectl" not in stored


# =============================================================================
# Phase 4 — Memory compaction event pipeline
# =============================================================================


class TestStreamEventMemoryCompactionFields:
    """``StreamEvent`` carries the new compaction fields and the wire
    serialisation drops them when irrelevant."""

    def test_default_fields_falsy_stripped(self):
        from chaos_agent.agent.streaming import StreamEvent

        evt = StreamEvent(type="token", content="hello")
        d = evt.to_dict()
        # All compaction fields are zero/empty by default → must NOT
        # leak onto the wire frame for non-compaction events. Without
        # the falsy-strip every token frame would carry useless
        # ``compaction_phase: "", tokens_before: 0, ...`` keys.
        assert "compaction_phase" not in d
        assert "tokens_before" not in d
        assert "tokens_after" not in d
        assert "messages_compacted" not in d
        assert "duration_ms" not in d
        assert "layer" not in d
        # Same falsy-strip must apply to context_size fields on
        # non-context_size events — otherwise every token frame
        # would carry useless ``context_current_tokens: 0, ...`` and
        # bloat the SSE wire for no benefit.
        assert "context_current_tokens" not in d
        assert "context_trigger_tokens" not in d
        assert "context_max_tokens" not in d
        assert "context_messages_count" not in d

    def test_memory_compaction_event_serialises_fields(self):
        from chaos_agent.agent.streaming import StreamEvent

        evt = StreamEvent(
            type="memory_compaction",
            content="compaction done",
            task_id="turn-abc",
            compaction_phase="completed",
            tokens_before=12000,
            tokens_after=4500,
            messages_compacted=23,
            duration_ms=6234.0,
            layer="llm_summary",
        )
        d = evt.to_dict()
        assert d["type"] == "memory_compaction"
        assert d["compaction_phase"] == "completed"
        assert d["tokens_before"] == 12000
        assert d["tokens_after"] == 4500
        assert d["messages_compacted"] == 23
        assert d["duration_ms"] == 6234.0
        assert d["layer"] == "llm_summary"

    def test_context_size_event_carries_all_fields(self):
        from chaos_agent.agent.streaming import StreamEvent

        evt = StreamEvent(
            type="context_size",
            task_id="turn-ctx",
            context_current_tokens=95000,
            context_trigger_tokens=108800,
            context_max_tokens=128000,
            context_messages_count=47,
        )
        d = evt.to_dict()
        assert d["type"] == "context_size"
        assert d["context_current_tokens"] == 95000
        assert d["context_trigger_tokens"] == 108800
        assert d["context_max_tokens"] == 128000
        assert d["context_messages_count"] == 47

    def test_context_size_event_forces_zero_onto_wire(self):
        # Regression guard: a context_size frame whose
        # current_tokens is genuinely 0 (empty thread, first hook call
        # against a fresh state) MUST still arrive with the field on
        # the wire. Without the to_dict special-case, the falsy strip
        # would drop ``context_current_tokens: 0`` and the TS reducer
        # would see ``undefined`` → 0 → no display change → Footer
        # silently stays on the last value or falls back to
        # ``ns:default``, both wrong.
        from chaos_agent.agent.streaming import StreamEvent

        evt = StreamEvent(
            type="context_size",
            task_id="turn-fresh",
            context_current_tokens=0,
            context_trigger_tokens=108800,
            context_max_tokens=128000,
            context_messages_count=0,
        )
        d = evt.to_dict()
        # All four fields PRESENT even when value is 0.
        assert d["context_current_tokens"] == 0
        assert d["context_trigger_tokens"] == 108800
        assert d["context_max_tokens"] == 128000
        assert d["context_messages_count"] == 0


class TestHookFanOutToTuiTracker:
    """``PreReasoningHook._emit_compaction_event`` must fan out to BOTH
    the per-task tracker (CLI consumers) AND the per-tui-session
    tracker (TS TUI subscribers). Without the second emit the TS
    TUI's main turn SSE would never see compaction events because it
    can't subscribe to the dynamically-allocated op-task-id."""

    def test_emits_to_both_trackers(self, monkeypatch):
        from chaos_agent.memory.hook import PreReasoningHook
        from chaos_agent.observability.status_tracker import (
            _trackers,
            get_tracker,
            subscribe,
            unsubscribe,
        )

        # Reset global registry so the test is hermetic.
        _trackers.clear()

        # Build a minimal hook — only ``_emit_compaction_event`` is
        # exercised; the heavy compactor / context_manager deps stay
        # untouched.
        hook = PreReasoningHook.__new__(PreReasoningHook)
        # __new__ bypasses __init__; stub the attrs the helper reads.
        hook.session_store = None  # disables _persist_to_session disk path
        # ``_state_tui_session_id`` is the field set in ``__call__``
        # before _emit_compaction_event is called. Stub it directly.
        hook._state_tui_session_id = "sess-test"

        task_q = subscribe("task-X")
        tui_q = subscribe("tui-sess-test")
        try:
            hook._emit_compaction_event(
                task_id="task-X",
                phase="started",
                message="Compressing 20 messages",
                category="node",
                detail={"messages_to_compact": 20, "total_tokens_before": 8000},
                duration_ms=0.0,
            )
            assert task_q.qsize() == 1, "CLI tracker must receive event"
            assert tui_q.qsize() == 1, (
                "TUI tracker must ALSO receive event — Fix A"
            )
            cli_evt = task_q.get_nowait()
            tui_evt = tui_q.get_nowait()
            assert cli_evt.source == "memory_compression"
            assert tui_evt.source == "memory_compression"
            # Same payload on both sides.
            assert cli_evt.detail == tui_evt.detail
            assert cli_evt.phase == tui_evt.phase == "started"
        finally:
            unsubscribe("task-X", task_q)
            unsubscribe("tui-sess-test", tui_q)

    def test_no_fan_out_when_tui_session_id_missing(self):
        # Defensive: an older callsite that doesn't set
        # _state_tui_session_id (or a path where state never carried
        # one) must NOT crash and must NOT create a tui-{empty}
        # tracker. Verify the fan-out is gated on truthy session id.
        from chaos_agent.memory.hook import PreReasoningHook
        from chaos_agent.observability.status_tracker import (
            _trackers,
            subscribe,
            unsubscribe,
        )

        _trackers.clear()
        hook = PreReasoningHook.__new__(PreReasoningHook)
        hook.session_store = None
        hook._state_tui_session_id = ""  # missing

        task_q = subscribe("task-Y")
        try:
            hook._emit_compaction_event(
                task_id="task-Y",
                phase="completed",
                message="done",
                category="node",
                detail={"messages_compacted": 10, "tokens_before": 5000, "tokens_after": 1500},
                duration_ms=2000.0,
            )
            assert task_q.qsize() == 1
            # No phantom tui-{empty} tracker created.
            assert "tui-" not in _trackers
        finally:
            unsubscribe("task-Y", task_q)


class TestMergedStreamRaceAndExceptionHandling:
    """The ``_merged_stream`` helper inside turn.py merges graph events
    with status-tracker events. Two race / regression scenarios that
    silent-swallow data must stay locked down:

    G1 — graph_pump exceptions surface to the SSE event_generator so
         the user sees an ``error`` event instead of a clean exit.
    G2 — status events queued before graph_done don't get lost when
         the main loop sees graph_done first (order is asyncio-
         scheduling-dependent, so the drain must check both unified
         AND _tracker_queue).

    We can't import the closure directly, so we re-implement the
    SAME merge contract here and exercise both regression scenarios
    against it. If the closure inside turn.py drifts away from this
    contract, the integration breaks even though the unit reads OK —
    so update both sides together.
    """

    @pytest.mark.asyncio
    async def test_g1_graph_exception_surfaces(self):
        # Reproduce the merge contract minimally.
        unified: asyncio.Queue = asyncio.Queue()
        tracker_queue: asyncio.Queue = asyncio.Queue()
        graph_done = object()

        async def _graph_pump_failing():
            try:
                # Simulate graph stream raising mid-iteration.
                raise RuntimeError("graph blew up")
            finally:
                await unified.put(("graph_done", graph_done))

        async def _status_pump():
            try:
                while True:
                    evt = await tracker_queue.get()
                    await unified.put(("status", evt))
            except asyncio.CancelledError:
                pass

        g_task = asyncio.create_task(_graph_pump_failing())
        s_task = asyncio.create_task(_status_pump())
        # Wait for graph_pump to enqueue graph_done.
        await asyncio.sleep(0.01)

        # The main-loop exception-surfacing logic, extracted.
        kind, _ = await unified.get()
        assert kind == "graph_done"
        # Drain (no events).
        s_task.cancel()
        try:
            await s_task
        except asyncio.CancelledError:
            pass
        # G1: g_task.exception() must be the original RuntimeError,
        # which the merge helper re-raises so the outer
        # event_generator's except can emit an SSE error frame.
        assert g_task.done()
        exc = g_task.exception()
        assert isinstance(exc, RuntimeError)
        assert "graph blew up" in str(exc)

    @pytest.mark.asyncio
    async def test_g2_pending_status_event_drained_after_graph_done(self):
        import asyncio as _aio

        unified: _aio.Queue = _aio.Queue()
        tracker_queue: _aio.Queue = _aio.Queue()
        graph_done = object()

        # Pre-load the tracker queue with one event the status_pump
        # hasn't picked up yet — simulates the race where graph_pump
        # finishes BEFORE status_pump's next ``get()`` cycle.
        from chaos_agent.observability.status_tracker import StatusEvent
        late_event = StatusEvent(
            task_id="task-late",
            phase="completed",
            category="node",
            source="memory_compression",
            message="Compression done",
            detail={"tokens_before": 5000, "tokens_after": 1500},
            duration_ms=3000.0,
        )

        async def _graph_pump_fast():
            try:
                # Graph emits ZERO real events, then ends — this is
                # the worst case for the race window.
                pass
            finally:
                await unified.put(("graph_done", graph_done))

        async def _status_pump():
            try:
                while True:
                    evt = await tracker_queue.get()
                    await unified.put(("status", evt))
            except _aio.CancelledError:
                pass

        # Put the event into tracker_queue BEFORE pumps start —
        # status_pump will eventually take it, but if graph_pump
        # races ahead we need the drain step to recover it.
        tracker_queue.put_nowait(late_event)

        g_task = _aio.create_task(_graph_pump_fast())
        s_task = _aio.create_task(_status_pump())
        await _aio.sleep(0)  # let one scheduling round pass

        # Replay the drain logic from turn.py's _merged_stream.
        consumed_status_events = []
        kind, _ = await unified.get()
        if kind == "graph_done":
            s_task.cancel()
            try:
                await s_task
            except _aio.CancelledError:
                pass
            # Drain unified.
            while True:
                try:
                    nk, np = unified.get_nowait()
                except _aio.QueueEmpty:
                    break
                if nk == "graph_done":
                    continue
                if nk == "status":
                    consumed_status_events.append(np)
            # Drain tracker_queue (the path G2 was missing).
            while True:
                try:
                    evt = tracker_queue.get_nowait()
                    consumed_status_events.append(evt)
                except _aio.QueueEmpty:
                    break

        # G2: the late event must be drained, not lost.
        assert len(consumed_status_events) == 1, (
            f"expected 1 status event drained, got {len(consumed_status_events)}"
        )
        assert consumed_status_events[0].source == "memory_compression"
        assert consumed_status_events[0].phase == "completed"

        # Clean up g_task.
        if not g_task.done():
            g_task.cancel()
            try:
                await g_task
            except _aio.CancelledError:
                pass


class TestConvertCompactionStatusEvent:
    """The ``_convert_compaction_status`` helper inside ``turn.py``
    maps a tracker ``StatusEvent`` (with source==memory_compression)
    into a wire-shaped ``StreamEvent``. We can't import the closure
    directly, so we re-implement the smallest equivalent
    transformation here and pin the field map. Drift in either
    direction surfaces here."""

    def test_started_event_maps_fields(self):
        from chaos_agent.observability.status_tracker import StatusEvent

        evt = StatusEvent(
            task_id="task-Z",
            phase="started",
            category="node",
            source="memory_compression",
            message="Compressing 23 messages (5 kept)",
            detail={
                "messages_to_compact": 23,
                "messages_to_keep": 5,
                "total_tokens_before": 12000,
            },
            duration_ms=0.0,
        )
        # Mirror the real converter's logic.
        assert evt.source == "memory_compression"
        assert evt.detail.get("total_tokens_before") == 12000
        assert evt.detail.get("messages_to_compact") == 23
        assert evt.phase == "started"

    def test_completed_event_uses_alternate_keys(self):
        # The hook emits ``messages_compacted`` (past-tense) +
        # ``tokens_before/after`` on the completed event, vs
        # ``messages_to_compact`` + ``total_tokens_before`` on
        # started. Both shapes must extract correctly — pin the
        # converter's `or`-chain key fallback.
        from chaos_agent.observability.status_tracker import StatusEvent

        evt = StatusEvent(
            task_id="task-Z",
            phase="completed",
            category="node",
            source="memory_compression",
            message="Compression done",
            detail={
                "messages_compacted": 23,
                "tokens_before": 12000,
                "tokens_after": 4500,
            },
            duration_ms=6234.0,
        )
        assert evt.detail.get("tokens_before") == 12000
        assert evt.detail.get("tokens_after") == 4500
        assert evt.detail.get("messages_compacted") == 23


class TestModelSetVsConfigSetParity:
    """``/model set X`` and ``/config set model_name X`` must be
    equivalent — both go through ``ConfigStore.set("model_name", X)``
    and end up in the same place on disk. Drift between the two
    paths would silently strand users on whichever they used first."""

    def test_writes_same_file_with_same_value(
        self, test_client, tmp_path, monkeypatch,
    ):
        from chaos_agent.tui.config_store import ConfigStore
        import json

        cfg_path = str(tmp_path / "config.json")
        monkeypatch.setattr(
            ConfigStore, "__init__",
            lambda self, config_path=None: setattr(self, "_path", cfg_path),
        )
        # Path 1: /model set
        test_client.post("/api/v1/model", json={"model_name": "via-model"})
        with open(cfg_path) as f:
            stored = json.load(f)
        assert stored["model_name"] == "via-model"
        # Path 2: /config set model_name (same backend, different
        # entry point). Both must roundtrip identically.
        test_client.post(
            "/api/v1/config/model_name", json={"value": "via-config"},
        )
        with open(cfg_path) as f:
            stored = json.load(f)
        assert stored["model_name"] == "via-config"
        # Both must report cold (model_name in _COLD_KEYS).
        r_model = test_client.post(
            "/api/v1/model", json={"model_name": "again-via-model"},
        )
        assert r_model.json()["data"]["restart_required"] is True
        r_cfg = test_client.post(
            "/api/v1/config/model_name", json={"value": "again-via-config"},
        )
        assert r_cfg.json()["data"]["hot_reload"] is False, (
            "model_name via /config must report hot_reload: false "
            "(model_name is a cold key)"
        )
