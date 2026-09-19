"""Tests for _verifier_finalize.py — finalize verification pure functions."""

from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.nodes.verify._verifier_finalize import (
    _overall_to_level,
    _verification_from_submit_args,
    _format_verification_detail,
    _build_verify_replan_context,
    _cleanup_residuals,
    _retired_uids_from_residuals,
    _verify_replan_eligible,
    _apply_step_coverage,
    _apply_deterministic_verdicts,
    _synthesize_passed,
    _layer1_contradiction_gap_fires,
    _COUNTER_EVIDENCE_REPLACEMENT_RE,
)
from chaos_agent.agent.nodes.verify._deterministic_rules import verdict_passed
from chaos_agent.agent.result.verdict import Layer1Result


class TestVerifyReplanEligible:
    """The verify-replan verdict gate: unverified + Layer 2 failed.

    Budget gating stays at the call site; the gate itself judges the
    verdict alone.
    """

    @staticmethod
    def _verification(level="unverified", l2="failed"):
        return {"level": level, "layer2": {"status": l2}}

    def test_unverified_l2_failed_is_eligible(self):
        assert _verify_replan_eligible(self._verification()) is True

    def test_verified_level_is_not_eligible(self):
        assert _verify_replan_eligible(
            self._verification(level="verified", l2="passed")
        ) is False

    def test_l2_not_failed_is_not_eligible(self):
        assert _verify_replan_eligible(
            self._verification(l2="partial")
        ) is False


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
        # Three-way glyph (✓ / ? / ✗, aligned with the batch summary):
        # honest ignorance keeps "?" — "✗ Verification" would translate
        # "cannot tell" back into "failed".
        assert text.startswith("? Verification: unverified")
        assert not text.startswith("✗")


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
        assert ctx["affected_step"] == "post-injection verification"
        assert ctx["decision"] == "plan_invalid"
        assert ctx["unresolved_questions"]
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
        state = {"experiment_uid": ""}
        cleaned = await _cleanup_residuals(state, "/fake/kubeconfig")
        assert cleaned == []

    @pytest.mark.asyncio
    async def test_no_blade_uid_key_returns_empty(self):
        state = {}
        cleaned = await _cleanup_residuals(state, "/fake/kubeconfig")
        assert cleaned == []

    @pytest.mark.asyncio
    async def test_with_blade_uid_cleans_up(self):
        """Round-31: the residual cleanup rides the liability sweep — the
        live-liability SET (owned − retired − proven-death), not the
        committed singular claim. The uid carries owned evidence (the
        no-net stance: a bare uid without owned evidence judges dead)
        and the sweep dispatches the bare destroy through the carrier's
        execution domain with the caller's resolved kubeconfig."""
        uid = "aabbccdd000000aa"
        state = {
            "experiment_uid": uid,
            "injection_method": "host_blade",
            "owned_experiment_uids": [uid],
            "retired_experiment_uids": [],
        }
        with patch(
            "chaos_agent.agent.providers.chaosblade.cli.blade_destroy"
        ) as mock_destroy:
            mock_destroy.ainvoke = AsyncMock(
                return_value='{"code":200,"success":true,"result":"%s"}' % uid
            )
            cleaned = await _cleanup_residuals(state, "/fake/kubeconfig")
            assert len(cleaned) == 1
            assert cleaned[0]["type"] == "running_experiment"
            assert cleaned[0]["id"] == uid
            assert cleaned[0]["cleanup_outcome"] == "success"
            mock_destroy.ainvoke.assert_awaited_once_with(
                {"uid": uid, "kubeconfig": "/fake/kubeconfig"}
            )

    @pytest.mark.asyncio
    async def test_blade_destroy_failure_recorded(self):
        """The sweep's failure face renders an honest FAILED artifact for
        the replan context — the uid rides the id field, the reason the
        result field."""
        uid = "aabbccdd000000bb"
        state = {
            "experiment_uid": uid,
            "injection_method": "host_blade",
            "owned_experiment_uids": [uid],
            "retired_experiment_uids": [],
        }
        with patch(
            "chaos_agent.agent.providers.chaosblade.cli.blade_destroy"
        ) as mock_destroy:
            mock_destroy.ainvoke = AsyncMock(
                side_effect=RuntimeError("connection refused")
            )
            cleaned = await _cleanup_residuals(state, "/fake/kubeconfig")
            assert len(cleaned) == 1
            assert cleaned[0]["type"] == "running_experiment"
            assert cleaned[0]["id"] == uid
            assert cleaned[0]["cleanup_outcome"] == "failed"
            assert "connection refused" in cleaned[0]["cleanup_result"]


class TestVerifyReplanDomainAlignment:
    """Round-31: the residual cleanup's criterion and action share ONE
    domain — the live liability set. The singular claim dispatch it
    replaced destroyed the CORPSE while a live sibling survived into the
    replan (R6'': the claim layer is COMMITTED with no death filter, so
    a composite create's first-birth corpse rode the dispatch and the
    fresh injection verified against TWO stacked faults)."""

    A = "aabbccdd000000aa"
    B = "aabbccdd000000bb"

    @staticmethod
    def _stub_destroy(monkeypatch) -> list:
        import json as _json

        from chaos_agent.agent.providers import FaultProviderRegistry

        provider = FaultProviderRegistry.resolve_by_method("host_blade")
        calls: list = []

        async def _fake_destroy(uid, kubeconfig=""):
            calls.append(uid)
            return _json.dumps(
                {"code": 200, "success": True, "result": uid}
            )

        monkeypatch.setattr(provider, "layer1_raw_destroy", _fake_destroy)
        return calls

    @pytest.mark.asyncio
    async def test_live_sibling_cleaned_not_the_dead_slot(self, monkeypatch):
        """R6'' flipped: first birth dead + sibling live — the sweep
        dispatches the SIBLING, the corpse stays untouched, and the
        cleaned artifact names the sibling."""
        calls = self._stub_destroy(monkeypatch)
        state = {
            "experiment_uid": self.A,  # slot = first birth (dead)
            "injection_method": "host_blade",
            "owned_experiment_uids": [self.A, self.B],
            "retired_experiment_uids": [self.A],
        }
        cleaned = await _cleanup_residuals(state, "/tmp/kc")
        assert calls == [self.B]
        assert [c["id"] for c in cleaned] == [self.B]
        assert cleaned[0]["cleanup_outcome"] == "success"

    @pytest.mark.asyncio
    async def test_all_dead_dispatches_nothing(self, monkeypatch):
        """New behaviour: an empty live set dispatches nothing — the old
        singular claim destroyed the corpse (idempotent NOT_FOUND
        waste); the sweep's own live filter refuses it."""
        calls = self._stub_destroy(monkeypatch)
        state = {
            "experiment_uid": self.A,
            "injection_method": "host_blade",
            "owned_experiment_uids": [self.A],
            "retired_experiment_uids": [self.A],
        }
        cleaned = await _cleanup_residuals(state, "/tmp/kc")
        assert calls == []
        assert cleaned == []

    @pytest.mark.asyncio
    async def test_plural_live_set_all_cleaned(self, monkeypatch):
        """Two live births (contract-replacement shape): the sweep covers
        EVERY live uid — the domain the pollution rationale licensed."""
        calls = self._stub_destroy(monkeypatch)
        state = {
            "experiment_uid": self.B,  # last-write-wins slot
            "injection_method": "host_blade",
            "owned_experiment_uids": [self.A, self.B],
            "retired_experiment_uids": [],
        }
        cleaned = await _cleanup_residuals(state, "/tmp/kc")
        assert sorted(calls) == [self.A, self.B]
        assert sorted(c["id"] for c in cleaned) == [self.A, self.B]
        assert all(
            c["cleanup_outcome"] == "success" for c in cleaned
        )

    @pytest.mark.asyncio
    async def test_incluster_residual_surfaces_guidance(self, monkeypatch):
        """The in-cluster delivery channel: the sweep refuses a host
        destroy at a CRD experiment and the artifact surfaces the
        guidance — an honest FAILED entry for the replan context
        instead of a corpse-destroy that reports the slot as cleaned."""
        calls = self._stub_destroy(monkeypatch)
        state = {
            "experiment_uid": self.A,
            "injection_method": "kubectl_exec",
            "owned_experiment_uids": [self.A, self.B],
            "retired_experiment_uids": [self.A],
            "kubeconfig": "/tmp/kc",
        }
        cleaned = await _cleanup_residuals(state, "/tmp/kc")
        assert calls == []  # no host destroy is fired at a CRD experiment
        assert len(cleaned) == 1
        assert cleaned[0]["id"] == self.B
        assert cleaned[0]["cleanup_outcome"] == "failed"
        assert "kubectl exec" in cleaned[0]["cleanup_result"]


class TestRetiredUidsFromResiduals:
    """Tests for _retired_uids_from_residuals (verify-replan UID retirement).

    Only UIDs whose destroy genuinely succeeded may be retired; a failed
    destroy may leave a live experiment that must stay visible to recovery.
    """

    def test_successful_destroy_retired(self):
        residuals = [
            {"type": "running_experiment", "id": "uid-1",
             "cleanup_result": '{"code":200,"success":true}',
             "cleanup_outcome": "success"},
        ]
        assert _retired_uids_from_residuals(residuals) == ["uid-1"]

    def test_exception_failure_not_retired(self):
        residuals = [
            {"type": "running_experiment", "id": "uid-1",
             "cleanup_result": "failed: connection refused",
             "cleanup_outcome": "failed"},
        ]
        assert _retired_uids_from_residuals(residuals) == []

    def test_soft_error_not_retired(self):
        """blade_destroy returns 'Error: ...' on exit-code failure without
        raising — such a UID may still be live and must NOT be retired."""
        residuals = [
            {"type": "running_experiment", "id": "uid-1",
             "cleanup_result": "Error: blade destroy failed (exit 1): not found",
             "cleanup_outcome": "failed"},
        ]
        assert _retired_uids_from_residuals(residuals) == []

    def test_missing_outcome_field_fails_closed(self):
        """Legacy residual dicts (pre-unification) carry no outcome field —
        the raw text alone must not retire; only the recorded SUCCESS
        verdict counts (the old prefix table retired ANY output not starting
        failed/Error:, including empty or garbage text)."""
        residuals = [
            {"type": "running_experiment", "id": "uid-1",
             "cleanup_result": '{"code":200,"success":true}'},
        ]
        assert _retired_uids_from_residuals(residuals) == []

    def test_non_experiment_types_ignored(self):
        residuals = [
            {"type": "debug_pod", "id": "pod-1", "cleanup_result": "deleted"},
            {"type": "running_experiment", "id": "", "cleanup_result": "ok"},
            {"type": "running_experiment", "cleanup_result": "ok"},
        ]
        assert _retired_uids_from_residuals(residuals) == []

    def test_empty_residuals(self):
        assert _retired_uids_from_residuals([]) == []

    def test_mixed_residuals(self):
        residuals = [
            {"type": "running_experiment", "id": "uid-ok",
             "cleanup_result": '{"code":200,"success":true}',
             "cleanup_outcome": "success"},
            {"type": "running_experiment", "id": "uid-err",
             "cleanup_result": "Error: blade destroy failed",
             "cleanup_outcome": "failed"},
        ]
        assert _retired_uids_from_residuals(residuals) == ["uid-ok"]


class TestApplyStepCoverageAnswerBased:
    """Answer-based coverage: every step answered; discretion needs a reason."""

    _SKILL = (
        "## 注入验证\n"
        "1. 检查目标内存占用是否升高\n"
        "2. 检查 Pod 是否出现 OOMKilled 事件\n"
        "3. 检查应用 A 的访问延迟\n"
    )

    def _verification(self, items):
        return {
            "level": "verified",
            "layer2": {"status": "passed", "details": ""},
            "warnings": [],
            "checklist": {
                "items": items,
                "total_executed": len(items),
                "total_count": len(items),
            },
        }

    def test_justified_discretionary_answers_do_not_downgrade(self):
        # Residual 'category' keys (historical checkpoints) are inert under
        # the single-tier contract — coverage logic never reads them.
        items = [
            {"step": 1, "status": "passed", "category": "core",
             "evidence": "memory 81% via kubectl top"},
            {"step": 2, "status": "expected", "category": "impact",
             "evidence": "no OOMKilled in events; mem-percent=80 below "
                         "eviction threshold"},
            {"step": 3, "status": "not_applicable", "category": "impact",
             "evidence": "'应用 A' matches no workload in this cluster"},
        ]
        v = self._verification(items)
        missing, expected_steps, executed = _apply_step_coverage(
            v, {"skill_case_content": self._SKILL}, None, False,
        )
        assert not missing
        assert expected_steps == 3
        assert executed == 3
        assert v["layer2"]["status"] == "passed"
        assert v["level"] == "verified"

    def test_expected_without_evidence_downgrades_to_partial(self):
        items = [
            {"step": 1, "status": "passed", "evidence": "memory 81%"},
            {"step": 2, "status": "expected"},
            {"step": 3, "status": "not_applicable",
             "evidence": "no workload matches"},
        ]
        v = self._verification(items)
        _apply_step_coverage(v, {"skill_case_content": self._SKILL}, None, False)
        assert v["layer2"]["status"] == "partial"
        assert v["level"] == "partial"
        assert any("without evidence" in w for w in v["warnings"])

    def test_silent_omission_still_flags_gap(self):
        items = [{"step": 1, "status": "passed", "evidence": "memory 81%"}]
        v = self._verification(items)
        missing, _, _ = _apply_step_coverage(
            v, {"skill_case_content": self._SKILL}, None, False,
        )
        assert missing == [2, 3]
        assert v["layer2"]["status"] == "partial"


class TestEnforceDiskBurnFactsDiscretionarySteps:
    """Burn override flips non-passed steps; discretionary statuses survive.

    Regression anchor for the disk_burn migration onto the deterministic
    rule registry: the SAME inputs must produce the SAME verdict and
    evidence structure as the legacy direct ``_enforce_disk_burn_facts``
    call (only the resolver path changed — the state now carries the
    fault identity so the registry pipeline can resolve the burn rule).
    """

    def test_override_flips_failed_spares_expected_and_does_not_gate_level(self):
        verification = {
            "level": "partial",
            "layer2": {"status": "failed", "details": ""},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "failed",
                 "evidence": "no I/O delta seen"},
                {"step": 2, "status": "expected",
                 "evidence": "no latency increase observed"},
            ]},
        }
        state = {
            "fault_target": "disk",
            "fault_action": "burn",
            "disk_burn_post_check": {
                "burn_io_detected": True,
                "active_partitions": [
                    {"name": "/dev/vdb", "write_throughput_mb_s": 120}],
            },
        }
        applied = _apply_deterministic_verdicts(verification, state)
        assert applied
        injection_item, propagated_item = verification["checklist"]["items"]
        # Injection-effect step overridden by the programmatic I/O evidence.
        assert injection_item["status"] == "passed"
        assert "OVERRIDE" in injection_item["evidence"]
        # Discretionary statuses ('expected') are preserved verbatim —
        # never flipped into fake passes.
        assert propagated_item["status"] == "expected"
        assert "OVERRIDE" not in propagated_item["evidence"]
        assert verification["layer2"]["status"] == "passed"
        # 'expected' is not in the failed set → does not gate the level.
        assert verification["level"] == "verified"

    def test_migration_preserves_legacy_evidence_wording(self):
        """Byte-level anchor: the migrated pipeline must reproduce the
        legacy disk_burn phrasing exactly (only the prefix was ever
        allowed to differ — and we kept [OVERRIDE])."""
        verification = {
            "level": "partial",
            "layer2": {"status": "failed", "details": ""},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "failed", "evidence": "no I/O delta"},
            ]},
        }
        state = {
            "fault_target": "disk",
            "fault_action": "burn",
            "disk_burn_post_check": {
                "burn_io_detected": True,
                "active_partitions": [
                    {"name": "/dev/vdb", "write_throughput_mb_s": 120}],
            },
        }
        _apply_deterministic_verdicts(verification, state)
        item = verification["checklist"]["items"][0]
        assert item["evidence"] == (
            "[OVERRIDE] Programmatic I/O check confirmed ACTIVE "
            "(write throughput: /dev/vdb: ~120 MB/s). "
            "Fault is still in effect — LLM observation was insufficient, "
            "not evidence of recovery."
        )
        assert verification["layer2"]["details"] == (
            "Programmatic I/O check: disk burn ACTIVE "
            "(write throughput: /dev/vdb: ~120 MB/s). LLM conclusion overridden."
        )
        assert verification["warnings"] == [
            "Programmatic override: disk_burn_post_check confirmed I/O ACTIVE "
            "(write throughput: /dev/vdb: ~120 MB/s), but LLM concluded "
            "the fault was absent (original status: 'failed')."
        ]

    def test_all_green_gets_annotation_only_no_verdict_rewrite(self):
        """Q1 semantics (deliberate, additive divergence from legacy):
        an already-green LLM verdict stays green — no lift, no warning,
        no evidence rewrite — but gains the [DETERMINISTIC] dual-source
        annotation row (the audit contract). Legacy left the verdict
        byte-untouched; the spec's quadrant-1 requirement supersedes
        (re-audit finding 6b: the old name claimed "zero interference"
        while the row WAS being appended — the test only passed because
        it never looked at items length)."""
        verification = {
            "level": "verified",
            "layer2": {"status": "passed", "details": "all good"},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "passed", "evidence": "io delta seen"},
            ]},
        }
        state = {
            "fault_target": "disk",
            "fault_action": "burn",
            "disk_burn_post_check": {
                "burn_io_detected": True,
                "active_partitions": [
                    {"name": "/dev/vdb", "write_throughput_mb_s": 120}],
            },
        }
        assert not _apply_deterministic_verdicts(verification, state)
        assert verification["level"] == "verified"
        assert verification["warnings"] == []
        assert verification["checklist"]["items"][0]["evidence"] == "io delta seen"
        # The one additive divergence from legacy: the annotation row.
        rows = verification["checklist"]["items"]
        assert len(rows) == 2
        assert rows[1]["step"] == "rule"
        assert "[DETERMINISTIC] rule 'disk_burn_io_active' passed" in (
            rows[1]["evidence"]
        )

    def test_l2_only_degradation_lifted_burn_family(self):
        """Q2a corner (deliberate divergence from legacy no-op, re-audit
        finding 6): post-check measured I/O ACTIVE but the LLM concluded
        degraded with NO checklist items contradicting it. Legacy nested
        the L2 lift under `if _io_overridden:` — a green/absent checklist
        left the contradictory state (program says ACTIVE, LLM says
        failed) standing. The spec's family-agnostic quadrant-2a
        semantics lifts it; both no-checklist and green-checklist shapes
        pin the divergence as deliberate."""
        state = {
            "fault_target": "disk",
            "fault_action": "burn",
            "disk_burn_post_check": {
                "burn_io_detected": True,
                "active_partitions": [
                    {"name": "/dev/vdb", "write_throughput_mb_s": 120}],
            },
        }
        # Shape A: no checklist at all
        v_a = {
            "level": "partial",
            "layer2": {"status": "failed", "details": "LLM saw nothing"},
            "warnings": [],
        }
        assert _apply_deterministic_verdicts(v_a, state)
        assert v_a["layer2"]["status"] == "passed"
        assert v_a["level"] == "verified"
        assert any("Programmatic override" in w for w in v_a["warnings"])
        # Shape B: all-green checklist + degraded L2
        v_b = {
            "level": "partial",
            "layer2": {"status": "failed", "details": "LLM saw nothing"},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "passed", "evidence": "io seen"},
            ]},
        }
        assert _apply_deterministic_verdicts(v_b, state)
        assert v_b["layer2"]["status"] == "passed"
        assert v_b["level"] == "verified"
        # Green item untouched (nothing to flip — only the L2 was degraded)
        assert v_b["checklist"]["items"][0]["evidence"] == "io seen"

    def test_no_rule_family_zero_interference(self):
        """A family without a declared rule must pass through byte-identical."""
        verification = {
            "level": "partial",
            "layer2": {"status": "failed", "details": ""},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "failed", "evidence": "no loss observed"},
            ]},
        }
        # network loss family: no declared deterministic rule
        state = {
            "fault_target": "network",
            "fault_action": "loss",
            "metric_observations": [{"iteration": 1, "metrics": {}}],
        }
        assert not _apply_deterministic_verdicts(verification, state)
        assert verification == {
            "level": "partial",
            "layer2": {"status": "failed", "details": ""},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "failed", "evidence": "no loss observed"},
            ]},
        }


class TestFiveQuadrantSynthesis:
    """The five-quadrant truth table (design decision 3, tasks 4.2).

      Q1  program passed × LLM green       → [DETERMINISTIC] dual-source
                                             annotation, verdict untouched;
      Q2a program passed × LLM degraded,
          no specific counter-proof        → lift ([OVERRIDE] + warnings);
      Q2b program passed × LLM degraded
          WITH a specific counter-proof    → LLM respected, withheld noted;
      Q3  program unknown (rule family)    → zero interference;
      Q4  no rule family                  → zero interference (anchored
                                             by the migration tests above);
      Q5  cross_check contradiction        → its downgrade semantics are
                                             unchanged and a program pass
                                             does not roll them back.
    """

    @staticmethod
    def _verdict():
        # Generic rule shape (no legacy render subjects): the process-kill
        # criteria as #25 measured them.
        return verdict_passed(
            "process_kill_restarts",
            [
                "RestartCount 8 → 10 (Δ+2), "
                "container ID replaced (2 distinct observed)",
                "mechanism anchor: fault_handle",
            ],
        )

    @staticmethod
    def _verification(l2_status="passed", step_status="passed"):
        return {
            "level": "verified" if l2_status == "passed" else l2_status,
            "layer2": {"status": l2_status, "details": "observed restarts"},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": step_status, "evidence": "restarts seen"},
            ]},
        }

    def test_quadrant1_dual_source_annotation(self):
        """Q1: program passed × LLM green — the verdict fields stay
        untouched; a [DETERMINISTIC] row is appended carrying the rule
        name, the numeric lines and the anchor (dual-source audit)."""
        v = self._verification()
        assert _synthesize_passed(v, self._verdict()) is False  # no lift
        assert v["checklist"]["items"][0]["evidence"] == "restarts seen"
        assert v["layer2"]["status"] == "passed"
        assert v["warnings"] == []
        rule_row = v["checklist"]["items"][1]
        # Non-int step marker: step-coverage validators skip it.
        assert rule_row["step"] == "rule"
        assert rule_row["status"] == "passed"
        assert rule_row["evidence"] == (
            "[DETERMINISTIC] rule 'process_kill_restarts' passed — "
            "RestartCount 8 → 10 (Δ+2), container ID replaced "
            "(2 distinct observed); mechanism anchor: fault_handle."
        )

    def test_quadrant1_annotation_is_idempotent(self):
        """Q1 stability: re-synthesizing the same rule never stacks a
        second [DETERMINISTIC] row (re-verify loops re-run finalize)."""
        v = self._verification()
        _synthesize_passed(v, self._verdict())
        _synthesize_passed(v, self._verdict())
        assert len(v["checklist"]["items"]) == 2

    @pytest.mark.parametrize(
        "degraded", ["failed", "recovered_before_observation", "partial"])
    def test_quadrant2a_l2_only_degradation_lifted(self, degraded):
        """Q2a (L2-only leg): an L2 downgrade with an all-green checklist
        is still a degraded verdict the program evidence overrides — the
        lift no longer hides behind a checklist flip."""
        v = self._verification(l2_status=degraded)
        assert _synthesize_passed(v, self._verdict()) is True
        assert v["layer2"]["status"] == "passed"
        # Green steps are never rewritten — only degraded ones lift.
        assert v["checklist"]["items"][0]["evidence"] == "restarts seen"
        assert any("Programmatic override" in w for w in v["warnings"])
        assert v["level"] == "verified"

    def test_quadrant2a_checklist_flip_uses_generic_wording(self):
        """Q2a (checklist leg): a rule without legacy render subjects lifts
        degraded steps with the pipeline's generic [OVERRIDE] phrasing —
        the rule's evidence lines render into the override subject."""
        v = self._verification(l2_status="failed", step_status="failed")
        assert _synthesize_passed(v, self._verdict()) is True
        item = v["checklist"]["items"][0]
        assert item["status"] == "passed"
        assert item["evidence"].startswith("[OVERRIDE] RestartCount 8 → 10")
        assert "not evidence of recovery" in item["evidence"]
        assert v["level"] == "verified"

    def test_quadrant2b_crosscheck_contradiction_respected(self):
        """Q2b/Q5: a cross_check contradiction already downgraded the
        verdict (hallucinated deltas ARE counter-proof); the rule's pass
        is WITHHELD — the downgrade stands and the override attempt is
        recorded in warnings."""
        v = self._verification(l2_status="failed", step_status="failed")
        v["warnings"] = [
            "LLM evidence cites CPU 5%→80% (Δ=+75), "
            "but observation timeline shows no change "
            "(stayed at 5 across 3 iteration(s))",
        ]
        assert _synthesize_passed(v, self._verdict()) is False
        # Downgrade not rolled back — the LLM verdict is respected.
        assert v["layer2"]["status"] == "failed"
        assert v["checklist"]["items"][0]["status"] == "failed"
        withheld = [w for w in v["warnings"] if "override withheld" in w]
        assert len(withheld) == 1
        assert "counter-proof" in withheld[0]
        assert "cross-check" in withheld[0]

    def test_quadrant2b_replacement_signal_respected(self):
        """Q2b: a replacement citation in the LLM's evidence (a new
        container id) means the rule's numbers may describe a stale
        object — the LLM verdict is respected."""
        v = self._verification(l2_status="failed")
        v["layer2"]["details"] = (
            "evidence is stale: a new container id appeared after the probe"
        )
        assert _synthesize_passed(v, self._verdict()) is False
        assert v["layer2"]["status"] == "failed"
        assert any("override withheld" in w for w in v["warnings"])

    def test_hedging_and_number_restatement_are_not_counter_proof(self):
        """Gate narrowness anchor: hedging ("might have recovered") and
        restating the numbers are absence, not refutation — the lift
        proceeds. Only a SPECIFIC counter-proof withholds."""
        v = self._verification(l2_status="failed")
        v["layer2"]["details"] = (
            "restarts observed but effect might have recovered; "
            "RestartCount went from 8 to 10"
        )
        assert _synthesize_passed(v, self._verdict()) is True
        assert v["layer2"]["status"] == "passed"

    def test_quadrant3_rule_unknown_passes_llm_verdict_through(self):
        """Q3: a rule family whose evaluate returns unknown (numbers
        absent) leaves the LLM verdict byte-identical — unknown is the
        LLM's territory, and absence never becomes a downgrade."""
        v = {
            "level": "partial",
            "layer2": {"status": "failed", "details": ""},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "failed", "evidence": "no signal"},
            ]},
        }
        state = {
            "fault_target": "process",
            "fault_action": "kill",
            # A single RestartCount observation → fewer than 2 → unknown.
            "metric_observations": [
                {"iteration": 1, "metrics": {"RestartCount": 8}},
            ],
            "fault_handle": {"experiment_uid": "exp-1"},
        }
        assert not _apply_deterministic_verdicts(v, state)
        assert v == {
            "level": "partial",
            "layer2": {"status": "failed", "details": ""},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "failed", "evidence": "no signal"},
            ]},
        }

    def test_process_kill_full_pipeline_lifts_degraded_llm_verdict(self):
        """End-to-end through the rule registry: #25's measured shape
        (RESTARTS 8→10, container ID replaced, handle anchored) lifts a
        degraded LLM verdict — RuleContext assembled from state via the
        legacy fault-spec fallback."""
        v = self._verification(l2_status="failed", step_status="failed")
        state = {
            "fault_target": "process",
            "fault_action": "kill",
            "metric_observations": [
                {"iteration": 1, "metrics": {
                    "RestartCount": 8,
                    "Container ID": "containerd://aaa",
                }},
                {"iteration": 3, "metrics": {
                    "RestartCount": 10,
                    "Container ID": "containerd://bbb",
                }},
            ],
            "fault_handle": {"experiment_uid": "exp-25"},
        }
        assert _apply_deterministic_verdicts(v, state)
        assert v["layer2"]["status"] == "passed"
        assert v["level"] == "verified"
        assert v["checklist"]["items"][0]["status"] == "passed"
        assert "[OVERRIDE] RestartCount 8 → 10" in v["checklist"]["items"][0]["evidence"]
        assert any("Programmatic override" in w for w in v["warnings"])

    def test_disk_fill_full_pipeline_lifts_degraded_llm_verdict(self):
        """End-to-end for the fill family: #29's measured shape (df
        11%→86% against an 85% injected target) lifts a degraded LLM
        verdict — the percent threshold flows from state.params through
        the legacy fault-spec fallback into the RuleContext."""
        v = self._verification(l2_status="failed", step_status="failed")
        state = {
            "fault_target": "disk",
            "fault_action": "fill",
            "params": {"percent": "85"},
            "metric_observations": [
                {"iteration": 1, "metrics": {
                    "Disk usage (overlay)": "11% (37384168/112277999680)"}},
                {"iteration": 3, "metrics": {
                    "Disk usage (overlay)": "86% (96636764160/112277999680)"}},
            ],
            "fault_handle": {"experiment_uid": "exp-29"},
        }
        assert _apply_deterministic_verdicts(v, state)
        assert v["layer2"]["status"] == "passed"
        assert v["level"] == "verified"
        ev = v["checklist"]["items"][0]["evidence"]
        assert "[OVERRIDE]" in ev
        assert "86%" in ev and "85%" in ev
        assert any(
            "Programmatic override" in w and "disk_fill_usage_target" in w
            for w in v["warnings"]
        )

    def test_cpu_fullload_placeholder_never_interferes(self):
        """End-to-end for the declared-but-uncalibrated families: even
        with perfect numbers and a solid anchor the rule stays unknown,
        so a degraded LLM verdict passes through byte-identical (the
        declaration is a typed extension point, not a behaviour)."""
        v = self._verification(l2_status="failed", step_status="failed")
        before = {
            "level": v["level"],
            "layer2": dict(v["layer2"]),
            "warnings": list(v["warnings"]),
            "items": [dict(i) for i in v["checklist"]["items"]],
        }
        state = {
            "fault_target": "cpu",
            "fault_action": "fullload",
            "params": {"cpu-percent": "80"},
            "metric_observations": [
                {"iteration": 1, "metrics": {"CPU usage": "3%"}},
                {"iteration": 2, "metrics": {"CPU usage": "97%"}},
            ],
            "fault_handle": {"experiment_uid": "exp-23"},
        }
        assert not _apply_deterministic_verdicts(v, state)
        assert v["level"] == before["level"]
        assert v["layer2"] == before["layer2"]
        assert v["warnings"] == before["warnings"]
        assert v["checklist"]["items"] == before["items"]


class TestLayer1ContradictionGate:
    """Tasks 6.1: the gap gate reads the POST-synthesis layer2 status.

    The deterministic rules run earlier in the finalize pipeline, so
    their lift is already visible to the gate — a programmatic pass
    suppresses the pointless re-check, while a genuine LLM downgrade
    (no rule, or a withheld lift) still surfaces the contradiction.
    """

    _KILL_STATE = {
        "fault_target": "process",
        "fault_action": "kill",
        "metric_observations": [
            {"iteration": 1, "metrics": {
                "RestartCount": 8, "Container ID": "containerd://aaa",
            }},
            {"iteration": 3, "metrics": {
                "RestartCount": 10, "Container ID": "containerd://bbb",
            }},
        ],
        "fault_handle": {"experiment_uid": "exp-25"},
    }

    @staticmethod
    def _degraded_verification():
        return {
            "level": "failed",
            "layer2": {"status": "failed", "details": ""},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "failed", "evidence": "no signal"},
            ]},
        }

    def test_gap_silent_after_programmatic_lift(self):
        """Program passed → synthesis lifts L2 to passed → the gap stays
        silent: re-checking a contradiction the rule already resolved
        just spins without terminating."""
        layer1 = Layer1Result(status="passed", affected_count=0)
        v = self._degraded_verification()
        assert _apply_deterministic_verdicts(v, self._KILL_STATE)
        assert v["layer2"]["status"] == "passed"  # the lift happened
        assert not _layer1_contradiction_gap_fires(layer1, v)

    def test_gap_fires_when_no_rule_family(self):
        """Pre-layer semantics unchanged: no rule lifts the LLM's failed
        verdict, so the blade-success-but-0-affected contradiction
        surfaces for a re-look."""
        layer1 = Layer1Result(status="passed", affected_count=0)
        v = self._degraded_verification()
        # network family — no declared rule, zero interference
        assert not _apply_deterministic_verdicts(v, {
            "fault_target": "network", "fault_action": "loss",
        })
        assert v["layer2"]["status"] == "failed"
        assert _layer1_contradiction_gap_fires(layer1, v)

    def test_gap_fires_when_lift_withheld_by_counter_evidence(self):
        """A withheld lift leaves the LLM downgrade in place — the
        contradiction deserves the re-look (the counter-proof and the
        blade report must be reconciled by the LLM, not by the rule)."""
        layer1 = Layer1Result(status="passed", affected_count=0)
        v = self._degraded_verification()
        # Cross-check contradiction pre-landed in warnings: the lift is
        # withheld, L2 stays failed.
        v["warnings"] = [
            "LLM evidence cites CPU 5%→80% (Δ=+75), "
            "but observation timeline shows no change "
            "(stayed at 5 across 3 iteration(s))",
        ]
        assert not _apply_deterministic_verdicts(v, self._KILL_STATE)
        assert v["layer2"]["status"] == "failed"
        assert _layer1_contradiction_gap_fires(layer1, v)

    def test_gap_never_fires_when_layer1_has_affected_resources(self):
        """The contradiction is specifically 0-affected — a normal
        experiment with affected resources never trips this gate."""
        layer1 = Layer1Result(status="passed", affected_count=3)
        v = self._degraded_verification()
        assert not _layer1_contradiction_gap_fires(layer1, v)


class TestReAuditFindings:
    """Re-audit regressions (post-implementation review of this change's
    own logic chain). Each test pins a defect the first implementation
    shipped with — found by replaying the full pipeline against the
    rule families' real evidence shapes.
    """

    _KILL_STATE = {
        "fault_target": "process",
        "fault_action": "kill",
        "metric_observations": [
            {"iteration": 1, "metrics": {
                "RestartCount": 8, "Container ID": "containerd://aaa",
            }},
            {"iteration": 3, "metrics": {
                "RestartCount": 10, "Container ID": "containerd://bbb",
            }},
        ],
        "fault_handle": {"experiment_uid": "exp"},
    }

    def test_kill_family_replacement_wording_is_not_counter_proof(self):
        """Finding 1: the kill rule's OWN evidence shape ("container ID
        changed") is what an honest green LLM writes — the replacement
        arm of the counter-evidence gate must be scoped off for rules
        that treat replacement as effect (Q1 annotates normally)."""
        v = {
            "level": "verified",
            "layer2": {"status": "passed",
                       "details": "restarts observed, container ID changed"},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "passed",
                 "evidence": "RestartCount 8 → 10; new container ID observed"},
            ]},
        }
        assert not _apply_deterministic_verdicts(v, self._KILL_STATE)
        assert v["warnings"] == []  # no misfired withheld warning
        rule_row = v["checklist"]["items"][1]
        assert rule_row["step"] == "rule"
        assert "[DETERMINISTIC]" in rule_row["evidence"]

    def test_numeric_contradiction_still_withholds_kill_rule(self):
        """Finding-1 regression guard: scoping off the replacement arm
        must NOT weaken the numeric cross-check contradiction arm —
        a hallucinated delta withholds the kill lift too."""
        v = {
            "level": "failed",
            "layer2": {"status": "failed", "details": ""},
            "warnings": [
                "LLM evidence cites CPU 5%→80% (Δ=+75), "
                "but observation timeline shows no change "
                "(stayed at 5 across 3 iteration(s))",
            ],
            "checklist": {"items": [
                {"step": 1, "status": "failed", "evidence": "no signal"},
            ]},
        }
        assert not _apply_deterministic_verdicts(v, self._KILL_STATE)
        assert v["layer2"]["status"] == "failed"
        assert any("override withheld" in w for w in v["warnings"])

    def test_replacement_gate_still_applies_to_continuity_rules(self):
        """Finding-1 regression guard: rules that PRESUPPOSE object
        continuity (fill: measured usage describes a stale object once
        the container is replaced) keep the full replacement gate."""
        v = {
            "level": "failed",
            "layer2": {"status": "failed",
                       "details": "evidence is stale: a new container id "
                                  "appeared after the probe"},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "failed", "evidence": "no signal"},
            ]},
        }
        state = {
            "fault_target": "disk",
            "fault_action": "fill",
            "params": {"percent": "85"},
            "metric_observations": [
                {"iteration": 1, "metrics": {
                    "Disk usage (overlay)": "11% (1/100)"}},
                {"iteration": 3, "metrics": {
                    "Disk usage (overlay)": "86% (86/100)"}},
            ],
            "fault_handle": {"experiment_uid": "exp"},
        }
        assert not _apply_deterministic_verdicts(v, state)
        assert v["layer2"]["status"] == "failed"
        assert any("override withheld" in w for w in v["warnings"])

    def test_annotation_without_checklist_lands_in_warnings_no_skeleton(self):
        """Finding 2: when the LLM emitted NO checklist, the annotation
        must not fabricate one — a synthetic checklist key flips the
        step-coverage guard into validation mode and manufactures a
        phantom step gap (downgrade + re-verify spin)."""
        v = {
            "level": "verified",
            "layer2": {"status": "passed", "details": "ok"},
            "warnings": [],
        }
        assert not _apply_deterministic_verdicts(v, self._KILL_STATE)
        assert "checklist" not in v  # no skeleton fabricated
        assert any(
            "[DETERMINISTIC]" in w and "no checklist" in w
            for w in v["warnings"]
        )
        # The step-coverage guard still sees "checklist absent → skip":
        skill = "# x\n## 注入验证\n1. check a\n2. check b\n"
        missing, expected, _ = _apply_step_coverage(
            v, {"skill_case_content": skill}, None, False,
        )
        assert missing is None and expected == 0
        assert v["layer2"]["status"] == "passed"  # no phantom downgrade

    def test_empty_items_lift_rederives_level(self):
        """Finding 3: an empty-items checklist whose degraded L2 gets
        lifted must re-derive the level too — L2 passed next to a stale
        'unverified' level is an inconsistent state."""
        v = {
            "level": "unverified",
            "layer2": {"status": "failed", "details": ""},
            "warnings": [],
            "checklist": {"items": []},
        }
        assert _apply_deterministic_verdicts(v, self._KILL_STATE)
        assert v["layer2"]["status"] == "passed"
        assert v["level"] == "verified"

    def test_echoed_programmatic_text_is_not_counter_proof(self):
        """Finding 7 (invariant made explicit): programmatic rows carry
        the pipeline's own words — the kill rule's override/annotation
        text names "container ID replaced" (its own criteria). If such
        text enters the scan surface (an LLM echoing a prior turn's
        override wording into its checklist, or a future profile with
        two rules sharing a match key), the gate must not let the
        program refute itself. Control: the SAME observation wording
        WITHOUT the programmatic prefix is the LLM's own words and
        still withholds."""
        state = {
            "fault_target": "disk",
            "fault_action": "fill",
            "params": {"percent": "85"},
            "metric_observations": [
                {"iteration": 1, "metrics": {
                    "Disk usage (overlay)": "11% (1/100)"}},
                {"iteration": 3, "metrics": {
                    "Disk usage (overlay)": "86% (86/100)"}},
            ],
            "fault_handle": {"experiment_uid": "exp"},
        }
        echo = (
            "[OVERRIDE] deterministic rule 'process_kill_restarts' "
            "passed — RestartCount 0 → 2 (Δ+2), "
            "container ID replaced (2 distinct observed)"
        )
        v = {
            "level": "failed",
            "layer2": {"status": "failed", "details": ""},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "failed", "evidence": echo},
            ]},
        }
        assert _apply_deterministic_verdicts(v, state)  # echo ≠ counter-proof
        assert v["layer2"]["status"] == "passed"

        # Control: fresh dict (the lift above mutates layer2 in place).
        control = {
            "level": "failed",
            "layer2": {"status": "failed", "details": ""},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "failed",
                 "evidence": "the container ID was replaced after the probe"},
            ]},
        }
        assert not _apply_deterministic_verdicts(control, state)
        assert control["layer2"]["status"] == "failed"
        assert any("override withheld" in w for w in control["warnings"])

    def test_documented_replacement_variants_all_match(self):
        """Finding 8 + its fix-of-fix: the enumerated auxiliary-verb list
        missed "have been" on plural subjects ("pods have been
        re-created") — the third fragility exposure of the list form,
        which is now structurally replaced by noun→window→core-verb
        co-occurrence so ANY auxiliary works. This is the REGEX coverage
        unit (hit surface only); the negation-exclusion semantics are
        pinned END-TO-END in test_negated_replacement_wording (a test
        mirroring the gate's two layers would survive a mutation that
        breaks the production check — caught by mutation testing).
        Also guards the boundary: effect-wording that is NOT a
        replacement claim stays unmatched."""
        for phrase in (
            "container ID changed from aaa to bbb",
            "new container id observed",
            "pod was re-created",
            "the container was replaced",
            "containers were replaced",
            "pod has been re-created",
            "pods have been re-created",   # fix-of-fix: was missed
            "pod got re-created",           # any auxiliary must work
            "containers might be replaced", # any modal must work
            "the pod, after eviction, was re-created",  # interjection
            "container id has changed",
            "container ids differ from baseline",
        ):
            assert _COUNTER_EVIDENCE_REPLACEMENT_RE.search(phrase), phrase
        for phrase in (
            "a new container is running",
            "the pod was created just now",
        ):
            assert not _COUNTER_EVIDENCE_REPLACEMENT_RE.search(phrase), phrase

    def test_negated_replacement_wording_is_not_counter_proof(self):
        """Fix-of-fix on finding 8, END-TO-END: a negated replacement
        mention is a CONTINUITY statement ("container id was not changed"
        = the object survived = the green case), not counter-proof — so
        the lift proceeds. Two exclusion layers in production: "no"
        immediately before the adjective-first arm via lookbehind;
        not/never/no inside the matched window via the code check.
        Drives the REAL gate (mutation testing showed an earlier draft
        that mirrored the gate inside the test survived a mutation
        that disabled the production check — a false pin)."""
        def _fill_state():
            return {
                "fault_target": "disk",
                "fault_action": "fill",
                "params": {"percent": "85"},
                "metric_observations": [
                    {"iteration": 1, "metrics": {
                        "Disk usage (overlay)": "11% (1/100)"}},
                    {"iteration": 3, "metrics": {
                        "Disk usage (overlay)": "86% (86/100)"}},
                ],
                "fault_handle": {"experiment_uid": "exp"},
            }

        for wording in (
            "container id was not changed since injection",
            "pods never got re-created during the window",
            "no new container id was observed",
            "pod has not been replaced",
        ):
            v = {
                "level": "failed",
                "layer2": {"status": "failed",
                           "details": f"object continuity confirmed: {wording}"},
                "warnings": [],
                "checklist": {"items": [
                    {"step": 1, "status": "failed", "evidence": wording},
                ]},
            }
            assert _apply_deterministic_verdicts(v, _fill_state()), wording
            assert v["layer2"]["status"] == "passed", wording

        # Control: the unnegated twin withholds (exclusions must not
        # swallow the real counter-proof shape).
        v2 = {
            "level": "failed",
            "layer2": {"status": "failed",
                       "details": "evidence is stale: container id was changed"},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "failed",
                 "evidence": "container id was changed"},
            ]},
        }
        assert not _apply_deterministic_verdicts(v2, _fill_state())
        assert v2["layer2"]["status"] == "failed"
        assert any("override withheld" in w for w in v2["warnings"])

    def test_non_english_replacement_wording_is_a_known_blind_spot(self):
        """Finding 14 (blind-spot pin, NOT a capability claim): the
        textual replacement gate matches English wording only — a
        Chinese-language counter-proof ("\u5bb9\u5668 ID \u5df2\u53d8\u66f4") does NOT fire it, so
        the lift proceeds. Pinning the CURRENT behavior so nobody later
        assumes the gate covers non-English evidence. Deliberately not
        "fixed": no Chinese live-run ground truth exists to calibrate
        against, and a guessed-form regex would be the next finding-8
        (documented coverage that is not real coverage). Mitigations that
        keep this acceptable: the system prompt is English with English
        evidence exemplars (strong output-language pull), and the
        numeric-contradiction counter-proof channel (cross_check) is
        language-independent. If Chinese samples ever appear, the
        noun\u2192window\u2192core-verb structure transfers (Chinese core verbs are
        a finite set with no auxiliary conjugation)."""
        def _fill_state():
            return {
                "fault_target": "disk",
                "fault_action": "fill",
                "params": {"percent": "85"},
                "metric_observations": [
                    {"iteration": 1, "metrics": {
                        "Disk usage (overlay)": "11% (1/100)"}},
                    {"iteration": 3, "metrics": {
                        "Disk usage (overlay)": "86% (86/100)"}},
                ],
                "fault_handle": {"experiment_uid": "exp"},
            }

        # A real replacement counter-proof, stated in Chinese: lifted
        # today because the textual gate is English-only. The numeric
        # cross-check channel would still catch a numeric contradiction.
        v = {
            "level": "failed",
            "layer2": {"status": "failed",
                       "details": "\u5bb9\u5668 ID \u5df2\u53d8\u66f4\uff0c\u8bc1\u636e\u5df2\u8fc7\u671f"},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "failed",
                 "evidence": "\u5bb9\u5668 ID \u5df2\u53d8\u66f4\uff0c\u8bc1\u636e\u5df2\u8fc7\u671f"},
            ]},
        }
        assert _apply_deterministic_verdicts(v, _fill_state())
        assert v["layer2"]["status"] == "passed"

    def test_primary_evidence_gate_interaction_with_quadrants(self):
        """Finding 9 (behaviour pin): the construction-time honesty gate
        (overall=verified requires primary_evidence_observed) interacts
        asymmetrically with the quadrants — each side contract-correct,
        the asymmetry emergent. Q1: the annotation is additive, so the
        LLM's own no-primary-evidence admission keeps level=partial even
        though a rule passed (the row records the numeric evidence).
        Q2a: the lift re-derives level from the post-lift state — and the
        rule's anchored numeric timeline IS primary evidence, so
        verified stands. Both outcomes keep the gate's warning:
        attributable either way."""
        # Q1 — green L2, honest admission
        v1 = _verification_from_submit_args({
            "overall": "verified", "layer2_status": "passed",
            "layer2_details": "ok", "primary_evidence_observed": False,
            "checklist": [{"step": 1, "status": "passed",
                           "evidence": "restarts seen"}],
        })
        assert v1["level"] == "partial"  # gate fired at construction
        assert not _apply_deterministic_verdicts(v1, self._KILL_STATE)
        assert v1["level"] == "partial"  # annotation is additive only
        assert len(v1["checklist"]["items"]) == 2  # annotation row added
        assert any("PrimaryEvidenceObserved" in w for w in v1["warnings"])

        # Q2a — degraded L2, same admission: the lift re-derives level
        v2 = _verification_from_submit_args({
            "overall": "verified", "layer2_status": "failed",
            "layer2_details": "saw nothing",
            "primary_evidence_observed": False,
            "checklist": [{"step": 1, "status": "failed",
                           "evidence": "no signal"}],
        })
        assert v2["level"] == "partial"  # gate fired here too
        assert _apply_deterministic_verdicts(v2, self._KILL_STATE)
        assert v2["layer2"]["status"] == "passed"
        assert v2["level"] == "verified"  # rule evidence IS primary evidence
        assert any("PrimaryEvidenceObserved" in w for w in v2["warnings"])

    def test_expired_fault_window_disables_rule_adjudication(self):
        """Finding 12 (honesty): the rules' numeric evidence outlives the
        fault window — timeline peaks, cumulative counters, the
        injection-time post-check snapshot are historical traces. When
        Layer 1 reports the experiment expired (timeout or early
        destroy), lifting an honest recovered_before_observation would
        assert "Fault is still in effect" over a window that verifiably
        closed. The rules stand down; the LLM keeps the call (same
        stance as the L1-only entry's known-cause handling)."""
        from chaos_agent.agent.result.verdict import Layer1Result

        expired_l1 = Layer1Result(
            status="failed", expired=True,
            details="Experiment status: Destroyed — the fault window has expired",
        )
        # The SAME state that lifts in every other test:
        v = {
            "level": "partial",
            "layer2": {"status": "recovered_before_observation", "details": ""},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "recovered_before_observation",
                 "evidence": "fault window closed before observation"},
            ]},
        }
        assert not _apply_deterministic_verdicts(
            v, self._KILL_STATE, layer1=expired_l1,
        )
        # LLM's honest expiry verdict stands, override text never lands
        assert v["layer2"]["status"] == "recovered_before_observation"
        assert v["checklist"]["items"][0]["status"] == "recovered_before_observation"
        assert not any("OVERRIDE" in w for w in v.get("warnings", []))

        # Control: the same inputs with a live window lift as usual.
        live_l1 = Layer1Result(status="passed", expired=False)
        v2 = {
            "level": "partial",
            "layer2": {"status": "recovered_before_observation", "details": ""},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "recovered_before_observation",
                 "evidence": "fault window closed before observation"},
            ]},
        }
        assert _apply_deterministic_verdicts(
            v2, self._KILL_STATE, layer1=live_l1,
        )
        assert v2["layer2"]["status"] == "passed"

    def test_expired_window_blocks_q1_annotation_too(self):
        """Finding 12 companion pin: the stand-down is TOTAL — the Q1
        dual-source annotation must not land either. An annotation row
        over an expired window would present historical traces (the
        cumulative RestartCount, the stale post-check snapshot) as
        current-effect evidence, which is the same honesty breach as the
        lift, just quieter."""
        from chaos_agent.agent.result.verdict import Layer1Result

        expired_l1 = Layer1Result(
            status="passed", expired=True,
            details="Experiment status: Destroyed — the fault window has expired",
        )
        # Non-empty checklist skeleton — the items-channel annotation
        # only lands on an existing skeleton (finding 2's no-skeleton
        # fallback goes to warnings instead, a different branch).
        v = {
            "level": "verified",
            "layer2": {"status": "passed", "details": "all green"},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "passed",
                 "evidence": "restart count rose as expected"},
            ]},
        }
        # NOTE: the return value is "lift applied" — Q1 (LLM already
        # green) returns False in BOTH the expired and live cases, so the
        # discriminating assertion is the annotation row, not the return.
        _apply_deterministic_verdicts(v, self._KILL_STATE, layer1=expired_l1)
        # No annotation row: the LLM's own row stays the only row
        assert len(v["checklist"]["items"]) == 1
        assert v["checklist"]["items"][0]["step"] == 1
        assert not any("DETERMINISTIC" in w for w in v.get("warnings", []))

        # Control: the same inputs with a live window annotate as usual.
        live_l1 = Layer1Result(status="passed", expired=False)
        v2 = {
            "level": "verified",
            "layer2": {"status": "passed", "details": "all green"},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "passed",
                 "evidence": "restart count rose as expected"},
            ]},
        }
        _apply_deterministic_verdicts(v2, self._KILL_STATE, layer1=live_l1)
        assert len(v2["checklist"]["items"]) == 2
        assert v2["checklist"]["items"][1]["step"] == "rule"

    def test_production_fault_spec_path_end_to_end(self):
        """Finding 11 (coverage gap): production entry points always set
        state["fault_spec"] — the legacy scattered-fields fallback my
        fixtures use is test-only. A FaultSpec field reshuffle (rename,
        type change on params) would silently break the production
        resolution while every test stayed green on the fallback. Pin
        the fault_spec-present branch end to end for both a lift (fill)
        and an annotation (kill)."""
        fill_state = {
            "fault_spec": {
                "namespace": "cms-demo", "scope": "pod",
                "names": ["accounting-7b9c6d8f5-x2k9p"],
                "labels": {"app": "accounting"},
                "fault_target": "disk", "fault_action": "fill",
                "params": {"percent": "85", "path": "/tmp"},
                "duration_seconds": 120, "source": "intent",
            },
            "metric_observations": [
                {"iteration": 1, "metrics": {
                    "Disk usage (overlay)": "11% (1/100)"}},
                {"iteration": 3, "metrics": {
                    "Disk usage (overlay)": "86% (86/100)"}},
            ],
            "fault_handle": {"experiment_uid": "exp"},
        }
        v = {
            "level": "failed",
            "layer2": {"status": "failed", "details": "saw nothing"},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "failed", "evidence": "no change"},
            ]},
        }
        assert _apply_deterministic_verdicts(v, fill_state)
        assert v["layer2"]["status"] == "passed"
        assert v["level"] == "verified"
        assert "86% ≥ injected target 85%" in v["checklist"]["items"][0]["evidence"]

        kill_state = {
            "fault_spec": {
                "namespace": "cms-demo", "scope": "pod",
                "names": ["accounting-x"], "labels": {},
                "fault_target": "process", "fault_action": "kill",
                "params": {}, "duration_seconds": 60, "source": "intent",
            },
            "metric_observations": [
                {"iteration": 1, "metrics": {
                    "RestartCount": 0, "Container ID": "containerd://aaa"}},
                {"iteration": 3, "metrics": {
                    "RestartCount": 2, "Container ID": "containerd://bbb"}},
            ],
            "fault_handle": {"experiment_uid": "exp"},
        }
        v2 = {
            "level": "verified",
            "layer2": {"status": "passed", "details": "ok"},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "passed", "evidence": "restarts seen"},
            ]},
        }
        assert not _apply_deterministic_verdicts(v2, kill_state)
        rows = v2["checklist"]["items"]
        assert len(rows) == 2 and rows[1]["step"] == "rule"

    def test_annotation_row_survives_structured_boundary(self):
        """Finding 4: the [DETERMINISTIC] annotation row (step="rule")
        must survive dict_to_verification_result — ChecklistItem.step
        was a hard int, so the string sentinel raised ValidationError
        (a ValueError subclass) that the boundary's except clause
        swallowed, silently dropping the audit row from the final
        structured result (spec's audit-chain requirement broke at the
        consumption face). Also pins the derived-count sync: the
        annotation bumps total_count (it IS a checklist row) but never
        total_executed (it is not an LLM-executed skill step)."""
        from chaos_agent.agent.nodes.verify._verifier_layer2_parse import (
            dict_to_verification_result,
        )

        v = {
            "level": "verified",
            "layer1": {"status": "passed", "details": ""},
            "layer2": {"status": "passed", "details": "ok"},
            "warnings": [],
            # submit-args shape: the LLM's checklist carries derived counts
            "checklist": {
                "items": [
                    {"step": 1, "status": "passed", "evidence": "restarts seen"},
                ],
                "total_count": 1,
                "skipped_count": 0,
                "non_passed_count": 0,
                "total_executed": 1,
            },
        }
        assert not _apply_deterministic_verdicts(v, self._KILL_STATE)
        cl = v["checklist"]
        assert cl["total_count"] == 2
        assert cl["total_executed"] == 1

        vr = dict_to_verification_result(v)
        rule_rows = [
            it for it in vr.checklist.items if it.step == "rule"
        ]
        assert len(rule_rows) == 1
        assert "[DETERMINISTIC] rule 'process_kill_restarts' passed" in (
            rule_rows[0].evidence
        )
        assert len(vr.checklist.items) == 2
        assert vr.checklist.total_count == 2
        assert vr.checklist.non_passed_count == 0


class TestDeterministicAuditChain:
    """Audit contract (spec: 程序裁决的审计链记录): every rule hit
    records the rule name, the numeric evidence lines (baseline→post)
    and the anchor signal TYPE; lifts and withheld overrides both land
    in warnings — the record must suffice to answer "why did the
    program's verdict hold" (attributability).
    """

    _OBSERVATIONS = [
        {"iteration": 1, "metrics": {
            "RestartCount": 8, "Container ID": "containerd://aaa",
        }},
        {"iteration": 3, "metrics": {
            "RestartCount": 10, "Container ID": "containerd://bbb",
        }},
    ]

    def test_override_evidence_carries_full_audit_fields(self):
        """Q2a audit: the [OVERRIDE] evidence carries the numeric line
        (baseline→post), the container-replacement record and the anchor
        signal; the warning names the rule."""
        v = {
            "level": "failed",
            "layer2": {"status": "failed", "details": ""},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "failed", "evidence": "no signal"},
            ]},
        }
        state = {
            "fault_target": "process",
            "fault_action": "kill",
            "metric_observations": self._OBSERVATIONS,
            "fault_handle": {"experiment_uid": "exp-25"},
        }
        assert _apply_deterministic_verdicts(v, state)
        ev = v["checklist"]["items"][0]["evidence"]
        assert "[OVERRIDE]" in ev
        # Numeric line, baseline→post:
        assert "RestartCount 8 → 10 (Δ+2)" in ev
        # Container-replacement record:
        assert "container ID replaced (2 distinct observed)" in ev
        # Anchor signal type:
        assert "mechanism anchor: fault_handle" in ev
        # The lift is explained in warnings, naming the rule.
        assert any(
            "Programmatic override" in w and "process_kill_restarts" in w
            for w in v["warnings"]
        )

    def test_anchor_signal_type_layer1_success_recorded(self):
        """The anchor TYPE is recorded per signal present — with no
        fault_handle, a Layer-1 Success carries the anchor and the
        evidence names layer1_success instead."""
        v = {
            "level": "verified",
            "layer2": {"status": "passed", "details": ""},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "passed", "evidence": "restarts seen"},
            ]},
        }
        state = {
            "fault_target": "process",
            "fault_action": "kill",
            "metric_observations": self._OBSERVATIONS,
        }
        assert not _apply_deterministic_verdicts(
            v, state, layer1=Layer1Result(status="passed"),
        )  # Q1: no lift, annotation only
        rule_row = v["checklist"]["items"][1]
        assert "[DETERMINISTIC]" in rule_row["evidence"]
        assert "mechanism anchor: layer1_success" in rule_row["evidence"]

    def test_withheld_warning_is_attributable(self):
        """Q2b audit: the withheld warning names the rule AND quotes the
        counter-proof — the reader can trace both sides of the overturn."""
        verdict = verdict_passed("process_kill_restarts", ["RestartCount 8 → 10"])
        v = {
            "level": "failed",
            "layer2": {"status": "failed", "details": ""},
            "warnings": [
                "LLM evidence cites CPU 5%→80% (Δ=+75), "
                "but observation timeline shows no change "
                "(stayed at 5 across 3 iteration(s))",
            ],
            "checklist": {"items": [
                {"step": 1, "status": "failed", "evidence": "no change"},
            ]},
        }
        assert not _synthesize_passed(v, verdict)
        withheld = [w for w in v["warnings"] if "override withheld" in w]
        assert len(withheld) == 1
        assert "process_kill_restarts" in withheld[0]
        assert "counter-proof" in withheld[0]


class TestSubmitArgsAbsenceEvidenceDowngrade:
    """Absence phrasing on ANY failed step forces the objective downgrade."""

    def test_absence_evidence_on_any_failed_step_forces_downgrade(self):
        # Single-tier contract: propagated-effect steps are protected by
        # the prompt contract (mark 'expected'/'not_applicable'), not by a
        # category guard — 'failed' with absence evidence is objective
        # counter-measurement wherever it appears.
        args = {
            "overall": "verified",
            "layer2_status": "passed",
            "primary_evidence_observed": True,
            "checklist": [
                {"step": 1, "status": "failed",
                 "evidence": "timing lag; retry confirmed effect later"},
                {"step": 2, "status": "failed",
                 "evidence": "no observable business impact"},
            ],
        }
        result = _verification_from_submit_args(args)
        assert result["layer2"]["status"] == "partial"
        assert any("inconsistency" in w for w in result["warnings"])


class TestMode2CoverageDisabledContract:
    """When the extractor cannot enumerate steps, the count fallback is off."""

    _SKILL = "# 场景\n## 注入验证\n1.\n"  # counter counts 1, extractor yields []

    def test_count_fallback_does_not_fire_without_parseable_steps(self):
        # Checklist answers nothing numerically, yet no count-based gap or
        # downgrade may fire — coverage validation is DISABLED per prompt.
        v = {
            "level": "verified",
            "layer2": {"status": "passed", "details": ""},
            "warnings": [],
            "checklist": {"items": [
                {"step": 1, "status": "passed", "evidence": "mem elevated"},
            ], "total_executed": 1},
        }
        missing, expected_steps, executed = _apply_step_coverage(
            v, {"skill_case_content": self._SKILL}, None, False,
        )
        assert expected_steps == 0
        assert not missing
        assert v["layer2"]["status"] == "passed"
        assert not any("never attempted" in w for w in v["warnings"])
