"""Tests for FCAT (Fault Context Adaptation Table) fail-closed behavior.

Core invariants under test:
  1. compute_safe_burn_size(None) → 20 (fail-closed, NOT 100)
  2. compute_safe_burn_size(0) → 20
  3. compute_safe_burn_size(low_memory) → small safe value
  4. P0 rules match when pod_memory_limit_mb is missing (empty dict)
  5. P0 rules match when pod_memory_limit_mb < threshold
  6. P0 rules do NOT match when pod_memory_limit_mb >= threshold
  7. lookup_adaptations handles empty dict without error
"""

from __future__ import annotations

from chaos_agent.utils.fault_context import (
    _BURN_DEFAULT_SIZE,
    _BURN_MINIMUM_SIZE,
    _OOMKILL_RISK_THRESHOLD_MB,
    compute_safe_burn_size,
    lookup_adaptations,
)


# ---------------------------------------------------------------------------
# compute_safe_burn_size
# ---------------------------------------------------------------------------

class TestComputeSafeBurnSize:
    """Fail-closed: unknown memory → minimum safe size (20), NOT default (100).

    Formula: limit // 7 (reserves 70% for app, splits 30% across 2 dd procs).
    With usage: (limit - usage) // 3 (2 dd procs + 50% safety margin).
    """

    def test_none_returns_minimum(self):
        """Unknown memory must default to minimum safe value (fail-closed)."""
        assert compute_safe_burn_size(None) == _BURN_MINIMUM_SIZE

    def test_zero_returns_minimum(self):
        assert compute_safe_burn_size(0) == _BURN_MINIMUM_SIZE

    def test_negative_returns_minimum(self):
        assert compute_safe_burn_size(-100) == _BURN_MINIMUM_SIZE

    def test_small_pod_returns_small_size(self):
        """240Mi pod: 240 // 7 = 34, which is < 100 default."""
        assert compute_safe_burn_size(240) == 34

    def test_medium_pod_returns_capped(self):
        """700Mi pod: 700 // 7 = 100, exactly at cap."""
        assert compute_safe_burn_size(700) == _BURN_DEFAULT_SIZE

    def test_large_pod_returns_default(self):
        """4096Mi pod: 4096 // 7 = 585, capped at 100."""
        assert compute_safe_burn_size(4096) == _BURN_DEFAULT_SIZE

    def test_threshold_boundary(self):
        """Pod at exactly the OOMKill risk threshold (512Mi): 512 // 7 = 73."""
        assert compute_safe_burn_size(512) == 73

    def test_below_threshold(self):
        """Pod just below threshold (511Mi): 511 // 7 = 73."""
        assert compute_safe_burn_size(511) == 73

    def test_very_small_pod(self):
        """128Mi pod: 128 // 7 = 18, floored at 20."""
        assert compute_safe_burn_size(128) == _BURN_MINIMUM_SIZE

    def test_minimum_observable_pod(self):
        """64Mi pod: 64 // 7 = 9, floored at 20."""
        assert compute_safe_burn_size(64) == _BURN_MINIMUM_SIZE

    def test_140mb_pod(self):
        """140Mi pod: 140 // 7 = 20, exactly minimum."""
        assert compute_safe_burn_size(140) == _BURN_MINIMUM_SIZE


class TestComputeSafeBurnSizeWithUsage:
    """Usage-aware size calculation: considers actual memory usage."""

    def test_usage_none_falls_back_to_limit_only(self):
        """No usage data → same as limit-only formula."""
        assert compute_safe_burn_size(240, pod_memory_usage_mb=None) == 240 // 7

    def test_usage_known_reduces_size(self):
        """240MB limit, 208MB used → available=32, 32//3=10, floored at 20."""
        assert compute_safe_burn_size(240, pod_memory_usage_mb=208) == _BURN_MINIMUM_SIZE

    def test_usage_low_allows_larger_size(self):
        """512MB limit, 100MB used → available=412, 412//3=137, capped at 100."""
        assert compute_safe_burn_size(512, pod_memory_usage_mb=100) == _BURN_DEFAULT_SIZE

    def test_usage_at_limit_returns_minimum(self):
        """240MB limit, 240MB used → available=0, returns minimum."""
        assert compute_safe_burn_size(240, pod_memory_usage_mb=240) == _BURN_MINIMUM_SIZE

    def test_usage_over_limit_returns_minimum(self):
        """240MB limit, 300MB used → available<0, returns minimum."""
        assert compute_safe_burn_size(240, pod_memory_usage_mb=300) == _BURN_MINIMUM_SIZE

    def test_usage_moderate(self):
        """512MB limit, 256MB used → available=256, 256//3=85."""
        assert compute_safe_burn_size(512, pod_memory_usage_mb=256) == 85


# ---------------------------------------------------------------------------
# P0 rule condition: fail-closed on missing key
# ---------------------------------------------------------------------------

class TestP0Conditions:
    """P0 rules must fire when pod_memory_limit_mb is missing (empty dict).

    The old `or 9999` pattern made missing key evaluate as 9999 < 512 → False,
    so P0 would never fire for unknown memory. The fix uses `is None or < threshold`
    so missing key evaluates as True → P0 fires → safe size is used.
    """

    SCOPE = "pod"
    TARGET = "disk"
    ACTION = "burn"

    def test_empty_dict_triggers_p0_param_safety(self):
        """Empty dict (memory unknown) must trigger P0-param-safety-burn-lowmem."""
        results = lookup_adaptations(
            self.SCOPE, self.TARGET, self.ACTION, {},
            rule_type="param_override",
        )
        ids = [r.id for r in results]
        assert "P0-param-safety-burn-lowmem" in ids

    def test_empty_dict_triggers_p0_evidence_snapshot(self):
        """Empty dict must trigger P0-evidence-snapshot."""
        results = lookup_adaptations(
            self.SCOPE, self.TARGET, self.ACTION, {},
            rule_type="param_override",
        )
        ids = [r.id for r in results]
        assert "P0-evidence-snapshot" in ids

    def test_low_memory_triggers_p0(self):
        """240Mi pod must trigger P0 rules."""
        metadata = {"pod_memory_limit_mb": 240}
        results = lookup_adaptations(
            self.SCOPE, self.TARGET, self.ACTION, metadata,
            rule_type="param_override",
        )
        ids = [r.id for r in results]
        assert "P0-param-safety-burn-lowmem" in ids
        assert "P0-evidence-snapshot" in ids

    def test_high_memory_does_not_trigger_p0(self):
        """1024Mi pod must NOT trigger P0 rules."""
        metadata = {"pod_memory_limit_mb": 1024}
        results = lookup_adaptations(
            self.SCOPE, self.TARGET, self.ACTION, metadata,
            rule_type="param_override",
        )
        ids = [r.id for r in results]
        assert "P0-param-safety-burn-lowmem" not in ids
        assert "P0-evidence-snapshot" not in ids

    def test_threshold_memory_does_not_trigger_p0(self):
        """Pod at exactly 512Mi must NOT trigger P0 rules (not < threshold)."""
        metadata = {"pod_memory_limit_mb": _OOMKILL_RISK_THRESHOLD_MB}
        results = lookup_adaptations(
            self.SCOPE, self.TARGET, self.ACTION, metadata,
            rule_type="param_override",
        )
        ids = [r.id for r in results]
        assert "P0-param-safety-burn-lowmem" not in ids

    def test_wrong_scope_no_match(self):
        """Node scope must not trigger P0 pod-disk-burn rules."""
        results = lookup_adaptations(
            "node", self.TARGET, self.ACTION, {},
            rule_type="param_override",
        )
        ids = [r.id for r in results]
        assert "P0-param-safety-burn-lowmem" not in ids

    def test_wrong_action_no_match(self):
        """pod-disk-fill must not trigger P0 burn rules."""
        results = lookup_adaptations(
            self.SCOPE, self.TARGET, "fill", {},
            rule_type="param_override",
        )
        ids = [r.id for r in results]
        assert "P0-param-safety-burn-lowmem" not in ids

    def test_priority_ordering(self):
        """P0-param-safety-burn-lowmem (priority=10) must come before
        P0-evidence-snapshot (priority=5)."""
        metadata = {"pod_memory_limit_mb": 240}
        results = lookup_adaptations(
            self.SCOPE, self.TARGET, self.ACTION, metadata,
            rule_type="param_override",
        )
        param_safety_idx = next(
            i for i, r in enumerate(results) if r.id == "P0-param-safety-burn-lowmem"
        )
        evidence_idx = next(
            i for i, r in enumerate(results) if r.id == "P0-evidence-snapshot"
        )
        assert param_safety_idx < evidence_idx


# ---------------------------------------------------------------------------
# End-to-end: empty dict → P0 fires → compute_safe_burn_size → 20
# ---------------------------------------------------------------------------

class TestFailClosedEndToEnd:
    """Simulate the full chain: empty metadata → P0 match → safe size = 20.

    This reproduces the original incident scenario:
    - _collect_context() returns {}
    - if target_metadata: was False (old bug) → FCAT skipped
    - Now: if target_metadata is not None: → FCAT evaluates
    - P0 condition: is None → True → rule matches
    - compute_safe_burn_size(None) → 20 (not 100)
    """

    def test_empty_dict_produces_safe_size_20(self):
        metadata = {}
        results = lookup_adaptations(
            "pod", "disk", "burn", metadata,
            rule_type="param_override",
        )
        param_safety = next(
            (r for r in results if r.id == "P0-param-safety-burn-lowmem"), None,
        )
        assert param_safety is not None
        assert param_safety.action["param_overrides"]["size"] == "auto"

        # When size=auto, compute_safe_burn_size is called with the
        # missing key value → None → returns 20
        mem_limit = metadata.get("pod_memory_limit_mb")  # None
        safe_size = compute_safe_burn_size(mem_limit)
        assert safe_size == _BURN_MINIMUM_SIZE  # 20, not 100
