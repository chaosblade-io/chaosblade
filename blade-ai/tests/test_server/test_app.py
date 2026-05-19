"""Tests for FastAPI application factory."""

from unittest.mock import MagicMock

import pytest

from chaos_agent.server.app import TaskTracker, create_app


class TestTaskTracker:
    """Tests for TaskTracker."""

    def test_register_task(self):
        tracker = TaskTracker()
        task = MagicMock()
        tracker.register("task-1", task)
        assert "task-1" in tracker._active_tasks

    def test_unregister_task(self):
        tracker = TaskTracker()
        task = MagicMock()
        tracker.register("task-1", task)
        tracker.unregister("task-1")
        assert "task-1" not in tracker._active_tasks

    def test_unregister_nonexistent(self):
        tracker = TaskTracker()
        # Should not raise
        tracker.unregister("nonexistent")

    def test_is_shutting_down_default(self):
        tracker = TaskTracker()
        assert tracker.is_shutting_down is False

    @pytest.mark.asyncio
    async def test_drain_sets_shutting_down(self):
        tracker = TaskTracker()
        await tracker.drain()
        assert tracker.is_shutting_down is True

    @pytest.mark.asyncio
    async def test_drain_with_no_tasks(self):
        tracker = TaskTracker()
        await tracker.drain()
        # Should complete immediately
        assert tracker.is_shutting_down is True

    @pytest.mark.asyncio
    async def test_drain_timeout(self):
        tracker = TaskTracker(drain_timeout=0)

        # Create a task that never completes
        async def infinite_task():
            import asyncio
            await asyncio.sleep(1000)

        import asyncio
        task = asyncio.create_task(infinite_task())
        tracker.register("slow-task", task)

        await tracker.drain()
        assert tracker.is_shutting_down is True
        # Clean up
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class TestCreateApp:
    """Tests for create_app."""

    def test_returns_fastapi_app(self):
        app = create_app()
        from fastapi import FastAPI
        assert isinstance(app, FastAPI)

    def test_app_title(self):
        app = create_app()
        assert app.title == "Chaos Engineering Agent"

    def test_app_has_middleware(self):
        app = create_app()
        # App should have middleware registered
        assert len(app.user_middleware) > 0 or len(app.middleware_stack) > 0

    def test_app_has_routes(self):
        app = create_app()
        routes = [route.path for route in app.routes]
        assert any("/api/v1" in r for r in routes)

    def test_health_endpoint_exists(self):
        app = create_app()
        routes = [route.path for route in app.routes]
        assert "/api/v1/health" in routes

    def test_version_endpoint_exists(self):
        app = create_app()
        routes = [route.path for route in app.routes]
        assert "/api/v1/version" in routes
