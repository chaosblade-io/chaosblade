"""Tests for direct_setup._collect_context — target-aware prefetch gate."""

from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.nodes.direct_setup import _collect_context


class TestCollectContextMemoryLimitGate:
    """Regression guard: ``_collect_context`` must skip the
    ``pod_memory_limit_mb`` prefetch (and its kubectl call) for any
    non-memory fault. The previous version fetched it for every
    pod-scoped drill, wasting a kubectl roundtrip on cpu / network /
    io drills and producing a misleading OOMKill warning downstream."""

    @pytest.mark.asyncio
    async def test_cpu_target_skips_memory_limit_fetch(self):
        # CPU fault: prefetch must NOT run, target_metadata must NOT
        # contain ``pod_memory_limit_mb``.
        state = {
            "blade_scope": "pod",
            "blade_target": "cpu",
            "blade_action": "fullload",
            "kubeconfig": "/path/to/kubeconfig",
            "target": {"namespace": "ns", "names": ["p"], "labels": {}},
            "task_id": "t-cpu",
        }
        with patch(
            "chaos_agent.agent.nodes.direct_execute._fetch_pod_memory_limit_mb",
            new=AsyncMock(side_effect=AssertionError("must not be called for cpu")),
        ):
            metadata = await _collect_context(state)
        assert "pod_memory_limit_mb" not in metadata

    @pytest.mark.asyncio
    async def test_network_target_skips_memory_limit_fetch(self):
        state = {
            "blade_scope": "pod",
            "blade_target": "network",
            "blade_action": "delay",
            "kubeconfig": "/path/to/kubeconfig",
            "target": {"namespace": "ns", "names": ["p"], "labels": {}},
            "task_id": "t-net",
        }
        with patch(
            "chaos_agent.agent.nodes.direct_execute._fetch_pod_memory_limit_mb",
            new=AsyncMock(side_effect=AssertionError("must not be called for network")),
        ):
            metadata = await _collect_context(state)
        assert "pod_memory_limit_mb" not in metadata

    @pytest.mark.asyncio
    async def test_mem_target_does_fetch_memory_limit(self):
        # Memory-burn fault: prefetch MUST run, the value must land in
        # ``target_metadata`` so the downstream FCAT P0 size ceiling and
        # OOMKill risk warning can consume it without re-fetching.
        state = {
            "blade_scope": "pod",
            "blade_target": "mem",
            "blade_action": "burn",
            "kubeconfig": "/path/to/kubeconfig",
            "target": {"namespace": "ns", "names": ["p"], "labels": {}},
            "task_id": "t-mem",
        }
        with patch(
            "chaos_agent.agent.nodes.direct_execute._fetch_pod_memory_limit_mb",
            new=AsyncMock(return_value=512),
        ):
            metadata = await _collect_context(state)
        assert metadata.get("pod_memory_limit_mb") == 512

    @pytest.mark.asyncio
    async def test_non_pod_scope_skips_regardless_of_target(self):
        # Node-scoped fault: even if target is "mem" the prefetch
        # doesn't apply (pod_memory_limit_mb is pod-specific).
        state = {
            "blade_scope": "node",
            "blade_target": "mem",
            "blade_action": "load",
            "kubeconfig": "/path/to/kubeconfig",
            "target": {"namespace": "ns", "names": ["n"], "labels": {}},
            "task_id": "t-node",
        }
        with patch(
            "chaos_agent.agent.nodes.direct_execute._fetch_pod_memory_limit_mb",
            new=AsyncMock(side_effect=AssertionError("must not be called for node scope")),
        ):
            metadata = await _collect_context(state)
        assert "pod_memory_limit_mb" not in metadata

    @pytest.mark.asyncio
    async def test_missing_kubeconfig_skips_even_for_mem(self):
        # Defensive: without kubeconfig the fetch would fail anyway, so
        # the gate must short-circuit before trying.
        state = {
            "blade_scope": "pod",
            "blade_target": "mem",
            "blade_action": "burn",
            "kubeconfig": "",
            "target": {"namespace": "ns", "names": ["p"], "labels": {}},
            "task_id": "t-no-kube",
        }
        with patch(
            "chaos_agent.agent.nodes.direct_execute._fetch_pod_memory_limit_mb",
            new=AsyncMock(side_effect=AssertionError("must not be called without kubeconfig")),
        ):
            metadata = await _collect_context(state)
        assert "pod_memory_limit_mb" not in metadata
