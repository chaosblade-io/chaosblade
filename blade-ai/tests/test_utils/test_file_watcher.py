"""Tests for the generic FileSystemWatcher (utils/file_watcher.py).

These tests do NOT spin up real watchdog Observers. Instead they:
1. Test the no-op paths (missing watchdog / missing path) by
   monkey-patching the import / filesystem.
2. Test debounce + reload + filter + error-handling by directly
   exercising the public ``_schedule_reload`` / ``_reload`` plumbing.

Real watchdog integration is covered implicitly by the skill / knowledge
facade tests when they exercise their respective registries end-to-end.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

import pytest

from chaos_agent.utils.file_watcher import FileSystemWatcher, WatchSpec


def _make_spec(
    tmp_path: Path,
    *,
    on_change=None,
    file_filter=None,
    counter=None,
    debounce_seconds: float = 0.05,
    label: str = "Test",
    recursive: bool = True,
) -> WatchSpec:
    return WatchSpec(
        label=label,
        path=tmp_path,
        file_filter=file_filter or (lambda p: True),
        on_change=on_change or (lambda: None),
        recursive=recursive,
        debounce_seconds=debounce_seconds,
        counter=counter,
    )


class TestStartNoOpPaths:
    """start() must be silent + safe when its preconditions don't hold."""

    def test_missing_watchdog_logs_warning_and_no_ops(
        self, tmp_path, caplog, monkeypatch,
    ):
        # Pretend watchdog isn't installed by stuffing a sentinel into
        # sys.modules that raises ImportError on submodule access.
        class _Blocker:
            def __getattr__(self, name):
                raise ImportError("simulated missing watchdog")

        monkeypatch.setitem(sys.modules, "watchdog", _Blocker())
        monkeypatch.setitem(sys.modules, "watchdog.observers", _Blocker())
        monkeypatch.setitem(sys.modules, "watchdog.events", _Blocker())

        spec = _make_spec(tmp_path, label="WD-Missing")
        watcher = FileSystemWatcher(spec)
        with caplog.at_level(logging.WARNING, logger="chaos_agent.utils.file_watcher"):
            watcher.start()

        assert watcher._observer is None
        assert any(
            "watchdog not installed" in r.message and "WD-Missing" in r.message
            for r in caplog.records
        )
        # stop on a never-started watcher must not blow up
        watcher.stop()

    def test_missing_path_logs_warning_and_no_ops(self, tmp_path, caplog):
        ghost = tmp_path / "does-not-exist"
        spec = _make_spec(ghost, label="Ghost")
        watcher = FileSystemWatcher(spec)
        with caplog.at_level(logging.WARNING, logger="chaos_agent.utils.file_watcher"):
            watcher.start()

        assert watcher._observer is None
        assert any("does not exist" in r.message for r in caplog.records)
        watcher.stop()  # safe no-op

    def test_observer_schedule_failure_keeps_observer_unset(
        self, tmp_path, caplog, monkeypatch,
    ):
        """If Observer.schedule() raises, self._observer must stay None
        so that subsequent stop() doesn't try to stop an unstarted Observer."""
        spec = _make_spec(tmp_path, label="ScheduleFail")
        watcher = FileSystemWatcher(spec)

        from watchdog.observers import Observer

        def boom(*args, **kwargs):
            raise PermissionError("simulated schedule failure")

        monkeypatch.setattr(Observer, "schedule", boom)

        with caplog.at_level(logging.WARNING, logger="chaos_agent.utils.file_watcher"):
            watcher.start()

        assert watcher._observer is None
        assert any(
            "ScheduleFail watcher failed to start" in r.message
            for r in caplog.records
        )
        watcher.stop()  # safe no-op


class TestReloadAndCounter:
    """_reload() invokes on_change, logs before/after counts, swallows errors."""

    def test_reload_calls_on_change(self, tmp_path):
        calls: list[int] = []
        spec = _make_spec(tmp_path, on_change=lambda: calls.append(1))
        FileSystemWatcher(spec)._reload()
        assert calls == [1]

    def test_reload_logs_before_after_counts(self, tmp_path, caplog):
        state = {"n": 5}

        def reload_fn():
            state["n"] = 7

        spec = _make_spec(
            tmp_path,
            on_change=reload_fn,
            counter=lambda: state["n"],
            label="Counted",
        )
        with caplog.at_level(logging.INFO, logger="chaos_agent.utils.file_watcher"):
            FileSystemWatcher(spec)._reload()
        assert any("Counted hot-reloaded: 5 -> 7" in r.message for r in caplog.records)

    def test_reload_without_counter_logs_plain(self, tmp_path, caplog):
        spec = _make_spec(tmp_path, label="Plain", counter=None)
        with caplog.at_level(logging.INFO, logger="chaos_agent.utils.file_watcher"):
            FileSystemWatcher(spec)._reload()
        assert any(
            r.message == "Plain hot-reloaded" for r in caplog.records
        )

    def test_reload_swallows_on_change_exception(self, tmp_path, caplog):
        def boom():
            raise RuntimeError("kaboom")

        spec = _make_spec(tmp_path, on_change=boom, label="Boom")
        # Must not propagate — watcher thread would die otherwise.
        with caplog.at_level(logging.ERROR, logger="chaos_agent.utils.file_watcher"):
            FileSystemWatcher(spec)._reload()
        assert any(
            "Boom hot-reload failed" in r.message for r in caplog.records
        )

    def test_reload_swallows_post_reload_counter_exception(self, tmp_path, caplog):
        state = {"first_call": True}

        def flaky_counter():
            if state["first_call"]:
                state["first_call"] = False
                return 3
            raise RuntimeError("counter broken")

        spec = _make_spec(
            tmp_path,
            on_change=lambda: None,
            counter=flaky_counter,
            label="Flaky",
        )
        with caplog.at_level(logging.INFO, logger="chaos_agent.utils.file_watcher"):
            FileSystemWatcher(spec)._reload()
        # Falls back to plain message rather than crashing.
        assert any(
            r.message == "Flaky hot-reloaded" for r in caplog.records
        )


class TestDebounce:
    """_schedule_reload coalesces rapid bursts into a single reload."""

    def test_burst_collapses_to_single_reload(self, tmp_path):
        calls: list[int] = []
        spec = _make_spec(
            tmp_path,
            on_change=lambda: calls.append(1),
            debounce_seconds=0.05,
        )
        watcher = FileSystemWatcher(spec)

        for _ in range(10):
            watcher._schedule_reload()
            time.sleep(0.005)  # well within debounce window

        # Wait for the (single) timer to fire.
        time.sleep(0.2)
        assert calls == [1]

    def test_stop_cancels_pending_reload(self, tmp_path):
        calls: list[int] = []
        spec = _make_spec(
            tmp_path,
            on_change=lambda: calls.append(1),
            debounce_seconds=0.5,  # long enough we can stop first
        )
        watcher = FileSystemWatcher(spec)
        watcher._schedule_reload()
        watcher.stop()
        time.sleep(0.6)
        assert calls == []  # reload was cancelled

    def test_stop_is_idempotent(self, tmp_path):
        spec = _make_spec(tmp_path)
        watcher = FileSystemWatcher(spec)
        watcher.stop()  # never started — safe
        watcher.stop()  # double stop — safe
