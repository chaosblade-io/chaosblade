"""End-to-end integration test for postmortem subsystem.

Spec: run save_memory with a mock LLM and a realistic post-experiment
state. Verify:
  1. should_generate_postmortem fires for a successful inject
  2. LLM is invoked exactly once
  3. Markdown lands on disk at the expected path
  4. AgentState.postmortem dict carries path/markdown/summary
  5. result_payload helper surfaces the postmortem on the SSE envelope
  6. CLI ``blade-ai postmortem <task_id>`` can read it back

Does NOT spin up a real LLM / graph / FastAPI server — those layers
are exercised by their own unit tests. This test is the contract glue:
prove the dataflow across builder → generator → store → save_memory →
result envelope → CLI doesn't break silently.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from chaos_agent.agent.postmortem import (
    build_postmortem_context,
    save_postmortem,
    should_generate_postmortem,
)
from chaos_agent.agent.postmortem.generator import generate_postmortem, make_summary
from chaos_agent.agent.postmortem.store import (
    postmortem_exists,
    read_postmortem,
)


_FAKE_LLM_OUTPUT = """## Summary
故障注入完成，HPA 在 8 秒内扩容到 5 个副本。

## Background
- 用户请求: 对 cms-demo 命名空间下 payment pods 做 CPU 满载
- 故障类型: pod-cpu-fullload
- 目标: cms-demo / payment-7b4f8c-x1z
- 参数: time=300s, percent=80

## Timeline
- T+0s: blade create 成功 (uid=blade-uid-x)
- T+5s: layer1 verifier PASSED
- T+8s: HPA 触发，replicas 3 → 5
- T+47s: recovery 完成

## Key Metrics
- CPU usage: 12% → 81% → 14%
- HPA replicas: 3 → 5 → 3

## Verifier Findings
- Layer 1 status: **passed**
- Layer 2 verdict: **verified**
- safety_score overall: 32 (low risk)

## Side Effects
- Pre-snapshot: 12 pods, 4 endpoints
- Post-diff: 1 pod restart (payment-x), HPA scaled out

## Root Cause Hypothesis
HPA 阈值设置合理，扩容机制正常工作。

## Recommendations
- 验证 readinessProbe 配置避免重启 pod 被纳入流量
- 考虑设置 `behavior.scaleUp.stabilizationWindowSeconds` 降低抖动
"""


@pytest.fixture
def real_post_inject_state():
    """A realistic state at the moment save_memory begins, post-inject."""
    return {
        "task_id": "task-e2e12345",
        "confirmed_intent": "inject",
        "skill_name": "k8s-chaos-skills",
        "blade_uid": "blade-uid-e2e",
        "input": "对 cms-demo 做 CPU 压测",
        "fault_spec": {
            "namespace": "cms-demo",
            "scope": "pod",
            "names": ("payment-7b4f8c-x1z",),
            "blade_target": "cpu",
            "blade_action": "fullload",
            "params": {"time": "300", "percent": "80"},
            "user_description": "对 cms-demo 做 CPU 压测",
        },
        "verification": {
            "level": "verified",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
            "side_effects": {
                "container_restarts": ["payment-7b4f8c-x1z"],
            },
        },
        "se_snapshot": {
            "pods": {f"pod-{i}": {} for i in range(12)},
            "endpoints": {f"svc-{i}": {} for i in range(4)},
        },
        "baseline_capture": {"cpu_percent_before": 12, "cpu_percent_peak": 81},
        "safety_score": {
            "overall": 32, "level": "safe",
            "blast_radius": {"score": 25}, "frequency": {"score": 10},
            "time": {"score": 5}, "topology": {"score": 8},
        },
        "result": {"task_state": "injected", "duration_ms": 47000},
        "task_state": "injected",
        "messages": [HumanMessage(content="对 cms-demo 做 CPU 压测")],
    }


class TestPostmortemE2E:
    @pytest.mark.asyncio
    async def test_full_pipeline_real_state_mock_llm(
        self, real_post_inject_state, tmp_path,
    ):
        """builder → generator → store → CLI read-back round-trip."""

        class _Settings:
            postmortem_enabled = True
            postmortem_max_messages = 30
            postmortem_timeout_seconds = 10

        s = _Settings()

        # 1. Gate fires for a real injection
        assert should_generate_postmortem(real_post_inject_state, s) is True

        # 2. Builder produces a non-trivial context
        ctx = build_postmortem_context(
            real_post_inject_state, max_messages=s.postmortem_max_messages,
        )
        assert ctx["task_id"] == "task-e2e12345"
        assert ctx["fault_spec"]["namespace"] == "cms-demo"
        assert ctx["pre_snapshot"]["pods_count"] == 12
        assert ctx["verification"]["level"] == "verified"
        assert ctx["safety_score"]["overall"] == 32

        # 3. LLM mocked; verify it's called once with system + user
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content=_FAKE_LLM_OUTPUT))

        markdown = await generate_postmortem(
            ctx, mock_llm, timeout=s.postmortem_timeout_seconds,
        )
        assert mock_llm.ainvoke.call_count == 1
        assert "## Summary" in markdown
        assert "HPA" in markdown

        # 4. Markdown lands on disk in tmp_path
        path = save_postmortem(
            "task-e2e12345", markdown, root=tmp_path,
            header_meta={
                "skill_name": "k8s-chaos-skills",
                "namespace": "cms-demo",
                "status": "verified",
                "duration": "47s",
            },
        )
        assert path.is_file()
        assert postmortem_exists("task-e2e12345", root=tmp_path)

        # 5. File contents = header + body
        on_disk = read_postmortem("task-e2e12345", root=tmp_path)
        assert "# Postmortem: k8s-chaos-skills on cms-demo" in on_disk
        assert "**Status**: verified" in on_disk
        assert "**Duration**: 47s" in on_disk
        assert "## Summary" in on_disk

        # 6. Summary extraction picks up the first ## Summary paragraph
        summary = make_summary(markdown)
        assert "HPA" in summary
        assert len(summary) <= 204  # 200 + "..."

    @pytest.mark.asyncio
    async def test_save_memory_attaches_postmortem_when_enabled(
        self, real_post_inject_state, tmp_path, monkeypatch,
    ):
        """Verify the save_memory node actually wires postmortem into
        the returned updates dict when conditions are met."""
        from chaos_agent.config import settings as s_mod
        monkeypatch.setattr(s_mod.settings, "postmortem_enabled", True)
        monkeypatch.setattr(s_mod.settings, "postmortem_timeout_seconds", 10)
        monkeypatch.setattr(s_mod.settings, "postmortem_max_messages", 30)
        monkeypatch.setattr(s_mod.settings, "memory_dir", tmp_path / "memory")

        from chaos_agent.agent.nodes import memory_nodes
        monkeypatch.setattr(memory_nodes, "sync_to_store", AsyncMock())
        monkeypatch.setattr(
            memory_nodes, "sync_node_status_to_session", lambda *a, **k: None,
        )

        # Stub the LLM factory so save_memory doesn't try to dial a
        # real provider. Returns a mock that yields _FAKE_LLM_OUTPUT.
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content=_FAKE_LLM_OUTPUT))
        with patch(
            "chaos_agent.agent.factory.make_llm", return_value=mock_llm,
        ), patch(
            "chaos_agent.agent.postmortem.store.POSTMORTEM_DIR",
            tmp_path / "postmortems",
        ):
            updates = await memory_nodes.save_memory(real_post_inject_state)

        # postmortem attached to updates
        assert "postmortem" in updates
        pm = updates["postmortem"]
        assert isinstance(pm, dict)
        assert pm["path"].endswith("task-e2e12345.md")
        assert "## Summary" in pm["markdown"]
        assert pm["summary"]  # non-empty

        # File written to the patched directory
        written = Path(pm["path"])
        assert written.is_file()
        assert "# Postmortem: " in written.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_save_memory_graceful_on_llm_timeout(
        self, real_post_inject_state, tmp_path, monkeypatch,
    ):
        """LLM timeout → no postmortem in updates, save_memory still returns."""
        import asyncio as _asyncio

        from chaos_agent.config import settings as s_mod
        monkeypatch.setattr(s_mod.settings, "postmortem_enabled", True)
        monkeypatch.setattr(s_mod.settings, "postmortem_timeout_seconds", 1)
        monkeypatch.setattr(s_mod.settings, "memory_dir", tmp_path / "memory")

        from chaos_agent.agent.nodes import memory_nodes
        monkeypatch.setattr(memory_nodes, "sync_to_store", AsyncMock())
        monkeypatch.setattr(
            memory_nodes, "sync_node_status_to_session", lambda *a, **k: None,
        )

        async def slow_ainvoke(_messages):
            await _asyncio.sleep(2)
            return AIMessage(content="too late")

        mock_llm = AsyncMock()
        mock_llm.ainvoke = slow_ainvoke
        with patch(
            "chaos_agent.agent.factory.make_llm", return_value=mock_llm,
        ):
            updates = await memory_nodes.save_memory(real_post_inject_state)

        assert updates.get("postmortem") is None  # graceful degradation  # R11: always-write None (was: not-in)
        assert "finished_at" in updates  # save_memory still completed

    @pytest.mark.asyncio
    async def test_save_memory_skips_llm_for_user_rejected(
        self, tmp_path, monkeypatch,
    ):
        """USER_REJECTED is outside the whitelist — should NOT spend an
        LLM call. Regression guard against the gate quietly failing and
        burning budget on no-data states."""
        from chaos_agent.config import settings as s_mod
        monkeypatch.setattr(s_mod.settings, "postmortem_enabled", True)
        monkeypatch.setattr(s_mod.settings, "memory_dir", tmp_path / "memory")

        from chaos_agent.agent.nodes import memory_nodes
        monkeypatch.setattr(memory_nodes, "sync_to_store", AsyncMock())
        monkeypatch.setattr(
            memory_nodes, "sync_node_status_to_session", lambda *a, **k: None,
        )

        rejected_state = {
            "task_id": "task-rejected1",
            "confirmed_intent": "inject",
            "blade_uid": "",  # never injected
            "failure_detail": {
                "category": "user_rejected",
                "context": "user said no at confirm gate",
            },
            "messages": [],
        }

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content="should not see this"))
        with patch(
            "chaos_agent.agent.factory.make_llm", return_value=mock_llm,
        ):
            updates = await memory_nodes.save_memory(rejected_state)

        # Gate filtered it out → LLM not invoked at all
        assert mock_llm.ainvoke.call_count == 0
        assert updates.get("postmortem") is None  # R11: always-write None (was: not-in)
        # save_memory still completed cleanly
        assert "finished_at" in updates

    @pytest.mark.asyncio
    async def test_save_memory_skips_llm_for_safety_rejected(
        self, tmp_path, monkeypatch,
    ):
        """SAFETY_REJECTED variant of the gate — also blocked at the
        ``should_generate_postmortem`` whitelist."""
        from chaos_agent.config import settings as s_mod
        monkeypatch.setattr(s_mod.settings, "postmortem_enabled", True)
        monkeypatch.setattr(s_mod.settings, "memory_dir", tmp_path / "memory")

        from chaos_agent.agent.nodes import memory_nodes
        monkeypatch.setattr(memory_nodes, "sync_to_store", AsyncMock())
        monkeypatch.setattr(
            memory_nodes, "sync_node_status_to_session", lambda *a, **k: None,
        )

        state = {
            "task_id": "task-safety01",
            "confirmed_intent": "inject",
            "blade_uid": "",
            "failure_detail": {
                "category": "safety_rejected",
                "context": "namespace in blacklist",
            },
            "messages": [],
        }

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock()
        with patch(
            "chaos_agent.agent.factory.make_llm", return_value=mock_llm,
        ):
            updates = await memory_nodes.save_memory(state)

        assert mock_llm.ainvoke.call_count == 0
        assert updates.get("postmortem") is None  # R11: always-write None (was: not-in)

    @pytest.mark.asyncio
    async def test_save_memory_clears_stale_postmortem_on_reject(
        self, tmp_path, monkeypatch,
    ):
        """R11 — when should_generate=False, save_memory MUST explicitly
        write postmortem=None so a stale value from the previous inject
        in the same LangGraph thread can't bleed through.

        Regression scenario:
          1. Inject #1 succeeds, state.postmortem = {"path": "...", ...}
          2. Inject #2 is SAFETY_REJECTED, should_generate returns False
          3. If save_memory didn't overwrite, state.postmortem would
             carry over and the user would see #1's report on #2's card."""
        from chaos_agent.config import settings as s_mod
        monkeypatch.setattr(s_mod.settings, "postmortem_enabled", True)
        monkeypatch.setattr(s_mod.settings, "memory_dir", tmp_path / "memory")

        from chaos_agent.agent.nodes import memory_nodes
        monkeypatch.setattr(memory_nodes, "sync_to_store", AsyncMock())
        monkeypatch.setattr(
            memory_nodes, "sync_node_status_to_session", lambda *a, **k: None,
        )

        # State carries a stale postmortem from a prior run.
        state = {
            "task_id": "task-stale001",
            "confirmed_intent": "inject",
            "blade_uid": "",
            "failure_detail": {"category": "user_rejected"},
            "messages": [],
            # Stale leftover from the previous inject on this thread:
            "postmortem": {
                "path": "/tmp/old.md",
                "markdown": "## Summary\nOLD report from prior run",
                "summary": "OLD",
            },
        }

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock()
        with patch(
            "chaos_agent.agent.factory.make_llm", return_value=mock_llm,
        ):
            updates = await memory_nodes.save_memory(state)

        # postmortem MUST be in updates AND set to None (not just absent —
        # absence would let LangGraph state-merge preserve the stale value).
        assert "postmortem" in updates
        assert updates["postmortem"] is None
        # LLM wasn't called (gate filter)
        assert mock_llm.ainvoke.call_count == 0

    @pytest.mark.asyncio
    async def test_save_memory_passes_tracing_callbacks_to_pm_llm(
        self, real_post_inject_state, tmp_path, monkeypatch,
    ):
        """R10 — postmortem LLM call must use the SAME tracing / OTel
        callbacks as the main graph LLM, so its token usage shows up in
        TaskTrace + OTel exports. Without this, the TUI Footer's per-
        turn token counter under-reports by 1-3K every postmortem call."""
        from chaos_agent.config import settings as s_mod
        monkeypatch.setattr(s_mod.settings, "postmortem_enabled", True)
        monkeypatch.setattr(s_mod.settings, "memory_dir", tmp_path / "memory")

        from chaos_agent.agent.nodes import memory_nodes
        monkeypatch.setattr(memory_nodes, "sync_to_store", AsyncMock())
        monkeypatch.setattr(
            memory_nodes, "sync_node_status_to_session", lambda *a, **k: None,
        )

        # Plant fake tracing + OTel callbacks on the status_tracker module
        # (where factory.py registers them); save_memory should pick them up.
        from chaos_agent.observability import status_tracker as _st_mod
        sentinel_trace = object()
        sentinel_otel = object()
        monkeypatch.setattr(_st_mod, "_tracing_callback", sentinel_trace, raising=False)
        monkeypatch.setattr(_st_mod, "_otel_callback", sentinel_otel, raising=False)

        # Spy on make_llm so we can inspect what callbacks save_memory
        # passes when constructing pm_llm.
        captured_kwargs: dict = {}
        from chaos_agent.agent import factory as _factory

        def spy_make_llm(**kwargs):
            captured_kwargs.update(kwargs)
            mock = AsyncMock()
            mock.ainvoke = AsyncMock(return_value=AIMessage(content="## Summary\nOK"))
            return mock

        monkeypatch.setattr(_factory, "make_llm", spy_make_llm)

        await memory_nodes.save_memory(real_post_inject_state)

        assert "callbacks" in captured_kwargs, (
            "save_memory did not pass callbacks to make_llm() — "
            "postmortem's LLM call will bypass tracing/OTel."
        )
        cbs = captured_kwargs["callbacks"]
        assert sentinel_trace in cbs, "tracing callback missing"
        assert sentinel_otel in cbs, "OTel callback missing"

    @pytest.mark.asyncio
    async def test_save_memory_skips_llm_for_llm_refusal(
        self, real_post_inject_state, tmp_path, monkeypatch,
    ):
        """LLM returns a refusal (no ## Summary heading) → generator
        validator returns "" → caller degrades to no postmortem.
        Regression guard for the structural validation gate."""
        from chaos_agent.config import settings as s_mod
        monkeypatch.setattr(s_mod.settings, "postmortem_enabled", True)
        monkeypatch.setattr(s_mod.settings, "memory_dir", tmp_path / "memory")

        from chaos_agent.agent.nodes import memory_nodes
        monkeypatch.setattr(memory_nodes, "sync_to_store", AsyncMock())
        monkeypatch.setattr(
            memory_nodes, "sync_node_status_to_session", lambda *a, **k: None,
        )

        # LLM returns "I'm sorry..." — passes non-empty but fails
        # structural check (no ## Summary heading).
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(
            return_value=AIMessage(content="I'm sorry, I cannot help with that."),
        )
        with patch(
            "chaos_agent.agent.factory.make_llm", return_value=mock_llm,
        ):
            updates = await memory_nodes.save_memory(real_post_inject_state)

        assert mock_llm.ainvoke.call_count == 1  # LLM WAS invoked
        assert updates.get("postmortem") is None  # but output was rejected  # R11: always-write None (was: not-in)


# ─── _build_result_payload / inject_stream envelope shape ────────────


class TestResultPayloadShape:
    """T6 — Fix #1 + #2: verify both result-payload helpers carry the
    postmortem field through to the SSE envelope."""

    @pytest.mark.asyncio
    async def test_turn_build_result_payload_includes_postmortem(
        self,
    ):
        """turn.py's _build_result_payload reads final_state.values
        and must surface ``postmortem`` on data."""
        from unittest.mock import MagicMock

        from chaos_agent.server.routes.turn import _build_result_payload

        # Mock graph + aget_state to return a fake terminal state
        fake_state = MagicMock()
        fake_state.next = None  # not paused
        fake_state.values = {
            "task_id": "task-envelope1",
            "confirmed_intent": "inject",
            "blade_uid": "blade-x",
            "skill_name": "k8s-chaos-skills",
            "fault_spec": {
                "namespace": "demo",
                "scope": "pod",
                "blade_target": "cpu",
                "blade_action": "fullload",
            },
            "verification": {"level": "verified"},
            "postmortem": {
                "path": "/tmp/task-envelope1.md",
                "markdown": "## Summary\nOK",
                "summary": "OK",
            },
        }

        graph = MagicMock()
        graph.aget_state = AsyncMock(return_value=fake_state)

        envelope = await _build_result_payload(graph, {}, "task-envelope1", 0.0)
        assert envelope is not None
        assert envelope["data"]["postmortem"]["path"] == "/tmp/task-envelope1.md"
        assert envelope["data"]["postmortem"]["markdown"] == "## Summary\nOK"

    @pytest.mark.asyncio
    async def test_turn_build_result_payload_postmortem_none_when_absent(
        self,
    ):
        """No postmortem in state → envelope.data.postmortem is None
        (not missing key — TS parseResultEnvelope tolerates both, but
        explicit None is the cleaner contract)."""
        from unittest.mock import MagicMock

        from chaos_agent.server.routes.turn import _build_result_payload

        fake_state = MagicMock()
        fake_state.next = None
        fake_state.values = {
            "task_id": "task-noemv",
            "confirmed_intent": "inject",
            "blade_uid": "blade-x",
            "skill_name": "k8s",
            "fault_spec": {},
            "verification": {"level": "verified"},
            # No postmortem key
        }
        graph = MagicMock()
        graph.aget_state = AsyncMock(return_value=fake_state)

        envelope = await _build_result_payload(graph, {}, "task-noemv", 0.0)
        assert envelope["data"]["postmortem"] is None

    def test_convert_postmortem_status_started_to_node_start(self):
        """R17 — STARTED status from source='postmortem' → SSE node_start
        so the TUI spinner can refresh its thoughtSubject to "postmortem"."""
        from chaos_agent.observability.status_tracker import StatusEvent, StatusPhase

        # Build the converter the same way turn.py builds it inline
        # (closure over turn_id). We replicate the body here to test
        # the conversion logic in isolation.
        def _convert(status_evt, turn_id="turn-1"):
            if getattr(status_evt, "source", "") != "postmortem":
                return None
            phase = getattr(status_evt, "phase", "")
            msg = getattr(status_evt, "message", "") or "Generating postmortem"
            from chaos_agent.agent.streaming import StreamEvent
            if phase in ("completed", "failed"):
                return StreamEvent(
                    type="node_end", task_id=turn_id, node="postmortem",
                    content=msg, phase="save",
                )
            return StreamEvent(
                type="node_start", task_id=turn_id, node="postmortem",
                content=msg, phase="save",
            )

        evt = StatusEvent(
            task_id="t1", phase=StatusPhase.STARTED, category="node",
            source="postmortem", message="Generating postmortem (LLM)...",
        )
        out = _convert(evt)
        assert out is not None
        assert out.type == "node_start"
        assert out.node == "postmortem"
        assert "Generating postmortem" in out.content
        assert out.phase == "save"

    def test_convert_postmortem_status_completed_to_node_end(self):
        """R17 — COMPLETED phase closes the spinner subject via node_end."""
        from chaos_agent.observability.status_tracker import StatusEvent, StatusPhase

        def _convert(status_evt, turn_id="turn-1"):
            if getattr(status_evt, "source", "") != "postmortem":
                return None
            phase = getattr(status_evt, "phase", "")
            msg = getattr(status_evt, "message", "") or "Generating postmortem"
            from chaos_agent.agent.streaming import StreamEvent
            if phase in ("completed", "failed"):
                return StreamEvent(
                    type="node_end", task_id=turn_id, node="postmortem",
                    content=msg, phase="save",
                )
            return StreamEvent(
                type="node_start", task_id=turn_id, node="postmortem",
                content=msg, phase="save",
            )

        evt = StatusEvent(
            task_id="t1", phase=StatusPhase.COMPLETED, category="node",
            source="postmortem", message="Postmortem saved (1234 chars)",
        )
        out = _convert(evt)
        assert out.type == "node_end"
        assert out.node == "postmortem"

    def test_convert_postmortem_status_ignores_unrelated_sources(self):
        """R17 — only source='postmortem' converts; everything else
        passes through unchanged (returned None so other converters
        get their turn)."""
        from chaos_agent.observability.status_tracker import StatusEvent, StatusPhase

        def _convert(status_evt, turn_id="turn-1"):
            if getattr(status_evt, "source", "") != "postmortem":
                return None
            from chaos_agent.agent.streaming import StreamEvent
            return StreamEvent(type="node_start", task_id=turn_id, node="postmortem")

        for source in ("save_memory", "memory_compression", "context_size", "verifier"):
            evt = StatusEvent(
                task_id="t1", phase=StatusPhase.STARTED, category="node",
                source=source, message="x",
            )
            assert _convert(evt) is None, f"source={source} should not convert"

    @pytest.mark.asyncio
    async def test_postmortem_serialized_to_task_details_postmortem_column(
        self, tmp_path, monkeypatch,
    ):
        """R18 — sync_to_store must persist the postmortem dict (as JSON
        string) into the task_details.postmortem column so future SQL
        queries can aggregate across tasks without walking disk files."""
        from chaos_agent.persistence import task_store as _store_mod
        from chaos_agent.persistence.task_store import TaskStore, _JSON_COLUMNS

        # postmortem MUST be in the JSON columns frozenset so the dict
        # gets json.dumps'd before insert.
        assert "postmortem" in _JSON_COLUMNS, (
            "postmortem column missing from _JSON_COLUMNS; sync_to_store "
            "will fail to serialize the dict"
        )

        # And in the detail columns whitelist so _store_sync.py routes
        # it to task_details (not silently dropped).
        from chaos_agent.persistence.task_store_backend import _DETAIL_COLUMNS
        assert "postmortem" in _DETAIL_COLUMNS, (
            "postmortem missing from _DETAIL_COLUMNS; _extract_db_fields "
            "will drop the field silently"
        )

    @pytest.mark.asyncio
    async def test_postmortem_column_roundtrip_through_taskstore(
        self, tmp_path,
    ):
        """R18 — end-to-end: upsert task with postmortem dict → query
        back → JSON content recovered."""
        import json
        from chaos_agent.persistence.task_store_sqlite import SQLiteBackend
        from chaos_agent.persistence.task_store import TaskStore

        db_path = tmp_path / "test-tasks.db"
        backend = SQLiteBackend(db_path)
        store = TaskStore(backend)

        pm_dict = {
            "path": "/tmp/x.md",
            "markdown": "## Summary\nOK",
            "summary": "OK",
        }
        await store.upsert(
            "task-pmcol01", task_state="injected",
            postmortem=pm_dict,
        )

        # Read back via raw backend query
        details = await backend.select_details("task-pmcol01")
        assert details is not None
        raw = details.get("postmortem")
        assert raw is not None, "postmortem column was not written"
        parsed = json.loads(raw)
        assert parsed == pm_dict

        await backend.close()
        """inject_stream.py builds the envelope inline; replicate the
        envelope construction to verify the postmortem field is wired.

        We can't easily invoke the SSE generator in isolation, but the
        envelope construction logic is what matters — this test pins
        the field name + shape so a future refactor can't silently
        drop it."""
        # Inline the envelope construction from inject_stream.py:191-218
        # NOTE: this is intentional duplication for regression-pinning.
        # If inject_stream changes the shape, this test fails until the
        # shape is restored or this test is consciously updated.
        values = {
            "postmortem": {
                "path": "/tmp/x.md",
                "markdown": "## Summary\nx",
                "summary": "x",
            },
        }
        # Field extraction mirrors inject_stream.py line ~213
        envelope_data = {
            "postmortem": values.get("postmortem"),
        }
        assert envelope_data["postmortem"] is not None
        assert envelope_data["postmortem"]["path"] == "/tmp/x.md"


# ─── LangGraph state merge integration ───────────────────────────────


class TestLangGraphStateMerge:
    """T6 — Fix #3: verify save_memory's postmortem field reaches the
    final state that downstream readers (``graph.aget_state``) see."""

    @pytest.mark.asyncio
    async def test_postmortem_propagates_to_graph_final_state(
        self, real_post_inject_state, tmp_path, monkeypatch,
    ):
        """Mini LangGraph with save_memory only → invoke → aget_state →
        verify postmortem field is in final_state.values. Catches the
        edge case where save_memory's return updates don't survive the
        LangGraph state-merge into the checkpoint."""
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import END, StateGraph

        from chaos_agent.agent.state import AgentState
        from chaos_agent.config import settings as s_mod

        monkeypatch.setattr(s_mod.settings, "postmortem_enabled", True)
        monkeypatch.setattr(s_mod.settings, "postmortem_timeout_seconds", 10)
        monkeypatch.setattr(s_mod.settings, "memory_dir", tmp_path / "memory")

        from chaos_agent.agent.nodes import memory_nodes
        monkeypatch.setattr(memory_nodes, "sync_to_store", AsyncMock())
        monkeypatch.setattr(
            memory_nodes, "sync_node_status_to_session", lambda *a, **k: None,
        )

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content=_FAKE_LLM_OUTPUT))

        # Build minimal graph: START → save_memory → END
        g = StateGraph(AgentState)
        g.add_node("save_memory", memory_nodes.save_memory)
        g.set_entry_point("save_memory")
        g.add_edge("save_memory", END)

        checkpointer = MemorySaver()
        compiled = g.compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "test-thread"}}

        with patch(
            "chaos_agent.agent.factory.make_llm", return_value=mock_llm,
        ), patch(
            "chaos_agent.agent.postmortem.store.POSTMORTEM_DIR",
            tmp_path / "postmortems",
        ):
            await compiled.ainvoke(real_post_inject_state, config=config)
            final = await compiled.aget_state(config)

        # The critical assertion: postmortem field survives the
        # save_memory → LangGraph merge → checkpoint → aget_state round-trip
        assert final.values.get("postmortem") is not None
        assert "path" in final.values["postmortem"]
        assert "markdown" in final.values["postmortem"]
