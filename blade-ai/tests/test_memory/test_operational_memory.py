"""Tests for operational memory (MEMORY.md)."""

import pytest

from chaos_agent.memory.operational_memory import (
    OperationalMemory,
    DEFAULT_MEMORY_CONTENT,
    MEMORY_TYPES,
    MEMORY_SAVE_GUIDANCE,
    MEMORY_ACCESS_GUIDANCE,
    VALID_MEMORY_TYPES,
)


class TestOperationalMemoryRead:
    """Test reading operational memory."""

    def test_creates_default_if_not_exists(self, tmp_path):
        mem = OperationalMemory(tmp_path / "subdir" / "MEMORY.md")
        content = mem.read()
        assert "Operational Memory" in content
        assert "User Preferences" in content

    def test_returns_existing_content(self, tmp_path):
        path = tmp_path / "MEMORY.md"
        path.write_text("# Custom Memory\nMy notes", encoding="utf-8")
        mem = OperationalMemory(path)
        content = mem.read()
        assert "Custom Memory" in content


class TestOperationalMemoryWrite:
    """Test writing operational memory."""

    def test_overwrites_content(self, tmp_path):
        path = tmp_path / "MEMORY.md"
        path.write_text("old content", encoding="utf-8")
        mem = OperationalMemory(path)
        mem.write("# New Content\nUpdated")
        assert "New Content" in path.read_text(encoding="utf-8")
        assert "old" not in path.read_text(encoding="utf-8")

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "MEMORY.md"
        mem = OperationalMemory(path)
        mem.write("test content")
        assert path.exists()


class TestOperationalMemoryAppendSection:
    """Test appending to sections."""

    def test_append_to_existing_section(self, tmp_path):
        path = tmp_path / "MEMORY.md"
        path.write_text("# Operational Memory\n\n## Known Issues\n(none)\n\n## Best Practices\n- Rule 1\n", encoding="utf-8")
        mem = OperationalMemory(path)
        mem.append_section("Known Issues", "- Pod X has intermittent issues")
        content = path.read_text(encoding="utf-8")
        assert "Pod X" in content
        assert "Best Practices" in content  # Next section preserved

    def test_create_new_section(self, tmp_path):
        path = tmp_path / "MEMORY.md"
        path.write_text("# Operational Memory\n\n## Known Issues\n(none)\n", encoding="utf-8")
        mem = OperationalMemory(path)
        mem.append_section("Environment", "Cluster: k8s-prod")
        content = path.read_text(encoding="utf-8")
        assert "## Environment" in content
        assert "k8s-prod" in content


# ---------------------------------------------------------------------------
# New tests: Memory type taxonomy (Migration Point 5)
# ---------------------------------------------------------------------------


class TestMemoryTypes:
    """Test the 4-type memory taxonomy aligned with Claude Code memoryTypes.ts."""

    def test_four_types_defined(self):
        assert len(MEMORY_TYPES) == 4
        assert "user_preference" in MEMORY_TYPES
        assert "feedback" in MEMORY_TYPES
        assert "project" in MEMORY_TYPES
        assert "reference" in MEMORY_TYPES

    def test_each_type_has_required_fields(self):
        for name, definition in MEMORY_TYPES.items():
            assert "description" in definition, f"{name} missing description"
            assert "when_to_save" in definition, f"{name} missing when_to_save"
            assert "body_structure" in definition, f"{name} missing body_structure"
            assert "example" in definition, f"{name} missing example"

    def test_valid_memory_types_matches_keys(self):
        assert VALID_MEMORY_TYPES == set(MEMORY_TYPES.keys())


class TestMemoryGuidance:
    """Test memory save/access/trust guidance sections."""

    def test_save_guidance_mentions_what_not_to_save(self):
        assert "NOT to save" in MEMORY_SAVE_GUIDANCE
        assert "Transient state" in MEMORY_SAVE_GUIDANCE

    def test_save_guidance_mentions_redundancy(self):
        assert "Redundant" in MEMORY_SAVE_GUIDANCE

    def test_access_guidance_mentions_when_to_access(self):
        assert "When to access" in MEMORY_ACCESS_GUIDANCE

    def test_access_guidance_mentions_trusting(self):
        assert "Trusting" in MEMORY_ACCESS_GUIDANCE
        assert "not the same as" in MEMORY_ACCESS_GUIDANCE


class TestDefaultMemoryContent:
    """Test DEFAULT_MEMORY_CONTENT uses the 4-type taxonomy."""

    def test_contains_all_four_sections(self):
        assert "## User Preferences" in DEFAULT_MEMORY_CONTENT
        assert "## Feedback" in DEFAULT_MEMORY_CONTENT
        assert "## Project Knowledge" in DEFAULT_MEMORY_CONTENT
        assert "## Reference Commands" in DEFAULT_MEMORY_CONTENT


class TestSaveTypedMemory:
    """Test save_typed_memory() maps memory types to sections."""

    def test_save_user_preference(self, tmp_path):
        path = tmp_path / "MEMORY.md"
        mem = OperationalMemory(path)
        mem.save_typed_memory("user_preference", "Always confirm before injection")
        content = mem.read()
        assert "Always confirm before injection" in content
        assert "## User Preferences" in content

    def test_save_feedback(self, tmp_path):
        path = tmp_path / "MEMORY.md"
        mem = OperationalMemory(path)
        mem.save_typed_memory("feedback", "pod-kill on single-replica deployment causes outage")
        content = mem.read()
        assert "pod-kill on single-replica deployment causes outage" in content
        assert "## Feedback" in content

    def test_save_project(self, tmp_path):
        path = tmp_path / "MEMORY.md"
        mem = OperationalMemory(path)
        mem.save_typed_memory("project", "Production namespace: prod")
        content = mem.read()
        assert "Production namespace: prod" in content
        assert "## Project Knowledge" in content

    def test_save_reference(self, tmp_path):
        path = tmp_path / "MEMORY.md"
        mem = OperationalMemory(path)
        mem.save_typed_memory("reference", "blade_create network-delay requires --interface")
        content = mem.read()
        assert "blade_create network-delay requires --interface" in content
        assert "## Reference Commands" in content

    def test_invalid_type_raises_error(self, tmp_path):
        path = tmp_path / "MEMORY.md"
        mem = OperationalMemory(path)
        with pytest.raises(ValueError, match="Invalid memory type"):
            mem.save_typed_memory("invalid_type", "some content")

    def test_multiple_saves_to_same_section(self, tmp_path):
        path = tmp_path / "MEMORY.md"
        mem = OperationalMemory(path)
        mem.save_typed_memory("feedback", "Rule 1")
        mem.save_typed_memory("feedback", "Rule 2")
        content = mem.read()
        assert "Rule 1" in content
        assert "Rule 2" in content


class TestSearchMemories:
    """Test search_memories() keyword search."""

    def test_search_finds_matching_entries(self, tmp_path):
        path = tmp_path / "MEMORY.md"
        mem = OperationalMemory(path)
        mem.save_typed_memory("feedback", "pod-kill on single replica causes outage")
        results = mem.search_memories("pod-kill")
        assert len(results) > 0
        assert any("pod-kill" in r for r in results)

    def test_search_empty_query(self, tmp_path):
        path = tmp_path / "MEMORY.md"
        mem = OperationalMemory(path)
        results = mem.search_memories("")
        assert results == []

    def test_search_no_match(self, tmp_path):
        path = tmp_path / "MEMORY.md"
        mem = OperationalMemory(path)
        mem.save_typed_memory("feedback", "pod-kill causes outage")
        results = mem.search_memories("network-delay")
        assert results == []

    def test_search_multi_term_matches_any(self, tmp_path):
        path = tmp_path / "MEMORY.md"
        mem = OperationalMemory(path)
        mem.save_typed_memory("feedback", "pod-kill on deployment")
        results = mem.search_memories("pod-kill outage")
        # Should match on "pod-kill" even though "outage" isn't in the text
        assert any("pod-kill" in r for r in results)

    def test_search_skips_section_headers(self, tmp_path):
        path = tmp_path / "MEMORY.md"
        mem = OperationalMemory(path)
        results = mem.search_memories("Feedback")
        # Section headers should be excluded
        assert not any(r.startswith("## ") for r in results)

    def test_search_skips_placeholder_lines(self, tmp_path):
        path = tmp_path / "MEMORY.md"
        mem = OperationalMemory(path)
        results = mem.search_memories("No")
        # Placeholder lines like "(No preferences recorded yet)" should be excluded
        assert not any(r.startswith("(") for r in results)
