"""Tests for Patch E — pipeline attempt tracker."""

from __future__ import annotations

import time

import pytest

from chaos_agent.agent.attempt_tracker import (
    REASON_GRAPH_REPLAN,
    REASON_INITIAL,
    REASON_LLM_TARGET_SWITCH,
    REASON_USER_RERUN,
    begin_attempt,
    detect_target_switch,
    end_attempt,
)


# ---------------------------------------------------------------------------
# begin_attempt — pure function returning delta
# ---------------------------------------------------------------------------


class TestBeginAttempt:
    def test_first_attempt_increments_from_zero(self):
        state = {}
        delta = begin_attempt(
            state, target={"names": ["n1"]}, reason=REASON_INITIAL
        )
        assert delta["pipeline_attempt"] == 1
        assert len(delta["pipeline_attempts_history"]) == 1
        entry = delta["pipeline_attempts_history"][0]
        assert entry["seq"] == 1
        assert entry["target"] == {"names": ["n1"]}
        assert entry["reason"] == REASON_INITIAL
        assert entry["started_at"] > 0
        assert entry["ended_at"] is None
        assert entry["outcome"] is None

    def test_second_attempt_increments(self):
        state = {
            "pipeline_attempt": 1,
            "pipeline_attempts_history": [
                {"seq": 1, "target": {"names": ["n1"]}, "reason": "initial"}
            ],
        }
        delta = begin_attempt(
            state,
            target={"names": ["n2"]},
            reason=REASON_LLM_TARGET_SWITCH,
        )
        assert delta["pipeline_attempt"] == 2
        assert len(delta["pipeline_attempts_history"]) == 2
        # Old entry preserved verbatim, new entry appended
        assert delta["pipeline_attempts_history"][0]["seq"] == 1
        assert delta["pipeline_attempts_history"][1]["seq"] == 2
        assert delta["pipeline_attempts_history"][1]["reason"] == REASON_LLM_TARGET_SWITCH

    def test_does_not_mutate_state(self):
        state = {"pipeline_attempt": 5}
        original = dict(state)
        begin_attempt(state, target=None, reason="x")
        assert state == original

    def test_target_is_deep_copied(self):
        target = {"names": ["n1"], "labels": {"a": "1"}}
        delta = begin_attempt({}, target=target, reason="initial")
        # Mutate the original — history should not change
        target["names"].append("MUTATED")
        target["labels"]["a"] = "MUTATED"
        entry = delta["pipeline_attempts_history"][0]["target"]
        assert entry["names"] == ["n1"]
        assert entry["labels"] == {"a": "1"}

    def test_target_none_recorded(self):
        delta = begin_attempt({}, target=None, reason="initial")
        entry = delta["pipeline_attempts_history"][0]
        assert entry["target"] is None

    def test_notes_recorded(self):
        delta = begin_attempt(
            {}, target=None, reason="x", notes="LLM said: switch to backup"
        )
        entry = delta["pipeline_attempts_history"][0]
        assert entry["notes"] == "LLM said: switch to backup"


# ---------------------------------------------------------------------------
# end_attempt
# ---------------------------------------------------------------------------


class TestEndAttempt:
    def test_no_attempts_returns_empty_delta(self):
        # Idempotent on empty / fresh state
        assert end_attempt({}, outcome="success") == {}

    def test_marks_last_attempt_with_outcome(self):
        state = {
            "pipeline_attempt": 1,
            "pipeline_attempts_history": [
                {
                    "seq": 1,
                    "target": None,
                    "reason": "initial",
                    "notes": "",
                    "started_at": time.time() - 10,
                    "ended_at": None,
                    "outcome": None,
                }
            ],
        }
        delta = end_attempt(state, outcome="success")
        assert "pipeline_attempt" not in delta  # Counter unchanged
        history = delta["pipeline_attempts_history"]
        assert history[-1]["outcome"] == "success"
        assert history[-1]["ended_at"] >= history[-1]["started_at"]

    def test_does_not_mutate_state(self):
        state = {
            "pipeline_attempt": 1,
            "pipeline_attempts_history": [
                {"seq": 1, "outcome": None, "started_at": 1.0}
            ],
        }
        snapshot = list(state["pipeline_attempts_history"])
        end_attempt(state, outcome="failed")
        assert state["pipeline_attempts_history"] == snapshot


# ---------------------------------------------------------------------------
# detect_target_switch
# ---------------------------------------------------------------------------


class TestDetectTargetSwitch:
    def test_same_target_is_not_switch(self):
        t = {"names": ["n1"], "namespace": "default"}
        assert detect_target_switch(t, t) is False
        assert detect_target_switch(t, dict(t)) is False

    def test_different_names_is_switch(self):
        a = {"names": ["n1"], "namespace": "default"}
        b = {"names": ["n2"], "namespace": "default"}
        assert detect_target_switch(a, b) is True

    def test_different_namespace_is_switch(self):
        a = {"names": ["p1"], "namespace": "ns-a"}
        b = {"names": ["p1"], "namespace": "ns-b"}
        assert detect_target_switch(a, b) is True

    def test_different_labels_is_switch(self):
        a = {"labels": {"app": "nginx"}}
        b = {"labels": {"app": "redis"}}
        assert detect_target_switch(a, b) is True

    def test_param_change_is_not_switch(self):
        # Refining params (e.g. cpu_percent 80→90) is NOT a target switch
        a = {"names": ["n"], "params": {"cpu_percent": 80}}
        b = {"names": ["n"], "params": {"cpu_percent": 90}}
        assert detect_target_switch(a, b) is False

    def test_none_inputs(self):
        assert detect_target_switch(None, None) is False
        assert detect_target_switch({"names": ["n"]}, None) is False
        assert detect_target_switch(None, {"names": ["n"]}) is False

    def test_empty_inputs(self):
        assert detect_target_switch({}, {}) is False

    def test_non_dict_inputs(self):
        assert detect_target_switch("foo", {"names": ["n"]}) is False
        assert detect_target_switch({"names": ["n"]}, []) is False


# ---------------------------------------------------------------------------
# Integration: full state evolution across a multi-attempt turn
# ---------------------------------------------------------------------------


class TestSaveMemoryEndsAttempt:
    """Patch E — save_memory must close out the current attempt with
    the right outcome so the history entry has end_at + outcome."""

    @pytest.mark.asyncio
    async def test_save_memory_chat_path_ends_attempt(self, monkeypatch):
        from chaos_agent.agent.nodes import memory_nodes as mn

        # Stub out persistence-side I/O — we only care about the
        # state delta semantics here.
        async def noop(*a, **kw):
            return None

        monkeypatch.setattr(mn, "sync_to_store", noop)
        monkeypatch.setattr(mn, "sync_node_status_to_session", lambda *a, **kw: None)

        state = {
            "task_id": "t-test",
            "confirmed_intent": "chat",
            "pipeline_attempt": 1,
            "pipeline_attempts_history": [
                {"seq": 1, "outcome": None, "started_at": time.time() - 1}
            ],
        }
        out = await mn.save_memory(state)
        # Attempt history must be closed with success outcome
        history = out.get("pipeline_attempts_history") or []
        assert history[-1]["outcome"] == "success"
        assert history[-1]["ended_at"] is not None


class TestStateEvolution:
    """Walk through the user-reported scenario (LLM switches node mid-turn)."""

    def test_initial_then_target_switch(self):
        state: dict = {}
        # Step 1 — agent_loop initial entry
        state.update(begin_attempt(
            state,
            target={"names": ["cn-hongkong.10.0.1.120"]},
            reason=REASON_INITIAL,
        ))
        assert state["pipeline_attempt"] == 1

        # Step 2 — LLM detects DiskPressure, switches to .61
        state.update(begin_attempt(
            state,
            target={"names": ["cn-hongkong.10.0.1.61"]},
            reason=REASON_LLM_TARGET_SWITCH,
            notes="DiskPressure on .120 (103d)",
        ))
        assert state["pipeline_attempt"] == 2

        # Step 3 — turn ends with success on .61
        state.update(end_attempt(state, outcome="success"))

        history = state["pipeline_attempts_history"]
        assert len(history) == 2
        assert history[0]["reason"] == REASON_INITIAL
        assert history[0]["target"]["names"] == ["cn-hongkong.10.0.1.120"]
        assert history[1]["reason"] == REASON_LLM_TARGET_SWITCH
        assert history[1]["target"]["names"] == ["cn-hongkong.10.0.1.61"]
        assert history[1]["outcome"] == "success"
        # Initial attempt was implicitly superseded — we don't backfill
        # outcome, that's a future enhancement (REASON_SUPERSEDED).
        assert history[0]["outcome"] is None
