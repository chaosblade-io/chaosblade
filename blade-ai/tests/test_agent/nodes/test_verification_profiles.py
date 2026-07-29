"""Tests for the slimmed VerificationProfile seam.

After the knowledge-slimming refactor, ``VerificationProfile`` owns ONLY the
programmatic post-injection effect check. Verification KNOWLEDGE lives in the
data layer (skill case + knowledge docs), so ``_get_fault_verification_hints``
emits a pointer to the knowledge docs rather than hardcoded domain knowledge.
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.nodes.verify._verification_profiles import (
    VerificationContext,
    resolve_verification_profile,
)
from chaos_agent.agent.nodes.verify._verifier_hints import (
    _get_fault_verification_hints,
)


class TestPostInjectionCheckRegistry:
    """The one surviving code-side responsibility: each disk action declares the
    programmatic post-injection check the execute node runs; non-disk targets and
    unmatched actions declare none."""

    def _ctx(self, target, action):
        return VerificationContext(target=target, action=action)

    def test_disk_action_declares_matching_result_key(self):
        from chaos_agent.agent.nodes.execute._effect_checks import (
            _verify_disk_burn_effect,
            _verify_disk_fill_effect,
        )
        disk = resolve_verification_profile("disk")
        fill_specs = disk.post_injection_checks(self._ctx("disk", "fill"))
        burn_specs = disk.post_injection_checks(self._ctx("disk", "burn"))
        assert [(s.result_key, s.fn) for s in fill_specs] == [
            ("disk_fill_post_check", _verify_disk_fill_effect)
        ]
        assert [(s.result_key, s.fn) for s in burn_specs] == [
            ("disk_burn_post_check", _verify_disk_burn_effect)
        ]
        union = {s.result_key for s in fill_specs + burn_specs}
        assert union == {"disk_fill_post_check", "disk_burn_post_check"}

    def test_disk_unmatched_action_declares_none(self):
        disk = resolve_verification_profile("disk")
        assert disk.post_injection_checks(self._ctx("disk", "")) == ()
        assert disk.post_injection_checks(self._ctx("disk", "corrupt")) == ()

    @pytest.mark.parametrize("target", ["cpu", "mem", "network", "process", None])
    def test_non_disk_declares_no_post_checks(self, target):
        profile = resolve_verification_profile(target)
        assert profile.post_injection_checks(self._ctx(target, "fill")) == ()
        assert profile.post_injection_checks(self._ctx(target, "burn")) == ()


class TestSlimSeam:
    """The registry only carries fault types with a programmatic post-check;
    everything else falls back to the neutral default (knowledge comes from the
    data layer)."""

    def test_unknown_target_returns_default_empty(self):
        profile = resolve_verification_profile("no_such_target")
        ctx = VerificationContext(target="no_such_target", action="x")
        assert profile.post_injection_checks(ctx) == ()

    def test_none_and_empty_target_resolve_same(self):
        assert resolve_verification_profile(None) is resolve_verification_profile("")

    def test_only_disk_is_registered(self):
        # network / mem / process no longer carry code knowledge → default.
        for t in ("network", "mem", "process", "cpu"):
            assert resolve_verification_profile(t) is resolve_verification_profile("zzz")
        assert resolve_verification_profile("disk") is not resolve_verification_profile("zzz")


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
