"""Tests for fault_type duration utilities: ensure_min_duration, get_recommended_duration."""

import pytest

from chaos_agent.utils.fault_type import (
    _DEFAULT_MIN_DURATION,
    _FAULT_TYPE_MIN_DURATION,
    ensure_min_duration,
    get_recommended_duration,
)


class TestGetRecommendedDuration:
    """Tests for get_recommended_duration()."""

    def test_known_fault_type_node_disk_fill(self):
        assert get_recommended_duration("node", "disk", "fill") == 600

    def test_known_fault_type_pod_cpu_fullload(self):
        assert get_recommended_duration("pod", "cpu", "fullload") == 600

    def test_known_fault_type_container_cpu_fullload(self):
        assert get_recommended_duration("container", "cpu", "fullload") == 600

    def test_unknown_fault_type_returns_default(self):
        assert get_recommended_duration("pod", "io", "stress") == _DEFAULT_MIN_DURATION

    def test_all_entries_are_at_least_600(self):
        """Every entry in _FAULT_TYPE_MIN_DURATION must be >= 600."""
        for key, value in _FAULT_TYPE_MIN_DURATION.items():
            assert value >= 600, f"{key}: {value}s is below 600s minimum"


class TestEnsureMinDuration:
    """Tests for ensure_min_duration()."""

    def test_none_timeout_returns_recommended(self):
        assert ensure_min_duration(None, "node", "disk", "fill") == 600

    def test_zero_timeout_returns_recommended(self):
        assert ensure_min_duration(0, "pod", "cpu", "fullload") == 600

    def test_short_timeout_boosted_to_600(self):
        assert ensure_min_duration(60, "pod", "cpu", "fullload") == 600

    def test_string_timeout_boosted(self):
        assert ensure_min_duration("60", "node", "network", "delay") == 600

    def test_sufficient_timeout_not_reduced(self):
        assert ensure_min_duration(800, "pod", "cpu", "fullload") == 800

    def test_exact_minimum_not_changed(self):
        assert ensure_min_duration(600, "pod", "cpu", "fullload") == 600

    def test_none_scope_target_action_uses_default(self):
        assert ensure_min_duration(0, None, None, None) == _DEFAULT_MIN_DURATION

    def test_partial_scope_info_uses_default(self):
        assert ensure_min_duration(0, "pod", None, "fullload") == _DEFAULT_MIN_DURATION

    def test_invalid_string_returns_recommended(self):
        assert ensure_min_duration("abc", "pod", "cpu", "fullload") == 600

    def test_empty_string_returns_recommended(self):
        assert ensure_min_duration("", "node", "disk", "fill") == 600

    def test_default_min_duration_is_600(self):
        assert _DEFAULT_MIN_DURATION == 600
