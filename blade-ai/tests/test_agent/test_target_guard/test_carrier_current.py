"""Unit tests for ``registered_carrier_is_current`` (target_guard E part 3).

The security boundary is IDENTITY (uid + approved node + namespace + privileged),
not liveness. Once a network fault drives the target node to Unknown/NodeLost,
the API server reports the still-existing debug pod as not-Ready / phase!=Running;
that must NOT turn "injection worked" into a false "carrier unavailable" rejection.
"""

from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.target_guard.carriers import registered_carrier_is_current

_META = "chaos_agent.tools.kubectl._debug_pod_metadata"


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
