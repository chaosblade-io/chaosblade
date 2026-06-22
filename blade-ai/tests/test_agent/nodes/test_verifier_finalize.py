"""Tests for _verifier_finalize.py — finalize verification pure functions."""

import pytest

from chaos_agent.agent.nodes._verifier_finalize import (
    _overall_to_level,
    _verification_from_submit_args,
    _format_verification_detail,
)
from chaos_agent.agent.verdict import Layer1Result


class TestOverallToLevel:
    @pytest.mark.parametrize("overall, expected", [
        ("verified", "verified"),
        ("partial", "partial"),
        ("unverified", "unverified"),
        ("garbage", "unverified"),
        ("", "unverified"),
    ])
    def test_mapping(self, overall, expected):
        assert _overall_to_level(overall) == expected


class TestVerificationFromSubmitArgs:
    def test_basic_verified(self):
        args = {
            "overall": "verified",
            "layer2_status": "passed",
            "layer2_details": "CPU confirmed at 95%",
            "primary_evidence_observed": True,
            "baseline_used": True,
        }
        result = _verification_from_submit_args(args)
        assert result["level"] == "verified"
        assert result["layer2"]["status"] == "passed"
        assert result["layer2"]["details"] == "CPU confirmed at 95%"
        assert result["primary_evidence_observed"] is True
        assert result["baseline_used"] is True

    def test_primary_evidence_false_downgrades(self):
        args = {
            "overall": "verified",
            "layer2_status": "passed",
            "primary_evidence_observed": False,
        }
        result = _verification_from_submit_args(args)
        assert result["level"] == "partial"
        assert any("PrimaryEvidenceObserved" in w for w in result["warnings"])

    def test_layer2_failed_blocks_verified(self):
        args = {
            "overall": "verified",
            "layer2_status": "failed",
            "primary_evidence_observed": True,
        }
        result = _verification_from_submit_args(args)
        assert result["level"] == "unverified"
        assert any("Layer2='failed'" in w for w in result["warnings"])

    def test_layer2_partial_forces_partial(self):
        args = {
            "overall": "verified",
            "layer2_status": "partial",
        }
        result = _verification_from_submit_args(args)
        assert result["level"] == "partial"

    def test_checklist_with_inconsistency(self):
        args = {
            "overall": "verified",
            "layer2_status": "passed",
            "primary_evidence_observed": True,
            "checklist": [
                {"step": 1, "status": "passed"},
                {"step": 2, "status": "failed", "evidence": "no change, at 2%"},
            ],
        }
        result = _verification_from_submit_args(args)
        assert result["layer2"]["status"] == "partial"
        assert result["level"] == "partial"

    def test_invalid_overall_defaults_unverified(self):
        args = {"overall": "maybe", "layer2_status": "passed"}
        result = _verification_from_submit_args(args)
        assert result["level"] == "unverified"

    def test_non_list_checklist_ignored(self):
        args = {
            "overall": "verified",
            "layer2_status": "passed",
            "primary_evidence_observed": True,
            "checklist": "not a list",
        }
        result = _verification_from_submit_args(args)
        assert "checklist" not in result

    def test_non_dict_checklist_items_filtered(self):
        args = {
            "overall": "verified",
            "layer2_status": "passed",
            "primary_evidence_observed": True,
            "checklist": ["string item", {"step": 1, "status": "passed"}],
        }
        result = _verification_from_submit_args(args)
        assert result["checklist"]["total_count"] == 1


class TestFormatVerificationDetail:
    def test_basic_format(self):
        verification = {
            "level": "verified",
            "layer2": {"status": "passed", "details": "CPU at 95%"},
            "checklist": {"items": [
                {"step": 1, "status": "passed", "evidence": "CPU confirmed"},
            ]},
            "warnings": [],
        }
        layer1 = Layer1Result(status="passed", details="blade_status: Running")
        text = _format_verification_detail(verification, layer1)
        assert "verified" in text.lower()
        assert "Layer1:" in text
        assert "Layer2: passed" in text

    def test_with_warnings(self):
        verification = {
            "level": "partial",
            "layer2": {"status": "partial", "details": ""},
            "warnings": ["Some important warning"],
        }
        layer1 = Layer1Result(status="passed", details="")
        text = _format_verification_detail(verification, layer1)
        assert "Some important warning" in text

    def test_no_checklist(self):
        verification = {
            "level": "unverified",
            "layer2": {"status": "failed", "details": "no effect"},
            "warnings": [],
        }
        layer1 = Layer1Result(status="passed", details="")
        text = _format_verification_detail(verification, layer1)
        assert "unverified" in text.lower()
