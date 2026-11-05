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


def test_recover_payload_summary_without_payload_derives_from_durable_state():
    # a82984a3 root cure: PAYLOAD mode used to persist "" whenever the caller
    # built no payload — which is exactly EVERY crash/abort arm, because
    # build_recover_result_payload only runs on normal completion. "" threw away
    # the durable state (the recover_layer1_cache proof). The card is now derived
    # from values through the SAME single-source renderer the normal path uses
    # (build_recover_data_from_state → Route B floor), so the floor is
    # structurally unavoidable on every PAYLOAD face. A completed recover keeps
    # its real verdict ("recovered"); the floor never downgrades a live verdict.
    summary = build_recover_session_summary(
        _recover_values(),
        recover_task_id="task-recover",
        inject_task_id="task-inject",
        inject_state_values=_inject_values(),
        mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
    )

    assert isinstance(summary, dict)
    assert summary["status"] == "success"
    assert summary["data"]["task_state"] == "recovered"


def test_recover_payload_summary_without_state_stays_empty():
    # round-64 F1 boundary preserved: empty values (aget_state failed) keep the
    # honest "" — no evidence, no upgrade. The floor derives from durable state;
    # with none on record there is nothing honest to spell.
    summary = build_recover_session_summary(
        {},
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


def _paused_inject_values() -> dict:
    """A run parked at the confirmation gate: a plan exists, a confirmation
    is still owed, and NOTHING has committed (no uid, no verification)."""
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
        "needs_confirmation": True,
        "plan_summary": "Inject pod-cpu fullload on pod-a",
    }


class _PausedGraph:
    """A graph snapshot sitting at an interrupt boundary (``next`` non-empty)
    — the engine's authority that the run has paused, not ended."""

    def __init__(self, values: dict, next_nodes=("confirmation_gate",)):
        self.values = values
        self.next = next_nodes

    async def aget_state(self, config):
        return SimpleNamespace(values=self.values, next=self.next)


@pytest.mark.asyncio
async def test_finalize_inject_session_keeps_paused_run_active():
    """Round-64 F3 regression: a run parked at the confirmation gate has NOT
    ended — ``blade-ai confirm`` / ``POST /confirm/{task_id}`` continues it.
    Finalizing must flush nothing terminal and leave the session OPEN for the
    resume path; the pre-fix behavior archived it as a finished failure
    (``inject_session_status`` is fail-closed on a verdict-less projection),
    released the in-memory session, and blinded the turn route's has_active
    defensive arm."""
    store = _SessionStore()

    await finalize_inject_session(
        store,
        _PausedGraph(_paused_inject_values()),
        {"configurable": {"thread_id": "task-paused"}},
        "task-paused",
    )

    # No terminal word written — the session stays active for the resume.
    assert store.finalized is None


@pytest.mark.asyncio
async def test_finalize_inject_session_still_finalizes_terminal_run():
    """Guard against the pause fix over-firing: a graph that is NOT paused
    (``next`` empty) with the same verdict-less values is a finished failure
    and MUST still be finalized fail-closed."""
    store = _SessionStore()

    await finalize_inject_session(
        store,
        _Graph({**_paused_inject_values(), "error": "execution_failed: boom"}),
        {"configurable": {"thread_id": "task-ended"}},
        "task-ended",
    )

    assert store.finalized is not None
    assert store.finalized["status"] == "failed"



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


# ---------------------------------------------------------------------------
# Structural terminal-row chokepoint (the zombie-row root cure).
#
# finalize_inject_session is the single un-bypassable finally EVERY inject
# exit routes through, so it — not each per-path abort handler — owns the
# TaskStore row's terminal word. It stamps the SAME floor-aware word the
# session record and the crash envelope carry (data["task_state"] via
# terminal_task_state), or the abort status_override when the run was still
# mid-flight, through write_aborted_task_row (update_task_state with
# skip_if_terminal=True). These tests drive the REAL finalizer and assert the
# row write, so a regression that drops the stamp — or fires it on a paused /
# unreadable run — turns red.
# ---------------------------------------------------------------------------


class _RowStore:
    """Fake TaskStore recording the chokepoint's terminal-word writes."""

    def __init__(self):
        self.writes: list[tuple] = []

    async def update_task_state(
        self, task_id, task_state, *, recover_verification=None,
        skip_if_terminal=False,
    ):
        self.writes.append((task_id, task_state, skip_if_terminal))
        return True


def _patch_row_store():
    from unittest.mock import AsyncMock, patch

    row_store = _RowStore()
    return row_store, patch(
        "chaos_agent.persistence.task_store.get_task_store",
        new=AsyncMock(return_value=row_store),
    )


@pytest.mark.asyncio
async def test_chokepoint_stamps_terminal_row_with_reached_verdict():
    """A run that reached its own verdict: the row is stamped with THAT
    word (the same one the session record carries), skip_if_terminal=True
    so a clean run whose final upsert already landed the word is an
    idempotent no-op at the store layer."""
    store = _SessionStore()
    row_store, row_patch = _patch_row_store()

    with row_patch:
        await finalize_inject_session(
            store,
            _Graph(_inject_values()),
            {"configurable": {"thread_id": "task-1"}},
            "task-1",
        )

    assert row_store.writes == [("task-1", "injected", True)]
    # The row word and the session record's word are ONE fact: both derive
    # from data["task_state"] (terminal_task_state) in the same call.
    assert store.finalized["result_summary"]["data"]["task_state"] == "injected"


@pytest.mark.asyncio
async def test_chokepoint_stamps_verdictless_crash_row_failed():
    """The #20 zombie-row cure: a verdict-less run that ENDED (crash, no
    durable Layer-1 floor) is stamped "failed" — never left frozen at the
    node-level upsert's "injecting". Before the chokepoint this path wrote
    no row at all on the CLI/non-stream server exits."""
    store = _SessionStore()
    row_store, row_patch = _patch_row_store()
    values = {
        **_inject_values(),
        "experiment_uid": "",
        "verification": None,
        "error": "internal_error: Inject failed: ReadTimeout",
        "failure_reason": "internal_error: Inject failed: ReadTimeout",
    }

    with row_patch:
        await finalize_inject_session(
            store,
            graph_or_agent=None,
            config=None,
            session_id="task-crash",
            precomputed_values=values,
        )

    assert row_store.writes == [("task-crash", "failed", True)]


@pytest.mark.asyncio
async def test_chokepoint_stamps_abort_override_on_midflight_run():
    """An interrupt of a still-mid-flight run (infer == "injecting") stamps
    the abort word "cancelled" on the row — the override survives the
    round-62 P8 verdict-yield gate because no verdict was reached."""
    store = _SessionStore()
    row_store, row_patch = _patch_row_store()
    values = {**_inject_values(), "experiment_uid": "", "verification": None}

    with row_patch:
        await finalize_inject_session(
            store,
            graph_or_agent=None,
            config=None,
            session_id="task-cancel",
            precomputed_values=values,
            status_override="cancelled",
        )

    assert row_store.writes == [("task-cancel", "cancelled", True)]
    assert store.finalized["status"] == "cancelled"


@pytest.mark.asyncio
async def test_chokepoint_override_yields_to_reached_verdict():
    """An abort racing a run that ALREADY reached its verdict must not
    rewrite the word: the P8 gate nullifies the override, so the row keeps
    "injected" (not "cancelled") — the row surface's skip_if_terminal is the
    same rule, both surfaces hold."""
    store = _SessionStore()
    row_store, row_patch = _patch_row_store()

    with row_patch:
        await finalize_inject_session(
            store,
            _Graph(_inject_values()),
            {"configurable": {"thread_id": "task-1"}},
            "task-1",
            status_override="cancelled",
        )

    assert row_store.writes == [("task-1", "injected", True)]
    assert store.finalized["status"] != "cancelled"


@pytest.mark.asyncio
async def test_chokepoint_does_not_stamp_paused_run():
    """Round-64 F3 preserved: a run parked at the confirmation gate has NOT
    ended — the resumable_pause guard returns before the stamp, so the row
    stays active for the resume path (no terminal word, no session write)."""
    store = _SessionStore()
    row_store, row_patch = _patch_row_store()

    with row_patch:
        await finalize_inject_session(
            store,
            _PausedGraph(_paused_inject_values()),
            {"configurable": {"thread_id": "task-paused"}},
            "task-paused",
        )

    assert row_store.writes == []
    assert store.finalized is None


@pytest.mark.asyncio
async def test_chokepoint_skips_out_of_domain_word():
    """An unreadable graph state yields data["task_state"] == "unknown" (the
    absence of a verdict, not one). "unknown" is outside the task_state
    column domain, so the chokepoint skips the stamp rather than raising —
    update_task_state's closed-set gate would reject it."""
    store = _SessionStore()
    row_store, row_patch = _patch_row_store()

    with row_patch:
        await finalize_inject_session(
            store,
            graph_or_agent=None,
            config=None,
            session_id="task-unreadable",
            precomputed_values=None,
        )

    assert row_store.writes == []



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
    # a82984a3 root cure: the summary is no longer "" — PAYLOAD mode derives the
    # card from durable state through the floor. The CONTRACT this test guards
    # (a failed fallback must NOT write a FALSE SUCCESS) still holds: a
    # verdict-less, cache-less recover fail-closes to task_state="failed" /
    # status="fail", never a success card. "" was a bug snapshot of the floor
    # bypass, not the contract.
    summary = store.finalized["result_summary"]
    assert summary["status"] == "fail"
    assert summary["data"]["task_state"] == "failed"


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
async def test_finalize_recover_no_word_is_fail_closed_midflight():
    """Round-64 F1: with NO classified word and NO verdict the floor is
    "failed", never "completed". This is the interrupt shape — the CLI
    recover paths used to reach this finalize with a mid-flight state and
    the optimistic default, recording a Ctrl-C'd recovery as a completed
    one while the same record's envelope said "recovering"."""
    store = _SessionStore()

    await finalize_recover_session(
        store,
        _Graph({"operation": "recover"}),
        {"configurable": {"thread_id": "task-recover-noword-midflight"}},
        "task-recover-noword-midflight",
        "task-inject",
        _inject_values(),
        result_summary_mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
    )

    assert store.finalized["status"] == "failed"
    # The two faces do not contradict each other: the session word is the
    # verdict ("no evidence → failed"), the envelope's own result is the
    # position ("recovering"). Neither claims a success.
    assert store.finalized["result_summary"]["data"]["result"] == "recovering"


@pytest.mark.asyncio
async def test_finalize_recover_no_word_verdict_still_speaks():
    """A reached verdict speaks for a caller with no word of its own —
    the fail-closed floor must never overwrite evidence on record."""
    cases = (
        (_recover_values(), "completed"),
        (
            {
                "operation": "recover",
                "recover_verification": {
                    "level": "failed",
                    "layer1": {"status": "failed"},
                },
            },
            "failed",
        ),
        (
            {
                "operation": "recover",
                "recover_verification": {"level": "unverified"},
            },
            "completed",
        ),
    )
    for values, expected in cases:
        store = _SessionStore()
        await finalize_recover_session(
            store,
            _Graph(values),
            {"configurable": {"thread_id": "task-recover-noword-verdict"}},
            "task-recover-noword-verdict",
            "task-inject",
            _inject_values(),
            result_summary_mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
        )
        assert store.finalized["status"] == expected, values


@pytest.mark.asyncio
async def test_finalize_recover_no_word_no_payload_no_values_is_fail_closed():
    """The payload-mode twin: an empty state and no payload is the weakest
    evidence a recover run can leave — the former "completed" default
    recorded exactly that run as a success."""
    store = _SessionStore()

    await finalize_recover_session(
        store,
        _Graph({}),
        {"configurable": {"thread_id": "task-recover-noword-empty"}},
        "task-recover-noword-empty",
        "task-inject",
        _inject_values(),
        result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
    )

    assert store.finalized["status"] == "failed"


@pytest.mark.asyncio
async def test_finalize_recover_no_word_falls_back_to_payload_word():
    """A payload on record still speaks for a caller with no word: the
    fail-closed floor applies only when nothing else is left."""
    store = _SessionStore()

    await finalize_recover_session(
        store,
        _Graph({"operation": "recover"}),
        {"configurable": {"thread_id": "task-recover-noword-payload"}},
        "task-recover-noword-payload",
        "task-inject",
        _inject_values(),
        result_payload={
            "status": "success",
            "data": {
                "task_id": "task-recover-noword-payload",
                "task_state": "recovered",
            },
        },
        result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
    )

    assert store.finalized["status"] == "completed"


@pytest.mark.asyncio
async def test_finalize_recover_no_word_data_less_payload_falls_through():
    """A payload carrying no data dict is not a verdict — the state's own
    verdict decides (the former code read any non-dict data as a
    completion)."""
    store = _SessionStore()

    await finalize_recover_session(
        store,
        _Graph({"operation": "recover"}),
        {"configurable": {"thread_id": "task-recover-noword-barepayload"}},
        "task-recover-noword-barepayload",
        "task-inject",
        _inject_values(),
        result_payload={"status": "success"},
        result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
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


# ---------------------------------------------------------------------------
# Reason channel (error_override): the harness-testimony fix.
#
# The graph state is the single authoritative source of WHY a run ended, but
# an UNCAUGHT exception is the one ending the run cannot record itself — the
# node dies before writing failure_reason/error, the checkpoint freezes at the
# prior superstep, and the projection carries an empty reason that
# build_inject_envelope's fail-closed branch collapses to the bare
# "Injection failed" (code 4002). error_override lets the surviving
# orchestrator (the CLI/server except arm) deposit the cause it witnessed.
# Contract: graph state ALWAYS wins — the override fills ONLY a reason-less
# projection.
# ---------------------------------------------------------------------------

_OVERRIDE = "internal_error: Inject failed: RuntimeError: Connection error."


def _reason_less_failed_values() -> dict:
    """The exact projection an early-abort crash leaves: a plan/spec on
    record but NO verdict, NO uid, NO error — terminal_task_state maps it to
    "failed" (fail-closed) and build_inject_envelope's empty-reason branch
    renders the bare "Injection failed"."""
    return {**_inject_values(), "experiment_uid": "", "verification": None}


@pytest.mark.asyncio
async def test_error_override_fills_reason_less_crash_projection():
    """The defect: a reason-less failed projection persisted the bare
    "Injection failed". With the orchestrator's testimony the durable record
    carries the actual cause instead of a false-certainty generic string."""
    store = _SessionStore()

    await finalize_inject_session(
        store,
        graph_or_agent=None,
        config=None,
        session_id="task-crash",
        precomputed_values=_reason_less_failed_values(),
        error_override=_OVERRIDE,
    )

    assert store.finalized["status"] == "failed"
    summary = store.finalized["result_summary"]
    assert summary["message"] == _OVERRIDE
    assert summary["message"] != "Injection failed"
    assert summary["data"]["error"] == _OVERRIDE


@pytest.mark.asyncio
async def test_error_override_yields_to_graph_state_reason():
    """Contrapositive (precedence): a run that recorded its OWN reason keeps
    it — the override fills the participant's silence, never overwrites its
    last words. Every normal terminal exit (reject / loop-exhausted /
    wall-clock / victim-absent) writes a reason and must be untouched."""
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
        session_id="task-graph-reason",
        precomputed_values=values,
        error_override="internal_error: SHOULD NOT APPEAR",
    )

    summary = store.finalized["result_summary"]
    assert summary["message"] == "execution_failed: iptables not found"
    assert "SHOULD NOT APPEAR" not in summary["message"]


@pytest.mark.asyncio
async def test_error_override_none_keeps_bare_fallback():
    """Contrapositive (no testimony): with no override the projection is
    unchanged — the pre-fix bare "Injection failed" still renders. The fix is
    strictly additive; a caller with nothing to say behaves exactly as
    before."""
    store = _SessionStore()

    await finalize_inject_session(
        store,
        graph_or_agent=None,
        config=None,
        session_id="task-no-override",
        precomputed_values=_reason_less_failed_values(),
    )

    assert store.finalized["result_summary"]["message"] == "Injection failed"


@pytest.mark.asyncio
async def test_error_override_fills_data_envelope_mode():
    """The server inject_stream crash arm persists DATA_ENVELOPE: the cause
    lands in data["error"] verbatim (the audit record's reason field)."""
    store = _SessionStore()

    await finalize_inject_session(
        store,
        graph_or_agent=None,
        config=None,
        session_id="task-data-env",
        precomputed_values=_reason_less_failed_values(),
        result_summary_mode=RESULT_SUMMARY_DATA_ENVELOPE,
        error_override=_OVERRIDE,
    )

    assert store.finalized["result_summary"]["data"]["error"] == _OVERRIDE


@pytest.mark.asyncio
async def test_error_override_fills_status_envelope_mode():
    """The server inject.py crash arm persists STATUS_ENVELOPE: the cause
    lands in the status envelope's error field."""
    store = _SessionStore()

    await finalize_inject_session(
        store,
        graph_or_agent=None,
        config=None,
        session_id="task-status-env",
        precomputed_values=_reason_less_failed_values(),
        result_summary_mode=RESULT_SUMMARY_STATUS_ENVELOPE,
        error_override=_OVERRIDE,
    )

    assert store.finalized["result_summary"]["data"]["error"] == _OVERRIDE


@pytest.mark.asyncio
async def test_error_override_fills_unknown_projection():
    """The worst crash shape: aget_state itself failed, so there is NO graph
    state at all (build_unknown_inject_data). The orchestrator's testimony is
    the ONLY cause on record — it must still land."""
    store = _SessionStore()

    await finalize_inject_session(
        store,
        graph_or_agent=None,
        config=None,
        session_id="task-unknown",
        precomputed_values=None,
        result_summary_mode=RESULT_SUMMARY_DATA_ENVELOPE,
        error_override=_OVERRIDE,
    )

    assert store.finalized["status"] == "failed"
    assert store.finalized["result_summary"]["data"]["error"] == _OVERRIDE


@pytest.mark.asyncio
async def test_recover_cli_envelope_error_override_fills_reason_less():
    """Recover edition: the CLI recover except arm persists
    RECOVER_CLI_ENVELOPE, which routes through build_inject_envelope and hits
    the same bare fallback. A reason-less failed recovery takes the cause."""
    store = _SessionStore()
    values = {
        "operation": "recover",
        "recover_verification": {"level": "failed", "layer1": {"status": "failed"}},
    }
    override = "internal_error: Recovery failed: RuntimeError: boom"

    await finalize_recover_session(
        store,
        _Graph(values),
        {"configurable": {"thread_id": "task-recover-crash"}},
        "task-recover-crash",
        "task-inject",
        _inject_values(),
        result_summary_mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
        default_status="failed",
        error_override=override,
    )

    assert store.finalized["status"] == "failed"
    assert store.finalized["result_summary"]["message"] == override


@pytest.mark.asyncio
async def test_recover_cli_envelope_error_override_yields_to_state_reason():
    """Contrapositive (recover precedence): a recover graph that recorded its
    own reason keeps it."""
    store = _SessionStore()
    values = {
        "operation": "recover",
        "recover_verification": {"level": "failed", "layer1": {"status": "failed"}},
        "error": "recover_graph_reason",
        "failure_reason": "recover_graph_reason",
    }

    await finalize_recover_session(
        store,
        _Graph(values),
        {"configurable": {"thread_id": "task-recover-graphreason"}},
        "task-recover-graphreason",
        "task-inject",
        _inject_values(),
        result_summary_mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
        default_status="failed",
        error_override="internal_error: SHOULD NOT APPEAR",
    )

    summary = store.finalized["result_summary"]
    assert summary["message"] == "recover_graph_reason"
    assert "SHOULD NOT APPEAR" not in summary["message"]


def test_recover_payload_mode_ignores_error_override():
    """Scope boundary: PAYLOAD mode is NOT a consumer of the reason channel —
    error_override must not reshape its summary. a82984a3 root cure: when the
    caller built no payload, PAYLOAD mode now DERIVES the card from durable
    state (floor) instead of persisting ""; that derivation reads ONLY the graph
    state (build_recover_data_from_state takes no error_override), so the reason
    channel still cannot leak in. The recover_stream / l4 / turn recover
    fallbacks all run this mode."""
    summary = build_recover_session_summary(
        {"operation": "recover"},
        recover_task_id="task-recover",
        inject_task_id="task-inject",
        inject_state_values=_inject_values(),
        mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
        error_override="internal_error: SHOULD NOT RESHAPE",
    )

    # A verdict-less, cache-less recover fail-closes to "failed", and the
    # override string appears NOWHERE in the state-derived card.
    assert summary["status"] == "fail"
    assert summary["data"]["task_state"] == "failed"
    assert "SHOULD NOT RESHAPE" not in str(summary)


def test_every_envelope_crash_arm_wires_error_override():
    """Mechanical closure (穷举消费面): every ENVELOPE-mode crash finalize
    call site must pass error_override, or the reason channel has a hole. A
    new crash arm that forgets the wiring turns this red — the regression
    guard does not rely on author diligence."""
    checked_files = [
        PROJECT_ROOT / "src/chaos_agent/cli/runner.py",
        PROJECT_ROOT / "src/chaos_agent/server/routes/inject.py",
        PROJECT_ROOT / "src/chaos_agent/server/routes/inject_stream.py",
        PROJECT_ROOT / "src/chaos_agent/server/routes/turn_event_stream.py",
    ]
    missing = [
        path.name
        for path in checked_files
        if "error_override=" not in path.read_text(encoding="utf-8")
    ]

    assert missing == []
