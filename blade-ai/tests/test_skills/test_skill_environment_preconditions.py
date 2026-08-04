"""Each chaos skill's description must state its execution environment.

A real k8s-channel session activated ``host-chaos-skills`` and queried cluster
nodes as if they were injectable hosts: the Capability Profile section said
"Kubernetes", yet the Skill Index still advertised the host skill with "立即使用"
and no environment precondition, so the model had no basis to notice the
mismatch until the tool-layer gate refused it — which reads as the model being
unaware rather than guided.

The fix is soft (telling, not enforcing): every skill's description carries its
environment precondition, which ``get_skill_index_section`` keeps verbatim, so
the model can pair it with the Capability Profile and warn before submitting.
The tool-layer gate remains the backstop.

Wording constraints (from the product owner): plain language, no transport
jargon (``profile`` / ``kubeconfig`` / ``kubewiz`` / 能力画像) in the added
sentences; body text is deliberately left untouched.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml

_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


def _description(skill: str) -> str:
    text = io.open(_SKILLS_DIR / skill / "SKILL.md", encoding="utf-8").read()
    front = yaml.safe_load(text.split("---")[1])
    return front["description"]


def test_host_skill_requires_host_environment():
    desc = _description("host-chaos-skills")
    assert "只能在主机环境" in desc
    assert "Kubernetes 集群" in desc  # names the mismatching channel explicitly


def test_k8s_skill_requires_cluster_environment():
    desc = _description("k8s-chaos-skills")
    assert "只能在 Kubernetes 集群环境" in desc
    assert "主机" in desc  # names the mismatching channel explicitly


def test_python_skill_states_both_preconditions():
    """Python faults resolve to the *host* profile — not a third channel — and
    additionally need the in-process agent."""
    desc = _description("python-app-chaos-skills")
    assert "只能在主机环境" in desc          # same channel as host faults
    assert "Kubernetes 集群" in desc         # cannot run on the cluster channel
    assert "探针" in desc                    # extra precondition: the agent


@pytest.mark.parametrize("skill", [
    "host-chaos-skills",
    "k8s-chaos-skills",
    "python-app-chaos-skills",
])
def test_precondition_is_stated_once_and_jargon_free(skill):
    """One "适用环境" clause, no leftover technical "适用前提", no jargon.

    The earlier fix left a jargon-worded "适用前提" line in host/k8s; this one
    replaces it rather than stacking a second, near-duplicate sentence.
    """
    desc = _description(skill)
    assert desc.count("适用环境") == 1, "expected exactly one environment clause"
    assert "适用前提" not in desc, "old jargon-worded clause must be replaced, not kept"

    # Jargon must not appear in the added environment clause. Body text (kept
    # verbatim) may still mention kubeconfig/kubewiz, so scope the check to the
    # sentence that starts at 适用环境.
    start = desc.index("适用环境")
    clause = desc[start:start + 200]
    for banned in ("profile", "PROFILE", "kubeconfig", "kubewiz", "KubeWiz", "能力画像"):
        assert banned not in clause, f"{banned!r} leaked into the {skill} environment clause"


@pytest.mark.parametrize("skill", [
    "host-chaos-skills",
    "k8s-chaos-skills",
    "python-app-chaos-skills",
])
def test_skill_md_still_parses(skill):
    text = io.open(_SKILLS_DIR / skill / "SKILL.md", encoding="utf-8").read()
    front = yaml.safe_load(text.split("---")[1])
    assert front["skill_type"] == "fault-injection"
    assert front["name"] == skill
