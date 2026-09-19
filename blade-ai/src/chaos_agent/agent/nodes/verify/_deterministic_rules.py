"""Deterministic verdict rules — the Layer 1.5 of the verify chain.

Position in the adjudication stack:

  Layer 1   (execution domain, program) — blade_status / kubectl collection,
            produces the experiment's execution status.
  Layer 1.5 (THIS module, program)      — deterministic rules that turn
            *measured numbers* into a verdict when the comparison is
            mechanical (RestartCount deltas, disk usage vs. the injected
            target, measured I/O throughput). This is where the
            "same evidence, different verdicts" sampling variance of the
            LLM's last-mile judgment is eliminated.
  Layer 2   (ReAct loop, LLM)           — evidence collection + semantic
            adjudication (negative-evidence attribution, timing
            discrimination, log semantics). Remains the authority whenever
            Layer 1.5 returns ``unknown``.

Contract (frozen in this change's spec):

  * The verdict enum is CLOSED to ``{passed, unknown}``. There is no
    programmatic downgrade: absent numbers mean the LLM's territory, and
    a false negative is more expensive than variance.
  * ``passed`` requires BOTH the effect numbers AND a mechanism anchor
    (fault_handle committed / Layer 1 Success / a PostCheckSpec result) —
    a bare numeric delta may be an unrelated OOMKill.
  * Rules are DECLARATIVE and hang off ``VerificationProfile`` (same layer
    as ``PostCheckSpec``): a new fault family's rule rides the same
    extension path as its post-injection check.

The application half — how a verdict is arbitrated against the LLM's own
(the five-quadrant truth table) — lives in the finalize pipeline
(``_verifier_finalize``), not here. Rules only ADJUDICATE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

# ---------------------------------------------------------------------------
# Verdict — enum closed to {passed, unknown}
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeterministicVerdict:
    """Outcome of a deterministic rule's ``evaluate``.

    Construct via the module factories :func:`verdict_passed` /
    :func:`verdict_unknown`. ``__post_init__`` rejects any other ``kind``,
    so no code path can mint a programmatic downgrade — that boundary is
    enforced by the type itself, not by convention.

    ``evidence_lines`` are AUDIT numeric lines (rule name lives in
    ``rule_name``; lines like ``"RestartCount 8→11"`` or measured
    throughput). The three ``*_subject`` fields are optional render
    subjects consumed by the finalize lift application — they exist so
    the disk_burn migration can keep its legacy phrasing byte-identical;
    rules that leave them empty get the pipeline's generic phrasing.
    """

    kind: str  # "passed" | "unknown" — nothing else survives construction
    rule_name: str = ""
    evidence_lines: tuple[str, ...] = ()
    reason: str = ""
    # Render subjects for the finalize lift application (optional):
    checklist_subject: str = ""
    layer2_subject: str = ""
    warning_subject: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("passed", "unknown"):
            raise ValueError(
                f"DeterministicVerdict kind must be 'passed' or 'unknown', "
                f"got {self.kind!r} — programmatic downgrade is outside "
                f"this layer's contract (see design decision 4)."
            )

    @property
    def is_passed(self) -> bool:
        return self.kind == "passed"


def verdict_passed(
    rule_name: str, evidence_lines: list[str] | tuple[str, ...],
    *,
    checklist_subject: str = "",
    layer2_subject: str = "",
    warning_subject: str = "",
) -> DeterministicVerdict:
    """The rule's numeric criteria AND a mechanism anchor both held."""
    return DeterministicVerdict(
        kind="passed",
        rule_name=rule_name,
        evidence_lines=tuple(evidence_lines),
        checklist_subject=checklist_subject,
        layer2_subject=layer2_subject,
        warning_subject=warning_subject,
    )


def verdict_unknown(reason: str) -> DeterministicVerdict:
    """Anything short of the passed criteria — the LLM's territory."""
    return DeterministicVerdict(kind="unknown", reason=reason)


# ---------------------------------------------------------------------------
# RuleContext — the inputs an evaluate may consult
# ---------------------------------------------------------------------------


@dataclass
class RuleContext:
    """Everything a deterministic rule may read. Assembled by the finalize
    pipeline from state; rules never touch state directly.

    Mechanism-anchor signals (design decision 5 — ``passed`` requires one):
      ``fault_handle``   the injection was committed (an experiment handle
                         exists on state)
      ``layer1_passed``  Layer 1 (execution domain) reported Success
      ``post_check``     a PostCheckSpec measured result is present
    """

    metric_observations: list[dict] = field(default_factory=list)
    fault_handle: dict | None = None
    layer1_passed: bool = False
    post_check: dict | None = None
    # FaultSpec-derived context (read via read_fault_spec at assembly time):
    spec_names: tuple[str, ...] = ()
    spec_params: dict = field(default_factory=dict)

    def anchoring_signal(self) -> str:
        """Name the mechanism-anchor signal that is present, if any.

        Returns ``""`` when no anchor is in play — evaluate must then
        refuse ``passed`` even if the numbers look compelling.
        """
        if self.fault_handle:
            return "fault_handle"
        if self.layer1_passed:
            return "layer1_success"
        if self.post_check:
            return "post_check"
        return ""


# ---------------------------------------------------------------------------
# DeterministicRule — declarative, hung off a VerificationProfile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeterministicRule:
    """A per-fault-family deterministic adjudication.

    ``match`` is the ``(fault_target, fault_action)`` key — the same
    identity the profile registry and PostCheckSpec dispatch use, so a
    rule only fires for the family it was calibrated for.

    ``post_check_key`` names the state key of the PostCheckSpec result
    the rule consumes as its mechanism anchor / effect measurement
    (e.g. ``"disk_burn_post_check"``). Empty when the rule's inputs are
    purely the metric timeline.

    ``treats_replacement_as_effect``: set when OBJECT REPLACEMENT is
    itself part of this rule's effect criteria (process kill: a new
    container ID is one of its three legs). For such rules an LLM
    evidence line like "container ID changed" is the rule's OWN
    evidence shape, not a counter-proof — the finalize synthesis then
    skips the replacement-signal gate for this rule (the numeric
    cross-check contradiction gate always stays on). Rules that
    PRESUPPOSE object continuity (cpu / mem / burn / fill: the measured
    numbers describe a stale object once it is replaced) leave this
    False and keep the full gate.
    """

    name: str
    match: tuple[str, str]
    evaluate: Callable[[RuleContext], DeterministicVerdict]
    post_check_key: str = ""
    treats_replacement_as_effect: bool = False


# ---------------------------------------------------------------------------
# disk_burn rule — migrated from _enforce_disk_burn_facts (judgement half).
# The application half (checklist/layer2 lift + [OVERRIDE] evidence +
# warnings) keeps living in the finalize pipeline; migration must be
# behaviour-identical (design decision 8).
# ---------------------------------------------------------------------------


def _evaluate_disk_burn(ctx: RuleContext) -> DeterministicVerdict:
    """Measured I/O ACTIVE from the burn post-check → passed.

    The post-check itself is both the effect measurement (per-partition
    write throughput sampled twice) and the mechanism anchor (a
    PostCheckSpec result in play), so no further anchor is required.

    The three render subjects reproduce the legacy
    ``_enforce_disk_burn_facts`` phrasing byte-identically (migration
    contract: only the evidence PREFIX was ever allowed to differ).
    """
    post = ctx.post_check or {}
    if not post.get("burn_io_detected"):
        return verdict_unknown(
            "disk_burn_post_check absent or measured no active I/O"
        )
    parts_str = ", ".join(
        f"{p['name']}: ~{p['write_throughput_mb_s']} MB/s"
        for p in post.get("active_partitions", [])[:3]
    ) or "measured"
    return verdict_passed(
        "disk_burn_io_active",
        [
            "Programmatic I/O check confirmed ACTIVE "
            f"(write throughput: {parts_str}).",
        ],
        checklist_subject=(
            "Programmatic I/O check confirmed ACTIVE "
            f"(write throughput: {parts_str})."
        ),
        layer2_subject=(
            "Programmatic I/O check: disk burn ACTIVE "
            f"(write throughput: {parts_str})."
        ),
        warning_subject=(
            "disk_burn_post_check confirmed I/O ACTIVE "
            f"(write throughput: {parts_str})"
        ),
    )


DISK_BURN_RULE = DeterministicRule(
    name="disk_burn_io_active",
    match=("disk", "burn"),
    evaluate=_evaluate_disk_burn,
    post_check_key="disk_burn_post_check",
)


# ---------------------------------------------------------------------------
# Metric-timeline helpers — shared reading pattern for timeline rules.
# Semantics deliberately mirrors _build_truth_deltas (cross-check): sort by
# iteration (stable — same-iteration observations keep arrival order),
# earliest = baseline, latest = post.
# ---------------------------------------------------------------------------


def _numeric_series(observations: list[dict], metric_name: str) -> list[float]:
    """Numeric values of ``metric_name`` across the timeline, iteration-
    ordered. Non-numeric values are skipped (Container IDs, booleans)."""
    series: list[tuple[int, float]] = []
    for obs in observations or []:
        raw = (obs.get("metrics") or {}).get(metric_name)
        # Zero is a legal observation (RestartCount baseline IS 0 before a
        # kill) — only absent / blank values are skipped. A bare truthiness
        # check would drop every zero baseline and either starve the series
        # (len<2 → unknown on the most common kill shape) or shift the
        # baseline to the first non-zero reading (re-audit finding 5).
        if raw is None or str(raw).strip() == "":
            continue
        m = re.match(r"^\s*(\d+(?:\.\d+)?)", str(raw))
        if not m:
            continue
        series.append((int(obs.get("iteration", 0) or 0), float(m.group(1))))
    series.sort(key=lambda t: t[0])
    return [value for _, value in series]


def _distinct_values(observations: list[dict], metric_name: str) -> list[str]:
    """Distinct non-empty values of a (non-numeric) metric across the
    timeline, first-seen order — e.g. the container IDs ever observed."""
    seen: list[str] = []
    for obs in observations or []:
        raw = (obs.get("metrics") or {}).get(metric_name)
        if not raw:
            continue
        value = str(raw).strip()
        if value and value not in seen:
            seen.append(value)
    return seen


# ---------------------------------------------------------------------------
# process kill rule — criteria empirically double-verified by #25 / #25-R
# (RESTARTS 8→10 / 15→17, containerID replaced, exit 137).
# ---------------------------------------------------------------------------


# Minimum RestartCount growth across the timeline. Δ2 — one kill event
# restarts the container once; a single restart can also be an unrelated
# transient, two within the observation window is the empirically
# calibrated kill signature (#25: Δ+2 within 90s of arming).
_PROCESS_KILL_MIN_DELTA = 2


def _evaluate_process_kill(ctx: RuleContext) -> DeterministicVerdict:
    """RestartCount Δ≥2 AND container replacement AND an anchor → passed.

    The numeric criterion alone is NOT sufficient: an unrelated OOMKill
    produces the same RestartCount shape (#25-R iter-5 attribution nuance).
    The container-ID replacement narrows it to a kill (SIGKILL'd containers
    come back with a new runtime ID), and the mechanism anchor pins the
    injection itself. Any missing leg → unknown (the LLM's territory).
    """
    anchor = ctx.anchoring_signal()
    if not anchor:
        return verdict_unknown(
            "no mechanism anchor (fault_handle / Layer-1 Success / "
            "post-check all absent) — numeric delta may be an unrelated kill"
        )

    series = _numeric_series(ctx.metric_observations, "RestartCount")
    if len(series) < 2:
        return verdict_unknown(
            "RestartCount timeline has fewer than 2 numeric observations"
        )
    # Monotonicity guard: a single pod's RestartCount never decreases —
    # a dip means the FLAT timeline interleaves observations from more
    # than one pod (multi-replica kills share the label selector), and
    # without per-pod identity the delta is not attributable.
    for _prev, _cur in zip(series, series[1:]):
        if _cur < _prev:
            return verdict_unknown(
                f"RestartCount timeline not monotonic "
                f"({_prev:g}→{_cur:g}) — observations may interleave "
                f"multiple pods; delta not attributable"
            )
    base, post = series[0], series[-1]
    delta = post - base
    if delta < _PROCESS_KILL_MIN_DELTA:
        return verdict_unknown(
            f"RestartCount delta {delta:+g} (earliest {base:g} → latest "
            f"{post:g}) below the +{_PROCESS_KILL_MIN_DELTA} kill signature"
        )

    container_ids = _distinct_values(ctx.metric_observations, "Container ID")
    if len(container_ids) < 2:
        return verdict_unknown(
            "Container ID timeline shows no replacement "
            "(fewer than 2 distinct IDs observed)"
        )

    return verdict_passed(
        "process_kill_restarts",
        [
            f"RestartCount {base:g} → {post:g} (Δ{delta:+g}), "
            f"container ID replaced ({len(container_ids)} distinct observed)",
            f"mechanism anchor: {anchor}",
        ],
    )


PROCESS_KILL_RULE = DeterministicRule(
    name="process_kill_restarts",
    match=("process", "kill"),
    evaluate=_evaluate_process_kill,
    # Container replacement is one of this rule's OWN criteria — an
    # honest green LLM citing "container ID changed" is describing the
    # effect, not refuting the measurement (the gate would otherwise
    # misfire on nearly every kill run; re-audit finding 1).
    treats_replacement_as_effect=True,
)


# ---------------------------------------------------------------------------
# disk fill rule — usage reaches the INJECTED target. The threshold is
# derived from the fault's own params (percent / size), never hardcoded:
# the rule has no opinion about what "full" means — the injection does.
# Empirically shaped by #9 (pod-scope: overlay usage climbs with the fill
# file) and #29 (node-scope: df lands 11%→86% on a 85% fallocate target).
# ---------------------------------------------------------------------------

# overlay: pod-scope fills (df run inside the pod); nodefs: node-scope
# fills (df on /host). Whichever key the timeline carries is the
# measured filesystem.
_FILL_USAGE_KEYS = ("Disk usage (overlay)", "Disk usage (nodefs)")

# ChaosBlade size flag ('10g', '512m', bare bytes; tolerate the 'Ki/Mi/Gi'
# IEC suffix spelling some tooling emits).
_SIZE_SUFFIXES = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?\s*$", re.IGNORECASE)
# df usage values carry "86% (used/total)" — the paren pair is the raw
# byte pair (df -k; df -h prints human units which this deliberately
# does not guess at).
_USED_BYTES_RE = re.compile(r"\(\s*(\d+)\s*/\s*(\d+)\s*\)")


def _parse_percent(raw) -> float | None:
    """'85' / '85%' / 85 → 85.0; anything else → None."""
    if raw is None:
        return None
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*%?\s*$", str(raw))
    return float(m.group(1)) if m else None


def _parse_size_bytes(raw) -> float | None:
    """ChaosBlade size flag ('10g', '512m', bare bytes) → bytes."""
    m = _SIZE_RE.match(str(raw or ""))
    if not m:
        return None
    return float(m.group(1)) * _SIZE_SUFFIXES.get(m.group(2).lower(), 1)


def _used_bytes_series(observations: list[dict], metric_name: str) -> list[float]:
    """Used-bytes values parsed from df usage strings ("86% (used/total)")."""
    series: list[tuple[int, float]] = []
    for obs in observations or []:
        raw = (obs.get("metrics") or {}).get(metric_name)
        if not raw:
            continue
        m = _USED_BYTES_RE.search(str(raw))
        if not m:
            continue
        series.append((int(obs.get("iteration", 0) or 0), float(m.group(1))))
    series.sort(key=lambda t: t[0])
    return [value for _, value in series]


def _evaluate_disk_fill(ctx: RuleContext) -> DeterministicVerdict:
    """Measured usage reaches the injection's own target → passed.

    Two derivable thresholds (either one may carry the judgement):
      ``percent``  post-injection usage ≥ the injected target percent
                   (absolute judgement — a fill is a static fault, the
                   target is where the injection parked it);
      ``size``     used-bytes growth ≥ the requested size.

    Multi-replica flat-timeline approximation: observations may come
    from several pods sharing a label selector, so "reached" reads as
    max(pct-series) and max(used)−min(used) — any source hitting the
    target counts (the per-pod max contract in its flat-timeline
    form; refine when observations carry per-pod identity).
    """
    anchor = ctx.anchoring_signal()
    if not anchor:
        return verdict_unknown(
            "no mechanism anchor (fault_handle / Layer-1 Success / "
            "post-check all absent) — usage numbers alone are not causation"
        )

    pct_target = _parse_percent(ctx.spec_params.get("percent"))
    size_target = _parse_size_bytes(ctx.spec_params.get("size"))
    # A degenerate target (percent=0 / size=0) is not a threshold — the
    # comparison would be vacuous (any observation "reaches" 0) and hand
    # out a free pass for an injection that parked nothing. Read as not
    # derivable (re-audit finding 10).
    if pct_target is not None and pct_target <= 0:
        pct_target = None
    if size_target is not None and size_target <= 0:
        size_target = None
    if pct_target is None and size_target is None:
        return verdict_unknown(
            "spec params carry no derivable target (percent/size absent "
            "or degenerate ≤ 0) — fill thresholds are never hardcoded"
        )

    lines: list[str] = []
    hit = False
    saw_any_observation = False
    for key in _FILL_USAGE_KEYS:
        if pct_target is not None:
            pct_series = _numeric_series(ctx.metric_observations, key)
            if pct_series:
                saw_any_observation = True
                peak = max(pct_series)
                if peak >= pct_target:
                    hit = True
                    lines.append(
                        f"{key} peaked at {peak:g}% ≥ injected target "
                        f"{pct_target:g}%"
                    )
                else:
                    lines.append(
                        f"{key} peaked at {peak:g}% below injected target "
                        f"{pct_target:g}%"
                    )
        if size_target is not None:
            used_series = _used_bytes_series(ctx.metric_observations, key)
            if len(used_series) >= 2:
                saw_any_observation = True
                growth = max(used_series) - min(used_series)
                if growth >= size_target:
                    hit = True
                    lines.append(
                        f"{key} used bytes grew by {growth:g} ≥ requested "
                        f"size {size_target:g}"
                    )
                else:
                    lines.append(
                        f"{key} used bytes grew by {growth:g} below "
                        f"requested size {size_target:g}"
                    )

    if not saw_any_observation:
        return verdict_unknown(
            "no Disk usage (overlay/nodefs) observations in the metric "
            "timeline"
        )
    if not hit:
        return verdict_unknown("; ".join(lines))
    return verdict_passed(
        "disk_fill_usage_target",
        lines + [f"mechanism anchor: {anchor}"],
    )


DISK_FILL_RULE = DeterministicRule(
    name="disk_fill_usage_target",
    match=("disk", "fill"),
    evaluate=_evaluate_disk_fill,
    post_check_key="disk_fill_post_check",
)


# ---------------------------------------------------------------------------
# cpu / mem rules — DECLARED but NOT CALIBRATED (tasks 5.1 adjudication).
#
# The measurement record holds only extreme shapes for these families:
# #23 cpu (top 56m → 15950m/100% — baseline≈0 → ceiling) and #27 node
# mem (3204Mi/5% → 48413Mi/80% — landed exactly on the injected
# percent). No mid-band sample (e.g. 30%→60%) exists to calibrate a
# relative-baseline threshold's boundary, and pod-scope mem has zero
# samples. Per the tasks' own escape hatch the rules are declared —
# the extension point is real and typed — but evaluate is pinned to
# unknown until calibration data exists: enabling a guessed threshold
# would trade sampling variance for systematic false verdicts, the
# exact failure this layer exists to prevent.
# ---------------------------------------------------------------------------

_UNCALIBRATED_REASON = (
    "rule declared but not calibrated — the measurement record holds "
    "only extreme-shape samples for this family (no mid-band baseline "
    "to anchor a relative threshold); per tasks 5.1 the rule stays "
    "disabled until calibration data exists"
)


def _evaluate_uncalibrated(ctx: RuleContext) -> DeterministicVerdict:
    return verdict_unknown(_UNCALIBRATED_REASON)


CPU_FULLLOAD_RULE = DeterministicRule(
    name="cpu_fullload_relative_baseline",
    match=("cpu", "fullload"),
    evaluate=_evaluate_uncalibrated,
)

MEM_LOAD_RULE = DeterministicRule(
    name="mem_load_relative_baseline",
    match=("mem", "load"),
    evaluate=_evaluate_uncalibrated,
)


__all__ = [
    "DeterministicRule",
    "DeterministicVerdict",
    "RuleContext",
    "verdict_passed",
    "verdict_unknown",
    "DISK_BURN_RULE",
    "DISK_FILL_RULE",
    "PROCESS_KILL_RULE",
    "CPU_FULLLOAD_RULE",
    "MEM_LOAD_RULE",
]
