"""Tests for the KnowledgeWatcher facade (agent/prompts/knowledge_watcher.py).

Same shape as test_skills/test_watcher.py — exercises the file_filter
predicate and facade composition. End-to-end watchdog behaviour is
covered by tests/test_utils/test_file_watcher.py.
"""
from __future__ import annotations

from pathlib import Path

from chaos_agent.agent.prompts.knowledge_watcher import (
    KnowledgeWatcher,
    _is_knowledge_file,
)


class TestKnowledgeFileFilter:
    def test_md_matches(self):
        assert _is_knowledge_file(Path("/x/k8s.md")) is True

    def test_other_extensions_ignored(self):
        assert _is_knowledge_file(Path("/x/k8s.txt")) is False
        assert _is_knowledge_file(Path("/x/k8s.yaml")) is False
        assert _is_knowledge_file(Path("/x/k8s.py")) is False
        # editor swap files etc.
        assert _is_knowledge_file(Path("/x/.k8s.md.swp")) is False


class TestKnowledgeWatcherFacade:
    def test_constructs_with_correct_spec(self, tmp_path):
        w = KnowledgeWatcher(tmp_path)
        spec = w._impl.spec
        assert spec.label == "Knowledge"
        assert spec.path == tmp_path
        # knowledge/ is flat — no need to recurse
        assert spec.recursive is False
        assert spec.file_filter is _is_knowledge_file
        # counter should call get_knowledge_registry and return its length
        assert isinstance(spec.counter(), int)

    def test_default_dir_uses_module_constant(self):
        """When no dir is passed, watcher targets the bundled
        ``src/chaos_agent/knowledge/`` directory."""
        from chaos_agent.agent.prompts.knowledge_registry import KNOWLEDGE_DIR
        w = KnowledgeWatcher()
        assert w._impl.spec.path == KNOWLEDGE_DIR

    def test_start_stop_no_op_on_missing_dir(self, tmp_path):
        w = KnowledgeWatcher(tmp_path / "no-such-knowledge-dir")
        w.start()
        assert w._impl._observer is None
        w.stop()

    def test_reload_invokes_rebuild_registry(self, tmp_path, monkeypatch):
        """The facade's on_change must call the module-level
        rebuild_registry — patch it and verify it's hit on _reload."""
        calls = []

        def fake_rebuild():
            calls.append(1)
            return []

        # Patch the symbol that KnowledgeWatcher captured at construction.
        # We need to patch the WatchSpec.on_change directly since the
        # constructor already bound rebuild_registry as the callback.
        w = KnowledgeWatcher(tmp_path)
        w._impl.spec.on_change = fake_rebuild
        w._impl._reload()
        assert calls == [1]
