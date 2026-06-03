"""Verification verdict enums and structured models.

Single source of truth for all verification-related types. All enums
inherit (str, Enum) so JSON serialization is transparent (no custom
encoder needed). Pydantic models provide schema validation and
type-safe construction.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Verdict enums
# ---------------------------------------------------------------------------


class InjectVerdict(str, Enum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"


class RecoverVerdict(str, Enum):
    RECOVERED = "recovered"
    PARTIAL = "partial"
    FAILED = "failed"


class Layer1Status(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    ERROR = "error"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class Layer2Status(str, Enum):
    PASSED = "passed"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"
    RECOVERED_BEFORE_OBSERVATION = "recovered_before_observation"
    UNKNOWN = "unknown"


class ChecklistItemStatus(str, Enum):
    PASSED = "passed"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"
    RECOVERED_BEFORE_OBSERVATION = "recovered_before_observation"


# ---------------------------------------------------------------------------
# Warning codes — closed-set vocabulary
# ---------------------------------------------------------------------------


class WarningCode(str, Enum):
    LAYER2_SKIPPED = "layer2_skipped"
    EXPERIMENT_EXPIRED = "experiment_expired"
    CHECKLIST_HAS_SKIPPED = "checklist_has_skipped"
    CHECKLIST_RECOVERED_BEFORE_OBS = "checklist_recovered_before_obs"
    NO_CHECKLIST_DETECTED = "no_checklist_detected"
    CONTRADICTION_OVERRIDE = "contradiction_override"
    CHECKLIST_CONCLUSION_INCONSISTENCY = "checklist_conclusion_inconsistency"
    PRIMARY_EVIDENCE_NOT_OBSERVED = "primary_evidence_not_observed"
    COVERAGE_INCOMPLETE = "coverage_incomplete"
    CROSS_CHECK_CONTRADICTION = "cross_check_contradiction"
    CROSS_CHECK_DOWNGRADED = "cross_check_downgraded"
    BASELINE_AVAILABLE_NOT_USED = "baseline_available_not_used"
    SEE_VERIFICATION_DETAILS = "see_verification_details"


# ---------------------------------------------------------------------------
# Failure categories — replaces FailureReason enum in errors.py
# ---------------------------------------------------------------------------


class FailureCategory(str, Enum):
    PLANNING_TIMEOUT = "planning_timeout"
    PLANNING_REJECTED = "planning_rejected"
    SAFETY_REJECTED = "safety_rejected"
    USER_REJECTED = "user_rejected"
    PREREQUISITE_FAILED = "prerequisite_failed"
    EXECUTION_FAILED = "execution_failed"
    EXECUTION_TIMEOUT = "execution_timeout"
    REPLAN_EXHAUSTED = "replan_exhausted"
    VERIFICATION_FAILED = "verification_failed"
    RECOVERY_FAILED = "recovery_failed"
    RECOVERY_VERIFICATION_TIMEOUT = "recovery_verification_timeout"
    INTERNAL_ERROR = "internal_error"
    WALL_CLOCK_TIMEOUT = "wall_clock_timeout"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class StructuredWarning(BaseModel):
    code: WarningCode
    detail: str = ""


class FailureDetail(BaseModel):
    category: FailureCategory
    context: str = ""
    llm_analysis: str = ""

    def to_reason_string(self) -> str:
        """Legacy-compatible failure_reason string."""
        base = f"{self.category.value}: {self.context}" if self.context else self.category.value
        if self.llm_analysis:
            return f"{base} | llm_analysis: {self.llm_analysis}"
        return base


class ChecklistItem(BaseModel):
    step: int
    description: str = ""
    status: ChecklistItemStatus
    evidence: str = ""


class Checklist(BaseModel):
    items: list[ChecklistItem] = []
    total_count: int = 0
    skipped_count: int = 0
    non_passed_count: int = 0


class Layer1Result(BaseModel):
    status: Layer1Status = Layer1Status.UNKNOWN
    details: str = ""
    raw_output: str = ""
    resource_statuses: list[dict] = []
    affected_count: int = 0
    expired: bool = False

    def is_passed(self) -> bool:
        return self.status == Layer1Status.PASSED

    def is_terminal(self) -> bool:
        if self.expired:
            return False
        return self.status in (Layer1Status.FAILED, Layer1Status.ERROR)


class Layer2Result(BaseModel):
    status: Layer2Status = Layer2Status.UNKNOWN
    details: str = ""


class VerificationResult(BaseModel):
    """Inject verification result."""

    level: InjectVerdict = InjectVerdict.UNVERIFIED
    layer1: Layer1Result = Layer1Result()
    layer2: Layer2Result = Layer2Result()
    checklist: Optional[Checklist] = None
    warnings: list[StructuredWarning] = []
    baseline_used: Optional[bool] = None
    baseline_confidence: Optional[str] = None
    primary_evidence_observed: Optional[bool] = None
    side_effects: Optional[dict] = None
    overall: str = ""

    def add_warning(self, code: WarningCode, detail: str = "") -> None:
        self.warnings.append(StructuredWarning(code=code, detail=detail))

    def has_warning(self, code: WarningCode) -> bool:
        return any(w.code == code for w in self.warnings)


class RecoverVerificationResult(BaseModel):
    """Recovery verification result."""

    level: RecoverVerdict = RecoverVerdict.FAILED
    layer1: Layer1Result = Layer1Result()
    layer2: Layer2Result = Layer2Result()
    checklist: Optional[Checklist] = None
    warnings: list[StructuredWarning] = []

    def add_warning(self, code: WarningCode, detail: str = "") -> None:
        self.warnings.append(StructuredWarning(code=code, detail=detail))

    def has_warning(self, code: WarningCode) -> bool:
        return any(w.code == code for w in self.warnings)
