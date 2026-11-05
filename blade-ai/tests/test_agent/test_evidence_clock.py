"""Evidence clock — framework-stamped wall clock on every tool receipt.

Case #64 (Service_调用失败_ReadinessProbe配置不一致): the verifier read its
probes as a flat, undated list, reasoned "three Services refuse, one pod IP
answers 200" into "ClusterIP egress from pods is broken environment-wide", and
downgraded a verified injection to ``partial``. Nothing in its context carried a
timestamp — not the message history, not the ledger, not the system prompt — so
it could not see that the recovery carrier had reached a ClusterIP successfully
moments before one of those refusals.

These tests pin the three properties that make the fix a fact supply rather than
a judgement: the stamp never touches ``content`` (which is parsed), the interval
is pure arithmetic over state, and the stamp lands at the one chokepoint every
tool in every phase passes through.
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from chaos_agent.agent.evidence_clock import (
    EVIDENCE_INTERVAL_KEY,
    EVIDENCE_TS_KEY,
    INJECTION_ANCHOR_OFFSET_PAIRS,
    INJECTION_TIME_ANCHOR_SLOTS,
    POST_FIRE,
    POST_ISSUE_WINDOW,
    POST_ISSUE_WINDOW_PRE_FIRE,
    PRE_ISSUE,
    WINDOW_UNKNOWN,
    WITHIN_ISSUE_WINDOW,
    append_time_facts_reminder,
    classify_evidence_interval,
    injection_anchor_offsets,
    injection_time_anchors,
    read_window_facts,
    render_evidence_timeline,
    render_time_facts,
    scan_host_recovery_receipts,
    stamp_tool_results,
)
from chaos_agent.agent.execution_artifacts import (
    PROVENANCE_FRAMEWORK,
    PROVENANCE_UNOBSERVED,
)
from chaos_agent.utils.time import BEIJING_TZ


@pytest.fixture
def window():
    """A window with every fact on record: T0 500s ago, D=300, fire at T0+420.

    ``t0_epoch`` is a WHOLE second on purpose. The state carries T0 as an ISO
    string and ``read_window_facts`` parses it back, so a fractional epoch
    round-trips with sub-microsecond drift — invisible everywhere except an
    exact-boundary assertion (offset == duration), where it flips the label at
    random. Real runs never hinge on a microsecond at the window edge; the
    test must not either.
    """
    t0_epoch = float(int(time.time()) - 500)
    return {
        "t0_epoch": t0_epoch,
        "state": {
            "injection_start_time": datetime.fromtimestamp(t0_epoch, BEIJING_TZ).isoformat(),
            "fault_spec": {"duration_seconds": 300},
            "execution_artifacts": [
                {"status": "recovery_armed", "recovery_deadline_epoch": t0_epoch + 420},
            ],
            "messages": [],
        },
    }


class TestIntervalIsArithmeticNotJudgement:
    """The label is a coordinate. None of these branches weighs evidence."""

    @pytest.mark.parametrize("offset,expected", [
        (-20, PRE_ISSUE),
        (0, WITHIN_ISSUE_WINDOW),
        (299, WITHIN_ISSUE_WINDOW),
        (300, POST_ISSUE_WINDOW_PRE_FIRE),
        (419, POST_ISSUE_WINDOW_PRE_FIRE),
        (420, POST_FIRE),
        (900, POST_FIRE),
    ])
    def test_positions_on_the_timeline(self, window, offset, expected):
        state = window["state"]
        assert classify_evidence_interval(state, window["t0_epoch"] + offset) == expected

    def test_boundary_belongs_to_the_later_interval(self, window):
        # Pinned separately from the parametrised sweep because it is the one
        # position a half-open interval can get wrong: the window is
        # ``[T0, T0+D)`` and the fire is ``[fire, …)``, so each boundary
        # instant is the FIRST moment of the interval that follows it.
        state = window["state"]
        t0 = window["t0_epoch"]
        assert classify_evidence_interval(state, t0) == WITHIN_ISSUE_WINDOW
        assert classify_evidence_interval(state, t0 + 300) == POST_ISSUE_WINDOW_PRE_FIRE
        assert classify_evidence_interval(state, t0 + 420) == POST_FIRE

    def test_no_armed_fire_leaves_the_post_window_label_honest(self, window):
        # The ChaosBlade path times out inside the experiment and never arms a
        # carrier. Claiming a fire boundary that is not on record would be a
        # fabricated fact, so the label says only what is known.
        state = dict(window["state"], execution_artifacts=[])
        assert classify_evidence_interval(state, window["t0_epoch"] + 900) == POST_ISSUE_WINDOW

    def test_missing_duration_is_unknown_not_assumed(self, window):
        state = dict(window["state"], fault_spec={})
        assert classify_evidence_interval(state, window["t0_epoch"] + 10) == WINDOW_UNKNOWN

    def test_missing_onset_is_unknown(self):
        assert classify_evidence_interval({}, time.time()) == WINDOW_UNKNOWN

    def test_unparseable_onset_reads_as_absent(self):
        # A replan seam clears injection_start_time; a malformed leftover must
        # not anchor the whole timeline on a bogus T0.
        state = {"injection_start_time": "not-a-timestamp",
                 "fault_spec": {"duration_seconds": 300}}
        assert classify_evidence_interval(state, time.time()) == WINDOW_UNKNOWN

    def test_earliest_fire_is_the_boundary(self, window):
        # Several carriers armed: the FIRST fire is what ends "nothing has been
        # reversed yet", so a later deadline must not push the boundary out.
        t0 = window["t0_epoch"]
        state = dict(window["state"], execution_artifacts=[
            {"status": "recovery_armed", "recovery_deadline_epoch": t0 + 900},
            {"status": "recovery_armed", "recovery_deadline_epoch": t0 + 420},
            {"status": "cleaned", "recovery_deadline_epoch": t0 + 10},
        ])
        facts = read_window_facts(state)
        assert facts[2] == pytest.approx(t0 + 420)
        assert classify_evidence_interval(state, t0 + 500) == POST_FIRE

    def test_non_numeric_deadline_is_ignored(self, window):
        state = dict(window["state"], execution_artifacts=[
            {"status": "recovery_armed", "recovery_deadline_epoch": "soon"},
            {"status": "recovery_armed"},
            {"status": "recovery_armed", "recovery_deadline_epoch": True},
        ])
        assert read_window_facts(state)[2] is None


class TestStampNeverTouchesContent:
    """``content`` is parsed: ``tool_verdicts.loads_dict`` requires it to start
    with ``{`` and abstains otherwise, so one prepended line would silently turn
    every JSON receipt into an unreadable one — and the provider verdicts that
    read it with it. This is the constraint that pushed the stamp into
    ``additional_kwargs``."""

    def test_json_receipt_still_parses_after_stamping(self, window):
        from chaos_agent.agent.tool_verdicts import loads_dict

        receipt = ToolMessage(content='{"status": "success"}', tool_call_id="a", name="kubectl")
        assert loads_dict(receipt.content) == {"status": "success"}
        stamp_tool_results({"messages": [receipt]}, window["state"])
        assert receipt.content == '{"status": "success"}'
        assert loads_dict(receipt.content) == {"status": "success"}

    def test_stamp_lands_in_additional_kwargs(self, window):
        msg = ToolMessage(content="plain", tool_call_id="a", name="shell")
        stamp_tool_results({"messages": [msg]}, window["state"], now=window["t0_epoch"] + 10)
        assert msg.additional_kwargs[EVIDENCE_INTERVAL_KEY] == WITHIN_ISSUE_WINDOW
        assert parse(msg.additional_kwargs[EVIDENCE_TS_KEY]) == pytest.approx(
            window["t0_epoch"] + 10, abs=1)

    @pytest.mark.parametrize("shape", ["dict", "command", "nested", "tuple"])
    def test_every_toolnode_return_shape_is_reached(self, window, shape):
        # The shape is not fixed: a plain batch returns a dict, a tool that
        # answers with ``Command(update=...)`` (update_progress,
        # finish_execution) makes the node return a Command, and mixed batches
        # nest both. Walking structurally is what keeps a new shape from
        # silently skipping the stamp.
        msg = ToolMessage(content="{}", tool_call_id="a", name="t")
        shapes = {
            "dict": {"messages": [msg]},
            "command": Command(update={"messages": [msg]}),
            "nested": [Command(update={"messages": [msg]})],
            "tuple": ({"messages": [msg]},),
        }
        stamp_tool_results(shapes[shape], window["state"])
        assert EVIDENCE_TS_KEY in msg.additional_kwargs, shape

    def test_second_stamp_does_not_redate_the_receipt(self, window):
        # A rebuild, a retry, a re-entered node: the first observation time is
        # the fact. Silently re-dating it to a later pass would fabricate a
        # timeline that never happened.
        msg = ToolMessage(content="{}", tool_call_id="a", name="t")
        stamp_tool_results({"messages": [msg]}, window["state"], now=window["t0_epoch"] + 10)
        first = msg.additional_kwargs[EVIDENCE_TS_KEY]
        stamp_tool_results({"messages": [msg]}, window["state"], now=window["t0_epoch"] + 900)
        assert msg.additional_kwargs[EVIDENCE_TS_KEY] == first
        assert msg.additional_kwargs[EVIDENCE_INTERVAL_KEY] == WITHIN_ISSUE_WINDOW

    def test_the_same_message_reachable_twice_is_stamped_once(self, window):
        msg = ToolMessage(content="{}", tool_call_id="a", name="t")
        stamp_tool_results({"messages": [msg], "again": [msg]}, window["state"])
        assert EVIDENCE_TS_KEY in msg.additional_kwargs

    def test_a_hostile_state_cannot_fail_the_drill(self):
        # Stamping sits on the return path of EVERY tool node in the graph. A
        # raising stamp would turn an observability gap into a failed mutation
        # that has ALREADY been dispatched — strictly worse than an undated
        # receipt — so the whole stamp degrades to a no-op instead.
        class _Hostile(dict):
            def get(self, *args, **kwargs):
                raise RuntimeError("state is unreadable")

        msg = ToolMessage(content="{}", tool_call_id="a", name="kubectl")
        result = {"messages": [msg]}
        assert stamp_tool_results(result, _Hostile()) is result
        assert EVIDENCE_TS_KEY not in msg.additional_kwargs

    def test_a_hostile_receipt_cannot_fail_the_drill(self):
        class _HostileKwargs(dict):
            def setdefault(self, *args, **kwargs):
                raise RuntimeError("kwargs are read-only")

        msg = ToolMessage(content="{}", tool_call_id="a", name="kubectl")
        msg.additional_kwargs = _HostileKwargs()
        result = {"messages": [msg]}
        assert stamp_tool_results(result, {}) is result


class TestTimelineRendering:
    def test_receipts_are_dated_in_order(self, window):
        state = window["state"]
        early = ToolMessage(content="{}", tool_call_id="a", name="kubectl")
        late = ToolMessage(content="{}", tool_call_id="b", name="update_progress")
        stamp_tool_results({"messages": [early]}, state, now=window["t0_epoch"] + 274)
        stamp_tool_results({"messages": [late]}, state, now=window["t0_epoch"] + 446)
        state["messages"] = [early, late]

        out = render_evidence_timeline(state)
        assert "kubectl" in out and "update_progress" in out
        assert out.index("kubectl") < out.index("update_progress")
        # the window facts the coordinates are measured against — labelled as
        # the ISSUE window, not the fault window (fault onset is unobserved)
        assert "issue window 300s" in out
        assert WITHIN_ISSUE_WINDOW in out and POST_FIRE in out
        assert "+274s since command issued" in out
        assert "+446s since command issued" in out
        # reverse-pin the retired mislabels: the offset must never again assert
        # the anchor is "the injection" (the #75 W-75-2 effect-lag root)
        assert "since injection" not in out
        assert "fault window 300s" not in out

    def test_pre_issue_receipt_reads_as_before_command_issued(self, window):
        state = window["state"]
        msg = ToolMessage(content="{}", tool_call_id="a", name="kubectl")
        stamp_tool_results({"messages": [msg]}, state, now=window["t0_epoch"] - 30)
        state["messages"] = [msg]
        out = render_evidence_timeline(state)
        assert "30s before command issued" in out
        assert "before injection" not in out
        assert PRE_ISSUE in out

    def test_no_fire_on_record_is_said_so(self, window):
        state = dict(window["state"], execution_artifacts=[])
        msg = ToolMessage(content="{}", tool_call_id="a", name="kubectl")
        stamp_tool_results({"messages": [msg]}, state)
        state["messages"] = [msg]
        assert "no armed recovery fire on record" in render_evidence_timeline(state)

    @pytest.mark.parametrize("state", [
        {},
        {"messages": []},
        {"messages": [ToolMessage(content="q", tool_call_id="q", name="kubectl")]},
    ])
    def test_silent_when_nothing_is_stamped(self, state):
        # Callers append unconditionally, so an unstamped history (a ledger
        # written before this existed, a replan-cleared state) must render
        # nothing rather than an empty header claiming authority.
        assert render_evidence_timeline(state) == ""

    def test_unstamped_receipts_are_skipped_not_invented(self, window):
        state = window["state"]
        stamped = ToolMessage(content="{}", tool_call_id="a", name="kubectl")
        bare = ToolMessage(content="{}", tool_call_id="b", name="shell")
        stamp_tool_results({"messages": [stamped]}, state)
        state["messages"] = [stamped, bare]
        out = render_evidence_timeline(state)
        assert "kubectl" in out
        assert "shell" not in out


class TestDispatchChokepoint:
    """One wiring point covers every tool in every phase — execute loop,
    verifier, recover verifier, clarification, plan builder. Stamping per tool
    or per node would need re-enumerating each time either grows."""

    @pytest.mark.asyncio
    async def test_with_tool_span_stamps_the_batch(self, window):
        from chaos_agent.agent.dispatch import with_tool_span

        receipt = ToolMessage(content='{"ok": true}', tool_call_id="a", name="kubectl")

        class _ToolNode:
            async def ainvoke(self, state, config=None):
                return {"messages": [receipt]}

        wrapped = with_tool_span("phase2_tools", _ToolNode())
        result = await wrapped(window["state"])
        assert result["messages"] == [receipt]
        assert EVIDENCE_TS_KEY in receipt.additional_kwargs
        assert EVIDENCE_INTERVAL_KEY in receipt.additional_kwargs

    @pytest.mark.asyncio
    async def test_stamping_survives_a_tool_node_that_raises(self, window):
        # The span's error path must not swallow the exception on the way to
        # a stamp that never runs.
        from chaos_agent.agent.dispatch import with_tool_span

        class _Boom:
            async def ainvoke(self, state, config=None):
                raise RuntimeError("tool exploded")

        wrapped = with_tool_span("phase2_tools", _Boom())
        with pytest.raises(RuntimeError, match="tool exploded"):
            await wrapped(window["state"])


def parse(iso: str) -> float:
    from chaos_agent.utils.time import parse_iso_timestamp
    return parse_iso_timestamp(iso).timestamp()


class TestVerifierWiring:
    """The stamp is worthless unless the verifier is actually shown it.

    Stamping and rendering are two separate wirings; either can be dropped
    without the other failing. Case #58's window clock is the precedent for
    where a time fact belongs — a system reminder rendered fresh on every
    builder call, never persisted.
    """

    @staticmethod
    def _layer2_text(state) -> str:
        from langchain_core.messages import HumanMessage

        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _build_layer2_messages,
        )
        from chaos_agent.agent.result.verdict import Layer1Result

        layer1 = Layer1Result(status="passed", affected_count=1, raw_output="Success")
        msgs = _build_layer2_messages(
            state, layer1, "uid-64", "readinessprobe-mismatch",
            "/path/to/kc", count=1,
        )
        return "\n".join(
            m.content for m in msgs
            if isinstance(m, HumanMessage) and isinstance(m.content, str)
        )

    def _state(self, window):
        from chaos_agent.agent.spec.fault_spec import FaultSpec

        state = dict(window["state"])
        state.update({
            "fault_spec": FaultSpec(
                namespace="default", scope="deployment",
                fault_target="readinessprobe", fault_action="replace",
                duration_seconds=300,
            ).to_dict(),
            "injection_parsed_params": {},
            "params": {},
            "kubeconfig": "/path/to/kc",
        })
        probe = ToolMessage(
            content='{"code": "000"}', tool_call_id="p1", name="kubectl",
        )
        stamp_tool_results({"messages": [probe]}, state, now=window["t0_epoch"] + 446)
        state["messages"] = [probe]
        return state

    def test_the_timeline_reaches_the_verifier(self, window):
        text = self._layer2_text(self._state(window))
        assert "EVIDENCE TIMELINE" in text
        assert "kubectl" in text
        assert POST_FIRE in text

    def test_it_sits_beside_the_window_clock(self, window):
        # Same freshness contract as the clock: rendered per builder call, not
        # persisted, so the offsets cannot go stale.
        text = self._layer2_text(self._state(window))
        assert "INJECTION WINDOW CLOCK" in text
        assert text.index("INJECTION WINDOW CLOCK") < text.index("EVIDENCE TIMELINE")

    def test_silent_when_nothing_was_stamped(self, window):
        state = self._state(window)
        state["messages"] = [
            ToolMessage(content='{"code": "000"}', tool_call_id="p1", name="kubectl"),
        ]
        assert "EVIDENCE TIMELINE" not in self._layer2_text(state)
        # the clock still renders — dropping the timeline must not take it with it
        assert "INJECTION WINDOW CLOCK" in self._layer2_text(state)


def _is_time_facts(m) -> bool:
    """True for the transient time-fact reminder (its header key phrase)."""
    c = getattr(m, "content", None)
    return isinstance(c, str) and "SYSTEM CLOCK" in c


def _window_state(*, t0_offset, duration=None, fire_offset=None):
    """A state whose T0 is ``t0_offset`` seconds from now (negative = past),
    optionally with a fault duration and an armed recovery fire
    ``fire_offset`` seconds from now."""
    t0 = float(int(time.time()) + t0_offset)
    state = {
        "injection_start_time": datetime.fromtimestamp(t0, BEIJING_TZ).isoformat(),
        "messages": [],
    }
    if duration is not None:
        state["fault_spec"] = {"duration_seconds": duration}
    if fire_offset is not None:
        state["execution_artifacts"] = [
            {"status": "recovery_armed",
             "recovery_deadline_epoch": time.time() + fire_offset},
        ]
    return state


class TestTimeFactsRendering:
    """``render_time_facts``: the blind phases (planning/execute/recover) get the
    ONE time fact no message carries — the current system clock — so their LLM
    stops reverse-engineering today's date from tool-output archaeology
    (inject-14fdcd15: the executor inferred the date from a binary's build year).
    It renders NO fault-window position and NO armed-fire countdown: those are
    lifecycle judgements belonging to the verifier (which runs after execute once
    landing is confirmed), and a blind phase computing them would read
    ``injection_start_time`` — an issue-time marker that survives a FAILED
    injection command and so anchors a phantom window (the retest rendered
    "window CLOSED ~438s ago" while the real fault had just landed). These pin
    "clock only, never a window".
    """

    def test_renders_the_now_anchor(self):
        out = render_time_facts({"messages": []})
        assert "SYSTEM CLOCK" in out
        assert "now " in out

    def test_empty_state_still_renders_the_now_anchor(self):
        assert "now " in render_time_facts({})

    def test_a_fully_populated_window_still_renders_only_the_clock(self, window):
        # The regression pin: even when state carries T0 + duration + an armed
        # fire (everything a window rendering would need), render_time_facts
        # supplies the clock and NONE of the window judgement. Reverse-asserts
        # the exact phantom-window wording the issue-time marker used to produce.
        out = render_time_facts(window["state"])
        assert "now " in out
        for banned in (
            "injection committed", "fault window", "remaining", "CLOSED",
            "elapsed", "armed recovery", "EVIDENCE TIMELINE",
            "INJECTION WINDOW CLOCK",
        ):
            assert banned not in out

    def test_a_failed_issue_marker_cannot_fabricate_a_window(self):
        # injection_start_time is stamped at ISSUE time and survives a failed
        # command; rendering it as a committed/closed window is the bug this
        # strips. A state that looks "mid-injection" must still yield clock only.
        state = _window_state(t0_offset=-500, duration=300, fire_offset=-10)
        out = render_time_facts(state)
        assert "now " in out
        assert "CLOSED" not in out
        assert "injection committed" not in out


class TestTimeFactsNeverAccumulates:
    """``append_time_facts_reminder`` mounts on the TRANSIENT per-round messages
    copy, never on the list persisted to state — the evidence_clock freshness
    contract (rendered per call, never persisted). If a mount site ever appended
    to the persisted list, every round would leave one more stale clock in
    history. These pin the helper half of that contract; the mount-site half
    (each node passes its transient copy, not ``_hints_for_state`` /
    ``_injections_for_state`` / ``result_update["messages"]``) is the wiring the
    node tests exercise.
    """

    def test_appends_exactly_one_reminder_to_the_passed_list(self, window):
        msgs = [HumanMessage(content="注入网络延迟")]
        append_time_facts_reminder(msgs, window["state"])
        assert len(msgs) == 2
        assert _is_time_facts(msgs[-1])

    def test_it_never_mutates_state_messages(self, window):
        # The ROOT of non-accumulation: the helper touches the passed list, not
        # state["messages"] (the persisted history the next round rebuilds from).
        state = window["state"]
        before = list(state["messages"])
        transient = list(state["messages"])
        append_time_facts_reminder(transient, state)
        assert state["messages"] == before
        assert not any(_is_time_facts(m) for m in state["messages"])

    def test_three_rounds_leave_zero_reminders_in_history(self, window):
        # Replay the real node loop: rebuild the transient copy each round,
        # persist ONLY the genuine turn. The reminder must never pile up.
        state = dict(window["state"])
        state["messages"] = [HumanMessage(content="注入网络延迟")]
        for rnd in range(3):
            transient = list(state["messages"])          # fresh copy per round
            append_time_facts_reminder(transient, state)
            assert sum(1 for m in transient if _is_time_facts(m)) == 1
            state["messages"] = state["messages"] + [AIMessage(content=f"r{rnd}")]
        assert sum(1 for m in state["messages"] if _is_time_facts(m)) == 0

    def test_the_reminder_is_a_wrapped_human_message(self, window):
        # It rides the same HumanMessage reminder channel as the ledger /
        # convergence hints; a bare string would break the message stream.
        msgs = []
        append_time_facts_reminder(msgs, window["state"])
        assert isinstance(msgs[0], HumanMessage)
        assert _is_time_facts(msgs[0])


class TestReminderCarriesClockNotTimeline:
    """``append_time_facts_reminder`` mounts ONE transient reminder carrying the
    clock ONLY. It must NOT fold in the per-tool receipt timeline
    (``render_evidence_timeline``): that timeline labels each receipt with a
    window interval and a "+Ns since command issued" offset measured against
    ``injection_start_time``, so folding it into a blind phase propagates the
    same phantom-window contamination the header strip removes. The timeline
    stays the verifier's tool (Case #64, pinned in TestTimelineRendering /
    TestVerifierWiring). These pin the blind-phase reminder clean of it even
    when receipts ARE stamped.
    """

    def _stamped_state(self, window):
        # A real stamped receipt: stamp_tool_results writes the observation time
        # into additional_kwargs, which is what render_evidence_timeline reads.
        state = window["state"]
        msg = ToolMessage(content="{}", tool_call_id="a", name="kubectl")
        stamp_tool_results({"messages": [msg]}, state, now=window["t0_epoch"] + 274)
        state["messages"] = [msg]
        return state

    def test_no_receipt_timeline_folds_into_the_blind_phase_reminder(self, window):
        state = self._stamped_state(window)
        msgs = [HumanMessage(content="注入网络延迟")]
        append_time_facts_reminder(msgs, state)
        # ONE reminder, clock only — the stamped receipt's timeline stays out.
        assert len(msgs) == 2
        body = msgs[-1].content
        assert _is_time_facts(msgs[-1])
        assert "EVIDENCE TIMELINE" not in body
        assert "since command issued" not in body

    def test_reminder_still_never_touches_state_history(self, window):
        # Stripping the timeline must not weaken the non-accumulation contract:
        # the stamped receipt lives in state["messages"], the reminder does not.
        state = self._stamped_state(window)
        before = list(state["messages"])
        transient = list(state["messages"])
        append_time_facts_reminder(transient, state)
        assert state["messages"] == before
        assert not any(_is_time_facts(m) for m in state["messages"])


class TestInjectionTimeAnchors:
    """The injection-side multi-anchor model, mirroring ``recovery_time_anchors``.

    Root-cause pin for #75 W-75-2: ``command_issued`` is ISSUE-time (framework
    bookkeeping, a lower bound on onset), ``fault_onset`` is declared UNOBSERVED
    with no framework writer, so the effect lag the verifier used to mis-derive
    from the bare T0 becomes a NAMED GAP, never a number the framework asserts.
    """

    def test_every_slot_is_always_present_with_provenance(self, window):
        anchors = injection_time_anchors(window["state"])
        assert set(anchors) == set(INJECTION_TIME_ANCHOR_SLOTS)
        for slot in INJECTION_TIME_ANCHOR_SLOTS:
            assert "provenance" in anchors[slot]
            assert "epoch" in anchors[slot]

    def test_command_issued_is_framework_not_onset(self, window):
        cmd = injection_time_anchors(window["state"])["command_issued"]
        assert cmd["provenance"] == PROVENANCE_FRAMEWORK
        assert cmd["epoch"] == pytest.approx(window["t0_epoch"])

    def test_fault_onset_is_always_unobserved(self, window):
        # No framework writer stamps onset — the slot is a standing, machine-
        # readable statement of what the framework cannot prove (the recovery
        # side's host_armed/host_fire precedent).
        onset = injection_time_anchors(window["state"])["fault_onset"]
        assert onset["provenance"] == PROVENANCE_UNOBSERVED
        assert onset["epoch"] is None

    def test_window_deadline_is_command_issued_plus_duration(self, window):
        deadline = injection_time_anchors(window["state"])["window_deadline"]
        assert deadline["provenance"] == PROVENANCE_FRAMEWORK
        assert deadline["epoch"] == pytest.approx(window["t0_epoch"] + 300)

    def test_absent_anchor_reads_as_unobserved_not_zero(self):
        # No injection_start_time (replan seam / pre-injection): every slot is
        # unobserved, never epoch 0 anchoring a timeline in 1970.
        anchors = injection_time_anchors({})
        for slot in INJECTION_TIME_ANCHOR_SLOTS:
            assert anchors[slot]["provenance"] == PROVENANCE_UNOBSERVED
            assert anchors[slot]["epoch"] is None

    def test_a_bool_stamp_cannot_anchor_a_timeline(self, window):
        # bool is an int subclass: a corrupted True must not read as epoch 1.
        state = dict(window["state"], injection_start_time=True)
        assert injection_time_anchors(state)["command_issued"]["epoch"] is None


class TestInjectionAnchorOffsets:
    """The effect lag is a NAMED GAP, not a framework-asserted number."""

    def test_only_the_contract_duration_is_observable(self, window):
        offsets = injection_anchor_offsets(injection_time_anchors(window["state"]))
        assert offsets["seconds"] == {"command_issued_to_window_deadline": 300.0}

    def test_every_onset_pair_is_an_unobserved_gap(self, window):
        offsets = injection_anchor_offsets(injection_time_anchors(window["state"]))
        # command_issued->fault_onset is the effect lag the #75 verifier
        # mis-derived; reported as a gap naming the missing end, never a number.
        assert offsets["unobserved"]["command_issued_to_fault_onset"] == ["fault_onset"]
        assert "fault_onset_to_window_deadline" in offsets["unobserved"]
        assert "command_issued_to_fault_onset" not in offsets["seconds"]

    def test_offset_labels_cover_every_declared_pair(self, window):
        offsets = injection_anchor_offsets(injection_time_anchors(window["state"]))
        labels = set(offsets["seconds"]) | set(offsets["unobserved"])
        assert labels == {f"{a}_to_{b}" for a, b in INJECTION_ANCHOR_OFFSET_PAIRS}


class TestAnchorReceiptIsMarked:
    """The receipt that SET the anchor is labelled setup-or-fault, so a "+0s"
    entry cannot masquerade as "the injection landed here" (the #75
    self-reinforcement: T0 always coincides with the receipt that set it)."""

    def test_the_zero_offset_receipt_carries_the_anchor_marker(self, window):
        state = window["state"]
        msg = ToolMessage(content="{}", tool_call_id="a", name="kubectl")
        stamp_tool_results({"messages": [msg]}, state, now=window["t0_epoch"])
        state["messages"] = [msg]
        out = render_evidence_timeline(state)
        assert "+0s since command issued" in out
        assert "anchor: first mutating command" in out
        assert "setup-or-fault not distinguished" in out

    def test_a_later_receipt_is_not_marked_as_the_anchor(self, window):
        state = window["state"]
        msg = ToolMessage(content="{}", tool_call_id="a", name="kubectl")
        stamp_tool_results({"messages": [msg]}, state, now=window["t0_epoch"] + 129)
        state["messages"] = [msg]
        out = render_evidence_timeline(state)
        assert "+129s since command issued" in out
        assert "anchor: first mutating command" not in out


# ---------------------------------------------------------------------------
# Host-native self-recovery receipt scan + four-state fire-note.
#
# A raw-shell host fault recovers through a systemd transient timer that leaves
# NO ``execution_artifacts`` entry (the ledger's collector is kubectl-gated), so
# Case #13 run-5's verifier read a bare "no armed recovery fire on record" and
# invented a "primary effect must be live-observed" gate out of that silence.
# These tests pin the read-side derivation that fills the hole: it correlates an
# arm/fire receipt with the framework's OWN ``--unit`` (so a stray host
# systemd-run cannot masquerade), abstains to unobserved on any doubt, and never
# writes content / kwargs / artifact.
# ---------------------------------------------------------------------------

_HOST_UNIT = "drill-timeoffset-recover"


def _ai_call(call_id: str, name: str, args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": args}])


def _arm_command(unit: str = _HOST_UNIT) -> str:
    """The canonical Tier-1 self-recovery timer the framework arms."""
    return f"systemd-run --on-active=30s --unit={unit} date -s REALTIME"


def _armed_stream(unit: str = _HOST_UNIT) -> list:
    """AIMessage arming the timer + its ``Running as unit:`` stderr receipt."""
    return [
        _ai_call("arm", "host_inject", {"command": _arm_command(unit)}),
        ToolMessage(
            content=f"--- STDERR ---\nRunning as unit: {unit}.service\n",
            tool_call_id="arm", name="host_inject",
        ),
    ]


def _fire_stream(unit: str = _HOST_UNIT, result: str = "success") -> list:
    """The verifier's own ``systemctl show <U>.timer -p Result`` + receipt."""
    return [
        _ai_call("fire", "host_read", {"command": f"systemctl show {unit}.timer -p Result"}),
        ToolMessage(
            content=f"Result={result}\n", tool_call_id="fire", name="host_read",
        ),
    ]


class TestHostRecoveryReceiptScan:
    def test_empty_state_is_all_unobserved(self):
        facts = scan_host_recovery_receipts({})
        assert facts["armed"]["confirmed"] is False
        assert facts["fired"]["confirmed"] is False

    def test_arm_receipt_on_the_frameworks_own_unit_confirms(self):
        facts = scan_host_recovery_receipts({"messages": _armed_stream()})
        assert facts["armed"]["confirmed"] is True
        assert facts["armed"]["unit_name"] == _HOST_UNIT
        # fire is a SEPARATE fact — an arm receipt alone must not claim it
        assert facts["fired"]["confirmed"] is False

    def test_arm_receipt_for_an_unregistered_unit_is_ignored(self):
        # A stray host systemd-run the framework never armed: the receipt names
        # a unit that is not in the registered set, so it must NOT confirm.
        msgs = [
            _ai_call("arm", "host_inject", {"command": _arm_command()}),
            ToolMessage(
                content="Running as unit: some-unrelated.service\n",
                tool_call_id="arm", name="host_inject",
            ),
        ]
        assert scan_host_recovery_receipts({"messages": msgs})["armed"]["confirmed"] is False

    def test_fire_requires_command_and_output_correlation(self):
        # Both the arm and a fire receipt are present -> fire confirms.
        msgs = _armed_stream() + _fire_stream()
        facts = scan_host_recovery_receipts({"messages": msgs})
        assert facts["fired"]["confirmed"] is True
        assert facts["fired"]["result"] == "success"
        assert facts["fired"]["unit_name"] == _HOST_UNIT

    def test_fire_confirms_when_result_is_batched_with_other_properties(self):
        # Case #13 live retest: the verifier read the timer's state AND its fire
        # result in ONE query — ``systemctl show <U>.timer -p
        # ActiveState,SubState,Result``. ``Result`` is one element of a
        # comma-separated property list, so the fire must still confirm (a bare
        # ``== "Result"`` test missed this and dropped CONFIRMED to ARMED-only).
        msgs = _armed_stream() + [
            _ai_call(
                "fire", "host_read",
                {"command": f"systemctl show {_HOST_UNIT}.timer -p ActiveState,SubState,Result"},
            ),
            ToolMessage(
                content="ActiveState=inactive\nSubState=dead\nResult=success\n",
                tool_call_id="fire", name="host_read",
            ),
        ]
        facts = scan_host_recovery_receipts({"messages": msgs})
        assert facts["fired"]["confirmed"] is True
        assert facts["fired"]["result"] == "success"
        assert facts["fired"]["unit_name"] == _HOST_UNIT

    def test_a_still_waiting_timer_with_result_success_does_not_confirm_a_fire(self):
        # Bandwidth #25 live false-CONFIRMED root fix. A systemd-run transient
        # ``.timer`` reports ``Result=success`` from the MOMENT it is armed — that
        # property is its arm-state, NOT a fire signal. Its pre-fire state is
        # ``ActiveState=active`` / ``SubState=waiting``. At 01:26:08 the tbf qdisc
        # was still mounted (fault live, carrier not yet fired) yet the verifier's
        # batch query returned exactly this, and the old scan read the bare
        # ``Result=success`` as a confirmed fire — the false CONFIRMED the module
        # header forbids. The still-``waiting`` receipt must veto the fire.
        msgs = _armed_stream() + [
            _ai_call(
                "fire", "host_read",
                {"command": f"systemctl show {_HOST_UNIT}.timer -p ActiveState,SubState,Result"},
            ),
            ToolMessage(
                content="Result=success\nActiveState=active\nSubState=waiting\n",
                tool_call_id="fire", name="host_read",
            ),
        ]
        facts = scan_host_recovery_receipts({"messages": msgs})
        # armed is still confirmed (the arm receipt is real); fire is NOT.
        assert facts["armed"]["confirmed"] is True
        assert facts["fired"]["confirmed"] is False

    def test_a_fired_timer_leaving_waiting_confirms(self):
        # The positive control for the veto above: the SAME batch query AFTER the
        # timer fired reports ``inactive``/``dead`` (it left ``waiting``) — that
        # receipt DOES confirm the fire.
        msgs = _armed_stream() + [
            _ai_call(
                "fire", "host_read",
                {"command": f"systemctl show {_HOST_UNIT}.timer -p ActiveState,SubState,Result"},
            ),
            ToolMessage(
                content="Result=success\nActiveState=inactive\nSubState=dead\n",
                tool_call_id="fire", name="host_read",
            ),
        ]
        facts = scan_host_recovery_receipts({"messages": msgs})
        assert facts["fired"]["confirmed"] is True
        assert facts["fired"]["result"] == "success"

    def test_a_property_list_without_result_does_not_confirm_a_fire(self):
        # The batch form is only a fire query when ``Result`` is among the
        # selected properties; ``-p ActiveState,SubState`` reads state, not the
        # fire outcome, so it must NOT confirm even with ``Result=success`` text.
        msgs = _armed_stream() + [
            _ai_call(
                "fire", "host_read",
                {"command": f"systemctl show {_HOST_UNIT}.timer -p ActiveState,SubState"},
            ),
            ToolMessage(
                content="ActiveState=inactive\nSubState=dead\nResult=success\n",
                tool_call_id="fire", name="host_read",
            ),
        ]
        assert scan_host_recovery_receipts({"messages": msgs})["fired"]["confirmed"] is False

    def test_a_stray_result_success_without_the_query_does_not_confirm(self):
        # ``Result=success`` in some unrelated receipt (no ``systemctl show
        # <U>.timer -p Result`` command behind it) must NOT read as a fire —
        # the dual correlation is what blocks the generic-string false positive.
        msgs = _armed_stream() + [
            _ai_call("x", "host_read", {"command": "systemctl is-active nginx"}),
            ToolMessage(content="Result=success\n", tool_call_id="x", name="host_read"),
        ]
        assert scan_host_recovery_receipts({"messages": msgs})["fired"]["confirmed"] is False

    def test_a_failed_result_does_not_confirm_a_fire(self):
        msgs = _armed_stream() + _fire_stream(result="exit-code")
        assert scan_host_recovery_receipts({"messages": msgs})["fired"]["confirmed"] is False

    def test_exec_host_command_binary_args_shape_registers_the_unit(self):
        # The other host arg shape: ``binary`` + ``args`` (an argv list), not a
        # raw ``command`` string.
        msgs = [
            _ai_call("arm", "exec_host_command", {
                "binary": "systemd-run",
                "args": ["--on-active=30s", f"--unit={_HOST_UNIT}", "date", "-s", "REALTIME"],
            }),
            ToolMessage(
                content=f"Running timer as unit: {_HOST_UNIT}.timer\n",
                tool_call_id="arm", name="exec_host_command",
            ),
        ] + _fire_stream()
        facts = scan_host_recovery_receipts({"messages": msgs})
        assert facts["armed"]["confirmed"] is True
        assert facts["fired"]["confirmed"] is True

    def test_tier2_setsid_carrier_yields_no_facts(self):
        # No systemd unit -> no ``--unit`` to register -> unobserved by design.
        msgs = [
            _ai_call("arm", "host_inject", {
                "command": "setsid --fork sh -c 'sleep 30 && date -s REALTIME'",
            }),
            ToolMessage(content="detached\n", tool_call_id="arm", name="host_inject"),
        ]
        facts = scan_host_recovery_receipts({"messages": msgs})
        assert facts["armed"]["confirmed"] is False
        assert facts["fired"]["confirmed"] is False

    def test_branch_b_no_carrier_is_unobserved(self):
        msgs = [
            _ai_call("inj", "host_inject", {"command": "iptables -A INPUT -j DROP"}),
            ToolMessage(content="ok\n", tool_call_id="inj", name="host_inject"),
        ]
        assert scan_host_recovery_receipts({"messages": msgs})["armed"]["confirmed"] is False

    def test_a_json_receipt_is_never_parsed_as_a_host_receipt(self):
        # A provider-verdict JSON channel that happens to contain the token must
        # be skipped verbatim — the scan reads host shell receipts only.
        msgs = [
            _ai_call("arm", "host_inject", {"command": _arm_command()}),
            ToolMessage(
                content='{"ok": true, "note": "Running as unit: %s.service"}' % _HOST_UNIT,
                tool_call_id="arm", name="host_inject",
            ),
        ]
        assert scan_host_recovery_receipts({"messages": msgs})["armed"]["confirmed"] is False

    def test_a_malformed_stream_degrades_to_unobserved(self):
        # A non-list message entry / junk must abstain, never raise: this runs on
        # the render path of every verifier call.
        facts = scan_host_recovery_receipts({"messages": [None, 42, "junk", object()]})
        assert facts["armed"]["confirmed"] is False
        assert facts["fired"]["confirmed"] is False

    def test_the_scan_is_read_only(self):
        # The scan must not mutate content or additional_kwargs of any message.
        msgs = _armed_stream() + _fire_stream()
        before = [(m.content, dict(m.additional_kwargs)) for m in msgs if isinstance(m, ToolMessage)]
        scan_host_recovery_receipts({"messages": msgs})
        after = [(m.content, dict(m.additional_kwargs)) for m in msgs if isinstance(m, ToolMessage)]
        assert before == after


class TestFireNoteFourStates:
    """The fire-note states the recovery clock honestly, separating the HOST
    facts (message scan) from the FRAMEWORK deadline (k8s carrier artifact) —
    the old single line rendered a computed deadline as if it were an observed
    fire."""

    def _stamped(self, msgs, window):
        state = dict(window["state"], execution_artifacts=[])
        for m in msgs:
            if isinstance(m, ToolMessage):
                stamp_tool_results({"messages": [m]}, state, now=window["t0_epoch"] + 10)
        state["messages"] = msgs
        return state

    def test_host_confirmed_fire_renders_confirmed_and_declares_no_wall_clock(self, window):
        out = render_evidence_timeline(self._stamped(_armed_stream() + _fire_stream(), window))
        assert "recovery fire CONFIRMED on host" in out
        assert f"{_HOST_UNIT}.timer" in out
        assert "wall-clock fire time is unavailable" in out
        # reverse-pin: a confirmed host fire must NOT fall back to the old holes
        assert "no armed recovery fire on record" not in out
        assert "predicted recovery deadline" not in out

    def test_armed_but_unobserved_fire_renders_armed_not_silence(self, window):
        out = render_evidence_timeline(self._stamped(_armed_stream(), window))
        assert "recovery carrier ARMED on host" in out
        assert "fire is NOT confirmed" in out
        # the whole point of the fix: an armed carrier is NOT "no fire on record"
        assert "no armed recovery fire on record" not in out

    def test_a_still_waiting_timer_receipt_renders_armed_not_confirmed(self, window):
        # Bandwidth #25 render-level regression: a fire query whose receipt still
        # shows the transient timer ``SubState=waiting`` (armed, Result=success is
        # the arm-state) must render ARMED, never "recovery fire CONFIRMED".
        msgs = _armed_stream() + [
            _ai_call(
                "fire", "host_read",
                {"command": f"systemctl show {_HOST_UNIT}.timer -p ActiveState,SubState,Result"},
            ),
            ToolMessage(
                content="Result=success\nActiveState=active\nSubState=waiting\n",
                tool_call_id="fire", name="host_read",
            ),
        ]
        out = render_evidence_timeline(self._stamped(msgs, window))
        assert "recovery carrier ARMED on host" in out
        assert "recovery fire CONFIRMED on host" not in out

    def test_framework_deadline_renders_predicted_not_fire(self, window):
        # A k8s carrier artifact with a deadline, no host receipts at all.
        state = dict(window["state"])
        state["execution_artifacts"] = [{
            "status": "recovery_armed",
            "recovery_deadline_epoch": window["t0_epoch"] + 420,
        }]
        msg = ToolMessage(content="{}", tool_call_id="a", name="kubectl")
        stamp_tool_results({"messages": [msg]}, state, now=window["t0_epoch"] + 10)
        state["messages"] = [msg]
        out = render_evidence_timeline(state)
        assert "predicted recovery deadline at" in out
        assert "framework-computed, not a host-confirmed fire" in out
        # reverse-pin: a computed deadline must never read as an observed fire
        assert "recovery fire CONFIRMED" not in out
        assert "recovery fire at " not in out

    def test_no_facts_at_all_says_no_fire_on_record(self, window):
        state = dict(window["state"], execution_artifacts=[])
        msg = ToolMessage(content="{}", tool_call_id="a", name="kubectl")
        stamp_tool_results({"messages": [msg]}, state, now=window["t0_epoch"] + 10)
        state["messages"] = [msg]
        out = render_evidence_timeline(state)
        assert "no armed recovery fire on record" in out
        assert "recovery fire CONFIRMED" not in out
        assert "recovery carrier ARMED" not in out
        assert "predicted recovery deadline" not in out
