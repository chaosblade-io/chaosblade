"""Generic filesystem watcher with debounce.

One ``FileSystemWatcher`` instance + one ``WatchSpec`` = one watch target.
Used by skill / knowledge hot-reload subsystems.

watchdog is the only optional dependency: if it's not installed the watcher
becomes a no-op (start logs a warning, stop is harmless). This keeps
hot-reload from being a hard requirement for minimal-install CI.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class WatchSpec:
    """Configuration for one watch target.

    label
        Human-readable name used in log lines (e.g. ``"Skill"`` / ``"Knowledge"``).
    path
        Directory to watch. Non-existent path → start() logs a warning and no-op.
    file_filter
        Predicate ``(Path) -> bool``. True means the change should trigger reload.
        Called for every non-directory filesystem event under ``path``.
    on_change
        Callable invoked after the debounce window. Runs on the Timer thread —
        callees must be thread-safe (registry implementations already are).
    recursive
        Whether to watch subdirectories.
    debounce_seconds
        Coalescing window for rapid event bursts (e.g. editor's save-tmp-rename
        produces several events; we only want to reload once).
    counter
        Optional ``() -> int`` returning current resource count. When set,
        the post-reload log line shows ``"<label> hot-reloaded: 12 -> 13"``;
        otherwise it just says ``"<label> hot-reloaded"``. Purely for ops
        visibility — doesn't affect reload behavior.
    """

    label: str
    path: Path
    file_filter: Callable[[Path], bool]
    on_change: Callable[[], None]
    recursive: bool = True
    debounce_seconds: float = 0.5
    counter: Optional[Callable[[], int]] = None


class FileSystemWatcher:
    """Generic watchdog wrapper. One instance = one watched directory."""

    def __init__(self, spec: WatchSpec):
        self.spec = spec
        self._observer = None
        self._debounce_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Begin watching. No-op + warning if watchdog is unavailable or
        the target directory does not exist."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            logger.warning(
                "watchdog not installed, %s hot-reload disabled",
                self.spec.label,
            )
            return

        if not self.spec.path.is_dir():
            logger.warning(
                "%s watch path does not exist or is not a directory: %s",
                self.spec.label, self.spec.path,
            )
            return

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                # Directory events are noisy and never meaningful for a
                # registry that scans files — skip them outright.
                if event.is_directory:
                    return
                src = Path(event.src_path) if event.src_path else None
                if src is None or not watcher.spec.file_filter(src):
                    return
                watcher._schedule_reload()

        # Build the Observer in a local var; only assign to self._observer
        # if every setup step succeeds. Otherwise a mid-setup exception
        # (e.g. PermissionError from schedule on a non-readable subdir)
        # would leave self._observer set but unstarted, and stop() would
        # call .stop() on a never-started Observer.
        try:
            observer = Observer()
            observer.schedule(
                _Handler(), str(self.spec.path), recursive=self.spec.recursive,
            )
            observer.daemon = True
            observer.start()
        except Exception as exc:
            logger.warning(
                "%s watcher failed to start on %s: %s",
                self.spec.label, self.spec.path, exc,
            )
            return
        self._observer = observer
        logger.info("%s watcher started on %s", self.spec.label, self.spec.path)

    def stop(self) -> None:
        """Stop watching and cancel any pending debounced reload."""
        with self._lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
                self._debounce_timer = None
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            logger.info("%s watcher stopped", self.spec.label)

    def _schedule_reload(self) -> None:
        """Cancel pending timer and schedule a fresh debounced reload."""
        with self._lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(
                self.spec.debounce_seconds, self._reload,
            )
            # daemon so a hanging reload doesn't block shutdown
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _reload(self) -> None:
        """Invoke ``on_change``; log before/after counts when available.

        Errors are caught and logged — a buggy reload must NOT crash the
        watcher thread, otherwise subsequent changes go unnoticed silently.
        """
        try:
            old = self.spec.counter() if self.spec.counter else None
            self.spec.on_change()
            if old is not None:
                try:
                    new = self.spec.counter()
                    logger.info(
                        "%s hot-reloaded: %d -> %d",
                        self.spec.label, old, new,
                    )
                except Exception:
                    # counter() blew up post-reload — still log the success
                    logger.info("%s hot-reloaded", self.spec.label)
            else:
                logger.info("%s hot-reloaded", self.spec.label)
        except Exception:
            logger.exception("%s hot-reload failed", self.spec.label)
