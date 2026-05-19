"""Skill watcher: monitors skills/ directory for changes and triggers hot reload.

Uses watchdog to monitor filesystem events with debounce (500ms)
to avoid reloading on every intermediate file write.
"""

import logging
import threading
from pathlib import Path
from typing import Optional

from chaos_agent.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


class SkillWatcher:
    """Monitor skills directory for changes and trigger hot reload."""

    def __init__(self, skills_dir: Path, registry: SkillRegistry):
        self.skills_dir = skills_dir
        self.registry = registry
        self._observer = None

    def start(self) -> None:
        """Start monitoring the skills directory."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            logger.warning("watchdog not installed, skill hot-reload disabled")
            return

        class Handler(FileSystemEventHandler):
            """Watchdog handler with debounce for skill directory changes."""

            def __init__(self, watcher: "SkillWatcher"):
                self.watcher = watcher
                self._debounce_timer: Optional[threading.Timer] = None

            def on_any_event(self, event):
                # Only react to file changes, not directory events
                if event.is_directory:
                    return
                # Only react to SKILL.md or files in scripts/references/assets
                src_path = Path(event.src_path) if event.src_path else None
                if src_path and (
                    src_path.name == "SKILL.md"
                    or src_path.suffix in (".py", ".sh", ".yaml", ".yml", ".json", ".md")
                ):
                    # Debounce: 500ms window
                    if self._debounce_timer:
                        self._debounce_timer.cancel()
                    self._debounce_timer = threading.Timer(0.5, self.watcher._reload)
                    self._debounce_timer.start()

        handler = Handler(self)
        self._observer = Observer()
        self._observer.schedule(handler, str(self.skills_dir), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        logger.info(f"Skill watcher started on {self.skills_dir}")

    def stop(self) -> None:
        """Stop monitoring."""
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            logger.info("Skill watcher stopped")

    def _reload(self) -> None:
        """Reload all skills (called by watchdog handler with debounce)."""
        try:
            old_count = len(self.registry)
            self.registry.reload()
            new_count = len(self.registry)
            logger.info(f"Skills hot-reloaded: {old_count} -> {new_count}")
        except Exception as e:
            logger.exception("Skill hot-reload failed")
