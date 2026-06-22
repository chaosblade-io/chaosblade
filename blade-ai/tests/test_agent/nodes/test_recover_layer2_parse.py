"""Tests for _recover_layer2_parse.py — recovery verification parsing."""

import pytest

from chaos_agent.agent.nodes._recover_layer2_parse import (
    _detect_recovery_contradiction,
    _detect_recovery_checklist_inconsistency,
    _parse_recovery_verification_result,
    _detect_primary_evidence_generic_contradiction,
    _count_recovery_steps_in_skill_case,
    _extract_recovery_verification_section,
)


class TestDetectRecoveryContradiction:
    def test_text_contradiction(self):
        warning = _detect_recovery_contradiction("cpu usage normal, cpu back to normal")
        assert warning is not None
        assert "Contradiction" in warning

    def test_no_contradiction_with_absence(self):
        warning = _detect_recovery_contradiction("still elevated, cpu remains high")
        assert warning is None

    def test_checklist_all_passed_contradiction(self):
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "passed"},
        ]
        warning = _detect_recovery_contradiction("some details", items)
        assert warning is not None
        assert "ALL checklist steps" in warning

    def test_checklist_not_all_passed_no_contradiction(self):
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "failed"},
        ]
        warning = _detect_recovery_contradiction("some details", items)
        assert warning is None

    def test_empty_inputs(self):
        assert _detect_recovery_contradiction("", None) is None

    def test_absence_phrase_blocks_checklist_check(self):
        items = [{"step": 1, "status": "passed"}]
        warning = _detect_recovery_contradiction("still elevated, remains high", items)
        assert warning is None


class TestDetectRecoveryChecklistInconsistency:
    def test_inconsistency_detected(self):
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "skipped"},
        ]
        warning = _detect_recovery_checklist_inconsistency(items, "passed")
        assert warning is not None
        assert "inconsistency" in warning.lower()

    def test_no_inconsistency_all_passed(self):
        items = [{"step": 1, "status": "passed"}]
        assert _detect_recovery_checklist_inconsistency(items, "passed") is None

    def test_no_trigger_when_l2_not_passed(self):
        items = [{"step": 1, "status": "failed"}]
        assert _detect_recovery_checklist_inconsistency(items, "failed") is None

    def test_empty_checklist(self):
        assert _detect_recovery_checklist_inconsistency([], "passed") is None

    def test_failed_item_triggers(self):
        items = [{"step": 1, "status": "failed"}]
        warning = _detect_recovery_checklist_inconsistency(items, "passed")
        assert warning is not None


class TestParseRecoveryVerificationResult:
    def test_basic_recovered(self):
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "Layer2: passed — disk usage back to normal\n"
            "Overall: recovered\n"
            "BaselineUsed: true\n"
        )
        result = _parse_recovery_verification_result(text)
        assert result["level"] == "recovered"
        assert result["layer2"]["status"] == "passed"
        assert result["baseline_used"] is True

    def test_layer2_failed(self):
        text = "Layer2: failed\nOverall: unrecovered"
        result = _parse_recovery_verification_result(text)
        assert result["level"] == "unrecovered"
        assert result["layer2"]["status"] == "failed"

    def test_wrong_format_success(self):
        text = "RECOVERY_EXECUTION_RESULT:\nStatus: success"
        result = _parse_recovery_verification_result(text)
        assert result["level"] == "recovered"
        assert result["layer2"]["status"] == "passed"
        assert any("RECOVERY_EXECUTION_RESULT" in w for w in result["warnings"])

    def test_wrong_format_non_success(self):
        text = "RECOVERY_EXECUTION_RESULT:\nStatus: failed"
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "failed"

    def test_primary_evidence_false_downgrades(self):
        text = (
            "Layer2: passed\n"
            "PrimaryEvidenceObserved: false\n"
            "Overall: recovered"
        )
        result = _parse_recovery_verification_result(text)
        assert result["level"] == "partial"
        assert any("PrimaryEvidenceObserved" in w for w in result["warnings"])

    def test_contradiction_overrides_to_partial(self):
        text = "Layer2: failed — cpu usage normal, cpu back to normal\nOverall: unrecovered"
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "partial"
        assert result["level"] == "partial"

    def test_layer2_unknown_warning(self):
        text = "some random text"
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "unknown"
        assert any("unknown" in w.lower() for w in result["warnings"])

    def test_no_checklist_warning(self):
        text = "Layer2: passed\nOverall: recovered"
        result = _parse_recovery_verification_result(text)
        assert any("No Recovery Verification Checklist" in w for w in result["warnings"])

    def test_l2_partial_overrides_level(self):
        text = (
            "RECOVERY_VERIFICATION_CHECKLIST:\n"
            "Step 1: passed\n"
            "Step 2: skipped\n"
            "RECOVERY_VERIFICATION_RESULT:\n"
            "Layer2: passed\n"
            "Overall: recovered"
        )
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "partial"
        assert result["level"] == "partial"


class TestDetectPrimaryEvidenceGenericContradiction:
    def test_generic_only_triggers(self):
        warning = _detect_primary_evidence_generic_contradiction(
            True, "pod running, no new restarts, healthy",
        )
        assert warning is not None
        assert "generic" in warning.lower()

    def test_fault_specific_no_trigger(self):
        warning = _detect_primary_evidence_generic_contradiction(
            True, "cpu usage back to baseline, pod running",
        )
        assert warning is None

    def test_primary_false_no_trigger(self):
        warning = _detect_primary_evidence_generic_contradiction(
            False, "pod running, no restarts",
        )
        assert warning is None

    def test_pod_kill_exemption(self):
        warning = _detect_primary_evidence_generic_contradiction(
            True, "pod running, no new restarts",
            skill_name="pod-kill",
        )
        assert warning is None

    def test_no_indicators_no_trigger(self):
        warning = _detect_primary_evidence_generic_contradiction(
            True, "something ambiguous happened",
        )
        assert warning is None


class TestCountRecoverySteps:
    def test_numbered_steps(self):
        content = (
            "**恢复验证**：\n"
            "1. 确认 CPU 恢复\n"
            "2. 确认磁盘恢复\n"
            "**其他**：\n"
        )
        assert _count_recovery_steps_in_skill_case(content) == 2

    def test_bullet_fallback(self):
        content = (
            "**恢复验证**：\n"
            "- CPU 检查\n"
            "- 磁盘检查\n"
        )
        assert _count_recovery_steps_in_skill_case(content) == 2

    def test_no_section(self):
        assert _count_recovery_steps_in_skill_case("no recovery section") == 0


class TestExtractRecoveryVerificationSection:
    def test_basic_extraction(self):
        content = (
            "**注入验证**：\n检查CPU\n"
            "**恢复验证**：\n确认恢复正常\n"
            "**注意事项**：\n小心操作\n"
        )
        result = _extract_recovery_verification_section(content)
        assert "恢复验证" in result
        assert "确认恢复正常" in result
        assert "注意事项" not in result

    def test_cross_reference(self):
        content = (
            "**注入验证**：\n1. 检查 CPU\n2. 检查磁盘\n"
            "**恢复验证**：\n同注入验证步骤，确认恢复\n"
            "**其他**：\n"
        )
        result = _extract_recovery_verification_section(content)
        assert "注入验证参考" in result
        assert "检查 CPU" in result

    def test_no_section(self):
        assert _extract_recovery_verification_section("no recovery here") == ""
