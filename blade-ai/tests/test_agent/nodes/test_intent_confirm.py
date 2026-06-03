"""Tests for intent_confirm node — verifies the interrupt payload, the
post-decision state transitions, and the **Option A handoff**.

Why payload-shape matters: the TUI renderer (tui/renderers/intent_confirm.py)
reads ``intent_confidence`` out of this dict to decide whether to draw the
low-confidence warning row. If the node forgets to forward it, the warning
silently never fires.

Why the Option A handoff matters: the messages-trim and
``bootstrap_task_session`` side effects used to fire from
``intent_clarification`` the moment intent converged, which meant a user
rejection at the confirm gate left the dialogue truncated and an orphan
task file on disk. The approved / dry_run branches now own these side
effects so a rejection is fully reversible. The blast-radius assertions
in ``test_intent_clarification.py`` cover the inverse — that
``intent_clarification`` no longer produces these side effects on the
inject branch — so the two test files together pin the contract from
both sides.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)

from chaos_agent.agent.fault_spec import FaultSpec
from chaos_agent.agent.nodes import intent_confirm as ic_mod
from chaos_agent.agent.nodes.intent_confirm import intent_confirm


def _spec(
    *,
    scope: str = "pod",
    blade_target: str = "cpu",
    blade_action: str = "fullload",
    namespace: str = "cms-demo",
    **kwargs,
) -> dict:
    """Build a ``fault_spec`` state dict in the new (post-refactor)
    serialised shape — what ``intent_clarification`` writes and what
    ``read_fault_spec`` expects to find."""
    spec = FaultSpec(
        namespace=namespace,
        scope=scope,
        blade_target=blade_target,
        blade_action=blade_action,
        **kwargs,
    )
    return spec.to_dict()


def _state(**overrides):
    base = {
        "task_id": "t-confirm-1",
        # State key is ``fault_spec`` after the FaultSpec refactor; the
        # legacy ``fault_intent`` key on state was retired in favour of
        # a single normalised source of truth (``read_fault_spec``).
        "fault_spec": _spec(),
        "intent_confidence": 0.92,
    }
    base.update(overrides)
    return base


class TestIntentConfirmInterruptPayload:
    """interrupt() is monkey-patched to a sentinel-raising lambda that
    captures its argument; we read the dict the node tried to send."""

    @pytest.mark.asyncio
    async def test_payload_carries_intent_confidence(self):
        captured: dict = {}

        def fake_interrupt(info):
            captured.update(info)
            raise RuntimeError("interrupt-stub")

        with patch("chaos_agent.agent.nodes.intent_confirm.interrupt", fake_interrupt):
            with pytest.raises(RuntimeError, match="interrupt-stub"):
                await intent_confirm(_state())

        assert captured["type"] == "intent_confirm"
        assert captured["intent_confidence"] == pytest.approx(0.92)
        # ``fault_type`` is a *derived* property — composed from
        # ``scope-blade_target-blade_action`` inside ``FaultSpec`` (see
        # ``fault_spec.py:fault_type`` @property). Asserting the
        # composed value here pins both the projection (spec →
        # legacy intent dict via ``to_intent_dict``) and the payload
        # wiring (intent dict → confirm SSE).
        assert captured["fault_intent"]["fault_type"] == "pod-cpu-fullload"

    @pytest.mark.asyncio
    async def test_missing_confidence_defaults_to_zero(self):
        """When upstream did not set intent_confidence (e.g. legacy paths),
        the node must coerce to 0.0 rather than propagate None — the
        renderer's ``> 0`` gate relies on a real float."""
        captured: dict = {}

        def fake_interrupt(info):
            captured.update(info)
            raise RuntimeError("interrupt-stub")

        state = _state()
        state.pop("intent_confidence")
        with patch("chaos_agent.agent.nodes.intent_confirm.interrupt", fake_interrupt):
            with pytest.raises(RuntimeError, match="interrupt-stub"):
                await intent_confirm(state)

        assert captured["intent_confidence"] == 0.0


class TestIntentConfirmDecisionRouting:

    @pytest.mark.asyncio
    async def test_approved_emits_handoff_summary(self):
        """After Option A the approved branch is no longer a no-op:
        it commits the inject-pipeline handoff (trim + summary +
        bootstrap_task_session). The full mechanics are exercised in
        ``TestIntentConfirmApprovedHandoff``; here we just pin the
        existence of the marker so a regression to "return {}"
        is caught early.
        """
        with patch.object(ic_mod, "bootstrap_task_session", lambda *_a, **_k: None), \
             patch.object(ic_mod, "interrupt", return_value="approved"):
            result = await intent_confirm(_state())
        msgs = result.get("messages") or []
        sys_msgs = [m for m in msgs if isinstance(m, SystemMessage)]
        assert sys_msgs, "approved branch must emit the IntentClarificationSummary"
        assert str(sys_msgs[0].content).startswith("[Intent Clarification Summary]")

    @pytest.mark.asyncio
    async def test_rejected_clears_confirmed_intent_only(self):
        """Rejection must reset only ``confirmed_intent`` so the router
        takes the END path. The ``fault_spec`` written by the upstream
        ``intent_clarification`` is intentionally preserved — the user
        typically wants to refine the same intent in the next turn
        (change namespace, add a label selector, …), and clearing it
        would force ``intent_clarification`` to re-collect every fact
        from scratch. Pinning this asymmetry stops a future "tidy-up"
        refactor from re-introducing the regression where rejection
        wipes the partial intent state.
        """
        with patch(
            "chaos_agent.agent.nodes.intent_confirm.interrupt",
            return_value="rejected",
        ):
            result = await intent_confirm(_state())
        assert result == {"confirmed_intent": None}
        # Defensive: explicitly assert the spec key is NOT in the delta
        # (so LangGraph's add_messages reducer leaves state.fault_spec
        # untouched). A future regression that adds ``"fault_spec":
        # None`` to the delta would silently nuke the spec — this
        # second assertion catches it independently of dict equality.
        assert "fault_spec" not in result
        assert "fault_intent" not in result


# ---------------------------------------------------------------------------
# Option A — handoff side effects (trim + summary + bootstrap) live HERE,
# not in intent_clarification. The classes below pin the full contract.
# ---------------------------------------------------------------------------


def _make_dialogue_messages(n: int) -> list:
    """Build N alternating Human/AI messages with stable ids so a
    trim-on-id assertion can identify which entries were dropped."""
    out: list = []
    for i in range(n):
        if i % 2 == 0:
            out.append(HumanMessage(content=f"u-{i}", id=f"m-{i}"))
        else:
            out.append(AIMessage(content=f"a-{i}", id=f"m-{i}"))
    return out


def _handoff_state(decision_messages: list, *, dry_run: bool = False) -> dict:
    """Common state shape — ``task_id`` already allocated by clarification,
    ``fault_spec`` populated in the post-refactor serialised shape,
    ``dialogue_round`` set non-zero so the summary's ``Dialogue rounds:
    N`` line is exercised. The spec uses pod/cpu/fullload so the
    summary's "Fault: ..." line reads as "pod-cpu-fullload →
    pod/cpu/fullload @ production" — distinctive enough that a
    spec-projection regression (e.g. ``read_fault_spec`` returning
    None silently) would surface as an empty/unknown summary line and
    fail the prefix assertion.
    """
    return {
        "task_id": "task-deadbeef",
        "tui_session_id": "sess_test",
        "fault_spec": _spec(namespace="production"),
        "intent_confidence": 1.0,
        "dialogue_round": 3,
        "messages": decision_messages,
        "dry_run": dry_run,
    }


class TestIntentConfirmApprovedHandoff:
    @pytest.mark.asyncio
    async def test_trim_drops_all_but_last_four(self, monkeypatch):
        monkeypatch.setattr(ic_mod, "interrupt", lambda *_a, **_k: "approved")
        called: dict = {}

        def fake_bootstrap(op_task_id, *, operation, tui_session_id, handoff_message):
            called["op_task_id"] = op_task_id
            called["operation"] = operation
            called["tui_session_id"] = tui_session_id
            called["handoff_message"] = handoff_message

        monkeypatch.setattr(ic_mod, "bootstrap_task_session", fake_bootstrap)

        # 6 messages — trim window keeps last 4, drops first 2.
        state = _handoff_state(_make_dialogue_messages(6))
        result = await intent_confirm(state)

        # bootstrap_task_session fires exactly once with the inject tag
        # and the same summary that lands in the messages delta.
        assert called["op_task_id"] == "task-deadbeef"
        assert called["operation"] == "inject"
        assert called["tui_session_id"] == "sess_test"
        assert isinstance(called["handoff_message"], SystemMessage)
        assert str(called["handoff_message"].content).startswith(
            "[Intent Clarification Summary]"
        )

        delta = result.get("messages") or []
        remove_msgs = [m for m in delta if isinstance(m, RemoveMessage)]
        sys_msgs = [m for m in delta if isinstance(m, SystemMessage)]
        assert len(remove_msgs) == 2
        assert {m.id for m in remove_msgs} == {"m-0", "m-1"}
        assert len(sys_msgs) == 1
        # The on-disk seed and the in-memory marker are the same object.
        assert sys_msgs[0] is called["handoff_message"]
        # Summary content must reflect the spec actually written in
        # ``_handoff_state`` (pod-cpu-fullload @ production). A stealth
        # regression where ``read_fault_spec`` silently returns None
        # (e.g. wrong state key, broken from_dict) would produce
        # "Fault: unknown → // @ " here; pinning the composed fault
        # type string catches that path independently of the prefix.
        summary_text = str(sys_msgs[0].content)
        assert "Fault: pod-cpu-fullload" in summary_text
        assert "pod/cpu/fullload @ production" in summary_text
        assert "Dialogue rounds: 3" in summary_text

    @pytest.mark.asyncio
    async def test_short_history_skips_trim_but_emits_summary(self, monkeypatch):
        """When working memory has ≤4 messages the trim is a no-op,
        but the summary still lands so downstream consumers always see
        the dialogue→execution boundary."""
        monkeypatch.setattr(ic_mod, "interrupt", lambda *_a, **_k: "approved")
        monkeypatch.setattr(ic_mod, "bootstrap_task_session", lambda *_a, **_k: None)

        state = _handoff_state(_make_dialogue_messages(2))
        result = await intent_confirm(state)

        delta = result.get("messages") or []
        assert [m for m in delta if isinstance(m, RemoveMessage)] == []
        sys_msgs = [m for m in delta if isinstance(m, SystemMessage)]
        assert len(sys_msgs) == 1
        assert str(sys_msgs[0].content).startswith("[Intent Clarification Summary]")

    @pytest.mark.asyncio
    async def test_missing_task_id_skips_bootstrap(self, monkeypatch):
        """Defensive: if upstream forgot to allocate a ``task-<hex>``
        the confirm node still produces the messages delta (so the
        graph keeps running) but skips ``bootstrap_task_session``
        rather than passing it an empty id."""
        monkeypatch.setattr(ic_mod, "interrupt", lambda *_a, **_k: "approved")
        called: dict = {"n": 0}
        monkeypatch.setattr(
            ic_mod, "bootstrap_task_session",
            lambda *_a, **_k: called.__setitem__("n", called["n"] + 1),
        )

        state = _handoff_state(_make_dialogue_messages(2))
        state["task_id"] = ""
        await intent_confirm(state)
        assert called["n"] == 0


class TestIntentConfirmRejectedPreservesDialogue:
    @pytest.mark.asyncio
    async def test_rejection_does_not_touch_messages_or_bootstrap(self, monkeypatch):
        """The whole point of Option A: rejection leaves the working
        message list untouched (so the next conversational turn can
        iterate on already-established context) and produces no on-disk
        task file (so a discarded intent doesn't leave clutter behind).
        """
        monkeypatch.setattr(ic_mod, "interrupt", lambda *_a, **_k: "rejected")
        bootstrap_called: dict = {"n": 0}
        monkeypatch.setattr(
            ic_mod, "bootstrap_task_session",
            lambda *_a, **_k: bootstrap_called.__setitem__("n", bootstrap_called["n"] + 1),
        )

        state = _handoff_state(_make_dialogue_messages(8))
        result = await intent_confirm(state)

        assert bootstrap_called["n"] == 0
        assert "messages" not in result, (
            "rejection must NOT touch messages — dialogue is preserved "
            "so the next turn iterates on established context"
        )
        assert result.get("confirmed_intent") is None
        # The whole point of Option A: rejection MUST also preserve
        # the spec so the next ``intent_clarification`` round can
        # merge on top of what was already captured rather than
        # re-collecting every fact. A future regression that wipes
        # ``fault_spec`` on reject would re-introduce the "agent
        # forgot the last 5 rounds" UX bug this change fixed.
        assert "fault_spec" not in result


class TestIntentConfirmDryRunHandoff:
    @pytest.mark.asyncio
    async def test_dry_run_commits_handoff_without_interrupt(self, monkeypatch):
        """``state.dry_run=True`` mirrors the approved path: no
        ``interrupt()`` call (the user opted into preview-only via
        /plan), but the inject pipeline still gets the same clean
        handoff so the plan preview is generated against the same
        Phase-1 LLM context the real flow would see.
        """
        called: dict = {"interrupt": 0, "bootstrap": 0}

        def fake_interrupt(*_a, **_k):
            called["interrupt"] += 1
            return "should-not-be-used"

        monkeypatch.setattr(ic_mod, "interrupt", fake_interrupt)
        monkeypatch.setattr(
            ic_mod, "bootstrap_task_session",
            lambda *_a, **_k: called.__setitem__("bootstrap", called["bootstrap"] + 1),
        )

        state = _handoff_state(_make_dialogue_messages(6), dry_run=True)
        result = await intent_confirm(state)

        assert called["interrupt"] == 0, "dry_run must NOT trigger interrupt()"
        assert called["bootstrap"] == 1, "dry_run must still bootstrap the task session"

        delta = result.get("messages") or []
        sys_msgs = [m for m in delta if isinstance(m, SystemMessage)]
        assert len(sys_msgs) == 1
        assert str(sys_msgs[0].content).startswith("[Intent Clarification Summary]")
