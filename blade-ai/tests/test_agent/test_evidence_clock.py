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
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from chaos_agent.agent.evidence_clock import (
    EVIDENCE_INTERVAL_KEY,
    EVIDENCE_TS_KEY,
    IN_FAULT_WINDOW,
    POST_FIRE,
    POST_WINDOW,
    POST_WINDOW_PRE_FIRE,
    PRE_ONSET,
    WINDOW_UNKNOWN,
    classify_evidence_interval,
    read_window_facts,
    render_evidence_timeline,
    stamp_tool_results,
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
        (-20, PRE_ONSET),
        (0, IN_FAULT_WINDOW),
        (299, IN_FAULT_WINDOW),
        (300, POST_WINDOW_PRE_FIRE),
        (419, POST_WINDOW_PRE_FIRE),
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
        assert classify_evidence_interval(state, t0) == IN_FAULT_WINDOW
        assert classify_evidence_interval(state, t0 + 300) == POST_WINDOW_PRE_FIRE
        assert classify_evidence_interval(state, t0 + 420) == POST_FIRE

    def test_no_armed_fire_leaves_the_post_window_label_honest(self, window):
        # The ChaosBlade path times out inside the experiment and never arms a
        # carrier. Claiming a fire boundary that is not on record would be a
        # fabricated fact, so the label says only what is known.
        state = dict(window["state"], execution_artifacts=[])
        assert classify_evidence_interval(state, window["t0_epoch"] + 900) == POST_WINDOW

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
        assert msg.additional_kwargs[EVIDENCE_INTERVAL_KEY] == IN_FAULT_WINDOW
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
        assert msg.additional_kwargs[EVIDENCE_INTERVAL_KEY] == IN_FAULT_WINDOW

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
        # the window facts the coordinates are measured against
        assert "fault window 300s" in out
        assert IN_FAULT_WINDOW in out and POST_FIRE in out
        assert "+274s since injection" in out
        assert "+446s since injection" in out

    def test_pre_onset_receipt_reads_as_before_injection(self, window):
        state = window["state"]
        msg = ToolMessage(content="{}", tool_call_id="a", name="kubectl")
        stamp_tool_results({"messages": [msg]}, state, now=window["t0_epoch"] - 30)
        state["messages"] = [msg]
        out = render_evidence_timeline(state)
        assert "30s before injection" in out
        assert PRE_ONSET in out

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
