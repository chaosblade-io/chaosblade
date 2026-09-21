"""Tests for confirm route: POST /api/v1/confirm/{task_id}.

Connected defect 2 (round-64): the HTTP confirm ack used to carry only
{task_id, action, reason, confirmed_at}, so an HTTP caller could not tell an
injected drill from a rejected one — the docstring's "reflects the final
state" promise had no matching field. The route now reads the resumed graph
through the SAME single-source projection (build_inject_data_from_state) the
CLI runner uses and returns ``task_state``/``result``/``error`` alongside the
ack. These tests guard that shape so a future refactor cannot silently drop
the verdict again.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# A verifier-confirmed run: L1 passed + L2 passed → the single-source
# projection resolves the terminal word to "injected". The resume is terminal
# (next empty), so the snapshot drives the fail-closed terminal branch, not the
# paused branch — faithful to a graph that has run to its own verdict.
_INJECTED_VALUES = {
    "operation": "inject",
    "verification": {
        "level": "verified",
        "layer1": {"status": "passed"},
        "layer2": {"status": "passed"},
    },
}


class _ResumeGraph:
    """Stands in for the pipeline graph after a resume ran it to a verdict."""

    async def ainvoke(self, command, config):
        return dict(_INJECTED_VALUES)

    async def aget_state(self, config):
        return SimpleNamespace(values=dict(_INJECTED_VALUES), next=(), tasks=[])


@pytest.fixture
def client():
    app = FastAPI()
    app.state.agents = {"pipeline": _ResumeGraph()}

    from chaos_agent.server.routes.confirm import confirm_router
    app.include_router(confirm_router)
    return TestClient(app)


class TestConfirmRoute:
    def test_confirm_returns_final_task_state(self, client):
        resp = client.post("/api/v1/confirm/task-approve", json={"action": "approve"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        data = body["data"]
        # The whole point: the verdict rides the confirm ack.
        assert data["task_state"] == "injected"
        assert data["result"] == "injected"
        assert data["action"] == "approve"
        assert data["task_id"] == "task-approve"

    def test_confirm_invalid_action_still_rejected(self, client):
        """The verdict field must not weaken the pre-existing action gate."""
        resp = client.post("/api/v1/confirm/task-x", json={"action": "maybe"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 1001
        assert "task_state" not in (body.get("data") or {})
