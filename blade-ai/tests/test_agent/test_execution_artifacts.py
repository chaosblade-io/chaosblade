"""Tests for durable execution artifact extraction."""

import time
from unittest.mock import AsyncMock, patch

import pytest

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.execution_artifacts import (
    cleanup_debug_pod_artifacts,
    collect_execution_artifacts,
    find_active_debug_pod,
    issue_call_is_registered_teardown,
    make_teardown_matcher,
    parse_debug_pod_metadata,
    parse_drill_vehicle_markers,
)
from chaos_agent.agent.providers import FaultProviderRegistry
from chaos_agent.config.settings import settings


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
        kind="pod",
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


def _bounded_exec_with_command(command: str):
    return _debug_messages() + [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {"subcommand": "exec", "v_args": command},
                "id": "tc-exec",
            }],
        ),
        ToolMessage(
            content="injection started",
            name="kubectl",
            tool_call_id="tc-exec",
        ),
    ]


def test_stoploop_arms_deadline_from_timer_not_loop_interval():
    # A timer-armed crictl-stop loop carries a SHORT loop-interval sleep;
    # the fault window is the systemd-run duration. Arming from the interval
    # would release the carrier while the loop still runs.
    command = (
        "node-debugger-n1-abc12 -n kubewiz -- chroot /host sh -c "
        "'systemd-run --on-active=60s --unit=blade-stoploop-mysql sh -c "
        "\"pkill -f crictl-stoploop\" && for i in 1 2 3 4; do crictl stop "
        "-t 0 abc123; sleep 15; done'"
    )
    with patch(
        "chaos_agent.agent.execution_artifacts.time.time", return_value=1000,
    ):
        artifacts = collect_execution_artifacts(_bounded_exec_with_command(command))

    assert artifacts[0]["status"] == "recovery_armed"
    assert artifacts[0]["recovery_timeout_seconds"] == 60
    assert artifacts[0]["recovery_deadline_epoch"] == 1060


def test_systemd_run_timer_form_arms_deadline_without_sleep():
    # A timer carrying the inverse has no sleep at all — its window IS the
    # timer duration. Before this was read from sleep only, so the carrier
    # was never armed and could take a second mutation mid-fault.
    command = (
        "node-debugger-n1-abc12 -n kubewiz -- chroot /host sh -c "
        "'iptables -I OUTPUT -j DROP && systemd-run --on-active=600s "
        "iptables -D OUTPUT -j DROP'"
    )
    with patch(
        "chaos_agent.agent.execution_artifacts.time.time", return_value=1000,
    ):
        artifacts = collect_execution_artifacts(_bounded_exec_with_command(command))

    assert artifacts[0]["status"] == "recovery_armed"
    assert artifacts[0]["recovery_timeout_seconds"] == 600
    assert artifacts[0]["recovery_deadline_epoch"] == 1600


def test_timeout_bounded_listener_arms_deadline_from_timeout():
    # Self-terminating forms carry neither a systemd timer nor a recovery
    # sleep — the ``timeout N`` bound IS the fault window. Reading sleep
    # only would never arm the carrier, leaving the double-injection gate
    # sealed forever.
    command = (
        "node-debugger-n1-abc12 -n kubewiz -- chroot /host sh -c "
        "'timeout 300 nc -l -p 8080 -k'"
    )
    with patch(
        "chaos_agent.agent.execution_artifacts.time.time", return_value=1000,
    ):
        artifacts = collect_execution_artifacts(_bounded_exec_with_command(command))

    assert artifacts[0]["status"] == "recovery_armed"
    assert artifacts[0]["recovery_timeout_seconds"] == 300
    assert artifacts[0]["recovery_deadline_epoch"] == 1300


def test_timeout_bounded_burn_loop_arms_deadline_from_timeout():
    command = (
        "node-debugger-n1-abc12 -n kubewiz -- chroot /host sh -c "
        "'timeout 300 sh -c \"while true; do dd if=/dev/zero of=/host/tmp/"
        "burn bs=1M count=512 oflag=direct; done\"'"
    )
    with patch(
        "chaos_agent.agent.execution_artifacts.time.time", return_value=1000,
    ):
        artifacts = collect_execution_artifacts(_bounded_exec_with_command(command))

    assert artifacts[0]["status"] == "recovery_armed"
    assert artifacts[0]["recovery_timeout_seconds"] == 300
    assert artifacts[0]["recovery_deadline_epoch"] == 1300


def test_freezer_suspend_arms_deadline_from_thaw_timer_not_gap_sleep():
    # The arm-then-freeze suspend carries a short gap sleep between arming
    # and freezing; the fault window is the THAW timer's duration.
    command = (
        "node-debugger-n1-abc12 -n kubewiz -- chroot /host sh -c "
        "'systemd-run --on-active=120s --unit=blade-thaw sh -c \"echo THAWED "
        "> /sys/fs/cgroup/freezer/kubepods/abc123/freezer.state\"; sleep 1; "
        "echo FROZEN > /sys/fs/cgroup/freezer/kubepods/abc123/freezer.state'"
    )
    with patch(
        "chaos_agent.agent.execution_artifacts.time.time", return_value=1000,
    ):
        artifacts = collect_execution_artifacts(_bounded_exec_with_command(command))

    assert artifacts[0]["status"] == "recovery_armed"
    assert artifacts[0]["recovery_timeout_seconds"] == 120
    assert artifacts[0]["recovery_deadline_epoch"] == 1120


def test_discrete_one_shot_stop_passes_gate_but_arms_no_deadline():
    # A one-shot crictl stop is an instantaneous event the kubelet
    # self-heals — it clears the bounded-recovery gate, but carries no
    # fault WINDOW, so the carrier must not be armed: no deadline means
    # immediate cleanup stays possible and the double-injection gate is
    # not sealed by a phantom window.
    command = (
        "node-debugger-n1-abc12 -n kubewiz -- chroot /host sh -c "
        "'crictl stop -t 0 abc123'"
    )
    with patch(
        "chaos_agent.agent.execution_artifacts.time.time", return_value=1000,
    ):
        artifacts = collect_execution_artifacts(_bounded_exec_with_command(command))

    assert artifacts[0]["status"] == "active"
    assert "recovery_deadline_epoch" not in artifacts[0]


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


# ---------------------------------------------------------------------------
# Drill-vehicle registration lines (script channel)
# ---------------------------------------------------------------------------


def test_parse_drill_vehicle_markers():
    content = (
        '{"status": "success", "replicas": 42}\n'
        '[drill-vehicle: {"kind": "deployment", "name": "chaos-ip-exhaust", '
        '"namespace": "cms-demo"}]\n'
    )
    assert parse_drill_vehicle_markers(content) == [
        {"kind": "deployment", "name": "chaos-ip-exhaust", "namespace": "cms-demo"},
    ]


def test_parse_drill_vehicle_markers_ignores_invalid_lines():
    # No name → unusable for cleanup → dropped; malformed JSON → dropped.
    assert parse_drill_vehicle_markers(
        '[drill-vehicle: {"kind": "pod", "namespace": "ns"}]'
    ) == []
    assert parse_drill_vehicle_markers("[drill-vehicle: {not json}]") == []
    assert parse_drill_vehicle_markers("no marker here") == []


def _skill_script_messages(marker: str):
    return [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "execute_skill_script",
                "args": {
                    "skill_name": "k8s-chaos-skills",
                    "script_name": "inject_cni_exhaust.py",
                    "params": "--namespace cms-demo --node n1 --kubeconfig /k",
                },
                "id": "tc-script",
            }],
        ),
        ToolMessage(
            content=marker,
            name="execute_skill_script",
            tool_call_id="tc-script",
        ),
    ]


def test_collect_registers_script_vehicle_from_marker():
    marker = (
        '{"status": "success"}\n'
        '[drill-vehicle: {"kind": "deployment", "name": "chaos-ip-exhaust", '
        '"namespace": "cms-demo"}]'
    )
    artifacts = collect_execution_artifacts(
        _skill_script_messages(marker),
        task_id="task-1", operation_family="resource_occupancy",
    )
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact["artifact_id"] == "occupant_deployment:cms-demo/chaos-ip-exhaust"
    assert artifact["type"] == "occupant_deployment"
    assert artifact["status"] == "active"
    assert artifact["cleanup"]["v_args"] == (
        "deployment chaos-ip-exhaust -n cms-demo --ignore-not-found"
    )


def test_collect_ignores_script_output_without_marker():
    artifacts = collect_execution_artifacts(
        _skill_script_messages('{"status": "failed", "message": "boom"}'),
        task_id="task-1",
    )
    assert artifacts == []


@pytest.mark.asyncio
async def test_cleanup_deletes_occupant_deployment_with_deployment_kind():
    # The occupant deployment registered via marker line is cleaned by the
    # SAME sweep as debug pods, but its delete must target the deployment
    # kind, not pod.
    marker = (
        '[drill-vehicle: {"kind": "deployment", "name": "chaos-ip-exhaust", '
        '"namespace": "cms-demo"}]'
    )
    artifacts = collect_execution_artifacts(
        _skill_script_messages(marker), task_id="task-1",
    )
    delete = AsyncMock(return_value="confirmed")
    with patch(
        "chaos_agent.agent.nodes.execute._debug_pod.delete_debug_pod",
        new=delete,
    ):
        updated, names = await cleanup_debug_pod_artifacts(
            artifacts, kubeconfig="/tmp/kubeconfig", task_id="task-1",
        )

    assert delete.await_count == 1
    assert delete.await_args.kwargs.get("kind") == "deployment"
    assert updated[0]["status"] == "cleaned"
    assert names == ["chaos-ip-exhaust"]


# ---------------------------------------------------------------------------
# Temporal exemption teeth (R19/G-3): messages are EVERGREEN, registrations
# are task-scoped — the matcher must still recognise a teardown delete
# AFTER the vehicle's artifact has been cleaned. Message history outlives
# the cleanup marking: channel-B re-scans (RESUME / REVOCATION / upgrade /
# downgrade) re-read the same delete call long after the artifact flipped
# to ``cleaned``. If the predicate ever grows a status check ("why match a
# cleaned registration?"), or cleanup ever physically removes entries
# ("cleaned artifacts just bloat state"), the historical delete instantly
# becomes mutation evidence again — the R8-1 ghost door re-opens through
# EXPIRED REGISTRATION, not a missing filter. Before R19, this defence
# existed ONLY as a docstring sentence; zero teeth pinned the cleaned
# shape (every family tooth constructs an ACTIVE vehicle).
# ---------------------------------------------------------------------------


def _cleaned_vehicle_artifact() -> list:
    """A registry whose ONLY entry is already ``cleaned``.

    Built through the real pipeline (collect → cleanup) so the artifact
    carries exactly the field shapes production leaves behind — including
    the cleanup stamps — rather than a hand-written stand-in.
    """
    import asyncio

    artifacts = collect_execution_artifacts(_debug_messages())
    with patch(
        "chaos_agent.agent.nodes.execute._debug_pod.delete_debug_pod",
        new=AsyncMock(return_value="confirmed"),
    ):
        cleaned, _ = asyncio.run(
            cleanup_debug_pod_artifacts(
                artifacts, kubeconfig="/tmp/kubeconfig", task_id="task-1",
            )
        )
    return cleaned


def test_cleaned_registration_still_exempt_from_teardown_matcher():
    """时间轴主牙：cleaned 载体的 delete 仍被 matcher 认出（层 1+2）。

    注册标记翻转后，matcher 构造时那个载体仍在列表里且谓词仍认它——
    钉住 is_vehicle_teardown_delete 的 status 无关设计与 cleanup 的
    标记不删除语义两者。未来任何「顺手加 status 检查」或「cleanup 改
    物理移除」的重构都会击穿本断言而全家测试其他牙全绿。"""
    cleaned = _cleaned_vehicle_artifact()
    # Sanity: the fixture really is the cleaned shape.
    assert cleaned[0]["status"] == "cleaned"

    matcher = make_teardown_matcher(cleaned)
    call_args = {
        "subcommand": "delete",
        "v_args": "pod node-debugger-n1-abc12 -n kubewiz",
    }
    assert matcher("kubectl", call_args) is True
    # And the neutral single-sourced predicate agrees.
    assert issue_call_is_registered_teardown("kubectl", call_args, cleaned)


def test_cleaned_vehicle_delete_is_not_mutation_evidence():
    """幂等重放牙：cleaned 载体的 delete 重放不构成 mutation 证据。

    即 is_vehicle_teardown_delete docstring 显式立法的语义——a cleaned
    registration still names assets whose idempotent ``--ignore-not-found``
    replay is cleanup。从词汇层真实消费面断言：scan_kubectl_mutation_index
    穿 matcher 后对同一历史返回 -1（无 mutation 索引），裸调（RAW）则
    返回命中——证明豁免正是那条防线，而非词汇层另有跳过。"""
    from chaos_agent.agent.providers.message_scanning import (
        KUBECTL_WRITE_SUBCOMMANDS,
        scan_kubectl_mutation_index,
    )

    cleaned = _cleaned_vehicle_artifact()
    history = [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "delete",
                    "v_args": "pod node-debugger-n1-abc12 -n kubewiz",
                },
                "id": "tc-del",
            }],
        ),
    ]

    idx = scan_kubectl_mutation_index(
        history, KUBECTL_WRITE_SUBCOMMANDS,
        is_teardown=make_teardown_matcher(cleaned),
    )
    assert idx == -1, (
        "a cleaned vehicle's delete replay must carry no mutation index "
        "(temporal exemption via expired registration = ghost door)"
    )
    # RAW contrast: without the matcher the same history hits — the
       # exemption IS the defence (no other skip hides inside the scan).
    assert (
        scan_kubectl_mutation_index(history, KUBECTL_WRITE_SUBCOMMANDS)
        == 0
    )


# ---------------------------------------------------------------------------
# Delete-shape matrix teeth (R20/G-4): the teardown exemption judged a
# delete by the (kind, namespace, names) the CLASSIFIER extracted — but
# kubectl's legal batch spellings (``pod a,b``, ``pod/a,pod/b``, "pod a
# b") arrive there as ONE joined "name" that matches no registration,
# so a legal batch teardown replay was counted as mutation evidence
# (the ghost-door family, parser-flavour). The predicate docstring's
# standing law ("one unregistered name in the batch poisons it")
# presupposes per-name resolution that no parser ever provided. Instead
# of another problem-driven tooth, this section pins the WHOLE legal
# name-shape space × exemption-verdict as an explicit matrix — already-
# correct shapes get pinned (no longer "accidentally right"), the batch
# cells run RED pre-fix, and any future shape is a visible new row, not
# a dark blind spot.
# ---------------------------------------------------------------------------

_R20_POD_A = "node-debugger-n1-abc12"
_R20_POD_B = "node-debugger-n2-def34"


def _r20_two_pod_registry() -> list:
    """Two registered debug pods in one namespace — the registry a batch
    teardown would name."""
    return [
        {
            "type": "debug_pod", "kind": "pod", "name": _R20_POD_A,
            "namespace": "kubewiz", "status": "active",
        },
        {
            "type": "debug_pod", "kind": "pod", "name": _R20_POD_B,
            "namespace": "kubewiz", "status": "active",
        },
    ]


#: (id, v_args, expect_exempt) — every legal ``kubectl delete`` name
#: shape against a registry holding BOTH pods.
_R20_SHAPE_MATRIX = [
    # -- baselines: shapes the family already handled, re-pinned here so
    #    this matrix is the ONE enumeration of the name-shape space --
    ("single-bare", f"pod {_R20_POD_A} -n kubewiz", True),
    ("single-slash", f"pod/{_R20_POD_A} -n kubewiz", True),
    ("single-alias", f"po {_R20_POD_A} -n kubewiz", True),
    ("single-replay-flag",
     f"pod {_R20_POD_A} -n kubewiz --ignore-not-found", True),
    # -- G-4 red cells: legal batch spellings --
    ("batch-comma", f"pod {_R20_POD_A},{_R20_POD_B} -n kubewiz", True),
    ("batch-slash-comma",
     f"pod/{_R20_POD_A},pod/{_R20_POD_B} -n kubewiz", True),
    ("batch-spaces", f"pod {_R20_POD_A} {_R20_POD_B} -n kubewiz", True),
    ("batch-glued-ns", f"pod {_R20_POD_A},{_R20_POD_B} -nkubewiz", True),
    ("batch-replay-flag",
     f"pod {_R20_POD_A},{_R20_POD_B} -n kubewiz --ignore-not-found", True),
    # -- poison cells: a batch naming an UNREGISTERED object is a real
    #    delete riding the batch — the whole call loses the exemption --
    ("poison-comma", f"pod {_R20_POD_A},other-pod -n kubewiz", False),
    ("poison-spaces", f"pod {_R20_POD_A} other-pod -n kubewiz", False),
    ("poison-mixed-kind",
     f"pod/{_R20_POD_A},svc/other -n kubewiz", False),
    # -- standing law: a nameless label delete is not asset-targeted --
    ("label-selector", "pod -l app=x -n kubewiz", False),
]


@pytest.mark.parametrize(
    "v_args,expect_exempt", [m[1:] for m in _R20_SHAPE_MATRIX],
    ids=[m[0] for m in _R20_SHAPE_MATRIX],
)
def test_teardown_matrix_exemption_verdict(v_args, expect_exempt):
    """G-4 矩阵主牙：每个合法 delete 名字形态对两 pod 注册表有钉住的豁免
    判定。修复前批删格红（逗号/空格形态以整串"名字"到达，匹配不到任何
    注册项）；poison/label 格须在修复前后都是 False——过度豁免是比缺失
    豁免更错的失败方向。"""
    registry = _r20_two_pod_registry()
    call = {"subcommand": "delete", "v_args": v_args}
    assert (
        issue_call_is_registered_teardown("kubectl", call, registry)
        is expect_exempt
    ), f"{v_args!r}: expected exemption={expect_exempt}"


@pytest.mark.parametrize(
    "v_args,expect_exempt", [m[1:] for m in _R20_SHAPE_MATRIX],
    ids=[m[0] for m in _R20_SHAPE_MATRIX],
)
def test_teardown_matrix_vocab_layer_index(v_args, expect_exempt):
    """矩阵投影到词汇层：豁免形态的成功 delete 重放经 P3 matcher 不携带
    mutation 索引（-1）；poison/label 形态的 delete 是真实 mutation 证据，
    必须依然命中（0）。与 R19 幂等重放牙同构，但覆盖全部名字形态。"""
    from chaos_agent.agent.providers.message_scanning import (
        KUBECTL_WRITE_SUBCOMMANDS,
        scan_kubectl_mutation_index,
    )

    registry = _r20_two_pod_registry()
    history = [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {"subcommand": "delete", "v_args": v_args},
                "id": "tc-del",
            }],
        ),
    ]
    idx = scan_kubectl_mutation_index(
        history, KUBECTL_WRITE_SUBCOMMANDS,
        is_teardown=make_teardown_matcher(registry),
    )
    if expect_exempt:
        assert idx == -1, (
            f"{v_args!r}: exempt batch leaked a mutation index "
            "(ghost door, parser flavour)"
        )
    else:
        assert idx == 0, (
            f"{v_args!r}: non-exempt delete must remain mutation evidence"
        )


def test_teardown_matrix_registered_mixed_kind_batch_is_exempt():
    """注册混合 kind 批牙（G-4/R20）：§6 四路清理打成一条
    ``pod/carrier,role/carrier`` —— 每个成员各按自己的 kind 匹配
    注册表（主资产按自身 kind，rbac 成员按 member kind），全注册
    → 豁免；与 poison-mixed-kind 格（未注册 svc 混入）互为对照。"""
    from chaos_agent.agent.providers.message_scanning import (
        KUBECTL_WRITE_SUBCOMMANDS,
        scan_kubectl_mutation_index,
    )

    registry = [{
        "type": "recovery_carrier", "kind": "pod", "name": "drill-rc-x",
        "namespace": "ns", "status": "cleaned",
        "rbac_family": [
            {"kind": "rolebinding", "name": "drill-rc-x", "namespace": "ns"},
            {"kind": "role", "name": "drill-rc-x", "namespace": "ns"},
            {"kind": "serviceaccount", "name": "drill-rc-x", "namespace": "ns"},
        ],
    }]
    call = {
        "subcommand": "delete",
        "v_args": "pod/drill-rc-x,role/drill-rc-x -n ns --ignore-not-found",
    }
    assert issue_call_is_registered_teardown("kubectl", call, registry)

    history = [AIMessage(
        content="",
        tool_calls=[{
            "name": "kubectl", "args": call, "id": "tc-del",
        }],
    )]
    idx = scan_kubectl_mutation_index(
        history, KUBECTL_WRITE_SUBCOMMANDS,
        is_teardown=make_teardown_matcher(registry),
    )
    assert idx == -1, (
        "the §6 four-way sweep batched into ONE mixed-kind call must "
        "carry no mutation index"
    )


#: (id, v_args, flipped_names) — the status flips a SUCCESSFUL delete
#: forces in the registry. Collect tracks the PHYSICAL world (an object
#: that was deleted is gone), so a partially-registered batch flips its
#: registered half even though the SAME call's exemption verdict is
#: False (poison) — two different questions, two different answers.
_R20_COLLECT_MATRIX = [
    ("single", f"pod {_R20_POD_A} -n kubewiz", [_R20_POD_A]),
    ("batch-comma",
     f"pod {_R20_POD_A},{_R20_POD_B} -n kubewiz",
     [_R20_POD_A, _R20_POD_B]),
    ("batch-slash-comma",
     f"pod/{_R20_POD_A},pod/{_R20_POD_B} -n kubewiz",
     [_R20_POD_A, _R20_POD_B]),
    ("batch-spaces",
     f"pod {_R20_POD_A} {_R20_POD_B} -n kubewiz",
     [_R20_POD_A, _R20_POD_B]),
    ("poison-batch-flips-registered-half",
     f"pod {_R20_POD_A},other-pod -n kubewiz", [_R20_POD_A]),
]


@pytest.mark.parametrize(
    "v_args,flipped", [m[1:] for m in _R20_COLLECT_MATRIX],
    ids=[m[0] for m in _R20_COLLECT_MATRIX],
)
def test_teardown_matrix_collect_flips_batch(v_args, flipped):
    """G-4 collect 牙：成功的批删把它点名的每个注册 pod 翻为 cleaned
    （登记册跟踪物理世界，与豁免判定无关）。修复前批删格红：
    _deleted_pod_identity 把整串当单名，一个也翻不了。"""
    messages = [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {"subcommand": "delete", "v_args": v_args},
                "id": "tc-del",
            }],
        ),
        ToolMessage(
            content='pod "deleted"', name="kubectl", tool_call_id="tc-del",
        ),
    ]
    artifacts = collect_execution_artifacts(
        messages, _r20_two_pod_registry(),
    )
    actual = sorted(
        a["name"] for a in artifacts
        if a.get("type") == "debug_pod" and a.get("status") == "cleaned"
    )
    assert actual == sorted(flipped), (
        f"{v_args!r}: expected flips={sorted(flipped)}, got={actual}"
    )


def test_teardown_matrix_batch_delete_disarms_recovery_carrier():
    """批删若点到 armed 载体 pod，同样触发 disarm 翻回 active（定时器
    宿主已物理消失）——与单名 delete 相同语义，不因批形态丢失。"""
    existing = [{
        "type": "recovery_carrier", "kind": "pod", "name": "drill-rc-x",
        "namespace": "ns", "status": "recovery_armed",
        "recovery_deadline_epoch": 99999,
    }]
    messages = [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "delete",
                    "v_args": "pod drill-rc-x,other-pod -n ns",
                },
                "id": "tc-del",
            }],
        ),
        ToolMessage(
            content='pod "deleted"', name="kubectl", tool_call_id="tc-del",
        ),
    ]
    artifacts = collect_execution_artifacts(messages, existing)
    assert artifacts[0]["status"] == "active", (
        "batch delete naming the armed carrier must disarm it"
    )
    assert "recovery_deadline_epoch" not in artifacts[0]


# ---------------------------------------------------------------------------
# Namespace-topology teeth (R21/G-5): the third match dimension —
# namespace — had NO legislation. A cluster-scoped member (B28
# five-object variant) registered under the carrier ns (the attach
# fallback) while its own self-rendered cleanup audit entry carries NO
# ``-n``: replaying the audit list the artifact itself prescribes was
# rejected by the exemption predicate — a self-referential
# contradiction (registration and audit entry come from the same
# function). The topology rule (``is_cluster_scoped_kind`` — which
# kinds live outside any namespace) is now single-sourced in the
# classifier, registration records the member's TRUE topology, and ns
# matching ignores the dimension for cluster-scoped kinds on BOTH
# sides (kubectl ignores ``-n`` on their commands, so its presence is
# command noise, not a discriminator).
# ---------------------------------------------------------------------------

_R21_CARRIER = "drill-rc-x"


def _r21_carrier_registry(cluster_member_ns: str) -> list:
    """A five-object-variant carrier registry: namespaced trio + cluster
    pair. ``cluster_member_ns`` controls the cluster members' REGISTERED
    namespace — "" is the true topology (post-R21 attach), "ns" is the
    pre-R21 legacy shape (carrier-ns fallback)."""
    return [{
        "type": "recovery_carrier", "kind": "pod", "name": _R21_CARRIER,
        "namespace": "ns", "status": "active",
        "rbac_family": [
            {"kind": "serviceaccount", "name": _R21_CARRIER,
             "namespace": "ns"},
            {"kind": "role", "name": _R21_CARRIER, "namespace": "ns"},
            {"kind": "rolebinding", "name": _R21_CARRIER,
             "namespace": "ns"},
            {"kind": "clusterrole", "name": _R21_CARRIER,
             "namespace": cluster_member_ns},
            {"kind": "clusterrolebinding", "name": _R21_CARRIER,
             "namespace": cluster_member_ns},
        ],
    }]


#: (id, kind, registered_ns, cmd_has_n) — a cluster-scoped object lives
#: in NO namespace: ``-n`` on its commands is noise kubectl ignores, so
#: EVERY registration shape × command shape combination must match.
#: The reg-"ns" rows pin the pre-R21 legacy registration shape (topology
#: matching keeps old in-flight registries exempt — no hydration).
_R21_TOPOLOGY_MATRIX = [
    ("clusterrole-reg-empty-cmd-bare", "clusterrole", "", False),
    ("clusterrole-reg-empty-cmd-n", "clusterrole", "", True),
    ("clusterrole-reg-ns-cmd-bare", "clusterrole", "ns", False),
    ("clusterrole-reg-ns-cmd-n", "clusterrole", "ns", True),
    ("clusterrolebinding-reg-empty-cmd-bare", "clusterrolebinding", "", False),
    ("clusterrolebinding-reg-empty-cmd-n", "clusterrolebinding", "", True),
    ("clusterrolebinding-reg-ns-cmd-bare", "clusterrolebinding", "ns", False),
    ("clusterrolebinding-reg-ns-cmd-n", "clusterrolebinding", "ns", True),
]


@pytest.mark.parametrize(
    "kind,registered_ns,cmd_has_n", [m[1:] for m in _R21_TOPOLOGY_MATRIX],
    ids=[m[0] for m in _R21_TOPOLOGY_MATRIX],
)
def test_teardown_topology_cluster_ns_is_noise(kind, registered_ns, cmd_has_n):
    """拓扑主牙：cluster-scoped kind 的 ns 维度两侧全部忽略——注册
    形态（真实拓扑 '' 或 pre-R21 遗留 'ns'）× 命令形态（不带/带 -n）
    八格全豁免。修复前 bare-cmd 对 reg-ns 格与 -n 对 reg-empty 格均
    False（豁免拒绝自家清理重放）。"""
    registry = _r21_carrier_registry(registered_ns)
    v_args = f"{kind} {_R21_CARRIER}"
    if cmd_has_n:
        v_args += " -n ns"
    call = {"subcommand": "delete", "v_args": v_args}
    assert issue_call_is_registered_teardown("kubectl", call, registry), (
        f"{kind} reg-ns={registered_ns!r} cmd-n={cmd_has_n}: a "
        "cluster-scoped member's ns is command noise — the exemption "
        "must not discriminate on it (ghost door, topology flavour)"
    )


@pytest.mark.parametrize(
    "v_args,expect_exempt",
    [
        ("role drill-rc-x -n ns", True),
        # a namespaced object without -n targets the default ns —
        # strict equality must survive the topology change
        ("role drill-rc-x", False),
        ("role drill-rc-x -n other", False),
        ("serviceaccount drill-rc-x -n other", False),
    ],
    ids=["match", "bare-cmd-default-ns", "wrong-ns", "wrong-ns-sa"],
)
def test_teardown_topology_namespaced_strictness_survives(v_args, expect_exempt):
    """namespaced 对照牙：拓扑化只忽略 cluster-scoped 的 ns 维度，
    namespaced 对象严格相等不变——ns 错配仍是拒绝（毒化方向不受
    拓扑化影响）。"""
    registry = _r21_carrier_registry("")
    call = {"subcommand": "delete", "v_args": v_args}
    assert (
        issue_call_is_registered_teardown("kubectl", call, registry)
        is expect_exempt
    )


def _r21_family_create_messages() -> list:
    """A successful five-object-variant creation history: the namespaced
    trio (serviceaccount/role/rolebinding) followed by the cluster pair —
    every stack member attaches for real, mirroring the production
    pairing of a pre-rendered cleanup entry with its create receipt."""
    return [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "create",
                    "v_args": f"serviceaccount {_R21_CARRIER} -n ns",
                },
                "id": "tc-sa",
            }],
        ),
        ToolMessage(
            content=f"serviceaccount/{_R21_CARRIER} created",
            name="kubectl", tool_call_id="tc-sa",
        ),
        AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "create",
                    "v_args": (
                        f"role {_R21_CARRIER} -n ns "
                        "--verb=get,patch --resource=pods"
                    ),
                },
                "id": "tc-role",
            }],
        ),
        ToolMessage(
            content=(
                f"role.rbac.authorization.k8s.io/{_R21_CARRIER} created"
            ),
            name="kubectl", tool_call_id="tc-role",
        ),
        AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "create",
                    "v_args": (
                        f"rolebinding {_R21_CARRIER} -n ns "
                        f"--role={_R21_CARRIER} "
                        f"--serviceaccount=ns:{_R21_CARRIER}"
                    ),
                },
                "id": "tc-rb",
            }],
        ),
        ToolMessage(
            content=(
                f"rolebinding.rbac.authorization.k8s.io/{_R21_CARRIER} "
                "created"
            ),
            name="kubectl", tool_call_id="tc-rb",
        ),
        AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "create",
                    "v_args": (
                        f"clusterrole {_R21_CARRIER} --verb=get,patch "
                        "--resource=persistentvolumes"
                    ),
                },
                "id": "tc-cr",
            }],
        ),
        ToolMessage(
            content="clusterrole.rbac.authorization.k8s.io/drill-rc-x created",
            name="kubectl", tool_call_id="tc-cr",
        ),
        AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "create",
                    "v_args": (
                        f"clusterrolebinding {_R21_CARRIER} "
                        f"--clusterrole={_R21_CARRIER} "
                        "--serviceaccount=ns:drill-rc-x"
                    ),
                },
                "id": "tc-crb",
            }],
        ),
        ToolMessage(
            content=(
                "clusterrolebinding.rbac.authorization.k8s.io/"
                "drill-rc-x created"
            ),
            name="kubectl", tool_call_id="tc-crb",
        ),
    ]


def test_cluster_member_registers_true_topology():
    """注册形态牙（真实管线）：cluster 变体 create 成功回执 → attach
    后成员记录其真实拓扑 namespace=""（不是 carrier ns 兜底）——
    注册数据与自家审计条目（不带 -n）自此同源自洽。"""
    artifacts = [{
        "type": "recovery_carrier", "kind": "pod", "name": _R21_CARRIER,
        "namespace": "ns", "status": "active", "rbac_family": [],
    }]
    out = collect_execution_artifacts(
        _r21_family_create_messages(), artifacts,
    )
    members = [
        m for m in out[0]["rbac_family"] if m["kind"] == "clusterrole"
    ]
    assert members, "the successful clusterrole create must attach"
    assert members[0].get("namespace") == "", (
        "a cluster-scoped member has NO namespace — recording the "
        "carrier-ns fallback contradicts the member's own cleanup audit "
        "entry (which deliberately carries no -n)"
    )


def test_cleanup_audit_entries_replay_exempt():
    """自洽不变量牙（G-5 核心坐标系）：载体 artifact 渲染的每一条
    cleanup 审计条目，按条目自身重放必须全部豁免——注册数据与审计
    条目永不漂移从「换轴检视才照见」升格为「提交即红的不变量」。
    修复前 cluster 条目（不带 -n）重放被拒：矛盾出自同一函数
    （member_ns 兜底 vs 审计条目渲染）。"""
    # carrier with the pre-rendered four-way stack entries...
    carrier = {
        "type": "recovery_carrier", "kind": "pod", "name": _R21_CARRIER,
        "namespace": "ns", "status": "active", "rbac_family": [],
        "cleanup": [
            {"tool": "kubectl", "subcommand": "delete",
             "v_args": f"pod {_R21_CARRIER} -n ns --ignore-not-found"},
            {"tool": "kubectl", "subcommand": "delete",
             "v_args": f"serviceaccount {_R21_CARRIER} -n ns --ignore-not-found"},
            {"tool": "kubectl", "subcommand": "delete",
             "v_args": f"role {_R21_CARRIER} -n ns --ignore-not-found"},
            {"tool": "kubectl", "subcommand": "delete",
             "v_args": f"rolebinding {_R21_CARRIER} -n ns --ignore-not-found"},
        ],
    }
    # ...then the five-object variant's full stack attaches for real:
    # a pre-rendered audit entry presupposes its create receipt (the
    # production pairing), so the namespaced trio attaches too.
    registry = collect_execution_artifacts(
        _r21_family_create_messages(), [carrier],
    )
    entries = [
        e for e in registry[0].get("cleanup") or []
        if isinstance(e, dict) and e.get("subcommand") == "delete"
    ]
    assert len(entries) >= 6, (
        "fixture sanity: four-way stack + cluster pair must all be present"
    )
    for entry in entries:
        call = {
            "subcommand": entry["subcommand"],
            "v_args": str(entry.get("v_args") or ""),
        }
        assert issue_call_is_registered_teardown(
            "kubectl", call, registry,
        ), f"audit entry self-contradicts: {call['v_args']!r} replay rejected"


def test_cluster_full_stack_batch_delete_is_exempt():
    """cluster 全栈批牙：``clusterrolebinding/x,clusterrole/x``（不带
    -n，kubectl 对 cluster-scoped 合法形态）全注册 → 豁免。"""
    registry = _r21_carrier_registry("")
    call = {
        "subcommand": "delete",
        "v_args": (
            f"clusterrolebinding/{_R21_CARRIER},clusterrole/{_R21_CARRIER}"
            " --ignore-not-found"
        ),
    }
    assert issue_call_is_registered_teardown("kubectl", call, registry)


def test_cluster_mixed_batch_with_namespaced_is_exempt():
    """cluster+namespaced 混批牙：``clusterrole/x,sa/x -n ns``——
    kubectl 真实语义（cluster 段忽略 -n，sa 段用 -n），两成员各按
    自己拓扑匹配 → 豁免。"""
    registry = _r21_carrier_registry("")
    call = {
        "subcommand": "delete",
        "v_args": f"clusterrole/{_R21_CARRIER},sa/{_R21_CARRIER} -n ns",
    }
    assert issue_call_is_registered_teardown("kubectl", call, registry)


def test_deployment_main_asset_delete_shapes_pinned():
    """E 线补格（R21）：occupant_deployment 主资产的 delete 名字形态
    （单名/斜杠/批逗号）进钉——R20 矩阵全部是 pod 域，deployment 域
    主资产形态此前只被探针路过从未被钉。"""
    registry = [
        {
            "type": "occupant_deployment", "kind": "deployment",
            "name": "chaos-ip-exhaust", "namespace": "cms-demo",
            "status": "active",
        },
        {
            "type": "occupant_deployment", "kind": "deployment",
            "name": "chaos-fs-fill", "namespace": "cms-demo",
            "status": "active",
        },
    ]
    for v_args in (
        "deployment chaos-ip-exhaust -n cms-demo",
        "deployment/chaos-ip-exhaust -n cms-demo",
        "deployments chaos-ip-exhaust,chaos-fs-fill -n cms-demo",
        "deployment/chaos-ip-exhaust,deployment/chaos-fs-fill -n cms-demo",
    ):
        call = {"subcommand": "delete", "v_args": v_args}
        assert issue_call_is_registered_teardown(
            "kubectl", call, registry,
        ), f"{v_args!r}: registered deployment teardown must be exempt"


# ---------------------------------------------------------------------------
# R22/G-6 — machinery≠mutation, CHANNEL face (exec into a registered
# recovery carrier)
#
# B76's contract legislated the DEMOLITION face (a registered-vehicle
# delete is asset removal) but the task's self-built recovery machinery
# operates a SECOND verb family: §3's SA-token verify exec and §4's
# timer arm/re-arm exec — both into the registered carrier pod, both
# judged MUTATING by the fail-safe vocabulary (the arm payload carries
# `curl -X PATCH`; the verify payload's `$(cat ...)` command
# substitution is un-analysable, so readonly fails closed on it —
# correct vocabulary-layer behaviour). The teardown predicate answered
# only `subcommand == "delete"`, so these machinery calls were
# attributed as kubectl_native injections: a re-arm beside a live
# experiment mis-marked combo_native_issued (recovery permanently
# re-routed to the LLM path), a verify exec pre-empted the
# first-native attribution slot, and a state-less restored session
# read a re-arm as "native takeover after blade failed".
#
# The fix generalises the exemption domain from "the delete verb hits a
# registered asset" to "machinery verbs hit registered machinery": an
# exec whose target is a REGISTERED recovery carrier (and whose payload
# is not a fault-binary mutation — the drift layer's withhold, same
# flag) is machinery maintenance, never injection evidence. The three
# verdict boundaries are deliberate: debug_pod execs stay attributed
# (they are the native-takeover channel), occupant pods stay attributed
# (the standard has no exec channel onto them), and a fault binary
# inside the carrier keeps identity review (hostNetwork shaping cannot
# be ruled out statically).
# ---------------------------------------------------------------------------

_R22_CARRIER = "drill-rc-x"
_R22_NS = "ns"

#: §4's compact arm payload (timer: sleep → curl -X PATCH → self log)
_R22_ARM_V_ARGS = (
    f"{_R22_CARRIER} -n {_R22_NS} -- sh -c '( sleep 300; "
    "C=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt; "
    "T=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); "
    "U=https://kubernetes.default.svc/apis/apps/v1/namespaces/"
    f"{_R22_NS}/deployments/web; "
    'for d in "{\\"spec\\":{\\"replicas\\":2}}"; do '
    "curl -s -X PATCH --cacert $C -H \"Authorization: Bearer $T\" "
    '-H "Content-Type: application/merge-patch+json" -d "$d" $U; done '
    ") >/tmp/restore.log 2>&1 & echo armed'"
)

#: §3's SA-token verify payload (curl GET whose token rides $(cat ...))
_R22_VERIFY_V_ARGS = (
    f"{_R22_CARRIER} -n {_R22_NS} -- sh -c 'T=$(cat "
    "/var/run/secrets/kubernetes.io/serviceaccount/token); "
    "curl -s --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt "
    "-H \"Authorization: Bearer $T\" "
    "https://kubernetes.default.svc/api'"
)

_R22_PAYLOADS = {
    "arm": _R22_ARM_V_ARGS,
    "verify": _R22_VERIFY_V_ARGS,
}


def _r22_registry(shape: str) -> list:
    """Carrier registries for the channel-face matrix.

    ``carrier`` — the registered recovery carrier (exempt target);
    ``carrier-other-ns`` — same carrier registered in another ns;
    ``debug_pod`` / ``occupant`` — OTHER vehicle types (must stay
    attributed: the exec channel's exemption is recovery-carrier only);
    ``none`` — no registration at all.
    """
    if shape == "carrier":
        return [{
            "type": "recovery_carrier", "kind": "pod",
            "name": _R22_CARRIER, "namespace": _R22_NS,
            "status": "active", "rbac_family": [],
        }]
    if shape == "carrier-other-ns":
        return [{
            "type": "recovery_carrier", "kind": "pod",
            "name": _R22_CARRIER, "namespace": "other-ns",
            "status": "active", "rbac_family": [],
        }]
    if shape == "debug_pod":
        return [{
            "type": "debug_pod", "kind": "pod",
            "name": _R22_CARRIER, "namespace": _R22_NS,
            "status": "active", "uid": "uid-1",
            "target": {"name": "victim"},
        }]
    if shape == "occupant":
        return [{
            "type": "occupant_pod", "kind": "pod",
            "name": _R22_CARRIER, "namespace": _R22_NS,
            "status": "active",
        }]
    return []


#: (id, payload, registry-shape, expect_exempt) — the channel face's
#: coordinate system: skill-standard payloads × registration shapes.
#: The registered-carrier rows are the G-6 gap (red before the fix);
#: every other row pins the verdict boundary (machinery exemption is
#: EARNED by the recovery_carrier registration, never by the verb).
_R22_CHANNEL_MATRIX = [
    ("verify-into-registered-carrier", "verify", "carrier", True),
    ("arm-into-registered-carrier", "arm", "carrier", True),
    ("verify-into-unregistered-pod", "verify", "none", False),
    ("arm-into-unregistered-pod", "arm", "none", False),
    ("verify-into-carrier-wrong-ns", "verify", "carrier-other-ns", False),
    ("arm-into-carrier-wrong-ns", "arm", "carrier-other-ns", False),
    ("verify-into-debug-pod", "verify", "debug_pod", False),
    ("verify-into-occupant-pod", "verify", "occupant", False),
]


@pytest.mark.parametrize(
    "payload_id,registry_shape,expect_exempt",
    [(m[1], m[2], m[3]) for m in _R22_CHANNEL_MATRIX],
    ids=[m[0] for m in _R22_CHANNEL_MATRIX],
)
def test_channel_exec_into_registered_carrier_is_machinery(
    payload_id, registry_shape, expect_exempt,
):
    """通道面主牙（G-6 坐标系）：skill 标准载荷（§3 验权 / §4 arm）×
    注册形态（注册载体 / 未注册 / 错 ns / debug_pod / occupant）。
    修复前 registered-carrier 格穿透（谓词只认 delete——载体 exec 被
    归因 kubectl_native：combo 误标 / 抢注 / 幽灵接替三链）；边界格
    钉住：豁免由 recovery_carrier 注册挣得，不由动词挣得。"""
    call = {
        "subcommand": "exec",
        "v_args": _R22_PAYLOADS[payload_id],
    }
    assert (
        issue_call_is_registered_teardown(
            "kubectl", call, _r22_registry(registry_shape),
        )
        is expect_exempt
    )


def test_channel_exec_fault_binary_into_carrier_stays_attributed():
    """边界钉住牙：故障二进制（stress-ng）exec 进注册载体仍归因——
    与漂移层同一个 withhold（fault_binary_mutation：静态分类器无法
    排除 hostNetwork 形变，身份审查不豁免）。"""
    call = {
        "subcommand": "exec",
        "v_args": f"{_R22_CARRIER} -n {_R22_NS} -- stress-ng --cpu 1",
    }
    assert not issue_call_is_registered_teardown(
        "kubectl", call, _r22_registry("carrier"),
    )


def test_channel_exec_slash_target_into_registered_carrier():
    """斜杠形态牙：``exec pod/carrier -- <mutating payload>``（kubectl
    合法形态；classifier 的 exec names 保留前缀）打注册载体 → 豁免——
    谓词按 R20 的段拆分原语剥前缀后匹配（与 delete 面同源单源）。"""
    payload = (
        f"pod/{_R22_CARRIER} -n {_R22_NS} -- sh -c "
        "'curl -s -X PATCH https://kubernetes.default.svc/api'"
    )
    call = {"subcommand": "exec", "v_args": payload}
    assert issue_call_is_registered_teardown(
        "kubectl", call, _r22_registry("carrier"),
    )


def test_channel_face_payloads_are_in_the_attribution_domain():
    """归因域钉住牙（豁免非空转证明）：arm/verify 载荷被判 mutating
    且 issue-time 分类器返回 kubectl_native——这两条载荷确实站在归因
    域内，通道面豁免是活扣不是空转；对照 readonly 载荷（cat
    restore.log 取证）在归因域外，无需豁免。"""
    from chaos_agent.agent.nodes.execute._injection_detection import (
        classify_issue_time_method,
    )
    from chaos_agent.agent.providers.message_scanning import (
        exec_inner_command_mutates,
    )

    for payload in (_R22_ARM_V_ARGS, _R22_VERIFY_V_ARGS):
        assert exec_inner_command_mutates(payload), (
            "fixture sanity: the skill-standard payloads must be judged "
            "mutating (the exemption exists to cover them)"
        )
        assert classify_issue_time_method(
            "kubectl",
            {"subcommand": "exec", "v_args": payload},
            is_host=False,
        ) == "kubectl_native"
    assert not exec_inner_command_mutates(
        f"{_R22_CARRIER} -n {_R22_NS} -- cat /tmp/restore.log"
    )


def test_channel_exec_after_failed_blade_is_not_native_takeover():
    """channel-B 投影牙（H3）：blade_create 失败后对注册载体 arm exec →
    原生接替扫描（matcher 已线程）不得计入——state-less restored
    session 的 was_blade_create_attempted 兜底读「blade 尝试且失败」
    而非「原生已接替」；对照组 mutating exec 进未注册 pod 照常计入。"""
    from chaos_agent.agent.providers.message_scanning import (
        KUBECTL_COMMAND_SUBCOMMANDS,
        KUBECTL_WRITE_SUBCOMMANDS,
        exec_inner_command_mutates,
        scan_kubectl_injection_after_blade,
    )

    def _history(v_args: str) -> list:
        return [
            AIMessage(content="", tool_calls=[{
                "name": "blade_create",
                "args": {"v_args": "create k8s pod-network delay --time 3000"},
                "id": "tc-blade",
            }]),
            ToolMessage(
                content="Error: blade create failed",
                name="blade_create", tool_call_id="tc-blade",
            ),
            AIMessage(content="", tool_calls=[{
                "name": "kubectl",
                "args": {"subcommand": "exec", "v_args": v_args},
                "id": "tc-arm",
            }]),
            ToolMessage(
                content="armed", name="kubectl", tool_call_id="tc-arm",
            ),
        ]

    registry = _r22_registry("carrier")
    matcher = make_teardown_matcher(registry)
    assert not scan_kubectl_injection_after_blade(
        _history(_R22_ARM_V_ARGS),
        KUBECTL_WRITE_SUBCOMMANDS,
        command_subcommands=KUBECTL_COMMAND_SUBCOMMANDS,
        is_mutating_command=exec_inner_command_mutates,
        is_teardown=matcher,
    ), "a re-arm into the registered carrier is machinery, not takeover"
    assert scan_kubectl_injection_after_blade(
        _history(f"victim-pod -n {_R22_NS} -- sh -c 'echo x > /etc/hosts'"),
        KUBECTL_WRITE_SUBCOMMANDS,
        command_subcommands=KUBECTL_COMMAND_SUBCOMMANDS,
        is_mutating_command=exec_inner_command_mutates,
        is_teardown=matcher,
    ), "a mutating exec into an UNREGISTERED pod is real takeover evidence"


# ---------------------------------------------------------------------------
# R23/G-7 — machinery≠mutation, HOST face (arm-first timer registration)
#
# The exemption umbrella gained its CHANNEL face in R22 (exec into a
# registered recovery carrier) — but only on the kubectl channel. The HOST
# channel's own recovery machinery was never legislated: every host skill
# 降级方案 prescribes "先武装定时恢复，再注入" (arm the systemd-run
# transient timer FIRST, then inject), so the arm call — a mutating
# ``host_inject`` whose payload runs at the DEADLINE, never at issue time —
# reached the issue-time attributor as ``host_native`` evidence. With
# ``break`` at the first committing call, the arm-first order let the timer
# registration pre-empt the method slot (H1': a failed injection after a
# successful arm reports the task as injected), mis-mark combo on
# blade+timer pairs (H2'), and read as native takeover in a restored
# session (H3': ``scan_host_native_index`` has no is_teardown thread — the
# P3 seam is severed on the host side).
#
# The HOST face's anchor is the FORM, not a registry: ToolGuard admits
# ``systemd-run`` ONLY in its timer form (``_check_systemd_run`` — every
# non-``--on-active`` shape is UNSUPPORTED_FORM, rejected before the tool
# runs), so an admitted systemd-run call IS a timer registration by
# single-source construction. The form primitive is shared with the guard
# (``is_systemd_run_timer``), never re-derived here. Deliberate boundary:
# the payload behind ``--on-active`` is statically ambiguous (a recovery
# inverse vs a delayed fault like ``kill -STOP``) — same family as the
# R22 carrier-payload blind spot; the guard's payload readmission narrows
# but does not eliminate it, and the exemption trusts the skill's
# arm-first discipline, not the payload's semantics.
# ---------------------------------------------------------------------------

_R23_HOST_ARM = (
    "systemd-run --on-active=600s --unit=blade-cont-nginx "
    "sh -c 'kill -CONT $(pgrep -f nginx)'"
)
_R23_HOST_ARM_SPLIT_FLAG = (
    "systemd-run --on-active 600s --unit=blade-restore-x "
    "sh -c 'mv /tmp/x.bak /etc/x'"
)
#: (id, command, expect_exempt) — the host face's coordinate system.
#: Timer rows are the G-7 gap (red before the fix); every other row pins
#: the boundary: the exemption is EARNED by the timer form alone, and an
#: injection command, a non-timer systemd-run (the ToolGuard-rejected
#: shape), or a payload-carried --on-active (the flag belongs to the
#: PAYLOAD, not the timer) all stay attributed.
_R23_HOST_MATRIX = [
    ("arm-timer-is-machinery", _R23_HOST_ARM, True),
    ("arm-timer-split-flag-is-machinery", _R23_HOST_ARM_SPLIT_FLAG, True),
    (
        "injection-command-stays-attributed",
        "kill -STOP 1234",
        False,
    ),
    (
        "non-timer-systemd-run-stays-attributed",
        "systemd-run nginx",
        False,
    ),
    (
        "payload-carried-on-active-stays-attributed",
        "systemd-run nginx --on-active=600s",
        False,
    ),
    (
        "readonly-diagnostic-stays-outside",
        "cat /proc/loadavg",
        False,
    ),
]


@pytest.mark.parametrize(
    "command,expect_exempt",
    [(m[1], m[2]) for m in _R23_HOST_MATRIX],
    ids=[m[0] for m in _R23_HOST_MATRIX],
)
def test_host_call_machinery_face(command, expect_exempt):
    """host 面主牙（G-7 坐标系）：host_inject × skill 标准载荷。
    修复前 timer 格穿透（谓词只认 kubectl——arm 抢注/combo 误标/
    幽灵接替三链）；钉住格：豁免由 timer 形态挣得（ToolGuard 准入
    单源），注入命令与非 timer 形态照常归因。"""
    assert (
        issue_call_is_registered_teardown(
            "host_inject", {"command": command}, [],
        )
        is expect_exempt
    )


def test_host_face_exec_host_command_shape():
    """argv 形态（exec_host_command 的 binary+args）：timer 判定不依赖
    command 字符串形态——两种 args 形状（与 readonly 层
    _host_native_call_is_readonly 同构处理）同一判定。"""
    call = {
        "binary": "systemd-run",
        "args": ["--on-active=300s", "--unit=blade-restore-y", "true"],
    }
    assert issue_call_is_registered_teardown(
        "exec_host_command", call, [],
    )


def test_host_face_requires_inject_tool_name():
    """通道钉住：host_read 不是注入载体（readonly 层域），kubectl 通道
    不路由到 host 面——豁免域按工具名单源（provider.inject_tool_names）。"""
    assert not issue_call_is_registered_teardown(
        "host_read", {"command": _R23_HOST_ARM}, [],
    )
    assert not issue_call_is_registered_teardown(
        "kubectl", {"command": _R23_HOST_ARM}, [],
    )


def test_namespaced_delete_without_n_stays_poisoned():
    """R21 矩阵盲区补格：namespaced 载体 delete 不带 -n。
    transport 默认 ns 静态不可知（命令本体的 ns 维度为空），毒化法
    fail-safe 拒绝豁免——保守方向钉住（真实删除发生在 transport ns，
    但归因层只信命令显式声明）。"""
    registry = _r22_registry("carrier")
    assert not issue_call_is_registered_teardown(
        "kubectl",
        {"subcommand": "delete", "v_args": f"pod {_R22_CARRIER}"},
        registry,
    )


# ---------------------------------------------------------------------------
# R25/G-8 — machinery≠mutation, BLADE-DEMOLITION face (exec-carried
# blade destroy / revoke)
#
# R22/R23 legislated the exec channel's machinery faces by TARGET (the
# registered carrier) — but the corpus prescribes a demolition delivery
# whose target is the CLUSTER's tool pod, never a task asset:
# ``kubectl exec <chaosblade-tool-pod> -- blade destroy <uid>`` (the
# kubelet-stall case's preferred recovery; the registry sweep's in-cluster
# delivery prescription; the re-arm protocol's "先 destroy 旧实验" inside
# the execute loop). The inline-blade classifier route eats the pod name
# (names=() — the CHANNEL face structurally cannot match), and the
# fail-safe readonly vocabulary judges ``blade destroy`` mutating
# (correct — readonly's own domain; Task-5193538b fixed only the
# inspection verbs), so the call fell through BOTH attribution faces to
# kubectl_native: a re-arm's destroy half mis-marked combo_native_issued
# beside a live experiment (recovery permanently re-routed), counted as
# replan attempt evidence, and polluted the recency/mutation-index scans
# — the R22 harm chain verbatim, destroy half.
#
# The fix anchors the exemption on the VERB×DOMAIN syntax primitive
# (classify_blade_exec_payload.has_destroy — segment-level, wrapper-
# tolerant, sh -c-expanding; the same single source the blade carrier's
# own faces use), NOT on carrier registration and NOT on the E3
# blade_destroy_uid form anchor (narrower: hex16/revoke shapes only — a
# non-hex UID spelling would silently fall back to attribution). Three
# deliberate boundaries: a create segment in the same payload stays
# attributed (a REAL experiment delivery — the mixed re-arm compound's
# attribution belongs to kubectl_exec), a fault binary riding past a
# ``;`` withholds via the same fault_binary_mutation flag the CHANNEL
# face withholds on (G-9 makes that flag segment-level), and the
# demolition verdict is registration-INDEPENDENT (the tool pod is
# cluster infrastructure, outside the artifact registry's domain).
# ---------------------------------------------------------------------------

_TOOL_POD = "chaosblade-tool-abc"
_TOOL_NS = "chaosblade"
_HEX16 = "aa11bb22cc33dd44"

#: pure demolition payloads × syntax forms (corpus shapes: the kubelet-
#: stall direct form; the sh -c wrapped form; a timeout wrapper; revoke —
#: classify_blade_exec_payload's own verb set; a non-hex UID spelling; a
#: readonly companion past ``;`` is still pure machinery).
_R25_DESTROY_PAYLOADS = {
    "direct-hex16": f"{_TOOL_POD} -n {_TOOL_NS} -- blade destroy {_HEX16}",
    "direct-nonhex-uid": (
        f"{_TOOL_POD} -n {_TOOL_NS} -- blade destroy exp-1234567890"
    ),
    "sh-c-wrapped": (
        f"{_TOOL_POD} -n {_TOOL_NS} -- sh -c 'blade destroy {_HEX16}'"
    ),
    "timeout-wrapped": (
        f"{_TOOL_POD} -n {_TOOL_NS} -- timeout 30 blade destroy {_HEX16}"
    ),
    "revoke-verb": f"{_TOOL_POD} -n {_TOOL_NS} -- blade revoke {_HEX16}",
    "readonly-companion": (
        f"{_TOOL_POD} -n {_TOOL_NS} -- sh -c "
        f"'blade destroy {_HEX16}; cat /tmp/destroy.log'"
    ),
}


@pytest.mark.parametrize("payload_id", sorted(_R25_DESTROY_PAYLOADS))
def test_blade_demolition_exec_is_machinery_regardless_of_registration(
    payload_id,
):
    """demolition 面主牙（G-8）：纯 destroy/revoke 载荷（动词×域语法锚）
    → machinery，与注册形态无关——tool-pod 是集群设施（registry 域外），
    注册载体直接形态的 names 被 inline-blade 路由吃掉（CHANNEL 面结构
    性失配）；两种 registry 形态下同一豁免。"""
    call = {
        "subcommand": "exec",
        "v_args": _R25_DESTROY_PAYLOADS[payload_id],
    }
    assert issue_call_is_registered_teardown("kubectl", call, []), (
        f"{payload_id}: pure demolition must be machinery (empty registry)"
    )
    assert issue_call_is_registered_teardown(
        "kubectl", call, _r22_registry("carrier"),
    ), (
        f"{payload_id}: pure demolition must be machinery (registered "
        "carrier — the face is registration-INDEPENDENT by design)"
    )


def test_blade_demolition_mixed_with_create_stays_attributed():
    """混合载荷钉住牙：destroy + create 复合（重武装复合命令的真实形态）
    → 不豁免——create 段是真实验投递（chaosblade provider 截胡归因
    kubectl_exec 是正确行为），整条豁免会掩盖注入段（replan 伪证据/
    逆扫描污染经 is_teardown 线程回潮）。"""
    payload = (
        f"{_TOOL_POD} -n {_TOOL_NS} -- sh -c "
        f"'blade destroy {_HEX16}; blade create k8s pod-cpu fullload "
        "--labels app=demo'"
    )
    assert not issue_call_is_registered_teardown(
        "kubectl", {"subcommand": "exec", "v_args": payload}, [],
    )


def test_blade_demolition_mixed_with_fault_binary_stays_attributed():
    """故障二进制扣留牙：destroy + stress-ng 复合 → 不豁免——与 CHANNEL
    面同一个 withhold（fault_binary_mutation 单旗，G-9 后 segment 级
    可靠）；fail-safe 方向：复合载荷宁可多归因不可漏注入。"""
    payload = (
        f"{_TOOL_POD} -n {_TOOL_NS} -- sh -c "
        f"'blade destroy {_HEX16}; stress-ng --cpu 4'"
    )
    assert not issue_call_is_registered_teardown(
        "kubectl", {"subcommand": "exec", "v_args": payload}, [],
    )


def test_blade_demolition_mixed_with_escape_primitive_stays_attributed():
    """逃逸原语扣留牙（R27/G-11a）：destroy + nsenter 复合 → 不豁免。

    docstring 自称 PURE demolition，但本面只立了 fbm/create 两个
    withhold——destroy 后面贴一段 nsenter 的复合被整条豁免（分类器
    已判 SCOPE_ESCAPE，豁免却在读 fbm 旗后放行）。escape 载荷在
    learning 模式下真实执行且零归因，是 fbm withhold 同一问题的
    第三面：复合载荷里的越界段必须扣留豁免，宁可多归因不可漏
    逃逸。"""
    payload = (
        f"{_TOOL_POD} -n {_TOOL_NS} -- sh -c "
        f"'blade destroy {_HEX16}; nsenter -t 1 -m sh'"
    )
    assert not issue_call_is_registered_teardown(
        "kubectl", {"subcommand": "exec", "v_args": payload}, [],
    ), (
        "an escape primitive riding past ';' withholds the machinery "
        "exemption — the same sentence the CHANNEL face's fbm withhold "
        "legislates"
    )


def test_blade_demolition_hidden_structure_riders_withhold_the_exemption():
    """结构藏匿扣留牙（R33/G-12）：destroy + $(nsenter / (nsenter /
    "$(nsenter 复合 → 不豁免。

    命令替换/子 shell 里的原语与 ';' 后的一样真实执行，但切分词
    汇表看不见它们——分类器落 scope=pod，ESCAPE WITHHOLD 无从触
    发，豁免整条放行（修复前实测 machinery=True：G-11a 在 G-12
    形态下重演）。"""
    for tail in (
        "$(nsenter -t 1 -m sh)",
        "(nsenter -t 1 -m sh)",
        "\"$(nsenter -t 1 -m sh)\"",
        "$(stress-ng --cpu 4)",
        # R33/G-12b: the case pattern terminator opens a branch command
        # list — the escape/fbm primitive in the branch executes.
        "case x in a) nsenter -t 1 -m sh;; esac",
    ):
        payload = (
            f"{_TOOL_POD} -n {_TOOL_NS} -- sh -c "
            f"'blade destroy {_HEX16}; {tail}'"
        )
        assert not issue_call_is_registered_teardown(
            "kubectl", {"subcommand": "exec", "v_args": payload}, [],
        ), (
            f"a forbidden segment hidden in a shell structure rides the "
            f"demolition exemption just as one past ';' does: {tail!r}"
        )


def test_arg_tail_parameter_text_keeps_the_exemption():
    """ARG-TAIL 误扣回收牙（R35/G-13）：destroy + echo \$(date) stress-ng
    复合 → 豁免保持。

    stress-ng 是 echo 的参数文本（shell 实证：只被打印，不执行），但
    G-12 closer 切段把它立为段头 → fbm 误报 → 豁免误扣（修复前实测
    False——与无 \$() 的同语义形态 echo done stress-ng（True）内部
    不一致）。对照的第三格钉住真 fbm 复合（stress-ng 真执行）仍然
    扣留——回收不得过冲。"""
    def teardown(v):
        return issue_call_is_registered_teardown(
            "kubectl",
            {"subcommand": "exec",
             "v_args": (f"{_TOOL_POD} -n {_TOOL_NS} -- sh -c "
                        f"'blade destroy {_HEX16}; {v}'")},
            [],
        )

    assert teardown("echo done $(date) stress-ng -c 1"), (
        "stress-ng in an echo argument tail is parameter text — the "
        "fbm withhold must not fire on a word the shell never runs"
    )
    assert teardown("echo done stress-ng"), (
        "same shell semantics without the substitution must stay exempt"
    )
    assert not teardown("stress-ng -c 1"), (
        "a fault binary at a real command position still withholds"
    )


def test_heredoc_body_text_keeps_the_exemption():
    """heredoc 体文本豁免牙（R36/G-14）：teardown 落盘清理脚本（carrier
    钦定 quoted heredoc 形态）体内含 fbm 词 → 豁免保持。

    体内容是 stdin 文本（shell 实证：写入文件不执行），但解析器把体行
    立为段头 → fbm 误报 → 豁免误扣（修复前实测 False——与体内无禁词
    的同形态（True）内部不一致）。第三格钉住 terminator 之后的真 fbm
    命令仍然扣留——回收不得过冲。"""
    def teardown(body, tail="echo cleaned"):
        return issue_call_is_registered_teardown(
            "kubectl",
            {"subcommand": "exec",
             "v_args": (f"{_TOOL_POD} -n {_TOOL_NS} -- sh -c "
                        f"'blade destroy {_HEX16}; "
                        f"cat > /tmp/cleanup.sh <<\"EOF\"\n"
                        f"{body}\nEOF\n{tail}'")},
            [],
        )

    assert teardown("stress-ng --cpu 4 --timeout 0"), (
        "a fault binary mentioned inside a heredoc body is stdin text "
        "— the fbm withhold must not fire on text the shell never runs"
    )
    assert teardown("echo plain cleanup"), (
        "the same form without the banned word must stay exempt"
    )
    assert not teardown("body", tail="stress-ng -c 1"), (
        "a fault binary after the heredoc terminator still withholds"
    )


def test_blade_demolition_payloads_are_in_the_attribution_domain():
    """归因域钉住牙（豁免非空转证明）：纯 destroy 载荷被判 mutating 且
    issue-time 归因 kubectl_native——正是被误归因的载荷才需要豁免面；
    对照 readonly 载荷（blade status，Task-5193538b 已修）在归因域
    外，无需豁免。"""
    from chaos_agent.agent.nodes.execute._injection_detection import (
        classify_issue_time_method,
    )
    from chaos_agent.agent.providers.message_scanning import (
        exec_inner_command_mutates,
    )

    payload = _R25_DESTROY_PAYLOADS["direct-hex16"]
    assert exec_inner_command_mutates(payload), (
        "fixture sanity: blade destroy must be judged mutating "
        "(fail-safe readonly vocabulary — its own domain is correct)"
    )
    assert classify_issue_time_method(
        "kubectl", {"subcommand": "exec", "v_args": payload}, is_host=False,
    ) == "kubectl_native"
    assert classify_issue_time_method(
        "kubectl",
        {
            "subcommand": "exec",
            "v_args": (
                f"{_TOOL_POD} -n {_TOOL_NS} -- blade status --uid {_HEX16}"
            ),
        },
        is_host=False,
    ) is None


def test_host_blade_destroy_is_machinery():
    """HOST 孪生牙：host 通道 host_inject 携带 blade destroy（即兴路径）
    → machinery——host_shell.issue_time_method 对 inject 工具无动词
    过滤（host_native），与 kubectl 面同一个误归因链；语法锚同源
    （classify_blade_exec_payload 同一判定）。"""
    assert issue_call_is_registered_teardown(
        "host_inject", {"command": f"blade destroy {_HEX16}"}, [],
    )
    assert issue_call_is_registered_teardown(
        "exec_host_command",
        {"binary": "blade", "args": ["destroy", _HEX16]},
        [],
    )


def test_host_blade_create_and_fault_binary_stay_attributed():
    """HOST 边界牙：host_inject 携带 blade create / 故障二进制 → 不豁免
    ——demolition 面动词×域锚只认 destroy/revoke，create 是真注入。"""
    assert not issue_call_is_registered_teardown(
        "host_inject",
        {"command": "blade create k8s pod-cpu fullload --labels app=x"},
        [],
    )
    assert not issue_call_is_registered_teardown(
        "host_inject", {"command": "stress-ng --cpu 4"}, [],
    )


# --- B85 layer-1: registered Role verbs (create --verb + json-patch fold) ---

_B85_CARRIER = "drill-rc-b85"


def _b85_carrier() -> dict:
    return {
        "type": "recovery_carrier", "kind": "pod", "name": _B85_CARRIER,
        "namespace": "ns", "status": "active", "rbac_family": [],
    }


def _tool_roundtrip(subcommand: str, v_args: str, call_id: str,
                    content: str = "created") -> list:
    return [
        AIMessage(content="", tool_calls=[{
            "name": "kubectl",
            "args": {"subcommand": subcommand, "v_args": v_args},
            "id": call_id,
        }]),
        ToolMessage(content=content, name="kubectl", tool_call_id=call_id),
    ]


def test_create_role_verbs_inline_flag_recorded():
    """B85 输入面：create role 成功回执的 --verb=get,patch 落进 member
    verbs——对账（tool_screener 层）从这里读授权面，不回扫命令。"""
    out = collect_execution_artifacts(
        _tool_roundtrip(
            "create",
            f"role {_B85_CARRIER} -n ns "
            "--verb=get,patch --resource=deployments.apps",
            "tc-b85-1",
        ),
        [_b85_carrier()],
    )
    role = next(
        m for m in out[0]["rbac_family"] if m["kind"] == "role"
    )
    assert role.get("verbs") == ["get", "patch"]


def test_create_role_verbs_separate_and_repeated_flags():
    """flag 三形态全覆盖：分离值 ``--verb get`` 与重复 flag 并集。"""
    out = collect_execution_artifacts(
        _tool_roundtrip(
            "create",
            f"clusterrole {_B85_CARRIER} --verb get --verb patch "
            "--resource=nodes",
            "tc-b85-2",
        ),
        [_b85_carrier()],
    )
    role = next(
        m for m in out[0]["rbac_family"] if m["kind"] == "clusterrole"
    )
    assert role.get("verbs") == ["get", "patch"]


def test_json_patch_verbs_fold_into_member():
    """两步建栈法第二步：json-patch 追加自删规则的 delete verb 并进
    member——六对象栈标准流程的武装载荷含自删 DELETE，不 fold 会假拒。"""
    messages = _tool_roundtrip(
        "create",
        f"clusterrole {_B85_CARRIER} --verb=get,patch "
        "--resource=persistentvolumes",
        "tc-b85-3a",
    ) + _tool_roundtrip(
        "patch",
        f"clusterrole {_B85_CARRIER} --type=json -p "
        "'[{\"op\":\"add\",\"path\":\"/rules/-\",\"value\":{\"apiGroups\":"
        "[\"rbac.authorization.k8s.io\"],\"resources\":[\"clusterrolebindings\""
        "],\"resourceNames\":[\"" + _B85_CARRIER + "\"],\"verbs\":[\"delete\"]}}]'",
        "tc-b85-3b",
        content="clusterrole.rbac.authorization.k8s.io/b85 patched",
    )
    out = collect_execution_artifacts(messages, [_b85_carrier()])
    role = next(
        m for m in out[0]["rbac_family"] if m["kind"] == "clusterrole"
    )
    assert role.get("verbs") == ["delete", "get", "patch"]


def test_verb_grant_union_idempotent_under_replay():
    """回放幂等：collect 每轮全量重扫——create/patch 收据重放不 clobber
    也不重复累积（并集幂等）。"""
    messages = _tool_roundtrip(
        "create",
        f"role {_B85_CARRIER} -n ns --verb=get,patch --resource=deployments",
        "tc-b85-4a",
    ) + _tool_roundtrip(
        "patch",
        f"role {_B85_CARRIER} -n ns --type=json -p "
        "'[{\"op\":\"add\",\"path\":\"/rules/-\",\"value\":{\"verbs\":[\"delete\"]}}]'",
        "tc-b85-4b",
        content="role.rbac.authorization.k8s.io/b85 patched",
    )
    once = collect_execution_artifacts(messages, [_b85_carrier()])
    twice = collect_execution_artifacts(messages, once)
    role = next(m for m in twice[0]["rbac_family"] if m["kind"] == "role")
    assert role.get("verbs") == ["delete", "get", "patch"]


def test_patch_verbs_ignore_failed_receipt_and_foreign_name():
    """patch 收据失败（回执含 Error）不 fold；名字非载体族成员的 patch
    不误挂（authorization 面不能被旁路扩权记账）。"""
    carrier = _b85_carrier()
    carrier["rbac_family"] = [{
        "kind": "role", "name": _B85_CARRIER, "namespace": "ns",
        "verbs": ["get", "patch"],
    }]
    messages = [
        AIMessage(content="", tool_calls=[{
            "name": "kubectl",
            "args": {
                "subcommand": "patch",
                "v_args": (
                    f"role {_B85_CARRIER} -n ns --type=json -p "
                    "'[{\"op\":\"add\",\"path\":\"/rules/-\",\"value\":"
                    "{\"verbs\":[\"delete\"]}}]'"
                ),
            },
            "id": "tc-b85-5a",
        }]),
        ToolMessage(
            content="Error: from server (role not found)",
            name="kubectl", tool_call_id="tc-b85-5a",
        ),
        AIMessage(content="", tool_calls=[{
            "name": "kubectl",
            "args": {
                "subcommand": "patch",
                "v_args": (
                    "role some-other-role -n ns --type=json -p "
                    "'[{\"op\":\"add\",\"path\":\"/rules/-\",\"value\":"
                    "{\"verbs\":[\"*\"]}}]'"
                ),
            },
            "id": "tc-b85-5b",
        }]),
        ToolMessage(content="patched", name="kubectl", tool_call_id="tc-b85-5b"),
    ]
    out = collect_execution_artifacts(messages, [carrier])
    role = next(m for m in out[0]["rbac_family"] if m["kind"] == "role")
    assert role.get("verbs") == ["get", "patch"]


# ---------------------------------------------------------------------------
# R46: exec/debug flag arity — one shared reader
# ---------------------------------------------------------------------------


class TestExecPodIdentityFlagArity:
    """``_exec_pod_identity`` must skip a value flag's VALUE for the WHOLE
    shared arity table (``_readonly_facts.kubectl_flag_takes_value``), not
    just ``-n``/``-c``.

    Its input is the RAW v_args: hygiene strips ``--context``/
    ``--kubeconfig`` downstream, but this reader runs FIRST, and
    ``--request-timeout 30s`` / ``-v 6`` / ``--as admin`` are never
    stripped at all. Reading the value as the pod name sent
    ``prod``/``30s``/``6`` into the B85 layer-1 reconcile (no registered
    carrier → the check was silently skipped) and the carrier arming
    marker (no match → the recovery window never armed).
    """

    @pytest.mark.parametrize("args, expected", [
        (["drill-rc-x", "-n", "ns1"], ("drill-rc-x", "ns1")),
        (["--context", "prod", "drill-rc-x", "-n", "ns1"], ("drill-rc-x", "ns1")),
        (["--request-timeout", "30s", "drill-rc-x"], ("drill-rc-x", "")),
        (["-v", "6", "drill-rc-x"], ("drill-rc-x", "")),
        (["--as", "admin", "drill-rc-x"], ("drill-rc-x", "")),
        (["--kubeconfig", "/x/config", "drill-rc-x"], ("drill-rc-x", "")),
        (["-c", "app", "drill-rc-x"], ("drill-rc-x", "")),
        (["-it", "drill-rc-x"], ("drill-rc-x", "")),
        (["--namespace=ns1", "drill-rc-x"], ("drill-rc-x", "ns1")),
    ])
    def test_value_flag_values_are_not_read_as_pod_names(self, args, expected):
        from chaos_agent.agent.execution_artifacts import _exec_pod_identity
        assert _exec_pod_identity(args) == expected


# ---------------------------------------------------------------------------
# Provider-owned carrier artifacts (openspec faultdrill-cr-channel task 2.6)
# ---------------------------------------------------------------------------

_FAULTDRILL_STDIN = """apiVersion: drill.blade-ai.io/v1alpha1
kind: FaultDrill
metadata:
  name: fd-demo1
  namespace: cms-demo
spec:
  action: secretSwap
"""

_FD_LEDGER_ARTIFACT = {
    "artifact_id": "faultdrill_cr:cms-demo/fd-demo1",
    "type": "faultdrill_cr",
    "kind": "FaultDrill",
    "status": "active",
    "task_id": "t-1",
    "name": "fd-demo1",
    "namespace": "cms-demo",
    "operation_family": "faultdrill_cr",
}


def _cr_apply_messages(*, result: str = "faultdrill/fd-demo1 created") -> list:
    return [
        AIMessage(
            content="",
            tool_calls=[{
                "id": "tc-fd",
                "name": "kubectl",
                "args": {
                    "subcommand": "apply",
                    "v_args": "-f -",
                    "stdin_data": _FAULTDRILL_STDIN,
                },
            }],
        ),
        ToolMessage(content=result, tool_call_id="tc-fd", name="kubectl"),
    ]


@pytest.fixture()
def _cr_channel():
    """Register the CR channel (flag on) for one test; restore the
    PRE-TEST flag afterwards so no stale registration leaks into
    neighbours (restore-the-original, not hardcode-off — the default
    flipped True post dark-launch)."""
    _orig = settings.faultdrill_enabled
    settings.faultdrill_enabled = True
    FaultProviderRegistry._providers = {}
    FaultProviderRegistry.register_builtins()
    try:
        yield
    finally:
        settings.faultdrill_enabled = _orig
        FaultProviderRegistry._providers = {}
        FaultProviderRegistry.register_builtins()


def test_collect_registers_provider_artifact(_cr_channel):
    """The ledger layer aggregates carrier-owned artifacts through the
    registry seam — no carrier import leaks into this module, and the
    artifact keys by its own ns/name (P11's full-set rule)."""
    arts = collect_execution_artifacts(
        _cr_apply_messages(), task_id="t-1", operation_family="faultdrill_cr",
    )
    fd = [a for a in arts if a.get("type") == "faultdrill_cr"]
    assert len(fd) == 1
    assert fd[0]["artifact_id"] == "faultdrill_cr:cms-demo/fd-demo1"
    assert fd[0]["status"] == "active"
    assert fd[0]["task_id"] == "t-1"
    assert fd[0]["cleanup"]["subcommand"] == "delete"


def test_collect_replay_keeps_cleaned_status(_cr_channel):
    """Replay protection: a re-collect over a settled row never rewinds
    ``cleaned`` back to ``active`` — the shared merge rule."""
    existing = [{
        "artifact_id": "faultdrill_cr:cms-demo/fd-demo1",
        "type": "faultdrill_cr",
        "name": "fd-demo1",
        "namespace": "",
        "status": "cleaned",
    }]
    arts = collect_execution_artifacts(_cr_apply_messages(), existing)
    fd = [a for a in arts if a.get("type") == "faultdrill_cr"]
    assert len(fd) == 1
    assert fd[0]["status"] == "cleaned"
    # Durable facts still fill (empty fields are backfilled, never
    # rewound).
    assert fd[0]["namespace"] == "cms-demo"


def test_collect_dark_launch_registers_nothing():
    """Dark launch: the channel is structurally absent — a landed-apply
    history contributes no artifact row."""
    _orig = settings.faultdrill_enabled
    settings.faultdrill_enabled = False
    FaultProviderRegistry._providers = {}
    FaultProviderRegistry.register_builtins()
    try:
        assert collect_execution_artifacts(_cr_apply_messages()) == []
    finally:
        settings.faultdrill_enabled = _orig
        FaultProviderRegistry._providers = {}
        FaultProviderRegistry.register_builtins()


@pytest.fixture()
def _sweep_claim(monkeypatch):
    """Replace the registry sweep seam with a recording stub."""
    calls: list = []
    holder = {"value": None, "raise": False}

    async def fake_sweep(artifact, *, kubeconfig="", task_id=""):
        calls.append((artifact, kubeconfig, task_id))
        if holder["raise"]:
            raise RuntimeError("claiming hook exploded")
        return holder["value"]

    monkeypatch.setattr(FaultProviderRegistry, "sweep_artifact", fake_sweep)
    return {"calls": calls, "holder": holder}


@pytest.mark.asyncio
async def test_sweep_claim_settled_marks_cleaned(_sweep_claim):
    _sweep_claim["holder"]["value"] = True
    updated, cleaned = await cleanup_debug_pod_artifacts(
        [dict(_FD_LEDGER_ARTIFACT)], kubeconfig="/kc", task_id="t-1",
    )
    assert updated[0]["status"] == "cleaned"
    assert cleaned == ["fd-demo1"]
    assert _sweep_claim["calls"][0][0]["type"] == "faultdrill_cr"
    assert _sweep_claim["calls"][0][1:] == ("/kc", "t-1")


@pytest.mark.asyncio
async def test_sweep_claim_false_keeps_for_next_round(_sweep_claim):
    _sweep_claim["holder"]["value"] = False
    updated, cleaned = await cleanup_debug_pod_artifacts(
        [dict(_FD_LEDGER_ARTIFACT)], kubeconfig="/kc", task_id="t-1",
    )
    assert updated[0]["status"] == "active"
    assert cleaned == []


@pytest.mark.asyncio
async def test_sweep_unclaimed_artifact_untouched(_sweep_claim):
    """No carrier claims it (``None``): the artifact stays exactly as it
    was — the sweep is opt-in per carrier, not a default delete."""
    _sweep_claim["holder"]["value"] = None
    updated, cleaned = await cleanup_debug_pod_artifacts(
        [dict(_FD_LEDGER_ARTIFACT)], kubeconfig="/kc", task_id="t-1",
    )
    assert updated[0]["status"] == "active"
    assert cleaned == []


@pytest.mark.asyncio
async def test_sweep_claiming_hook_error_keeps_and_never_raises(_sweep_claim):
    """Fire-and-forget contract: a claiming-hook error is contained —
    warning + keep, the sweep layer itself never raises."""
    _sweep_claim["holder"]["raise"] = True
    updated, cleaned = await cleanup_debug_pod_artifacts(
        [dict(_FD_LEDGER_ARTIFACT)], kubeconfig="/kc", task_id="t-1",
    )
    assert updated[0]["status"] == "active"
    assert cleaned == []
    assert len(_sweep_claim["calls"]) == 1


@pytest.mark.asyncio
async def test_sweep_vehicle_types_bypass_the_claim_seam(_sweep_claim):
    """Vehicles keep their OWN cleanup path: a debug pod delete goes
    through ``delete_debug_pod``, and the claim seam is never consulted."""
    artifacts = collect_execution_artifacts(_debug_messages())
    with patch(
        "chaos_agent.agent.nodes.execute._debug_pod.delete_debug_pod",
        new=AsyncMock(return_value="confirmed"),
    ):
        updated, cleaned = await cleanup_debug_pod_artifacts(
            artifacts, kubeconfig="/tmp/kc", task_id="task-1",
        )
    assert updated[0]["status"] == "cleaned"
    assert cleaned == ["node-debugger-n1-abc12"]
    assert _sweep_claim["calls"] == []
