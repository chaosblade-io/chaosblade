"""Deterministic verdict rules — the Layer 1.5 of the verify chain.

Position in the adjudication stack:

  Layer 1   (execution domain, program) — blade_status / kubectl collection,
            produces the experiment's execution status.
  Layer 1.5 (THIS module, program)      — deterministic rules that turn
            *measured numbers* into a verdict when the comparison is
            mechanical (RestartCount deltas, disk usage vs. the injected
            target). This is where the "same evidence, different verdicts"
            sampling variance of the LLM's last-mile judgment is eliminated.
  Layer 2   (ReAct loop, LLM)           — evidence collection + semantic
            adjudication (negative-evidence attribution, timing
            discrimination, log semantics). Remains the authority whenever
            Layer 1.5 returns ``unknown``.

Contract (frozen in this change's spec):

  * The verdict enum is CLOSED to ``{passed, unknown}``. There is no
    programmatic downgrade: absent numbers mean the LLM's territory, and
    a false negative is more expensive than variance.
  * ``passed`` requires BOTH the effect numbers AND a mechanism anchor
    (fault_handle committed / Layer 1 Success) — a bare numeric delta may
    be an unrelated OOMKill.
  * Rules are DECLARATIVE and hang off ``VerificationProfile``: a new
    fault family's rule rides the same extension path as every other
    family's, keyed on the ``(fault_target, fault_action)`` identity.

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
    ``rule_name``; lines like ``"RestartCount 8→11"`` or a disk-usage
    peak). The three ``*_subject`` fields are optional render subjects
    consumed by the finalize lift application: a rule may pin the exact
    checklist / Layer-2 / warning phrasing it wants, while a rule that
    leaves them empty (currently all of them) gets the pipeline's
    generic phrasing.
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
    """

    metric_observations: list[dict] = field(default_factory=list)
    fault_handle: dict | None = None
    layer1_passed: bool = False
    # FaultSpec-derived context (read via read_fault_spec at assembly time):
    spec_names: tuple[str, ...] = ()
    spec_params: dict = field(default_factory=dict)
    # Frozen case declaration (openspec mechanism-invariant-trust-boundary):
    # the ``expected`` map from the case's ``deterministic_verdict``
    # frontmatter, keyed by the SAME humanized metric name the labeled-
    # jsonpath extractor produces (e.g. ``{"Partition": "2"}``). Carries the
    # mechanism-invariant effect value N a field-state rule compares against.
    # It is ORTHOGONAL to ``spec_params`` — for #77-shaped declarative faults
    # ``spec_params`` is empty (N lives only in the frozen json-patch), so N
    # must arrive via this declaration, never re-derived. Empty for every
    # case without the declaration (rules then abstain, never guess N).
    declared_expected: dict = field(default_factory=dict)

    def anchoring_signal(self) -> str:
        """Name the mechanism-anchor signal that is present, if any.

        Returns ``""`` when no anchor is in play — evaluate must then
        refuse ``passed`` even if the numbers look compelling.
        """
        if self.fault_handle:
            return "fault_handle"
        if self.layer1_passed:
            return "layer1_success"
        return ""


# ---------------------------------------------------------------------------
# DeterministicRule — declarative, hung off a VerificationProfile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeterministicRule:
    """A per-fault-family deterministic adjudication.

    ``match`` is the ``(fault_target, fault_action)`` key — the same
    identity the profile registry uses, so a rule only fires for the
    family it was calibrated for.

    ``treats_replacement_as_effect``: set when OBJECT REPLACEMENT is
    itself part of this rule's effect criteria (process kill: a new
    container ID is one of its three legs). For such rules an LLM
    evidence line like "container ID changed" is the rule's OWN
    evidence shape, not a counter-proof — the finalize synthesis then
    skips the replacement-signal gate for this rule (the numeric
    cross-check contradiction gate always stays on). Rules that
    PRESUPPOSE object continuity (cpu / mem / fill: the measured
    numbers describe a stale object once it is replaced) leave this
    False and keep the full gate.
    """

    name: str
    match: tuple[str, str]
    evaluate: Callable[[RuleContext], DeterministicVerdict]
    treats_replacement_as_effect: bool = False


# ---------------------------------------------------------------------------
# Metric-timeline helpers — shared reading pattern for timeline rules.
# Semantics deliberately mirrors _build_truth_series (cross-check): sort by
# iteration (stable — same-iteration observations keep arrival order) and
# keep the FULL numeric series (the cross-check no longer reduces to
# earliest-vs-latest; see D15).
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


def _latest_value(observations: list[dict], metric_name: str) -> str:
    """As-of-latest snapshot value of ``metric_name``: the value carried by
    the highest-iteration observation that has it (arrival order breaks
    iteration ties). Blank/absent skipped; ``""`` when never observed.

    FIELD-STATE judgements — a single-moment comparison such as
    ``currentRevision != updateRevision`` — must read THIS, never
    :func:`_distinct_values`'s timeline union. A pre-injection baseline
    observation (``currentRevision == updateRevision``, before the new
    revision exists) pollutes the union with the baseline value, so the two
    sets overlap and a genuine stall is masked as "no split". Reproduced on
    real cluster data (drill-sts-partition-target, openspec mechanism-
    invariant-trust-boundary Phase 2): baseline ``7c99c5c4d`` + stalled
    ``898f79484`` → union ``{7c99c5c4d, 898f79484}`` overlaps the
    ``{7c99c5c4d}`` current-set → false ``unknown`` on a real partition
    stall. The as-of-latest read is immune: the latest snapshot carries the
    stalled pair. Contrast the process-kill rule, which WANTS the union
    (any new Container ID ever seen is the replacement signal).
    """
    best_iter: int | None = None
    best_val = ""
    for obs in observations or []:
        raw = (obs.get("metrics") or {}).get(metric_name)
        if raw is None or str(raw).strip() == "":
            continue
        it = int(obs.get("iteration", 0) or 0)
        if best_iter is None or it >= best_iter:
            best_iter = it
            best_val = str(raw).strip()
    return best_val


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
            "no mechanism anchor (fault_handle / Layer-1 Success both "
            "absent) — usage numbers alone are not causation"
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


# ---------------------------------------------------------------------------
# statefulset partition rule — ACTIVE (openspec mechanism-invariant-trust-
# boundary; pin LIFTED by the re-scoped calibration gate, tasks 5.1/5.4/5.5).
#
# Forensics (memory 66425653): the runtime LLM triple for #77 is
# fault_target=rollingupdate / fault_action=partition-stuck — both drift-prone
# (LLM decides them on the fly), which is exactly why the rule binds to the
# FROZEN author-declared key ``("statefulset", "partition-stall")`` carried by
# the case's ``deterministic_verdict`` frontmatter, NOT the LLM triple.
#
# WHY THE PIN WAS LIFTED (gate re-scope — see design.md "Calibration gate
# re-scope for fail-safe unique-field families"): the original C1 bar required
# ≥N MULTI-CASE closed-loop records. The partition family is a SINGLE case
# (#77) and the catalogue is one-case-per-fault-form BY DESIGN, so the
# multi-case bar is structurally unsatisfiable — the pin could never flip. For
# fail-safe unique-field families the bar is re-scoped to: (a) legs proven on
# MULTI-RUN real closed-loop data (#77: 5 runs; TestStsPartitionFullChain-
# Calibration binds VERBATIM real bytes to the verdict), AND (b) the rule is
# FAIL-SAFE — every unmet leg abstains to ``unknown``, so a drifted/absent
# metric can NEVER manufacture a false ``passed`` (drift only makes the rule
# stand down and defer to LLM Layer-2). CPU_FULLLOAD_RULE / MEM_LOAD_RULE stay
# pinned (_evaluate_uncalibrated) — they have NO written legs, so this re-scope
# does not touch them.
# ---------------------------------------------------------------------------


def _evaluate_sts_partition(ctx: RuleContext) -> DeterministicVerdict:
    """ACTIVE — delegates to the three-leg criteria (pin lifted; see the module
    comment above and design.md "Calibration gate re-scope").

    Fail-safe by construction: ``_sts_partition_legs`` returns ``passed`` only
    when every leg holds (declared Partition N reached + revision split +
    mechanism anchor); any unmet leg abstains to ``unknown`` and defers to the
    LLM. It never downgrades and never guesses.
    """
    return _sts_partition_legs(ctx)


def _sts_partition_legs(ctx: RuleContext) -> DeterministicVerdict:
    """Three-leg criteria for a STS partition-stall effect — wired live via
    :func:`_evaluate_sts_partition` (pin lifted; gate re-scoped).

    Legs (ALL required for ``passed``):
      1. Partition reaches the DECLARED expected value N, read from
         ``ctx.declared_expected["Partition"]`` — the frozen case
         declaration, NOT ``spec_params`` (empty for this family);
      2. Revision split proven: observed ``Current revision`` and
         ``Update revision`` values are both non-empty and disjoint (the
         rollout genuinely stalled mid-way rather than converging);
      3. A mechanism anchor is present (``ctx.anchoring_signal()``).

    Any missing leg → ``unknown`` (the LLM's territory). Never a downgrade.
    """
    anchor = ctx.anchoring_signal()
    if not anchor:
        return verdict_unknown(
            "no mechanism anchor (fault_handle / Layer-1 Success / "
            "post-check all absent) — field-state numbers alone are not "
            "causation"
        )

    declared = str((ctx.declared_expected or {}).get("Partition") or "").strip()
    m = re.match(r"^\s*(\d+(?:\.\d+)?)", declared)
    if not m:
        return verdict_unknown(
            "no declared expected Partition value N in the frozen "
            "deterministic_verdict — N is never hardcoded nor read from "
            "spec_params (empty for this family)"
        )
    n_expected = float(m.group(1))

    partition_series = _numeric_series(ctx.metric_observations, "Partition")
    if not partition_series:
        return verdict_unknown(
            "no Partition observations in the metric timeline"
        )
    if not any(v == n_expected for v in partition_series):
        return verdict_unknown(
            f"Partition series {partition_series} never reached declared "
            f"N={n_expected:g}"
        )

    # Leg 2 is a FIELD-STATE snapshot judgement, NOT a timeline-union one:
    # the rollout split holds iff the LATEST observed currentRevision differs
    # from the LATEST observed updateRevision. Reading the distinct-value
    # union across the whole timeline (the pre-fix shape) is wrong — a
    # pre-injection baseline read has currentRevision == updateRevision (the
    # new revision does not exist yet), so its value pollutes the
    # updateRevision union and the two sets overlap, masking a genuine stall
    # as "no split" (real-data finding, see :func:`_latest_value`).
    cur = _latest_value(ctx.metric_observations, "Current revision")
    upd = _latest_value(ctx.metric_observations, "Update revision")
    if not cur or not upd:
        return verdict_unknown(
            "no Current/Update revision observations to prove a rollout split"
        )
    if cur == upd:
        return verdict_unknown(
            f"latest snapshot Current revision == Update revision ({cur}) — "
            "no rollout split (converged, or the new revision never landed)"
        )

    return verdict_passed(
        "sts_partition_stall",
        [
            f"Partition reached declared N={n_expected:g}",
            f"Current revision {cur} ≠ Update revision {upd} (rollout split)",
            f"mechanism anchor: {anchor}",
        ],
    )


STS_PARTITION_RULE = DeterministicRule(
    name="sts_partition_stall",
    match=("statefulset", "partition-stall"),
    evaluate=_evaluate_sts_partition,
)


# ---------------------------------------------------------------------------
# deployment scale-down rule — ACTIVE (openspec mechanism-invariant-trust-
# boundary task 6.3; pin LIFTED by the re-scoped calibration gate, tasks
# 5.1/5.4/5.5 — same re-scope as STS_PARTITION_RULE).
#
# Second field-state family after #77 StatefulSet partition. The injection
# scales a workload's ``spec.replicas`` down to a declared expected value N;
# the field parks at N for the fault's lifetime; recovery patches it back up.
# The rule binds to the FROZEN author-declared key ``("deployment",
# "scale-down")`` carried by the case's ``deterministic_verdict`` frontmatter
# (same discipline as STS_PARTITION_RULE — NOT the drift-prone LLM triple).
# Grounding (memory 66425653 / 10a4db43): the #34 real run (inject-238fd5c3)
# has ``spec_params={}`` empty, so N=1 arrives only via ``declared_expected``,
# and the labeled-jsonpath read-backs produce the named metric ``Replicas``
# crossing the injection boundary (baseline 3 -> injected 1 -> verified 1).
# The family is a SINGLE case (#34), so the original multi-CASE bar was
# structurally unsatisfiable (one-case-per-fault-form catalogue). The gate is
# re-scoped for fail-safe unique-field families (see design.md "Calibration
# gate re-scope"): legs proven on MULTI-RUN real data (#34: 3 runs;
# TestDeploymentScaleDownFullChainCalibration binds VERBATIM real bytes to the
# verdict), and the rule is FAIL-SAFE (every unmet leg abstains to ``unknown``;
# the came-DOWN leg rejects an unrelated HPA/controller reconciliation the way
# a mechanism anchor does). Never a downgrade.
# ---------------------------------------------------------------------------


def _evaluate_deployment_scale_down(ctx: RuleContext) -> DeterministicVerdict:
    """ACTIVE — delegates to the three-leg criteria (pin lifted; see the module
    comment above and design.md "Calibration gate re-scope").

    Fail-safe by construction: ``_deployment_scale_down_legs`` returns
    ``passed`` only when every leg holds (mechanism anchor + declared Replicas
    N reached + came-DOWN from a higher baseline); any unmet leg abstains to
    ``unknown`` and defers to the LLM. It never downgrades and never guesses.
    """
    return _deployment_scale_down_legs(ctx)


def _deployment_scale_down_legs(ctx: RuleContext) -> DeterministicVerdict:
    """Three-leg criteria for a Deployment scale-down effect — wired live via
    :func:`_evaluate_deployment_scale_down` (pin lifted; gate re-scoped).

    Legs (ALL required for ``passed``):
      1. A mechanism anchor is present (``ctx.anchoring_signal()``) — a bare
         replica delta may be an unrelated HPA / controller reconciliation,
         not the injected fault;
      2. Replicas reaches the DECLARED expected value N, read from
         ``ctx.declared_expected["Replicas"]`` — the frozen case declaration,
         NOT ``spec_params`` (empty for this family);
      3. Came-DOWN direction proven: some timeline observation is strictly
         greater than N (the pre-injection baseline), so the series genuinely
         DESCENDED to N rather than having always sat at N (a no-op).

    Any missing leg → ``unknown`` (the LLM's territory). Never a downgrade.
    """
    anchor = ctx.anchoring_signal()
    if not anchor:
        return verdict_unknown(
            "no mechanism anchor (fault_handle / Layer-1 Success / "
            "post-check all absent) — a replica delta alone is not causation"
        )

    declared = str((ctx.declared_expected or {}).get("Replicas") or "").strip()
    m = re.match(r"^\s*(\d+(?:\.\d+)?)", declared)
    if not m:
        return verdict_unknown(
            "no declared expected Replicas value N in the frozen "
            "deterministic_verdict — N is never hardcoded nor read from "
            "spec_params (empty for this family)"
        )
    n_expected = float(m.group(1))

    replicas_series = _numeric_series(ctx.metric_observations, "Replicas")
    if not replicas_series:
        return verdict_unknown(
            "no Replicas observations in the metric timeline"
        )
    if not any(v == n_expected for v in replicas_series):
        return verdict_unknown(
            f"Replicas series {replicas_series} never reached declared "
            f"N={n_expected:g}"
        )

    # Leg 3 is a DIRECTION proof, not a snapshot: a scale-down must have
    # descended FROM a baseline strictly above N. A timeline that only ever
    # reads N (e.g. [1.0, 1.0]) means the fault never took effect (or the
    # target already sat at N) — abstain rather than credit a no-op as a
    # passed scale-down.
    if not any(v > n_expected for v in replicas_series):
        return verdict_unknown(
            f"Replicas series {replicas_series} never exceeded declared "
            f"N={n_expected:g} — no came-down direction proof (the target may "
            "have always sat at N; a scale-down must descend from a higher "
            "baseline)"
        )

    return verdict_passed(
        "deployment_scale_down",
        [
            f"Replicas reached declared N={n_expected:g}",
            f"came down from a higher baseline (series {replicas_series})",
            f"mechanism anchor: {anchor}",
        ],
    )


DEPLOYMENT_SCALE_DOWN_RULE = DeterministicRule(
    name="deployment_scale_down",
    match=("deployment", "scale-down"),
    evaluate=_evaluate_deployment_scale_down,
)


__all__ = [
    "DeterministicRule",
    "DeterministicVerdict",
    "RuleContext",
    "verdict_passed",
    "verdict_unknown",
    "DISK_FILL_RULE",
    "PROCESS_KILL_RULE",
    "CPU_FULLLOAD_RULE",
    "MEM_LOAD_RULE",
    "STS_PARTITION_RULE",
    "_sts_partition_legs",
    "DEPLOYMENT_SCALE_DOWN_RULE",
    "_deployment_scale_down_legs",
]
