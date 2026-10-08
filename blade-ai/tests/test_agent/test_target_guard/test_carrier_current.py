"""Unit tests for ``registered_carrier_is_current`` (target_guard E part 3).

The security boundary is IDENTITY (uid + approved node + namespace + privileged),
not liveness. Once a network fault drives the target node to Unknown/NodeLost,
the API server reports the still-existing debug pod as not-Ready / phase!=Running;
that must NOT turn "injection worked" into a false "carrier unavailable" rejection.
"""

import json
import sys
from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.target_guard.carriers import registered_carrier_is_current

_META = "chaos_agent.tools.kubectl_cli._debug_pod_metadata"


def _artifact():
    return {
        "name": "node-debugger-node-a-abc12",
        "namespace": "kubewiz",
        "uid": "uid-1",
        "target": {"scope": "node", "name": "node-a"},
        "privileged": True,
    }


def _state():
    return {"kubeconfig": "", "kube_context": ""}


def _meta(**overrides):
    base = {
        "uid": "uid-1",
        "node": "node-a",
        "namespace": "kubewiz",
        "phase": "Running",
        "ready": True,
        "privileged": True,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_node_unknown_not_ready_still_current():
    # Node went Unknown/NodeLost: phase!=Running and ready=False, but the pod
    # identity (uid + node + namespace + privileged) is intact → still current.
    meta = _meta(phase="Failed", ready=False)
    with patch(_META, new=AsyncMock(return_value=(meta, None))):
        assert await registered_carrier_is_current(_artifact(), _state()) is True


@pytest.mark.asyncio
async def test_uid_mismatch_rejected():
    # A recreated pod gets a fresh uid → identity boundary rejects it.
    with patch(_META, new=AsyncMock(return_value=(_meta(uid="uid-2"), None))):
        assert await registered_carrier_is_current(_artifact(), _state()) is False


@pytest.mark.asyncio
async def test_wrong_node_rejected():
    with patch(_META, new=AsyncMock(return_value=(_meta(node="node-b"), None))):
        assert await registered_carrier_is_current(_artifact(), _state()) is False


@pytest.mark.asyncio
async def test_not_privileged_rejected():
    with patch(_META, new=AsyncMock(return_value=(_meta(privileged=False), None))):
        assert await registered_carrier_is_current(_artifact(), _state()) is False


@pytest.mark.asyncio
async def test_probe_error_rejected():
    # In-band get pod failed (error) → cannot confirm → reject (fail-closed).
    with patch(_META, new=AsyncMock(return_value=(None, "timed out"))):
        assert await registered_carrier_is_current(_artifact(), _state()) is False


class TestRealSignatureIntegration:
    """W-56-2/4 regression (#56 msg#179): the carriers.py call sites must
    match the REAL ``_debug_pod_metadata`` signature. The unit tests above
    mock the function itself — exactly the boundary that masked the 5-vs-3
    signature drift (tests green, production TypeError on every re-read,
    fail-closed REJECT_BANNED on a healthy carrier). These tests mock only
    the transport layer underneath, so a future call-site/signature drift
    fails here instead of in a live drill."""

    @staticmethod
    def _pod_json(name="node-debugger-node-a-abc12", uid="uid-1"):
        return json.dumps({
            "metadata": {"name": name, "namespace": "kubewiz", "uid": uid},
            "spec": {
                "nodeName": "node-a",
                "containers": [{"securityContext": {"privileged": True}}],
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [{"ready": True}],
            },
        })

    @staticmethod
    def _transport_returning(stdout: str):
        from chaos_agent.models.command_result import CommandResult

        async def fake_execute(cmd, target, timeout=0,
                               expect_profile=None, **kwargs):
            return CommandResult(exit_code=0, stdout=stdout, stderr="")

        return fake_execute

    @pytest.mark.asyncio
    async def test_registered_carrier_re_read_hits_real_signature(
        self, monkeypatch,
    ):
        # kubectl.py binds execute_via_transport via from-import, AND the
        # tools package re-binds the name ``kubectl`` to the StructuredTool
        # instance — so resolve the MODULE via sys.modules, not the string
        # path (monkeypatch would getattr the tool object instead).
        kubectl_module = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(
            kubectl_module, "execute_via_transport",
            self._transport_returning(self._pod_json()),
        )
        # Pre-fix this call raised
        # ``TypeError: _debug_pod_metadata() takes 3 positional arguments
        # but 5 were given`` on EVERY invocation.
        assert await registered_carrier_is_current(_artifact(), _state()) is True

    @pytest.mark.asyncio
    async def test_probe_backoff_hits_real_signature(self, monkeypatch):
        from chaos_agent.agent.target_guard.carriers import (
            _probe_debug_pod_with_backoff,
        )

        kubectl_module = sys.modules["chaos_agent.tools.kubectl_cli"]
        monkeypatch.setattr(
            kubectl_module, "execute_via_transport",
            self._transport_returning(self._pod_json()),
        )
        meta, err = await _probe_debug_pod_with_backoff(
            "node-debugger-node-a-abc12", "kubewiz", _state(),
        )
        assert err == ""
        assert meta.get("uid") == "uid-1"
        assert meta.get("node") == "node-a"
        assert meta.get("privileged") is True


class TestDerivedNodeCarrierAuthorization:
    """W-56-1: a node-host mechanism under a namespaced (pod) victim writes the
    node the victim runs on, which never appears in ``approved.names`` (that
    holds the victim pod identity). The frozen ``mechanism_entries`` carry the
    materialized victim-node name, so the chroot/host-exec carrier path must
    consult it — the same authorization the drift guard's manifest branch uses
    for the blade path. The two enforcement points must agree."""

    @staticmethod
    def _approved_pod(node_entry_names):
        from chaos_agent.agent.spec.fault_spec import FaultSpec
        from chaos_agent.agent.target_guard import (
            approved_from_dict, freeze_approved_target_from_spec,
        )
        from chaos_agent.agent.target_guard.mechanism_writes import (
            MechanismWriteEntry,
        )
        spec = FaultSpec(
            scope="pod", namespace="default", names=["victim-pod"],
            fault_target="network", fault_action="loss",
        )
        entries = (
            (MechanismWriteEntry(scope="node", namespace="", names=node_entry_names),)
            if node_entry_names else ()
        )
        return approved_from_dict(
            freeze_approved_target_from_spec(spec, mechanism_entries=entries),
        )

    @staticmethod
    def _host_exec_args():
        return {
            "subcommand": "exec",
            "v_args": "debug-pod-abc -n default -- chroot /host iptables -A "
                      "OUTPUT -p tcp --dport 6443 -j DROP",
        }

    @pytest.mark.asyncio
    async def test_derived_node_entry_authorises_the_carrier(self):
        from chaos_agent.agent.target_guard.carriers import (
            CarrierRejectReason, discover_unregistered_carrier,
        )
        approved = self._approved_pod(("node-x",))
        with patch(_META, new=AsyncMock(return_value=(_meta(node="node-x"), None))):
            res = await discover_unregistered_carrier(
                "kubectl", self._host_exec_args(), _state(), approved,
            )
        # The node check must NOT fire — the derived entry authorises node-x.
        assert res.reason != CarrierRejectReason.NODE_NOT_APPROVED

    @pytest.mark.asyncio
    async def test_node_outside_derived_entry_still_rejected(self):
        from chaos_agent.agent.target_guard.carriers import (
            CarrierRejectReason, discover_unregistered_carrier,
        )
        approved = self._approved_pod(("node-x",))
        with patch(_META, new=AsyncMock(return_value=(_meta(node="node-evil"), None))):
            res = await discover_unregistered_carrier(
                "kubectl", self._host_exec_args(), _state(), approved,
            )
        assert res.reason == CarrierRejectReason.NODE_NOT_APPROVED

    @pytest.mark.asyncio
    async def test_no_entry_no_name_rejects_as_before(self):
        # Regression: without a derived entry and with the node absent from
        # approved.names, the pre-existing NODE_NOT_APPROVED still fires.
        from chaos_agent.agent.target_guard.carriers import (
            CarrierRejectReason, discover_unregistered_carrier,
        )
        approved = self._approved_pod(())
        with patch(_META, new=AsyncMock(return_value=(_meta(node="node-x"), None))):
            res = await discover_unregistered_carrier(
                "kubectl", self._host_exec_args(), _state(), approved,
            )
        assert res.reason == CarrierRejectReason.NODE_NOT_APPROVED
