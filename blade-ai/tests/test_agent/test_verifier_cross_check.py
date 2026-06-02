"""Tests for E2 Phase 3 — evidence cross-check.

``cross_check_evidence`` compares LLM-cited baseline→post deltas in the
verification result against the structured metric timeline on
``state.metric_observations``. It downgrades verified→partial
and appends warnings when the LLM claims a change but the timeline
shows none.
"""
from __future__ import annotations

from chaos_agent.agent.nodes._verifier_layer2_parse import (
    _build_truth_deltas,
    _collect_evidence_text,
    _metric_alias,
    _parse_numeric,
    _pick_metric_by_context,
    cross_check_evidence,
)


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------


class TestParseNumeric:
    def test_plain_int(self):
        assert _parse_numeric("7") == 7.0

    def test_float(self):
        assert _parse_numeric("13.5") == 13.5

    def test_percent_suffix(self):
        assert _parse_numeric("26%") == 26.0

    def test_millicore_suffix(self):
        assert _parse_numeric("250m") == 250.0

    def test_memory_suffix(self):
        assert _parse_numeric("512Mi") == 512.0

    def test_categorical_returns_none(self):
        assert _parse_numeric("True") is None
        assert _parse_numeric("OOMKilled") is None

    def test_empty_returns_none(self):
        assert _parse_numeric("") is None
        assert _parse_numeric(None) is None  # type: ignore[arg-type]


class TestMetricAlias:
    def test_drops_parens(self):
        assert _metric_alias("Disk usage (overlay)") == "disk usage"

    def test_no_parens_just_lowercased(self):
        assert _metric_alias("RestartCount") == "restartcount"


# ---------------------------------------------------------------------------
# Truth deltas
# ---------------------------------------------------------------------------


class TestBuildTruthDeltas:
    def _obs(self, iteration: int, **metrics) -> dict:
        return {"iteration": iteration, "metrics": dict(metrics)}

    def test_single_observation_skipped(self):
        # Need at least 2 datapoints to compute a delta.
        deltas = _build_truth_deltas([self._obs(1, RestartCount="7")])
        assert deltas == {}

    def test_two_observations_no_change(self):
        deltas = _build_truth_deltas([
            self._obs(1, RestartCount="7"),
            self._obs(2, RestartCount="7"),
        ])
        assert deltas == {"RestartCount": (7.0, 7.0)}

    def test_baseline_first_post_last_across_many_iterations(self):
        deltas = _build_truth_deltas([
            self._obs(1, RestartCount="7"),
            self._obs(2, RestartCount="7"),
            self._obs(3, RestartCount="8"),
            self._obs(4, RestartCount="8"),
        ])
        assert deltas == {"RestartCount": (7.0, 8.0)}

    def test_categorical_metric_skipped(self):
        deltas = _build_truth_deltas([
            self._obs(1, Pod_Ready="True"),
            self._obs(2, Pod_Ready="False"),
        ])
        assert deltas == {}  # non-numeric, no delta

    def test_mixed_categorical_and_numeric(self):
        deltas = _build_truth_deltas([
            self._obs(1, Pod_Ready="True", RestartCount="7"),
            self._obs(2, Pod_Ready="False", RestartCount="8"),
        ])
        assert deltas == {"RestartCount": (7.0, 8.0)}


# ---------------------------------------------------------------------------
# Evidence text collection
# ---------------------------------------------------------------------------


class TestCollectEvidenceText:
    def test_includes_layer2_details(self):
        result = {"layer2": {"details": "Disk usage 10%→13%, restart not observed"}}
        text = _collect_evidence_text(result)
        assert "10%→13%" in text

    def test_includes_checklist_evidence(self):
        result = {
            "layer2": {"details": ""},
            "checklist": {"items": [
                {"step": 1, "status": "passed",
                 "evidence": "RestartCount 7 → 8"},
                {"step": 2, "status": "passed",
                 "evidence": "Disk usage stable at 10%"},
            ]},
        }
        text = _collect_evidence_text(result)
        assert "RestartCount 7 → 8" in text
        assert "Disk usage stable at 10%" in text

    def test_empty_result_returns_empty(self):
        assert _collect_evidence_text({}) == ""


# ---------------------------------------------------------------------------
# cross_check_evidence — main entry point
# ---------------------------------------------------------------------------


class TestCrossCheckEvidence:
    def _verified_result(self, evidence: str) -> dict:
        return {
            "level": "verified",
            "layer2": {"status": "passed", "details": evidence},
            "warnings": [],
        }

    def _obs_timeline(self, *iter_metrics: tuple[int, dict[str, str]]) -> list[dict]:
        return [
            {"iteration": i, "timestamp": "t", "tool_call_id": f"tc-{i}",
             "tool_name": "kubectl", "metrics": m}
            for i, m in iter_metrics
        ]

    def test_no_observations_no_changes(self):
        # Empty timeline → can't cross-check → pass through.
        result = self._verified_result("RestartCount 7→8 (Δ+1)")
        out = cross_check_evidence(result, [])
        assert out["level"] == "verified"
        assert out["warnings"] == []

    def test_llm_claim_matches_truth_no_downgrade(self):
        # LLM says 7→8, truth says 7→8 — no contradiction.
        result = self._verified_result("RestartCount 7→8 (Δ+1)")
        observations = self._obs_timeline(
            (1, {"RestartCount": "7"}),
            (3, {"RestartCount": "8"}),
        )
        out = cross_check_evidence(result, observations)
        assert out["level"] == "verified"
        assert out["layer2"]["status"] == "passed"
        assert out["warnings"] == []

    def test_llm_hallucinates_change_downgrades_to_partial(self):
        # LLM claims RestartCount 7→8 but truth shows it stayed at 7.
        # This is the core E2 win: detect baseline→post hallucination.
        result = self._verified_result("RestartCount 7→8 (Δ+1)")
        observations = self._obs_timeline(
            (1, {"RestartCount": "7"}),
            (2, {"RestartCount": "7"}),
            (3, {"RestartCount": "7"}),
        )
        out = cross_check_evidence(result, observations)
        assert out["level"] == "partial"
        assert out["layer2"]["status"] == "partial"
        # Two warnings: the contradiction itself + the downgrade rationale.
        assert any("RestartCount" in w for w in out["warnings"])
        assert any("downgraded" in w.lower() for w in out["warnings"])

    def test_llm_claims_no_change_not_flagged(self):
        # When LLM says "Δ=0" (e.g. "7→7"), no claim to disprove.
        result = self._verified_result("RestartCount stayed at 7→7")
        observations = self._obs_timeline(
            (1, {"RestartCount": "7"}),
            (2, {"RestartCount": "7"}),
        )
        out = cross_check_evidence(result, observations)
        assert out["level"] == "verified"
        assert out["warnings"] == []

    def test_disk_usage_with_percent_suffix(self):
        # Format variation: "disk usage 10%→13%" — percent sign should
        # not break the regex.
        result = self._verified_result("Disk usage 10%→13%, fault confirmed")
        observations = self._obs_timeline(
            (1, {"Disk usage (overlay)": "10%"}),
            (2, {"Disk usage (overlay)": "10%"}),
        )
        out = cross_check_evidence(result, observations)
        assert out["level"] == "partial"
        assert any("Disk usage" in w for w in out["warnings"])

    def test_arrow_alternatives_recognized(self):
        # The regex should accept "→", "->", and " to " as the arrow.
        for arrow in ("→", "->", " to "):
            text = f"RestartCount 7{arrow}8 (Δ+1)"
            result = self._verified_result(text)
            observations = self._obs_timeline(
                (1, {"RestartCount": "7"}),
                (2, {"RestartCount": "7"}),
            )
            out = cross_check_evidence(result, observations)
            assert out["level"] == "partial", (
                f"failed to flag contradiction with arrow {arrow!r}"
            )

    def test_no_nearby_metric_name_skipped(self):
        # Bare numbers without a recognised metric name nearby — must
        # NOT flag (could be timestamps, ports, exit codes, etc.).
        result = self._verified_result(
            "The pod was checked at 14→15 minutes after injection"
        )
        observations = self._obs_timeline(
            (1, {"RestartCount": "7"}),
            (2, {"RestartCount": "7"}),
        )
        out = cross_check_evidence(result, observations)
        # "14→15" has no metric name in the lookback context, so it's
        # ignored. No contradiction.
        assert out["level"] == "verified"
        assert out["warnings"] == []

    def test_checklist_evidence_also_scanned(self):
        # The cross-check should pick up deltas from checklist items
        # too, not just layer2.details.
        result = {
            "level": "verified",
            "layer2": {"status": "passed", "details": "All checks passed"},
            "checklist": {"items": [
                {"step": 1, "status": "passed",
                 "evidence": "RestartCount 7→8 confirms injection"},
            ]},
            "warnings": [],
        }
        observations = self._obs_timeline(
            (1, {"RestartCount": "7"}),
            (2, {"RestartCount": "7"}),
        )
        out = cross_check_evidence(result, observations)
        assert out["level"] == "partial"

    def test_partial_truth_value_only_flags_zero_truth_delta(self):
        # When truth delta is non-zero (e.g. +1) and LLM cites a
        # different delta (+5), we DON'T flag — too noisy. Only the
        # "LLM claims change, truth has none" case is in scope.
        result = self._verified_result("RestartCount 7→12 confirmed (Δ+5)")
        observations = self._obs_timeline(
            (1, {"RestartCount": "7"}),
            (2, {"RestartCount": "8"}),  # real delta +1
        )
        out = cross_check_evidence(result, observations)
        # Conservative MVP: don't downgrade on partial mismatch.
        assert out["level"] == "verified"
        assert out["warnings"] == []

    def test_status_already_failed_no_further_downgrade(self):
        # If layer2 was already failed, no need to further downgrade.
        result = {
            "level": "unverified",
            "layer2": {
                "status": "failed",
                "details": "RestartCount 7→8 claimed but unclear",
            },
            "warnings": [],
        }
        observations = self._obs_timeline(
            (1, {"RestartCount": "7"}),
            (2, {"RestartCount": "7"}),
        )
        out = cross_check_evidence(result, observations)
        # Status stays failed, but the warning is still added.
        assert out["layer2"]["status"] == "failed"
        assert any("RestartCount" in w for w in out["warnings"])

    def test_observations_none_safe(self):
        # state.metric_observations defaults to None; must not crash.
        result = self._verified_result("anything")
        out = cross_check_evidence(result, None)
        assert out["level"] == "verified"


# ---------------------------------------------------------------------------
# _pick_metric_by_context — Bug B regression
# ---------------------------------------------------------------------------


class TestPickMetricByContext:
    """Two-priority metric resolution: full-name > alias > None.

    Regression for Bug B — when overlay/nodefs both alias to "disk
    usage", the dict-order-first match used to falsely flag a real
    nodefs change as a contradiction (because it pulled overlay's
    no-change truth).
    """

    def _truth(self, **pairs) -> dict[str, tuple[float, float]]:
        return dict(pairs)

    def test_full_name_wins_over_alias(self):
        truth = self._truth(**{
            "Disk usage (overlay)": (10.0, 10.0),
            "Disk usage (nodefs)": (50.0, 80.0),
        })
        # Context explicitly mentions nodefs — full name wins.
        assert _pick_metric_by_context(
            "the disk usage (nodefs) went from", truth,
        ) == "Disk usage (nodefs)"

    def test_unambiguous_alias_picks_only_candidate(self):
        truth = self._truth(RestartCount=(7.0, 7.0))
        # No parens in this metric name; alias == full name.
        assert _pick_metric_by_context(
            "restartcount went up", truth,
        ) == "RestartCount"

    def test_colliding_aliases_returns_none(self):
        # Both "(overlay)" and "(nodefs)" alias to "disk usage" — when
        # the evidence context only says "disk usage" with no
        # qualifier, refuse to guess. Returning None ⇒ skip
        # cross-check ⇒ no false contradiction.
        truth = self._truth(**{
            "Disk usage (overlay)": (10.0, 10.0),
            "Disk usage (nodefs)": (50.0, 80.0),
        })
        assert _pick_metric_by_context("disk usage was", truth) is None

    def test_no_context_match_returns_none(self):
        truth = self._truth(RestartCount=(7.0, 7.0))
        assert _pick_metric_by_context("nothing related here", truth) is None


class TestBugBNodefsScenario:
    """End-to-end Bug B repro: real nodefs injection must not be
    falsely downgraded because overlay shares the alias."""

    def test_nodefs_change_not_falsely_flagged(self):
        result = {
            "level": "verified",
            "layer2": {
                "status": "passed",
                "details": "Disk usage (nodefs) 50%→80% confirms injection",
            },
            "warnings": [],
        }
        observations = [
            {"iteration": 1, "timestamp": "t1", "tool_call_id": "tc-1",
             "tool_name": "kubectl",
             "metrics": {"Disk usage (overlay)": "10%",
                         "Disk usage (nodefs)": "50%"}},
            {"iteration": 2, "timestamp": "t2", "tool_call_id": "tc-2",
             "tool_name": "kubectl",
             "metrics": {"Disk usage (overlay)": "10%",
                         "Disk usage (nodefs)": "80%"}},
        ]
        out = cross_check_evidence(result, observations)
        # nodefs change is real (50→80); overlay didn't move (10→10);
        # evidence cites nodefs explicitly → full-name match → NO
        # contradiction. Verdict stays verified.
        assert out["level"] == "verified", (
            f"Bug B regression: nodefs change ({observations[0]['metrics']['Disk usage (nodefs)']}"
            f" → {observations[1]['metrics']['Disk usage (nodefs)']}) "
            f"was falsely flagged as contradiction. Got warnings: {out['warnings']}"
        )
        assert out["layer2"]["status"] == "passed"
        assert out["warnings"] == []

    def test_ambiguous_disk_usage_skipped_gracefully(self):
        # Evidence says generic "disk usage 10→13%" — could refer to
        # either overlay or nodefs. We refuse to guess; no warning,
        # no downgrade. Better than a false flag.
        result = {
            "level": "verified",
            "layer2": {
                "status": "passed",
                "details": "Disk usage 10%→13% confirms injection",
            },
            "warnings": [],
        }
        observations = [
            {"iteration": 1, "timestamp": "t1", "tool_call_id": "tc-1",
             "tool_name": "kubectl",
             "metrics": {"Disk usage (overlay)": "10%",
                         "Disk usage (nodefs)": "50%"}},
            {"iteration": 2, "timestamp": "t2", "tool_call_id": "tc-2",
             "tool_name": "kubectl",
             "metrics": {"Disk usage (overlay)": "10%",
                         "Disk usage (nodefs)": "50%"}},
        ]
        out = cross_check_evidence(result, observations)
        # Both truths show no change; LLM claims +3; ambiguous metric
        # name → skip cross-check → conservative pass-through.
        assert out["level"] == "verified"
        assert out["warnings"] == []
