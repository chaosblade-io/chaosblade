"""Tests for _verifier_layer2_parse.py — Layer 2 verification result parsing."""

import json

import pytest

from chaos_agent.agent.nodes._verifier_layer2_parse import (
    _parse_numeric,
    _metric_alias,
    _pick_metric_by_context,
    _build_truth_deltas,
    _find_contradictions,
    cross_check_evidence,
    _determine_level,
    _detect_checklist_conclusion_inconsistency,
    _parse_verification_result,
    _try_parse_json,
    _count_verification_steps_in_skill_case,
    _split_candidates,
    _validate_step_number_coverage,
)


class TestParseNumeric:
    def test_integer(self):
        assert _parse_numeric("42") == 42.0

    def test_float(self):
        assert _parse_numeric("3.14") == 3.14

    def test_with_percent(self):
        assert _parse_numeric("95%") == 95.0

    def test_with_unit_mi(self):
        assert _parse_numeric("512Mi") == 512.0

    def test_non_numeric(self):
        assert _parse_numeric("OOMKilled") is None

    def test_empty(self):
        assert _parse_numeric("") is None

    def test_none(self):
        assert _parse_numeric(None) is None

    def test_boolean_string(self):
        assert _parse_numeric("True") is None


class TestMetricAlias:
    def test_strips_parenthetical(self):
        assert _metric_alias("Disk usage (overlay)") == "disk usage"

    def test_no_parenthetical(self):
        assert _metric_alias("CPU usage") == "cpu usage"

    def test_multiple_parentheticals(self):
        assert _metric_alias("Mem (RSS) (node)") == "mem"

    def test_lowercases(self):
        assert _metric_alias("RestartCount") == "restartcount"


class TestPickMetricByContext:
    def test_full_name_hit(self):
        truth = {"Disk usage (overlay)": (10.0, 90.0)}
        assert _pick_metric_by_context("disk usage (overlay) increased", truth) == "Disk usage (overlay)"

    def test_alias_hit(self):
        truth = {"Disk usage (overlay)": (10.0, 90.0)}
        assert _pick_metric_by_context("disk usage went up", truth) == "Disk usage (overlay)"

    def test_ambiguous_alias_returns_none(self):
        truth = {
            "Disk usage (overlay)": (10.0, 90.0),
            "Disk usage (nodefs)": (20.0, 80.0),
        }
        assert _pick_metric_by_context("disk usage changed", truth) is None

    def test_no_match(self):
        truth = {"CPU usage": (5.0, 95.0)}
        assert _pick_metric_by_context("memory is fine", truth) is None


class TestBuildTruthDeltas:
    def test_two_iterations(self):
        obs = [
            {"iteration": 0, "metrics": {"CPU": "10%"}},
            {"iteration": 1, "metrics": {"CPU": "90%"}},
        ]
        result = _build_truth_deltas(obs)
        assert result == {"CPU": (10.0, 90.0)}

    def test_single_iteration_skipped(self):
        obs = [{"iteration": 0, "metrics": {"CPU": "50%"}}]
        assert _build_truth_deltas(obs) == {}

    def test_non_numeric_skipped(self):
        obs = [
            {"iteration": 0, "metrics": {"status": "Running"}},
            {"iteration": 1, "metrics": {"status": "Running"}},
        ]
        assert _build_truth_deltas(obs) == {}

    def test_empty_observations(self):
        assert _build_truth_deltas([]) == {}
        assert _build_truth_deltas(None) == {}


class TestFindContradictions:
    def test_contradiction_detected(self):
        evidence = "CPU usage 10 → 90 (significant increase)"
        truth = {"CPU usage": (50.0, 50.0)}
        result = _find_contradictions(evidence, truth, 3)
        assert len(result) == 1
        assert "no change" in result[0]

    def test_no_contradiction_when_truth_has_delta(self):
        evidence = "CPU usage 10 → 90"
        truth = {"CPU usage": (10.0, 90.0)}
        result = _find_contradictions(evidence, truth, 2)
        assert result == []

    def test_llm_claims_no_change_skipped(self):
        evidence = "CPU usage 50 → 50 (stable)"
        truth = {"CPU usage": (50.0, 50.0)}
        result = _find_contradictions(evidence, truth, 2)
        assert result == []

    def test_no_matching_metric(self):
        evidence = "memory 10 → 90"
        truth = {"CPU usage": (50.0, 50.0)}
        result = _find_contradictions(evidence, truth, 2)
        assert result == []


class TestCrossCheckEvidence:
    def test_empty_observations_noop(self):
        result = {"level": "verified", "layer2": {"status": "passed", "details": ""}}
        returned = cross_check_evidence(result, None)
        assert returned["level"] == "verified"

    def test_contradiction_downgrades(self):
        result = {
            "level": "verified",
            "layer2": {"status": "passed", "details": "CPU usage 10 → 90 (big jump)"},
            "warnings": [],
        }
        obs = [
            {"iteration": 0, "metrics": {"CPU usage": "50"}},
            {"iteration": 1, "metrics": {"CPU usage": "50"}},
        ]
        returned = cross_check_evidence(result, obs)
        assert returned["level"] == "partial"
        assert any("contradiction" in w.lower() for w in returned["warnings"])


class TestDetermineLevel:
    @pytest.mark.parametrize("l1, l2, expected", [
        ("passed", "passed", "verified"),
        ("skipped", "passed", "verified"),
        ("passed", "partial", "partial"),
        ("passed", "skipped", "partial"),
        ("skipped", "partial", "partial"),
        ("passed", "failed", "unverified"),
        ("passed", "unknown", "unverified"),
        ("failed", "passed", "unverified"),
        ("passed", "recovered_before_observation", "partial"),
    ])
    def test_level_matrix(self, l1, l2, expected):
        assert _determine_level(l1, l2) == expected


class TestDetectChecklistConclusionInconsistency:
    def test_no_inconsistency_when_all_passed(self):
        items = [{"step": 1, "status": "passed"}, {"step": 2, "status": "passed"}]
        warning, downgrade = _detect_checklist_conclusion_inconsistency(items, "passed")
        assert warning is None
        assert downgrade is False

    def test_inconsistency_with_failed_and_absence(self):
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "failed", "evidence": "disk usage at 2%, no change observed"},
        ]
        warning, downgrade = _detect_checklist_conclusion_inconsistency(items, "passed", "disk usage at 2%, no change observed")
        assert warning is not None
        assert "inconsistency" in warning.lower()
        assert downgrade is True

    def test_no_trigger_when_l2_not_passed(self):
        items = [{"step": 1, "status": "failed"}]
        warning, downgrade = _detect_checklist_conclusion_inconsistency(items, "failed")
        assert warning is None

    def test_only_recovered_before_obs_no_trigger(self):
        items = [{"step": 1, "status": "recovered_before_observation"}]
        warning, downgrade = _detect_checklist_conclusion_inconsistency(items, "passed")
        assert warning is None
        assert downgrade is False


class TestParseVerificationResult:
    def test_basic_verified(self):
        text = (
            "VERIFICATION_RESULT:\n"
            "Layer1: passed\n"
            "Layer2: passed — CPU at 95%\n"
            "Overall: verified\n"
            "BaselineUsed: true\n"
        )
        result = _parse_verification_result(text)
        assert result["level"] == "verified"
        assert result["layer1"]["status"] == "passed"
        assert result["layer2"]["status"] == "passed"
        assert result["baseline_used"] is True

    def test_layer2_failed(self):
        text = "Layer1: passed\nLayer2: failed\nOverall: unverified"
        result = _parse_verification_result(text)
        assert result["level"] == "unverified"
        assert result["layer2"]["status"] == "failed"

    def test_primary_evidence_false_downgrades(self):
        text = (
            "Layer1: passed\nLayer2: passed\n"
            "PrimaryEvidenceObserved: false\n"
            "Overall: verified"
        )
        result = _parse_verification_result(text)
        assert result["level"] == "partial"
        assert any("PrimaryEvidenceObserved" in w for w in result["warnings"])

    def test_no_layer_info_defaults_unknown(self):
        text = "Some random text without structured output"
        result = _parse_verification_result(text)
        assert result["level"] == "unverified"
        assert result["layer1"]["status"] == "unknown"
        assert result["layer2"]["status"] == "unknown"

    def test_layer2_skipped_warning(self):
        text = "Layer1: passed\nLayer2: skipped"
        result = _parse_verification_result(text)
        assert result["layer2"]["status"] == "skipped"
        assert any("skipped" in w.lower() for w in result["warnings"])


class TestTryParseJson:
    def test_valid_json(self):
        data = {
            "layer1": "passed",
            "layer2": "passed",
            "overall": "verified",
            "layer2_details": "CPU confirmed high",
        }
        result = _try_parse_json(json.dumps(data))
        assert result is not None
        assert result["level"] == "verified"
        assert result["layer2"]["status"] == "passed"

    def test_invalid_json(self):
        assert _try_parse_json("not json at all") is None

    def test_missing_required_fields(self):
        assert _try_parse_json(json.dumps({"foo": "bar"})) is None

    def test_invalid_l2_status(self):
        data = {"layer1": "passed", "layer2": "maybe", "overall": "verified"}
        assert _try_parse_json(json.dumps(data)) is None

    def test_checklist_inconsistency_downgrade(self):
        data = {
            "layer1": "passed",
            "layer2": "passed",
            "overall": "verified",
            "verification_checklist": [
                {"step": 1, "status": "passed"},
                {"step": 2, "status": "failed", "evidence": "no change observed, at 2%"},
            ],
        }
        result = _try_parse_json(json.dumps(data))
        assert result is not None
        assert result["layer2"]["status"] == "partial"
        assert result["level"] == "partial"


class TestCountVerificationSteps:
    def test_numbered_steps(self):
        content = (
            "**注入验证**：\n"
            "1. 检查 CPU 使用率\n"
            "2. 进入容器查看进程\n"
            "3. 确认 top 输出\n"
            "**恢复验证**：\n"
        )
        assert _count_verification_steps_in_skill_case(content) == 3

    def test_bullet_fallback(self):
        content = (
            "**注入验证**：\n"
            "- 检查 CPU\n"
            "- 检查内存\n"
            "**恢复验证**：\n"
        )
        assert _count_verification_steps_in_skill_case(content) == 2

    def test_no_section(self):
        assert _count_verification_steps_in_skill_case("no verification section here") == 0


class TestSplitCandidates:
    def test_single_candidate(self):
        assert _split_candidates("just one candidate") == ["just one candidate"]

    def test_multiple_candidates(self):
        content = (
            "--- Candidate 1: foo ---\n"
            "body one\n"
            "--- Candidate 2: bar ---\n"
            "body two\n"
        )
        result = _split_candidates(content)
        assert len(result) == 2
        assert "body one" in result[0]
        assert "body two" in result[1]


class TestValidateStepNumberCoverage:
    def test_missing_steps_detected(self):
        skill_case = (
            "**注入验证**：\n"
            "1. 检查 CPU\n"
            "2. 检查磁盘\n"
            "3. 确认进程\n"
        )
        checklist = [
            {"step": 1, "status": "passed"},
            {"step": 3, "status": "passed"},
        ]
        missing, deviated = _validate_step_number_coverage(skill_case, checklist)
        assert missing == [2]
        assert deviated == []

    def test_deviated_step_detected(self):
        skill_case = "**注入验证**：\n1. 检查 CPU\n"
        checklist = [
            {"step": 1, "status": "passed", "evidence": "deviation: used top instead of metrics API"},
        ]
        missing, deviated = _validate_step_number_coverage(skill_case, checklist)
        assert missing == []
        assert deviated == [1]

    def test_no_skill_case_section(self):
        missing, deviated = _validate_step_number_coverage("no section", [])
        assert missing == []
        assert deviated == []
