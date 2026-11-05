"""Tests for CLI AgentRunner (local execution wrapper)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chaos_agent.cli.runner import AgentRunner

# A run the verifier confirmed: L1 passed + L2 passed → the single-source
# projection resolves the terminal word to "injected" (state.infer_task_state).
# Both the CLI confirm() and the server confirm route read a resumed graph
# through build_inject_data_from_state, so this shape drives a deterministic
# verdict without mocking the projection itself.
_INJECTED_VALUES = {
    "operation": "inject",
    "verification": {
        "level": "verified",
        "layer1": {"status": "passed"},
        "layer2": {"status": "passed"},
    },
}


class TestAgentRunnerInit:
    def test_not_initialized_by_default(self):
        runner = AgentRunner()
        assert runner._initialized is False
        assert runner._registry is None
        assert runner._agents is None


class TestAgentRunnerMetric:
    @pytest.mark.asyncio
    async def test_metric_returns_not_found_for_unknown_task(self):
        runner = AgentRunner()
        result = await runner.metric("nonexistent-task")
        assert result["code"] == 2001
        assert "not found" in result["message"].lower() or "Task not found" in result["message"]

    @pytest.mark.asyncio
    async def test_metric_list_all(self):
        runner = AgentRunner()
        result = await runner.metric()
        assert result["code"] == 0
        assert "data" in result


class TestAgentRunnerVersion:
    @pytest.mark.asyncio
    async def test_version_returns_version_info(self):
        runner = AgentRunner()
        runner._initialized = True
        runner._registry = MagicMock()
        runner._registry.__len__ = MagicMock(return_value=5)

        result = await runner.version()
        assert result["code"] == 0
        assert "version" in result["data"]


class TestAgentRunnerListSkills:
    @pytest.mark.asyncio
    async def test_list_skills_returns_categories(self):
        runner = AgentRunner()
        runner._initialized = True

        # Create a mock registry
        from chaos_agent.skills.models import SkillMetadata, SkillParameter
        mock_registry = MagicMock()
        mock_meta = SkillMetadata(
            name="test-skill",
            description="A test skill for unit testing",
            version="1.0",
            category="test",
            target="pod",
            required_tools=["blade", "kubectl"],
            tags=["test"],
            parameters=[
                SkillParameter(
                    name="time",
                    type="int",
                    required=True,
                    description="Delay in milliseconds",
                    example="3000",
                )
            ],
        )
        mock_registry.metadata = {"test-skill": mock_meta}
        mock_registry.__len__ = MagicMock(return_value=1)
        mock_registry.activate = MagicMock(return_value="skill content for testing")
        runner._registry = mock_registry

        # Mock generate_skill_catalog to return a use case
        with patch("chaos_agent.cli.runner.generate_skill_catalog", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = [{
                "category": "Pod_cpu使用率过高",
                "use_case_name": "Pod CPU high",
                "fault_symptom": "CPU fullload",
                "resource_path": "references/catalogue/Pod_cpu使用率过高/Pod_cpu使用率过高_CPU满载.md",
                "example_cmd": 'blade-ai inject -i "帮我注入CPU故障..."',
            }]
            # Stub ``factory.make_llm`` (its definition site — runner
            # imports it locally), NOT ``langchain_openai.ChatOpenAI``.
            #
            # ``make_llm`` lazily imports ``resilient_llm``, whose module body
            # runs ``class ResilientChatOpenAI(ChatOpenAI)``. Patching
            # ``langchain_openai.ChatOpenAI`` while that first import happens
            # makes the subclass inherit from a MagicMock — and because a
            # module body executes only once, the poisoned class stays in
            # ``sys.modules`` after the patch exits, so every later
            # ``make_llm()`` in the session returns a mock whose auto-built
            # side_effect iterator eventually raises StopIteration.
            with patch("chaos_agent.agent.factory.make_llm"):
                result = await runner.list_skills()

        assert result["code"] == 0
        assert result["data"]["total"] == 1
        assert len(result["data"]["categories"]) >= 1

    @pytest.mark.asyncio
    async def test_list_skills_with_category_filter(self):
        runner = AgentRunner()
        runner._initialized = True

        from chaos_agent.skills.models import SkillMetadata
        mock_registry = MagicMock()
        mock_meta = SkillMetadata(
            name="test-skill",
            description="A test skill",
            version="1.0",
            category="network",
            target="pod",
            required_tools=["blade"],
            tags=["test"],
            parameters=[],
        )
        mock_registry.metadata = {"test-skill": mock_meta}
        mock_registry.__len__ = MagicMock(return_value=1)
        mock_registry.activate = MagicMock(return_value="skill content")
        runner._registry = mock_registry

        with patch("chaos_agent.cli.runner.generate_skill_catalog", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = []
            # See the note in test_list_skills_returns_categories: patching
            # ``langchain_openai.ChatOpenAI`` here would permanently poison
            # ``resilient_llm`` in ``sys.modules``.
            with patch("chaos_agent.agent.factory.make_llm"):
                result = await runner.list_skills(category="network")

        assert result["code"] == 0


class TestAgentRunnerConfirm:
    @pytest.mark.asyncio
    async def test_confirm_invalid_action(self):
        runner = AgentRunner()
        runner._initialized = True

        result = await runner.confirm("task-123", "invalid_action")
        assert result["code"] == 1001
        assert "invalid" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_confirm_returns_final_task_state(self):
        """Connected defect 2 (round-64): confirm()'s docstring promises "the
        returned task_state reflects the final state", but the envelope used
        to carry only {task_id, action, reason, confirmed_at} — so the CLI's
        two-phase confirm (which replaces ``result`` with this envelope)
        printed a bare "approved" with no verdict, and the user could not tell
        an injected drill from a rejected one. The resumed run must be read
        through the SAME single-source projection every terminal surface uses
        and its word must ride the ack.
        """

        class _ResumeGraph:
            async def ainvoke(self, command, config):
                # resume ran the pipeline to its own verdict.
                return dict(_INJECTED_VALUES)

            async def aget_state(self, config):
                # Terminal: the graph is no longer parked at the gate.
                return SimpleNamespace(
                    values=dict(_INJECTED_VALUES), next=(), tasks=[],
                )

        runner = AgentRunner()
        runner._initialized = True
        runner._agents = {"pipeline": _ResumeGraph()}

        result = await runner.confirm("task-approve", "approve")
        assert result["code"] == 0
        data = result["data"]
        # The whole point: the verdict rides the confirm ack.
        assert data["task_state"] == "injected"
        assert data["result"] == "injected"
        assert data["action"] == "approve"


class TestListInterruptedTasks:
    """Connected defect 1 (round-64): crash-recovery discovery must see a run
    parked at the confirmation gate.

    ``list_interrupted_tasks`` used to source candidates ONLY from
    ``query_active()``, which keys off the materialised liability column
    (round-32) — a COMMITTED fault awaiting recovery. A run paused at
    ``confirmation_gate`` has committed nothing, carries no liability, and was
    therefore invisible, blinding the TUI startup scan to exactly the tasks its
    docstring promises ("paused at interrupt points, waiting for user input").
    The fix unions in ``list_tasks(task_state="waiting_input")`` (a "find
    resumable pauses" query, NOT the round-32 liability predicate) while the
    per-task ``state.next`` check stays the authority on whether a candidate
    is really paused.
    """

    @pytest.mark.asyncio
    async def test_discovers_paused_row_with_no_liability(self):
        store = MagicMock()
        # The paused row is ONLY in the waiting_input set; query_active (the
        # old sole source) returns nothing — reproduces the pre-fix blind spot.
        store.list_tasks = AsyncMock(return_value=[{"task_id": "task-paused"}])
        store.query_active = AsyncMock(return_value=[])

        class _Graph:
            async def aget_state(self, config):
                intr = SimpleNamespace(value={"plan_summary": "kill pod X"})
                task = SimpleNamespace(interrupts=[intr])
                return SimpleNamespace(
                    next=("confirmation_gate",), tasks=[task], values={},
                )

        runner = AgentRunner()
        runner._initialized = True
        runner._agents = {"pipeline": _Graph()}

        with patch(
            "chaos_agent.persistence.task_store.get_task_store",
            new=AsyncMock(return_value=store),
        ):
            result = await runner.list_interrupted_tasks()

        assert [t["task_id"] for t in result] == ["task-paused"]
        assert result[0]["next_nodes"] == ["confirmation_gate"]
        assert result[0]["interrupt_info"] == {"plan_summary": "kill pod X"}

    @pytest.mark.asyncio
    async def test_union_dedups_and_state_next_is_authority(self):
        store = MagicMock()
        store.list_tasks = AsyncMock(return_value=[{"task_id": "task-paused"}])
        # task-paused appears in BOTH sources (dedup); task-liability is a
        # committed-fault row the engine does NOT report as paused (next empty),
        # so it must be filtered — the union widens discovery without letting a
        # non-paused row through.
        store.query_active = AsyncMock(return_value=[
            {"task_id": "task-paused"},
            {"task_id": "task-liability"},
        ])

        class _Graph:
            async def aget_state(self, config):
                tid = config["configurable"]["thread_id"]
                if tid == "task-paused":
                    intr = SimpleNamespace(value={"plan_summary": "kill pod X"})
                    return SimpleNamespace(
                        next=("confirmation_gate",),
                        tasks=[SimpleNamespace(interrupts=[intr])],
                        values={},
                    )
                return SimpleNamespace(next=(), tasks=[], values={})

        runner = AgentRunner()
        runner._initialized = True
        runner._agents = {"pipeline": _Graph()}

        with patch(
            "chaos_agent.persistence.task_store.get_task_store",
            new=AsyncMock(return_value=store),
        ):
            result = await runner.list_interrupted_tasks()

        assert [t["task_id"] for t in result] == ["task-paused"]


class TestResumeStreamCleanupContract:
    """resume_stream's finally must clean up without raising (Bug R1).

    ``unsubscribe`` requires ``(task_id, queue)``; the old single-argument
    call raised TypeError in the generator's finally, which propagated to
    the TUI consumer as a spurious "Resume failed" after a successful
    resume and skipped ``printer_task.cancel()`` / ``remove_tracker``
    (tracker leak).
    """

    @pytest.mark.asyncio
    async def test_resume_stream_exhausts_without_cleanup_error(self):
        import asyncio
        from uuid import uuid4

        from chaos_agent.observability import status_tracker as st

        task_id = f"task-{uuid4()}"

        class _PausedState:
            next = ("confirmation_gate",)
            tasks = []
            values = {}

        class _FakeGraph:
            async def aget_state(self, config):
                return _PausedState()

            async def astream_events(self, *args, **kwargs):
                if False:  # pragma: no cover - makes this an async generator
                    yield

        runner = AgentRunner()
        runner._initialized = True
        runner._agents = {"pipeline": _FakeGraph()}

        events = []
        # Must exhaust the generator without the finally raising.
        async for evt in runner.resume_stream(task_id, resume_value="approved"):
            events.append(evt)

        # Give the cancelled printer task a chance to settle.
        await asyncio.sleep(0)
        # The whole finally ran: remove_tracker (its last statement) popped
        # the tracker, proving unsubscribe did not raise mid-cleanup.
        assert task_id not in st._trackers

    @pytest.mark.asyncio
    async def test_resume_stream_unsubscribes_the_status_queue(self):
        from uuid import uuid4

        from chaos_agent.observability import status_tracker as st

        task_id = f"task-{uuid4()}"

        class _PausedState:
            next = ("confirmation_gate",)
            tasks = []
            values = {}

        class _FakeGraph:
            async def aget_state(self, config):
                return _PausedState()

            async def astream_events(self, *args, **kwargs):
                if False:  # pragma: no cover
                    yield

        runner = AgentRunner()
        runner._initialized = True
        runner._agents = {"pipeline": _FakeGraph()}

        tracker = st.get_tracker(task_id)
        async for _ in runner.resume_stream(task_id, resume_value=None):
            pass
        # The queue subscribed by resume_stream was removed from the
        # tracker (unsubscribe received the queue, not just the task id).
        assert tracker._subscribers == []


class TestInjectStreamCrashOwedTeardown:
    """W-78R-1: a mid-run uncaught exception must still surface the owed
    vehicle teardown on the crash envelope + render the CLI recover hint.

    The crash arm used to build the terminal envelope off ``initial_state``
    — the pre-run projection, which by definition carries no carrier armed
    DURING the run. So an elevated-RBAC recovery vehicle orphaned by an
    httpx.ReadError-class crash was recorded in the audit row but never
    surfaced to the operator, who was never told to run ``blade-ai recover``.

    These tests drive the REAL ``inject_stream`` end to end; the only seam is
    the fake pipeline graph the whole suite injects via ``runner._agents``
    (r47: prove the mechanism actually fires, don't mock the integration
    boundary it lives on).
    """

    @staticmethod
    def _crash_graph(final_values):
        class _Snap:
            values = final_values
            next = ()
            tasks = []

        class _Graph:
            async def aget_state(self, config):
                return _Snap()

            async def astream_events(self, *args, **kwargs):
                # Simulate the uncaught transport exception that terminates
                # the run mid-superstep (the ReadError this gap shipped as).
                raise RuntimeError("ReadError: connection reset by peer")
                if False:  # pragma: no cover - makes this an async generator
                    yield

        return _Graph()

    @staticmethod
    async def _drive(runner):
        events = []
        async for evt in runner.inject_stream(
            scope="node",
            target="disk",
            action="fill",
            target_name="node-1",
            namespace="cms-demo",
        ):
            events.append(evt)
        return events

    @pytest.mark.asyncio
    async def test_crash_arm_surfaces_owed_teardown_and_hint(self):
        import json

        carrier_values = {
            "operation": "inject",
            "execution_artifacts": [
                {
                    "type": "recovery_carrier",
                    "name": "carrier-abc",
                    "namespace": "cms-demo",
                    "status": "recovery_armed",
                },
            ],
        }
        runner = AgentRunner()
        runner._initialized = True
        runner._session_store = None  # set by _ensure_initialized, bypassed here
        runner._agents = {"pipeline": self._crash_graph(carrier_values)}

        events = await self._drive(runner)

        # The crash result envelope carries the owed vehicle — read off the
        # FINAL checkpoint, NOT initial_state (which has no carrier). This is
        # the assertion with teeth: revert the arm to initial_state and the
        # ledger reads empty here.
        result_evts = [e for e in events if e.type == "result"]
        assert result_evts, "crash arm must yield a terminal result event"
        data = json.loads(result_evts[-1].content)["data"]
        assert data["vehicle_teardown_pending"] == [
            "recovery_carrier:cms-demo/carrier-abc"
        ]

        # And the CLI hint is rendered, symmetric with the success path
        # (_build_inject_result_events) — the operator watching a crashed
        # inject is told to collect the carrier stack.
        assert [
            e
            for e in events
            if e.type == "token" and "blade-ai recover" in (e.content or "")
        ], "crash arm must render the vehicle-teardown hint"

    @pytest.mark.asyncio
    async def test_crash_arm_no_hint_when_no_carrier_owed(self):
        import json

        # Reverse proposition: a crash with NO carrier in the final state
        # rides an empty ledger and renders NO hint. The fix surfaces owed
        # teardown; it does not invent it (no false `blade-ai recover` cry on
        # a task that built no vehicle).
        runner = AgentRunner()
        runner._initialized = True
        runner._session_store = None  # set by _ensure_initialized, bypassed here
        runner._agents = {"pipeline": self._crash_graph({"operation": "inject"})}

        events = await self._drive(runner)

        result_evts = [e for e in events if e.type == "result"]
        assert result_evts
        data = json.loads(result_evts[-1].content)["data"]
        assert data["vehicle_teardown_pending"] == []
        assert not [
            e
            for e in events
            if e.type == "token" and "blade-ai recover" in (e.content or "")
        ]
