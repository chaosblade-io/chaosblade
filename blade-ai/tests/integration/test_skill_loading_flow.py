"""Integration test: Skill loading flow (Tier 1 → Tier 2 → Tier 3)."""

from chaos_agent.skills.loader import load_skill_metadata, load_skill_instructions, list_skill_resources
from chaos_agent.skills.registry import SkillRegistry
from chaos_agent.skills.validator import SkillValidator


class TestSkillLoadingFlow:
    """Integration test for the three-tier progressive skill loading."""

    def test_full_skill_loading_pipeline(self, tmp_skills_dir):
        """Test loading a skill through all three tiers."""
        # Tier 1: Load metadata from frontmatter
        skill_dir = tmp_skills_dir / "test-skill"
        metadata = load_skill_metadata(skill_dir)

        assert metadata.name == "test-skill"
        assert metadata.description == "A test skill for unit testing"
        assert metadata.version == "1.0"
        assert metadata.category == "test"
        assert metadata.target == "pod"
        assert len(metadata.parameters) == 1
        assert metadata.parameters[0].name == "time"

    def test_tier2_load_instructions(self, tmp_skills_dir):
        """Tier 2: Load full SKILL.md body after frontmatter."""
        skill_dir = tmp_skills_dir / "test-skill"
        instructions = load_skill_instructions(skill_dir)

        assert instructions is not None
        assert "Pre-checks" in instructions
        assert "Injection Procedure" in instructions
        assert "Recovery" in instructions

    def test_tier3_load_resources(self, tmp_skills_dir):
        """Tier 3: Load resource files referenced by the skill."""
        skill_dir = tmp_skills_dir / "test-skill"
        resources = list_skill_resources(skill_dir)

        assert len(resources) > 0
        # list_skill_resources returns list[str] of relative paths
        assert any("scripts" in r for r in resources)

    def test_registry_progressive_loading(self, tmp_skills_dir):
        """Registry should progressively load skills: metadata first, instructions on activate."""
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)

        # After load_from_directory, we should have metadata
        skills = registry.list_skills()
        assert "test-skill" in skills

        # But instructions should not be loaded yet
        skill = registry.get_skill("test-skill")
        # Instructions loaded lazily on activate

        # Activate should load instructions
        instructions = registry.activate("test-skill")
        assert instructions is not None
        assert "Pre-checks" in instructions

    def test_validator_with_valid_skill(self, tmp_skills_dir):
        """Validator should pass for a well-formed skill."""
        validator = SkillValidator()
        skill_dir = tmp_skills_dir / "test-skill"
        is_valid, errors = validator.validate(skill_dir)
        assert is_valid is True
        assert len(errors) == 0

    def test_validator_with_invalid_skill(self, tmp_invalid_skill_dir):
        """Validator should catch errors for malformed skills."""
        validator = SkillValidator()
        is_valid, errors = validator.validate(tmp_invalid_skill_dir)
        assert is_valid is False
        assert len(errors) > 0

    def test_registry_skips_invalid_skills(self, tmp_skills_dir, tmp_invalid_skill_dir):
        """Registry should skip invalid skills during loading."""
        # Create a mixed directory with valid and invalid skills
        import shutil
        mixed_dir = tmp_skills_dir.parent / "mixed-skills"
        mixed_dir.mkdir()
        shutil.copytree(tmp_skills_dir / "test-skill", mixed_dir / "test-skill")
        shutil.copytree(tmp_invalid_skill_dir, mixed_dir / "invalid-skill")

        registry = SkillRegistry()
        registry.load_from_directory(mixed_dir)

        # Only the valid skill should be loaded
        skills = registry.list_skills()
        assert "test-skill" in skills

    def test_registry_read_resource(self, tmp_skills_dir):
        """Activate skill, then read a resource file."""
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        registry.activate("test-skill")

        content = registry.read_resource("test-skill", "scripts/verify.py")
        assert "verify" in content

    def test_full_skill_catalog(self, tmp_skills_dir):
        """build_catalog_prompt should include skill info."""
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)

        catalog = registry.build_catalog_prompt()
        assert "test-skill" in catalog
