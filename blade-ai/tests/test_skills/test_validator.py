"""Tests for skill structure validator."""

import pytest

from chaos_agent.skills.validator import SkillValidator


class TestSkillValidatorValid:
    """Test validation of valid skills."""

    def test_valid_skill(self, tmp_skills_dir):
        validator = SkillValidator()
        skill_dir = tmp_skills_dir / "test-skill"
        is_valid, errors = validator.validate(skill_dir)
        assert is_valid is True
        assert errors == []


class TestSkillValidatorInvalid:
    """Test validation of invalid skills."""

    def test_missing_skill_md(self, tmp_path):
        validator = SkillValidator()
        skill_dir = tmp_path / "no-skill-md"
        skill_dir.mkdir()
        is_valid, errors = validator.validate(skill_dir)
        assert is_valid is False
        assert any("SKILL.md not found" in e for e in errors)

    def test_missing_required_field_name(self, tmp_path):
        validator = SkillValidator()
        skill_dir = tmp_path / "no-name"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: has desc but no name\n---\nbody",
            encoding="utf-8",
        )
        is_valid, errors = validator.validate(skill_dir)
        assert is_valid is False
        assert any("name" in e for e in errors)

    def test_missing_required_field_description(self, tmp_path):
        validator = SkillValidator()
        skill_dir = tmp_path / "no-desc"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: has-name\n---\nbody",
            encoding="utf-8",
        )
        is_valid, errors = validator.validate(skill_dir)
        assert is_valid is False
        assert any("description" in e for e in errors)

    def test_invalid_name_format_uppercase(self, tmp_path):
        validator = SkillValidator()
        skill_dir = tmp_path / "bad-name"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            '---\nname: "InvalidName"\ndescription: desc\n---\nbody',
            encoding="utf-8",
        )
        is_valid, errors = validator.validate(skill_dir)
        assert is_valid is False
        assert any("Invalid name format" in e for e in errors)

    def test_invalid_name_format_underscore(self, tmp_path):
        validator = SkillValidator()
        skill_dir = tmp_path / "underscore-name"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            '---\nname: "my_skill"\ndescription: desc\n---\nbody',
            encoding="utf-8",
        )
        is_valid, errors = validator.validate(skill_dir)
        assert is_valid is False
        assert any("Invalid name format" in e for e in errors)

    def test_invalid_script_extension(self, tmp_path):
        validator = SkillValidator()
        skill_dir = tmp_path / "bad-scripts"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test\ndescription: test\n---\nbody",
            encoding="utf-8",
        )
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "run.exe").write_text("binary", encoding="utf-8")

        is_valid, errors = validator.validate(skill_dir)
        assert is_valid is False
        assert any("Unexpected script file type" in e for e in errors)

    def test_invalid_frontmatter(self, tmp_path):
        validator = SkillValidator()
        skill_dir = tmp_path / "bad-yaml"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "No frontmatter at all\nJust plain text",
            encoding="utf-8",
        )
        is_valid, errors = validator.validate(skill_dir)
        assert is_valid is False
        assert any("Invalid or missing YAML frontmatter" in e for e in errors)

    def test_multiple_errors_reported(self, tmp_path):
        """Multiple issues should all be reported."""
        validator = SkillValidator()
        skill_dir = tmp_path / "multi-error"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            '---\nname: "BadName"\n---\nbody',
            encoding="utf-8",
        )
        is_valid, errors = validator.validate(skill_dir)
        assert is_valid is False
        # Should have errors for both invalid name and missing description
        assert len(errors) >= 2


class TestSkillValidatorAllowedScripts:
    """Test script file extension validation."""

    @pytest.mark.parametrize("ext", [".py", ".sh", ".yaml", ".yml", ".json"])
    def test_allowed_script_extensions(self, tmp_path, ext):
        validator = SkillValidator()
        skill_dir = tmp_path / f"skill-{ext.strip('.')}"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test\ndescription: test\n---\nbody",
            encoding="utf-8",
        )
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / f"script{ext}").write_text("content", encoding="utf-8")

        is_valid, errors = validator.validate(skill_dir)
        assert is_valid is True
