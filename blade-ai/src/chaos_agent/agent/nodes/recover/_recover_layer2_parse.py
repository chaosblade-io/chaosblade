"""Layer 2 parse domain for recover verifier.

Extracted from recover_verifier.py — contains all parse/detect functions
and data constants used by _parse_recovery_verification_result and its
sub-functions.
"""

import logging
import re

from chaos_agent.transports import PROFILE_K8S
from chaos_agent.agent.nodes.verify._verifier_shared import (
    _has_negative_prefix,
    _parse_status_keyword,
    has_checklist,
    parse_checklist_items,
)
from chaos_agent.agent.result.verdict import ChecklistItemStatus
from chaos_agent.utils.skill_case_section import find_section

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Layer 2 prompt
# ---------------------------------------------------------------------------

def _build_recover_verifier_prompt(
    *, layer1_label: str = "deterministic destroy", profile: str = PROFILE_K8S,
    ledger_section: str = "",
) -> str:
    """Build the recover verifier system prompt using U-shaped composition.

    Delegates to section functions in recovery.py, following the same
    architecture pattern as the inject verifier (verification.py).
    Core Principles at BEGINNING (primacy); the prompt CLOSES on the
    machine-parsed Output contract (REMEMBER recency mirror removed in
    the 2026-09-20 pass-5 cleanup — message tail owns recency), with
    low-priority information in the middle.

    Args:
        layer1_label: The label describing what Layer 1 did — "deterministic
            destroy" for the carrier's programmatic experiment destroy,
            "recovery execution" for the LLM-driven (kubectl-exec /
            non-experiment-carrier) path.
        profile: Channel profile ("k8s"|"host").
    """
    from chaos_agent.agent.prompts.sections.recovery import build_recover_verifier_system_prompt

    return build_recover_verifier_system_prompt(
        layer1_label=layer1_label, profile=profile, ledger_section=ledger_section
    )


# ---------------------------------------------------------------------------
# Recovery verification checklist parsing (mirrors verifier.py checklist logic)
# ---------------------------------------------------------------------------

# Recovery-specific checklist patterns — the status vocabulary derives
# from the legislation enum (B76 round-14 root-cause fix): this regex
# previously taught a 5-word set while the recover submit prompt taught a
# different 4-word set and the inject regex a third 6-word set — three
# hand-copies, three drifts. Longest-first ordering keeps a future member
# that prefixes an existing one from shadowing it in the alternation.
_RECOVERY_ITEM_STATUS_ALT = "|".join(
    sorted((m.value for m in ChecklistItemStatus), key=len, reverse=True)
)

_RECOVERY_CHECKLIST_PATTERNS = [
    # Primary: Step N: <status> / Check N: <status>
    re.compile(
        rf"(?:step|check)\s*(\d+)\s*[:.)]\s*\[?({_RECOVERY_ITEM_STATUS_ALT})\]?",
        re.IGNORECASE,
    ),
    # Explicit skip marker: [SKIPPED] Step N
    re.compile(r"(?<!\d[.:)]\s)\[SKIPPED\]\s*(?:step\s*)?(\d+)?", re.IGNORECASE),
    # Bare numbered list: 1. <status>
    re.compile(
        rf"^\s*(\d+)\s*[.:)]\s*\[?({_RECOVERY_ITEM_STATUS_ALT})\]?",
        re.IGNORECASE | re.MULTILINE,
    ),
]

# Recovery-side free-text contradiction indicators (_RECOVERY_CONTRADICTION_INDICATORS
# / _RECOVERY_ABSENCE_PHRASES) removed (Phase 2c / D14): the program no longer
# reverse-infers "recovery looks effective" or "fault still present" from
# free-text l2_details. Recovery contradiction detection is now a pure
# ChecklistItemStatus enum comparison (see _detect_recovery_contradiction).


def _parse_recovery_checklist_items(text: str) -> list[dict]:
    """Parse Recovery Verification Checklist items from LLM output."""
    return parse_checklist_items(
        text,
        section_marker="RECOVERY_VERIFICATION_CHECKLIST:",
        end_marker="RECOVERY_VERIFICATION_RESULT:",
        patterns=_RECOVERY_CHECKLIST_PATTERNS,
    )


def _has_recovery_checklist(text: str) -> bool:
    """Check if text contains a Recovery Verification Checklist section or items."""
    return has_checklist(
        text,
        section_marker="RECOVERY_VERIFICATION_CHECKLIST:",
        patterns=[_RECOVERY_CHECKLIST_PATTERNS[0], _RECOVERY_CHECKLIST_PATTERNS[2]],
    )


def _count_recovery_steps_in_skill_case(content: str) -> int:
    """Count verification steps from skill case's '恢复验证' section.

    Counts top-level numbered items in the 恢复验证 section, falling back
    to bullet sub-items if no numbered steps are found.

    Section located via ``skill_case_section`` (heading-anchored) — a bare
    substring anchor is hijacked by in-text mentions such as
    「判据见恢复验证第 1 条」 (16 of 105 skill files before the fix).
    """
    section = find_section(content, "恢复验证")
    if section is None:
        return 0

    step_matches = re.findall(r'^\s*(\d+)\.\s', section, re.MULTILINE)
    if step_matches:
        return len(set(step_matches))

    return len(re.findall(r'^\s*[-*]\s', section, re.MULTILINE))


def _extract_recovery_verification_section(content: str) -> str:
    """Extract the 恢复验证 section from a skill case file.

    Instead of injecting the entire skill case (~1,200-6,400 chars), only
    inject the recovery verification section (~100-400 chars) plus any
    cross-referenced 注入验证 steps. This reduces HumanMessage size by
    70-75% while preserving all actionable recovery verification content.

    Section heading is ``## 恢复验证`` / ``## 恢复验证（限定语）`` — with the
    legacy bold form ``**恢复验证**：`` still matched as fallback (all
    resolved by ``skill_case_section``, which superseded the old literal
    ``**恢复验证**：`` match that missed the qualified form on 6 skill
    files). End boundary: next wordlist section heading, ``---`` rule, or
    end of file.
    Cross-references detected: "同注入验证", "再次执行", "注入验证中的", "Pod 级验证方法中的".
    """
    section = find_section(content, "恢复验证")
    if section is None:
        return ""
    section = section.strip()

    # Detect cross-references and extract referenced 注入验证 steps
    cross_ref_keywords = ["同注入验证", "再次执行", "注入验证中的", "Pod 级验证方法中的"]
    referenced_steps = ""
    for kw in cross_ref_keywords:
        if kw not in section:
            continue
        # Extract 注入验证 section
        inject_section = find_section(content, "注入验证")
        if inject_section is None:
            continue
        referenced_steps = (
            "\n\n**Injection-verification reference** "
            f"(the 恢复验证 section cross-references this block):\n{inject_section.strip()}"
        )
        break  # Only add once, even if multiple keywords match

    return f"## 恢复验证\n{section}{referenced_steps}"


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
    checklist_items: list[dict] | None = None,
) -> str | None:
    """Detect a STRUCTURAL contradiction: Layer2 concluded 'failed' but ALL
    checklist steps are 'passed'.

    Pure closed-set ``ChecklistItemStatus`` enum comparison (D14, structured-
    evidence-binding spec) — the program never reverse-infers "recovery looks
    effective" from free-text ``l2_details``. The LLM's structured status is
    authoritative; if every step is 'passed' while the conclusion says 'failed',
    that is the LLM contradicting itself at the enum level.

    The former text-based Check 2 (``_RECOVERY_CONTRADICTION_INDICATORS`` keyword
    match) and the ``_RECOVERY_ABSENCE_PHRASES`` exemption are removed:
    still-present / recovered semantics are the LLM's to declare via status, not
    the program's to guess from prose.
    """
    if not checklist_items:
        return None

    passed_count = sum(
        1 for item in checklist_items
        if isinstance(item, dict) and item.get("status") == "passed"
    )
    if passed_count == len(checklist_items):
        return (
            "Contradiction: Layer2 concluded 'failed' but ALL checklist steps "
            "are 'passed' — a structural contradiction in the LLM's own status "
            "declarations. Overriding to 'partial'."
        )
    return None


# ---------------------------------------------------------------------------
# Generic-primary-evidence text detection removed (Phase 2c / D14).
# _GENERIC_HEALTH_INDICATORS / _FAULT_SPECIFIC_EVIDENCE /
# _detect_primary_evidence_generic_contradiction reverse-inferred "the LLM said
# primary_evidence_observed=true but its prose looks generic" from free-text
# l2_details keyword matching — a program-side semantic guess that overrides the
# LLM's structured declaration. Per the structured-evidence-binding spec the
# "is the evidence fault-specific or generic" judgement belongs to the LLM and is
# carried by its primary_evidence_observed bool; the program only enforces the
# structural 'recovered' + primary_evidence_observed=false gate.
# ---------------------------------------------------------------------------


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

    # Contradiction detection (structured only, D14): L2="failed" but ALL
    # checklist steps are 'passed' — a structural enum contradiction. The former
    # free-text keyword check is removed; the LLM's status is authoritative.
    if result["layer2"]["status"] == "failed":
        contradiction_warning = _detect_recovery_contradiction(
            checklist_items if checklist_items else None,
        )
        if contradiction_warning:
            result["warnings"].append(contradiction_warning)
            result["layer2"]["status"] = "partial"

    # Determine overall level
    if "overall:" in text_lower:
        overall = text_lower.split("overall:", 1)[1].split("\n")[0].strip()
        # "unverified" must be checked FIRST: it contains "verified" as a
        # substring, so the recovered branch below would otherwise swallow it.
        if "unverified" in overall:
            result["level"] = "unverified"
        # "verified" is a common synonym for "recovered" in LLM output
        # (inject verifier uses "verified", LLM may cross-contaminate)
        elif ("recovered" in overall or "verified" in overall) and not _has_negative_prefix(overall, "recovered") and "partial" not in overall and "unrecovered" not in overall:
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

    # Generic-primary-evidence text detection removed (Phase 2c / D14): whether
    # the observed evidence is fault-specific vs generic health is the LLM's
    # judgement, declared via primary_evidence_observed. The program no longer
    # reverse-infers it from l2_details keyword matching. The structural
    # 'recovered' + primary_evidence_observed=false gate above is retained.

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
