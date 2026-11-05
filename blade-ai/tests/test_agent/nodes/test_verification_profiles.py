"""Tests for the slimmed VerificationProfile seam.

After the knowledge-slimming refactor, ``VerificationProfile`` owns ONLY the
deterministic verdict rules (finalize-time adjudication of measured numbers);
the former programmatic post-injection *sampling* seam was retired (sampling is
the ReAct loop's cognitive job, not an enumerable per-fault probe). Verification
KNOWLEDGE lives in the data layer (skill case + knowledge docs), so
``_get_fault_verification_hints`` emits a pointer to the knowledge docs rather
than hardcoded domain knowledge.
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.nodes.verify._verification_profiles import (
    resolve_verification_profile,
)
from chaos_agent.agent.nodes.verify._verifier_hints import (
    _get_fault_verification_hints,
)


class TestSlimSeam:
    """The registry only carries fault types with a deterministic verdict rule;
    everything else falls back to the neutral default (knowledge comes from the
    data layer)."""

    def test_unknown_target_returns_default_empty(self):
        profile = resolve_verification_profile("no_such_target")
        assert profile.deterministic_rules() == ()

    def test_none_and_empty_target_resolve_same(self):
        assert resolve_verification_profile(None) is resolve_verification_profile("")

    def test_only_asset_carrying_families_are_registered(self):
        # Families without a deterministic verdict rule → default. "process"
        # joined "disk" when the kill rule landed; cpu/mem joined when their
        # placeholder rules were declared (pinned to unknown — no runtime
        # behaviour); network still carries no code asset at all.
        for t in ("network",):
            assert resolve_verification_profile(t) is resolve_verification_profile("zzz")
        for t in ("disk", "process", "cpu", "mem"):
            assert resolve_verification_profile(t) is not resolve_verification_profile("zzz")


class TestFaultHintsCarryNoKnowledge:
    """``_get_fault_verification_hints`` must emit fault metadata + a knowledge-doc
    POINTER, and must NOT hardcode the domain knowledge that now lives in the
    skill case / knowledge docs."""

    def test_emits_metadata_and_doc_pointer(self):
        out = _get_fault_verification_hints("node", "disk", "fill", {"path": "/var/log"})
        assert "Scope: node" in out and "Target: disk" in out
        # Pointer to the data layer, not the knowledge itself.
        assert "read_knowledge_resource" in out
        assert "fault-verification-strategies.md" in out

    @pytest.mark.parametrize("target,action", [("disk", "fill"), ("disk", "burn"), ("network", "dns")])
    def test_no_hardcoded_domain_knowledge(self, target, action):
        out = _get_fault_verification_hints("node", target, action, {"path": "/tmp"})
        # None of the former hardcoded K8s knowledge leaks from code anymore.
        for banned in ("imagefs", "nodefs", "/proc/diskstats", "df -h /host",
                       "container overlay", "/etc/hosts", "chaos_filldisk"):
            assert banned not in out
