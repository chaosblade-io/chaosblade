"""Layer 2 parse domain for verifier: checklist/JSON/result parsing.

Extracted from verifier.py — contains the parsing logic for LLM verification
output: checklist parsing, JSON parsing, and the main _parse_verification_result
function that combines both into a structured result dict.
"""

import json
import logging
import re

from chaos_agent.agent.nodes.verify._metric_extractor import canonicalize_number
from chaos_agent.agent.nodes.verify._verifier_shared import (
    _parse_status_keyword,
    has_checklist,
    parse_checklist_items,
)
from chaos_agent.agent.result.verdict import (
    CHECKLIST_BENIGN_STATUSES,
    CHECKLIST_STATUS_VALUES,
    Checklist,
    ChecklistItem,
    ChecklistItemStatus,
    InjectVerdict,
    INJECT_VERDICT_VALUES,
    Layer1Result,
    LAYER1_STATUS_VALUES,
    LAYER2_PARSE_KEYWORDS,
    Layer2Result,
    LAYER2_STATUS_VALUES,
    StructuredWarning,
    VerificationResult,
    WarningCode,
)
from chaos_agent.utils.skill_case_section import find_section, has_section

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Evidence cross-check — structured metric claims (W-77R-5 root fix)
# ---------------------------------------------------------------------------
#
# After the LLM submits its verdict, this helper compares the numeric
# baseline→post claims it STRUCTURED via submit_verification's
# ``metric_claims`` against the metric timeline accumulated on
# state.metric_observations (populated by PreReasoningHook from each
# ToolMessage's extracted_metrics).
#
# Failure mode this catches: the LLM claims "RestartCount 7 → 8" but the
# timeline shows RestartCount stayed at 7 across every iteration — without
# this cross-check the LLM could declare ``verified`` on a hallucinated
# change.
#
# Binding discipline (the W-77R-5 fix): WHICH metric a claim refers to is
# declared by the LLM as a structured key (``metric_claims[].metric``), NOT
# reverse-inferred by the program from free text. The old path parsed
# ``layer2.details`` with a delta regex + substring metric-name guess
# (``_pick_metric_by_context``); because K8s metric families nest as
# substrings (``"replicas"`` ⊂ ``"updatedreplicas"``), a CORRECT
# ``updatedReplicas 3→1`` claim was mis-bound to the unmoving ``Replicas``
# and falsely downgraded verified→partial (task inject-0fc9a9a1). Semantic
# binding is the LLM's job (it reads the difference); numeric comparison is
# the program's job (exact key lookup). The program never parses natural
# language to recover a binding.
#
# Fail-open (danger asymmetry): a false downgrade of a real-injection
# verdict is worse than a missed hallucination — it is an authoritative
# override that silently rewrites ``level``. So the cross-check downgrades
# ONLY on a deterministic contradiction (declared key exactly hits truth,
# truth shows zero change, claim declares non-zero). No structured claims,
# or a declared key absent from the timeline, ABSTAIN (warn at most).


def _parse_numeric(value: str) -> float | None:
    """Pull a float out of a metric-value string, dropping units.

    Recognises percent, millicores, Mi/Gi/Ki. Returns ``None`` when no
    leading number can be extracted (e.g. ``"True"``, ``"OOMKilled"``).
    """
    if not value:
        return None
    m = re.match(r"^\s*(\d+(?:\.\d+)?)", value)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _collect_evidence_text(result: dict) -> str:
    """Gather every free-text field where the LLM could cite numbers.

    Pulls from layer2.details + checklist[].evidence + warnings.
    Concatenated with newline separators so context-lookback regex
    doesn't span across unrelated fields.
    """
    parts: list[str] = []
    l2_details = (result.get("layer2") or {}).get("details", "")
    if l2_details:
        parts.append(l2_details)
    for item in (result.get("checklist") or {}).get("items", []) or []:
        ev = item.get("evidence", "")
        if ev:
            parts.append(ev)
    return "\n".join(parts)


def _build_truth_series(
    observations: list[dict],
) -> dict[str, list[float]]:
    """For each metric seen across observations, its full numeric series in
    iteration order (same-iteration ties keep arrival order).

    Replaces the old first-vs-last ``(base, post)`` tuple: a zero
    first-vs-last delta is NOT proof the metric never changed — it's an
    artifact of self-recovery (last obs reverted to baseline, so a real
    mid-window peak gets erased by comparing only the endpoints; D15 Mode A)
    or a single-phase snapshot (baseline read dropped by the extractor, so
    the series is constant at the post value; Mode B). The contradiction
    check needs the WHOLE series to tell "pinned at one value" from "moved
    and came back". Non-numeric values (revision hashes, ``"OOMKilled"``,
    ``"True"``) are skipped — those have their own categorical detection
    elsewhere; a metric needs >=2 numeric observations to be comparable.
    """
    by_metric: dict[str, list[tuple[int, str]]] = {}
    for obs in observations or []:
        iteration = int(obs.get("iteration", 0))
        for name, value in (obs.get("metrics") or {}).items():
            by_metric.setdefault(name, []).append((iteration, value))

    series: dict[str, list[float]] = {}
    for name, values in by_metric.items():
        if len(values) < 2:
            continue
        values.sort(key=lambda t: t[0])
        nums = [
            n for n in (_parse_numeric(raw) for _, raw in values)
            if n is not None
        ]
        if len(nums) < 2:
            continue
        series[name] = nums
    return series


def _unknown_key_abstain_note(metric: str, base, post) -> str:
    """Honest coverage-gap warning for a claim whose metric key is absent from
    the timeline AND whose value could not be corroborated by fingerprint.

    This is the B-minimal text, kept byte-stable: absence is symmetric between
    an extractor coverage gap and a genuine hallucination, so it NEVER
    downgrades (D12 fail-open danger asymmetry) and directs the auditor to
    out-of-band confirmation. ``test_unknown_key_is_warning_not_contradiction``
    pins the ``out-of-band`` invariant.
    """
    return (
        f"LLM cited metric '{metric}' (baseline={base}, post={post}) "
        f"whose key is absent from the observation timeline, so it "
        f"cannot be cross-checked. The timeline only carries what "
        f"extract_metrics parses from a FIXED vocabulary of "
        f"structured reads (df / describe pod / get -o json / "
        f"labeled jsonpath / top / /proc/stat / diskstats / du / "
        f"logs / get-table); free-form exec output (ping, "
        f"/proc/net/*, tc -s qdisc, ss, iptables -L, ...) is "
        f"structurally not parsed. Absence is therefore ambiguous "
        f"between an extractor coverage gap and a genuine "
        f"hallucination, which the framework cannot distinguish "
        f"without a declared evidence anchor. NOT downgrading "
        f"(absence is not a contradiction); this evidence class "
        f"requires out-of-band confirmation."
    )


def _norm_source(s: str) -> str:
    """Normalize a source/command fragment for substring anchoring (D17 T3):
    case-fold + collapse internal whitespace, so an LLM-echoed ``"tc  -s
    qdisc"`` matches the stored command ``"kubectl exec … -- tc -s qdisc show
    dev eth0"``. Deliberately a soft substring relation, NOT an exact match —
    the LLM cannot reliably reproduce a full kubectl invocation, and an
    unmappable/ambiguous source abstains (fail-open) rather than guessing."""
    return " ".join((s or "").lower().split())


# A declared metric-claim value is a number the LLM may annotate with a unit
# ("1014.755 ms", "0%", "6.433 ms") or leave bare (1012.525). Both are the SAME
# numeric claim, so the cross-check must key on the value's semantics, not on
# whether the LLM happened to append a unit — without this tolerance the whole
# D17 layer is inert for unit-suffixed claims (float("1014.755 ms") raises), so
# the same drill is cross-checked in one run and silently skipped in the next on
# pure output-formatting variance. Anchored at the START so a measurement must be
# presented as a leading quantity; a genuinely non-numeric base/post ("OOMKilled",
# "sub-ms healthy expectation") has no leading digit → None → skipped, exactly the
# pre-existing behaviour for a non-numeric claim.
_CLAIM_NUM_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)")


def _parse_claim_number(value) -> float | None:
    """Numeric value of a metric-claim base/post, tolerating a trailing unit.

    Returns the float for a bare number (int / float / numeric string) or a
    unit-suffixed string (``"1014.755 ms"`` → 1014.755); ``None`` for a genuinely
    non-numeric claim (no leading digit, NaN / ±inf, wrong type) so the caller
    skips it. ``bool`` is rejected explicitly (``isinstance(True, int)`` is True).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        m = _CLAIM_NUM_RE.match(value)
        if not m:
            return None
        try:
            f = float(m.group(1))
        except ValueError:
            return None
    else:
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / ±inf
        return None
    return f


def _classify_unknown_claim(
    claim: dict,
    metric: str,
    base,
    post,
    fingerprints: dict | None,
) -> tuple[str, str]:
    """D17 — resolve a claim whose metric key is ABSENT from the structured
    timeline using the format-agnostic value fingerprint, breaking the
    hallucination-vs-coverage-gap symmetry the key space cannot (D12).

    Returns ``(classification, note)``:
      - "corroborated" (T2): the claimed post value appears in some harvested
        observation output AS A STANDALONE NUMBER (the ``numeric_fields`` view)
        → the number is REAL, its key was simply never parsed into the timeline
        (extractor coverage gap, NOT a hallucination). Non-downgrading positive
        note. This is the netem-class fix: ``tc -s qdisc`` / ping values the
        fixed-vocabulary extractor returns ``{}`` for are nonetheless provable
        from the raw output.
      - "abstain" (T2-weak): the value's digits occur in an observation ONLY
        inside a longer non-numeric token — a hex experiment UID (``932`` of
        ``932eaa6bd493bf54``) or an ISO-date component whose sign is really a
        separator (``-10`` of ``2026-10-27``) — or the fingerprint predates
        field-grading. Such a collision proves too little to certify reality,
        and it simultaneously defeats T3's absence proof, so the only sound
        ruling is an honest coverage-gap warning. Never downgrades.
      - "fabricated" (T3): the claim explicitly declares ``kind=raw`` and a
        ``source`` that anchors to EXACTLY ONE complete harvested observation,
        and the value is in NO observation output → a verbatim-copied number
        that exists nowhere is a deterministic fabrication. MAY downgrade
        (routed to the contradictions channel by ``_check_metric_claims``).
      - "abstain": no fingerprint, a derived/unmarked claim, an unmapped or
        ambiguous source, an incomplete harvest, or a value not found without a
        raw+source anchor → honest coverage-gap warning, never downgrades (D12).

    MONOTONICITY — the invariant that makes T2's narrowing safe. T2-weak
    intercepts exactly the values present in the over-inclusive ``tokens`` union
    but absent from every ``numeric_fields``, and T3 still keys its absence proof
    on ``tokens``. T3 therefore fires on precisely the inputs it fired on before
    field-grading existed: this design can turn a "corroborated" into an
    "abstain", never into a "fabricated". Narrowing the presence certificate
    cannot manufacture a false accusation.

    Fail-open by construction: T2 only ever SUPPRESSES a scary warning when the
    value is positively present as a number; T3 only fires on a strict per-source
    anchor to a COMPLETE harvest (every ambiguous case abstains), so it cannot
    reproduce D12's false-downgrade — the value axis is positively exhaustive
    where the key axis was merely absent.
    """
    # Unit-tolerant parse (fix b): a claim post of "1014.755 ms" carries the
    # SAME numeric value as a bare 1014.755, so it must canonicalize identically
    # rather than be silently dropped (float("1014.755 ms") raises). Without this
    # the whole D17 fingerprint comparison is inert for unit-suffixed claims and
    # the identical drill is cross-checked in one run, skipped in the next.
    post_canon = canonicalize_number(_parse_claim_number(post))
    if post_canon is None or not fingerprints:
        return "abstain", _unknown_key_abstain_note(metric, base, post)
    fps = [fp for fp in fingerprints.values() if isinstance(fp, dict)]
    field_hits = sorted({
        (fp.get("tool_name") or "?") for fp in fps
        if post_canon in (fp.get("numeric_fields") or [])
    })
    if field_hits:
        return "corroborated", (
            f"LLM cited metric '{metric}' (baseline={base}, post={post}) whose "
            f"key is not in the structured timeline (free-form exec output the "
            f"fixed-vocabulary extractor does not parse), but its post value "
            f"{post} WAS found as a standalone numeric field in harvested "
            f"observation output ({', '.join(field_hits)}). Value-presence "
            f"corroborates the claim (D17 T2) — an extractor coverage gap, NOT "
            f"a hallucination. NOT downgrading."
        )
    token_hits = sorted({
        (fp.get("tool_name") or "?") for fp in fps
        if post_canon in (fp.get("tokens") or [])
    })
    if token_hits:
        # Present only as an embedded digit run. Certifying this as REAL is what
        # let an incidental UID/date collision vouch for a hallucinated value;
        # and because the digits ARE in the over-inclusive corpus, T3 cannot
        # prove absence either. Both certificates fail → abstain (fail-open).
        return "abstain", (
            f"LLM cited metric '{metric}' (baseline={base}, post={post}) whose "
            f"key is not in the structured timeline. The value's digits DO occur "
            f"in harvested observation output ({', '.join(token_hits)}), but only "
            f"inside a longer non-numeric token — an experiment UID or timestamp "
            f"fragment — never as a standalone number. That collision is too weak "
            f"to certify the value as REAL, and it also defeats the absence proof, "
            f"so neither certificate applies (D17 T2-weak). Coverage-gap warning, "
            f"NOT downgrading."
        )
    # T3 — fabrication. The value is in NO harvested observation: it is absent
    # from every ``numeric_fields`` AND from every ``tokens`` (both unions were
    # checked above), i.e. absent from the over-inclusive corpus the absence
    # proof requires. Downgrade ONLY under strict per-source anchoring;
    # every ambiguous case abstains (fail-open, D12's lesson). The claim must
    # explicitly declare kind=raw AND a source mapping to EXACTLY ONE complete
    # harvest — a derived value, a missing/unmapped/ambiguous source, or an
    # incomplete (token-capped) harvest all abstain. D17 earns this downgrade
    # where D12 could not: the value axis is positively exhaustive (the full
    # raw output was harvested), so absence here is proof, not a coverage gap.
    kind = str(claim.get("kind") or "").strip().lower()
    source = str(claim.get("source") or "").strip()
    if kind != "raw" or not source:
        return "abstain", _unknown_key_abstain_note(metric, base, post)
    src_norm = _norm_source(source)
    anchored = [
        fp for fp in fingerprints.values()
        if isinstance(fp, dict) and fp.get("complete") and src_norm
        and src_norm in _norm_source(fp.get("command") or "")
    ]
    if len(anchored) != 1:
        # Unmapped (0) or ambiguous (2+) source → cannot prove WHICH observation
        # the LLM read → abstain rather than risk a false downgrade.
        return "abstain", _unknown_key_abstain_note(metric, base, post)
    if post_canon in (anchored[0].get("tokens") or []):
        # Load-bearing monotonicity guard, not merely defensive: ``anchored[0]``
        # is a subset of the ``tokens`` union already ruled out above, so this
        # cannot fire today. Re-checking here is what keeps a narrowing of the T2
        # PRESENCE certificate (as field-grading just did) from ever widening the
        # T3 DOWNGRADE set — should the token-union gate above ever be made
        # stricter, this falls back to abstain rather than manufacturing a false
        # accusation.
        return "abstain", _unknown_key_abstain_note(metric, base, post)
    return "fabricated", (
        f"LLM claims {metric} {base}→{post} as a RAW value read from "
        f"'{source}', but that observation was harvested IN FULL and its output "
        f"contains no such value {post} — nor does ANY other cluster observation "
        f"(D17 T3). A verbatim-copied number that exists in no observation is a "
        f"deterministic fabrication, not an extractor coverage gap."
    )


def _check_metric_claims(
    claims: list,
    truth: dict[str, list[float]],
    fingerprints: dict | None = None,
) -> tuple[list[str], list[str]]:
    """Compare LLM-declared structured metric claims against ``truth``.

    Each claim is ``{"metric": key, "baseline": n, "post": n}`` — the
    binding (which metric a number belongs to) is DECLARED by the LLM, so
    the program does an exact key lookup, never a substring guess.

    Returns ``(contradictions, notes)``:
      - contradictions: deterministic fabrication signals that MAY downgrade
        the verdict. Two kinds: (a) the observed series is CONSTANT (metric
        provably never moved) at a value that is NOT the claimed post — the
        claimed transition to ``post`` never materialized for an exactly-matched
        key (a zero first-vs-last delta alone is NOT enough; see
        ``_build_truth_series`` / D15: self-recovery and single-phase snapshots
        both fake it). (b) D17 T3: an ABSENT-key claim explicitly declared
        ``kind=raw`` whose value is provably in NO harvested observation
        (strict per-source anchor; see ``_classify_unknown_claim``).
      - notes: NON-downgrading auditor notes appended to ``warnings``. All for
        a key absent from the timeline (D17): a value-presence CORROBORATION
        (T2 — the post value was found AS A STANDALONE NUMBER in a harvested
        observation output, so it is a coverage gap not a hallucination) and an
        honest ABSTAIN coverage-gap warning. The abstain arm covers both the
        uncorroborated value and T2-weak, where the value's digits occur only
        inside a longer non-numeric token (a UID / timestamp fragment) — too
        weak to certify reality, and it defeats the absence proof besides.
        Neither ever downgrades — absence cannot be distinguished from an
        extractor coverage gap, so per the fail-open danger asymmetry it must
        not override the LLM's verdict.

    ``fingerprints`` (D17) is the ``state.evidence_fingerprints`` map
    ``{tool_call_id: {tokens, numeric_fields, complete, tool_name, command}}``
    harvested from raw observation output — ``tokens`` is the over-inclusive
    corpus the absence proof (T3) keys on, ``numeric_fields`` the delimited
    subset that alone may certify presence (T2); see
    ``harvest_evidence_fingerprint``. ``None`` (legacy callers / no
    observations) makes every absent-key claim abstain, i.e. byte-identical to
    pre-D17 behaviour, and a fingerprint persisted before field-grading (no
    ``numeric_fields`` key) likewise abstains rather than certifying.
    """
    contradictions: list[str] = []
    notes: list[str] = []
    for claim in claims or []:
        if not isinstance(claim, dict):
            continue
        metric = claim.get("metric")
        if not isinstance(metric, str) or not metric:
            continue
        base, post = claim.get("baseline"), claim.get("post")
        # Unit-tolerant parse (fix b): tolerate a trailing unit on either value
        # ("1014.755 ms", "0%") so the cross-check keys on the number, not on
        # whether the LLM happened to annotate it. A genuinely non-numeric
        # base/post ("OOMKilled", bool, NaN) yields None → skipped, exactly the
        # pre-existing behaviour for a claim with nothing numeric to disprove.
        base_num = _parse_claim_number(base)
        declared_post = _parse_claim_number(post)
        if base_num is None or declared_post is None:
            continue  # non-numeric claim — nothing to cross-check
        declared_delta = declared_post - base_num
        if declared_delta == 0:
            continue  # LLM claims no change — nothing to disprove

        if metric not in truth:
            # D17 — the key is absent from the structured timeline. Resolve via
            # the format-agnostic value fingerprint (see _classify_unknown_claim):
            # T2 corroborates (value present AS A STANDALONE NUMBER → positive
            # note), T3 flags a provable fabrication (kind=raw anchored to
            # exactly one complete harvest, value present nowhere in the
            # over-inclusive token corpus → contradiction), and every other case
            # abstains with an honest coverage-gap warning — including T2-weak,
            # where the value's digits occur only inside a longer non-numeric
            # token. Only the T3 "fabricated" classification routes to
            # contradictions; T2 and both abstain arms append to ``notes`` and
            # never downgrade. With no kind/source declared (or
            # fingerprints=None) the branch can only ever append to ``notes``.
            classification, note = _classify_unknown_claim(
                claim, metric, base, post, fingerprints,
            )
            if classification == "fabricated":
                contradictions.append(note)
            else:  # "corroborated" (T2) or "abstain" (incl. T2-weak) — no downgrade
                notes.append(note)
            continue

        series = truth[metric]
        # Deterministic contradiction (D15): the observed series is CONSTANT
        # across every observation AND that constant is NOT the claimed post
        # — the metric provably never moved, so the claimed transition to
        # ``post`` never happened. Anchoring on the constant value (not the
        # first-vs-last delta) is what makes this sound for self-recovering
        # timelines (Mode A: series [0,0,2,2,0] is not constant) and
        # single-phase snapshots pinned at the post value (Mode B: [2,2,2]
        # claim 0→2 — constant == post → the endpoint is corroborated, so
        # abstain). Partial mismatches (non-constant series) stay silent —
        # too noisy to flag.
        if (
            len(series) >= 2
            and all(v == series[0] for v in series)
            and series[0] != declared_post
        ):
            contradictions.append(
                f"LLM claims {metric} {base}→{post} (Δ={declared_delta:+g}), "
                f"but observation timeline shows no change "
                f"(pinned at {series[0]:g} across {len(series)} "
                f"observation(s); claimed post {declared_post:g} never "
                f"appeared)"
            )
    return contradictions, notes


def _apply_contradiction_downgrade(
    result: dict,
    contradictions: list[str],
) -> None:
    """Append contradiction warnings and downgrade verdict in-place.

    Same downgrade direction as
    ``_detect_checklist_conclusion_inconsistency``: ``passed`` →
    ``partial`` (objective signal overrides subjective judgement).
    Verdicts already worse than passed are left alone.
    """
    warnings = result.setdefault("warnings", [])
    for c in contradictions:
        warnings.append(c)
    if (result.get("layer2") or {}).get("status") == "passed":
        result["layer2"]["status"] = "partial"
    if result.get("level") == "verified":
        result["level"] = "partial"
        warnings.append(
            "Verdict downgraded verified→partial due to evidence/timeline "
            "contradiction(s) above. Objective metric measurements override "
            "the LLM's stated conclusion."
        )


def cross_check_evidence(
    result: dict,
    observations: list[dict] | None,
    fingerprints: dict | None = None,
) -> dict:
    """Detect LLM structured metric claims that contradict the timeline.

    Pipeline (structured binding — see module header):
      1. Read the LLM's declared ``metric_claims`` (from submit_verification
         args / JSON mode). No claims → abstain (fail-open, no mutation).
      2. Build per-metric truth series (full iteration-ordered numeric
         observations) from the timeline.
      3. Exact-key compare each claim; collect deterministic contradictions
         (may downgrade) and non-downgrading notes (D17 value-presence
         corroboration + honest coverage-gap abstain).
      4. Append notes; if contradictions, downgrade verdict.

    ``fingerprints`` (D17) is ``state.evidence_fingerprints`` — the
    format-agnostic value harvest that lets an absent-key claim be corroborated
    by value-presence (T2) instead of only warned about. When BOTH the key-space
    truth and the value-space fingerprints are empty there is nothing to
    cross-check against, so it early-exits with no mutation (byte-identical to
    pre-D17 behaviour for legacy callers passing only observations).

    Returns ``result`` (mutated). Safe with empty / missing observations,
    fingerprints, or claims.
    """
    claims = result.get("metric_claims") or []
    if not claims:
        return result  # fail-open: no structured binding → abstain
    truth = _build_truth_series(observations)
    if not truth and not fingerprints:
        # Nothing in either the key space (timeline) or the value space
        # (fingerprints) to cross-check against → abstain, no mutation. A
        # pure free-form-exec drill (no structured read ever parsed) lands
        # here on the key space but is still corroborable via fingerprints.
        return result
    contradictions, notes = _check_metric_claims(claims, truth, fingerprints)
    if notes:
        result.setdefault("warnings", []).extend(notes)
    if contradictions:
        _apply_contradiction_downgrade(result, contradictions)
    return result


# ---------------------------------------------------------------------------
# Refactor 6: 提取 LLM 验证结果解析
# 原因: _parse_verification_result 用字符串 split 解析 LLM 输出，脆弱
#        但这是独立的解析逻辑，不应与 verifier 流程混在一起
# 做法: 保持原样但放在顶部，逻辑清晰分离
# ---------------------------------------------------------------------------

def _determine_level(l1_status: str, l2_status: str) -> str:
    """Determine overall verification level from Layer 1 + Layer 2 status."""
    # recovered_before_observation: fault was injected but effects dissipated
    # before observation. Not "verified" (can't confirm effects) but not
    # "unverified" (injection did happen). Maps to "partial".
    if l2_status == "recovered_before_observation":
        return "partial"
    if l1_status == "passed" and l2_status == "passed":
        return "verified"
    elif l1_status == "skipped" and l2_status == "passed":
        return "verified"
    elif l1_status == "passed" and l2_status in ("partial", "skipped"):
        return "partial"
    elif l1_status == "skipped" and l2_status == "partial":
        return "partial"
    elif l1_status == "passed" and l2_status == "failed":
        return "unverified"
    elif l1_status == "passed":
        return "unverified"
    return "unverified"


# _CONTRADICTION_INDICATORS removed (Phase 2c / D14): the program no longer
# reverse-infers "contradiction" semantics from free-text layer2.details via a
# static keyword table. verdict overrides consume structured status enums only
# (structured-evidence-binding spec, third consumption face).


# _has_negative_prefix and _parse_status_keyword moved to _verifier_shared.py


# ---------------------------------------------------------------------------
# Checklist parsing: detect skipped verification steps for auto-downgrade
# ---------------------------------------------------------------------------

# The parseable item-status vocabulary derives from the legislation enum
# (B76 round-14): sorted longest-first so a future member that prefixes an
# existing one cannot shadow it in the alternation. Teaching (submit tool
# docs + prompt rules) parses from the same enum — parse can never drift
# from teach.
_ITEM_STATUS_ALT = "|".join(
    sorted((m.value for m in ChecklistItemStatus), key=len, reverse=True)
)

_CHECKLIST_PATTERNS = [
    # Primary: Step N: <status> [— evidence]
    # Captures step number, status, and optional evidence text after
    # separator. A residual [CORE]/[IMPACT] tag from older prompts is
    # tolerated via a non-capturing optional group.
    # Group layout: (1)=step, (2)=status, (3)=evidence.
    re.compile(
        rf"(?:step|check)\s*(\d+)\s*[:.)]\s*(?:\[?(?:core|impact)\]?\s*)?"
        rf"\[?({_ITEM_STATUS_ALT})\]?"
        rf"(?:\s*[—–-]\s*(.+?))?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    # Explicit skip marker from prompt instruction: [SKIPPED] Step N
    # Negative lookbehind prevents matching [skipped] inside "2. [skipped]"
    # or "Step 1: [skipped]" — those are handled by Pattern 0 and Pattern 2.
    re.compile(r"(?<!\d[.:)]\s)\[SKIPPED\]\s*(?:step\s*)?(\d+)?", re.IGNORECASE),
    # Bare numbered list: 1. <status> [— evidence]
    re.compile(
        rf"^\s*(\d+)\s*[.:)]\s*(?:\[?(?:core|impact)\]?\s*)?"
        rf"\[?({_ITEM_STATUS_ALT})\]?"
        rf"(?:\s*[—–-]\s*(.+?))?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
]


def _parse_checklist_items(text: str) -> list[dict]:
    """Parse Verification Checklist items from LLM output."""
    return parse_checklist_items(
        text,
        section_marker="VERIFICATION_CHECKLIST:",
        end_marker="VERIFICATION_RESULT:",
        patterns=_CHECKLIST_PATTERNS,
        capture_evidence=True,
    )


def _has_checklist(text: str) -> bool:
    """Check if text contains a Verification Checklist section or items."""
    return has_checklist(text, section_marker="VERIFICATION_CHECKLIST:", patterns=_CHECKLIST_PATTERNS)


def _detect_checklist_conclusion_inconsistency(
    checklist_items: list[dict],
    l2_status: str,
) -> tuple[str | None, bool]:
    """Detect a STRUCTURAL contradiction between checklist statuses and Layer2.

    Purely a closed-set ``ChecklistItemStatus`` enum comparison — the program
    never reads ``evidence`` free text and never reverse-infers absence /
    benign / expected-negative semantics from it (D14, structured-evidence-
    binding spec: the third verdict-override consumption face).

    The single downgrade trigger: Layer2 concluded ``passed`` while at least one
    checklist item is marked ``failed``. That is the LLM contradicting itself at
    the structured-status level (a hard enum inconsistency), so the program
    downgrades to ``partial``.

    Benign non-passed statuses (``partial``, ``recovered_before_observation``,
    ``expected``, ``skipped``, ``not_applicable``) do NOT contradict a ``passed``
    conclusion and MUST NOT trigger a downgrade — whether such a step represents
    fault-effect absence / a benign / an expected-negative outcome is the LLM's
    judgement, expressed via its status choice, not the program's to re-derive
    from text.

    Returns:
        (warning_message | None, should_auto_downgrade: bool)
    """
    if l2_status != "passed" or not checklist_items:
        return None, False

    failed_items = [
        item for item in checklist_items
        if isinstance(item, dict) and item.get("status") == "failed"
    ]
    if not failed_items:
        return None, False

    steps = [i.get("step", "?") for i in failed_items]
    warning = (
        f"Checklist-conclusion inconsistency: Step(s) {steps} marked 'failed' "
        f"but Layer2 concluded 'passed' — a structural contradiction in the "
        f"LLM's own status declarations. Auto-downgrading to 'partial'."
    )
    return warning, True


def _apply_all_dissipated_override(
    checklist_items: list, level: str
) -> tuple[str, str | None]:
    """Structured all-dissipated safety net (D14, enum-only — no free text).

    When EVERY checklist item is marked ``recovered_before_observation`` the
    fault effect left no observable trace by verification time, so any
    verified-family level is dishonest — force ``unverified`` (honest
    ignorance: nothing was observed). A MIXED checklist (some items genuinely
    observed) only records a warning; partial dissipation is the LLM's overall
    judgement, not a program override.

    SINGLE-SOURCED across both verdict paths (``_verification_from_submit_args``
    and ``_parse_verification_result``) so the override cannot drift between
    them. MUST be applied AFTER the overall level is derived and AFTER the
    primary-evidence / layer2-sync downgrades, so "nothing observable" is the
    final word (an earlier placement is dead: the level is still the initial
    'unverified' before overall-derivation, and overall-derivation overwrites
    it afterward — the pre-refactor inline copy in the text path was doubly
    dead for exactly this reason). Pure ``ChecklistItemStatus`` enum counting;
    no ``evidence`` free text is read.

    Returns:
        (level, warning_message | None)
    """
    if not checklist_items:
        return level, None
    recovered_count = sum(
        1 for item in checklist_items
        if isinstance(item, dict)
        and item.get("status") == ChecklistItemStatus.RECOVERED_BEFORE_OBSERVATION.value
    )
    if recovered_count == 0:
        return level, None
    warning = (
        f"Verification Checklist has {recovered_count} step(s) marked "
        f"'recovered_before_observation'. The fault effect had already "
        f"dissipated before verification could observe it."
    )
    if recovered_count == len(checklist_items) and level != "unverified":
        level = "unverified"
    return level, warning


def _count_verification_steps_in_skill_case(content: str) -> int:
    """Count verification steps from skill case's '注入验证' section.

    Counts top-level numbered items in the 注入验证 section, falling back
    to bullet sub-items if no numbered steps are found.
    """
    section = find_section(content, "注入验证")
    if section is None:
        return 0

    # Count top-level numbered steps (1., 2., 3., etc.)
    step_matches = re.findall(r'^\s*(\d+)\.\s', section, re.MULTILINE)
    if step_matches:
        return len(set(step_matches))

    # Fallback: count bullet sub-items
    return len(re.findall(r'^\s*[-*]\s', section, re.MULTILINE))


def _has_injection_verification_section(content: str) -> bool:
    """Check if skill case content contains an 注入验证 section heading.

    Purely structural — returns True even if the section has only prose
    paragraphs without numbered or bullet steps. A mere in-text mention of
    the word (「见注入验证」) does NOT count as the section existing.
    """
    return has_section(content, "注入验证")


_CANDIDATE_SPLIT_RE = re.compile(r'^---\s*Candidate\s+\d+\s*:.*?---\s*$', re.MULTILINE)


def _split_candidates(content: str) -> list[str]:
    """Split multi-candidate skill_case into individual candidate texts.

    Returns a list of candidate bodies (1-indexed in the original text,
    0-indexed in the returned list).  Single-candidate content returns
    ``[content]`` unchanged.
    """
    markers = list(_CANDIDATE_SPLIT_RE.finditer(content))
    if len(markers) < 2:
        return [content]

    candidates: list[str] = []
    for i, m in enumerate(markers):
        body_start = m.end()
        body_end = markers[i + 1].start() if i + 1 < len(markers) else len(content)
        candidates.append(content[body_start:body_end].strip())
    return candidates


def _extract_verification_step_descriptions(content: str) -> list[str]:
    """Extract verification step descriptions from skill case's 注入验证 section.

    Returns a list of description strings in order, e.g.:
    ["查看 Pod CPU 使用率监控，确认持续高于阈值", "进入容器查看 CPU 占用进程", ...]
    Returns empty list if no 注入验证 section or steps can't be parsed.

    Section located via ``skill_case_section`` (heading-anchored). A bare
    substring anchor would be hijacked by the first in-text mention of
    「注入验证」 (e.g. 「2. 注入验证探针的查询目标…」), which happened on
    12 of 105 skill files before the fix.
    """
    section = find_section(content, "注入验证")
    if section is None:
        return []

    # Extract numbered step descriptions: "N. description text..."
    numbered = re.findall(r'^\s*\d+\.\s+(.+)', section, re.MULTILINE)
    if numbered:
        cleaned = []
        for desc in numbered:
            first_line = desc.split('\n')[0].strip()
            # Remove trailing colon from step titles
            first_line = re.sub(r'[：:]$', '', first_line).strip()
            cleaned.append(first_line)
        return cleaned

    # Fallback: extract bullet items
    bullets = re.findall(r'^\s*[-*]\s+(.+)', section, re.MULTILINE)
    return [b.split('\n')[0].strip().rstrip('：:') for b in bullets]


def _validate_step_number_coverage(
    skill_case_content: str,
    checklist_items: list[dict],
) -> tuple[list[int], list[int]]:
    """Validate step number coverage between skill case and LLM checklist.

    Returns (missing_steps, deviated_steps):
      - missing_steps: step numbers present in skill case but absent from checklist
      - deviated_steps: step numbers present in both but where LLM used a different
        method than the skill case specified (detected by deviation keyword in evidence)

    This provides step-number-level granularity beyond the simple count comparison
    in _count_verification_steps_in_skill_case.
    """
    expected_descs = _extract_verification_step_descriptions(skill_case_content)
    if not expected_descs:
        return [], []

    expected_numbers = set(range(1, len(expected_descs) + 1))
    executed_numbers = {item["step"] for item in checklist_items if isinstance(item.get("step"), int)}

    missing = sorted(expected_numbers - executed_numbers)

    # Detect deviations: steps that exist in both but have a "deviation:" note in evidence
    deviated = []
    for item in checklist_items:
        if not isinstance(item.get("step"), int):
            continue
        evidence = item.get("evidence", "")
        if "deviation:" in evidence.lower() or "(deviation:" in evidence.lower():
            deviated.append(item["step"])

    return missing, deviated


def _try_parse_json(content: str) -> dict | None:
    """Try to parse LLM output as JSON and map to verification dict.

    Returns a verification dict on success, None on failure.
    Used as primary parser when the final iteration runs in JSON mode
    (response_format={"type": "json_object"}).
    """
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    # Required-field presence and closed-set validity are separate gates:
    # a missing layer2/overall is a schema violation (reject → fall through
    # to the text parser), while an explicit in-set word — "unknown"
    # included, matching what the JSON-mode reminder teaches — is accepted.
    if "layer2" not in data or "overall" not in data:
        return None
    l2 = data["layer2"]
    overall = data["overall"]
    # Closed sets derive from the legislation enums (B76 round-14):
    # out-of-set claims reject this JSON (fall through to the text
    # parser) instead of flowing verbatim into the verification dict.
    if l2 not in LAYER2_STATUS_VALUES:
        return None
    if overall not in INJECT_VERDICT_VALUES:
        return None

    result = {
        "level": overall,
        "layer1": {"status": data.get("layer1", "unknown"), "details": ""},
        "layer2": {"status": l2, "details": data.get("layer2_details", "")},
        "warnings": data.get("warnings", []),
    }
    # Structured metric claims (W-77R-5) — same passthrough as the
    # submit-args path, so JSON-mode verdicts carry the binding too.
    _claims = data.get("metric_claims")
    if isinstance(_claims, list):
        result["metric_claims"] = [c for c in _claims if isinstance(c, dict)]

    checklist = data.get("verification_checklist")
    if isinstance(checklist, list) and checklist:
        result["checklist"] = {
            "items": checklist,
            "skipped_count": sum(1 for c in checklist if c.get("status") == "skipped"),
            # Fail-closed counting (B76 round-14 F3): closed-set-outside
            # words are not pass claims.
            "non_passed_count": sum(
                1 for c in checklist
                if c.get("status") not in CHECKLIST_BENIGN_STATUSES
            ),
            "total_count": len(checklist),
            "total_executed": len(checklist),
        }
        _outside = sorted({
            c.get("status") for c in checklist
            if isinstance(c, dict) and c.get("status") not in CHECKLIST_STATUS_VALUES
        })
        if _outside:
            result["warnings"].append(
                f"Checklist item statuses outside the closed vocabulary: {_outside}."
            )
        # Checklist-conclusion inconsistency check (same logic as text parser):
        # pure structured status-enum comparison, no free-text evidence (D14).
        if l2 == "passed":
            inconsistency_warning, should_downgrade = _detect_checklist_conclusion_inconsistency(
                checklist, l2,
            )
            if inconsistency_warning:
                result["warnings"].append(inconsistency_warning)
                if should_downgrade:
                    result["layer2"]["status"] = "partial"

    # If L2 was programmatically downgraded to "partial", override level too.
    # Mirrors the level-sync logic in _parse_verification_result.
    if result["layer2"]["status"] == "partial" and result["level"] in ("verified", "unverified"):
        result["level"] = "partial"

    return result


def _parse_verification_result(text: str) -> dict:
    """Parse the LLM's verification summary into a structured result."""
    result = {
        "level": "unverified",
        "layer1": {"status": "unknown", "details": ""},
        "layer2": {"status": "unknown", "details": ""},
        "warnings": [],
        "baseline_used": None,
    }

    text_lower = text.lower()

    # Parse Layer 1
    if "layer1" in text_lower:
        l1_part = text_lower.split("layer1", 1)[1].split("layer2", 1)[0]
        result["layer1"]["status"] = _parse_status_keyword(l1_part)

    # Parse Layer 2
    if "layer2" in text_lower:
        l2_first_line = text_lower.split("layer2", 1)[1].split("\n")[0]
        l2_status = _parse_status_keyword(l2_first_line)
        result["layer2"]["status"] = l2_status
        if l2_status == "skipped":
            result["warnings"].append("Layer 2 skipped: LLM could not design a verification plan")
        # Extract details after the status keyword (same ordered keyword
        # legislation the status parser above consumes — the two hand copies
        # had drifted into different orders).
        for status_kw in LAYER2_PARSE_KEYWORDS:
            kw_idx = l2_first_line.find(status_kw)
            if kw_idx >= 0:
                after = l2_first_line[kw_idx + len(status_kw):].strip()
                details = after.lstrip("-: ")
                if details:
                    result["layer2"]["details"] = details
                break

    # --- Checklist parsing ---
    checklist_items = _parse_checklist_items(text)
    skipped_count = sum(1 for item in checklist_items if item["status"] == "skipped")
    non_passed_count = sum(
        1 for item in checklist_items
        if item["status"] not in CHECKLIST_BENIGN_STATUSES
    )

    if checklist_items:
        result["checklist"] = {
            "items": checklist_items,
            "skipped_count": skipped_count,
            "non_passed_count": non_passed_count,
            "total_count": len(checklist_items),
            "total_executed": len(checklist_items),
        }

    if result["layer2"]["status"] == "passed" and skipped_count > 0:
        result["warnings"].append(
            f"Verification Checklist has {skipped_count} skipped step(s). "
            f"These checks were not executable. Executed checks all passed."
        )
        # No longer downgrade — skipped means genuinely unexecutable
        # (checks that were executed but not met should be marked 'failed', not 'skipped')

    # recovered_before_observation structured override moved to a single-sourced
    # helper applied AFTER overall-level derivation (see the call below the
    # PrimaryEvidenceObserved constraint). The former inline copy here was dead:
    # result["level"] is still the initial 'unverified' at this point, and the
    # overall-level determination further down overwrites it regardless.

    if result["layer2"]["status"] in ("passed", "partial") and not _has_checklist(text):
        result["warnings"].append(
            "No Verification Checklist detected in LLM output. "
            "Verification completeness cannot be confirmed."
        )

    # Contradiction detection removed (Phase 2c / D14): the program no longer
    # reverse-infers "the LLM said failed but details look affirmative" from
    # free-text layer2.details via _CONTRADICTION_INDICATORS / _ABSENCE_PHRASES.
    # The LLM's structured layer2 status is authoritative; absence / benign /
    # expected-negative semantics are declared via ChecklistItemStatus, not
    # re-derived from text. See structured-evidence-binding spec.

    # Checklist-conclusion inconsistency (structured status-enum comparison only,
    # no free-text evidence — D14): a 'failed' item contradicting a 'passed'
    # Layer2 conclusion is a hard structural inconsistency → downgrade partial.
    if checklist_items:
        inconsistency_warning, should_downgrade = _detect_checklist_conclusion_inconsistency(
            checklist_items, result["layer2"]["status"],
        )
        if inconsistency_warning:
            result["warnings"].append(inconsistency_warning)
            if should_downgrade:
                result["layer2"]["status"] = "partial"

    # Determine overall level
    if "overall:" in text_lower:
        overall = text_lower.split("overall:", 1)[1].split("\n")[0].strip()
        if "verified" in overall and "partial" not in overall and "unverified" not in overall:
            result["level"] = "verified"
        elif "partial" in overall:
            result["level"] = "partial"
        elif "unverified" in overall:
            result["level"] = "unverified"
    else:
        result["level"] = _determine_level(
            result["layer1"]["status"], result["layer2"]["status"]
        )

    # If L2 was programmatically downgraded to "partial" (e.g., a structural
    # checklist inconsistency detected by _detect_checklist_conclusion_inconsistency),
    # override the level to "partial" as well.
    # This mirrors recover_verifier.py's level-sync logic.
    if result["layer2"]["status"] == "partial" and result["level"] in ("verified", "unverified"):
        result["level"] = "partial"

    # Parse PrimaryEvidenceObserved: LLM must explicitly declare whether
    # primary (not side-effect) evidence of the fault was directly observed.
    # Hard constraint: verified verdict requires primary evidence.
    primary_observed = None
    if "primaryevidenceobserved:" in text_lower:
        pv = text_lower.split("primaryevidenceobserved:", 1)[1].split("\n")[0].strip()
        primary_observed = "true" in pv

    if result["level"] == "verified" and primary_observed is False:
        result["level"] = "partial"
        result["warnings"].append(
            "Verdict 'verified' is incompatible with PrimaryEvidenceObserved=false. "
            "Cannot confirm fault effectiveness without direct primary evidence. "
            "Downgraded to 'partial'."
        )

    # All-dissipated structured override (single-sourced with the submit-args
    # path): applied last so "every criterion recovered before observation ⇒
    # nothing observable ⇒ unverified" is the final word.
    result["level"], _rbo_warning = _apply_all_dissipated_override(
        checklist_items, result["level"]
    )
    if _rbo_warning:
        result["warnings"].append(_rbo_warning)

    # Warnings: only match explicit "Warnings:" line, not any mention of the word
    for line in text_lower.split("\n"):
        line_stripped = line.strip()
        if line_stripped.startswith("warnings:") and "none" not in line_stripped:
            result["warnings"].append("See verification details for warnings")
            break

    # Parse BaselineUsed field
    if "baselineused:" in text_lower:
        bu = text_lower.split("baselineused:", 1)[1].split("\n")[0].strip()
        result["baseline_used"] = "true" in bu

    if result["layer2"]["status"] == "skipped":
        result["warnings"].append(
            "Layer 2 (fault-specific) verification was skipped. "
            "Only Layer 1 (programmatic) verification was performed."
        )
    elif result["layer2"]["status"] == "unknown":
        result["warnings"].append(
            "Layer 2 (fault-specific) verification result is unknown. "
            "The LLM did not produce a clear verification conclusion. "
            "Only Layer 1 (programmatic) verification was confirmed."
        )

    return result


# ---------------------------------------------------------------------------
# E3 — dict → VerificationResult conversion
# ---------------------------------------------------------------------------

_WARNING_TEXT_TO_CODE: list[tuple[str, WarningCode]] = [
    ("Layer 2 skipped", WarningCode.LAYER2_SKIPPED),
    ("Layer 2 (fault-specific) verification was skipped", WarningCode.LAYER2_SKIPPED),
    ("Layer 2 (fault-specific) verification result is unknown", WarningCode.LAYER2_SKIPPED),
    ("experiment expired", WarningCode.EXPERIMENT_EXPIRED),
    ("Fault experiment expired", WarningCode.EXPERIMENT_EXPIRED),
    ("skipped step", WarningCode.CHECKLIST_HAS_SKIPPED),
    ("recovered_before_observation", WarningCode.CHECKLIST_RECOVERED_BEFORE_OBS),
    ("No Verification Checklist detected", WarningCode.NO_CHECKLIST_DETECTED),
    ("Contradiction:", WarningCode.CONTRADICTION_OVERRIDE),
    ("Checklist-conclusion inconsistency", WarningCode.CHECKLIST_CONCLUSION_INCONSISTENCY),
    ("PrimaryEvidenceObserved=false", WarningCode.PRIMARY_EVIDENCE_NOT_OBSERVED),
    ("coverage", WarningCode.COVERAGE_INCOMPLETE),
    ("See verification details", WarningCode.SEE_VERIFICATION_DETAILS),
    ("baseline available", WarningCode.BASELINE_AVAILABLE_NOT_USED),
]


def _classify_warning(text: str) -> WarningCode:
    """Map a freeform warning string to a WarningCode."""
    text_lower = text.lower()
    for fragment, code in _WARNING_TEXT_TO_CODE:
        if fragment.lower() in text_lower:
            return code
    return WarningCode.SEE_VERIFICATION_DETAILS


def dict_to_verification_result(raw: dict) -> VerificationResult:
    """Convert a raw _parse_verification_result dict to a VerificationResult model.

    Keeps _parse_verification_result unchanged (battle-tested parsing) and
    adds a structured conversion layer at the boundary.

    One failure philosophy throughout (B76 round-14 F4 — this function
    previously mixed a bare raise, a silent drop, a fail-open default and
    a correct clamp): coerce to the enum's fail-safe member where one
    exists (layer1/layer2/level → unknown/unverified), drop-and-log
    where none does (a checklist item's status has no "unknown"
    member), and always recompute counts from the surviving items so
    items/total can never decouple.
    """
    # Layer 1 — out-of-set claims coerce to UNKNOWN (fail-safe).
    l1_raw = raw.get("layer1") or {}
    layer1 = Layer1Result(
        status=l1_raw.get("status", "unknown")
        if l1_raw.get("status", "unknown") in LAYER1_STATUS_VALUES else "unknown",
        details=l1_raw.get("details", ""),
        raw_output=l1_raw.get("raw_output", ""),
        resource_statuses=l1_raw.get("resource_statuses", []),
        affected_count=l1_raw.get("affected_count", 0),
        expired=l1_raw.get("expired", False),
    )

    # Layer 2 — out-of-set claims coerce to UNKNOWN (fail-safe).
    l2_raw = raw.get("layer2") or {}
    layer2 = Layer2Result(
        status=l2_raw.get("status", "unknown")
        if l2_raw.get("status", "unknown") in LAYER2_STATUS_VALUES else "unknown",
        details=l2_raw.get("details", ""),
    )

    # Checklist — items whose status is outside the closed set (or
    # missing) cannot be represented in the model: dropped EXPLICITLY
    # (logged, counted from survivors) instead of silently.
    checklist = None
    cl_raw = raw.get("checklist")
    if cl_raw and isinstance(cl_raw, dict):
        items = []
        dropped = []
        for item in cl_raw.get("items", []):
            if not isinstance(item, dict):
                continue
            if item.get("status") not in CHECKLIST_STATUS_VALUES:
                dropped.append(item.get("step"))
                continue
            try:
                items.append(ChecklistItem(
                    step=item.get("step", 0),
                    description=item.get("description", ""),
                    status=item.get("status"),
                    evidence=item.get("evidence", ""),
                ))
            except (ValueError, KeyError):
                dropped.append(item.get("step"))
        if dropped:
            logger.warning(
                "dict_to_verification_result: dropped %d checklist item(s) "
                "with status outside the closed vocabulary (steps: %s)",
                len(dropped), dropped,
            )
        # Counts are recomputed from the surviving items — a stale
        # raw-claimed total_count contradicts the items at this boundary.
        # Fail-closed rule is single-sourced: benign complement of
        # CHECKLIST_BENIGN_STATUSES (verdict.py legislation).
        checklist = Checklist(
            items=items,
            total_count=len(items),
            skipped_count=sum(
                1 for it in items if it.status == ChecklistItemStatus.SKIPPED
            ),
            non_passed_count=sum(
                1 for it in items
                if it.status.value not in CHECKLIST_BENIGN_STATUSES
            ),
        )

    # Warnings
    warnings: list[StructuredWarning] = []
    for w in raw.get("warnings", []):
        if isinstance(w, str):
            warnings.append(StructuredWarning(code=_classify_warning(w), detail=w))
        elif isinstance(w, dict):
            warnings.append(StructuredWarning.model_validate(w))

    # Level
    level_str = raw.get("level", "unverified")
    try:
        level = InjectVerdict(level_str)
    except ValueError:
        level = InjectVerdict.UNVERIFIED

    return VerificationResult(
        level=level,
        layer1=layer1,
        layer2=layer2,
        checklist=checklist,
        warnings=warnings,
        baseline_used=raw.get("baseline_used"),
        baseline_confidence=raw.get("baseline_confidence"),
        primary_evidence_observed=raw.get("primary_evidence_observed"),
        side_effects=raw.get("side_effects"),
        overall=raw.get("overall", ""),
    )