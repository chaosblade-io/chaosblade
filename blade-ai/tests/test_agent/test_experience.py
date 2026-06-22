"""Tests for AGENT.md experience accumulation."""

from __future__ import annotations


def test_append_experience_uses_fault_spec_fault_type(tmp_path, monkeypatch):
    from chaos_agent.agent import experience

    monkeypatch.setattr(experience, "AGENT_MD_PATH", tmp_path / "AGENT.md")

    result = experience.append_experience(
        "",
        {
            "skill_name": "stale-skill",
            "fault_spec": {
                "namespace": "cms-demo",
                "scope": "pod",
                "names": ["pod-a"],
                "labels": {},
                "blade_target": "network",
                "blade_action": "loss",
                "params": {},
                "params_flags": [],
                "duration_seconds": 0,
                "source": "test",
                "user_description": "",
            },
            "error": "planning rejected",
        },
    )

    text = (tmp_path / "AGENT.md").read_text(encoding="utf-8")
    assert result["status"] == "appended"
    assert result["category"] == "Fault Injection"
    assert "Issue with pod-network-loss" in text
    assert "Issue with stale-skill" not in text
