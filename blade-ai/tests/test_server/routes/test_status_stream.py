"""Tests for SSE status stream endpoint."""

import json

import pytest

from chaos_agent.observability.status_tracker import (
    StatusCategory,
    get_tracker,
    remove_tracker,
)
from chaos_agent.server.routes.status_stream import status_stream


class MockRequest:
    """Minimal mock of FastAPI Request for testing."""

    def __init__(self):
        self._disconnected = False

    async def is_disconnected(self):
        return self._disconnected


class TestStatusStreamFunction:
    """Test the status_stream endpoint function directly (without HTTP client).

    This avoids the complexity of SSE streaming with httpx AsyncClient
    which blocks on infinite keepalive loops.
    """

    @pytest.mark.asyncio
    async def test_returns_streaming_response(self):
        """status_stream should return a StreamingResponse."""
        from fastapi.responses import StreamingResponse

        tracker = get_tracker("sse-func-1")
        tracker.start(StatusCategory.NODE, "test_node", "Test event")
        tracker.complete("Done")

        request = MockRequest()
        response = await status_stream("sse-func-1", request)

        assert isinstance(response, StreamingResponse)
        assert response.media_type == "text/event-stream"
        assert "no-cache" in response.headers.get("Cache-Control", "")

        remove_tracker("sse-func-1")

    @pytest.mark.asyncio
    async def test_event_generator_yields_historical_events(self):
        """The event generator should yield historical events as SSE data."""
        tracker = get_tracker("sse-func-2")
        tracker.start(StatusCategory.NODE, "agent_loop", "Thinking...")
        tracker.complete("Done thinking")

        request = MockRequest()
        response = await status_stream("sse-func-2", request)

        # Read first few chunks from the generator
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
            if len(chunks) >= 2:
                request._disconnected = True  # break the loop
                break

        # First chunk should be a data event
        content = chunks[0] if isinstance(chunks[0], str) else chunks[0].decode()
        assert "data:" in content
        parsed = json.loads(content.removeprefix("data: ").removesuffix("\n\n"))
        assert parsed["source"] == "agent_loop"
        assert parsed["phase"] == "started"

        remove_tracker("sse-func-2")

    @pytest.mark.asyncio
    async def test_event_generator_includes_tool_events(self):
        """Tool events with detail fields should be streamed correctly."""
        tracker = get_tracker("sse-func-3")
        tracker.start(
            StatusCategory.TOOL,
            "blade_create",
            "Creating experiment",
            detail={"command": "blade create pod network delay"},
        )
        tracker.complete("Created", detail={"exit_code": 0})

        request = MockRequest()
        response = await status_stream("sse-func-3", request)

        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
            if len(chunks) >= 1:
                request._disconnected = True
                break

        content = chunks[0] if isinstance(chunks[0], str) else chunks[0].decode()
        parsed = json.loads(content.removeprefix("data: ").removesuffix("\n\n"))
        assert parsed["category"] == "tool"
        assert parsed["detail"]["command"] == "blade create pod network delay"

        remove_tracker("sse-func-3")

    @pytest.mark.asyncio
    async def test_stream_headers(self):
        """Response should have proper SSE headers."""
        tracker = get_tracker("sse-func-4")
        tracker.start(StatusCategory.NODE, "test", "x")
        tracker.complete("y")

        request = MockRequest()
        response = await status_stream("sse-func-4", request)

        assert response.headers.get("Cache-Control") == "no-cache"
        assert response.headers.get("Connection") == "keep-alive"
        assert response.headers.get("X-Accel-Buffering") == "no"

        # Drain the generator to avoid warning
        request._disconnected = True
        async for _ in response.body_iterator:
            break

        remove_tracker("sse-func-4")
