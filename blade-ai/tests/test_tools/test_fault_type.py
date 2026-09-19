"""Tests for fault_type duration utilities: ensure_min_duration, get_recommended_duration."""

import logging

import pytest

from chaos_agent.config.settings import blade_ai_context
from chaos_agent.utils.fault_type import (
    _DEFAULT_MIN_DURATION,
    _FAULT_TYPE_MIN_DURATION,
    ensure_min_duration,
    get_recommended_duration,
)


@pytest.fixture(autouse=True)
def _isolate_experiment_timeout():
    """Pin the operator-configured default to the code default.

    Floor assertions must not depend on the host machine's
    ~/.blade-ai/config.json: an installed config carrying a stale
    experiment_timeout would win over the code default in every
    unspecified-duration path (max(configured, floor)) and break
    these tests on that machine only. TestExperimentTimeoutWiring
    re-overrides inside its own context, which nests correctly.
    """
    with blade_ai_context(experiment_timeout=_DEFAULT_MIN_DURATION):
        yield


class TestGetRecommendedDuration:
    """Tests for get_recommended_duration()."""

    def test_known_fault_type_node_disk_fill(self):
        assert get_recommended_duration("node", "disk", "fill") == 300

    def test_known_fault_type_pod_cpu_fullload(self):
        assert get_recommended_duration("pod", "cpu", "fullload") == 300

    def test_known_fault_type_container_cpu_fullload(self):
        assert get_recommended_duration("container", "cpu", "fullload") == 300

    def test_unknown_fault_type_returns_default(self):
        assert get_recommended_duration("pod", "io", "stress") == _DEFAULT_MIN_DURATION

    def test_all_entries_are_at_least_300(self):
        """Every entry in _FAULT_TYPE_MIN_DURATION must be >= 300."""
        for key, value in _FAULT_TYPE_MIN_DURATION.items():
            assert value >= 300, f"{key}: {value}s is below 300s minimum"


class TestEnsureMinDuration:
    """Tests for ensure_min_duration()."""

    def test_none_timeout_returns_recommended(self):
        assert ensure_min_duration(None, "node", "disk", "fill") == 300

    def test_zero_timeout_returns_recommended(self):
        assert ensure_min_duration(0, "pod", "cpu", "fullload") == 300

    def test_explicit_short_timeout_preserved_with_warning(self, caplog):
        """Explicit durations are honoured verbatim — never silently raised.

        The boost-to-floor behaviour was retired (l4-contract-faithfulness):
        the executor must not amend a contract-stated duration in either
        direction; the requested-vs-recommended gap is surfaced as a
        warning instead.
        """
        with caplog.at_level(logging.WARNING, logger="chaos_agent.utils.fault_type"):
            assert ensure_min_duration(60, "pod", "cpu", "fullload") == 60
        assert "below the recommended 300s floor" in caplog.text
        assert "applying the requested 60s verbatim" in caplog.text

    def test_explicit_string_timeout_preserved_with_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="chaos_agent.utils.fault_type"):
            assert ensure_min_duration("60", "node", "network", "delay") == 60
        assert "below the recommended" in caplog.text

    def test_sufficient_timeout_not_reduced(self):
        assert ensure_min_duration(800, "pod", "cpu", "fullload") == 800

    def test_exact_minimum_not_changed(self):
        assert ensure_min_duration(300, "pod", "cpu", "fullload") == 300

    def test_none_scope_target_action_uses_default(self):
        assert ensure_min_duration(0, None, None, None) == _DEFAULT_MIN_DURATION

    def test_partial_scope_info_uses_default(self):
        assert ensure_min_duration(0, "pod", None, "fullload") == _DEFAULT_MIN_DURATION

    def test_invalid_string_returns_recommended(self):
        assert ensure_min_duration("abc", "pod", "cpu", "fullload") == 300

    def test_empty_string_returns_recommended(self):
        assert ensure_min_duration("", "node", "disk", "fill") == 300

    def test_default_min_duration_is_300(self):
        assert _DEFAULT_MIN_DURATION == 300


class TestExperimentTimeoutWiring:
    """settings.experiment_timeout feeds the unspecified-timeout default."""

    def test_configured_above_floor_used_when_unspecified(self):
        with blade_ai_context(experiment_timeout=1800):
            assert ensure_min_duration(None, "pod", "cpu", "fullload") == 1800

    def test_configured_above_floor_used_for_unknown_fault_type(self):
        with blade_ai_context(experiment_timeout=1800):
            assert ensure_min_duration(0, None, None, None) == 1800

    def test_configured_below_floor_clamped_to_300(self):
        with blade_ai_context(experiment_timeout=60):
            assert ensure_min_duration(None, "pod", "cpu", "fullload") == 300
            assert ensure_min_duration(0, None, None, None) == 300

    def test_explicit_timeout_unaffected_by_configured_default(self):
        with blade_ai_context(experiment_timeout=1800):
            # Explicit value above the floor passes through untouched.
            assert ensure_min_duration(900, "pod", "cpu", "fullload") == 900
            # Explicit value below the floor stays at the requested value —
            # neither the floor nor the configured default lifts it.
            assert ensure_min_duration(60, "pod", "cpu", "fullload") == 60
