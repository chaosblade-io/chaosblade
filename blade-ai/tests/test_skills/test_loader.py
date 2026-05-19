"""Tests for SKILL.md loader and parser."""

from pathlib import Path

import pytest

from chaos_agent.skills.loader import (
    _parse_parameters,
    get_skills_dir,
    list_skill_resources,
    load_skill_instructions,
    load_skill_metadata,
    load_skill_resource,
    parse_frontmatter,
)


class TestParseFrontmatter:
    """Test YAML frontmatter extraction."""

    def test_valid_frontmatter(self):
        content = """---
name: test-skill
description: A test
category: network
---
## Instructions
Do stuff.
"""
        result = parse_frontmatter(content)
        assert result is not None
        assert result["name"] == "test-skill"
        assert result["description"] == "A test"
        assert result["category"] == "network"

    def test_missing_separator(self):
        content = "No frontmatter here\nJust content"
        result = parse_frontmatter(content)
        assert result is None

    def test_only_one_separator(self):
        content = "---\nname: test\nNo closing separator"
        result = parse_frontmatter(content)
        assert result is None

    def test_invalid_yaml(self):
        content = "---\n: invalid yaml: [\n---\nContent"
        result = parse_frontmatter(content)
        assert result is None

    def test_empty_frontmatter(self):
        content = "---\n---\nContent"
        result = parse_frontmatter(content)
        # Empty YAML between separators parses as None
        assert result is None or result == {}

    def test_frontmatter_with_parameters(self):
        content = """---
name: skill
description: desc
parameters:
  - name: time
    type: int
    required: true
---
Body
"""
        result = parse_frontmatter(content)
        assert result is not None
        assert len(result["parameters"]) == 1
        assert result["parameters"][0]["name"] == "time"


class TestLoadSkillMetadata:
    """Test Tier 1 metadata loading."""

    def test_load_from_valid_skill(self, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "test-skill"
        meta = load_skill_metadata(skill_dir)
        assert meta.name == "test-skill"
        assert meta.description == "A test skill for unit testing"
        assert meta.category == "test"
        assert meta.target == "pod"
        assert "blade" in meta.required_tools

    def test_load_with_parameters(self, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "test-skill"
        meta = load_skill_metadata(skill_dir)
        assert len(meta.parameters) == 1
        assert meta.parameters[0].name == "time"
        assert meta.parameters[0].type == "int"
        assert meta.parameters[0].required is True

    def test_missing_frontmatter_raises(self, tmp_path):
        skill_dir = tmp_path / "bad-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("No frontmatter here", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid or missing"):
            load_skill_metadata(skill_dir)


class TestLoadSkillInstructions:
    """Test Tier 2 instructions loading."""

    def test_extracts_markdown_body(self, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "test-skill"
        instructions = load_skill_instructions(skill_dir)
        assert "Pre-checks" in instructions
        assert "Injection Procedure" in instructions

    def test_empty_body(self, tmp_path):
        skill_dir = tmp_path / "empty-body"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: x\ndescription: x\n---", encoding="utf-8")
        result = load_skill_instructions(skill_dir)
        assert result == ""


class TestLoadSkillResource:
    """Test Tier 3 resource loading."""

    def test_read_existing_resource(self, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "test-skill"
        content = load_skill_resource(skill_dir, "scripts/verify.py")
        assert "verify" in content

    def test_read_reference_file(self, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "test-skill"
        content = load_skill_resource(skill_dir, "references/troubleshooting.md")
        assert "Troubleshooting" in content

    def test_nonexistent_resource_raises(self, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "test-skill"
        with pytest.raises(FileNotFoundError):
            load_skill_resource(skill_dir, "nonexistent.txt")


class TestListSkillResources:
    """Test resource file enumeration."""

    def test_lists_all_resources(self, tmp_skills_dir):
        skill_dir = tmp_skills_dir / "test-skill"
        resources = list_skill_resources(skill_dir)
        assert any("scripts" in r for r in resources)
        assert any("verify.py" in r for r in resources)
        assert any("references" in r for r in resources)

    def test_no_resources(self, tmp_path):
        skill_dir = tmp_path / "bare-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: x\ndescription: x\n---", encoding="utf-8")
        resources = list_skill_resources(skill_dir)
        assert resources == []


class TestGetSkillsDir:
    """Test skills directory resolution."""

    @staticmethod
    def _make_skill_dir(parent: Path, name: str = "test-skill") -> Path:
        """Create a directory with a valid SKILL.md inside."""
        skill_dir = parent / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: test\n---\nbody", encoding="utf-8"
        )
        return parent

    def test_env_var_used_when_no_config_override(self, monkeypatch, tmp_path):
        """When config.json skills_dir is empty, env var takes effect."""
        env_skills = self._make_skill_dir(tmp_path / "env_skills")
        monkeypatch.setattr("chaos_agent.config.settings.settings.skills_dir", Path("/nonexistent/skills"))
        monkeypatch.setenv("BLADE_AI_SKILLS_DIR", str(env_skills))
        result = get_skills_dir()
        assert result == env_skills

    def test_config_overrides_env_var(self, monkeypatch, tmp_path):
        """When config.json sets a skills_dir with skills, it overrides env var."""
        config_skills = self._make_skill_dir(tmp_path / "config_skills")
        monkeypatch.setattr("chaos_agent.config.settings.settings.skills_dir", config_skills)
        monkeypatch.setenv("BLADE_AI_SKILLS_DIR", "/should/not/be/used")
        result = get_skills_dir()
        assert result == config_skills

    def test_empty_dir_skipped(self, monkeypatch, tmp_path):
        """An empty skills_dir should be skipped, falling through to lower priority."""
        empty_dir = tmp_path / "empty_skills"
        empty_dir.mkdir()
        monkeypatch.setattr("chaos_agent.config.settings.settings.skills_dir", empty_dir)
        result = get_skills_dir()
        # Should NOT return the empty dir, should fall through to dev path or fallback
        assert result != empty_dir


class TestParseParameters:
    """Test parameter list parsing."""

    def test_valid_parameters(self):
        data = [
            {"name": "time", "type": "int", "required": True},
            {"name": "offset", "type": "int", "required": False, "default": "0"},
        ]
        params = _parse_parameters(data)
        assert len(params) == 2
        assert params[0].name == "time"
        assert params[0].required is True
        assert params[1].default == "0"

    def test_empty_list(self):
        assert _parse_parameters([]) == []

    def test_none_input(self):
        assert _parse_parameters(None) == []

    def test_non_dict_entry_skipped(self):
        params = _parse_parameters(["not a dict"])
        assert params == []

    def test_partial_dict(self):
        params = _parse_parameters([{"name": "x"}])
        assert len(params) == 1
        assert params[0].name == "x"
        assert params[0].type == "string"
