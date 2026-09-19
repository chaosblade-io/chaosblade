"""Verification verdict enums and structured models.

Single source of truth for all verification-related types. All enums
inherit ``StrEnum`` so JSON serialization is transparent (no custom
encoder needed) AND every string rendering path (f-string, str(),
format()) yields the plain value — never the ``ClassName.MEMBER`` form
(B76 round-13 Q1: the legacy ``(str, Enum)`` form rendered
``Layer1Status.FAILED`` into 38 user-visible/contract sites, including
the failure_reason contract field; comparisons were always correct, only
the display half was broken). Pydantic models provide schema validation
and type-safe construction for the public API surface; the production
pipeline itself is dict-shaped end-to-end and enforces these closed sets
at the LLM boundary via the derived ``*_VALUES`` constants below (B76
round-14: the models are not constructed on the main chain).

Vocabulary single-sourcing (B76 round-14 root-cause fix): every closed
set below is legislation. Enforcement sites — clamps, parser regexes,
prompt teaching lines, counting subsets — MUST derive from the enums /
derived constants, never hand-copy the word list. Round-14 found three
mutually contradictory hand-copies of the recover-level vocabulary live
at once (enum said failed, pipeline clamp said unverified/unrecovered,
prompt taught a fourth set) and twelve hand-copies of the non-passed
checklist triple across two files. Derivation makes drift structurally
impossible: change the enum, every enforcement point follows.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Verdict enums
#
# The whole family is ``StrEnum`` (not ``(str, Enum)``) by legislation:
# on Python 3.11 a ``(str, Enum)`` member renders its FULL class name in
# f-strings (``f"{Layer1Status.FAILED}"`` → "Layer1Status.FAILED"),
# poisoning every display/contract path that interpolates a status
# object directly, while equality keeps working — so tests stay green
# and the defect survives. ``StrEnum`` makes the value form the only
# rendering form. Verified equivalent on 3.10/3.11/3.12/3.14
# (.codex_work/probe_enum_compat.py).
# ---------------------------------------------------------------------------


class InjectVerdict(StrEnum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"


class RecoverVerdict(StrEnum):
    """Recovery verdict — the four words the pipeline actually produces.

    B76 round-14 F1: this enum previously legislated
    {recovered, partial, failed}, but the enforcement clamp
    (recover finalize) and the submit prompt both operate on the
    four-word set below — "failed" was unreachable (the clamp maps any
    out-of-set claim, including a literal "failed", to "unrecovered")
    and step-level failure is carried by Layer1/Layer2 status, not by
    the recovery level. "unverified" (observation channel unavailable)
    and "unrecovered" (counter-evidence: fault still present) are
    deliberately distinct words — the vocabulary exists to prevent
    conflating "cannot tell" with "fault persists".
    """

    RECOVERED = "recovered"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"
    UNRECOVERED = "unrecovered"


class Layer1Status(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"
    ERROR = "error"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"
    IN_PROGRESS = "in_progress"


class Layer2Status(StrEnum):
    PASSED = "passed"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"
    RECOVERED_BEFORE_OBSERVATION = "recovered_before_observation"
    UNKNOWN = "unknown"


class ChecklistItemStatus(StrEnum):
    PASSED = "passed"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"
    RECOVERED_BEFORE_OBSERVATION = "recovered_before_observation"
    EXPECTED = "expected"
    NOT_APPLICABLE = "not_applicable"


# ---------------------------------------------------------------------------
# Warning codes — closed-set vocabulary
# ---------------------------------------------------------------------------


class WarningCode(StrEnum):
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
    CONVERGENCE_TAIL = "convergence_tail"
    RESIDUAL_ATTRIBUTION_CONTRADICTION = "residual_attribution_contradiction"


class ResidualAttribution(StrEnum):
    """Where residual deviations from baseline are attributed to.

    Recover Layer-2 judgement contract: recovery propagation cost is NOT
    recovery failure — only fault-attributable residuals justify partial.
    """

    NONE = "none"
    RECOVERY_PROCESS = "recovery_process"
    FAULT_RESIDUAL = "fault_residual"
    MIXED = "mixed"


# ---------------------------------------------------------------------------
# Derived vocabulary accessors (B76 round-14 root-cause fix)
#
# Membership-check forms of the closed sets above. Clamps and boundary
# validation use these; prompt teaching lines and parser regexes iterate
# the enum directly (declaration order = teaching order). A vocabulary
# literal may live ONLY in this file.
# ---------------------------------------------------------------------------


def _vocabulary_set(enum_cls: type[StrEnum]) -> frozenset[str]:
    return frozenset(member.value for member in enum_cls)


INJECT_VERDICT_VALUES = _vocabulary_set(InjectVerdict)
RECOVER_VERDICT_VALUES = _vocabulary_set(RecoverVerdict)
LAYER1_STATUS_VALUES = _vocabulary_set(Layer1Status)
LAYER2_STATUS_VALUES = _vocabulary_set(Layer2Status)
CHECKLIST_STATUS_VALUES = _vocabulary_set(ChecklistItemStatus)
RESIDUAL_ATTRIBUTION_VALUES = _vocabulary_set(ResidualAttribution)

# Ordered keyword tuple for parsing a status keyword out of free-form
# Layer-2 text (the verify and recover layer2 parsers share it). The order
# is load-bearing: the parser scans the tuple in order and the FIRST
# keyword present in the text wins, so a line carrying two status words
# resolves deterministically. The two pre-legislation hand copies had
# already drifted into two different orders ("skipped, partial" vs
# "partial, skipped" — the next hand-copy recurrence after round-14's
# twelve-site non-passed triple).
LAYER2_PARSE_KEYWORDS: tuple[str, ...] = (
    "recovered_before_observation",  # first: longest keyword, a superset of no other
    "passed",
    "failed",
    "skipped",
    "partial",
)

# ---------------------------------------------------------------------------
# Checklist counting policy — semantic subsets of ChecklistItemStatus,
# defined once beside the legislation (round-14 found the non-passed
# triple hand-copied at twelve sites across two files, one copy already
# in a different word order).
#
# Counting is fail-closed: an item counts as non-passed unless its
# status is in the benign set — a closed-set-outside word (or a missing
# status key) is not a pass claim. Known negatives are bucketed
# separately for the inconsistency detector.
# ---------------------------------------------------------------------------

CHECKLIST_NON_PASSED_STATUSES = frozenset({
    ChecklistItemStatus.FAILED.value,
    ChecklistItemStatus.PARTIAL.value,
    ChecklistItemStatus.RECOVERED_BEFORE_OBSERVATION.value,
})

CHECKLIST_BENIGN_STATUSES = frozenset({
    ChecklistItemStatus.PASSED.value,
    ChecklistItemStatus.SKIPPED.value,
    ChecklistItemStatus.EXPECTED.value,
    ChecklistItemStatus.NOT_APPLICABLE.value,
})

# Layer 2 negative-outcome subset — the statuses the deterministic-rule
# override lifts to "passed" when a rule contradicts the LLM's degraded
# conclusion (see _verifier_finalize).
LAYER2_DEGRADED_STATUSES = frozenset({
    Layer2Status.PARTIAL.value,
    Layer2Status.FAILED.value,
    Layer2Status.RECOVERED_BEFORE_OBSERVATION.value,
})

# Verdict semantic subsets (B76 round-15 legislation). The recover-success
# pair and the inject unverified veto were hand-copied as bare tuples at
# three enforcement sites (recover finalize, postmortem builder, memory
# nodes); one copy drifting silently splits the success domain.

# Recover verdicts that license a recovery success claim — full or partial.
RECOVER_SUCCESS_VALUES = frozenset({
    RecoverVerdict.RECOVERED.value,
    RecoverVerdict.PARTIAL.value,
})

# Inject verdicts that unilaterally veto an inject success claim even when
# no layer failed: "unverified" is honest ignorance (the observation channel
# was unavailable), and ignorance is never success.
INJECT_VETO_VALUES = frozenset({
    InjectVerdict.UNVERIFIED.value,
})


# ---------------------------------------------------------------------------
# Failure categories — replaces FailureReason enum in errors.py
# ---------------------------------------------------------------------------


class FailureCategory(StrEnum):
    PLANNING_TIMEOUT = "planning_timeout"
    PLANNING_REJECTED = "planning_rejected"
    SAFETY_REJECTED = "safety_rejected"
    USER_REJECTED = "user_rejected"
    # Unattended CLI declined to auto-approve a widened write-set contract:
    # the settled case's mechanism_writes manifest carries entries beyond
    # the victim target's coverage and no human has seen them. Terminates
    # BEFORE any cluster mutation; the payload carries the entries plus
    # interactive re-run guidance.
    WRITE_SET_BOUNDARY = "write_set_boundary"
    # CLI drift hard-termination: the second target-drift rejection ended
    # the run and NO human was ever consulted (CLI has no interactive drift
    # card). Previously reported as USER_REJECTED — a misattribution: no
    # user rejected anything. Genuine human rejections (confirmation gate,
    # drift card) keep USER_REJECTED.
    DRIFT_TERMINATED = "drift_terminated"
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
    alternatives: str = ""

    def to_reason_string(self) -> str:
        """Legacy-compatible failure_reason string."""
        base = f"{self.category.value}: {self.context}" if self.context else self.category.value
        if self.llm_analysis:
            base = f"{base} | llm_analysis: {self.llm_analysis}"
        if self.alternatives:
            base = f"{base}\n\nViable alternatives:\n{self.alternatives}"
        return base


class ChecklistItem(BaseModel):
    # ``step`` is the skill-case step number (int) for LLM-emitted items;
    # the deterministic-rule layer's dual-source annotation rows carry the
    # sentinel string "rule" (non-int by design — step-coverage validators
    # skip them, and the boundary conversion must keep them).
    step: int | str
    description: str = ""
    status: ChecklistItemStatus
    evidence: str = ""


class Checklist(BaseModel):
    items: list[ChecklistItem] = []
    total_count: int = 0
    skipped_count: int = 0
    non_passed_count: int = 0


class ExperimentEvidence(BaseModel):
    """One experiment's Layer-1 verdict — the plural evidence unit.

    Round-29 root fix. The r26-r28 pluralisation repaired identification
    (alignment), licensing (the birth primitive) and execution (the plural
    poll) — but the EVIDENCE then entered a single-experiment pipeline:
    the r28 poll appended sibling verdicts as strings onto the anchor's
    ``details``/``raw_output``, and the single-value consumers downstream
    (the Layer-2 window renders ``raw_output[:500]`` anchor-first and never
    renders ``details``; the finalize contract field renders the dispatch
    identity's dead anchor) each dropped, starved or mis-rendered the
    evidence in their own way. A sibling's verdict is not a string suffix
    — it is an experiment's verdict, so it gets the same structure the
    anchor always had. The ``is_anchor`` flag keeps the anchor
    addressable inside the plural set (``experiments[0]`` is the anchor
    whenever the list is populated, by construction).
    """

    uid: str = ""
    status: Layer1Status = Layer1Status.UNKNOWN
    details: str = ""
    raw_output: str = ""
    is_anchor: bool = False


class Layer1Result(BaseModel):
    """Layer-1 verdict — anchor projection (legacy fields) + plural face.

    The anchor fields keep their EXACT pre-round-29 semantics: the
    machine verdict of the ANCHOR experiment (the dispatch identity when
    live, else the first surviving liability). Every pre-round-29
    consumer — the recover chain, deterministic rules, contradiction
    gaps, tracker, session records, the Layer-2 context mainline — reads
    them unchanged and stays byte-compatible. ``experiments`` is the
    complete plural record: the anchor entry plus one entry per live
    sibling, each polled, each carrying its own status (a failed sibling
    poll is an honest ``error`` entry — the same honesty standard the
    anchor always had, replacing the swallowed exception). An empty list
    is the single-experiment / legacy-checkpoint shape (model_validate
    tolerates the missing key on old caches).
    """

    status: Layer1Status = Layer1Status.UNKNOWN
    details: str = ""
    raw_output: str = ""
    resource_statuses: list[dict] = []
    affected_count: int = 0
    expired: bool = False
    experiments: list[ExperimentEvidence] = []

    def is_passed(self) -> bool:
        return self.status == Layer1Status.PASSED

    def is_terminal(self) -> bool:
        if self.expired:
            return False
        return self.status in (Layer1Status.FAILED, Layer1Status.ERROR)

    def is_in_progress(self) -> bool:
        """True when Layer 1 ReAct loop is still executing recovery actions.

        Used by ``finalize_recover_verification`` to detect that the LLM
        bypassed the Layer 1 text output (e.g. called
        ``submit_recover_verification`` directly while still in the
        Layer 1 ReAct loop).  In that case the finalize node should
        attempt to recover the Layer 1 result from message history.
        """
        return self.status == Layer1Status.IN_PROGRESS


def layer1_to_dict(result: Layer1Result) -> dict:
    """Convert a Layer1Result to the plain dict used for state storage.

    Canonical home since phase-7 T1 — unifies the recover-side
    ``recover_layer1_to_dict`` (formerly in
    ``providers/chaosblade/recover.py``) and the verify-side
    ``_layer1_to_dict`` (formerly in ``nodes/verify/_verifier_layer1.py``)
    into one address beside the data class, so both chains and every
    provider serialize Layer 1 results identically. mode="json" renders
    the enum status as its plain string value so persistence and
    comparisons never see enum members.
    """
    return result.model_dump(mode="json")


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
    """Recovery verification result (public API surface; the main chain
    stores the dict form and clamps via RECOVER_VERDICT_VALUES).
    """

    # Fail-closed default mirroring the pipeline clamp: a result with no
    # verdict is not a confirmed recovery.
    level: RecoverVerdict = RecoverVerdict.UNRECOVERED
    layer1: Layer1Result = Layer1Result()
    layer2: Layer2Result = Layer2Result()
    checklist: Optional[Checklist] = None
    warnings: list[StructuredWarning] = []
    residual_attribution: Optional[ResidualAttribution] = None

    def add_warning(self, code: WarningCode, detail: str = "") -> None:
        self.warnings.append(StructuredWarning(code=code, detail=detail))

    def has_warning(self, code: WarningCode) -> bool:
        return any(w.code == code for w in self.warnings)
