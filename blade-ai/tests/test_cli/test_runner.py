"""Tests for CLI AgentRunner (local execution wrapper)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chaos_agent.cli.runner import AgentRunner


class TestAgentRunnerInit:
    def test_not_initialized_by_default(self):
        runner = AgentRunner()
        assert runner._initialized is False
        assert runner._registry is None
        assert runner._agents is None


class TestAgentRunnerMetric:
    @pytest.mark.asyncio
    async def test_metric_returns_not_found_for_unknown_task(self):
        runner = AgentRunner()
        result = await runner.metric("nonexistent-task")
        assert result["code"] == 2001
        assert "not found" in result["message"].lower() or "Task not found" in result["message"]

    @pytest.mark.asyncio
    async def test_metric_list_all(self):
        runner = AgentRunner()
        result = await runner.metric()
        assert result["code"] == 0
        assert "data" in result


class TestAgentRunnerVersion:
    @pytest.mark.asyncio
    async def test_version_returns_version_info(self):
        runner = AgentRunner()
        runner._initialized = True
        runner._registry = MagicMock()
        runner._registry.__len__ = MagicMock(return_value=5)

        result = await runner.version()
        assert result["code"] == 0
        assert "version" in result["data"]


class TestAgentRunnerListSkills:
    @pytest.mark.asyncio
    async def test_list_skills_returns_categories(self):
        runner = AgentRunner()
        runner._initialized = True

        # Create a mock registry
        from chaos_agent.skills.models import SkillMetadata, SkillParameter
        mock_registry = MagicMock()
        mock_meta = SkillMetadata(
            name="test-skill",
            description="A test skill for unit testing",
            version="1.0",
            category="test",
            target="pod",
            required_tools=["blade", "kubectl"],
            tags=["test"],
            parameters=[
                SkillParameter(
                    name="time",
                    type="int",
                    required=True,
                    description="Delay in milliseconds",
                    example="3000",
                )
            ],
        )
        mock_registry.metadata = {"test-skill": mock_meta}
        mock_registry.__len__ = MagicMock(return_value=1)
        mock_registry.activate = MagicMock(return_value="skill content for testing")
        runner._registry = mock_registry

        # Mock generate_skill_catalog to return a use case
        with patch("chaos_agent.cli.runner.generate_skill_catalog", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = [{
                "category": "Pod_cpu使用率过高",
                "use_case_name": "Pod CPU high",
                "fault_symptom": "CPU fullload",
                "resource_path": "references/catalogue/Pod_cpu使用率过高/Pod_cpu使用率过高_CPU满载.md",
                "example_cmd": 'blade-ai inject -i "帮我注入CPU故障..."',
            }]
            # Mock ChatOpenAI to avoid real LLM call
            with patch("langchain_openai.ChatOpenAI"):
                result = await runner.list_skills()

        assert result["code"] == 0
        assert result["data"]["total"] == 1
        assert len(result["data"]["categories"]) >= 1

    @pytest.mark.asyncio
    async def test_list_skills_with_category_filter(self):
        runner = AgentRunner()
        runner._initialized = True

        from chaos_agent.skills.models import SkillMetadata
        mock_registry = MagicMock()
        mock_meta = SkillMetadata(
            name="test-skill",
            description="A test skill",
            version="1.0",
            category="network",
            target="pod",
            required_tools=["blade"],
            tags=["test"],
            parameters=[],
        )
        mock_registry.metadata = {"test-skill": mock_meta}
        mock_registry.__len__ = MagicMock(return_value=1)
        mock_registry.activate = MagicMock(return_value="skill content")
        runner._registry = mock_registry

        with patch("chaos_agent.cli.runner.generate_skill_catalog", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = []
            with patch("langchain_openai.ChatOpenAI"):
                result = await runner.list_skills(category="network")

        assert result["code"] == 0


class TestAgentRunnerConfirm:
    @pytest.mark.asyncio
    async def test_confirm_invalid_action(self):
        runner = AgentRunner()
        runner._initialized = True

        result = await runner.confirm("task-123", "invalid_action")
        assert result["code"] == 1001
        assert "invalid" in result["message"].lower()
