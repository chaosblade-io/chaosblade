"""Phase 1 conformance tests for the built-in FaultProviders.

Locks in the behaviour-equivalent migration of ``_detect_injection_method`` into
the provider registry: the three built-in backends (ChaosBlade / kubectl-native
/ host-shell) plus the ``detect_method`` orchestration must reproduce the
original precedence (ChaosBlade UID scan → kubectl-native → host-native).
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.providers import FaultProvider, FaultProviderRegistry
from chaos_agent.agent.providers.chaosblade import ChaosbladeProvider
from chaos_agent.agent.providers.host_shell import HostShellProvider
from chaos_agent.agent.providers.k8s_native import K8sNativeProvider


@pytest.fixture(autouse=True)
def _isolate_registry():
    FaultProviderRegistry.clear()
    yield
    FaultProviderRegistry.clear()
    FaultProviderRegistry.register_builtins()


# -- message-history builders (mirror test_execute_loop.py conventions) ------


def _blade_ok(uid: str = "uid-123") -> ToolMessage:
    return ToolMessage(
        content='{"code":200,"success":true,"result":"%s"}' % uid,
        name="blade_create",
        tool_call_id="b1",
    )


def _kubectl_blade_ok(uid: str = "uid-xyz") -> ToolMessage:
    return ToolMessage(
        content='{"code":200,"success":true,"result":"%s"}' % uid,
        name="kubectl",
        tool_call_id="k1",
    )


def _host_ok(name: str = "exec_host_command") -> ToolMessage:
    return ToolMessage(content="filled /tmp/x", name=name, tool_call_id="h1")


def _kubectl_native_after_failed_blade() -> list:
    """blade_create fails, then a kubectl scale succeeds → kubectl_native."""
    return [
        AIMessage(content="", tool_calls=[
            {"name": "blade_create", "args": {}, "id": "b1"},
        ]),
        ToolMessage(
            content="Error: blade binary too old", name="blade_create",
            tool_call_id="b1", status="error",
        ),
        AIMessage(content="", tool_calls=[
            {"name": "kubectl", "args": {"subcommand": "scale",
                                          "v_args": "deploy/foo --replicas=0"},
             "id": "k1"},
        ]),
        ToolMessage(content="deployment.apps/foo scaled", name="kubectl",
                    tool_call_id="k1"),
    ]


def _kubectl_exec_msgs(v_args: str) -> list:
    """A single kubectl exec ToolMessage carrying the given host command."""
    return [
        AIMessage(content="", tool_calls=[
            {"name": "kubectl", "args": {
                "subcommand": "exec",
                "v_args": v_args,
            }, "id": "k9"},
        ]),
        ToolMessage(content="", name="kubectl", tool_call_id="k9"),
    ]


# -- protocol / registration -------------------------------------------------


def test_builtins_satisfy_protocol():
    for prov in (ChaosbladeProvider(), K8sNativeProvider(), HostShellProvider()):
        assert isinstance(prov, FaultProvider)


def test_register_builtins_order_and_carriers():
    FaultProviderRegistry.register_builtins()
    carriers = [p.carrier for p in FaultProviderRegistry.all_providers()]
    # Order is load-bearing: chaosblade probed first (UID methods).
    assert carriers == ["chaosblade", "k8s_native", "host_shell", "chaosblade_python"]


def test_register_builtins_method_index():
    FaultProviderRegistry.register_builtins()
    assert FaultProviderRegistry.resolve_by_method("host_blade").carrier == "chaosblade"
    assert FaultProviderRegistry.resolve_by_method("kubectl_exec").carrier == "chaosblade"
    assert FaultProviderRegistry.resolve_by_method("kubectl_native").carrier == "k8s_native"
    assert FaultProviderRegistry.resolve_by_method("host_native").carrier == "host_shell"


def test_matches_channel_axes():
    cb, kn, host = ChaosbladeProvider(), K8sNativeProvider(), HostShellProvider()
    assert cb.matches_channel("k8s") and cb.matches_channel("host")
    assert kn.matches_channel("k8s") and not kn.matches_channel("host")
    assert host.matches_channel("host") and not host.matches_channel("k8s")


def test_capability_attrs():
    # ChaosBlade produces a pollable experiment UID (blade destroy semantics).
    assert ChaosbladeProvider().has_experiment_uid is True
    assert ChaosbladeProvider().is_multi_step is False
    # kubectl-native injections may span several steps (no single completion marker).
    assert K8sNativeProvider().has_experiment_uid is False
    assert K8sNativeProvider().is_multi_step is True
    # Host-shell has no code-side reverse derivation: the reverse command lives
    # in the skill case and is executed via the LLM recover loop (Layer 2).
    assert HostShellProvider().has_experiment_uid is False
    # Host-native is a UID-less multi-step raw-command backend → opts into the
    # injection step self-check (aligned with kubectl_native).
    assert HostShellProvider().is_multi_step is True


def test_required_params_namespace_gating():
    cb = ChaosbladeProvider()
    # pod is namespaced → namespace required.
    assert "namespace" in cb.required_params("pod")
    # node / host are cluster-scoped → no namespace.
    assert "namespace" not in cb.required_params("node")
    assert "namespace" not in HostShellProvider().required_params("host")


# -- detect_method orchestration equivalence ---------------------------------


def test_detect_host_blade():
    FaultProviderRegistry.register_builtins()
    assert FaultProviderRegistry.detect_method(
        [_blade_ok()], "uid-123", is_host=False
    ) == "host_blade"


def test_detect_kubectl_exec():
    FaultProviderRegistry.register_builtins()
    assert FaultProviderRegistry.detect_method(
        [_kubectl_blade_ok()], None, is_host=False
    ) == "kubectl_exec"


def test_detect_kubectl_native():
    FaultProviderRegistry.register_builtins()
    assert FaultProviderRegistry.detect_method(
        _kubectl_native_after_failed_blade(), None, is_host=False
    ) == "kubectl_native"


def test_detect_kubectl_native_host_command():
    # Command-mode host injection via kubectl exec/debug. Attribution keys on
    # the injection ATTEMPT (AIMessage tool_calls) AND the fail-safe read/mutate
    # classification of the inner command: each case below carries a real fault
    # operation (network/disk/cpu via iptables/tc/stress/dd, incl. chroot /
    # nsenter escapes), so on a k8s channel with no blade UID it is
    # kubectl_native (Layer 1 not applicable). A read-only exec would NOT be
    # attributed (see test_detect_readonly_exec_not_injection).
    FaultProviderRegistry.register_builtins()
    cases = [
        # iptables full DROP via chroot
        "node-debugger-x -n default -- chroot /host sh -c 'iptables -I OUTPUT -j DROP'",
        # tc network delay via chroot
        "dbg -n default -- chroot /host sh -c 'tc qdisc add dev eth0 root netem delay 100ms'",
        # stress-ng CPU load via chroot
        "dbg -n default -- chroot /host stress-ng --cpu 4 --timeout 60s",
        # dd disk fill via chroot
        "dbg -n default -- chroot /host sh -c 'dd if=/dev/zero of=/host/data/fill bs=1M count=1000'",
        # nsenter into a pod netns + iptables write (no chroot)
        "dbg -n default -- nsenter -t 12345 -n iptables -A OUTPUT -j DROP",
    ]
    for v in cases:
        assert FaultProviderRegistry.detect_method(
            _kubectl_exec_msgs(v), None, is_host=False
        ) == "kubectl_native", v


def test_detect_kubectl_native_non_fault_binary_injections():
    # Skill-case regression: kubectl-native injections whose inner command is
    # NOT a fault-family binary (the shapes the former blacklist missed). Each
    # is a real injection (CPU busy loop, /etc/hosts DNS hijack, dmsetup IO
    # error, nc/socat port occupation, direct kill) and MUST be attributed to
    # kubectl_native so verify / recover route to the right backend.
    FaultProviderRegistry.register_builtins()
    cases = [
        # CPU fullload via a shell busy loop (Pod_CPU使用率过高 fallback)
        "app-pod -n default -- sh -c 'while true; do :; done &'",
        # DNS hijack via /etc/hosts edit (Pod_网络故障_DNS劫持)
        "app-pod -n default -- sh -c "
        "'cp /etc/hosts /etc/hosts.bak && echo \"1.2.3.4 svc\" >> /etc/hosts'",
        # File-system IO error via device-mapper (Pod_磁盘IO异常, blade-unsupported)
        "app-pod -n default -- dmsetup create errdev --table '0 100 error'",
        # Port occupation via nc listener (Pod_网络故障_端口被占用)
        "app-pod -n default -- sh -c 'kill $(fuser 8080/tcp 2>/dev/null); nc -l -p 8080 -k &'",
        # Bare nc listener (no kill prefix)
        "app-pod -n default -- nc -l -p 8080 -k",
        # Direct process kill inside the pod
        "app-pod -n default -- kill -9 1",
    ]
    for v in cases:
        assert FaultProviderRegistry.detect_method(
            _kubectl_exec_msgs(v), None, is_host=False
        ) == "kubectl_native", v


def test_detect_readonly_subcommand_not_injection():
    # Read-only kubectl subcommands (get/describe/logs) are not a mutating
    # attempt → no attribution.
    FaultProviderRegistry.register_builtins()
    for sub in ("get", "describe", "logs"):
        msgs = [
            AIMessage(content="", tool_calls=[
                {"name": "kubectl", "args": {"subcommand": sub, "v_args": "pods"},
                 "id": "r1"},
            ]),
            ToolMessage(content="NAME READY", name="kubectl", tool_call_id="r1"),
        ]
        assert FaultProviderRegistry.detect_method(
            msgs, None, is_host=False
        ) is None, sub


def test_detect_readonly_exec_not_injection():
    # Command-mode exec/debug are attributed ONLY when the inner command
    # mutates. A read-only probe (cat/ls/df/ps/wget/nslookup, tc show, iptables
    # -L, or a read-only ``ps | grep`` pipeline) carries no fault operation, so
    # it must NOT be attributed to kubectl_native — otherwise a benign probe
    # would mis-route Layer 1 / verify / recover to the wrong backend.
    # Regression for the over-broad "any exec == injection" attribution.
    FaultProviderRegistry.register_builtins()
    readonly = [
        "mypod -n default -- cat /proc/net/dev",
        "mypod -n default -- ls -la /var/log",
        "mypod -n default -- df -h /",
        "mypod -n default -- ps aux",
        "mypod -n default -- wget -qO- --timeout=5 http://svc",
        "mypod -n default -- nslookup kubernetes.default",
        "mypod -n default -- tc qdisc show dev eth0",
        "mypod -n default -- iptables -L",
        "mypod -n default -- ps aux | grep java",
        "mypod -n default -- ss -tlnp | grep 8080",
        "node/n1 -it --image=busybox -- cat /proc/loadavg",
    ]
    for v in readonly:
        assert FaultProviderRegistry.detect_method(
            _kubectl_exec_msgs(v), None, is_host=False
        ) is None, v


def test_detect_kubectl_native_on_severed_exec():
    # Forensic paradox: a network-DROP injection severs the exec connection, so
    # the ToolMessage comes back as `Error:` (timeout). Attribution keys on the
    # ATTEMPT (AIMessage subcommand=exec), NOT the result — so the successful
    # injection is still kubectl_native (task-d1aa0593 regression).
    FaultProviderRegistry.register_builtins()
    msgs = [
        AIMessage(content="", tool_calls=[
            {"name": "kubectl", "args": {
                "subcommand": "exec",
                "v_args": "node-debugger-x -n kubewiz -- chroot /host iptables -I OUTPUT -j DROP",
            }, "id": "k9"},
        ]),
        ToolMessage(content="Error: command timed out after 60s", name="kubectl",
                    tool_call_id="k9", status="error"),
    ]
    assert FaultProviderRegistry.detect_method(
        msgs, None, is_host=False
    ) == "kubectl_native"


def test_detect_channel_scopes_out_cross_domain_provider():
    # Channel scopes candidates: a host-shell carrier is never attributed on a
    # k8s channel, and a kubectl-native carrier is never attributed on a host
    # channel — the cross-domain provider is not even probed.
    FaultProviderRegistry.register_builtins()
    # host carrier on a k8s channel → host_shell not a candidate → None
    assert FaultProviderRegistry.detect_method(
        [_host_ok()], None, is_host=False
    ) is None
    # kubectl exec on a host channel → k8s_native not a candidate → None
    assert FaultProviderRegistry.detect_method(
        _kubectl_exec_msgs("pod-x -- chroot /host iptables -I OUTPUT -j DROP"),
        None, is_host=True,
    ) is None


def test_detect_host_native_only_when_is_host():
    FaultProviderRegistry.register_builtins()
    msgs = [_host_ok()]
    assert FaultProviderRegistry.detect_method(msgs, None, is_host=False) is None
    assert FaultProviderRegistry.detect_method(msgs, None, is_host=True) == "host_native"


def test_detect_blade_uid_wins_over_host_native():
    FaultProviderRegistry.register_builtins()
    # A real blade experiment must not be downgraded even on a host channel.
    assert FaultProviderRegistry.detect_method(
        [_blade_ok()], "uid-123", is_host=True
    ) == "host_blade"


def test_detect_method_self_bootstraps_on_empty_registry():
    # No explicit register_builtins() — detect_method must lazily bootstrap.
    assert FaultProviderRegistry.all_providers() == ()
    assert FaultProviderRegistry.detect_method(
        [_blade_ok()], "uid-123", is_host=False
    ) == "host_blade"
    assert [p.carrier for p in FaultProviderRegistry.all_providers()] == [
        "chaosblade", "k8s_native", "host_shell", "chaosblade_python",
    ]


def test_detect_method_no_injection_returns_none():
    FaultProviderRegistry.register_builtins()
    unrelated = [ToolMessage(content="ok", name="kubectl_read", tool_call_id="v1")]
    assert FaultProviderRegistry.detect_method(unrelated, None, is_host=True) is None


# -- recency-based attribution (task-76c59364) -------------------------------


def _destroyed_blade_then_native(uid: str = "uid-dead") -> list:
    """blade_create yields a UID, it is blade_destroy'd, THEN a kubectl-native
    iptables DROP is injected. The stale (destroyed) UID must not be claimed."""
    return [
        AIMessage(content="", tool_calls=[
            {"name": "blade_create", "args": {}, "id": "b1"}]),
        _blade_ok(uid),
        AIMessage(content="", tool_calls=[
            {"name": "blade_destroy", "args": {"uid": uid}, "id": "d1"}]),
        ToolMessage(content='{"code":200,"success":true}', name="blade_destroy",
                    tool_call_id="d1"),
        AIMessage(content="", tool_calls=[
            {"name": "kubectl", "args": {
                "subcommand": "exec",
                "v_args": "node-debugger-x -n default -- chroot /host "
                          "sh -c 'iptables -I OUTPUT -j DROP'",
            }, "id": "k9"}]),
        ToolMessage(content="Error: command timed out", name="kubectl",
                    tool_call_id="k9", status="error"),
    ]


def test_destroyed_blade_uid_not_claimed_native_wins():
    # Core task-76c59364 regression: a failed-then-destroyed blade experiment
    # left a residual UID that hijacked attribution onto the ChaosBlade
    # Layer-1 path (blade_status '') and wrongly failed a successful native
    # partition. The destroyed UID must be ignored → kubectl_native.
    FaultProviderRegistry.register_builtins()
    assert FaultProviderRegistry.detect_method(
        _destroyed_blade_then_native(), None, is_host=False,
    ) == "kubectl_native"


def test_stale_blade_uid_in_state_does_not_force_blade():
    # Even when a stale blade_uid is still carried in state (passed here), the
    # later native injection owns the task — attribution is by recency, not by
    # the mere presence of a UID.
    FaultProviderRegistry.register_builtins()
    assert FaultProviderRegistry.detect_method(
        _destroyed_blade_then_native("uid-dead"), "uid-dead", is_host=False,
    ) == "kubectl_native"


def test_later_native_wins_over_earlier_live_blade():
    # Recency principle: a live (non-destroyed) blade UID EARLIER, then a
    # kubectl-native injection LATER → the last injection wins (kubectl_native).
    FaultProviderRegistry.register_builtins()
    msgs = [
        AIMessage(content="", tool_calls=[
            {"name": "blade_create", "args": {}, "id": "b1"}]),
        _blade_ok("uid-live"),
        AIMessage(content="", tool_calls=[
            {"name": "kubectl", "args": {
                "subcommand": "exec",
                "v_args": "dbg -n default -- chroot /host "
                          "iptables -I OUTPUT -j DROP",
            }, "id": "k9"}]),
        ToolMessage(content="", name="kubectl", tool_call_id="k9"),
    ]
    assert FaultProviderRegistry.detect_method(
        msgs, None, is_host=False,
    ) == "kubectl_native"


def test_later_live_blade_wins_over_earlier_native():
    # Symmetric recency: a native mutation EARLIER, then a live blade UID
    # LATER → the blade experiment wins (host_blade).
    FaultProviderRegistry.register_builtins()
    msgs = [
        AIMessage(content="", tool_calls=[
            {"name": "kubectl", "args": {
                "subcommand": "scale", "v_args": "deploy/foo --replicas=0",
            }, "id": "k1"}]),
        ToolMessage(content="deployment.apps/foo scaled", name="kubectl",
                    tool_call_id="k1"),
        AIMessage(content="", tool_calls=[
            {"name": "blade_create", "args": {}, "id": "b1"}]),
        _blade_ok("uid-late"),
    ]
    assert FaultProviderRegistry.detect_method(
        msgs, None, is_host=False,
    ) == "host_blade"


# -- Layer-1 seam delegation (run_layer1_for_state) --------------------------


async def test_layer1_seam_host_native_skipped():
    from chaos_agent.agent.nodes.verify._verifier_layer1 import run_layer1_for_state

    FaultProviderRegistry.register_builtins()
    state = {"injection_method": "host_native", "messages": []}
    result = await run_layer1_for_state(state, "", "", task_id="t1")
    assert result.status == "skipped"


async def test_layer1_seam_kubectl_native_skipped():
    from chaos_agent.agent.nodes.verify._verifier_layer1 import run_layer1_for_state

    FaultProviderRegistry.register_builtins()
    state = {"injection_method": "kubectl_native", "messages": []}
    result = await run_layer1_for_state(state, "", "", task_id="t1")
    assert result.status == "skipped"


async def test_layer1_seam_unknown_method_falls_back_to_direct():
    # injection_method None → no provider resolves → default ChaosBlade host-blade
    # Layer 1, whose no-UID branch is a safe neutral fallback. With no blade_uid
    # and no blade_create attempt, that path returns "skipped".
    from chaos_agent.agent.nodes.verify._verifier_layer1 import run_layer1_for_state

    FaultProviderRegistry.register_builtins()
    state = {"injection_method": None, "messages": []}
    result = await run_layer1_for_state(state, "", "", task_id="t1")
    assert result.status == "skipped"



# -- recover() delegation (Phase 1e) -----------------------------------------


async def test_host_shell_recover_no_llm_unrecovered():
    # host_shell has no blade_uid and no code-side reverse derivation, so the
    # no-LLM recover() reports an honest unrecovered verdict (Layer 1 not
    # applicable, Layer 2 skipped). The reverse command is executed by the LLM
    # recover loop from the skill case, not here.
    result = await HostShellProvider().recover({"execution_artifacts": []}, None, task_id="t1")
    assert result.recovered is False
    assert result.level == "unrecovered"
    assert result.blade_uid == ""
    assert result.layer1["status"] == "skipped"
    assert result.layer2["status"] == "skipped"
    assert result.failure is not None
    assert result.warnings


async def test_chaosblade_recover_kubectl_exec_unreachable():
    from langchain_core.messages import ToolMessage

    # A successful kubectl-exec blade injection in history → recover() derives
    # is_kubectl_exec from the messages (no longer passed in) and reports the
    # host-blade-destroy-cannot-reach-it verdict.
    kubectl_success = ToolMessage(
        content='{"code":200,"success":true,"result":"uid-k8s"}',
        name="kubectl",
        tool_call_id="",
    )
    result = await ChaosbladeProvider().recover(
        {}, None, blade_uid="uid-k8s", kubeconfig="", messages=[kubectl_success],
    )
    assert result.recovered is False
    assert result.level == "unrecovered"
    assert result.blade_uid == "uid-k8s"
    assert result.layer1["status"] == "skipped"
    assert any("kubectl exec" in w for w in result.warnings)
    assert result.failure is not None


async def test_chaosblade_recover_local_blade_destroy_passed():
    from unittest.mock import patch

    from chaos_agent.agent.result.verdict import Layer1Result, Layer1Status

    with patch(
        "chaos_agent.agent.nodes.recover._recover_layer1._run_recover_layer1"
    ) as mock_l1:
        mock_l1.return_value = Layer1Result(
            status=Layer1Status.PASSED, details="blade_destroy: success",
        )
        result = await ChaosbladeProvider().recover(
            {}, None, blade_uid="uid-host", kubeconfig="", messages=[],
        )
    assert result.recovered is True
    assert result.level == "recovered"
    assert result.failure is None
    assert any("Layer 2" in w for w in result.warnings)


async def test_k8s_native_recover_non_chaosblade_unrecovered():
    result = await K8sNativeProvider().recover({}, None, blade_uid="")
    assert result.recovered is False
    assert result.level == "unrecovered"
    assert result.layer1["status"] == "skipped"
    assert any("Non-ChaosBlade" in w for w in result.warnings)
    assert result.failure is not None
