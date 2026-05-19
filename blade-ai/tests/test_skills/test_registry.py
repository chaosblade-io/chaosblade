"""Tests for SkillRegistry progressive loading."""

from pathlib import Path

import pytest

from chaos_agent.skills.registry import SkillRegistry


class TestRegistryLoadFromDirectory:
    """Test loading skills from a directory."""

    def test_loads_valid_skills(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        assert "test-skill" in registry
        assert len(registry) == 1

    def test_invalid_skills_skipped(self, tmp_skills_dir, tmp_invalid_skill_dir, caplog):
        """Put an invalid skill alongside a valid one."""
        import shutil

        # Copy invalid skill into skills dir
        invalid_dest = tmp_skills_dir / "invalid-skill"
        if not invalid_dest.exists():
            shutil.copytree(tmp_invalid_skill_dir, invalid_dest)

        registry = SkillRegistry()
        with caplog.at_level("WARNING"):
            registry.load_from_directory(tmp_skills_dir)

        # Valid skill loaded, invalid skipped
        assert "test-skill" in registry
        # Invalid skill should have triggered a warning
        assert any("validation failed" in r.message.lower() or "empty name" in r.message.lower() for r in caplog.records)

    def test_nonexistent_directory(self, tmp_path, caplog):
        registry = SkillRegistry()
        with caplog.at_level("WARNING"):
            registry.load_from_directory(tmp_path / "nonexistent")
        assert len(registry) == 0

    def test_empty_directory(self, tmp_path):
        skills_dir = tmp_path / "empty-skills"
        skills_dir.mkdir()
        registry = SkillRegistry()
        registry.load_from_directory(skills_dir)
        assert len(registry) == 0

    def test_duplicate_skill_name_warns_and_keeps_first(self, tmp_skills_dir, caplog):
        """Duplicate skill names: first-loaded wins, later one skipped with WARNING."""
        # Create a second skill dir with the same name "test-skill"
        dup_dir = tmp_skills_dir / "test-skill-copy"
        dup_dir.mkdir()
        (dup_dir / "SKILL.md").write_text(
            "---\n"
            "name: test-skill\n"
            "description: A duplicate skill with the same name\n"
            "version: '2.0'\n"
            "category: test\n"
            "---\n"
            "\n"
            "Duplicate instructions.\n",
            encoding="utf-8",
        )

        registry = SkillRegistry()
        with caplog.at_level("WARNING"):
            registry.load_from_directory(tmp_skills_dir)

        # Only one skill with this name should exist
        assert len(registry) == 1
        assert "test-skill" in registry

        # The first-loaded skill's description should be preserved
        meta = registry.get_metadata("test-skill")
        assert meta.description == "A test skill for unit testing"

        # A warning about the duplicate should have been logged
        assert any(
            "duplicate skill name" in r.message.lower()
            or "duplicate" in r.message.lower() and "test-skill" in r.message.lower()
            for r in caplog.records
        )

    def test_duplicate_skill_name_keeps_sorted_first(self, tmp_path, caplog):
        """With sorted iteration, alphabetically first dir wins."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Create a-skill (alphabetically first)
        a_dir = skills_dir / "a-skill"
        a_dir.mkdir()
        (a_dir / "SKILL.md").write_text(
            "---\n"
            "name: my-skill\n"
            "description: First version from a-skill\n"
            "category: test\n"
            "---\n"
            "\n"
            "First version.\n",
            encoding="utf-8",
        )

        # Create b-skill (alphabetically second, same name)
        b_dir = skills_dir / "b-skill"
        b_dir.mkdir()
        (b_dir / "SKILL.md").write_text(
            "---\n"
            "name: my-skill\n"
            "description: Second version from b-skill\n"
            "category: test\n"
            "---\n"
            "\n"
            "Second version.\n",
            encoding="utf-8",
        )

        registry = SkillRegistry()
        with caplog.at_level("WARNING"):
            registry.load_from_directory(skills_dir)

        assert len(registry) == 1
        meta = registry.get_metadata("my-skill")
        assert meta.description == "First version from a-skill"


class TestRegistryActivate:
    """Test Tier 2 activation."""

    def test_activate_returns_instructions(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        instructions = registry.activate("test-skill")
        assert "Pre-checks" in instructions
        assert "Injection Procedure" in instructions

    def test_activate_caches_result(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        result1 = registry.activate("test-skill")
        result2 = registry.activate("test-skill")
        assert result1 == result2

    def test_activate_nonexistent_skill_raises(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        with pytest.raises(KeyError):
            registry.activate("nonexistent-skill")


class TestRegistryReadResource:
    """Test Tier 3 resource reading."""

    def test_read_existing_resource(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        content = registry.read_resource("test-skill", "scripts/verify.py")
        assert "verify" in content

    def test_read_nonexistent_resource_raises(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        with pytest.raises(FileNotFoundError):
            registry.read_resource("test-skill", "nonexistent.txt")

    def test_read_resource_nonexistent_skill_raises(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        with pytest.raises(KeyError):
            registry.read_resource("no-skill", "file.txt")


class TestRegistryListResources:
    """Test resource enumeration."""

    def test_list_resources(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        resources = registry.list_resources("test-skill")
        assert len(resources) > 0
        assert any("verify.py" in r for r in resources)


class TestRegistryAccessors:
    """Test get_skill, get_metadata, list_skills."""

    def test_get_skill(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        skill = registry.get_skill("test-skill")
        assert skill is not None
        assert skill.metadata.name == "test-skill"

    def test_get_skill_nonexistent(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        assert registry.get_skill("nope") is None

    def test_get_metadata(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        meta = registry.get_metadata("test-skill")
        assert meta is not None
        assert meta.name == "test-skill"

    def test_get_metadata_nonexistent(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        assert registry.get_metadata("nope") is None

    def test_list_skills(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        skills = registry.list_skills()
        assert "test-skill" in skills

    def test_len(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        assert len(registry) == 1

    def test_contains(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        assert "test-skill" in registry
        assert "nope" not in registry


class TestRegistryBuildCatalogPrompt:
    """Test catalog prompt generation."""

    def test_catalog_includes_skill_names(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        prompt = registry.build_catalog_prompt()
        assert "test-skill" in prompt
        assert "A test skill" in prompt


class TestRegistryReload:
    """Test hot-reload functionality."""

    def test_reload_clears_cache(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)

        # Activate to populate cache
        registry.activate("test-skill")
        assert "test-skill" in registry._instructions_cache

        # Reload should clear caches
        registry.reload()
        assert len(registry._instructions_cache) == 0

    def test_reload_reloads_metadata(self, tmp_skills_dir):
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        assert len(registry) == 1

        # Must pass skills_dir since reload clears _skill_dirs first
        registry.reload(skills_dir=tmp_skills_dir)
        assert len(registry) == 1
        assert "test-skill" in registry


class TestRegistryMatchUseCase:
    """Test match_use_case with the real k8s-chaos-skills catalogue."""

    @pytest.fixture
    def registry(self):
        registry = SkillRegistry()
        skills_dir = Path(__file__).resolve().parents[2] / "skills"
        if not (skills_dir / "k8s-chaos-skills" / "SKILL.md").exists():
            pytest.skip("k8s-chaos-skills not found in project skills directory")
        registry.load_from_directory(skills_dir)
        return registry

    def test_pod_network_loss(self, registry):
        result = registry.match_use_case("pod", "network", "loss")
        assert result is not None
        assert "网络丢包" in result

    def test_pod_network_dns(self, registry):
        result = registry.match_use_case("pod", "network", "dns")
        assert result is not None
        assert "网络DNS故障" in result

    def test_pod_network_delay_no_regression(self, registry):
        result = registry.match_use_case("pod", "network", "delay")
        assert result is not None
        assert "网络延迟" in result

    def test_node_disk_fill_no_regression(self, registry):
        result = registry.match_use_case("node", "disk", "fill")
        assert result is not None
        assert "磁盘使用率" in result or "容器运行时" in result

    def test_node_disk_burn_no_regression(self, registry):
        result = registry.match_use_case("node", "disk", "burn")
        assert result is not None
        assert "磁盘IO" in result

    def test_pod_disk_fill(self, registry):
        """Pod disk fill should match Pod_磁盘空间使用率过高, not Pod_磁盘IO过高."""
        result = registry.match_use_case("pod", "disk", "fill")
        assert result is not None
        assert "磁盘空间使用率过高" in result
        assert "应用日志数据积累" in result
        # Ensure no cross-action leakage: burn directory must NOT match
        assert "磁盘IO过高" not in result

    def test_pod_disk_burn(self, registry):
        """Pod disk burn should match Pod_磁盘IO过高, not Pod_磁盘空间使用率过高."""
        result = registry.match_use_case("pod", "disk", "burn")
        assert result is not None
        assert "磁盘IO过高" in result
        assert "异常IO占用" in result
        # Ensure no cross-action leakage: fill directory must NOT match
        assert "磁盘空间使用率过高" not in result

    def test_pod_cpu_fullload(self, registry):
        """Pod cpu fullload should match Pod_cpu使用率过高."""
        result = registry.match_use_case("pod", "cpu", "fullload")
        assert result is not None
        assert "cpu使用率过高" in result

    def test_node_cpu_fullload(self, registry):
        """Node cpu fullload should match Node_CPU使用率过高."""
        result = registry.match_use_case("node", "cpu", "fullload")
        assert result is not None
        assert "CPU使用率过高" in result
