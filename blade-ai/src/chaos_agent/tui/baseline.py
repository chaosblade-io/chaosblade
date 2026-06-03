"""PR-E4 — baseline double-point sampling.

The agent's existing ``baseline_capture`` node samples metrics ONCE
before injection. The verifier (and recover_verifier) re-sample the
same metrics post-injection / post-recovery, but the TUI never
*compares* the two sides — it just shows numbers as they arrive.

This module models the comparison: a ``BaselineSample`` carries a
metric's value at each of three points in the experiment lifecycle
(pre-injection / mid-injection / post-recovery), and helper
functions parse the metric strings the agent emits. The renderer
in ``tui/renderers/baseline_compare.py`` consumes these snapshots
to draw the side-by-side bars.

Why parse strings rather than wire structured events through? The
verifier already emits human-readable metric output (``CPU 0.42 →
0.78`` style) into the result envelope. Adding a parallel structured
channel would require LLM-prompt changes that don't pay back at this
scope. Best-effort parsing of the existing strings is enough to
populate the comparison panel; structured wiring is a follow-up if
parsing accuracy turns out to be a problem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# Phase labels used as keys in BaselineSample.values. ``pre`` is set
# by baseline_capture; ``mid`` (during injection, optional) by
# verifier; ``post`` by recover_verifier.
PHASE_PRE = "pre"
PHASE_MID = "mid"
PHASE_POST = "post"

# Tolerance for "recovered" detection: post-value within 10 % of pre
# is treated as recovered. Looser than perfect equality so noisy
# metrics (CPU%, RPS) don't flag false regressions.
RECOVERY_TOLERANCE = 0.10


@dataclass
class BaselineSample:
    """One metric sampled at up to three lifecycle points.

    ``unit`` is informational only (rendered as a suffix on the value).
    ``label`` is what the user sees — defaults to the metric key but
    can be overridden if the agent emits something more readable.
    """

    metric: str
    label: str = ""
    unit: str = ""
    values: dict[str, float] = field(default_factory=dict)

    def get(self, phase: str) -> Optional[float]:
        return self.values.get(phase)

    def display_label(self) -> str:
        return self.label or self.metric

    def recovered(self) -> Optional[bool]:
        """True iff post is within RECOVERY_TOLERANCE of pre.

        Returns ``None`` when either sample is missing — caller should
        render an "incomplete" indicator rather than a green check.
        """
        pre = self.get(PHASE_PRE)
        post = self.get(PHASE_POST)
        if pre is None or post is None:
            return None
        if pre == 0:
            return abs(post) < RECOVERY_TOLERANCE
        return abs(post - pre) / abs(pre) <= RECOVERY_TOLERANCE

    def delta_percent(self) -> Optional[float]:
        """Signed pct change from pre to post, or None if either missing."""
        pre = self.get(PHASE_PRE)
        post = self.get(PHASE_POST)
        if pre is None or post is None:
            return None
        if pre == 0:
            # Express as raw delta when there's no baseline magnitude
            # to divide by — better than NaN/inf.
            return post * 100.0
        return ((post - pre) / abs(pre)) * 100.0


@dataclass
class BaselineSnapshot:
    """Collection of named samples for one task — what the panel renders.

    ``task_id`` lets the renderer key snapshots so a multi-channel view
    can show multiple experiments side by side (PR-E7's future need).
    """

    task_id: str = ""
    samples: dict[str, BaselineSample] = field(default_factory=dict)

    def upsert(
        self,
        metric: str,
        value: float,
        phase: str,
        *,
        label: str = "",
        unit: str = "",
    ) -> BaselineSample:
        """Add / update a sample. Returns the affected sample."""
        sample = self.samples.get(metric)
        if sample is None:
            sample = BaselineSample(metric=metric, label=label, unit=unit)
            self.samples[metric] = sample
        if label and not sample.label:
            sample.label = label
        if unit and not sample.unit:
            sample.unit = unit
        sample.values[phase] = float(value)
        return sample

    def is_complete(self) -> bool:
        """True when every sample has both pre and post values."""
        if not self.samples:
            return False
        return all(
            PHASE_PRE in s.values and PHASE_POST in s.values
            for s in self.samples.values()
        )

    def has_any(self) -> bool:
        return any(s.values for s in self.samples.values())


# ---------------------------------------------------------------------------
# Parsing helpers — extract numeric metrics from agent free-text output
# ---------------------------------------------------------------------------

# Match patterns like "CPU 0.42", "Memory: 2.1GB", "load_avg=0.45".
# We lean on key-value structure rather than try to NER unstructured text.
_METRIC_RE = re.compile(
    r"(?P<key>[A-Za-z][\w\.\-/]{0,40})"   # metric key
    r"\s*[:=]?\s*"                         # optional : or =
    r"(?P<num>-?\d+(?:\.\d+)?)"           # number
    r"\s*"
    r"(?P<unit>%|m?s|ms|GB|MB|KB|B|RPS|req/s|qps|ops/s)?",
    re.IGNORECASE,
)


def parse_metrics(text: str) -> list[tuple[str, float, str]]:
    """Extract ``(key, value, unit)`` triples from a free-text metric blob.

    Designed for the kind of output ``kubectl top pod`` / chaosblade
    verification emits — short labelled numbers separated by whitespace.
    Results are deduped on key (first occurrence wins) so the same
    label appearing twice doesn't double-count.

    Numbers without a recognisable unit get an empty unit string —
    the caller can fall back to the metric's stored unit (often known
    a priori from the BASELINE_COMMANDS registry).
    """
    out: list[tuple[str, float, str]] = []
    seen: set[str] = set()
    for match in _METRIC_RE.finditer(text or ""):
        key = match.group("key").strip()
        if not key or key.lower() in _STOP_KEYS:
            continue
        if key in seen:
            continue
        try:
            value = float(match.group("num"))
        except ValueError:
            continue
        unit = (match.group("unit") or "").strip()
        out.append((key, value, unit))
        seen.add(key)
    return out


# Words that show up in ad-hoc metric strings as "false positives" —
# they look like a key but they're really English glue.
_STOP_KEYS = frozenset({
    "and",
    "or",
    "the",
    "for",
    "in",
    "of",
    "to",
    "no",
    "yes",
    "ok",
    "fail",
    "error",
    "true",
    "false",
})
