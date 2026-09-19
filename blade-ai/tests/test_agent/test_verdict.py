"""Tests for E3 — verdict enums and Pydantic models."""

from __future__ import annotations

import json

import pytest

from chaos_agent.agent.result.verdict import (
    ChecklistItem,
    ChecklistItemStatus,
    Checklist,
    FailureCategory,
    FailureDetail,
    InjectVerdict,
    Layer1Result,
    Layer1Status,
    Layer2Result,
    Layer2Status,
    RecoverVerdict,
    RecoverVerificationResult,
    ResidualAttribution,
    StructuredWarning,
    VerificationResult,
    WarningCode,
)


# ---------------------------------------------------------------------------
# Enum completeness
# ---------------------------------------------------------------------------


class TestEnumValues:
    def test_inject_verdict_values(self):
        assert set(v.value for v in InjectVerdict) == {"verified", "partial", "unverified"}

    def test_recover_verdict_values(self):
        # B76 round-14 F1: the closure is the four words the pipeline
        # actually produces ("failed" was unreachable; the clamp maps it
        # to "unrecovered"). See test_verdict_vocabulary_single_source.py
        # for the enum==clamp==prompt==regex reconciliation pins.
        assert set(v.value for v in RecoverVerdict) == {
            "recovered", "partial", "unverified", "unrecovered"
        }

    def test_layer1_status_values(self):
        assert set(v.value for v in Layer1Status) == {"passed", "failed", "error", "skipped", "unknown", "warning", "in_progress"}

    def test_layer2_status_values(self):
        assert set(v.value for v in Layer2Status) == {
            "passed", "partial", "failed", "skipped",
            "recovered_before_observation", "unknown",
        }

    def test_checklist_item_status_values(self):
        assert set(v.value for v in ChecklistItemStatus) == {
            "passed", "partial", "failed", "skipped",
            "recovered_before_observation",
            "expected", "not_applicable",
        }

    def test_warning_code_count(self):
        assert len(WarningCode) == 15

    def test_failure_category_matches_old_failure_reason(self):
        expected = {
            "planning_timeout", "planning_rejected",
            "safety_rejected", "user_rejected",
            "prerequisite_failed", "execution_failed", "execution_timeout",
            "replan_exhausted", "verification_failed", "recovery_failed",
            "recovery_verification_timeout", "internal_error",
            "wall_clock_timeout",
            # write-set-approval-contract: the unattended widened-contract
            # early exit and the honest CLI drift termination (B12).
            "write_set_boundary", "drift_terminated",
        }
        assert set(v.value for v in FailureCategory) == expected


# ---------------------------------------------------------------------------
# str, Enum mixin — JSON transparency
# ---------------------------------------------------------------------------


class TestStrEnumMixin:
    def test_inject_verdict_json_transparent(self):
        assert json.dumps(InjectVerdict.VERIFIED) == '"verified"'

    def test_failure_category_value_is_string(self):
        assert FailureCategory.WALL_CLOCK_TIMEOUT.value == "wall_clock_timeout"
        assert str(FailureCategory.WALL_CLOCK_TIMEOUT.value) == "wall_clock_timeout"

    def test_warning_code_equality_with_str(self):
        assert WarningCode.LAYER2_SKIPPED == "layer2_skipped"

    def test_layer2_status_from_string(self):
        assert Layer2Status("passed") == Layer2Status.PASSED


# ---------------------------------------------------------------------------
# Rendering form (B76 round-13 Q1) — the display half of the str-Enum trap
# ---------------------------------------------------------------------------


class TestEnumRenderingForm:
    """Every rendering channel must yield the VALUE, never the class name.

    Under the legacy ``(str, Enum)`` base on Python 3.11+, f-strings
    rendered ``Layer1Status.FAILED`` (not ``failed``) into 38 sites —
    including the failure_reason contract field — while every equality
    check kept passing, which is exactly how the defect survived six
    ledger-focused review rounds. The verdict enums are now ``StrEnum``;
    these pins hold the line against any regression of that base.
    """

    ENUM_FAMILIES = (
        InjectVerdict,
        RecoverVerdict,
        Layer1Status,
        Layer2Status,
        ChecklistItemStatus,
        WarningCode,
        ResidualAttribution,
        FailureCategory,
    )

    def test_every_member_renders_value_on_all_channels(self):
        """All 8 families × all members × f-string/str()/format()."""
        for family in self.ENUM_FAMILIES:
            for member in family:
                assert f"{member}" == member.value, (
                    f"{family.__name__}.{member.name}: f-string renders class name"
                )
                assert str(member) == member.value
                assert format(member) == member.value

    def test_equality_with_plain_str_survives(self):
        """The fix is display-only: comparison semantics unchanged."""
        assert Layer1Status.FAILED == "failed"
        assert FailureCategory.VERIFICATION_FAILED == "verification_failed"
        assert Layer1Status("warning") is Layer1Status.WARNING

    def test_contract_field_rendering_end_to_end(self):
        """The verifier.py L288 shape: a Layer1Result object interpolated
        into the failure-detail f-string must read ``Layer1=failed`` —
        this exact expression is what flows into the failure_reason
        task-JSON contract field (pre-fix: ``Layer1=Layer1Status.FAILED``)."""
        layer1 = Layer1Result(status=Layer1Status.FAILED, details="mock")
        detail = f"Layer1={layer1.status}, Layer2=skipped, details={layer1.details[:200]}"
        assert detail == "Layer1=failed, Layer2=skipped, details=mock"

    def test_recovery_status_message_rendering(self):
        """The _recover_finalize.py L406 shape: the user-visible status
        message renders the cache-restored (pydantic-coerced) enum member
        as its plain value."""
        restored = Layer1Result.model_validate(
            {"status": "failed", "details": "from cache", "raw_output": ""}
        )
        assert isinstance(restored.status, Layer1Status)  # coerced back to enum
        msg = f"Recovery verification: recovered (Layer1: {restored.status}, Layer2: passed)"
        assert msg == (
            "Recovery verification: recovered (Layer1: failed, Layer2: passed)"
        )


# ---------------------------------------------------------------------------
# StructuredWarning
# ---------------------------------------------------------------------------


class TestStructuredWarning:
    def test_basic_construction(self):
        w = StructuredWarning(code=WarningCode.EXPERIMENT_EXPIRED, detail="timeout=30s")
        assert w.code == WarningCode.EXPERIMENT_EXPIRED
        assert w.detail == "timeout=30s"

    def test_detail_defaults_empty(self):
        w = StructuredWarning(code=WarningCode.LAYER2_SKIPPED)
        assert w.detail == ""

    def test_model_dump_shape(self):
        w = StructuredWarning(code=WarningCode.CROSS_CHECK_CONTRADICTION, detail="RestartCount")
        d = w.model_dump()
        assert d == {"code": "cross_check_contradiction", "detail": "RestartCount"}


# ---------------------------------------------------------------------------
# FailureDetail
# ---------------------------------------------------------------------------


class TestFailureDetail:
    def test_basic_construction(self):
        fd = FailureDetail(
            category=FailureCategory.EXECUTION_TIMEOUT,
            context="max_iterations=15",
            llm_analysis="Agent failed to find target pod",
        )
        assert fd.category == FailureCategory.EXECUTION_TIMEOUT
        assert fd.context == "max_iterations=15"

    def test_to_reason_string_full(self):
        fd = FailureDetail(
            category=FailureCategory.REPLAN_EXHAUSTED,
            context="attempts=3",
            llm_analysis="Pod not found",
        )
        assert fd.to_reason_string() == "replan_exhausted: attempts=3 | llm_analysis: Pod not found"

    def test_to_reason_string_no_analysis(self):
        fd = FailureDetail(
            category=FailureCategory.SAFETY_REJECTED,
            context="namespace=kube-system",
        )
        assert fd.to_reason_string() == "safety_rejected: namespace=kube-system"

    def test_to_reason_string_no_context(self):
        fd = FailureDetail(category=FailureCategory.INTERNAL_ERROR)
        assert fd.to_reason_string() == "internal_error"

    def test_model_dump_shape(self):
        fd = FailureDetail(
            category=FailureCategory.WALL_CLOCK_TIMEOUT,
            context="budget=300s",
        )
        d = fd.model_dump()
        assert d == {
            "category": "wall_clock_timeout",
            "context": "budget=300s",
            "llm_analysis": "",
            "alternatives": "",
        }


# ---------------------------------------------------------------------------
# ChecklistItem / Checklist
# ---------------------------------------------------------------------------


class TestChecklist:
    def test_checklist_item(self):
        item = ChecklistItem(step=1, status=ChecklistItemStatus.PASSED, evidence="ok")
        assert item.step == 1
        assert item.status == ChecklistItemStatus.PASSED

    def test_checklist_model(self):
        items = [
            ChecklistItem(step=1, status=ChecklistItemStatus.PASSED),
            ChecklistItem(step=2, status=ChecklistItemStatus.SKIPPED),
        ]
        cl = Checklist(items=items, total_count=2, skipped_count=1, non_passed_count=0)
        assert cl.total_count == 2
        assert cl.skipped_count == 1

    def test_checklist_dump_roundtrip(self):
        cl = Checklist(
            items=[ChecklistItem(step=1, status=ChecklistItemStatus.FAILED, evidence="disk ok")],
            total_count=1,
            non_passed_count=1,
        )
        raw = cl.model_dump()
        restored = Checklist.model_validate(raw)
        assert restored.items[0].status == ChecklistItemStatus.FAILED


# ---------------------------------------------------------------------------
# VerificationResult
# ---------------------------------------------------------------------------


class TestVerificationResult:
    def test_default_construction(self):
        vr = VerificationResult()
        assert vr.level == InjectVerdict.UNVERIFIED
        assert vr.layer1.status == Layer1Status.UNKNOWN
        assert vr.layer2.status == Layer2Status.UNKNOWN
        assert vr.warnings == []
        assert vr.checklist is None

    def test_add_warning(self):
        vr = VerificationResult()
        vr.add_warning(WarningCode.LAYER2_SKIPPED, "no LLM available")
        assert len(vr.warnings) == 1
        assert vr.warnings[0].code == WarningCode.LAYER2_SKIPPED
        assert vr.warnings[0].detail == "no LLM available"

    def test_has_warning(self):
        vr = VerificationResult()
        vr.add_warning(WarningCode.EXPERIMENT_EXPIRED)
        assert vr.has_warning(WarningCode.EXPERIMENT_EXPIRED)
        assert not vr.has_warning(WarningCode.LAYER2_SKIPPED)

    def test_full_construction(self):
        vr = VerificationResult(
            level=InjectVerdict.VERIFIED,
            layer1=Layer1Result(status=Layer1Status.PASSED, details="Running"),
            layer2=Layer2Result(status=Layer2Status.PASSED, details="disk at 85%"),
            checklist=Checklist(
                items=[ChecklistItem(step=1, status=ChecklistItemStatus.PASSED)],
                total_count=1,
            ),
            baseline_used=True,
            primary_evidence_observed=True,
        )
        assert vr.level == InjectVerdict.VERIFIED
        assert vr.layer1.status == Layer1Status.PASSED
        assert vr.checklist.total_count == 1

    def test_model_dump_json_shape(self):
        vr = VerificationResult(
            level=InjectVerdict.PARTIAL,
            layer1=Layer1Result(status=Layer1Status.PASSED),
            layer2=Layer2Result(status=Layer2Status.PARTIAL, details="incomplete"),
            warnings=[StructuredWarning(code=WarningCode.COVERAGE_INCOMPLETE, detail="1/3 steps")],
        )
        d = vr.model_dump()
        assert d["level"] == "partial"
        assert d["layer1"]["status"] == "passed"
        assert d["layer2"]["status"] == "partial"
        assert d["warnings"][0]["code"] == "coverage_incomplete"

    def test_model_validate_roundtrip(self):
        vr = VerificationResult(
            level=InjectVerdict.VERIFIED,
            layer1=Layer1Result(status=Layer1Status.PASSED),
            layer2=Layer2Result(status=Layer2Status.PASSED),
        )
        vr.add_warning(WarningCode.BASELINE_AVAILABLE_NOT_USED, "confidence=high")
        raw = vr.model_dump()
        restored = VerificationResult.model_validate(raw)
        assert restored.level == InjectVerdict.VERIFIED
        assert restored.has_warning(WarningCode.BASELINE_AVAILABLE_NOT_USED)

    def test_json_serialization(self):
        vr = VerificationResult(level=InjectVerdict.VERIFIED)
        s = vr.model_dump_json()
        parsed = json.loads(s)
        assert parsed["level"] == "verified"
        assert parsed["warnings"] == []


# ---------------------------------------------------------------------------
# RecoverVerificationResult
# ---------------------------------------------------------------------------


class TestRecoverVerificationResult:
    def test_default_level_is_unrecovered(self):
        # B76 round-14: fail-closed default mirroring the pipeline clamp
        # (a result with no verdict is not a confirmed recovery).
        rvr = RecoverVerificationResult()
        assert rvr.level == RecoverVerdict.UNRECOVERED

    def test_recovered_construction(self):
        rvr = RecoverVerificationResult(
            level=RecoverVerdict.RECOVERED,
            layer1=Layer1Result(status=Layer1Status.PASSED),
            layer2=Layer2Result(status=Layer2Status.PASSED),
        )
        assert rvr.level == RecoverVerdict.RECOVERED

    def test_add_warning(self):
        rvr = RecoverVerificationResult()
        rvr.add_warning(WarningCode.LAYER2_SKIPPED)
        assert rvr.has_warning(WarningCode.LAYER2_SKIPPED)

    def test_model_dump_shape(self):
        rvr = RecoverVerificationResult(level=RecoverVerdict.PARTIAL)
        d = rvr.model_dump()
        assert d["level"] == "partial"
        assert d["layer1"]["status"] == "unknown"


# ---------------------------------------------------------------------------
# Cross-type integration
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_verification_result_as_state_dict(self):
        """VerificationResult.model_dump() produces a dict compatible with
        AgentState's verification: Optional[dict] field."""
        vr = VerificationResult(
            level=InjectVerdict.VERIFIED,
            layer1=Layer1Result(status=Layer1Status.PASSED, details="Running"),
            layer2=Layer2Result(status=Layer2Status.PASSED, details="confirmed"),
            side_effects={"container_restarts": [{"pod": "x", "restart_count": 1}]},
        )
        d = vr.model_dump()
        assert isinstance(d, dict)
        assert d["level"] == "verified"
        assert d["layer1"]["status"] == "passed"
        assert d["side_effects"]["container_restarts"][0]["pod"] == "x"

    def test_failure_detail_as_state_dict(self):
        """FailureDetail.model_dump() produces a dict compatible with
        AgentState's failure_detail: Optional[dict] field."""
        fd = FailureDetail(
            category=FailureCategory.EXECUTION_TIMEOUT,
            context="max_iterations=15",
            llm_analysis="Could not reach pod",
        )
        d = fd.model_dump()
        assert isinstance(d, dict)
        assert d["category"] == "execution_timeout"

    @pytest.mark.parametrize("code", list(WarningCode))
    def test_all_warning_codes_serializable(self, code: WarningCode):
        w = StructuredWarning(code=code, detail="test")
        d = w.model_dump()
        restored = StructuredWarning.model_validate(d)
        assert restored.code == code


# ---------------------------------------------------------------------------
# dict_to_verification_result conversion
# ---------------------------------------------------------------------------


class TestDictToVerificationResult:
    def test_basic_roundtrip(self):
        from chaos_agent.agent.nodes.verify._verifier_layer2_parse import (
            _parse_verification_result,
            dict_to_verification_result,
        )

        text = (
            "Layer1: passed - experiment Running\n"
            "Layer2: passed - disk usage confirmed at 85%\n"
            "Verification Checklist:\n"
            "Step 1: passed — disk fill confirmed\n"
            "Overall: verified\n"
            "BaselineUsed: true\n"
            "PrimaryEvidenceObserved: true\n"
        )
        raw = _parse_verification_result(text)
        vr = dict_to_verification_result(raw)

        assert vr.level == InjectVerdict.VERIFIED
        assert vr.layer1.status == Layer1Status.PASSED
        assert vr.layer2.status == Layer2Status.PASSED
        assert vr.checklist is not None
        assert vr.checklist.items[0].status == ChecklistItemStatus.PASSED
        assert vr.baseline_used is True

    def test_warnings_classified(self):
        from chaos_agent.agent.nodes.verify._verifier_layer2_parse import (
            dict_to_verification_result,
        )

        raw = {
            "level": "partial",
            "layer1": {"status": "passed"},
            "layer2": {"status": "partial"},
            "warnings": [
                "Layer 2 (fault-specific) verification was skipped. Only Layer 1 (programmatic) verification was performed.",
                "No Verification Checklist detected in LLM output.",
            ],
        }
        vr = dict_to_verification_result(raw)
        assert vr.has_warning(WarningCode.LAYER2_SKIPPED)
        assert vr.has_warning(WarningCode.NO_CHECKLIST_DETECTED)

    def test_empty_dict(self):
        from chaos_agent.agent.nodes.verify._verifier_layer2_parse import (
            dict_to_verification_result,
        )

        vr = dict_to_verification_result({})
        assert vr.level == InjectVerdict.UNVERIFIED
        assert vr.layer1.status == Layer1Status.UNKNOWN
        assert vr.warnings == []

    def test_model_dump_produces_state_compatible_dict(self):
        from chaos_agent.agent.nodes.verify._verifier_layer2_parse import (
            _parse_verification_result,
            dict_to_verification_result,
        )

        text = "Layer1: passed\nLayer2: failed\nOverall: unverified\n"
        raw = _parse_verification_result(text)
        vr = dict_to_verification_result(raw)
        d = vr.model_dump()
        assert d["level"] == "unverified"
        assert d["layer2"]["status"] == "failed"
        assert isinstance(d["warnings"], list)
