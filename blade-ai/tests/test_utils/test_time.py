"""Tests for chaos_agent.utils.time — Beijing-time timestamp generation and parsing."""

from datetime import datetime, timezone, timedelta

import pytest

from chaos_agent.utils.time import now_iso, parse_iso_timestamp


class TestNowIso:
    """now_iso() MUST produce Beijing-time ISO 8601 strings with +08:00 marker."""

    def test_returns_string(self):
        result = now_iso()
        assert isinstance(result, str)

    def test_ends_with_beijing_marker(self):
        result = now_iso()
        assert result.endswith("+08:00")

    def test_parseable_roundtrip(self):
        """now_iso() output MUST be parseable by parse_iso_timestamp."""
        ts = now_iso()
        dt = parse_iso_timestamp(ts)
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(hours=8)

    def test_beijing_time_not_utc(self):
        """now_iso() MUST produce Beijing time (+08:00), NOT UTC (+00:00)."""
        ts = now_iso()
        # Beijing time should NOT end with +00:00
        assert not ts.endswith("+00:00")
        assert "+08:00" in ts

    def test_beijing_hour_is_local(self):
        """The hour value in now_iso() should be Beijing local time (8h ahead of UTC)."""
        ts = now_iso()
        dt = parse_iso_timestamp(ts)
        utc_now = datetime.now(timezone.utc)
        # Beijing hour = UTC hour + 8 (allow 1 minute tolerance for test execution)
        expected_hour_range = (utc_now.hour + 8) % 24
        assert dt.hour == expected_hour_range or \
               (dt.hour == (expected_hour_range + 1) % 24 and utc_now.minute == 59)


class TestParseIsoTimestamp:
    """parse_iso_timestamp() MUST handle all three timestamp formats."""

    # --- Format 1: Beijing time with +08:00 suffix ---
    def test_beijing_time_with_offset(self):
        ts = "2026-05-12T19:51:47+08:00"
        dt = parse_iso_timestamp(ts)
        assert dt.year == 2026
        assert dt.hour == 19
        assert dt.utcoffset() == timedelta(hours=8)

    # --- Format 2: Kubernetes API with Z suffix ---
    def test_k8s_z_suffix(self):
        """Kubernetes API returns timestamps ending with Z.
        Python ≤3.10 fromisoformat() does NOT support Z.
        This is the core bug we are fixing."""
        ts = "2026-05-11T17:14:52Z"
        dt = parse_iso_timestamp(ts)
        assert dt.year == 2026
        assert dt.hour == 17
        assert dt.tzinfo == timezone.utc

    def test_k8s_z_suffix_with_microseconds(self):
        ts = "2026-05-12T08:30:00.123456Z"
        dt = parse_iso_timestamp(ts)
        assert dt.microsecond == 123456
        assert dt.tzinfo == timezone.utc

    # --- Format 3: UTC with +00:00 suffix ---
    def test_python_utc_with_offset(self):
        ts = "2026-05-12T08:30:00+00:00"
        dt = parse_iso_timestamp(ts)
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 8

    # --- Format 4: Bare local time (legacy, assumed UTC) ---
    def test_bare_local_time_assumed_utc(self):
        """Legacy datetime.now().isoformat() produces bare local time
        without timezone marker. parse_iso_timestamp MUST attach UTC."""
        ts = "2026-05-12T16:30:00"
        dt = parse_iso_timestamp(ts)
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 16  # preserves original value, just adds tzinfo

    # --- Cross-timezone comparison (THE KEY TEST) ---
    def test_beijing_vs_k8s_z_comparison(self):
        """Beijing-time internal timestamp vs K8s Z timestamp comparison.
        This is the EXACT scenario from task-36b2ec9d."""
        # now_iso() generates: 2026-05-12T19:51:47+08:00 (Beijing)
        # K8s finishedAt:       2026-05-11T17:14:52Z      (UTC)
        # Same instant comparison: Beijing 19:51 = UTC 11:51
        beijing_ts = "2026-05-12T11:51:47+08:00"  # injection start (Beijing)
        k8s_z_ts = "2026-05-11T17:14:52Z"          # OOMKill finishedAt (UTC)

        inject_dt = parse_iso_timestamp(beijing_ts)
        kill_dt = parse_iso_timestamp(k8s_z_ts)

        # OOMKill on May 11 UTC, injection on May 12 UTC (= May 11+8h Beijing)
        assert kill_dt < inject_dt, (
            f"OOMKill at {k8s_z_ts} MUST be before injection at {beijing_ts}"
        )

    def test_beijing_utc_same_instant(self):
        """Beijing 20:00 = UTC 12:00 — same instant, different representation."""
        beijing_ts = "2026-05-12T20:00:00+08:00"
        utc_ts = "2026-05-12T12:00:00+00:00"

        bj_dt = parse_iso_timestamp(beijing_ts)
        utc_dt = parse_iso_timestamp(utc_ts)

        assert bj_dt == utc_dt  # same instant, timezone-aware comparison works

    # --- Edge cases ---
    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="empty"):
            parse_iso_timestamp("")

    def test_garbage_string_raises(self):
        with pytest.raises(ValueError):
            parse_iso_timestamp("not-a-timestamp")

    def test_negative_offset(self):
        """Timestamps with negative UTC offset (e.g. -05:00) are valid."""
        ts = "2026-05-12T03:30:00-05:00"
        dt = parse_iso_timestamp(ts)
        assert dt.utcoffset() == timedelta(hours=-5)

    # --- Regression: the actual task data ---
    def test_task_36b2ec9d_beijing_vs_k8s(self):
        """Regression test: injection_start_time (now Beijing) vs
        K8s finishedAt (Z suffix).  Comparison must be timezone-safe."""
        # After the fix, injection_start_time will be Beijing time
        # e.g. 2026-05-12T19:51:47+08:00 (= UTC 11:51:47)
        # K8s finishedAt: 2026-05-11T17:14:52Z (= UTC 17:14:52)

        injection_beijing = "2026-05-12T19:51:47+08:00"  # Beijing = UTC+8
        oomkill_k8s = "2026-05-11T17:14:52Z"              # K8s UTC

        inject_dt = parse_iso_timestamp(injection_beijing)
        kill_dt = parse_iso_timestamp(oomkill_k8s)

        # Convert both to UTC for clarity:
        inject_utc = inject_dt.astimezone(timezone.utc)
        kill_utc = kill_dt.astimezone(timezone.utc)

        # Injection at UTC 11:51:47 May 12, OOMKill at UTC 17:14:52 May 11
        assert kill_utc < inject_utc