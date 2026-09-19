from pathlib import Path
from types import SimpleNamespace

import pytest

from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.memory.session_finalizer import (
    RESULT_SUMMARY_DATA_ENVELOPE,
    RESULT_SUMMARY_INJECT_ENVELOPE,
    RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
    RESULT_SUMMARY_RECOVER_PAYLOAD,
    RESULT_SUMMARY_STATUS_ENVELOPE,
    build_inject_session_summary,
    build_recover_session_summary,
    finalize_inject_session,
    finalize_recover_session,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _inject_values() -> dict:
    spec = FaultSpec(
        namespace="arms-prom",
        scope="pod",
        names=("pod-a",),
        fault_target="cpu",
        fault_action="fullload",
        params={"cpu-percent": "80"},
    )
    return {
        "fault_spec": spec.to_dict(),
        "experiment_uid": "uid-1",
        "result": {"success": True},
        "verification": {
            "level": "verified",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
        },
    }


def _recover_values() -> dict:
    return {
        "operation": "recover",
        "result": {"recovered": True, "recovery_level": "recovered"},
        "recover_verification": {
            "level": "recovered",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
        },
        "verification": {
            "level": "stale-inject",
            "layer1": {"status": "failed"},
            "layer2": {"status": "failed"},
        },
    }


class _Graph:
    def __init__(self, values: dict):
        self.values = values

    async def aget_state(self, config):
        return SimpleNamespace(values=self.values)


class _SessionStore:
    def __init__(self):
        self.finalized = None
        self.appended = None

    def finalize_session(self, task_id, **kwargs):
        self.finalized = {"task_id": task_id, **kwargs}

    def append_messages(self, task_id, messages):
        self.appended = {"task_id": task_id, "messages": messages}


def test_status_summary_preserves_legacy_server_inject_shape():
    data = {
        "task_id": "task-1",
        "task_state": "injected",
        "fault_type": "pod-cpu-fullload",
        "experiment_uid": "uid-1",
        "fault_spec": _inject_values()["fault_spec"],
        "target": {"namespace": "arms-prom", "names": ["pod-a"]},
        "verification": {"level": "verified"},
        "error": "",
    }

    summary = build_inject_session_summary(
        data,
        mode=RESULT_SUMMARY_STATUS_ENVELOPE,
    )

    assert summary["status"] == "success"
    assert summary["data"] == {
        "task_id": "task-1",
        "result": "injected",
        "fault_type": "pod-cpu-fullload",
        # Legacy-spelling input pins the hydrate path — the output keeps
        # the modern key only (phase-10 key-face normalisation).
        "experiment_uid": "uid-1",
        "fault_spec": _inject_values()["fault_spec"],
        "targets": [{"name": "pod-a", "namespace": "arms-prom"}],
        "verification": {"level": "verified"},
        "error": "",
    }


def test_data_summary_preserves_stream_session_shape():
    data = {"task_id": "task-1", "task_state": "injected"}

    summary = build_inject_session_summary(data, mode=RESULT_SUMMARY_DATA_ENVELOPE)

    assert summary["status"] == "success"
    assert summary["data"] == data


def test_inject_summary_uses_failure_envelope_semantics():
    data = {"task_id": "task-1", "task_state": "failed", "error": "boom"}

    summary = build_inject_session_summary(data, mode=RESULT_SUMMARY_INJECT_ENVELOPE)

    assert summary["status"] == "fail"
    assert summary["data"] == data
    assert summary["message"] == "boom"


def test_recover_payload_summary_preserves_server_payload_shape():
    payload = {
        "status": "success",
        "data": {"task_id": "task-recover", "task_state": "recovered"},
    }

    summary = build_recover_session_summary(
        {},
        recover_task_id="task-recover",
        inject_task_id="task-inject",
        inject_state_values=_inject_values(),
        result_payload=payload,
        mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
    )

    assert summary is payload


def test_recover_payload_summary_without_payload_stays_empty():
    summary = build_recover_session_summary(
        _recover_values(),
        recover_task_id="task-recover",
        inject_task_id="task-inject",
        inject_state_values=_inject_values(),
        mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
    )

    assert summary == ""


def test_recover_cli_summary_uses_recover_verification_not_inject_verification():
    summary = build_recover_session_summary(
        _recover_values(),
        recover_task_id="task-recover",
        inject_task_id="task-inject",
        inject_state_values=_inject_values(),
        mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
    )

    assert summary["status"] == "success"
    assert summary["data"]["result"] == "recovered"
    assert summary["data"]["verification"]["level"] == "recovered"
    assert summary["data"]["verification"]["layer1"] == {"status": "passed"}


def test_recover_cli_summary_empty_values_spells_unverified_not_recovered():
    """Round-16 S2: empty recover values (aget_state failure — swallowed
    by finalize_recover_session's ``except Exception: pass`` — or a
    genuinely empty final state) must NOT fabricate "recovered".
    "unverified" is the honest spelling of "no evidence" (D4/D5
    honest-ignorance family): the session still closes with an ok
    envelope, but the persisted result never claims a verdict it cannot
    prove."""
    summary = build_recover_session_summary(
        {},
        recover_task_id="task-recover",
        inject_task_id="task-inject",
        inject_state_values=_inject_values(),
        mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
    )

    assert summary["status"] == "success"
    assert summary["data"]["result"] == "unverified"


@pytest.mark.asyncio
async def test_finalize_inject_session_reads_graph_and_flushes_summary():
    store = _SessionStore()

    await finalize_inject_session(
        store,
        _Graph(_inject_values()),
        {"configurable": {"thread_id": "task-1"}},
        "task-1",
        result_summary_mode=RESULT_SUMMARY_STATUS_ENVELOPE,
    )

    assert store.finalized["task_id"] == "task-1"
    assert store.finalized["status"] == "completed"
    assert store.finalized["remaining_messages"] == []
    assert store.finalized["result_summary"]["data"]["result"] == "injected"
    assert store.finalized["result_summary"]["data"]["fault_spec"] == (
        _inject_values()["fault_spec"]
    )
    assert store.finalized["result_summary"]["data"]["targets"] == [
        {"name": "pod-a", "namespace": "arms-prom"}
    ]


@pytest.mark.asyncio
async def test_finalize_failed_inject_persists_failed_status_and_summary():
    store = _SessionStore()
    values = {
        **_inject_values(),
        "experiment_uid": "",
        "verification": None,
        "error": "execution_failed: iptables not found",
        "failure_reason": "execution_failed: iptables not found",
    }

    await finalize_inject_session(
        store,
        graph_or_agent=None,
        config=None,
        session_id="task-failed",
        precomputed_values=values,
    )

    assert store.finalized["status"] == "failed"
    assert store.finalized["result_summary"]["data"]["task_state"] == "failed"
    assert "iptables not found" in store.finalized["result_summary"]["data"]["error"]


@pytest.mark.asyncio
async def test_finalize_cancelled_inject_keeps_summary_but_marks_cancelled():
    store = _SessionStore()
    values = {**_inject_values(), "experiment_uid": "", "verification": None}

    await finalize_inject_session(
        store,
        graph_or_agent=None,
        config=None,
        session_id="task-cancelled",
        precomputed_values=values,
        status_override="cancelled",
    )

    assert store.finalized["status"] == "cancelled"
    assert store.finalized["result_summary"] is not None


@pytest.mark.asyncio
async def test_finalize_recover_session_uses_payload_status_for_server_mode():
    store = _SessionStore()
    payload = {
        "status": "success",
        "data": {"task_id": "task-recover", "task_state": "failed"},
    }

    await finalize_recover_session(
        store,
        _Graph(_recover_values()),
        {"configurable": {"thread_id": "task-recover"}},
        "task-recover",
        "task-inject",
        _inject_values(),
        result_payload=payload,
        result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
    )

    assert store.finalized["task_id"] == "task-recover"
    assert store.finalized["status"] == "failed"
    assert store.finalized["result_summary"] is payload


@pytest.mark.asyncio
async def test_finalize_recover_session_honors_failed_payload_envelope():
    store = _SessionStore()
    payload = {
        "status": "fail",
        "data": {"task_id": "task-recover"},
    }

    await finalize_recover_session(
        store,
        _Graph(_recover_values()),
        {"configurable": {"thread_id": "task-recover"}},
        "task-recover",
        "task-inject",
        _inject_values(),
        result_payload=payload,
        result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
    )

    assert store.finalized["status"] == "failed"
    assert store.finalized["result_summary"] is payload


@pytest.mark.asyncio
async def test_finalize_recover_failed_fallback_does_not_write_success_summary():
    store = _SessionStore()

    await finalize_recover_session(
        store,
        _Graph({"operation": "recover"}),
        {"configurable": {"thread_id": "task-recover"}},
        "task-recover",
        "task-inject",
        _inject_values(),
        result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
        default_status="failed",
    )

    assert store.finalized["status"] == "failed"
    assert store.finalized["result_summary"] == ""


@pytest.mark.asyncio
async def test_finalize_recover_default_yields_to_reached_verdict():
    """Round-63 P8': the recover-side counterpart of the inject surface's
    round-62 P8 gate. The classified default_status classifies runs still
    MID-FLIGHT only. A recover graph carrying its own verdict
    (recover_verification on record) keeps it even when the caller passes
    the abort word unconditionally — the G5 fallback and the
    recover-stream disconnect arm both do, and an interrupt racing in
    during result extraction used to rewrite "recovered" to
    "cancelled"/"failed" while the row's skip_if_terminal kept the row's
    verdict: the r55 F2 word split, recover edition."""
    store = _SessionStore()

    await finalize_recover_session(
        store,
        _Graph(_recover_values()),
        {"configurable": {"thread_id": "task-recover-verdict"}},
        "task-recover-verdict",
        "task-inject",
        _inject_values(),
        result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
        default_status="cancelled",
    )

    assert store.finalized["status"] == "completed"


@pytest.mark.asyncio
async def test_finalize_recover_default_lands_when_run_is_midflight():
    """The gate must not swallow the legitimate default: a mid-flight
    recover ("recovering" — no verdict on record) keeps the abort word —
    the interrupt is a KNOWN terminal fact, not something inference can
    derive."""
    store = _SessionStore()

    await finalize_recover_session(
        store,
        _Graph({"operation": "recover"}),
        {"configurable": {"thread_id": "task-recover-midflight"}},
        "task-recover-midflight",
        "task-inject",
        _inject_values(),
        result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
        default_status="cancelled",
    )

    assert store.finalized["status"] == "cancelled"


@pytest.mark.asyncio
async def test_finalize_recover_default_lands_on_empty_values():
    """Empty values (aget_state failed) do NOT suppress the abort word:
    the interrupt itself is a known terminal fact and with no state on
    record there is no evidence any verdict was reached (round-53
    ruling, mirrored from the inject surface's gate)."""
    store = _SessionStore()

    await finalize_recover_session(
        store,
        _Graph({}),
        {"configurable": {"thread_id": "task-recover-empty"}},
        "task-recover-empty",
        "task-inject",
        _inject_values(),
        result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
        default_status="cancelled",
    )

    assert store.finalized["status"] == "cancelled"


@pytest.mark.asyncio
async def test_finalize_recover_default_verdict_polarity_matches_payload_path():
    """The verdict-derived word follows the payload path's own polarity:
    only "failed" lands failed; unverified and partial_recovered are
    session-level completions (same mapping the turn stream's defensive
    finalize applies). A verdict of "failed" wins over the abort word —
    the run's own recorded failure is the stronger fact."""
    unverified = {
        "operation": "recover",
        "recover_verification": {"level": "unverified"},
    }
    partial = {
        "operation": "recover",
        "result": {"recovered": True},
        "recover_verification": {"level": "partial"},
    }
    failed = {
        "operation": "recover",
        "recover_verification": {
            "level": "failed",
            "layer1": {"status": "failed"},
        },
    }

    for name, values, expected in (
        ("unverified", unverified, "completed"),
        ("partial", partial, "completed"),
        ("failed", failed, "failed"),
    ):
        store = _SessionStore()
        await finalize_recover_session(
            store,
            _Graph(values),
            {"configurable": {"thread_id": f"task-recover-{name}"}},
            f"task-recover-{name}",
            "task-inject",
            _inject_values(),
            result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
            default_status="cancelled",
        )
        assert store.finalized["status"] == expected, name


@pytest.mark.asyncio
async def test_finalize_recover_cli_envelope_default_lands_on_midflight():
    """Round-63 R63-1: the CLI envelope branch used to hard-code
    "completed" regardless of the run's outcome — a recovery that FAILED
    (RECOVERY_FAILED return or the except in cli/runner.py) recorded
    "completed" on its session while the same record's data.result said
    failed/unverified. The branch now consumes default_status, and the
    verdict gate above spans EVERY result_summary_mode. The caller
    classifies which exit ran (the runner passes "failed" from its
    failure exits); a mid-flight state (no verdict on record) keeps the
    caller's word."""
    store = _SessionStore()

    await finalize_recover_session(
        store,
        _Graph({"operation": "recover"}),
        {"configurable": {"thread_id": "task-recover-cli-midflight"}},
        "task-recover-cli-midflight",
        "task-inject",
        _inject_values(),
        result_summary_mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
        default_status="failed",
    )

    assert store.finalized["status"] == "failed"


@pytest.mark.asyncio
async def test_finalize_recover_cli_envelope_default_yields_to_reached_verdict():
    """The CLI envelope is NOT exempt from the verdict gate: a verdict
    reached before the crash keeps its derived word even when the
    runner's failure exit passes "failed" — otherwise the gate's
    guarantee would be mode-dependent and the CLI's exception exit
    (which fires after the graph already recorded "recovered") would
    rewrite it."""
    store = _SessionStore()

    await finalize_recover_session(
        store,
        _Graph(_recover_values()),
        {"configurable": {"thread_id": "task-recover-cli-verdict"}},
        "task-recover-cli-verdict",
        "task-inject",
        _inject_values(),
        result_summary_mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
        default_status="failed",
    )

    assert store.finalized["status"] == "completed"


@pytest.mark.asyncio
async def test_finalize_recover_cli_envelope_default_lands_on_empty_values():
    """Empty values (aget_state failure swallowed by the finalize's
    except) do NOT suppress the CLI caller's classified word — the
    failure exit is a known terminal fact (round-53 ruling) and with no
    state on record there is no evidence any verdict was reached."""
    store = _SessionStore()

    await finalize_recover_session(
        store,
        _Graph({}),
        {"configurable": {"thread_id": "task-recover-cli-empty"}},
        "task-recover-cli-empty",
        "task-inject",
        _inject_values(),
        result_summary_mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
        default_status="failed",
    )

    assert store.finalized["status"] == "failed"


@pytest.mark.asyncio
async def test_finalize_recover_session_uses_precomputed_messages():
    store = _SessionStore()
    message = object()

    await finalize_recover_session(
        store,
        recover_graph=None,
        recover_config=None,
        recover_task_id="task-recover",
        inject_task_id="task-inject",
        inject_state_values=_inject_values(),
        result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
        precomputed_values={**_recover_values(), "messages": [message]},
    )

    assert store.finalized["remaining_messages"] == [message]


@pytest.mark.asyncio
async def test_finalize_recover_session_preserves_cli_completed_status():
    store = _SessionStore()

    await finalize_recover_session(
        store,
        _Graph(_recover_values()),
        {"configurable": {"thread_id": "task-recover"}},
        "task-recover",
        "task-inject",
        _inject_values(),
        result_summary_mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
    )

    assert store.finalized["status"] == "completed"
    assert store.finalized["result_summary"]["data"]["result"] == "recovered"


@pytest.mark.asyncio
async def test_finalize_recover_session_persists_parent_task_id():
    """Problem F: the recover record must link back to its inject task."""
    store = _SessionStore()
    payload = {
        "status": "success",
        "data": {"task_id": "task-recover", "task_state": "recovered"},
    }

    await finalize_recover_session(
        store,
        _Graph(_recover_values()),
        {"configurable": {"thread_id": "task-recover"}},
        "task-recover",
        "task-inject",
        _inject_values(),
        result_payload=payload,
        result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
    )

    # State carries no parent_task_id here -> the inject_task_id argument
    # is the fallback that reaches the store.
    assert store.finalized["parent_task_id"] == "task-inject"


@pytest.mark.asyncio
async def test_finalize_recover_session_prefers_state_parent_task_id():
    store = _SessionStore()
    payload = {
        "status": "success",
        "data": {"task_id": "task-recover", "task_state": "recovered"},
    }

    await finalize_recover_session(
        store,
        _Graph({**_recover_values(), "parent_task_id": "task-inject-durable"}),
        {"configurable": {"thread_id": "task-recover"}},
        "task-recover",
        "task-inject",
        _inject_values(),
        result_payload=payload,
        result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
    )

    assert store.finalized["parent_task_id"] == "task-inject-durable"


def test_server_inject_routes_use_shared_session_finalizer():
    checked_files = [
        PROJECT_ROOT / "src/chaos_agent/server/routes/inject.py",
        PROJECT_ROOT / "src/chaos_agent/server/routes/inject_stream.py",
    ]
    violations = []
    for path in checked_files:
        text = path.read_text(encoding="utf-8")
        if "finalize_inject_session" not in text:
            violations.append(f"{path.name}: missing finalize_inject_session")
        if "session_store.finalize_session(" in text:
            violations.append(f"{path.name}: direct session_store.finalize_session")

    assert violations == []


def test_recover_paths_use_shared_session_finalizer():
    checked_files = [
        PROJECT_ROOT / "src/chaos_agent/cli/runner.py",
        # L4 SDK recover finalization moved from agent.py to recovery.py
        # (_L4RecoveryMixin) in the baseline refactor (b78c82c).
        PROJECT_ROOT / "src/chaos_agent/l4/recovery.py",
        PROJECT_ROOT / "src/chaos_agent/server/routes/recover_stream.py",
        PROJECT_ROOT / "src/chaos_agent/server/routes/turn_event_stream.py",
    ]
    violations = []
    for path in checked_files:
        text = path.read_text(encoding="utf-8")
        if "finalize_recover_session" not in text:
            violations.append(f"{path.name}: missing finalize_recover_session")
        if "def _finalize_recover_session" in text:
            violations.append(f"{path.name}: private _finalize_recover_session")
        if path.name != "runner.py" and "session_store.finalize_session(" in text:
            violations.append(f"{path.name}: direct session_store.finalize_session")
        if path.name == "runner.py" and "self._session_store.finalize_session(" in text:
            violations.append(f"{path.name}: direct self._session_store.finalize_session")

    assert violations == []


def test_task_session_direct_finalize_calls_are_intentional():
    """Direct SessionStore finalization should remain limited to terminal/abort paths."""

    allowed = {
        "src/chaos_agent/agent/nodes/store/memory_nodes.py",
        "src/chaos_agent/memory/session_finalizer.py",
        "src/chaos_agent/server/app.py",
        "src/chaos_agent/server/routes/turn_event_stream.py",
    }
    violations = []
    for path in (PROJECT_ROOT / "src/chaos_agent").rglob("*.py"):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if ".finalize_session(" in text and rel not in allowed:
            violations.append(rel)

    assert violations == []


def test_memory_node_result_summary_uses_session_finalizer_projection():
    text = (
        PROJECT_ROOT / "src/chaos_agent/agent/nodes/store/memory_nodes.py"
    ).read_text(encoding="utf-8")

    assert "build_inject_session_summary" in text
    assert "build_inject_envelope" not in text


class TestInjectSessionStatusUnknownIsNotCompleted:
    """Round-53: ``task_state="unknown"`` must never map to a completed run.

    ``unknown`` is the ABSENCE of a verdict (``build_unknown_inject_data``
    — the graph state could not be read at finalize time, the exact shape
    an interrupted inject leaves behind when its aget_state dies under the
    scope-cancel path). Recording it as "completed" wrote the worst
    unknown as the best known — an interrupted run archived as a
    successful one. Fail-closed, the same rule ``terminal_task_state``
    legislates ("without a verdict the run is failed") and memory_nodes'
    own comment ("never upgrade a run without a verdict to 'completed'").
    """

    def test_unknown_maps_to_failed(self):
        from chaos_agent.memory.session_finalizer import inject_session_status

        assert inject_session_status({"task_state": "unknown"}) == "failed"

    def test_absent_task_state_maps_to_failed(self):
        from chaos_agent.memory.session_finalizer import inject_session_status

        assert inject_session_status({}) == "failed"

    def test_unknown_inject_data_end_to_end(self):
        """The exact record an interrupted inject's empty-state finalize
        builds — end to end through the real builder and mapper."""
        from chaos_agent.agent.result.operation_result import (
            build_unknown_inject_data,
        )
        from chaos_agent.memory.session_finalizer import inject_session_status

        data = build_unknown_inject_data("t-interrupted")
        assert inject_session_status(data) == "failed"

    def test_verdict_states_keep_their_own_mappings(self):
        """Only the no-verdict cell changed: real verdicts keep their
        existing status semantics (injected/unverified → completed, the
        failed family → failed)."""
        from chaos_agent.memory.session_finalizer import inject_session_status

        assert inject_session_status({"task_state": "injected"}) == "completed"
        assert inject_session_status({"task_state": "unverified"}) == "completed"
        assert inject_session_status({"task_state": "failed"}) == "failed"
        assert inject_session_status({"task_state": "rejected"}) == "failed"
        assert inject_session_status({"task_state": "recovered"}) == "completed"


@pytest.mark.asyncio
async def test_finalize_override_yields_to_reached_verdict():
    """Round-62 R62-1/P8: the abort override classifies runs still
    MID-FLIGHT only. A graph state carrying its own verdict (verification
    on record → infer_task_state != "injecting") keeps it, even when the
    caller passes the interrupt word unconditionally — inject_stream's
    flag arm does exactly that, and an interrupt racing in during result
    extraction used to rewrite "injected" to "cancelled" on the session
    while the row kept "injected" (skip_if_terminal): the r55 F2 word
    split, session surface. This is the single-source G6 on the session
    surface, the counterpart of the row's skip_if_terminal guard."""
    store = _SessionStore()

    await finalize_inject_session(
        store,
        graph_or_agent=None,
        config=None,
        session_id="task-verdict-kept",
        precomputed_values=_inject_values(),
        status_override="cancelled",
    )

    assert store.finalized["status"] == "completed"


@pytest.mark.asyncio
async def test_finalize_override_lands_when_run_is_midflight():
    """The guard must not swallow the legitimate override: a mid-flight
    run (no verdict on record) keeps the r54 semantics — the interrupt is
    a KNOWN terminal fact, and "cancelled" is a known terminal state, not
    an inference product."""
    store = _SessionStore()
    values = {**_inject_values(), "experiment_uid": "", "verification": None}

    await finalize_inject_session(
        store,
        graph_or_agent=None,
        config=None,
        session_id="task-midflight",
        precomputed_values=values,
        status_override="cancelled",
    )

    assert store.finalized["status"] == "cancelled"
