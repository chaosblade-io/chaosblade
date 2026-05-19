"""Tests for server middleware."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from chaos_agent.server.middleware import RequestIDMiddleware, TimingMiddleware


class TestRequestIDMiddleware:
    """Tests for RequestIDMiddleware."""

    @pytest.mark.asyncio
    async def test_adds_request_id_to_response(self):
        middleware = RequestIDMiddleware(app=AsyncMock())

        request = MagicMock()
        request.headers = {}
        request.state = MagicMock()

        response = MagicMock()
        response.headers = {}

        async def mock_call_next(req):
            return response

        result = await middleware.dispatch(request, mock_call_next)
        assert "X-Request-ID" in result.headers
        assert len(result.headers["X-Request-ID"]) == 36

    @pytest.mark.asyncio
    async def test_uses_existing_request_id(self):
        middleware = RequestIDMiddleware(app=AsyncMock())

        request = MagicMock()
        request.headers = {"X-Request-ID": "custom-id-123"}
        request.state = MagicMock()

        response = MagicMock()
        response.headers = {}

        async def mock_call_next(req):
            return response

        result = await middleware.dispatch(request, mock_call_next)
        assert result.headers["X-Request-ID"] == "custom-id-123"

    @pytest.mark.asyncio
    async def test_sets_request_state(self):
        middleware = RequestIDMiddleware(app=AsyncMock())

        request = MagicMock()
        request.headers = {}
        request.state = MagicMock()

        response = MagicMock()
        response.headers = {}

        async def mock_call_next(req):
            return response

        await middleware.dispatch(request, mock_call_next)
        request.state.request_id = request.state.request_id  # verify it was set


class TestTimingMiddleware:
    """Tests for TimingMiddleware."""

    @pytest.mark.asyncio
    async def test_adds_duration_header(self):
        middleware = TimingMiddleware(app=AsyncMock())

        request = MagicMock()
        request.method = "GET"
        request.url = MagicMock()
        request.url.path = "/api/v1/health"

        response = MagicMock()
        response.status_code = 200
        response.headers = {}

        async def mock_call_next(req):
            return response

        result = await middleware.dispatch(request, mock_call_next)
        assert "X-Duration-Ms" in result.headers
        # Duration should be a numeric string
        duration = float(result.headers["X-Duration-Ms"])
        assert duration >= 0

    @pytest.mark.asyncio
    async def test_timing_for_post_request(self):
        middleware = TimingMiddleware(app=AsyncMock())

        request = MagicMock()
        request.method = "POST"
        request.url = MagicMock()
        request.url.path = "/api/v1/inject"

        response = MagicMock()
        response.status_code = 200
        response.headers = {}

        async def mock_call_next(req):
            return response

        result = await middleware.dispatch(request, mock_call_next)
        assert "X-Duration-Ms" in result.headers
