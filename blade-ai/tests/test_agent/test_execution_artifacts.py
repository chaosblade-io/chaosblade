"""Tests for durable execution artifact extraction."""

import time
from unittest.mock import AsyncMock, patch

import pytest

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.execution_artifacts import (
    cleanup_debug_pod_artifacts,
    collect_execution_artifacts,
    find_active_debug_pod,
    parse_debug_pod_metadata,
)


def _debug_messages(*, ready: bool = True):
    meta = (
        '{"name":"node-debugger-n1-abc12","namespace":"kubewiz",'
        '"uid":"uid-1","node":"n1","phase":"Running",'
        f'"ready":{str(ready).lower()},"privileged":true'
        "}"
    )
    return [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "debug",
                    "v_args": (
                        "node/n1 -n kubewiz --profile=sysadmin "
                        "--image=debug -- sleep 900"
                    ),
                },
                "id": "tc-debug",
            }],
        ),
        ToolMessage(
            content=f"created\n[debug-pod-meta: {meta}]",
            name="kubectl",
            tool_call_id="tc-debug",
        ),
    ]


def test_parse_debug_pod_metadata():
    metadata = parse_debug_pod_metadata(_debug_messages()[1].content)
    assert metadata == {
        "name": "node-debugger-n1-abc12",
        "namespace": "kubewiz",
        "uid": "uid-1",
        "node": "n1",
        "phase": "Running",
        "ready": True,
        "privileged": True,
    }


def test_collect_ready_debug_pod_artifact():
    artifacts = collect_execution_artifacts(
        _debug_messages(), task_id="task-1", operation_family="network",
    )
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact["status"] == "active"
    assert artifact["uid"] == "uid-1"
    assert artifact["target"] == {"scope": "node", "name": "n1"}
    assert artifact["operation_family"] == "network"
    assert artifact["debug_profile"] == "sysadmin"
    assert artifact["privileged"] is True
    assert find_active_debug_pod(
        artifacts, "node-debugger-n1-abc12", "kubewiz",
    ) == artifact


def test_pod_scoped_ephemeral_debug_registers_no_deletable_artifact():
    # SAFETY regression: a Pod-scoped ``kubectl debug <pod> --target=`` attaches
    # an ephemeral container to the USER'S workload pod. It must NOT become a
    # debug_pod artifact — otherwise verifier finalize's cleanup would fire
    # ``kubectl delete pod <user-pod>`` and destroy the workload. The meta the
    # tool emits for this case carries ``ephemeral_container``.
    meta = (
        '{"name":"arms-llmfx","namespace":"arms-prom","uid":"u-9","node":"n1",'
        '"ephemeral_container":"debugger-xy12","ready":true,'
        '"privileged":false,"phase":"Running"}'
    )
    messages = [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "debug",
                    "v_args": ("arms-llmfx -n arms-prom --image=img "
                               "--target=app --profile=netadmin -- sleep 1800"),
                },
                "id": "tc-ec",
            }],
        ),
        ToolMessage(
            content=f"Targeting container \"app\".\n[debug-pod-meta: {meta}]",
            name="kubectl",
            tool_call_id="tc-ec",
        ),
    ]
    artifacts = collect_execution_artifacts(
        messages, task_id="task-1", operation_family="network",
    )
    # No debug_pod artifact for the user's workload pod.
    assert not any(
        a.get("type") == "debug_pod" and a.get("name") == "arms-llmfx"
        for a in artifacts
    ), "pod-scoped ephemeral debug must not register a deletable debug_pod artifact"


def test_active_debug_pod_gets_confirmed_live_epoch():
    artifacts = collect_execution_artifacts(
        _debug_messages(), task_id="task-1", operation_family="network",
    )
    epoch = artifacts[0].get("confirmed_live_epoch")
    assert isinstance(epoch, (int, float))
    assert epoch > 0


def test_confirmed_live_epoch_not_advanced_on_replay():
    # The freshness stamp is a durable fact: message history is replayed on
    # every execute-loop iteration, and re-collecting must NOT re-stamp it
    # (otherwise the liveness window would never expire).
    messages = _debug_messages()
    first = collect_execution_artifacts(
        messages, task_id="task-1", operation_family="network",
    )
    original_epoch = first[0]["confirmed_live_epoch"]
    time.sleep(0.01)
    second = collect_execution_artifacts(
        messages, first, task_id="task-1", operation_family="network",
    )
    assert second[0]["confirmed_live_epoch"] == original_epoch


def test_failed_debug_pod_has_no_confirmed_live_epoch():
    artifacts = collect_execution_artifacts(_debug_messages(ready=False))
    assert "confirmed_live_epoch" not in artifacts[0]


def test_unready_debug_pod_is_recorded_but_not_executable():
    artifacts = collect_execution_artifacts(_debug_messages(ready=False))
    assert artifacts[0]["status"] == "failed"
    assert find_active_debug_pod(
        artifacts, "node-debugger-n1-abc12", "kubewiz",
    ) is None


def test_successful_delete_marks_debug_pod_cleaned():
    messages = _debug_messages() + [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "delete",
                    "v_args": "pod node-debugger-n1-abc12 -n kubewiz",
                },
                "id": "tc-delete",
            }],
        ),
        ToolMessage(
            content='pod "node-debugger-n1-abc12" deleted',
            name="kubectl",
            tool_call_id="tc-delete",
        ),
    ]
    artifacts = collect_execution_artifacts(messages)
    assert artifacts[0]["status"] == "cleaned"
    assert artifacts[0]["cleanup_tool_call_id"] == "tc-delete"


@pytest.mark.asyncio
async def test_cleanup_debug_artifacts_is_idempotent():
    artifacts = collect_execution_artifacts(_debug_messages())
    with patch(
        "chaos_agent.agent.nodes.execute._debug_pod.delete_debug_pod",
        new=AsyncMock(return_value="confirmed"),
    ) as delete:
        cleaned, names = await cleanup_debug_pod_artifacts(
            artifacts, kubeconfig="/tmp/kubeconfig", task_id="task-1",
        )
        cleaned_again, names_again = await cleanup_debug_pod_artifacts(
            cleaned, kubeconfig="/tmp/kubeconfig", task_id="task-1",
        )

    delete.assert_awaited_once_with(
        "node-debugger-n1-abc12",
        "/tmp/kubeconfig",
        "task-1",
        namespace="kubewiz",
    )
    assert names == ["node-debugger-n1-abc12"]
    assert cleaned[0]["status"] == "cleaned"
    assert cleaned_again == cleaned
    assert names_again == []


def test_successful_bounded_host_exec_arms_recovery_deadline():
    messages = _bounded_exec_messages()
    with patch(
        "chaos_agent.agent.execution_artifacts.time.time", return_value=1000,
    ):
        artifacts = collect_execution_artifacts(messages)

    assert artifacts[0]["status"] == "recovery_armed"
    assert artifacts[0]["host_exec_tool_call_id"] == "tc-exec"
    assert artifacts[0]["recovery_timeout_seconds"] == 600
    assert artifacts[0]["recovery_deadline_epoch"] == 1600


def _bounded_exec_messages():
    return _debug_messages() + [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "exec",
                    "v_args": (
                        "node-debugger-n1-abc12 -n kubewiz -- chroot /host "
                        "sh -c 'iptables -I OUTPUT -j DROP && nohup sh -c "
                        '"sleep 600 && iptables -D OUTPUT -j DROP" '
                        ">/dev/null 2>&1 &'"
                    ),
                },
                "id": "tc-exec",
            }],
        ),
        ToolMessage(
            content="injection started",
            name="kubectl",
            tool_call_id="tc-exec",
        ),
    ]


def test_replaying_messages_does_not_move_recovery_deadline():
    messages = _bounded_exec_messages()
    with patch(
        "chaos_agent.agent.execution_artifacts.time.time", return_value=1000,
    ):
        artifacts = collect_execution_artifacts(messages)
    with patch(
        "chaos_agent.agent.execution_artifacts.time.time", return_value=1200,
    ):
        replayed = collect_execution_artifacts(messages, artifacts)

    assert replayed[0]["status"] == "recovery_armed"
    assert replayed[0]["recovery_deadline_epoch"] == 1600


def test_replaying_creation_does_not_reactivate_cleaned_artifact():
    artifacts = collect_execution_artifacts(_debug_messages())
    artifacts[0]["status"] = "cleaned"

    replayed = collect_execution_artifacts(_debug_messages(), artifacts)

    assert replayed[0]["status"] == "cleaned"


@pytest.mark.asyncio
async def test_cleanup_keeps_carrier_until_bounded_recovery_deadline():
    artifacts = collect_execution_artifacts(_debug_messages())
    artifacts[0].update({
        "status": "recovery_armed",
        "recovery_deadline_epoch": 1600,
    })
    with (
        patch(
            "chaos_agent.agent.execution_artifacts.time.time", return_value=1000,
        ),
        patch(
            "chaos_agent.agent.nodes.execute._debug_pod.delete_debug_pod",
            new=AsyncMock(),
        ) as delete,
    ):
        updated, names = await cleanup_debug_pod_artifacts(
            artifacts, kubeconfig="/tmp/kubeconfig", task_id="task-1",
        )

    delete.assert_not_awaited()
    assert updated[0]["status"] == "recovery_armed"
    assert names == []


@pytest.mark.asyncio
async def test_cleanup_deletes_once_and_marks_cleaned():
    # Fire-and-forget: exactly one delete attempt; a confirmed removal marks
    # the artifact cleaned.
    artifacts = collect_execution_artifacts(_debug_messages())
    delete = AsyncMock(return_value="confirmed")
    with patch(
        "chaos_agent.agent.nodes.execute._debug_pod.delete_debug_pod",
        new=delete,
    ):
        updated, names = await cleanup_debug_pod_artifacts(
            artifacts, kubeconfig="/tmp/kubeconfig", task_id="task-1",
        )

    assert delete.await_count == 1
    assert updated[0]["status"] == "cleaned"
    assert names == ["node-debugger-n1-abc12"]


@pytest.mark.asyncio
async def test_cleanup_unconfirmed_delete_is_fire_and_forget():
    # An unlanded delete is NOT retried and is still marked cleaned — the pod's
    # bounded ``-- sleep 3600`` lifetime lets it lapse on its own.
    artifacts = collect_execution_artifacts(_debug_messages())
    delete = AsyncMock(return_value="unconfirmed")
    with patch(
        "chaos_agent.agent.nodes.execute._debug_pod.delete_debug_pod",
        new=delete,
    ):
        updated, names = await cleanup_debug_pod_artifacts(
            artifacts, kubeconfig="/tmp/kubeconfig", task_id="task-1",
        )

    assert delete.await_count == 1  # single attempt, no retry
    assert updated[0]["status"] == "cleaned"
    assert names == ["node-debugger-n1-abc12"]
