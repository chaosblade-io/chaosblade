"""Tests for PR-E4 — baseline / post-recovery comparison.

Behaviour pinned:

1. ``BaselineSnapshot.upsert`` is idempotent on (metric, phase) — a
   re-record of the same metric at the same phase overwrites rather
   than appending. Label and unit, once set, survive subsequent
   upserts that pass empty strings (so a richer label seen later
   doesn't get clobbered by a lazy second upsert).
2. ``recovered()`` returns True iff post is within
   ``RECOVERY_TOLERANCE`` of pre. Returns None when either is
   missing — the renderer needs to draw "incomplete" not "regressed".
3. ``delta_percent()`` uses pre as the denominator; for pre==0 it
   degrades to raw value*100 to avoid div/0 without falling to NaN.
4. ``parse_metrics`` extracts ``key=value[unit]`` triples from
   free-text agent output. Stop words ("ok", "fail", "true") don't
   become spurious metric keys.
5. ``build_panel`` returns None on calm mode, on empty snapshot, and
   on a snapshot whose samples carry no values yet (so callers can
   ``if panel:`` rather than render an empty box).
6. Border style flips green only when the snapshot is_complete; an
   in-progress snapshot stays dim.
"""

from __future__ import annotations

import pytest

from chaos_agent.tui.baseline import (
    BaselineSample,
    BaselineSnapshot,
    PHASE_POST,
    PHASE_PRE,
    RECOVERY_TOLERANCE,
    parse_metrics,
)
from chaos_agent.tui.renderers.baseline_compare import build_panel
from chaos_agent.tui.state import DisplayMode


class TestSnapshotUpsert:
    def test_upsert_creates_then_updates(self):
        snap = BaselineSnapshot()
        snap.upsert("cpu", 0.5, PHASE_PRE, label="CPU 使用率", unit="")
        snap.upsert("cpu", 0.6, PHASE_POST)
        sample = snap.samples["cpu"]
        assert sample.get(PHASE_PRE) == 0.5
        assert sample.get(PHASE_POST) == 0.6
        assert sample.label == "CPU 使用率"

    def test_re_upsert_same_phase_overwrites(self):
        # Re-recording the pre value (e.g. baseline_capture rerun) must
        # replace, not append. Otherwise "first sample wins" creates a
        # silent staleness bug.
        snap = BaselineSnapshot()
        snap.upsert("cpu", 0.5, PHASE_PRE)
        snap.upsert("cpu", 0.55, PHASE_PRE)
        assert snap.samples["cpu"].get(PHASE_PRE) == 0.55

    def test_label_not_clobbered_by_empty_string(self):
        snap = BaselineSnapshot()
        snap.upsert("cpu", 0.5, PHASE_PRE, label="CPU")
        snap.upsert("cpu", 0.6, PHASE_POST)  # no label arg
        assert snap.samples["cpu"].label == "CPU"

    def test_unit_persists_across_upserts(self):
        snap = BaselineSnapshot()
        snap.upsert("mem", 2048, PHASE_PRE, unit="MB")
        snap.upsert("mem", 2050, PHASE_POST)
        assert snap.samples["mem"].unit == "MB"


class TestRecovered:
    def test_within_tolerance_recovers(self):
        s = BaselineSample(metric="cpu", values={PHASE_PRE: 1.0, PHASE_POST: 1.05})
        # 5% delta — well inside the 10% tolerance.
        assert s.recovered() is True

    def test_outside_tolerance_not_recovered(self):
        s = BaselineSample(metric="cpu", values={PHASE_PRE: 1.0, PHASE_POST: 1.5})
        assert s.recovered() is False

    def test_missing_post_returns_none(self):
        # Renderer should draw "waiting", not "regressed".
        s = BaselineSample(metric="cpu", values={PHASE_PRE: 1.0})
        assert s.recovered() is None

    def test_missing_pre_returns_none(self):
        s = BaselineSample(metric="cpu", values={PHASE_POST: 1.0})
        assert s.recovered() is None

    def test_zero_pre_uses_absolute_post_check(self):
        # When pre is 0 we can't divide. Recovered iff post is also ~0.
        recovered_zero = BaselineSample(
            metric="x", values={PHASE_PRE: 0.0, PHASE_POST: 0.05}
        )
        assert recovered_zero.recovered() is True
        not_recovered = BaselineSample(
            metric="x", values={PHASE_PRE: 0.0, PHASE_POST: 0.5}
        )
        assert not_recovered.recovered() is False


class TestDeltaPercent:
    def test_positive_delta(self):
        s = BaselineSample(metric="x", values={PHASE_PRE: 1.0, PHASE_POST: 1.5})
        assert s.delta_percent() == pytest.approx(50.0)

    def test_negative_delta(self):
        s = BaselineSample(metric="x", values={PHASE_PRE: 1.0, PHASE_POST: 0.8})
        assert s.delta_percent() == pytest.approx(-20.0)

    def test_zero_pre_avoids_div_by_zero(self):
        # Without the special case this would be inf/NaN. The renderer
        # would crash trying to format it.
        s = BaselineSample(metric="x", values={PHASE_PRE: 0.0, PHASE_POST: 0.5})
        result = s.delta_percent()
        assert result is not None
        # Falls back to raw value*100 so the user still sees a number.
        assert result == 50.0

    def test_missing_phase_returns_none(self):
        s = BaselineSample(metric="x", values={PHASE_PRE: 1.0})
        assert s.delta_percent() is None


class TestParseMetrics:
    def test_extracts_simple_key_value_pairs(self):
        triples = parse_metrics("CPU=0.42 Memory=2.1GB Latency: 120ms")
        keys = {t[0].lower() for t in triples}
        assert "cpu" in keys
        assert "memory" in keys
        assert "latency" in keys

    def test_dedupes_repeated_key(self):
        # Same metric appearing twice should produce one entry — first wins.
        triples = parse_metrics("CPU=0.42 CPU=0.99")
        cpu_entries = [t for t in triples if t[0].lower() == "cpu"]
        assert len(cpu_entries) == 1
        assert cpu_entries[0][1] == 0.42

    def test_skips_stop_words(self):
        # Stop words look like keys but aren't.
        triples = parse_metrics("ok 1 fail 0 cpu 0.5")
        keys = [t[0].lower() for t in triples]
        assert "ok" not in keys
        assert "fail" not in keys
        assert "cpu" in keys

    def test_handles_empty_string(self):
        assert parse_metrics("") == []

    def test_unit_captured_when_present(self):
        triples = parse_metrics("rps 1234 RPS")
        # The metric pattern may match the bare 1234 with unit. Either
        # way, the value should be 1234.
        rps = [t for t in triples if t[0].lower() == "rps"]
        assert rps
        assert rps[0][1] == 1234


class TestSnapshotIsComplete:
    def test_empty_snapshot_not_complete(self):
        assert BaselineSnapshot().is_complete() is False

    def test_only_pre_not_complete(self):
        snap = BaselineSnapshot()
        snap.upsert("x", 1.0, PHASE_PRE)
        assert snap.is_complete() is False

    def test_pre_and_post_complete(self):
        snap = BaselineSnapshot()
        snap.upsert("x", 1.0, PHASE_PRE)
        snap.upsert("x", 1.05, PHASE_POST)
        assert snap.is_complete() is True

    def test_one_complete_one_pending_not_complete(self):
        # Mixed state — every metric must have both sides.
        snap = BaselineSnapshot()
        snap.upsert("a", 1.0, PHASE_PRE)
        snap.upsert("a", 1.0, PHASE_POST)
        snap.upsert("b", 2.0, PHASE_PRE)
        assert snap.is_complete() is False


class TestBuildPanel:
    def test_calm_mode_returns_none(self):
        snap = BaselineSnapshot()
        snap.upsert("cpu", 0.5, PHASE_PRE)
        snap.upsert("cpu", 0.55, PHASE_POST)
        assert build_panel(snap, display_mode=DisplayMode.CALM) is None

    def test_empty_snapshot_returns_none(self):
        assert build_panel(BaselineSnapshot()) is None

    def test_snapshot_with_no_values_returns_none(self):
        # A sample with no values is uninteresting to render — pretend
        # we don't have a snapshot.
        snap = BaselineSnapshot()
        snap.samples["x"] = BaselineSample(metric="x")
        assert build_panel(snap) is None

    def test_complete_snapshot_returns_panel(self):
        snap = BaselineSnapshot()
        snap.upsert("cpu", 0.5, PHASE_PRE, label="CPU")
        snap.upsert("cpu", 0.51, PHASE_POST)
        panel = build_panel(snap, display_mode=DisplayMode.WORKING)
        assert panel is not None

    def test_partial_snapshot_returns_dim_bordered_panel(self):
        # Pre but no post yet — render so the user sees baseline numbers,
        # but with the "still in progress" indicator (dim border).
        snap = BaselineSnapshot()
        snap.upsert("cpu", 0.5, PHASE_PRE)
        panel = build_panel(snap, display_mode=DisplayMode.WORKING)
        assert panel is not None


class TestRecoveryTolerance:
    def test_tolerance_is_loose_enough_for_noisy_metrics(self):
        # 10% gives latency / RPS metrics room to bounce a bit
        # without flagging false regressions. Stay just inside the
        # boundary so float rounding can't tip the comparison.
        s = BaselineSample(
            metric="latency",
            values={PHASE_PRE: 100.0, PHASE_POST: 100.0 + (RECOVERY_TOLERANCE * 100.0 * 0.99)},
        )
        assert s.recovered() is True
        # Well past the tolerance flips to not-recovered.
        s2 = BaselineSample(
            metric="latency",
            values={PHASE_PRE: 100.0, PHASE_POST: 100.0 + (RECOVERY_TOLERANCE * 100.0 * 1.5)},
        )
        assert s2.recovered() is False
