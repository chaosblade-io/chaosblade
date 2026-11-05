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
    async def test_blade_version_host_scope_probes_host_blade(self):
        """D1 regression: in a host-scoped drill the injection runs the REMOTE
        host blade, so blade_version must report the host blade (probed via the
        transport) and label the agent-local one as local — never present the
        local vendor version as if it were the injection executor. That
        conflation is what manufactured the spurious 'agent-side vs host-side
        blade mismatch' narrative (local 1.9.0-alpha vs host 1.8.2)."""
        with patch("chaos_agent.transports.TransportRegistry.resolve") as mock_resolve, \
             patch("chaos_agent.agent.env_info._get_blade_version", new_callable=AsyncMock) as mock_local, \
             patch("chaos_agent.agent.env_info._get_host_blade_version", new_callable=AsyncMock) as mock_host:
            mock_resolve.return_value.name = "ssh"  # real profile_of -> HOST
            mock_local.return_value = "1.9.0-alpha"
            mock_host.return_value = "1.8.2"
            result = await compute_env_info()
        assert result["blade_version"] == (
            "1.8.2 (target host blade; agent-local blade 1.9.0-alpha)"
        )
        mock_host.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_blade_version_host_scope_unprobed_fallback(self):
        """D1: when the host blade probe fails (unreachable host), the field
        must still say the local value is agent-local — never silently present
        it as the host blade."""
        with patch("chaos_agent.transports.TransportRegistry.resolve") as mock_resolve, \
             patch("chaos_agent.agent.env_info._get_blade_version", new_callable=AsyncMock) as mock_local, \
             patch("chaos_agent.agent.env_info._get_host_blade_version", new_callable=AsyncMock) as mock_host:
            mock_resolve.return_value.name = "ssh"
            mock_local.return_value = "1.9.0-alpha"
            mock_host.return_value = ""  # probe failed
            result = await compute_env_info()
        assert result["blade_version"] == (
            "(target host blade unprobed; agent-local blade 1.9.0-alpha)"
        )

    @pytest.mark.asyncio
    async def test_blade_version_non_host_scope_unchanged(self):
        """D1: a non-host channel (local/k8s) executes with the agent-local
        blade, so blade_version stays the plain local value with no host probe
        and no label — the override must not fire outside host scope."""
        with patch("chaos_agent.transports.TransportRegistry.resolve") as mock_resolve, \
             patch("chaos_agent.agent.env_info._get_blade_version", new_callable=AsyncMock) as mock_local, \
             patch("chaos_agent.agent.env_info._get_host_blade_version", new_callable=AsyncMock) as mock_host:
            mock_resolve.return_value.name = "kubeconfig"  # real profile_of -> K8S
            mock_local.return_value = "1.9.0-alpha"
            result = await compute_env_info()
        assert result["blade_version"] == "1.9.0-alpha"
        mock_host.assert_not_awaited()

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

    @pytest.mark.asyncio
    async def test_kube_connection_mode_uses_resolved_channel(self):
        """kube_connection_mode reflects the resolved transport channel name."""
        with patch("chaos_agent.transports.TransportRegistry.resolve") as mock_resolve:
            mock_resolve.return_value.name = "kubewiz_k8s"
            result = await compute_env_info()
        assert result["kube_connection_mode"] == "kubewiz_k8s"
        # kubewiz channels mask the local kubeconfig fields.
        assert result["kubeconfig_path"] == "(kubewiz)"
        assert result["kube_context"] == "(kubewiz)"

    @pytest.mark.asyncio
    async def test_resolve_valueerror_degrades_to_unknown(self):
        """An invalid override / under-specified scope raises ValueError in
        resolve(); env_info must degrade to 'unknown' instead of crashing or
        misrepresenting the misconfiguration as a working kubeconfig cluster."""
        with patch(
            "chaos_agent.transports.TransportRegistry.resolve",
            side_effect=ValueError("host scope requires host_name or ssh_host"),
        ):
            result = await compute_env_info()
        assert result["kube_connection_mode"] == "unknown"
        # 'unknown' is not a kubewiz channel, so kubeconfig fields are not masked.
        assert result["kubeconfig_path"] != "(kubewiz)"


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
