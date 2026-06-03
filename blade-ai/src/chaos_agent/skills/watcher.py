"""Skill watcher: hot-reload SkillRegistry on changes under ``skills_dir``.

Thin facade over the generic ``utils.file_watcher.FileSystemWatcher``.
SKILL.md and any file under scripts/references/assets that the registry
might read counts as a change.
"""
from pathlib import Path

from chaos_agent.skills.registry import SkillRegistry
from chaos_agent.utils.file_watcher import FileSystemWatcher, WatchSpec

# Suffixes covered by SkillRegistry's scan path. ``SKILL.md`` is the
# canonical entry point; the others appear inside scripts/ /
# references/ / assets/ subdirectories and a change to any of them can
# invalidate the registry's view of a skill.
_SKILL_FILE_SUFFIXES = (".py", ".sh", ".yaml", ".yml", ".json", ".md")


def _is_skill_file(path: Path) -> bool:
    return path.name == "SKILL.md" or path.suffix in _SKILL_FILE_SUFFIXES


class SkillWatcher:
    """Backward-compatible facade over generic FileSystemWatcher.

    Existing callers do ``SkillWatcher(skills_dir, registry).start()``;
    the internal implementation delegates to FileSystemWatcher with a
    skill-specific filter and counter.
    """

    def __init__(self, skills_dir: Path, registry: SkillRegistry):
        self._impl = FileSystemWatcher(WatchSpec(
            label="Skill",
            path=skills_dir,
            file_filter=_is_skill_file,
            on_change=registry.reload,
            counter=lambda: len(registry),
            recursive=True,
        ))

    def start(self) -> None:
        self._impl.start()

    def stop(self) -> None:
        self._impl.stop()
