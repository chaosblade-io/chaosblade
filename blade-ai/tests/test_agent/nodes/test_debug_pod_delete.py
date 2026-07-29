"""Outcome classification for ``delete_debug_pod``.

Cleanup correctness hinges on distinguishing a confirmed removal from an
unconfirmed one: under an in-progress network fault the delete rides the very
API path the fault is severing, so a timeout must NOT be read as "cleaned".
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.nodes.execute._debug_pod import delete_debug_pod

_XPORT = "chaos_agent.agent.nodes.execute._debug_pod.execute_via_transport"


def _result(exit_code=0, stderr="", stdout=""):
    return SimpleNamespace(exit_code=exit_code, stderr=stderr, stdout=stdout)


@pytest.mark.asyncio
async def test_exit_zero_is_confirmed():
    with patch(_XPORT, new=AsyncMock(return_value=_result(exit_code=0))):
        assert await delete_debug_pod("p", "/kc", "task-1", namespace="default") == "confirmed"


@pytest.mark.asyncio
async def test_notfound_is_confirmed():
    # Pod already gone — the deletion goal is satisfied, not a failure.
    err = 'Error from server (NotFound): pods "p" not found'
    with patch(_XPORT, new=AsyncMock(return_value=_result(exit_code=1, stderr=err))):
        assert await delete_debug_pod("p", "/kc", "task-1", namespace="default") == "confirmed"


@pytest.mark.asyncio
async def test_transport_exception_is_unconfirmed():
    with patch(_XPORT, new=AsyncMock(side_effect=RuntimeError("task timed out after 10s"))):
        assert await delete_debug_pod("p", "/kc", "task-1", namespace="default") == "unconfirmed"


@pytest.mark.asyncio
async def test_other_nonzero_exit_is_unconfirmed():
    with patch(_XPORT, new=AsyncMock(return_value=_result(exit_code=1, stderr="i/o timeout"))):
        assert await delete_debug_pod("p", "/kc", "task-1", namespace="default") == "unconfirmed"
