"""Tests for prerequisites checker."""

from unittest.mock import AsyncMock

from chaos_agent.skills.prerequisites import PrerequisitesChecker
from chaos_agent.skills.registry import SkillRegistry
from chaos_agent.tools.guard import CommandResult


class TestPrerequisitesCheckAll:
    """Test check_all method."""

    async def test_all_tools_available(self, tmp_skills_dir, mocker):
        mocker.patch("shutil.which", return_value="/usr/bin/tool")
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)

        checker = PrerequisitesChecker()
        missing = await checker.check_all(registry)
        assert missing == {}

    async def test_missing_tools_detected(self, tmp_skills_dir, mocker):
        def which_side_effect(tool_name):
            if tool_name == "blade":
                return "/usr/bin/blade"
            return None  # kubectl is missing

        mocker.patch("shutil.which", side_effect=which_side_effect)
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)

        checker = PrerequisitesChecker()
        missing = await checker.check_all(registry)
        assert "kubectl" in missing
        assert "test-skill" in missing["kubectl"]

    async def test_empty_registry(self, mocker):
        mocker.patch("shutil.which", return_value="/usr/bin/tool")
        registry = SkillRegistry()  # empty registry

        checker = PrerequisitesChecker()
        missing = await checker.check_all(registry)
        assert missing == {}


class TestPrerequisitesCheckToolVersion:
    """Test check_tool_version method."""

    async def test_tool_available(self, mocker):
        mocker.patch("shutil.which", return_value="/usr/bin/blade")
        mock_run = mocker.patch(
            "chaos_agent.skills.prerequisites.run_command",
            new_callable=AsyncMock,
            return_value=CommandResult(
                exit_code=0, stdout="chaosblade version 1.7.2", stderr="", duration_ms=10.0
            ),
        )

        checker = PrerequisitesChecker()
        version = await checker.check_tool_version("blade")
        assert version == "chaosblade version 1.7.2"

    async def test_tool_not_available(self, mocker):
        mocker.patch("shutil.which", return_value=None)
        checker = PrerequisitesChecker()
        version = await checker.check_tool_version("blade")
        assert version is None

    async def test_tool_version_command_fails(self, mocker):
        mocker.patch("shutil.which", return_value="/usr/bin/blade")
        mocker.patch(
            "chaos_agent.skills.prerequisites.run_command",
            new_callable=AsyncMock,
            side_effect=Exception("failed"),
        )
        checker = PrerequisitesChecker()
        version = await checker.check_tool_version("blade")
        assert version is None


class TestPrerequisitesStartup:
    """Test check_startup_prerequisites method."""

    async def test_logs_warning_for_missing(self, tmp_skills_dir, mocker, caplog):
        def which_side_effect(tool_name):
            return None  # all tools missing

        mocker.patch("shutil.which", side_effect=which_side_effect)
        mocker.patch(
            "chaos_agent.skills.prerequisites.run_command",
            new_callable=AsyncMock,
            side_effect=Exception("no tools"),
        )
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)

        checker = PrerequisitesChecker()
        with caplog.at_level("WARNING"):
            missing = await checker.check_startup_prerequisites(registry)
        assert len(missing) > 0
