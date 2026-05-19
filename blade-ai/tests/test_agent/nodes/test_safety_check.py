"""Tests for safety_check node."""

import pytest

from chaos_agent.agent.nodes.safety_check import safety_check
from chaos_agent.config.settings import settings


class TestSafetyCheck:
    """Tests for the safety_check node function."""

    @pytest.mark.asyncio
    async def test_all_checks_pass(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "default", "names": ["my-pod"]}

        result = await safety_check(state)
        assert result["safety_status"] == "safe"
        assert result["safety_reason"] is None

    @pytest.mark.asyncio
    async def test_blacklisted_namespace(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "safety_blacklist_namespaces", "kube-system,kube-public")

        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "kube-system", "names": ["coredns"]}

        result = await safety_check(state)
        assert result["safety_status"] == "rejected"
        assert "kube-system" in result["safety_reason"]
        assert "blacklist" in result["safety_reason"].lower()

    @pytest.mark.asyncio
    async def test_another_blacklisted_namespace(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "safety_blacklist_namespaces", "kube-system,kube-public")

        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "kube-public", "names": ["some-res"]}

        result = await safety_check(state)
        assert result["safety_status"] == "rejected"
        assert "kube-public" in result["safety_reason"]

    @pytest.mark.asyncio
    async def test_no_skill_name(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = ""
        state["target"] = {"namespace": "default"}

        result = await safety_check(state)
        assert result["safety_status"] == "rejected"
        assert "no skill" in result["safety_reason"].lower()

    @pytest.mark.asyncio
    async def test_skill_name_none(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = None
        state["target"] = {"namespace": "default"}

        result = await safety_check(state)
        assert result["safety_status"] == "rejected"

    @pytest.mark.asyncio
    async def test_no_target(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = None

        result = await safety_check(state)
        assert result["safety_status"] == "rejected"
        assert "no target" in result["safety_reason"].lower()

    @pytest.mark.asyncio
    async def test_empty_target(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {}

        result = await safety_check(state)
        assert result["safety_status"] == "rejected"

    @pytest.mark.asyncio
    async def test_check_order_namespace_first(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "safety_blacklist_namespaces", "kube-system")

        state = sample_agent_state
        state["skill_name"] = ""
        state["target"] = {"namespace": "kube-system"}

        result = await safety_check(state)
        assert "blacklist" in result["safety_reason"].lower()

    @pytest.mark.asyncio
    async def test_check_order_skill_before_target(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = ""
        state["target"] = None

        result = await safety_check(state)
        assert "skill" in result["safety_reason"].lower()

    @pytest.mark.asyncio
    async def test_allowed_namespace(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "safety_blacklist_namespaces", "kube-system,kube-public")
        monkeypatch.setattr(settings, "kubeconfig_path", "")

        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "production", "names": ["my-app"]}

        result = await safety_check(state)
        assert result["safety_status"] == "safe"
