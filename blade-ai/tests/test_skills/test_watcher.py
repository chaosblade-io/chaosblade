"""Tests for the SkillWatcher facade (skills/watcher.py).

Validates the file_filter predicate and that the facade composes the
generic FileSystemWatcher correctly. Real watchdog Observer behavior is
covered by tests/test_utils/test_file_watcher.py.
"""
from __future__ import annotations

from pathlib import Path

from chaos_agent.skills.watcher import SkillWatcher, _is_skill_file


class _StubRegistry:
    """Minimal SkillRegistry surface needed by SkillWatcher."""

    def __init__(self):
        self.reload_count = 0
        self._size = 3

    def reload(self):
        self.reload_count += 1

    def __len__(self):
        return self._size


class TestSkillFileFilter:
    def test_skill_md_matches(self):
        assert _is_skill_file(Path("/x/SKILL.md")) is True

    def test_script_extensions_match(self):
        for suffix in (".py", ".sh", ".yaml", ".yml", ".json", ".md"):
            assert _is_skill_file(Path(f"/x/foo{suffix}")) is True, suffix

    def test_unrelated_files_are_ignored(self):
        assert _is_skill_file(Path("/x/note.txt")) is False
        assert _is_skill_file(Path("/x/binary.bin")) is False
        assert _is_skill_file(Path("/x/swap.swp")) is False


class TestSkillWatcherFacade:
    def test_constructs_with_correct_spec(self, tmp_path):
        reg = _StubRegistry()
        w = SkillWatcher(tmp_path, reg)
        spec = w._impl.spec
        assert spec.label == "Skill"
        assert spec.path == tmp_path
        assert spec.recursive is True
        assert spec.file_filter is _is_skill_file
        # on_change is the registry's reload; calling it bumps the count
        spec.on_change()
        assert reg.reload_count == 1
        # counter returns the registry's current __len__
        assert spec.counter() == 3

    def test_start_stop_no_op_on_missing_dir(self, tmp_path):
        """Missing path → start() warns and no-ops; stop() is safe."""
        ghost = tmp_path / "no-such-skills-dir"
        w = SkillWatcher(ghost, _StubRegistry())
        w.start()  # logs warning, _observer stays None
        assert w._impl._observer is None
        w.stop()  # idempotent

    def test_reload_through_facade(self, tmp_path):
        """Trigger _reload via the facade's internal impl — verifies the
        on_change binding survives the facade layer."""
        reg = _StubRegistry()
        w = SkillWatcher(tmp_path, reg)
        w._impl._reload()
        assert reg.reload_count == 1
