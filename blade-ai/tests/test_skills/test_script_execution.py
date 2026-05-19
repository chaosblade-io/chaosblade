"""Tests for skill script execution functionality."""

import sys
from unittest.mock import AsyncMock

import pytest

from chaos_agent.errors import ScriptExecutionError, ScriptTimeoutError
from chaos_agent.skills.models import ScriptInfo
from chaos_agent.tools.guard import CommandResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def skill_with_scripts(tmp_path):
    """Create a skill directory with Python and Shell scripts."""
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "test-skill"
    skill_dir.mkdir(parents=True)

    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: test-skill\n"
        "description: A test skill with scripts\n"
        "required_tools: [kubectl]\n"
        "scripts:\n"
        "  - name: list_items.py\n"
        "    description: List all items in JSON format\n"
        "    parameters:\n"
        "      - name: format\n"
        "        type: string\n"
        "        required: false\n"
        "        description: Output format\n"
        "  - name: cleanup.sh\n"
        "    description: Clean up temporary files\n"
        "---\n"
        "\n"
        "# Test Skill\n",
        encoding="utf-8",
    )

    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "list_items.py").write_text(
        '#!/usr/bin/env python3\nprint("items")\n', encoding="utf-8"
    )
    (scripts_dir / "cleanup.sh").write_text(
        '#!/bin/bash\necho "cleaned"\n', encoding="utf-8"
    )

    return skills_dir


@pytest.fixture
def registry_with_scripts(skill_with_scripts):
    """Create a SkillRegistry loaded with skill_with_scripts."""
    from chaos_agent.skills.registry import SkillRegistry

    registry = SkillRegistry()
    registry.load_from_directory(skill_with_scripts)
    return registry


# ---------------------------------------------------------------------------
# ScriptInfo model tests
# ---------------------------------------------------------------------------


class TestScriptInfo:
    """Test ScriptInfo data model."""

    def test_defaults(self):
        info = ScriptInfo(name="test.py")
        assert info.name == "test.py"
        assert info.description == ""
        assert info.parameters == []
        assert info.interpreter is None
        assert info.timeout is None

    def test_full_construction(self):
        from chaos_agent.skills.models import SkillParameter

        info = ScriptInfo(
            name="run.py",
            description="Run something",
            parameters=[SkillParameter(name="x", type="int", required=True)],
            interpreter="python3",
            timeout=30,
        )
        assert info.name == "run.py"
        assert info.description == "Run something"
        assert len(info.parameters) == 1
        assert info.interpreter == "python3"
        assert info.timeout == 30


# ---------------------------------------------------------------------------
# SkillMetadata.scripts tests
# ---------------------------------------------------------------------------


class TestSkillMetadataScripts:
    """Test that SkillMetadata correctly stores scripts field."""

    def test_scripts_field_default_empty(self, tmp_skills_dir):
        from chaos_agent.skills.registry import SkillRegistry

        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        meta = registry.get_metadata("test-skill")
        assert isinstance(meta.scripts, list)

    def test_scripts_field_parsed_from_frontmatter(self, registry_with_scripts):
        meta = registry_with_scripts.get_metadata("test-skill")
        assert len(meta.scripts) == 2
        assert meta.scripts[0].name == "list_items.py"
        assert meta.scripts[0].description == "List all items in JSON format"
        assert len(meta.scripts[0].parameters) == 1
        assert meta.scripts[1].name == "cleanup.sh"


# ---------------------------------------------------------------------------
# list_skill_scripts / list_scripts tests
# ---------------------------------------------------------------------------


class TestListScripts:
    """Test script listing and auto-discovery."""

    def test_list_scripts_returns_declared_and_discovered(self, registry_with_scripts):
        scripts = registry_with_scripts.list_scripts("test-skill")
        # Both declared scripts should appear
        names = [s["name"] for s in scripts]
        assert "list_items.py" in names
        assert "cleanup.sh" in names

    def test_list_scripts_auto_discovery(self, tmp_skills_dir):
        """Scripts in scripts/ dir not in frontmatter should still be discovered."""
        from chaos_agent.skills.registry import SkillRegistry

        # tmp_skills_dir has verify.py but no scripts frontmatter
        registry = SkillRegistry()
        registry.load_from_directory(tmp_skills_dir)
        scripts = registry.list_scripts("test-skill")
        names = [s["name"] for s in scripts]
        assert "verify.py" in names

    def test_list_scripts_nonexistent_skill_raises(self, registry_with_scripts):
        with pytest.raises(KeyError):
            registry_with_scripts.list_scripts("no-skill")


# ---------------------------------------------------------------------------
# get_script_metadata tests
# ---------------------------------------------------------------------------


class TestGetScriptMetadata:
    """Test retrieving individual script metadata."""

    def test_get_existing_script_metadata(self, registry_with_scripts):
        meta = registry_with_scripts.get_script_metadata("test-skill", "list_items.py")
        assert meta is not None
        assert meta.name == "list_items.py"
        assert meta.description == "List all items in JSON format"

    def test_get_nonexistent_script_metadata(self, registry_with_scripts):
        meta = registry_with_scripts.get_script_metadata("test-skill", "nonexistent.py")
        assert meta is None

    def test_get_script_metadata_nonexistent_skill(self, registry_with_scripts):
        meta = registry_with_scripts.get_script_metadata("no-skill", "list_items.py")
        assert meta is None


# ---------------------------------------------------------------------------
# execute_script security tests
# ---------------------------------------------------------------------------


class TestExecuteScriptSecurity:
    """Test security validation in execute_script."""

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self, registry_with_scripts):
        with pytest.raises(ScriptExecutionError, match="Path traversal"):
            await registry_with_scripts.execute_script(
                "test-skill", "../etc/passwd"
            )

    @pytest.mark.asyncio
    async def test_invalid_extension_blocked(self, registry_with_scripts):
        with pytest.raises(ScriptExecutionError, match="not allowed"):
            await registry_with_scripts.execute_script(
                "test-skill", "malicious.exe"
            )

    @pytest.mark.asyncio
    async def test_dangerous_params_blocked(self, registry_with_scripts):
        with pytest.raises(ScriptExecutionError, match="Dangerous pattern"):
            await registry_with_scripts.execute_script(
                "test-skill", "list_items.py", params="; rm -rf /"
            )

    @pytest.mark.asyncio
    async def test_dangerous_params_pipe_bash(self, registry_with_scripts):
        with pytest.raises(ScriptExecutionError, match="Dangerous pattern"):
            await registry_with_scripts.execute_script(
                "test-skill", "list_items.py", params="| bash"
            )

    @pytest.mark.asyncio
    async def test_dangerous_params_command_substitution(self, registry_with_scripts):
        with pytest.raises(ScriptExecutionError, match="Dangerous pattern"):
            await registry_with_scripts.execute_script(
                "test-skill", "list_items.py", params="$(cat /etc/passwd)"
            )

    @pytest.mark.asyncio
    async def test_nonexistent_skill_raises(self, registry_with_scripts):
        with pytest.raises(KeyError):
            await registry_with_scripts.execute_script(
                "no-skill", "list_items.py"
            )

    @pytest.mark.asyncio
    async def test_nonexistent_script_raises(self, registry_with_scripts):
        with pytest.raises(ScriptExecutionError, match="not found"):
            await registry_with_scripts.execute_script(
                "test-skill", "missing.py"
            )


# ---------------------------------------------------------------------------
# execute_script execution tests
# ---------------------------------------------------------------------------


class TestExecuteScriptExecution:
    """Test actual script execution flow."""

    @pytest.mark.asyncio
    async def test_python_script_uses_sys_executable(
        self, registry_with_scripts, mocker
    ):
        """Python scripts should use sys.executable as interpreter."""
        mock_result = CommandResult(
            exit_code=0, stdout="items", stderr="", duration_ms=50.0
        )
        mock_run = mocker.patch(
            "chaos_agent.skills.registry.run_command",
            new_callable=AsyncMock,
            return_value=mock_result,
        )

        await registry_with_scripts.execute_script(
            "test-skill", "list_items.py"
        )

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == sys.executable
        assert "list_items.py" in cmd[1]

    @pytest.mark.asyncio
    async def test_shell_script_uses_bash(self, registry_with_scripts, mocker):
        """Shell scripts should use bash as interpreter."""
        mock_result = CommandResult(
            exit_code=0, stdout="cleaned", stderr="", duration_ms=50.0
        )
        mock_run = mocker.patch(
            "chaos_agent.skills.registry.run_command",
            new_callable=AsyncMock,
            return_value=mock_result,
        )

        await registry_with_scripts.execute_script(
            "test-skill", "cleanup.sh"
        )

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "bash" in cmd[0] or "sh" in cmd[0]
        assert "cleanup.sh" in cmd[1]

    @pytest.mark.asyncio
    async def test_params_passed_as_cli_args(self, registry_with_scripts, mocker):
        """Params should be split and passed as CLI arguments."""
        mock_result = CommandResult(
            exit_code=0, stdout="items", stderr="", duration_ms=50.0
        )
        mock_run = mocker.patch(
            "chaos_agent.skills.registry.run_command",
            new_callable=AsyncMock,
            return_value=mock_result,
        )

        await registry_with_scripts.execute_script(
            "test-skill", "list_items.py", params="--format json --verbose"
        )

        cmd = mock_run.call_args[0][0]
        assert "--format" in cmd
        assert "json" in cmd
        assert "--verbose" in cmd

    @pytest.mark.asyncio
    async def test_skip_guard_true(self, registry_with_scripts, mocker):
        """run_command should be called with skip_guard=True."""
        mock_result = CommandResult(
            exit_code=0, stdout="ok", stderr="", duration_ms=50.0
        )
        mock_run = mocker.patch(
            "chaos_agent.skills.registry.run_command",
            new_callable=AsyncMock,
            return_value=mock_result,
        )

        await registry_with_scripts.execute_script(
            "test-skill", "list_items.py"
        )

        assert mock_run.call_args[1]["skip_guard"] is True

    @pytest.mark.asyncio
    async def test_kubeconfig_env_injected(self, registry_with_scripts, mocker):
        """KUBECONFIG should be injected when skill requires kubectl."""
        mock_result = CommandResult(
            exit_code=0, stdout="ok", stderr="", duration_ms=50.0
        )
        mock_run = mocker.patch(
            "chaos_agent.skills.registry.run_command",
            new_callable=AsyncMock,
            return_value=mock_result,
        )

        # Set a kubeconfig path via settings
        from chaos_agent.config.settings import settings
        original = settings.kubeconfig_path
        try:
            settings.__dict__["kubeconfig_path"] = "/tmp/test-kubeconfig"
            await registry_with_scripts.execute_script(
                "test-skill", "list_items.py"
            )
            env_override = mock_run.call_args[1]["env_override"]
            assert env_override is not None
            assert "KUBECONFIG" in env_override
        finally:
            settings.__dict__["kubeconfig_path"] = original

    @pytest.mark.asyncio
    async def test_timeout_uses_settings_default(self, registry_with_scripts, mocker):
        """Default timeout should come from settings.timeout_skill_script."""
        mock_result = CommandResult(
            exit_code=0, stdout="ok", stderr="", duration_ms=50.0
        )
        mock_run = mocker.patch(
            "chaos_agent.skills.registry.run_command",
            new_callable=AsyncMock,
            return_value=mock_result,
        )

        await registry_with_scripts.execute_script(
            "test-skill", "list_items.py"
        )

        timeout = mock_run.call_args[1]["timeout"]
        from chaos_agent.config.settings import settings

        assert timeout == settings.timeout_skill_script

    @pytest.mark.asyncio
    async def test_custom_timeout_overrides_default(self, registry_with_scripts, mocker):
        """Explicit timeout should override settings default."""
        mock_result = CommandResult(
            exit_code=0, stdout="ok", stderr="", duration_ms=50.0
        )
        mock_run = mocker.patch(
            "chaos_agent.skills.registry.run_command",
            new_callable=AsyncMock,
            return_value=mock_result,
        )

        await registry_with_scripts.execute_script(
            "test-skill", "list_items.py", timeout=120
        )

        assert mock_run.call_args[1]["timeout"] == 120

    @pytest.mark.asyncio
    async def test_timeout_error_converted(self, registry_with_scripts, mocker):
        """ToolTimeoutError should be converted to ScriptTimeoutError."""
        from chaos_agent.errors import ToolTimeoutError

        mock_run = mocker.patch(
            "chaos_agent.skills.registry.run_command",
            new_callable=AsyncMock,
            side_effect=ToolTimeoutError("timed out"),
        )

        with pytest.raises(ScriptTimeoutError):
            await registry_with_scripts.execute_script(
                "test-skill", "list_items.py"
            )


# ---------------------------------------------------------------------------
# Output formatting tests
# ---------------------------------------------------------------------------


class TestOutputFormatting:
    """Test script output formatting for LLM consumption."""

    @pytest.mark.asyncio
    async def test_success_output_format(self, registry_with_scripts, mocker):
        mock_result = CommandResult(
            exit_code=0, stdout='{"items": []}', stderr="", duration_ms=150.0
        )
        mocker.patch(
            "chaos_agent.skills.registry.run_command",
            new_callable=AsyncMock,
            return_value=mock_result,
        )

        output = await registry_with_scripts.execute_script(
            "test-skill", "list_items.py"
        )

        assert "[Script: list_items.py]" in output
        assert "Exit code: 0" in output
        assert '{"items": []}' in output
        assert "[ERROR]" not in output

    @pytest.mark.asyncio
    async def test_error_output_format(self, registry_with_scripts, mocker):
        mock_result = CommandResult(
            exit_code=1, stdout="", stderr="import error", duration_ms=50.0
        )
        mocker.patch(
            "chaos_agent.skills.registry.run_command",
            new_callable=AsyncMock,
            return_value=mock_result,
        )

        output = await registry_with_scripts.execute_script(
            "test-skill", "list_items.py"
        )

        assert "[ERROR]" in output
        assert "Exit code: 1" in output
        assert "import error" in output

    @pytest.mark.asyncio
    async def test_output_truncation(self, registry_with_scripts, mocker):
        """Long stdout should be truncated."""
        long_output = "x" * 10000
        mock_result = CommandResult(
            exit_code=0, stdout=long_output, stderr="", duration_ms=50.0
        )
        mocker.patch(
            "chaos_agent.skills.registry.run_command",
            new_callable=AsyncMock,
            return_value=mock_result,
        )

        output = await registry_with_scripts.execute_script(
            "test-skill", "list_items.py"
        )

        assert "truncated" in output
        # Output should be shorter than the original
        assert len(output) < len(long_output)

    @pytest.mark.asyncio
    async def test_stderr_omitted_when_empty(self, registry_with_scripts, mocker):
        """STDERR section should be omitted when stderr is empty."""
        mock_result = CommandResult(
            exit_code=0, stdout="ok", stderr="", duration_ms=50.0
        )
        mocker.patch(
            "chaos_agent.skills.registry.run_command",
            new_callable=AsyncMock,
            return_value=mock_result,
        )

        output = await registry_with_scripts.execute_script(
            "test-skill", "list_items.py"
        )

        assert "--- STDERR ---" not in output

    @pytest.mark.asyncio
    async def test_stderr_included_when_present(self, registry_with_scripts, mocker):
        """STDERR section should be included when stderr has content."""
        mock_result = CommandResult(
            exit_code=0, stdout="ok", stderr="warning msg", duration_ms=50.0
        )
        mocker.patch(
            "chaos_agent.skills.registry.run_command",
            new_callable=AsyncMock,
            return_value=mock_result,
        )

        output = await registry_with_scripts.execute_script(
            "test-skill", "list_items.py"
        )

        assert "--- STDERR ---" in output
        assert "warning msg" in output
