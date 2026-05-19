"""Layer 2 parse domain for recover verifier.

Extracted from recover_verifier.py — contains all parse/detect functions
and data constants used by _parse_recovery_verification_result and its
sub-functions.
"""

import logging
import re

from chaos_agent.agent.nodes._verifier_shared import (
    _has_negative_prefix,
    _parse_status_keyword,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Layer 2 prompt
# ---------------------------------------------------------------------------

def _build_recover_verifier_prompt(*, is_chaosblade: bool = True) -> str:
    """Build the recover verifier system prompt using U-shaped composition.

    Delegates to section functions in recovery.py, following the same
    architecture pattern as the inject verifier (verification.py).
    CRITICAL rules at BEGINNING (primacy) + END (recency), with
    low-priority information in the middle.

    Args:
        is_chaosblade: If True, Layer 1 label is "blade_destroy";
                       if False, Layer 1 label is "recovery execution".
    """
    from chaos_agent.agent.prompts.sections.recovery import build_recover_verifier_system_prompt

    return build_recover_verifier_system_prompt(is_chaosblade=is_chaosblade)


# ---------------------------------------------------------------------------
# Recovery verification checklist parsing (mirrors verifier.py checklist logic)
# ---------------------------------------------------------------------------

# Recovery-specific checklist patterns — includes "partial" status not present
# in injection verifier's _CHECKLIST_PATTERNS.
_RECOVERY_CHECKLIST_PATTERNS = [
    # Primary: Step N: <status> / Check N: <status>
    re.compile(
        r"(?:step|check)\s*(\d+)\s*[:.)]\s*\[?(passed|failed|skipped|partial|expected)\]?",
        re.IGNORECASE,
    ),
    # Explicit skip marker: [SKIPPED] Step N
    re.compile(r"(?<!\d[.:)]\s)\[SKIPPED\]\s*(?:step\s*)?(\d+)?", re.IGNORECASE),
    # Bare numbered list: 1. <status>
    re.compile(
        r"^\s*(\d+)\s*[.:)]\s*\[?(passed|failed|skipped|partial|expected)\]?",
        re.IGNORECASE | re.MULTILINE,
    ),
]

# Recovery-side contradiction detection: mirrors injection verifier's
# _CONTRADICTION_INDICATORS + _ABSENCE_PHRASES but with INVERTED semantics.
# Injection detects: L2="failed" but details show fault IS present (injection worked).
# Recovery detects:  L2="failed" but details show recovery IS effective.

_RECOVERY_CONTRADICTION_INDICATORS = {
    "cpu": ["cpu usage normal", "cpu back to normal", "cpu utilization normal",
            "cpu utilization back to", "cpu returned to normal"],
    "network": ["connectivity restored", "network recovered", "network back to normal",
                "latency back to normal", "latency back to baseline",
                "no packet loss", "no network delay", "packet loss resolved"],
    "disk": ["disk usage back to", "diskpressure is false", "diskpressure false",
             "disk usage normal", "disk back to normal", "no disk pressure"],
    "process": ["pod running", "pod is running", "no restarts",
                "pod healthy", "restart count 0", "no crashloop"],
}

# Phrases indicating the fault is STILL present — block contradiction detection.
# Mirrors injection _ABSENCE_PHRASES but inverted: injection uses low percentages
# (at 16%/17%/18%/19%) to indicate fault is absent; recovery uses high percentages
# (at 80%/85%/90%/95%) to indicate fault is still present.
# NOTE: "still running" is intentionally EXCLUDED — in recovery L2 context it
# typically means a pod is healthy (recovery evidence), not that a ChaosBlade
# experiment is still active (that's Layer 1's concern).
_RECOVERY_ABSENCE_PHRASES = (
    "still elevated", "remains high", "still at 95%", "not recovered",
    "still present", "persisting", "not yet recovered",
    "still high", "remains elevated", "at 95%", "at 90%", "at 85%", "at 80%",
    "diskpressure is true", "diskpressure true",
    "still not", "not yet", "still active",
    "still stressed", "remains at 95%", "remains at 90%", "remains at 85%",
)


def _parse_recovery_checklist_items(text: str) -> list[dict]:
    """Parse Recovery Verification Checklist items from LLM output.

    Mirrors _parse_checklist_items() from verifier.py but scoped to
    RECOVERY_VERIFICATION_CHECKLIST section and supports "partial" status.

    Returns list of dicts with 'step' (int) and 'status' (str) keys.
    """
    items = []
    seen_steps: set[str] = set()

    checklist_section = text
    if "RECOVERY_VERIFICATION_CHECKLIST:" in text:
        start = text.index("RECOVERY_VERIFICATION_CHECKLIST:") + len("RECOVERY_VERIFICATION_CHECKLIST:")
        remainder = text[start:]
        if "RECOVERY_VERIFICATION_RESULT:" in remainder:
            end = remainder.index("RECOVERY_VERIFICATION_RESULT:")
            checklist_section = remainder[:end]
        else:
            checklist_section = remainder

    for pattern in _RECOVERY_CHECKLIST_PATTERNS:
        for match in pattern.finditer(checklist_section):
            if "[skipped]" in match.group(0).lower():
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
            items.append({"step": step_num, "status": status})

    return items


def _has_recovery_checklist(text: str) -> bool:
    """Check if text contains a Recovery Verification Checklist section or items."""
    if "RECOVERY_VERIFICATION_CHECKLIST:" in text:
        return True
    # Check both Pattern 0 (Step N: / Check N:) and Pattern 2 (bare N.)
    # to match the prompt's suggested format and common LLM variations.
    return bool(_RECOVERY_CHECKLIST_PATTERNS[0].search(text)) or bool(_RECOVERY_CHECKLIST_PATTERNS[2].search(text))


def _count_recovery_steps_in_skill_case(content: str) -> int:
    """Count verification steps from skill case's '恢复验证' section.

    Counts top-level numbered items in the 恢复验证 section, falling back
    to bullet sub-items if no numbered steps are found.
    """
    if "恢复验证" not in content:
        return 0

    start = content.index("恢复验证")
    remainder = content[start:]
    next_section = re.search(r'\n\*\*[^*]+\*\*', remainder[3:])
    section_content = remainder[:3 + next_section.start()] if next_section else remainder

    step_matches = re.findall(r'^\s*(\d+)\.\s', section_content, re.MULTILINE)
    if step_matches:
        return len(set(step_matches))

    return len(re.findall(r'^\s*[-*]\s', section_content, re.MULTILINE))


def _extract_recovery_verification_section(content: str) -> str:
    """Extract the 恢复验证 section from a skill case file.

    Instead of injecting the entire skill case (~1,200-6,400 chars), only
    inject the recovery verification section (~100-400 chars) plus any
    cross-referenced 注入验证 steps. This reduces HumanMessage size by
    70-75% while preserving all actionable recovery verification content.

    Section delimiter is always: `**恢复验证**：` (verified across all 19 files).
    End boundary: next `**...**：` heading or end of file.
    Cross-references detected: "同注入验证", "再次执行", "注入验证中的", "Pod 级验证方法中的".
    """
    if "恢复验证" not in content:
        return ""

    # Find start: **恢复验证**：
    start_match = re.search(r'\*\*恢复验证\*\*[：:]', content)
    if not start_match:
        return ""
    start_pos = start_match.end()

    # Find end: next **...**： heading
    end_match = re.search(r'\n\*\*[^*]+\*\*[：:]', content[start_pos:])
    if end_match:
        section = content[start_pos:start_pos + end_match.start()].strip()
    else:
        section = content[start_pos:].strip()

    # Detect cross-references and extract referenced 注入验证 steps
    cross_ref_keywords = ["同注入验证", "再次执行", "注入验证中的", "Pod 级验证方法中的"]
    referenced_steps = ""
    for kw in cross_ref_keywords:
        if kw in section:
            # Extract 注入验证 section
            inject_match = re.search(r'\*\*注入验证\*\*[：:]', content)
            if inject_match:
                inject_start = inject_match.end()
                inject_end = re.search(r'\n\*\*[^*]+\*\*[：:]', content[inject_start:])
                if inject_end:
                    inject_section = content[inject_start:inject_start + inject_end.start()].strip()
                else:
                    inject_section = content[inject_start:].strip()
                referenced_steps = f"\n\n**注入验证参考**（恢复验证中引用了此段落):\n{inject_section}"
                break  # Only add once, even if multiple keywords match

    return f"**恢复验证**：\n{section}{referenced_steps}"


def _detect_recovery_checklist_inconsistency(
    checklist_items: list[dict],
    l2_status: str,
) -> str | None:
    """Detect when recovery checklist items contradict the Layer2 conclusion.

    For recovery verification, incomplete steps are more serious than
    in injection verification: skipped = unverified recovery aspect, which
    means we have unknown state; partial = partially verified; failed =
    verification showed fault still present. Claiming "passed" with any
    of these is a false positive.

    Note: 'expected' status items are intentionally excluded from this check.
    'expected' represents anticipated negative results — they are informational
    confirmations, not failures, and should not trigger inconsistency detection.

    Returns a warning message if inconsistency detected, None otherwise.
    """
    if l2_status != "passed" or not checklist_items:
        return None

    incomplete = [item for item in checklist_items if item.get("status") in ("skipped", "partial", "failed")]
    if incomplete:
        step_nums = [item.get("step", "?") for item in incomplete]
        statuses = sorted(set(item.get("status", "?") for item in incomplete))
        return (
            f"Recovery checklist-conclusion inconsistency: Step(s) {step_nums} "
            f"marked {statuses} but Layer2 concluded 'passed'. "
            f"For recovery, unverified or failed steps mean incomplete confirmation — "
            f"auto-downgrading to 'partial'."
        )
    return None


def _detect_recovery_contradiction(
    l2_details: str,
    checklist_items: list[dict] | None = None,
) -> str | None:
    """Detect when L2 says 'failed' but evidence describes recovery effects.

    Two evidence sources, checked in priority order:
    1. Checklist-based: ALL items are 'passed' (structural contradiction in
       LLM's own output — strongest evidence)
    2. Text-based: L2 details contain recovery indicator keywords without
       absence phrases (weaker, keyword-matching evidence)

    Absence phrases in L2 details block BOTH checks: if details explicitly
    describe the fault as still present, the LLM's 'failed' conclusion may
    be justified by evidence outside the checklist.
    """
    if not l2_details and not checklist_items:
        return None

    # Check absence phrases first — they apply to both evidence sources
    has_absence = False
    l2d_lower = ""
    if l2_details:
        l2d_lower = l2_details.lower()
        has_absence = any(phrase in l2d_lower for phrase in _RECOVERY_ABSENCE_PHRASES)

    # Check 1: Checklist-based (stronger — structural contradiction)
    if checklist_items and len(checklist_items) > 0 and not has_absence:
        passed_count = sum(1 for item in checklist_items if item.get("status") == "passed")
        if passed_count == len(checklist_items):
            # ALL checklist steps passed but L2 says failed — contradiction
            if l2_details:
                return (
                    "Contradiction: Layer2 concluded 'failed' but ALL checklist steps "
                    "are 'passed' and details describe no ongoing fault. "
                    "Overriding to 'partial'."
                )
            return (
                "Contradiction: Layer2 concluded 'failed' but ALL checklist steps "
                "are 'passed'. Overriding to 'partial'."
            )

    # Check 2: Text-based (weaker — keyword matching)
    if l2_details and not has_absence:
        for _, indicators in _RECOVERY_CONTRADICTION_INDICATORS.items():
            if any(ind in l2d_lower for ind in indicators):
                return (
                    "Contradiction: Layer2 concluded 'failed' but details describe "
                    "observable recovery effects. Overriding to 'partial'."
                )

    return None


# ---------------------------------------------------------------------------
# Detect PrimaryEvidenceObserved=true but evidence is generic (not fault-specific)
# ---------------------------------------------------------------------------

# Generic health indicators that are NOT primary evidence of fault removal.
# These indicate the target is "alive" but do not prove the specific fault
# effect (CPU spike, disk fill, network delay, etc.) is gone.
_GENERIC_HEALTH_INDICATORS = frozenset({
    "pod running", "pod is running", "pod running", "pods running",
    "no new restarts", "restart count", "restarts: 0",
    "healthy", "health check", "healthcheck",
    "pod status: running", "status: running", "phase: running",
    "ready", "1/1", "pods ready", "deployment available",
    "no errors", "no error", "error-free",
    "service responding", "responding", "reachable",
    "uptime", "container running", "containers running",
    "no crash", "no oom", "not evicted", "not restarting",
    "node ready", "nodes ready",
})

# Fault-specific evidence keywords that ARE primary evidence.
# These prove the specific fault effect has been removed.
_FAULT_SPECIFIC_EVIDENCE = frozenset({
    # CPU metrics
    "cpu usage", "cpu percent", "cpu load", "cpu back", "cpu returned",
    "cpu normal", "cpu baseline", "cpu utilization", "cpu metric",
    "top node", "top pod",
    # Disk / storage metrics
    "disk usage", "disk percent", "disk fill", "disk back", "disk returned",
    "disk normal", "disk baseline", "disk utilization", "disk metric",
    "diskpressure", "diskpressure=false", "diskpressure false",
    "/proc/diskstats", "iowait", "io wait", "%util", "disk io",
    "storage usage", "volume usage",
    # Memory metrics
    "memory usage", "memory percent", "memory back", "memory returned",
    "memory normal", "memory baseline", "memory utilization",
    "memorypressure", "memorypressure=false",
    "rss", "working set",
    # Network metrics
    "latency", "packet loss", "delay", "network delay", "rtt",
    "connectivity restored", "packet drop", "dns resolution",
    "networkpolicy", "network back", "network normal",
    # Process metrics
    "process killed", "process removed", "blade process", "chaosblade process",
    # Pod lifecycle (for pod-kill where restart IS primary evidence)
    "restart observed", "pod restarted", "pod recreated",
    "finalizers removed", "finalizer removed",
    # IO metrics
    "io throughput", "read latency", "write latency",
    "iops", "throughput",
})


def _detect_primary_evidence_generic_contradiction(
    primary_observed: bool,
    l2_details: str,
    *,
    skill_name: str = "",
) -> str | None:
    """Detect when LLM claims PrimaryEvidenceObserved=true but all evidence is generic.

    This is a programmatic guard that catches a specific LLM failure pattern:
    the LLM correctly sets PrimaryEvidenceObserved=true (following the prompt rule)
    but then lists generic health indicators ("Pod Running", "no new restarts")
    instead of fault-specific metrics ("CPU returned to baseline", "disk usage normal").

    The rule from the prompt states:
      "PrimaryEvidenceObserved: true ONLY if you directly observed the specific fault
       effect being absent. If only generic indicators observed, set PrimaryEvidenceObserved: false."

    When PrimaryEvidenceObserved=true but the evidence text contains only generic
    indicators and zero fault-specific indicators, this is a semantic contradiction.

    Special case: for pod-kill/pod-terminating skills, pod restart IS primary evidence
    (the fault effect IS the pod being killed), so generic indicators like "pod running"
    are actually primary evidence in this context.

    Args:
        primary_observed: The parsed PrimaryEvidenceObserved value (True/False).
        l2_details: The Layer2 details text (evidence summary after the status keyword).
        skill_name: The skill name (used for pod-kill exception).

    Returns:
        Warning string if contradiction detected, None otherwise.
    """
    # Only check when PrimaryEvidenceObserved=true
    if not primary_observed:
        return None

    # Special case: pod-kill/pod-terminating skills — "pod running" IS primary evidence
    pod_kill_skills = {"pod-kill", "pod-terminating", "pod-delete"}
    if skill_name and skill_name.lower() in pod_kill_skills:
        return None

    l2d_lower = l2_details.lower()

    # Check if any fault-specific evidence keyword appears in L2 details
    has_fault_specific = any(kw in l2d_lower for kw in _FAULT_SPECIFIC_EVIDENCE)

    # If at least one fault-specific indicator is present, no contradiction
    if has_fault_specific:
        return None

    # Check if generic health indicators appear (confirming the evidence is generic)
    has_generic = any(kw in l2d_lower for kw in _GENERIC_HEALTH_INDICATORS)

    # If no generic indicators either, the evidence text is unclear —
    # don't flag contradiction on ambiguous evidence (avoid false positives)
    if not has_generic:
        return None

    # Contradiction detected: PrimaryEvidenceObserved=true but evidence is purely generic
    return (
        "PrimaryEvidenceObserved=true contradicts evidence: all evidence items are "
        "generic health indicators (pod Running, no restarts, etc.) rather than "
        "fault-specific metrics (CPU/disk/memory/network returned to baseline). "
        "Per prompt rules, PrimaryEvidenceObserved should be false when only generic "
        "indicators are observed. Downgrading to 'partial'."
    )


# ---------------------------------------------------------------------------
# Parse LLM recovery verification result
# ---------------------------------------------------------------------------

def _parse_recovery_verification_result(text: str, *, skill_name: str = "") -> dict:
    """Parse the LLM's recovery verification summary into a structured result."""
    result = {
        "level": "unrecovered",
        "layer1": {"status": "passed", "details": ""},  # Layer 1 already passed
        "layer2": {"status": "unknown", "details": ""},
        "warnings": [],
        "baseline_used": None,
    }

    text_lower = text.lower()

    # Detect wrong-format: LLM used RECOVERY_EXECUTION_RESULT (Layer 1 format)
    # in Layer 2 context instead of RECOVERY_VERIFICATION_RESULT.
    if "recovery_execution_result" in text_lower and "recovery_verification_result" not in text_lower:
        if "status: success" in text_lower or "status:  success" in text_lower:
            result["layer2"]["status"] = "passed"
            result["level"] = "recovered"
            result["warnings"].append(
                "Layer 2 LLM used RECOVERY_EXECUTION_RESULT format instead of "
                "RECOVERY_VERIFICATION_RESULT; inferred Layer2=passed from Status: success"
            )
        else:
            result["layer2"]["status"] = "failed"
            result["warnings"].append(
                "Layer 2 LLM used RECOVERY_EXECUTION_RESULT format with non-success status"
            )
        return result

    # Parse Layer 2
    if "layer2" in text_lower:
        l2_line = text_lower.split("layer2", 1)[1].split("\n")[0]
        l2_status = _parse_status_keyword(l2_line)
        result["layer2"]["status"] = l2_status
        if l2_status == "skipped":
            result["warnings"].append("Layer 2 skipped: LLM could not design a verification plan")
        # Extract details after the status keyword
        for status_kw in ("passed", "failed", "partial", "skipped"):
            kw_idx = l2_line.find(status_kw)
            if kw_idx >= 0:
                after = l2_line[kw_idx + len(status_kw):].strip()
                # Remove leading separator characters
                details = after.lstrip("-: ")
                if details:
                    result["layer2"]["details"] = details
                break

    # --- Recovery Checklist parsing ---
    checklist_items = _parse_recovery_checklist_items(text)
    skipped_count = sum(1 for item in checklist_items if item["status"] == "skipped")
    partial_count = sum(1 for item in checklist_items if item["status"] == "partial")
    failed_count = sum(1 for item in checklist_items if item["status"] == "failed")

    if checklist_items:
        result["checklist"] = {
            "items": checklist_items,
            "skipped_count": skipped_count,
            "partial_count": partial_count,
            "failed_count": failed_count,
            "total_count": len(checklist_items),
        }

    # Checklist-conclusion inconsistency: checklist has skipped/partial steps
    # but Layer2 concluded "passed". For recovery, unverified steps mean
    # incomplete confirmation — auto-downgrade to "partial".
    if checklist_items:
        inconsistency_warning = _detect_recovery_checklist_inconsistency(
            checklist_items, result["layer2"]["status"],
        )
        if inconsistency_warning:
            result["warnings"].append(inconsistency_warning)
            # Auto-downgrade: recovery "skipped/partial" = unverified aspect =
            # unknown state, more dangerous than injection "skipped"
            result["layer2"]["status"] = "partial"

    # Warn when checklist is absent for passed/partial Layer2 results
    if result["layer2"]["status"] in ("passed", "partial") and not _has_recovery_checklist(text):
        result["warnings"].append(
            "No Recovery Verification Checklist detected in LLM output. "
            "Recovery verification completeness cannot be confirmed."
        )

    # Contradiction detection: L2="failed" but evidence describes recovery effects.
    # Checks both L2 details text AND checklist items (if available).
    # Absence phrases in details block detection when fault is still present.
    if result["layer2"]["status"] == "failed":
        contradiction_warning = _detect_recovery_contradiction(
            result["layer2"]["details"],
            checklist_items if checklist_items else None,
        )
        if contradiction_warning:
            result["warnings"].append(contradiction_warning)
            result["layer2"]["status"] = "partial"

    # Determine overall level
    if "overall:" in text_lower:
        overall = text_lower.split("overall:", 1)[1].split("\n")[0].strip()
        # "verified" is a common synonym for "recovered" in LLM output
        # (inject verifier uses "verified", LLM may cross-contaminate)
        if ("recovered" in overall or "verified" in overall) and not _has_negative_prefix(overall, "recovered") and "partial" not in overall and "unrecovered" not in overall:
            result["level"] = "recovered"
        elif (_has_negative_prefix(overall, "recovered") or _has_negative_prefix(overall, "verified")) and "unrecovered" not in overall:
            # "not recovered" / "not verified" (without "unrecovered") → treat as unrecovered
            result["level"] = "unrecovered"
        elif "partial" in overall:
            result["level"] = "partial"
        elif "unrecovered" in overall:
            result["level"] = "unrecovered"
    else:
        l2 = result["layer2"]["status"]
        if l2 == "passed":
            result["level"] = "recovered"
        elif l2 == "partial":
            result["level"] = "partial"
        elif l2 == "skipped":
            result["level"] = "recovered"  # Layer 1 passed, Layer 2 not performed

    # If L2 was downgraded to "partial" (by auto-downgrade or contradiction
    # detection) but level was set from the Overall text, override level to
    # "partial" as well. L2="partial" takes precedence over Overall text
    # because programmatic checks are more reliable than LLM's summary.
    if result["layer2"]["status"] == "partial" and result["level"] in ("recovered", "unrecovered"):
        result["level"] = "partial"

    # Parse BaselineUsed: whether LLM performed baseline comparison
    if "baselineused:" in text_lower:
        bu = text_lower.split("baselineused:", 1)[1].split("\n")[0].strip()
        result["baseline_used"] = "true" in bu

    # Parse PrimaryEvidenceObserved: LLM must explicitly declare whether
    # primary evidence of recovery (fault effects gone) was directly observed.
    # Hard constraint: recovered verdict requires primary evidence.
    primary_observed = None
    if "primaryevidenceobserved:" in text_lower:
        pv = text_lower.split("primaryevidenceobserved:", 1)[1].split("\n")[0].strip()
        primary_observed = "true" in pv

    if result["level"] == "recovered" and primary_observed is False:
        result["level"] = "partial"
        result["warnings"].append(
            "Verdict 'recovered' is incompatible with PrimaryEvidenceObserved=false. "
            "Cannot confirm recovery effectiveness without direct evidence "
            "that fault effects are gone. Downgraded to 'partial'."
        )

    # P2-1: Detect PrimaryEvidenceObserved=true but all evidence is generic
    # (not fault-specific). This catches LLMs that correctly set the flag
    # but fail to actually observe fault-specific metrics.
    if primary_observed is True and result["level"] == "recovered":
        l2_details = result["layer2"].get("details", "")
        generic_warning = _detect_primary_evidence_generic_contradiction(
            primary_observed, l2_details, skill_name=skill_name,
        )
        if generic_warning:
            result["level"] = "partial"
            result["warnings"].append(generic_warning)

    # Warnings: only match explicit "Warnings:" line, not any mention of the word
    for line in text_lower.split("\n"):
        line_stripped = line.strip()
        if line_stripped.startswith("warnings:") and "none" not in line_stripped:
            result["warnings"].append("See recovery verification details for warnings")
            break

    # Warn when Layer 2 result is unknown (aligns with injection verifier pattern)
    if result["layer2"]["status"] == "unknown":
        result["warnings"].append(
            "Layer 2 (fault-specific) recovery verification result is unknown. "
            "The LLM did not produce a clear verification conclusion."
        )

    return result