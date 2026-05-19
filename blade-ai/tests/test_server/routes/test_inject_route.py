"""Tests for inject route: POST /api/v1/inject."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from chaos_agent.server.app import TaskTracker


@pytest.fixture
def test_client():
    """Create a test client without lifespan (avoids real startup)."""
    from fastapi import FastAPI

    app = FastAPI()

    # Set up mock state
    app.state.agents = {"inject": AsyncMock()}
    app.state.task_tracker = TaskTracker()

    from chaos_agent.server.routes.inject import inject_router
    app.include_router(inject_router)

    return TestClient(app)


class TestInjectRoute:
    def test_inject_returns_task_id(self, test_client):
        response = test_client.post(
            "/api/v1/inject",
            json={
                "scope": "pod",
                "target": "pod",
                "action": "delete",
                "target_name": "my-pod",
                "namespace": "default",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert "task_id" in data["data"]
        assert data["data"]["fault_type"] == "pod-pod-delete"

    def test_inject_with_optional_params(self, test_client):
        response = test_client.post(
            "/api/v1/inject",
            json={
                "scope": "pod",
                "target": "network",
                "action": "delay",
                "target_name": "my-app",
                "namespace": "production",
                "duration": 120,
                "params": {"time": 3000},
                "params_flags": ["read", "write"],
                "confirm": True,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["result"] == "pending"
        assert data["data"]["fault_type"] == "pod-network-delay"

    def test_inject_batch_targets(self, test_client):
        response = test_client.post(
            "/api/v1/inject",
            json={
                "scope": "pod",
                "target": "pod",
                "action": "delete",
                "target_name": "pod1,pod2,pod3",
                "namespace": "default",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]["targets"]) == 3

    def test_inject_missing_required_field(self, test_client):
        response = test_client.post(
            "/api/v1/inject",
            json={
                "scope": "pod",
                "target": "cpu",
                # missing action, target_name and namespace
            },
        )
        assert response.status_code == 422  # Validation error

    def test_inject_shutting_down(self, test_client):
        """When server is shutting down, should return error code."""
        from fastapi import FastAPI

        app = FastAPI()
        tracker = TaskTracker()
        tracker._shutting_down = True
        app.state.agents = {"inject": AsyncMock()}
        app.state.task_tracker = tracker

        from chaos_agent.server.routes.inject import inject_router
        app.include_router(inject_router)
        client = TestClient(app)

        response = client.post(
            "/api/v1/inject",
            json={
                "scope": "pod",
                "target": "pod",
                "action": "delete",
                "target_name": "my-pod",
                "namespace": "default",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 5001
        assert "shutting down" in data["message"].lower()

    def test_inject_default_duration(self, test_client):
        response = test_client.post(
            "/api/v1/inject",
            json={
                "scope": "pod",
                "target": "pod",
                "action": "delete",
                "target_name": "my-pod",
                "namespace": "default",
            },
        )
        data = response.json()
        # Default duration should be 600
        assert "task_id" in data["data"]

    def test_inject_nl_field(self, test_client):
        response = test_client.post(
            "/api/v1/inject",
            json={
                "scope": "pod",
                "target": "pod",
                "action": "delete",
                "target_name": "my-pod",
                "namespace": "default",
                "input": "delete my pod in default namespace",
            },
        )
        assert response.status_code == 200

    def test_inject_input_only_mode(self, test_client):
        """NL mode: only --input provided, no structured params required."""
        response = test_client.post(
            "/api/v1/inject",
            json={
                "input": "给 default 命名空间的 my-pod 注入 pod-kill 故障",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["code"] == 0
        assert "task_id" in data["data"]

    def test_inject_input_mode_without_structured_params(self, test_client):
        """NL mode with partial structured params should still work."""
        response = test_client.post(
            "/api/v1/inject",
            json={
                "input": "kill the pod my-app in staging namespace",
                "duration": 120,
                "confirm": True,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["result"] == "pending"
        assert data["data"]["fault_type"] == ""

    def test_inject_neither_input_nor_structured(self, test_client):
        """Neither input nor full structured params should return validation error."""
        response = test_client.post(
            "/api/v1/inject",
            json={
                "scope": "pod",
                # missing target, action, target_name, namespace, no input
            },
        )
        assert response.status_code == 422

    def test_inject_direct_mode(self, test_client):
        """Direct mode with all structured params should succeed."""
        response = test_client.post(
            "/api/v1/inject",
            json={
                "scope": "pod",
                "target": "cpu",
                "action": "fullload",
                "target_name": "my-pod",
                "namespace": "default",
                "direct": True,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["fault_type"] == "pod-cpu-fullload"

    def test_inject_direct_with_input_raises(self, test_client):
        """Direct mode is not compatible with input."""
        response = test_client.post(
            "/api/v1/inject",
            json={
                "input": "kill the pod",
                "direct": True,
            },
        )
        assert response.status_code == 422

    def test_inject_invalid_scope_raises(self, test_client):
        """Invalid scope value should return validation error."""
        response = test_client.post(
            "/api/v1/inject",
            json={
                "scope": "invalid",
                "target": "cpu",
                "action": "fullload",
                "target_name": "my-pod",
                "namespace": "default",
            },
        )
        assert response.status_code == 422
