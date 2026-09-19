"""Attribution consistency guard in recover finalize (submit-args mapping).

The recover Layer-2 judgement contract attributes residual deviations before
judging: recovery propagation cost is NOT recovery failure, but a "recovered"
verdict paired with fault-attributed residuals is self-contradictory and must
be downgraded. recover-d93a4ddf showed the opposite failure mode (partial for
a clean-attribution tail); the prompt contract fixes that side, this guard
fixes the contradictory side.
"""

from chaos_agent.agent.nodes.recover._recover_finalize import (
    _recover_verification_from_submit_args,
)


def _submit(**extra) -> dict:
    args = {
        "overall": "recovered",
        "layer2_status": "passed",
        "layer2_details": "fault effect absent, baseline restored",
        "baseline_used": True,
        "checklist": [
            {"step": 1, "status": "passed", "evidence": "config rolled back"}
        ],
    }
    args.update(extra)
    return args


class TestResidualAttributionGuard:
    def test_recovered_with_fault_residual_is_downgraded_to_partial(self):
        result = _recover_verification_from_submit_args(
            _submit(residual_attribution="fault_residual")
        )
        assert result["level"] == "partial"
        assert result["residual_attribution"] == "fault_residual"
        assert any(
            "residual_attribution_contradiction" in w for w in result["warnings"]
        )

    def test_recovered_with_recovery_process_attribution_stays_recovered(self):
        """The d93a4ddf shape: converging tail attributed to the recovery
        itself is a recovered verdict, not partial."""
        result = _recover_verification_from_submit_args(
            _submit(residual_attribution="recovery_process")
        )
        assert result["level"] == "recovered"
        assert result["residual_attribution"] == "recovery_process"
        assert result["warnings"] == []

    def test_clean_tail_survives_the_layer2_partial_sync(self):
        """CASCADE REGRESSION — the exact d93a4ddf submission shape:
        layer2_status='partial' (convergence still in progress) with a
        holistic 'recovered' judgement and clean attribution. The pre-fix
        level sync mechanically downgraded this to partial, reproducing the
        incident at the code layer."""
        result = _recover_verification_from_submit_args(
            _submit(layer2_status="partial", residual_attribution="recovery_process")
        )
        assert result["level"] == "recovered"

    def test_layer2_partial_sync_still_applies_without_attribution(self):
        """Legacy submits (no attribution field) keep the conservative sync."""
        result = _recover_verification_from_submit_args(_submit(layer2_status="partial"))
        assert result["level"] == "partial"

    def test_recovered_with_mixed_attribution_is_downgraded(self):
        result = _recover_verification_from_submit_args(
            _submit(residual_attribution="mixed")
        )
        assert result["level"] == "partial"
        assert any(
            "residual_attribution_contradiction" in w for w in result["warnings"]
        )

    def test_layer2_failed_still_overrides_recovered(self):
        """failed = fault still active — attribution cannot rescue it."""
        result = _recover_verification_from_submit_args(
            _submit(layer2_status="failed", residual_attribution="recovery_process")
        )
        assert result["level"] == "unrecovered"

    def test_partial_with_fault_residual_is_not_double_downgraded(self):
        result = _recover_verification_from_submit_args(
            _submit(
                overall="partial",
                layer2_status="partial",
                residual_attribution="fault_residual",
            )
        )
        assert result["level"] == "partial"
        assert result["warnings"] == []

    def test_invalid_attribution_value_is_ignored(self):
        result = _recover_verification_from_submit_args(
            _submit(residual_attribution="something-else")
        )
        assert result["level"] == "recovered"
        assert "residual_attribution" not in result

    def test_missing_field_is_backward_compatible(self):
        """Submits predating the field must parse exactly as before."""
        result = _recover_verification_from_submit_args(_submit())
        assert result["level"] == "recovered"
        assert "residual_attribution" not in result
        assert result["warnings"] == []


class TestUnverifiedVerdict:
    """Recovery "unconfirmed" is a fourth verdict, not a disguised failure.

    The recover overall vocabulary was three-valued (recovered / partial /
    unrecovered) where "unrecovered" is defined as counter-evidence ("fault
    STILL present"). With observation channels unavailable the LLM had no
    honest output — "unverified" gives it one, mirroring the inject side.
    """

    def test_submit_args_unverified_passes_through(self):
        result = _recover_verification_from_submit_args(
            _submit(overall="unverified", layer2_status="unknown")
        )
        assert result["level"] == "unverified"

    def test_submit_args_invalid_still_falls_back_to_unrecovered(self):
        """Unknown vocabulary keeps the old fail-loud fallback."""
        result = _recover_verification_from_submit_args(
            _submit(overall="no-idea-what-this-is")
        )
        assert result["level"] == "unrecovered"

    def test_text_fallback_overall_unverified(self):
        """'unverified' must not be swallowed by the 'verified' substring
        branch (inject-side vocabulary cross-contamination guard)."""
        from chaos_agent.agent.nodes.recover._recover_layer2_parse import (
            _parse_recovery_verification_result,
        )

        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (blade_destroy): passed - success\n"
            "- Layer2 (fault-specific): unknown - metrics query forbidden\n"
            "- Overall: unverified\n"
            "- Warnings: observation channel unavailable"
        )
        result = _parse_recovery_verification_result(text)
        assert result["level"] == "unverified"
