"""Multi-dimensional numeric safety score for fault injection.

Adds a 0-100 score per dimension (blast_radius, frequency, time,
topology) plus an aggregated weighted overall score. Purely advisory
by default; when ``settings.safety_score_routing_enabled`` is true,
high overall can upgrade ``safety_status`` from safe → warning →
confirm_required.

This module is intentionally pure (no I/O, no async). Any K8s deep
signals required by the topology dimension must be fetched by the
caller (typically the async ``safety_check`` node) and passed in
through the ``context`` dict, so this module stays unit-testable
without mocking kubectl or the event loop.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from chaos_agent.utils.time import BEIJING_TZ

if TYPE_CHECKING:
    from chaos_agent.agent.fault_spec import FaultSpec

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS: dict[str, float] = {
    "blast_radius": 0.40,
    "topology": 0.30,
    "frequency": 0.20,
    "time": 0.10,
}

_SCOPE_BASE: dict[str, int] = {
    "container": 10,
    "pod": 30,
    "deployment": 50,
    "service": 50,
    "node": 70,
    "namespace": 80,
    "cluster": 100,
}

_PRODUCTION_NS_PATTERNS = ("prod", "production", "live")
_CRITICAL_NAME_PATTERNS = frozenset({
    "api", "db", "database", "redis", "kafka",
    "auth", "gateway", "ingress",
})
_CLUSTER_SCOPED_SCOPES = ("node", "cluster")

# Token separator for K8s resource names (Pods are usually
# ``<deploy>-<replicaset-hash>-<pod-suffix>``; Deployments use ``-`` /
# ``.``; cluster-scoped resources sometimes use ``/``). Splitting on
# this set turns ``accounting-6fbdb464c7-qn2vr`` into the token bag
# ``{accounting, 6fbdb464c7, qn2vr}`` so the critical-name check
# only fires on an exact token match — not on a hash that incidentally
# contains the substring ``db`` (e.g. ``6fbdb...`` → ``bdb`` → ``db``).
_NAME_TOKEN_SEP = re.compile(r"[-_./]+")


def _name_tokens(name: str) -> set[str]:
    """Split a resource name into lowercase tokens for exact matching."""
    return {t for t in _NAME_TOKEN_SEP.split(name.lower()) if t}

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DimensionScore:
    """A single dimension's score with a human-readable rationale."""
    value: int          # 0-100
    explanation: str


@dataclass(frozen=True)
class SafetyScore:
    """Aggregated multi-dimensional safety score."""
    blast_radius: DimensionScore
    frequency: DimensionScore
    time: DimensionScore
    topology: DimensionScore
    overall: int        # 0-100, weighted sum
    level: str          # low / medium / high / critical
    weights: dict[str, float]

    def to_dict(self) -> dict:
        return {
            "blast_radius": asdict(self.blast_radius),
            "frequency": asdict(self.frequency),
            "time": asdict(self.time),
            "topology": asdict(self.topology),
            "overall": self.overall,
            "level": self.level,
            "weights": dict(self.weights),
        }


def _score_to_level(score: int) -> str:
    if score < 30:
        return "low"
    if score < 60:
        return "medium"
    if score < 80:
        return "high"
    return "critical"


# ---------------------------------------------------------------------------
# Individual dimension scorers (pure)
# ---------------------------------------------------------------------------


def _score_blast_radius(
    spec: "FaultSpec",
    context: dict | None = None,
) -> DimensionScore:
    # An empty scope is a degenerate input (safety_check rejects these
    # before E10 gets called in practice, but rejected paths still call
    # the scorer for display consistency). Treat as 0 instead of falling
    # through to defaults that would otherwise inflate to ~60 via the
    # namespace-wide modifier (no names + no labels).
    if not spec.scope:
        return DimensionScore(value=0, explanation="no scope specified")

    scope_base = _SCOPE_BASE.get(spec.scope, 30)

    if spec.is_namespace_wide:
        count_mod, count_desc = 30, "namespace-wide"
    else:
        n = len(spec.names)
        if n <= 1:
            count_mod, count_desc = 0, f"{n} target"
        elif n <= 5:
            count_mod, count_desc = 10, f"{n} targets"
        elif n <= 20:
            count_mod, count_desc = 25, f"{n} targets"
        else:
            count_mod, count_desc = 40, f"{n} targets"

    value = min(100, scope_base + count_mod)
    explanation = (
        f"scope={spec.scope or 'unknown'} ({scope_base}), "
        f"{count_desc} (+{count_mod})"
    )

    # Execution-level blast radius override: the LLM declares whether
    # the actual implementation mutates resources beyond the target
    # (e.g. tainting all cluster nodes for a deployment-scoped fault).
    # This overrides the FaultSpec-based score when the execution scope
    # is wider than the target scope.
    ctx = context or {}
    br_scope = ctx.get("blast_radius_scope", "")
    br_detail = ctx.get("blast_radius_detail", "")
    if br_scope == "cluster-wide" and value < 90:
        value = 90
        explanation += f"; execution={br_scope} (override→90)"
        if br_detail:
            explanation += f" [{br_detail}]"
    elif br_scope == "namespace-wide" and value < 60:
        value = 60
        explanation += f"; execution={br_scope} (override→60)"
        if br_detail:
            explanation += f" [{br_detail}]"

    return DimensionScore(value=value, explanation=explanation)


def _score_frequency(context: dict) -> DimensionScore:
    n_conflicts = len(context.get("conflict_uids") or [])
    score = min(60, n_conflicts * 20)

    attempt = int(context.get("pipeline_attempt") or 0)
    if attempt >= 1:
        score += 30
    if attempt >= 3:
        score += 20

    score = min(100, score)

    parts: list[str] = []
    if n_conflicts > 0:
        parts.append(f"{n_conflicts} active conflict(s)")
    if attempt > 0:
        parts.append(f"attempt {attempt}")
    if not parts:
        parts.append("no conflicts, first attempt")

    return DimensionScore(value=score, explanation=", ".join(parts))


def _score_time(spec: "FaultSpec", context: dict) -> DimensionScore:
    """Time-of-day + duration risk.

    Uses ``BEIJING_TZ`` (UTC+8) per the project-wide timezone convention
    documented in ``chaos_agent.utils.time``. Reading ``.hour`` /
    ``.weekday`` from a Beijing-aware datetime gives Beijing wall-clock
    values regardless of the server's process TZ, so "business hours
    9-18" always means 9:00-18:00 Beijing time — what every other
    project timestamp uses.

    ``context['current_time']`` may be a naive datetime (trusted as
    Beijing wall-clock) or a tz-aware datetime in any zone (normalised
    to BEIJING_TZ before reading the wall-clock). Without normalisation
    a UTC-aware override would silently mis-score: e.g. UTC 08:00 =
    Beijing 16:00 (business) but ``.hour`` on the UTC value reads 8.
    """
    raw_now: datetime = context.get("current_time") or datetime.now(BEIJING_TZ)
    now: datetime = (
        raw_now.astimezone(BEIJING_TZ) if raw_now.tzinfo is not None else raw_now
    )
    hour = now.hour
    score = 0
    parts: list[str] = []

    if 9 <= hour < 18:
        score += 50
        parts.append(f"business hours ({hour:02d}:xx)")
    elif 6 <= hour < 9 or 18 <= hour < 22:
        score += 20
        parts.append(f"extended hours ({hour:02d}:xx)")
    else:
        parts.append(f"off-hours ({hour:02d}:xx)")

    if now.weekday() < 5:
        score += 20
        parts.append("weekday")
    else:
        parts.append("weekend")

    duration = int(getattr(spec, "duration_seconds", 0) or 0)
    if duration == 0:
        score += 30
        parts.append("permanent injection")
    elif duration > 600:
        score += 20
        parts.append(f"long duration ({duration}s)")
    elif duration > 60:
        score += 10
        parts.append(f"moderate duration ({duration}s)")
    else:
        parts.append(f"short duration ({duration}s)")

    return DimensionScore(value=min(100, score), explanation=", ".join(parts))


def _score_topology(spec: "FaultSpec", context: dict) -> DimensionScore:
    """Heuristic + optional deep K8s signal.

    The deep signal is computed by the caller (async safety_check) and
    passed via ``context['topology_deep_signal']`` as a ``(int, str)``
    tuple. This keeps this scorer pure-sync and testable.
    """
    score = 0
    parts: list[str] = []

    ns_lower = (spec.namespace or "").lower()
    if any(p in ns_lower for p in _PRODUCTION_NS_PATTERNS):
        score += 40
        parts.append(f"production namespace '{spec.namespace}'")

    # Critical-component check: exact token match, not substring.
    # Substring matching (the previous behavior) false-positives on any
    # K8s name whose ReplicaSet hash happens to contain ``db`` / ``api``
    # / etc. — e.g. ``accounting-6fbdb464c7-qn2vr`` contains ``bdb``
    # which contains ``db`` and was being flagged as a database.
    # Splitting on the standard K8s name separators (``-``, ``_``, ``.``,
    # ``/``) and intersecting with the pattern set restricts hits to
    # whole segments — ``redis-master`` still matches ``redis``, but
    # random hex hashes no longer match anything.
    critical_hit: tuple[str, str] | None = None
    for name in spec.names:
        hits = _name_tokens(name) & _CRITICAL_NAME_PATTERNS
        if hits:
            # ``next(iter(hits))`` is non-deterministic for multi-hit
            # names (e.g. ``auth-db``), but the resulting score is the
            # same +30 either way and only the pattern label in the
            # explanation differs. Sort for stable test output.
            critical_hit = (name, sorted(hits)[0])
            break
    if critical_hit:
        score += 30
        parts.append(f"critical component '{critical_hit[0]}'")

    if spec.scope in _CLUSTER_SCOPED_SCOPES:
        score += 30
        parts.append(f"cluster-scoped ({spec.scope})")

    # Optional pre-computed deep K8s signal: (bonus_score, description)
    deep = context.get("topology_deep_signal")
    if isinstance(deep, tuple) and len(deep) == 2:
        bonus, desc = deep
        # Use ``type(bonus) is int`` not ``isinstance`` because Python
        # treats ``True``/``False`` as int subclasses — a caller passing
        # ``(True, ...)`` would otherwise leak +1 silently here.
        if type(bonus) is int and bonus > 0:
            score += bonus
            if desc:
                parts.append(str(desc))

    if not parts:
        parts.append("no critical markers")

    return DimensionScore(value=min(100, score), explanation=", ".join(parts))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_safety_score(
    spec: "FaultSpec",
    context: dict | None = None,
    weights: dict[str, float] | None = None,
) -> SafetyScore:
    """Compute multi-dimensional safety score.

    Args:
        spec: Validated FaultSpec instance.
        context: Optional dict carrying caller-provided signals:
            - ``conflict_uids``: list of UIDs from check_blade_conflicts
            - ``pipeline_attempt``: int retry counter
            - ``current_time``: datetime override (for deterministic tests)
            - ``topology_deep_signal``: pre-computed (bonus_int, desc_str)
              tuple from an async K8s query in the caller
        weights: per-dimension weights; defaults to DEFAULT_WEIGHTS.

    Returns:
        SafetyScore with per-dim DimensionScore + overall + level.
    """
    ctx = context or {}
    w = weights or DEFAULT_WEIGHTS

    br = _score_blast_radius(spec, ctx)
    fr = _score_frequency(ctx)
    tm = _score_time(spec, ctx)
    tp = _score_topology(spec, ctx)

    raw = (
        br.value * w.get("blast_radius", DEFAULT_WEIGHTS["blast_radius"])
        + fr.value * w.get("frequency", DEFAULT_WEIGHTS["frequency"])
        + tm.value * w.get("time", DEFAULT_WEIGHTS["time"])
        + tp.value * w.get("topology", DEFAULT_WEIGHTS["topology"])
    )
    # Cap to [0, 100] so misweighted overrides (e.g. caller passes a
    # partial weights dict that falls back to DEFAULT for missing keys
    # and over-shoots 1.0 in total) can never produce a nonsense level.
    overall = max(0, min(100, round(raw)))

    return SafetyScore(
        blast_radius=br,
        frequency=fr,
        time=tm,
        topology=tp,
        overall=overall,
        level=_score_to_level(overall),
        weights=dict(w),
    )


def maybe_escalate_status(
    current: str,
    overall: int,
    warning_thresh: int = 70,
    confirm_thresh: int = 90,
) -> str:
    """Upgrade safety_status based on overall score.

    Routing rules:
      - rejected / confirm_required: never changed (already terminal/strict)
      - overall >= confirm_thresh: → confirm_required
      - safe + overall >= warning_thresh: → warning
      - everything else: unchanged
    """
    if current in ("rejected", "confirm_required"):
        return current
    if overall >= confirm_thresh:
        return "confirm_required"
    if current == "safe" and overall >= warning_thresh:
        return "warning"
    return current
