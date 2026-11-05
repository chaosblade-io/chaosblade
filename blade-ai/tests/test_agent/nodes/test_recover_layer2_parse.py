"""Tests for _recover_layer2_parse.py — recovery verification parsing."""


from chaos_agent.agent.nodes.recover._recover_layer2_parse import (
    _detect_recovery_contradiction,
    _detect_recovery_checklist_inconsistency,
    _parse_recovery_verification_result,
    _count_recovery_steps_in_skill_case,
    _extract_recovery_verification_section,
)


class TestDetectRecoveryContradiction:
    """Structural-only contradiction detection (Phase 2c / D14).

    The program compares ChecklistItemStatus enums only; it no longer
    reverse-infers "recovery looks effective" from free-text l2_details.
    """

    def test_all_passed_checklist_contradicts_failed(self):
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "passed"},
        ]
        warning = _detect_recovery_contradiction(items)
        assert warning is not None
        assert "ALL checklist steps" in warning

    def test_not_all_passed_no_contradiction(self):
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "failed"},
        ]
        assert _detect_recovery_contradiction(items) is None

    def test_empty_checklist_no_contradiction(self):
        assert _detect_recovery_contradiction(None) is None
        assert _detect_recovery_contradiction([]) is None

    def test_non_dict_items_ignored(self):
        # A structural comparison guards against non-dict items; only 'passed'
        # dicts count, so a mixed junk list is not "all passed".
        assert _detect_recovery_contradiction(["passed", {"status": "passed"}]) is None


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

    def test_free_text_recovery_indicators_no_longer_override(self):
        # Phase 2c / D14: the program no longer reverse-infers a contradiction
        # from free-text l2_details ("cpu usage normal"). With no checklist to
        # compare structurally, layer2 'failed' is authoritative and stays.
        text = "Layer2: failed — cpu usage normal, cpu back to normal\nOverall: unrecovered"
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "failed"
        assert result["level"] == "unrecovered"

    def test_structural_all_passed_checklist_overrides_failed(self):
        # Retained structural path: layer2 'failed' but ALL checklist steps
        # 'passed' is an enum-level self-contradiction → downgrade to partial.
        text = (
            "RECOVERY_VERIFICATION_CHECKLIST:\n"
            "Step 1: passed\n"
            "Step 2: passed\n"
            "RECOVERY_VERIFICATION_RESULT:\n"
            "Layer2: failed\n"
            "Overall: unrecovered"
        )
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


class TestGenericPrimaryEvidenceRetired:
    """Phase 2c / D14: generic-primary-evidence text detection is retired.

    Whether observed evidence is fault-specific vs generic health is the LLM's
    judgement (carried by primary_evidence_observed). The program no longer
    reverse-infers it from l2_details keyword matching, so a 'recovered' verdict
    with primary_evidence_observed=true is NOT downgraded on generic prose.
    """

    def test_generic_prose_no_longer_downgrades(self):
        text = (
            "Layer2: passed — pod running, no new restarts, healthy\n"
            "PrimaryEvidenceObserved: true\n"
            "Overall: recovered"
        )
        result = _parse_recovery_verification_result(text)
        # The structural gate only downgrades when the flag is FALSE; the
        # generic-prose downgrade is retired, so 'recovered' stands.
        assert result["level"] == "recovered"

    def test_structural_false_flag_still_downgrades(self):
        # Retained structural gate: recovered + primary_evidence_observed=false.
        text = (
            "Layer2: passed — pod running\n"
            "PrimaryEvidenceObserved: false\n"
            "Overall: recovered"
        )
        result = _parse_recovery_verification_result(text)
        assert result["level"] == "partial"


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
        assert "Injection-verification reference" in result
        assert "检查 CPU" in result

    def test_no_section(self):
        assert _extract_recovery_verification_section("no recovery here") == ""
