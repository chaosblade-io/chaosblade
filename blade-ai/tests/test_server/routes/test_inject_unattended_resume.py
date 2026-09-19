"""Unattended AUTO delegation on POST /api/v1/inject (channel convergence).

D4 channel-convergence guard: the unattended channels — CLI streaming, CLI
non-streaming, HTTP SSE, L4 pre_approved, and this route (the last one
wired, 2026-09-01) — must decide the
confirmation_gate resume through the shared ``unattended_resume_value``
helper (AUTO delegation: always ``"approved"``, the manifest is the
authority, the guard enforces the per-name boundary) and fire the resume
whenever the graph is PAUSED, mirroring the CLI non-streaming path
struct-for-struct. The interrupt payload is audit context, never a
precondition: a pause without a payload must not fall back into the
"planned but never executed" limbo this block exists to close.

``confirm=true`` stays the API's client-controlled confirmation contract:
the pause is kept for POST /confirm/{task_id} to resume.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx


def _paused_snapshot(tasks):
    """A graph-state snapshot paused at confirmation_gate."""
    return SimpleNamespace(next=("confirmation_gate",), tasks=tasks)


def _task_with_interrupt(payload):
    return SimpleNamespace(interrupts=[SimpleNamespace(value=payload)])


def _task_without_interrupt():
    return SimpleNamespace(interrupts=None)


def _make_app(paused_tasks):
    from fastapi import FastAPI

    from chaos_agent.server.app import TaskTracker
    from chaos_agent.server.routes.inject import inject_router

    app = FastAPI()
    pipeline = SimpleNamespace(
        ainvoke=AsyncMock(return_value={"status": "done"}),
        aget_state=AsyncMock(return_value=_paused_snapshot(paused_tasks)),
    )
    app.state.agents = {"pipeline": pipeline, "session_store": None}
    tracker = TaskTracker()
    app.state.task_tracker = tracker
    app.include_router(inject_router)
    return app, pipeline, tracker


async def _post_and_drain(app, tracker, *, confirm=False):
    """POST one inject request and wait for the background task to finish.

    The route registers the task on the tracker before responding, and
    unregisters it only from its done-callback — so "absent from the
    tracker" implies "already completed", and "present" can be awaited.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        payload = {"input": "kill the pod my-app in default"}
        if confirm:
            payload["confirm"] = True
        resp = await client.post("/api/v1/inject", json=payload)
    assert resp.status_code == 200
    assert resp.json()["code"] == 0
    task_id = resp.json()["data"]["task_id"]
    task = tracker._active_tasks.get(task_id)
    if task is not None:
        await asyncio.wait_for(task, timeout=5)


async def test_resume_fires_when_pause_has_no_interrupt_payload():
    """A pause without a payload is still a pause — resume it.

    This is the CLI-symmetry contract: the CLI non-streaming path resumes
    whenever ``current_state.next`` is set. Gating the server resume on
    ``interrupt_info is not None`` left a theoretical limbo (paused, no
    payload, never resumed); the payload is audit context only.
    """
    app, pipeline, tracker = _make_app([_task_without_interrupt()])
    await _post_and_drain(app, tracker)

    assert pipeline.ainvoke.await_count == 2
    resume_cmd = pipeline.ainvoke.await_args_list[1].args[0]
    assert getattr(resume_cmd, "resume", None) == "approved"


async def test_widened_delegation_resumes_and_is_audited(caplog):
    """A widened contract auto-approves through the shared helper + audit log.

    No event stream exists on this route, so the auditable ``auto_approved``
    record lands in the logger — the delegation must be on the record even
    with no human at the console.
    """
    payload = {
        "type": "confirmation",
        "plan_summary": "CoreDNS NXDOMAIN via kube-system ConfigMap",
        "write_set_widened": True,
        "mechanism_writes": [
            {"scope": "configmap", "namespace": "kube-system", "names": ["coredns-custom"]},
        ],
    }
    app, pipeline, tracker = _make_app([_task_with_interrupt(payload)])
    with caplog.at_level(logging.INFO, logger="chaos_agent.server.routes.inject"):
        await _post_and_drain(app, tracker)

    assert pipeline.ainvoke.await_count == 2
    resume_cmd = pipeline.ainvoke.await_args_list[1].args[0]
    assert getattr(resume_cmd, "resume", None) == "approved"
    assert "auto_approved" in caplog.text


async def test_plain_payload_resumes_without_audit_noise(caplog):
    """Ordinary payloads resume silently — no event, no noise."""
    payload = {"type": "confirmation", "plan_summary": "pod cpu fullload"}
    app, pipeline, tracker = _make_app([_task_with_interrupt(payload)])
    with caplog.at_level(logging.INFO, logger="chaos_agent.server.routes.inject"):
        await _post_and_drain(app, tracker)

    assert pipeline.ainvoke.await_count == 2
    assert "auto_approved" not in caplog.text


async def test_confirm_true_keeps_the_pause():
    """confirm=true is the client-controlled contract — no unattended resume.

    The pause stays for POST /confirm/{task_id}; the background task ends
    after the first invoke.
    """
    payload = {"type": "confirmation", "plan_summary": "pod cpu fullload"}
    app, pipeline, tracker = _make_app([_task_with_interrupt(payload)])
    await _post_and_drain(app, tracker, confirm=True)

    assert pipeline.ainvoke.await_count == 1
    assert pipeline.aget_state.await_count == 0
