"""Tests for ``chaos_agent.agent.nodes.execute._effect_channels``.

The channel registry owns the *where / how* of post-injection sampling. These
tests exercise scope dispatch, the ``allowed_scopes`` gate, node tool-pod
resolution, and the pod-direct-with-fallback logic — all with the transport /
discovery boundaries faked so no cluster is required.
"""

from __future__ import annotations

import types

import pytest

from chaos_agent.agent.nodes.execute import _effect_channels as ec
from chaos_agent.agent.nodes.execute._effect_channels import (
    HostSampleChannel,
    KubectlExecChannel,
    resolve_effect_channel,
)
from chaos_agent.agent.nodes.execute._effect_probes import Probe


def _result(stdout: str = "", exit_code: int = 0):
    return types.SimpleNamespace(stdout=stdout, exit_code=exit_code)


@pytest.fixture
def fake_exec(monkeypatch):
    """Replace ``execute_via_transport`` with a scripted async fake.

    Set ``fake_exec.responses`` to a list consumed in call order; each entry is
    a SimpleNamespace(stdout, exit_code). Records the built commands passed in.
    """
    state = types.SimpleNamespace(responses=[], calls=[])

    # ``**kwargs`` so a new transport argument (e.g. ``expect_profile``) does not
    # turn into a silent "kubectl exec failed" here — the real call swallows
    # exceptions, so a rigid signature would fail as an empty sample instead of
    # a TypeError anyone could read.
    async def _fake(cmd, target, timeout=None, task_id=None, **kwargs):  # noqa: ANN001
        state.calls.append(cmd)
        if state.responses:
            return state.responses.pop(0)
        return _result()

    # Only the transport boundary is faked; the real ``build_kubectl_cmd`` just
    # assembles an argv that the fake transport ignores.
    monkeypatch.setattr(ec, "execute_via_transport", _fake)
    return state


def _patch_discovery(monkeypatch, result):
    """Patch discover_tool_pod_on_node (lazily imported) to return ``result``."""
    import chaos_agent.agent.nodes.execute._injection_detection as inj

    async def _fake(node_name, kubeconfig, task_id):  # noqa: ANN001
        return result

    monkeypatch.setattr(inj, "discover_tool_pod_on_node", _fake, raising=True)


async def test_host_scope_returns_host_channel(monkeypatch):
    import chaos_agent.agent.nodes.execute._host_verify as hv

    async def _fake_diag(command, state, task_id):  # noqa: ANN001
        return _result(stdout=f"ran:{command}")

    monkeypatch.setattr(hv, "_run_host_diagnostic", _fake_diag, raising=True)

    channel = await resolve_effect_channel(
        "host", names="", namespace="", kubeconfig="", task_id="t", state=None,
    )
    assert isinstance(channel, HostSampleChannel)
    assert channel.scope == "host"
    assert channel.pod_name == "" and channel.node_name == ""
    assert await channel.run("df -h /") == "ran:df -h /"
    # sample() translates a semantic probe for the host scope, then delegates
    # to run(): disk_usage → "df -h <path>".
    assert await channel.sample(Probe("disk_usage", {"path": "/"})) == "ran:df -h /"


async def test_allowed_scopes_gate_excludes_pod():
    channel = await resolve_effect_channel(
        "pod", names="p1", namespace="ns", kubeconfig="", task_id="t",
        state=None, allowed_scopes=("host", "node"),
    )
    assert channel is None


async def test_node_scope_without_names_returns_none():
    channel = await resolve_effect_channel(
        "node", names="", namespace="", kubeconfig="", task_id="t", state=None,
    )
    assert channel is None


async def test_node_scope_no_tool_pod_returns_none(monkeypatch):
    _patch_discovery(monkeypatch, None)
    channel = await resolve_effect_channel(
        "node", names="node-1", namespace="", kubeconfig="", task_id="t",
        state=None,
    )
    assert channel is None


async def test_node_scope_resolves_tool_pod(monkeypatch, fake_exec):
    _patch_discovery(monkeypatch, ("tool-pod", "chaosblade"))
    fake_exec.responses = [_result(stdout="diskstats-out")]
    channel = await resolve_effect_channel(
        "node", names="node-1", namespace="", kubeconfig="kc", task_id="t",
        state=None,
    )
    assert isinstance(channel, KubectlExecChannel)
    assert channel.scope == "node"
    assert channel.pod_name == "tool-pod"
    assert channel.node_name == "node-1"
    assert await channel.run("cat /proc/diskstats") == "diskstats-out"


async def test_pod_scope_direct_when_probe_succeeds(monkeypatch, fake_exec):
    # Probe on the target pod succeeds → sample the target pod directly.
    fake_exec.responses = [_result(stdout="probe-ok", exit_code=0)]
    channel = await resolve_effect_channel(
        "pod", names="app-pod", namespace="prod", kubeconfig="kc", task_id="t",
        state=None, probe=Probe("diskstats"),
        allowed_scopes=("host", "node", "pod"),
    )
    assert isinstance(channel, KubectlExecChannel)
    assert channel.scope == "pod"
    assert channel.pod_name == "app-pod"
    assert channel.node_name == ""  # target-pod-direct has no resolved node


async def test_pod_scope_falls_back_to_tool_pod(monkeypatch, fake_exec):
    # Probe fails → resolve the pod's node, then a tool pod on that node.
    _patch_discovery(monkeypatch, ("tool-pod", "chaosblade"))
    fake_exec.responses = [
        _result(stdout="", exit_code=1),        # probe fails
        _result(stdout="node-x", exit_code=0),  # jsonpath nodeName lookup
    ]
    channel = await resolve_effect_channel(
        "pod", names="app-pod", namespace="prod", kubeconfig="kc", task_id="t",
        state=None, probe=Probe("diskstats"),
        allowed_scopes=("host", "node", "pod"),
    )
    assert isinstance(channel, KubectlExecChannel)
    assert channel.scope == "pod"
    assert channel.pod_name == "tool-pod"
    assert channel.node_name == "node-x"


async def test_unknown_scope_returns_none():
    channel = await resolve_effect_channel(
        "cluster", names="x", namespace="", kubeconfig="", task_id="t",
        state=None,
    )
    assert channel is None
