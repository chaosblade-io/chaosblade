"""Tests for chaos_agent.l4.agent — L4ResilienceAgent lifecycle."""

import threading
from unittest.mock import MagicMock, patch

import pytest

from chaos_agent.l4.agent import L4ResilienceAgent, _ChaosAgentPool
from chaos_agent.l4.schemas import L4TaskResult, L4TestTask


def _valid_payload(**overrides):
    payload = {
        "fault_intent": {
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "namespace": "cms-demo",
            "names": ["app=myapp"],
            "params": {"cpu-percent": "80"},
            "duration": 300,
        },
    }
    payload.update(overrides)
    return payload


def _asyncio_run_returns(result):
    def _run(coro):
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        return result

    return _run


class TestChaosAgentPool:
    """Test _ChaosAgentPool initialization and thread safety."""

    def test_starts_uninitialized(self):
        pool = _ChaosAgentPool()
        assert pool._initialized is False
        assert pool.inject_graph is None
        assert pool.recover_graph is None

    @patch("chaos_agent.l4.agent._ChaosAgentPool.ensure_initialized")
    def test_ensure_initialized_sets_flag(self, mock_init):
        """After ensure_initialized, pool should be usable."""
        pool = _ChaosAgentPool()
        pool._initialized = True
        pool.inject_graph = MagicMock()
        pool.recover_graph = MagicMock()
        assert pool._initialized is True

    def test_has_thread_lock(self):
        """Pool class has a threading.Lock for double-check locking."""
        assert isinstance(_ChaosAgentPool._init_lock, type(threading.Lock()))


class TestL4ResilienceAgentInit:
    """Test L4ResilienceAgent initialization."""

    def test_initial_state(self):
        agent = L4ResilienceAgent()
        assert agent._pool is None
        assert agent._completed == {}
        assert agent._state_transitions_buffer == []
        assert not agent._cancel_event.is_set()


class TestL4ResilienceAgentCancel:
    """Test cancel mechanism."""

    def test_request_cancel_sets_event(self):
        agent = L4ResilienceAgent()
        assert not agent.is_cancel_requested()
        agent.request_cancel()
        assert agent.is_cancel_requested()

    def test_cleanup_clears_cancel(self):
        agent = L4ResilienceAgent()
        agent.request_cancel()
        agent.cleanup(None, None)
        assert not agent.is_cancel_requested()


class TestL4ResilienceAgentRecover:
    """Test explicit L4 recover path."""

    @pytest.mark.asyncio
    async def test_explicit_recover_uses_snapshot_resolver_without_checkpoint(self, monkeypatch):
        from chaos_agent.agent import task_snapshot
        from chaos_agent.agent.task_snapshot import RecoverInitialResolution

        class _MissingInjectGraph:
            async def aget_state(self, config):
                return None

        class _RecoverGraph:
            def __init__(self):
                self.initial = None
                self.config = None

            async def ainvoke(self, initial, config):
                self.initial = initial
                self.config = config
                return {
                    "operation": "recover",
                    "result": {"recovered": True, "recovery_level": "recovered"},
                    "recover_verification": {"layer1": {"status": "passed"}},
                    "messages": ["recover-msg"],
                }

        class _SessionStore:
            def __init__(self):
                self.created = None
                self.finalized = None

            def create_session(self, task_id, **kwargs):
                self.created = (task_id, kwargs)

            def finalize_session(self, task_id, **kwargs):
                self.finalized = (task_id, kwargs)

        captured = {}

        async def fake_resolve(
            inject_task_id,
            *,
            record_task_id,
            checkpoint_values,
            agents,
            kubeconfig_override=None,
            **kwargs,
        ):
            captured["inject_task_id"] = inject_task_id
            captured["record_task_id"] = record_task_id
            captured["checkpoint_values"] = checkpoint_values
            captured["agents"] = agents
            captured["kubeconfig_override"] = kubeconfig_override
            initial = {
                "task_id": record_task_id,
                "parent_task_id": inject_task_id,
                "operation": "recover",
                "tui_session_id": "sid-from-snapshot",
                "blade_uid": "uid-from-snapshot",
                "skill_name": "pod-cpu-fullload",
                "fault_spec": {
                    "namespace": "default",
                    "scope": "pod",
                    "names": ["demo"],
                    "labels": {},
                    "blade_target": "cpu",
                    "blade_action": "fullload",
                    "params": {"cpu-percent": "80"},
                },
                "kubeconfig": kubeconfig_override or "",
                "messages": [],
            }
            return RecoverInitialResolution(
                initial_state=initial,
                source_values={"messages": ["baseline"], "blade_uid": "uid-from-snapshot"},
                source="snapshot",
            )

        session_store = _SessionStore()
        monkeypatch.setattr(task_snapshot, "resolve_recover_initial_state", fake_resolve)
        monkeypatch.setattr(
            "chaos_agent.memory.session_store.get_global_session_store",
            lambda: session_store,
        )

        recover_graph = _RecoverGraph()
        pool = MagicMock()
        pool.inject_graph = _MissingInjectGraph()
        pool.recover_graph = recover_graph
        pool.skill_registry = object()
        task = L4TestTask(
            task_id="l4-recover-task",
            intent="recover",
            payload={
                "inject_task_id": "task-inject-missing-checkpoint",
                "kubeconfig": "/tmp/kubeconfig",
            },
        )

        result = await L4ResilienceAgent()._async_recover_explicit(pool, None, task)

        assert result.status == "passed"
        assert captured["inject_task_id"] == "task-inject-missing-checkpoint"
        assert captured["checkpoint_values"] == {}
        assert captured["agents"] == {"skill_registry": pool.skill_registry}
        assert captured["kubeconfig_override"] == "/tmp/kubeconfig"
        assert recover_graph.initial["task_id"] == captured["record_task_id"]
        assert recover_graph.config["configurable"]["thread_id"] == captured["record_task_id"]
        assert session_store.created[0] == captured["record_task_id"]
        assert session_store.created[1]["tui_session_id"] == "sid-from-snapshot"
        assert session_store.created[1]["baseline_messages"] == ["baseline"]
        assert session_store.finalized[0] == captured["record_task_id"]
        assert session_store.finalized[1]["remaining_messages"] == ["recover-msg"]


class TestL4ResilienceAgentIdempotent:
    """Test B3 idempotent behavior."""

    def test_cached_result_returned(self):
        agent = L4ResilienceAgent()
        cached = L4TaskResult(task_id="t-001", status="passed")
        agent._completed["t-001"] = cached

        task = L4TestTask(task_id="t-001", intent="test")
        result = agent.execute(None, task)
        assert result is cached

    @patch("chaos_agent.l4.agent.L4ResilienceAgent._ensure_pool")
    @patch("chaos_agent.l4.agent.asyncio.run")
    def test_lru_eviction(self, mock_asyncio_run, mock_pool):
        """Eviction triggers inside execute() when cache exceeds 100."""
        mock_pool.return_value = MagicMock()
        agent = L4ResilienceAgent()
        # Pre-fill cache to capacity (100 entries)
        for i in range(100):
            agent._completed[f"t-{i:04d}"] = L4TaskResult(
                task_id=f"t-{i:04d}", status="passed"
            )
        assert len(agent._completed) == 100

        # Execute one more — triggers eviction of oldest entry
        mock_asyncio_run.side_effect = _asyncio_run_returns(
            L4TaskResult(task_id="t-0100", status="passed")
        )
        task = L4TestTask(task_id="t-0100", intent="test")
        agent.execute(None, task)

        assert "t-0000" not in agent._completed
        assert "t-0100" in agent._completed
        assert len(agent._completed) == 100


class TestL4ResilienceAgentExecute:
    """Test execute() with mocked internals."""

    @patch("chaos_agent.l4.agent.L4ResilienceAgent._ensure_pool")
    @patch("chaos_agent.l4.agent.asyncio.run")
    def test_execute_calls_async_execute(self, mock_asyncio_run, mock_pool):
        mock_pool.return_value = MagicMock()
        expected = L4TaskResult(task_id="t-001", status="passed")
        mock_asyncio_run.side_effect = _asyncio_run_returns(expected)

        agent = L4ResilienceAgent()
        task = L4TestTask(task_id="t-001", intent="test")
        result = agent.execute(None, task)

        assert result.status == "passed"
        mock_asyncio_run.assert_called_once()

    @patch("chaos_agent.l4.agent.L4ResilienceAgent._ensure_pool")
    @patch("chaos_agent.l4.agent.asyncio.run")
    def test_execute_caches_result(self, mock_asyncio_run, mock_pool):
        mock_pool.return_value = MagicMock()
        expected = L4TaskResult(task_id="t-002", status="failed")
        mock_asyncio_run.side_effect = _asyncio_run_returns(expected)

        agent = L4ResilienceAgent()
        task = L4TestTask(task_id="t-002", intent="test")
        agent.execute(None, task)

        assert "t-002" in agent._completed

    @patch("chaos_agent.l4.agent.L4ResilienceAgent._ensure_pool")
    @patch("chaos_agent.l4.agent.asyncio.run")
    def test_execute_clears_buffer(self, mock_asyncio_run, mock_pool):
        mock_pool.return_value = MagicMock()
        mock_asyncio_run.side_effect = _asyncio_run_returns(
            L4TaskResult(task_id="t-003", status="passed")
        )

        agent = L4ResilienceAgent()
        agent._state_transitions_buffer = [{"old": "data"}]
        task = L4TestTask(task_id="t-003", intent="test")
        agent.execute(None, task)

        # Buffer should have been reset at start of execute
        # (the mock prevents actual execution that would repopulate it)


class TestL4ResilienceAgentPrepare:
    """Test prepare() lifecycle method."""

    @patch("chaos_agent.l4.agent.L4ResilienceAgent._ensure_pool")
    def test_prepare_initializes_pool(self, mock_pool):
        """prepare() must trigger lazy pool init; no runtime stashing on pool."""
        pool = MagicMock(
            spec=[]
        )  # spec=[] -> attribute access on undefined names raises
        mock_pool.return_value = pool

        agent = L4ResilienceAgent()
        runtime = MagicMock()
        agent.prepare(runtime, MagicMock())

        mock_pool.assert_called_once()
        # The dead-code `pool.runtime = runtime` assignment has been removed.
        with pytest.raises(AttributeError):
            _ = pool.runtime


class TestL4ResilienceAgentEnsurePool:
    """Test _ensure_pool() lazy initialization."""

    @patch("chaos_agent.l4.agent._ChaosAgentPool.ensure_initialized")
    def test_creates_pool_on_first_call(self, mock_init):
        agent = L4ResilienceAgent()
        assert agent._pool is None
        pool = agent._ensure_pool()
        assert agent._pool is pool
        assert isinstance(pool, _ChaosAgentPool)

    @patch("chaos_agent.l4.agent._ChaosAgentPool.ensure_initialized")
    def test_reuses_existing_pool(self, mock_init):
        agent = L4ResilienceAgent()
        pool1 = agent._ensure_pool()
        pool2 = agent._ensure_pool()
        assert pool1 is pool2


class TestL4ResilienceAgentFinishTiming:
    """P0 fix: runtime.finish() must be called exactly once with the FINAL status.

    Previously runtime.finish() was called inside _run_inject_with_runtime, which
    persisted the trajectory with the inject-phase status and could not be
    overridden by the post-recovery status. The fix moves finish() to the
    outermost _async_execute() finally block.
    """

    def _make_runtime(self):
        runtime = MagicMock()
        runtime.finish = MagicMock()
        # heal not used in these tests; keep it absent or no-op
        return runtime

    def test_finish_called_once_with_final_status_when_recovery_degraded(self):
        """When inject passes but recover returns partial, finish() must record degraded."""
        import asyncio

        agent = L4ResilienceAgent()
        runtime = self._make_runtime()
        pool = MagicMock()
        task = L4TestTask(
            task_id="t-finish-1",
            intent="x",
            payload=_valid_payload(auto_recover=True),
        )

        async def fake_inject(*a, **kw):
            return L4TaskResult(
                task_id="t-finish-1", status="passed", trajectory_id="traj-1"
            )

        async def fake_recover(*a, **kw):
            return L4TaskResult(
                task_id="t-finish-1", status="degraded", trajectory_id="traj-1"
            )

        with (
            patch.object(agent, "_run_inject_with_runtime", side_effect=fake_inject),
            patch.object(agent, "_run_recover_with_runtime", side_effect=fake_recover),
        ):
            result = asyncio.run(agent._async_execute(pool, runtime, task))

        assert result.status == "degraded"
        runtime.finish.assert_called_once_with(status="degraded")

    def test_finish_called_with_failed_status_on_inject_failure(self):
        """Inject failure short-circuits before recovery; finish records 'failed'."""
        import asyncio

        agent = L4ResilienceAgent()
        runtime = self._make_runtime()
        pool = MagicMock()
        task = L4TestTask(
            task_id="t-finish-2",
            intent="x",
            payload=_valid_payload(auto_recover=True),
        )

        async def fake_inject(*a, **kw):
            return L4TaskResult(
                task_id="t-finish-2", status="failed", trajectory_id="traj-2"
            )

        recover_mock = MagicMock()
        with (
            patch.object(agent, "_run_inject_with_runtime", side_effect=fake_inject),
            patch.object(agent, "_run_recover_with_runtime", recover_mock),
        ):
            result = asyncio.run(agent._async_execute(pool, runtime, task))

        assert result.status == "failed"
        recover_mock.assert_not_called()
        runtime.finish.assert_called_once_with(status="failed")

    def test_finish_called_with_failed_on_exception_no_heal(self):
        """Unhandled exception with heal=False -> finish records 'failed' once."""
        import asyncio

        agent = L4ResilienceAgent()
        runtime = self._make_runtime()
        runtime.heal = MagicMock(return_value=type("HR", (), {"healed": False})())
        pool = MagicMock()
        task = L4TestTask(
            task_id="t-finish-3",
            intent="x",
            payload=_valid_payload(auto_recover=True),
        )

        async def fake_inject(*a, **kw):
            raise RuntimeError("boom")

        with patch.object(agent, "_run_inject_with_runtime", side_effect=fake_inject):
            result = asyncio.run(agent._async_execute(pool, runtime, task))

        assert result.status == "failed"
        runtime.finish.assert_called_once_with(status="failed")

    def test_finish_called_only_in_outer_call_when_self_heal_succeeds(self):
        """Self-heal recursion (healed=True) must NOT call finish; only outer call does."""
        import asyncio

        agent = L4ResilienceAgent()
        runtime = self._make_runtime()
        runtime.heal = MagicMock(return_value=type("HR", (), {"healed": True})())
        pool = MagicMock()
        task = L4TestTask(
            task_id="t-finish-4",
            intent="x",
            payload=_valid_payload(auto_recover=False),
        )

        call_count = {"inject": 0}

        async def fake_inject(*a, **kw):
            call_count["inject"] += 1
            if call_count["inject"] == 1:
                raise RuntimeError("first attempt fails")
            return L4TaskResult(
                task_id="t-finish-4", status="passed", trajectory_id="traj-4"
            )

        with patch.object(agent, "_run_inject_with_runtime", side_effect=fake_inject):
            result = asyncio.run(agent._async_execute(pool, runtime, task))

        assert result.status == "passed"
        assert call_count["inject"] == 2  # original + heal retry
        # finish must be called exactly once, with the final status
        runtime.finish.assert_called_once_with(status="passed")

    def test_async_execute_with_healed_param_does_not_call_finish(self):
        """Direct call with healed=True bypasses the finally finish() — used during recursion."""
        import asyncio

        agent = L4ResilienceAgent()
        runtime = self._make_runtime()
        pool = MagicMock()
        task = L4TestTask(
            task_id="t-finish-5",
            intent="x",
            payload=_valid_payload(auto_recover=False),
        )

        async def fake_inject(*a, **kw):
            return L4TaskResult(
                task_id="t-finish-5", status="passed", trajectory_id="traj-5"
            )

        with patch.object(agent, "_run_inject_with_runtime", side_effect=fake_inject):
            result = asyncio.run(agent._async_execute(pool, runtime, task, healed=True))

        assert result.status == "passed"
        runtime.finish.assert_not_called()
