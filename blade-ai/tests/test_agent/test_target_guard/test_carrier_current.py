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
