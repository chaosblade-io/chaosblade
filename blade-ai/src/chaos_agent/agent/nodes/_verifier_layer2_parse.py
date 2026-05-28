"""Layer 2 parse domain for verifier: checklist/JSON/result parsing.

Extracted from verifier.py — contains the parsing logic for LLM verification
output: checklist parsing, JSON parsing, and the main _parse_verification_result
function that combines both into a structured result dict.
"""

import json
import logging
import re

from langchain_core.messages import HumanMessage

from chaos_agent.agent.nodes._verifier_shared import (
    _parse_status_keyword,
)
from chaos_agent.agent.verdict import (
    Checklist,
    ChecklistItem,
    ChecklistItemStatus,
    InjectVerdict,
    Layer1Result,
    Layer1Status,
    Layer2Result,
    Layer2Status,
    StructuredWarning,
    VerificationResult,
    WarningCode,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# E2 Phase 3 — Evidence cross-check
# ---------------------------------------------------------------------------
#
# After the LLM emits its VERIFICATION_RESULT, this helper compares the
# baseline→post numbers it CITES in evidence against the structured
# metric timeline accumulated on state.metric_observations
# (populated by PreReasoningHook from each ToolMessage's extracted_metrics).
#
# Failure mode this catches: the LLM claims "RestartCount 7 → 8 (Δ+1)"
# in evidence — but the timeline shows RestartCount stayed at 7 across
# every iteration. Without this cross-check the LLM could declare
# ``verified`` based on a hallucinated change. We don't try to be clever
# about partial agreement; the rule is simple — if the LLM claims a
# change AND the timeline says no change for the same metric, downgrade
# the verdict and warn.

# Number with optional unit suffix (%, m for millicores, Mi/Gi/Ki for memory).
# The whitespace tolerance covers both "7 → 8" and "7→8". Unit suffix
# groups are non-capturing — we only care about the numeric values for
# delta computation; unit parsing happens later via _parse_numeric.
_DELTA_PATTERN = re.compile(
    r"(?P<base>\d+(?:\.\d+)?)\s*(?:%|m|Mi|Gi|Ki)?\s*(?:→|->|to|—>)\s*"
    r"(?P<post>\d+(?:\.\d+)?)\s*(?:%|m|Mi|Gi|Ki)?",
    re.IGNORECASE,
)

# How many chars of left context to scan for a metric-name mention.
# Smaller window → fewer false positives; larger → catches metric name
# preceding multiple sentences. 80 chars ≈ one sentence.
_METRIC_CONTEXT_LOOKBACK = 80


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


def _metric_alias(name: str) -> str:
    """Normalise a metric name for substring matching against LLM evidence.

    Drops parenthetical qualifiers and lowercases — so
    ``"Disk usage (overlay)"`` matches LLM text mentioning
    ``"disk usage"`` regardless of whether it spelled out the partition.
    """
    return re.sub(r"\s*\([^)]*\)", "", name).strip().lower()


def _pick_metric_by_context(
    context_lc: str,
    truth: dict[str, tuple[float, float]],
) -> str | None:
    """Resolve which truth metric a given evidence context refers to.

    Two priorities:

    1. **Full-name hit** — if the context literally contains the
       full metric name (e.g. ``"disk usage (overlay)"``), that's the
       answer. Most specific signal wins.
    2. **Alias hit** — fall back to the parenthetical-stripped form
       (e.g. ``"disk usage"`` matching either overlay or nodefs). When
       multiple metrics share the same alias, prefer the one with the
       LONGEST full name, on the theory that more-qualified metric
       names are usually more recently added and thus more specific.
       If still ambiguous, return ``None`` rather than guess wrong —
       false silence here is better than a false contradiction
       downgrading a real-injection verdict.

    Returns ``None`` when no metric name appears in the context window.
    """
    # Priority 1: full-name match (deterministic — exactly one full
    # name per truth key, no ambiguity).
    for metric_name in truth:
        if metric_name.lower() in context_lc:
            return metric_name

    # Priority 2: alias match. Collect all candidates, dedupe by alias
    # to detect collisions.
    alias_hits = [m for m in truth if _metric_alias(m) in context_lc]
    if not alias_hits:
        return None
    if len(alias_hits) == 1:
        return alias_hits[0]

    # Multiple aliases collide (e.g. "Disk usage (overlay)" and
    # "Disk usage (nodefs)" both alias to "disk usage"). The evidence
    # didn't qualify which one — refuse to guess. Returning None here
    # is the conservative-side path: we skip the cross-check for this
    # delta rather than risk a false contradiction warning that
    # would wrongly downgrade a valid verdict.
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


def _build_truth_deltas(
    observations: list[dict],
) -> dict[str, tuple[float, float]]:
    """For each metric seen across observations, compute (baseline, post).

    Baseline = earliest iteration's value; post = latest iteration's
    value. Skips metrics where either value isn't numeric (e.g.
    ``"OOMKilled"``, ``"True"``) — those have their own categorical
    detection elsewhere and don't fit the delta-comparison model.
    """
    by_metric: dict[str, list[tuple[int, str]]] = {}
    for obs in observations or []:
        iteration = int(obs.get("iteration", 0))
        for name, value in (obs.get("metrics") or {}).items():
            by_metric.setdefault(name, []).append((iteration, value))

    deltas: dict[str, tuple[float, float]] = {}
    for name, values in by_metric.items():
        if len(values) < 2:
            continue
        values.sort(key=lambda t: t[0])
        base = _parse_numeric(values[0][1])
        post = _parse_numeric(values[-1][1])
        if base is None or post is None:
            continue
        deltas[name] = (base, post)
    return deltas


def _find_contradictions(
    evidence: str,
    truth: dict[str, tuple[float, float]],
    n_obs: int,
) -> list[str]:
    """Scan evidence for ``X→Y`` deltas that contradict ``truth``.

    Conservative MVP: only flags the "LLM claims change, truth shows
    no change" case. Inverse cases are handled elsewhere
    (``_detect_checklist_conclusion_inconsistency``).
    """
    contradictions: list[str] = []
    for match in _DELTA_PATTERN.finditer(evidence):
        base_str, post_str = match.group("base"), match.group("post")
        try:
            llm_base, llm_post = float(base_str), float(post_str)
        except ValueError:
            continue
        llm_delta = llm_post - llm_base
        if llm_delta == 0:
            continue  # LLM claims no change — nothing to disprove

        ctx_start = max(0, match.start() - _METRIC_CONTEXT_LOOKBACK)
        context_lc = evidence[ctx_start:match.start()].lower()
        metric = _pick_metric_by_context(context_lc, truth)
        if metric is None:
            continue

        truth_base, truth_post = truth[metric]
        # Only flag the strong-signal case: truth shows ZERO change but
        # LLM cites a non-zero delta. Partial mismatches (e.g. truth
        # delta = +1, LLM delta = +5) might still be a real change the
        # LLM rounded — too noisy to flag.
        if truth_post - truth_base == 0:
            contradictions.append(
                f"LLM evidence cites {metric} "
                f"{base_str}→{post_str} (Δ={llm_delta:+g}), "
                f"but observation timeline shows no change "
                f"(stayed at {truth_base:g} across {n_obs} iteration(s))"
            )
    return contradictions


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
) -> dict:
    """Detect LLM evidence numbers that contradict the observation timeline.

    Pipeline:
      1. Build per-metric truth deltas from observations.
      2. Gather all free-text evidence the LLM emitted.
      3. Find contradictions (LLM claims delta, truth says zero).
      4. If any: append warnings + downgrade verdict.

    Returns ``result`` (mutated). Safe to call with empty / missing
    observations — early-exits with no mutation.
    """
    if not observations:
        return result
    truth = _build_truth_deltas(observations)
    if not truth:
        return result
    evidence = _collect_evidence_text(result)
    if not evidence:
        return result
    contradictions = _find_contradictions(evidence, truth, len(observations))
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


_CONTRADICTION_INDICATORS = {
    "cpu": ["cpu usage high", "cpu utilization", "cpu at"],
    "network": ["packet loss", "network delay", "latency increased",
                "connection refused", "network loss"],
    "disk": ["disk usage", "disk full", "no space left"],
    "process": ["restart count", "crashloop", "oomkilled"],
}


# _has_negative_prefix and _parse_status_keyword moved to _verifier_shared.py


# ---------------------------------------------------------------------------
# Checklist parsing: detect skipped verification steps for auto-downgrade
# ---------------------------------------------------------------------------

_CHECKLIST_PATTERNS = [
    # Primary: Step N: <status> [— evidence]
    # Captures step number, status, and optional evidence text after separator.
    re.compile(
        r"(?:step|check)\s*(\d+)\s*[:.)]\s*\[?(passed|failed|skipped|recovered_before_observation|expected)\]?"
        r"(?:\s*[—–-]\s*(.+?))?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    # Explicit skip marker from prompt instruction: [SKIPPED] Step N
    # Negative lookbehind prevents matching [skipped] inside "2. [skipped]"
    # or "Step 1: [skipped]" — those are handled by Pattern 0 and Pattern 2.
    re.compile(r"(?<!\d[.:)]\s)\[SKIPPED\]\s*(?:step\s*)?(\d+)?", re.IGNORECASE),
    # Bare numbered list: 1. <status> [— evidence]
    re.compile(
        r"^\s*(\d+)\s*[.:)]\s*\[?(passed|failed|skipped|recovered_before_observation|expected)\]?"
        r"(?:\s*[—–-]\s*(.+?))?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
]


def _parse_checklist_items(text: str) -> list[dict]:
    """Parse Verification Checklist items from LLM output.

    Returns list of dicts with 'step' (int), 'status' (str), and
    optional 'evidence' (str) keys.
    """
    items = []
    seen_steps: set[str] = set()

    # Scope search to the VERIFICATION_CHECKLIST section if present
    checklist_section = text
    if "VERIFICATION_CHECKLIST:" in text:
        start = text.index("VERIFICATION_CHECKLIST:") + len("VERIFICATION_CHECKLIST:")
        remainder = text[start:]
        if "VERIFICATION_RESULT:" in remainder:
            end = remainder.index("VERIFICATION_RESULT:")
            checklist_section = remainder[:end]
        else:
            checklist_section = remainder

    for pattern in _CHECKLIST_PATTERNS:
        for match in pattern.finditer(checklist_section):
            # Determine step number
            if "[skipped]" in match.group(0).lower():
                # [SKIPPED] pattern: group(1) may be None
                step_str = match.group(1) if match.group(1) else str(len(seen_steps) + 1)
                status = "skipped"
            else:
                step_str = match.group(1)
                status = match.group(2).lower()

            if step_str in seen_steps:
                continue
            seen_steps.add(step_str)
            try:
                step_num = int(step_str)
            except ValueError:
                step_num = len(items) + 1
            item: dict = {"step": step_num, "status": status}
            # Extract evidence text (group 3 in patterns that capture it)
            evidence = match.group(3) if match.lastindex and match.lastindex >= 3 else None
            if evidence:
                item["evidence"] = evidence.strip()
            items.append(item)

    return items


def _has_checklist(text: str) -> bool:
    """Check if text contains a Verification Checklist section or items."""
    if "VERIFICATION_CHECKLIST:" in text:
        return True
    # Also check for individual checklist item patterns
    for pattern in _CHECKLIST_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _detect_checklist_conclusion_inconsistency(
    checklist_items: list[dict],
    l2_status: str,
    failed_evidence: str = "",
) -> tuple[str | None, bool]:
    """Detect when checklist items contradict the Layer2 conclusion.

    Checks if any checklist item is 'failed' while Layer2 overall says 'passed'.
    This is a structural inconsistency in the LLM's own output.

    Note: 'expected' status items are intentionally excluded from this check.
    'expected' represents anticipated negative results (e.g., DiskPressure=False
    when below threshold) — they are informational confirmations, not failures,
    and should not trigger inconsistency detection.

    For transient faults (e.g. disk-burn), steps marked 'recovered_before_observation'
    with no 'failed' or 'partial' steps do NOT trigger inconsistency when Layer2
    is 'passed' — the LLM correctly judged the fault was active despite the
    observation tool limitation.

    When the failed items' evidence contains ABSENCE phrases (metrics far below
    threshold, not just slightly off), the conclusion is auto-downgraded to
    'partial' because objective measurement overrides subjective judgment.

    Returns:
        (warning_message | None, should_auto_downgrade: bool)
    """
    if l2_status != "passed" or not checklist_items:
        return None, False

    _non_passed_statuses = ("failed", "partial", "recovered_before_observation")
    non_passed_items = [item for item in checklist_items if item.get("status") in _non_passed_statuses]
    if not non_passed_items:
        return None, False

    failed_items = [item for item in non_passed_items if item.get("status") == "failed"]
    partial_items = [item for item in non_passed_items if item.get("status") == "partial"]
    recovered_items = [item for item in non_passed_items if item.get("status") == "recovered_before_observation"]

    if not failed_items and not partial_items and recovered_items:
        return None, False

    all_evidence = " ".join(item.get("evidence", "") for item in non_passed_items)
    if failed_evidence:
        all_evidence += " " + failed_evidence
    evidence_lower = all_evidence.lower()

    _INJECT_ABSENCE_PHRASES = (
        "no change", "no increase", "no increase observed", "not observed",
        "not elevated", "not increased", "no effect", "not affected",
        "remains at", "remains normal", "unchanged", "no fill",
        "still normal", "no observable", "not yet observable",
        "far below", "well below", "at 1%", "at 2%", "at 3%",
        "at 4%", "at 5%", "at 6%", "at 7%", "at 8%", "at 9%",
        "at 10%", "at 11%", "at 12%", "at 13%", "at 14%", "at 15%",
        "at 16%", "at 17%", "at 18%", "at 19%", "at 20%",
        "below threshold", "under threshold",
    )
    has_absence = any(phrase in evidence_lower for phrase in _INJECT_ABSENCE_PHRASES)

    status_parts = []
    if failed_items:
        status_parts.append(f"Step(s) {[i.get('step', '?') for i in failed_items]} marked 'failed'")
    if partial_items:
        status_parts.append(f"Step(s) {[i.get('step', '?') for i in partial_items]} marked 'partial'")
    if recovered_items:
        status_parts.append(f"Step(s) {[i.get('step', '?') for i in recovered_items]} marked 'recovered_before_observation'")
    status_desc = "; ".join(status_parts)

    warning = (
        f"Checklist-conclusion inconsistency: {status_desc} "
        f"but Layer2 concluded 'passed'. "
    )
    if has_absence:
        warning += (
            "Failed steps contain absence evidence (metric far below threshold). "
            "Auto-downgrading to 'partial' — objective measurement overrides subjective judgment."
        )
        return warning, True
    else:
        warning += (
            "The LLM may have determined these failures are benign (e.g., timing delays). "
            "Overall field is the final authority."
        )
        return warning, False


def _count_verification_steps_in_skill_case(content: str) -> int:
    """Count verification steps from skill case's '注入验证' section.

    Counts top-level numbered items in the 注入验证 section, falling back
    to bullet sub-items if no numbered steps are found.
    """
    if "注入验证" not in content:
        return 0

    start = content.index("注入验证")
    remainder = content[start:]
    # Find next section header (**) or end of content
    next_section = re.search(r'\n\*\*[^*]+\*\*', remainder[3:])
    section_content = remainder[:3 + next_section.start()] if next_section else remainder

    # Count top-level numbered steps (1., 2., 3., etc.)
    step_matches = re.findall(r'^\s*(\d+)\.\s', section_content, re.MULTILINE)
    if step_matches:
        return len(set(step_matches))

    # Fallback: count bullet sub-items
    return len(re.findall(r'^\s*[-*]\s', section_content, re.MULTILINE))


def _has_injection_verification_section(content: str) -> bool:
    """Check if skill case content contains an 注入验证 section.

    Unlike _count_verification_steps, this is purely structural —
    returns True even if the section has only prose paragraphs
    without numbered or bullet steps.
    """
    return "注入验证" in content


def _extract_verification_step_descriptions(content: str) -> list[str]:
    """Extract verification step descriptions from skill case's 注入验证 section.

    Returns a list of description strings in order, e.g.:
    ["查看 Pod CPU 使用率监控，确认持续高于阈值", "进入容器查看 CPU 占用进程", ...]
    Returns empty list if no 注入验证 section or steps can't be parsed.
    """
    if "注入验证" not in content:
        return []

    start = content.index("注入验证")
    remainder = content[start:]
    next_section = re.search(r'\n\*\*[^*]+\*\*', remainder[3:])
    section = remainder[:3 + next_section.start()] if next_section else remainder

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

    l2 = data.get("layer2", "unknown")
    overall = data.get("overall", "unverified")
    if l2 not in ("passed", "failed", "skipped", "partial", "recovered_before_observation"):
        return None
    if overall not in ("verified", "partial", "unverified"):
        return None

    result = {
        "level": overall,
        "layer1": {"status": data.get("layer1", "unknown"), "details": ""},
        "layer2": {"status": l2, "details": data.get("layer2_details", "")},
        "warnings": data.get("warnings", []),
    }

    checklist = data.get("verification_checklist")
    if isinstance(checklist, list) and checklist:
        result["checklist"] = {
            "items": checklist,
            "skipped_count": sum(1 for c in checklist if c.get("status") == "skipped"),
            "non_passed_count": sum(1 for c in checklist if c.get("status") in ("failed", "partial", "recovered_before_observation")),
            "total_count": len(checklist),
            "total_executed": len(checklist),
        }
        # Checklist-conclusion inconsistency check (same logic as text parser)
        if l2 == "passed":
            _non_passed_ev = " ".join(
                c.get("evidence", "") for c in checklist
                if c.get("status") in ("failed", "partial", "recovered_before_observation")
            )
            inconsistency_warning, should_downgrade = _detect_checklist_conclusion_inconsistency(
                checklist, l2, _non_passed_ev,
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


def _has_format_reminder(messages: list) -> bool:
    """Check if a VERIFICATION_RESULT format reminder has already been injected."""
    return any(
        isinstance(msg, HumanMessage)
        and "缺少要求的 VERIFICATION_RESULT 格式" in (msg.content or "")
        for msg in messages
    )


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
        # Extract details after the status keyword
        for status_kw in ("recovered_before_observation", "passed", "failed", "partial", "skipped"):
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
    non_passed_count = sum(1 for item in checklist_items if item["status"] in ("failed", "partial", "recovered_before_observation"))

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

    # recovered_before_observation: fault effect dissipated before verification
    recovered_count = sum(1 for item in checklist_items if item.get("status") == "recovered_before_observation")
    if recovered_count > 0:
        result["warnings"].append(
            f"Verification Checklist has {recovered_count} step(s) marked "
            f"'recovered_before_observation'. The fault effect had already "
            f"dissipated before verification could observe it."
        )
        # When ALL steps are recovered_before_observation, overall should be unverified
        if recovered_count == len(checklist_items) and result["level"] != "unverified":
            result["level"] = "unverified"

    if result["layer2"]["status"] in ("passed", "partial") and not _has_checklist(text):
        result["warnings"].append(
            "No Verification Checklist detected in LLM output. "
            "Verification completeness cannot be confirmed."
        )

    # Contradiction detection: Layer2 "failed" but details describe observable fault effects
    # A true contradiction is when the LLM says "failed" but the details contain
    # AFFIRMATIVE descriptions of fault effects (e.g., "disk usage at 95%").
    # Negative/absence context (e.g., "disk usage at 16%, no increase") is NOT
    # a contradiction — the LLM correctly describes why it concluded "failed".
    if result["layer2"]["status"] == "failed" and result["layer2"]["details"]:
        l2d = result["layer2"]["details"]
        l2d_lower = l2d.lower()
        _ABSENCE_PHRASES = (
            "no change", "no increase", "no increase observed", "not observed",
            "not elevated", "not increased", "no effect", "not affected",
            "remains at", "remains normal", "unchanged", "no fill",
            "still normal", "at 16%", "at 17%", "at 18%", "at 19%",
            "no observable", "not yet observable",
        )
        is_absence_context = any(phrase in l2d_lower for phrase in _ABSENCE_PHRASES)
        if not is_absence_context:
            for _, indicators in _CONTRADICTION_INDICATORS.items():
                if any(ind in l2d for ind in indicators):
                    result["warnings"].append(
                        "Contradiction: Layer2 concluded 'failed' but details describe "
                        "observable fault effects. Overriding to 'partial'."
                    )
                    result["layer2"]["status"] = "partial"
                    break

    # Checklist-conclusion inconsistency: checklist says failed but Layer2 says passed
    if checklist_items:
        # Collect evidence text from non-passed items for absence-phrase detection
        _non_passed_evidence = " ".join(
            item.get("evidence", "") for item in checklist_items
            if item.get("status") in ("failed", "partial", "recovered_before_observation")
        )
        inconsistency_warning, should_downgrade = _detect_checklist_conclusion_inconsistency(
            checklist_items, result["layer2"]["status"], _non_passed_evidence,
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

    # If L2 was programmatically downgraded to "partial" (e.g., absence evidence
    # detected by _detect_checklist_conclusion_inconsistency), override the level
    # to "partial" as well. Objective measurement overrides subjective judgment.
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
            "Only general blade_status verification was performed."
        )
    elif result["layer2"]["status"] == "unknown":
        result["warnings"].append(
            "Layer 2 (fault-specific) verification result is unknown. "
            "The LLM did not produce a clear verification conclusion. "
            "Only general blade_status verification was confirmed."
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
    """
    # Layer 1
    l1_raw = raw.get("layer1") or {}
    layer1 = Layer1Result(
        status=l1_raw.get("status", "unknown"),
        details=l1_raw.get("details", ""),
        raw_output=l1_raw.get("raw_output", ""),
        resource_statuses=l1_raw.get("resource_statuses", []),
        affected_count=l1_raw.get("affected_count", 0),
        expired=l1_raw.get("expired", False),
    )

    # Layer 2
    l2_raw = raw.get("layer2") or {}
    layer2 = Layer2Result(
        status=l2_raw.get("status", "unknown"),
        details=l2_raw.get("details", ""),
    )

    # Checklist
    checklist = None
    cl_raw = raw.get("checklist")
    if cl_raw and isinstance(cl_raw, dict):
        items = []
        for item in cl_raw.get("items", []):
            if isinstance(item, dict):
                try:
                    items.append(ChecklistItem(
                        step=item.get("step", 0),
                        description=item.get("description", ""),
                        status=item.get("status", "passed"),
                        evidence=item.get("evidence", ""),
                    ))
                except (ValueError, KeyError):
                    pass
        checklist = Checklist(
            items=items,
            total_count=cl_raw.get("total_count", len(items)),
            skipped_count=cl_raw.get("skipped_count", 0),
            non_passed_count=cl_raw.get("non_passed_count", 0),
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