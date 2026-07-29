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


def format_relative_time(ts: str, *, now: datetime | None = None) -> str:
    """Render an ISO timestamp as a compact Chinese relative label.

    Examples: ``今天 09:10`` / ``昨天 15:02`` / ``3天前 14:23``. Beyond 7 days
    it falls back to an absolute ``2026-06-20 14:23``. Empty or unparseable
    input returns ``""`` so callers can simply omit the prefix — this never
    raises.

    The day bucket is computed on the *calendar date* in Beijing time (so a
    22:00 injection and a 01:00 query the next morning read as "昨天", not
    "3 hours ago"); ``HH:MM`` is likewise shown in Beijing time.
    """
    if not ts:
        return ""
    try:
        dt = parse_iso_timestamp(ts)
    except (ValueError, TypeError):
        return ""
    dt_local = dt.astimezone(BEIJING_TZ)
    now_local = (now or datetime.now(BEIJING_TZ)).astimezone(BEIJING_TZ)
    hhmm = dt_local.strftime("%H:%M")
    day_diff = (now_local.date() - dt_local.date()).days
    if day_diff <= 0:
        return f"今天 {hhmm}"
    if day_diff == 1:
        return f"昨天 {hhmm}"
    if day_diff <= 6:
        return f"{day_diff}天前 {hhmm}"
    return dt_local.strftime("%Y-%m-%d %H:%M")