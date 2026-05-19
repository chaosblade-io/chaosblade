"""Time utilities — Beijing-time normalized timestamp generation and parsing.

All timestamps in blade-ai MUST use a single timezone.  Per team
convention this is **Asia/Shanghai (UTC+8)**, so that logs, SQLite data,
and task JSON are all readable in local time without mental conversion.

This module provides:
- ``now_iso()``: generate Beijing-time ISO 8601 timestamps (+08:00 marker)
- ``parse_iso_timestamp()``: parse ISO 8601 timestamps safely, including
  the ``Z`` suffix returned by Kubernetes API (which Python ≤3.10
  ``datetime.fromisoformat()`` does not support).

Comparison across timezones is safe: ``parse_iso_timestamp()`` always
returns timezone-aware datetimes, so ``<`` / ``>`` / ``==`` comparisons
between Beijing-time internal timestamps and K8s UTC (Z) timestamps
are correct regardless of the timezone representation.
"""

from datetime import datetime, timezone, timedelta

# China Standard Time — UTC+8
BEIJING_TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    """Return the current time as a Beijing-time ISO 8601 string.

    Output example: ``2026-05-12T19:51:47+08:00`` (Beijing time).
    Use this everywhere instead of bare ``datetime.now().isoformat()``.
    """
    return datetime.now(BEIJING_TZ).isoformat()


def parse_iso_timestamp(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp string into a timezone-aware datetime.

    Handles three formats that appear in blade-ai:
    1. ``2026-05-12T19:51:47+08:00`` — Beijing-time internal (from ``now_iso()``)
    2. ``2026-05-12T08:30:00Z`` — Kubernetes API (RFC 3339, UTC)
    3. ``2026-05-12T08:30:00+00:00`` — UTC with offset marker
    4. ``2026-05-12T08:30:00`` — bare local time (legacy; assumed UTC if no marker)

    For format 4, the returned datetime gets ``timezone.utc`` attached so
    that comparisons with formats 1-3 are always timezone-safe.

    Raises ``ValueError`` on genuinely unparseable strings.
    """
    if not ts:
        raise ValueError("empty timestamp string")

    # Kubernetes returns 'Z' suffix — Python ≤3.10 fromisoformat() rejects it.
    # Normalize to +00:00 before parsing.
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"

    dt = datetime.fromisoformat(ts)

    # If the parsed datetime is naive (no tzinfo), assume UTC.
    # This covers legacy ``datetime.now().isoformat()`` values that
    # have no timezone marker.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt