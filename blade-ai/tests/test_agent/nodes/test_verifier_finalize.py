"""Tests for _verifier_finalize.py — finalize verification pure functions."""

from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.nodes._verifier_finalize import (
    _overall_to_level,
    _verification_from_submit_args,
    _format_verification_detail,
    _build_verify_replan_context,
    _cleanup_residuals,
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


class TestBuildVerifyReplanContext:
    """Tests for _build_verify_replan_context."""

    def test_basic_context(self):
        verification = {
            "level": "unverified",
            "layer1": {"status": "passed", "details": "blade returned success"},
            "layer2": {"status": "failed", "details": "disk usage unchanged"},
            "warnings": ["test warning"],
        }
        ctx = _build_verify_replan_context(verification, [], 0, "k8s-disk-fill")
        assert ctx["trigger"] == "verify_replan"
        assert ctx["skill_name"] == "k8s-disk-fill"
        assert ctx["iteration_at_failure"] == 1
        assert ctx["failed_tool_calls"] == []
        assert ctx["failed_tool_names"] == []
        assert "Injection executed successfully" in ctx["error_summary"]
        assert "NOT observed" in ctx["error_summary"]
        assert ctx["verifier_findings"]["level"] == "unverified"
        assert ctx["verifier_findings"]["layer1_status"] == "passed"
        assert ctx["verifier_findings"]["layer2_status"] == "failed"
        assert ctx["verifier_findings"]["layer2_details"] == "disk usage unchanged"
        assert ctx["verifier_findings"]["warnings"] == ["test warning"]
        assert ctx["residuals_cleaned"] == []
        assert ctx["residuals_description"] == "None"

    def test_with_failed_evidence(self):
        verification = {
            "level": "unverified",
            "layer1": {"status": "passed", "details": ""},
            "layer2": {"status": "failed", "details": "no effect"},
            "checklist": {
                "items": [
                    {"step": 1, "status": "passed", "evidence": "ok"},
                    {"step": 2, "status": "failed", "evidence": "disk still at 39%"},
                ],
            },
        }
        ctx = _build_verify_replan_context(verification, [], 1, "test-skill")
        assert len(ctx["verifier_findings"]["failed_evidence"]) == 1
        assert "Step 2" in ctx["verifier_findings"]["failed_evidence"][0]
        assert "disk still at 39%" in ctx["verifier_findings"]["failed_evidence"][0]
        assert ctx["iteration_at_failure"] == 2

    def test_with_residuals(self):
        verification = {
            "level": "unverified",
            "layer1": {"status": "passed", "details": ""},
            "layer2": {"status": "failed", "details": ""},
        }
        residuals = [
            {"type": "running_experiment", "id": "abc123", "cleanup_result": "success"},
        ]
        ctx = _build_verify_replan_context(verification, residuals, 0, "test-skill")
        assert ctx["residuals_cleaned"] == residuals
        assert "running_experiment" in ctx["residuals_description"]
        assert "abc123" in ctx["residuals_description"]

    def test_suggestion_mentions_alternative(self):
        verification = {
            "level": "unverified",
            "layer1": {"status": "passed", "details": ""},
            "layer2": {"status": "failed", "details": ""},
        }
        ctx = _build_verify_replan_context(verification, [], 0, "test-skill")
        assert "alternative" in ctx["suggestion"].lower()


class TestCleanupResiduals:
    """Tests for _cleanup_residuals."""

    @pytest.mark.asyncio
    async def test_no_blade_uid_returns_empty(self):
        state = {"blade_uid": ""}
        cleaned = await _cleanup_residuals(state, "/fake/kubeconfig")
        assert cleaned == []

    @pytest.mark.asyncio
    async def test_no_blade_uid_key_returns_empty(self):
        state = {}
        cleaned = await _cleanup_residuals(state, "/fake/kubeconfig")
        assert cleaned == []

    @pytest.mark.asyncio
    async def test_with_blade_uid_cleans_up(self):
        state = {"blade_uid": "test-uid-123"}
        with patch(
            "chaos_agent.tools.blade.blade_destroy"
        ) as mock_destroy:
            mock_destroy.ainvoke = AsyncMock(
                return_value='{"status": "success"}'
            )
            cleaned = await _cleanup_residuals(state, "/fake/kubeconfig")
            assert len(cleaned) == 1
            assert cleaned[0]["type"] == "running_experiment"
            assert cleaned[0]["id"] == "test-uid-123"
            assert "success" in cleaned[0]["cleanup_result"]
            mock_destroy.ainvoke.assert_awaited_once_with(
                {"uid": "test-uid-123", "kubeconfig": "/fake/kubeconfig"}
            )

    @pytest.mark.asyncio
    async def test_blade_destroy_failure_recorded(self):
        state = {"blade_uid": "failing-uid"}
        with patch(
            "chaos_agent.tools.blade.blade_destroy"
        ) as mock_destroy:
            mock_destroy.ainvoke = AsyncMock(
                side_effect=RuntimeError("connection refused")
            )
            cleaned = await _cleanup_residuals(state, "/fake/kubeconfig")
            assert len(cleaned) == 1
            assert cleaned[0]["type"] == "running_experiment"
            assert "failed" in cleaned[0]["cleanup_result"]
            assert "connection refused" in cleaned[0]["cleanup_result"]
