"""Tests for CLI AgentClient (HTTP wrapper)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chaos_agent.cli.client import AgentClient


class TestAgentClientInit:
    def test_default_base_url(self, mock_settings):
        client = AgentClient()
        assert "localhost" in client.base_url

    def test_custom_base_url(self):
        client = AgentClient(base_url="http://my-host:9999")
        assert client.base_url == "http://my-host:9999"

    def test_base_url_strips_trailing_slash(self):
        client = AgentClient(base_url="http://my-host:9999/")
        assert client.base_url == "http://my-host:9999"

    def test_custom_timeout(self):
        client = AgentClient(timeout=60)
        assert client.timeout == 60

    def test_default_timeout(self, mock_settings):
        client = AgentClient()
        assert client.timeout == 30


class TestAgentClientUrl:
    def test_url_construction(self):
        client = AgentClient(base_url="http://localhost:8089")
        url = client._url("/api/v1/inject")
        assert url == "http://localhost:8089/api/v1/inject"


class TestAgentClientPost:
    @pytest.mark.asyncio
    async def test_post_success(self):
        client = AgentClient(base_url="http://localhost:8089")

        mock_response = MagicMock()
        mock_response.json.return_value = {"code": 0, "message": "success"}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await client.post("/api/v1/inject", {"key": "value"})
            assert result["code"] == 0

    @pytest.mark.asyncio
    async def test_post_connection_error(self):
        import httpx

        client = AgentClient(base_url="http://localhost:8089")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.ConnectError("Connection refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await client.post("/api/v1/inject", {})
            assert result["code"] == 5001
            assert "connect" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_post_timeout(self):
        import httpx

        client = AgentClient(base_url="http://localhost:8089")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("Timed out")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await client.post("/api/v1/inject", {})
            assert result["code"] == 5001
            assert "timed out" in result["message"].lower()


class TestAgentClientGet:
    @pytest.mark.asyncio
    async def test_get_success(self):
        client = AgentClient(base_url="http://localhost:8089")

        mock_response = MagicMock()
        mock_response.json.return_value = {"code": 0, "data": {}}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await client.get("/api/v1/health")
            assert result["code"] == 0


class TestAgentClientConvenience:
    @pytest.mark.asyncio
    async def test_inject_calls_post(self):
        client = AgentClient(base_url="http://localhost:8089")
        client.post = AsyncMock(return_value={"code": 0})

        result = await client.inject(scope="pod", target="pod", action="delete", target_name="my-pod", namespace="default")
        client.post.assert_called_once()
        call_args = client.post.call_args
        assert call_args[0][0] == "/api/v1/inject"

    @pytest.mark.asyncio
    async def test_recover_consumes_stream_result(self):
        client = AgentClient(base_url="http://localhost:8089")

        class _StreamResponse:
            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield 'data: {"type":"result","content":"{\\"status\\":\\"success\\",\\"data\\":{\\"task_id\\":\\"task-recover\\",\\"operation\\":\\"recover\\",\\"task_state\\":\\"recovered\\",\\"blade_uid\\":\\"uid-1\\",\\"target\\":{\\"namespace\\":\\"default\\",\\"names\\":[\\"pod-a\\"]}}}"}'

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        class _Client:
            def __init__(self):
                self.stream_args = None
                self.stream_kwargs = None

            def stream(self, *args, **kwargs):
                self.stream_args = args
                self.stream_kwargs = kwargs
                return _StreamResponse()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        fake_client = _Client()
        with patch("httpx.AsyncClient", return_value=fake_client):
            result = await client.recover(task_id="task-123")

        assert result["status"] == "success"
        assert result["code"] == 0
        assert result["data"]["task_id"] == "task-123"
        assert result["data"]["recover_task_id"] == "task-recover"
        assert result["data"]["result"] == "recovered"
        assert result["data"]["targets"] == [{"name": "pod-a", "namespace": "default"}]
        assert fake_client.stream_args[:2] == (
            "POST",
            "http://localhost:8089/api/v1/recover-stream",
        )
        assert fake_client.stream_kwargs["json"]["task_id"] == "task-123"

    @pytest.mark.asyncio
    async def test_metric_with_task_id_calls_get(self):
        client = AgentClient(base_url="http://localhost:8089")
        client.get = AsyncMock(return_value={"code": 0})

        result = await client.metric(task_id="task-123")
        client.get.assert_called_once_with("/api/v1/metric/task-123")

    @pytest.mark.asyncio
    async def test_metric_without_task_id_calls_get(self):
        client = AgentClient(base_url="http://localhost:8089")
        client.get = AsyncMock(return_value={"code": 0})

        result = await client.metric()
        client.get.assert_called_once_with("/api/v1/metric")

    @pytest.mark.asyncio
    async def test_metric_calls_get(self):
        client = AgentClient(base_url="http://localhost:8089")
        client.get = AsyncMock(return_value={"code": 0})

        result = await client.metric(task_id="task-123")
        client.get.assert_called_once_with("/api/v1/metric/task-123")

    @pytest.mark.asyncio
    async def test_list_skills_calls_get(self):
        client = AgentClient(base_url="http://localhost:8089")
        client.get = AsyncMock(return_value={"code": 0})

        result = await client.list_skills()
        client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_confirm_calls_post(self):
        client = AgentClient(base_url="http://localhost:8089")
        client.post = AsyncMock(return_value={"code": 0})

        result = await client.confirm(task_id="task-123", action="approve")
        client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_health_calls_get(self):
        client = AgentClient(base_url="http://localhost:8089")
        client.get = AsyncMock(return_value={"code": 0})

        result = await client.health()
        client.get.assert_called_once_with("/api/v1/health")

    @pytest.mark.asyncio
    async def test_version_calls_get(self):
        client = AgentClient(base_url="http://localhost:8089")
        client.get = AsyncMock(return_value={"code": 0})

        result = await client.version()
        client.get.assert_called_once_with("/api/v1/version")
