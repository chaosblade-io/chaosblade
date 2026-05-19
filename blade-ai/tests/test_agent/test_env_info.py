"""Tests for environment info collection (迁移点 7)."""

from unittest.mock import patch, AsyncMock

import pytest

from chaos_agent.agent.env_info import compute_env_info, clear_env_cache, _get_blade_version, _check_k8s_available


class TestComputeEnvInfo:
    """Test compute_env_info function."""

    def setup_method(self):
        """Clear cache before each test."""
        clear_env_cache()

    def teardown_method(self):
        """Clear cache after each test."""
        clear_env_cache()

    @pytest.mark.asyncio
    async def test_returns_dict(self):
        result = await compute_env_info()
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_contains_model_name(self):
        result = await compute_env_info()
        assert "model_name" in result
        assert isinstance(result["model_name"], str)

    @pytest.mark.asyncio
    async def test_contains_kubeconfig_path(self):
        result = await compute_env_info()
        assert "kubeconfig_path" in result

    @pytest.mark.asyncio
    async def test_contains_kube_context(self):
        result = await compute_env_info()
        assert "kube_context" in result

    @pytest.mark.asyncio
    async def test_blade_version_when_not_installed(self):
        """blade_version should be 'not installed' when blade is not found."""
        with patch("chaos_agent.agent.env_info._get_blade_version", new_callable=AsyncMock) as mock:
            mock.return_value = "not installed"
            result = await compute_env_info()
            assert result["blade_version"] == "not installed"

    @pytest.mark.asyncio
    async def test_blade_version_when_installed(self):
        with patch("chaos_agent.agent.env_info._get_blade_version", new_callable=AsyncMock) as mock:
            mock.return_value = "chaosblade 1.7.0"
            result = await compute_env_info()
            assert result["blade_version"] == "chaosblade 1.7.0"

    @pytest.mark.asyncio
    async def test_k8s_available_false(self):
        with patch("chaos_agent.agent.env_info._check_k8s_available", new_callable=AsyncMock) as mock:
            mock.return_value = False
            result = await compute_env_info()
            assert result["k8s_available"] is False

    @pytest.mark.asyncio
    async def test_k8s_available_true(self):
        with patch("chaos_agent.agent.env_info._check_k8s_available", new_callable=AsyncMock) as mock:
            mock.return_value = True
            result = await compute_env_info()
            assert result["k8s_available"] is True


class TestEnvCache:
    """Test environment info caching behavior."""

    def setup_method(self):
        clear_env_cache()

    def teardown_method(self):
        clear_env_cache()

    @pytest.mark.asyncio
    async def test_cache_by_task_id(self):
        """Same task_id should return cached result."""
        with patch("chaos_agent.agent.env_info._get_blade_version", new_callable=AsyncMock) as mock_blade, \
             patch("chaos_agent.agent.env_info._check_k8s_available", new_callable=AsyncMock) as mock_k8s:
            mock_blade.return_value = "1.7.0"
            mock_k8s.return_value = True

            result1 = await compute_env_info(task_id="task-1")
            result2 = await compute_env_info(task_id="task-1")

            # Should only call once due to caching
            assert mock_blade.call_count == 1
            assert result1 == result2

    @pytest.mark.asyncio
    async def test_different_task_ids_not_shared(self):
        """Different task_ids should have separate caches."""
        with patch("chaos_agent.agent.env_info._get_blade_version", new_callable=AsyncMock) as mock_blade, \
             patch("chaos_agent.agent.env_info._check_k8s_available", new_callable=AsyncMock) as mock_k8s:
            mock_blade.return_value = "1.7.0"
            mock_k8s.return_value = True

            await compute_env_info(task_id="task-1")
            await compute_env_info(task_id="task-2")

            # Should call twice for different task_ids
            assert mock_blade.call_count == 2

    @pytest.mark.asyncio
    async def test_clear_cache_specific_task(self):
        with patch("chaos_agent.agent.env_info._get_blade_version", new_callable=AsyncMock) as mock_blade, \
             patch("chaos_agent.agent.env_info._check_k8s_available", new_callable=AsyncMock) as mock_k8s:
            mock_blade.return_value = "1.7.0"
            mock_k8s.return_value = True

            await compute_env_info(task_id="task-1")
            clear_env_cache(task_id="task-1")
            await compute_env_info(task_id="task-1")

            # Cache was cleared, should call again
            assert mock_blade.call_count == 2

    @pytest.mark.asyncio
    async def test_clear_all_cache(self):
        with patch("chaos_agent.agent.env_info._get_blade_version", new_callable=AsyncMock) as mock_blade, \
             patch("chaos_agent.agent.env_info._check_k8s_available", new_callable=AsyncMock) as mock_k8s:
            mock_blade.return_value = "1.7.0"
            mock_k8s.return_value = True

            await compute_env_info(task_id="task-1")
            await compute_env_info(task_id="task-2")
            clear_env_cache()
            await compute_env_info(task_id="task-1")
            await compute_env_info(task_id="task-2")

            # All caches cleared, should call 4 times
            assert mock_blade.call_count == 4


class TestGetBladeVersion:
    """Test _get_blade_version helper."""

    @pytest.mark.asyncio
    async def test_blade_not_found(self):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            result = await _get_blade_version()
            assert result == "not installed"


class TestCheckK8sAvailable:
    """Test _check_k8s_available helper."""

    @pytest.mark.asyncio
    async def test_kubectl_not_found(self):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            result = await _check_k8s_available()
            assert result is False
