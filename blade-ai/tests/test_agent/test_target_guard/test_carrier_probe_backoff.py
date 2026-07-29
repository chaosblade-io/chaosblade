"""Tests for the live carrier-discovery probe backoff (target_guard).

``discover_unregistered_carrier`` rides the in-band API path that an
in-progress network fault intermittently severs, so a single ``kubectl get
pod`` can time out even though the pod exists. The probe must retry TRANSIENT
failures with backoff, but short-circuit on a genuine ``NotFound`` so a real
scope escape still fails closed without added latency.
"""

from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.target_guard import freeze_approved_target
from chaos_agent.agent.target_guard.freeze import approved_from_dict
from chaos_agent.agent.target_guard.carriers import (
    _PROBE_MAX_ATTEMPTS,
    _probe_debug_pod_with_backoff,
    discover_unregistered_carrier,
)

_META = "chaos_agent.tools.kubectl._debug_pod_metadata"
_SLEEP = "chaos_agent.agent.target_guard.carriers.asyncio.sleep"


def _meta(**overrides):
    base = {
        "name": "node-debugger-node-a-abc12",
        "namespace": "kubewiz",
        "uid": "uid-1",
        "node": "node-a",
        "privileged": True,
        "phase": "Running",
        "ready": True,
    }
    base.update(overrides)
    return base


def _state():
    return {"kubeconfig": "", "kube_context": ""}


class TestProbeBackoff:
    @pytest.mark.asyncio
    async def test_transient_timeout_then_success_retries(self):
        # First probe times out (flaky channel), second succeeds → resolved.
        probe = AsyncMock(side_effect=[({}, "i/o timeout"), (_meta(), "")])
        with patch(_META, new=probe), patch(_SLEEP, new=AsyncMock()):
            metadata, error = await _probe_debug_pod_with_backoff(
                "node-debugger-node-a-abc12", "kubewiz", _state(),
            )
        assert error == ""
        assert metadata["uid"] == "uid-1"
        assert probe.await_count == 2

    @pytest.mark.asyncio
    async def test_genuine_notfound_short_circuits(self):
        # A real "pod not found" is authoritative — return at once, NO retry.
        err = 'Error from server (NotFound): pods "x" not found'
        probe = AsyncMock(return_value=({}, err))
        sleep = AsyncMock()
        with patch(_META, new=probe), patch(_SLEEP, new=sleep):
            metadata, error = await _probe_debug_pod_with_backoff(
                "x", "kubewiz", _state(),
            )
        assert metadata == {}
        assert "not found" in error.lower()
        assert probe.await_count == 1
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_all_transient_exhausts_attempts(self):
        # Persistent flakiness → exhaust attempts, then report the last error.
        probe = AsyncMock(return_value=({}, "connection refused"))
        with patch(_META, new=probe), patch(_SLEEP, new=AsyncMock()):
            metadata, error = await _probe_debug_pod_with_backoff(
                "node-debugger-node-a-abc12", "kubewiz", _state(),
            )
        assert metadata == {}
        assert error == "connection refused"
        assert probe.await_count == _PROBE_MAX_ATTEMPTS


class TestDiscoveryUsesBackoff:
    @pytest.mark.asyncio
    async def test_discover_resolves_after_transient_timeout(self):
        # End-to-end: the discovery path resolves a privileged carrier on an
        # approved node even though the first in-band get pod timed out.
        approved = approved_from_dict(freeze_approved_target(
            target={"namespace": "", "names": ["node-a"]},
            params={"scope": "node"},
            blade_scope="node", blade_target="network", blade_action="drop",
        ))
        v_args = (
            "node-debugger-node-a-abc12 -n kubewiz -- "
            "chroot /host sh -c 'iptables -I OUTPUT -j DROP && "
            'nohup sh -c "sleep 600 && iptables -D OUTPUT -j DROP" '
            ">/dev/null 2>&1 &'"
        )
        probe = AsyncMock(side_effect=[({}, "i/o timeout"), (_meta(), "")])
        with patch(_META, new=probe), patch(_SLEEP, new=AsyncMock()):
            resolution = await discover_unregistered_carrier(
                "kubectl",
                {"subcommand": "exec", "v_args": v_args},
                _state(),
                approved,
            )
        assert resolution.resolved
        effective, artifact = resolution.effective, resolution.artifact
        assert effective.scope == "node"
        assert effective.names == ("node-a",)
        assert artifact["uid"] == "uid-1"
        assert probe.await_count == 2
