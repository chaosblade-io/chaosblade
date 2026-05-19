"""Tests for SessionState reactive model."""

import pytest

from chaos_agent.tui.state import (
    DisplayMode,
    PermissionMode,
    SessionState,
)


class TestSessionStateDefaults:
    def test_initial_permission_mode(self):
        state = SessionState()
        assert state.permission_mode == PermissionMode.CONFIRM

    def test_initial_namespace(self):
        state = SessionState()
        assert state.namespace == "default"

    def test_initial_connection_status(self):
        state = SessionState()
        assert state.connection_status == "unknown"

    def test_initial_config_complete(self):
        state = SessionState()
        assert state.config_complete is False

    def test_initial_active_task_count(self):
        state = SessionState()
        assert state.active_task_count == 0


class TestSessionStateSetters:
    def setup_method(self):
        self.state = SessionState()
        self.notifications = []
        self.state.add_listener(lambda s, f: self.notifications.append(f))

    def test_set_permission_mode(self):
        self.state.set_permission_mode(PermissionMode.AUTO)
        assert self.state.permission_mode == PermissionMode.AUTO
        assert "permission_mode" in self.notifications

    def test_set_namespace(self):
        self.state.set_namespace("kube-system")
        assert self.state.namespace == "kube-system"
        assert "namespace" in self.notifications

    def test_set_cluster_name(self):
        self.state.set_cluster_name("prod-cluster")
        assert self.state.cluster_name == "prod-cluster"
        assert "cluster_name" in self.notifications

    def test_set_active_task(self):
        self.state.set_active_task("task-123")
        assert self.state.active_task_id == "task-123"
        assert "active_task_id" in self.notifications

    def test_set_current_phase(self):
        self.state.set_current_phase("safety")
        assert self.state.current_phase == "safety"
        assert "current_phase" in self.notifications

    def test_set_streaming(self):
        self.state.set_streaming(True)
        assert self.state.is_streaming is True
        assert "is_streaming" in self.notifications

    def test_set_connection_status(self):
        self.state.set_connection_status("connected")
        assert self.state.connection_status == "connected"
        assert "connection_status" in self.notifications

    def test_set_config_complete(self):
        self.state.set_config_complete(True)
        assert self.state.config_complete is True
        assert "config_complete" in self.notifications

    def test_set_active_task_count(self):
        self.state.set_active_task_count(3)
        assert self.state.active_task_count == 3
        assert "active_task_count" in self.notifications


class TestSessionStateCycleMode:
    def test_cycle_confirm_to_auto(self):
        state = SessionState()
        new = state.cycle_permission_mode()
        assert new == PermissionMode.AUTO

    def test_cycle_auto_to_confirm(self):
        # PermissionMode.PLAN was removed in P2; the cycle is now confirm ↔ auto.
        state = SessionState()
        state.permission_mode = PermissionMode.AUTO
        new = state.cycle_permission_mode()
        assert new == PermissionMode.CONFIRM


class TestSessionStateDisplayMode:
    """PR-D1 §17.1 — display-density mode (calm / working / dense)."""

    def test_default_is_working(self):
        # Default lands on the daily-driver mode, not calm: the differentiating
        # blade-ai surfaces (experiment card, failure_reason, confirm risk
        # meter) are visible out of the box.
        state = SessionState()
        assert state.display_mode == DisplayMode.WORKING

    def test_set_display_mode_notifies(self):
        state = SessionState()
        events: list[str] = []
        state.add_listener(lambda s, f: events.append(f))
        state.set_display_mode(DisplayMode.DENSE)
        assert state.display_mode == DisplayMode.DENSE
        assert "display_mode" in events

    def test_set_same_display_mode_does_not_notify(self):
        # Avoid spurious renders when the mode hasn't actually changed.
        state = SessionState()
        events: list[str] = []
        state.add_listener(lambda s, f: events.append(f))
        state.set_display_mode(state.display_mode)
        assert events == []

    def test_cycle_walks_calm_working_dense(self):
        # Order is deliberately the cognitive-load ramp so a tap from the
        # default working lands on dense (next step up), then rolls back
        # to calm (the simplest view).
        state = SessionState()
        assert state.display_mode == DisplayMode.WORKING
        assert state.cycle_display_mode() == DisplayMode.DENSE
        assert state.cycle_display_mode() == DisplayMode.CALM
        assert state.cycle_display_mode() == DisplayMode.WORKING

    def test_cycle_recovers_from_unknown_mode(self):
        # If somehow the mode field gets clobbered (e.g. monkey-patched in a
        # test), the cycle still finds a deterministic next step rather than
        # raising. We re-anchor at "working" so the user lands somewhere sane.
        state = SessionState()

        class _Bogus:
            value = "bogus"

        state.display_mode = _Bogus  # type: ignore[assignment]
        new = state.cycle_display_mode()
        # WORKING is the anchor → the next step is DENSE.
        assert new == DisplayMode.DENSE


class TestSessionStateListeners:
    def test_multiple_listeners(self):
        state = SessionState()
        results = []
        state.add_listener(lambda s, f: results.append(("a", f)))
        state.add_listener(lambda s, f: results.append(("b", f)))
        state.set_namespace("test")
        assert len(results) == 2

    def test_remove_listener(self):
        state = SessionState()
        results = []
        cb = lambda s, f: results.append(f)
        state.add_listener(cb)
        state.set_namespace("test1")
        assert len(results) == 1
        state.remove_listener(cb)
        state.set_namespace("test2")
        assert len(results) == 1

    def test_listener_error_does_not_propagate(self):
        state = SessionState()

        def bad_listener(s, f):
            raise RuntimeError("boom")

        state.add_listener(bad_listener)
        state.set_namespace("test")
        assert state.namespace == "test"
