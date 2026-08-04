"""Unified debug-pod parsing + live discovery fallback (task-29848471).

Parsing lives ONCE in ``_debug_pod.parse_debug_pod_name`` (the kubectl tool
wrapper's weaker private copy was removed after it produced false "no pod
created" reports). ``discover_created_debug_pod`` is the parse-failure
fallback: one live get-pods filtered by spec.nodeName + the node-debugger-
prefix + creationTimestamp recency.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.nodes.execute._debug_pod import (
    discover_created_debug_pod,
    parse_debug_pod_name,
)

_XPORT = "chaos_agent.agent.nodes.execute._debug_pod.execute_via_transport"


def _result(exit_code=0, stderr="", stdout=""):
    return SimpleNamespace(exit_code=exit_code, stderr=stderr, stdout=stdout)


class TestParseDebugPodNameUnified:
    """The merged pattern set must cover BOTH former implementations."""

    @pytest.mark.parametrize("output, expected", [
        # kubectl 1.25+ node-debug banner (former kubectl.py pattern)
        (
            "Creating debugging pod node-debugger-node-a-abc12 "
            "with container debugger on node node-a.",
            "node-debugger-node-a-abc12",
        ),
        ("Starting debugging pod node-a-debug-xyz123...", "node-a-debug-xyz123"),
        ("pod/p0-dbg created", "p0-dbg"),
        # former _debug_pod.py patterns must keep working
        ("some output pod node-debugger-node-b-q1 trailing", "node-debugger-node-b-q1"),
        ("node-a-debug-abc created", "node-a-debug-abc"),
    ])
    def test_patterns(self, output, expected):
        assert parse_debug_pod_name(output) == expected

    def test_no_match_returns_empty(self):
        assert parse_debug_pod_name("Warning: some unusual output") == ""
        assert parse_debug_pod_name("") == ""


def _pods_json(pods: list[tuple[str, str, str]]) -> str:
    """pods: list of (name, nodeName, creationTimestamp)."""
    return json.dumps({"items": [
        {
            "metadata": {"name": name, "creationTimestamp": ts},
            "spec": {"nodeName": node},
        }
        for name, node, ts in pods
    ]})


def _ts(delta_seconds: float) -> str:
    return (
        datetime.now(tz=timezone.utc) - timedelta(seconds=delta_seconds)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.asyncio
async def test_discovery_recency_excludes_stale_pods():
    """A leftover from an hour ago must lose to the just-created pod."""
    now = datetime.now(tz=timezone.utc).timestamp()
    pods = _pods_json([
        ("node-debugger-node-a-stale", "node-a", _ts(3600)),
        ("node-debugger-node-a-fresh", "node-a", _ts(5)),
    ])
    with patch(_XPORT, new=AsyncMock(return_value=_result(stdout=pods))):
        found = await discover_created_debug_pod("node-a", "default", now)
    assert found == "node-debugger-node-a-fresh"


@pytest.mark.asyncio
async def test_discovery_disambiguates_concurrent_pods_by_newest():
    """k3 had two debug pods coexisting on one node; take the newest."""
    now = datetime.now(tz=timezone.utc).timestamp()
    pods = _pods_json([
        ("node-debugger-node-a-xp8nc", "node-a", _ts(30)),
        ("node-debugger-node-a-nkpzs", "node-a", _ts(2)),
    ])
    with patch(_XPORT, new=AsyncMock(return_value=_result(stdout=pods))):
        found = await discover_created_debug_pod("node-a", "default", now)
    assert found == "node-debugger-node-a-nkpzs"


@pytest.mark.asyncio
async def test_discovery_filters_by_node_name():
    now = datetime.now(tz=timezone.utc).timestamp()
    pods = _pods_json([("node-debugger-node-b-fresh", "node-b", _ts(5))])
    with patch(_XPORT, new=AsyncMock(return_value=_result(stdout=pods))):
        found = await discover_created_debug_pod("node-a", "default", now)
    assert found == ""


@pytest.mark.asyncio
async def test_discovery_filters_by_prefix():
    """Workload pods (no node-debugger- prefix) are never picked up."""
    now = datetime.now(tz=timezone.utc).timestamp()
    pods = _pods_json([("mysql-79794985d4-7zl5p", "node-a", _ts(5))])
    with patch(_XPORT, new=AsyncMock(return_value=_result(stdout=pods))):
        found = await discover_created_debug_pod("node-a", "default", now)
    assert found == ""


@pytest.mark.asyncio
async def test_discovery_all_expired_returns_empty():
    """Everything predates the dispatch anchor (minus skew margin)."""
    now = datetime.now(tz=timezone.utc).timestamp()
    pods = _pods_json([("node-debugger-node-a-old", "node-a", _ts(3600))])
    with patch(_XPORT, new=AsyncMock(return_value=_result(stdout=pods))):
        found = await discover_created_debug_pod("node-a", "default", now)
    assert found == ""


@pytest.mark.asyncio
async def test_discovery_transport_error_returns_empty():
    with patch(_XPORT, new=AsyncMock(side_effect=RuntimeError("i/o timeout"))):
        found = await discover_created_debug_pod("node-a", "default", 0.0)
    assert found == ""


@pytest.mark.asyncio
async def test_discovery_non_json_returns_empty():
    with patch(
        _XPORT,
        new=AsyncMock(return_value=_result(stdout="Warning: weird")),
    ):
        found = await discover_created_debug_pod("node-a", "default", 0.0)
    assert found == ""
