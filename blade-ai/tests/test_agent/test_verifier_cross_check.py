"""Tests for the evidence cross-check (structured metric claims).

``cross_check_evidence`` compares the LLM's STRUCTURED ``metric_claims``
(``{metric, baseline, post}``, submitted via ``submit_verification``)
against the metric timeline on ``state.metric_observations``. It
downgrades verified→partial ONLY on a deterministic contradiction: the
declared key exactly matches a timeline metric whose observed series is
CONSTANT (metric provably never moved) at a value that is NOT the claimed
post — so the claimed transition never materialized. A zero first-vs-last
delta alone is NOT enough (D15): self-recovery ``[0,0,2,2,0]`` and a
single-phase snapshot pinned at the post ``[2,2,2]`` both fake it.

Binding discipline (W-77R-5 root fix): WHICH metric a number belongs to
is DECLARED by the LLM as an exact key, never reverse-inferred by the
program via substring matching. The old free-text path
(``_pick_metric_by_context``) mis-bound ``updatedReplicas 3→1`` to the
unmoving ``Replicas`` (``"replicas"`` ⊂ ``"updatedreplicas"``) and falsely
downgraded a correct verdict (task inject-0fc9a9a1). Those helpers are
gone; these tests pin the structured contract that replaces them.
"""
from __future__ import annotations

from chaos_agent.agent.nodes.verify._verifier_layer2_parse import (
    _build_truth_series,
    _check_metric_claims,
    _collect_evidence_text,
    _parse_numeric,
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


# ---------------------------------------------------------------------------
# Truth series (full iteration-ordered numeric observations)
# ---------------------------------------------------------------------------


class TestBuildTruthSeries:
    def _obs(self, iteration: int, **metrics) -> dict:
        return {"iteration": iteration, "metrics": dict(metrics)}

    def test_single_observation_skipped(self):
        # Need at least 2 datapoints to reason about change.
        series = _build_truth_series([self._obs(1, RestartCount="7")])
        assert series == {}

    def test_two_observations_constant(self):
        series = _build_truth_series([
            self._obs(1, RestartCount="7"),
            self._obs(2, RestartCount="7"),
        ])
        assert series == {"RestartCount": [7.0, 7.0]}

    def test_full_series_kept_across_many_iterations(self):
        # The whole series is kept (not reduced to first-vs-last): a
        # self-recovering metric like [7,7,8,8,7] must stay distinguishable
        # from a constant one.
        series = _build_truth_series([
            self._obs(1, RestartCount="7"),
            self._obs(2, RestartCount="7"),
            self._obs(3, RestartCount="8"),
            self._obs(4, RestartCount="8"),
        ])
        assert series == {"RestartCount": [7.0, 7.0, 8.0, 8.0]}

    def test_categorical_metric_skipped(self):
        series = _build_truth_series([
            self._obs(1, Pod_Ready="True"),
            self._obs(2, Pod_Ready="False"),
        ])
        assert series == {}  # non-numeric, no series

    def test_mixed_categorical_and_numeric(self):
        series = _build_truth_series([
            self._obs(1, Pod_Ready="True", RestartCount="7"),
            self._obs(2, Pod_Ready="False", RestartCount="8"),
        ])
        assert series == {"RestartCount": [7.0, 8.0]}


# ---------------------------------------------------------------------------
# Evidence text collection — KEPT: still consumed by the finalize
# counter-evidence replacement gate (_verifier_finalize.py), independent
# of the cross-check binding path.
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
# _check_metric_claims — the structured comparator (unit level)
# ---------------------------------------------------------------------------


class TestCheckMetricClaims:
    def test_exact_key_constant_truth_nonzero_claim_is_contradiction(self):
        # Series pinned at 3 (never moved), claim says it reached 1 → the
        # claimed post never appeared → deterministic contradiction.
        truth = {"Replicas": [3.0, 3.0]}
        contradictions, unknown = _check_metric_claims(
            [{"metric": "Replicas", "baseline": 3, "post": 1}], truth,
        )
        assert len(contradictions) == 1
        assert "Replicas" in contradictions[0]
        assert unknown == []

    def test_nested_key_resolves_exactly_no_misbinding(self):
        # W-77R-5 at unit level: 'replicas' is a substring of
        # 'updatedreplicas', but exact-key lookup binds the claim to
        # updatedReplicas — whose series really moved [3,1] (non-constant,
        # post 1 present) — so no contradiction.
        truth = {"Replicas": [3.0, 3.0], "updatedReplicas": [3.0, 1.0]}
        contradictions, unknown = _check_metric_claims(
            [{"metric": "updatedReplicas", "baseline": 3, "post": 1}], truth,
        )
        assert contradictions == []
        assert unknown == []

    def test_unknown_key_is_warning_not_contradiction(self):
        # Declared key absent from timeline → warn-only, never a
        # contradiction (absence ≠ proof the claim is false).
        truth = {"Replicas": [3.0, 3.0]}
        contradictions, unknown = _check_metric_claims(
            [{"metric": "Phantom", "baseline": 1, "post": 2}], truth,
        )
        assert contradictions == []
        assert len(unknown) == 1
        assert "Phantom" in unknown[0]
        # B-minimal honesty invariant: the warning must explain the
        # structural coverage limitation and direct the auditor to
        # out-of-band confirmation — not cryptically label the claim
        # "hallucinated" when the framework cannot actually tell.
        assert "out-of-band" in unknown[0]

    def test_zero_declared_delta_skipped(self):
        truth = {"Replicas": [3.0, 3.0]}
        contradictions, unknown = _check_metric_claims(
            [{"metric": "Replicas", "baseline": 3, "post": 3}], truth,
        )
        assert contradictions == []
        assert unknown == []

    def test_nonconstant_truth_series_not_flagged(self):
        # Truth moved [7,8] (non-constant); claim says 7→12 — partial
        # mismatch, too noisy to flag.
        truth = {"RestartCount": [7.0, 8.0]}
        contradictions, unknown = _check_metric_claims(
            [{"metric": "RestartCount", "baseline": 7, "post": 12}], truth,
        )
        assert contradictions == []
        assert unknown == []

    def test_constant_third_value_still_contradicts(self):
        # Series pinned at 50 (≠ claimed post 90): the metric provably never
        # reached 90 → contradiction. This preserves the existing
        # hallucination-detection contract (test_contradiction_detected).
        truth = {"CPU usage": [50.0, 50.0]}
        contradictions, unknown = _check_metric_claims(
            [{"metric": "CPU usage", "baseline": 10, "post": 90}], truth,
        )
        assert len(contradictions) == 1
        assert unknown == []

    def test_malformed_claims_ignored(self):
        truth = {"RestartCount": [7.0, 7.0]}
        contradictions, unknown = _check_metric_claims(
            [
                "not-a-dict",
                {"baseline": 1, "post": 2},                       # no metric
                {"metric": "", "baseline": 1, "post": 2},          # empty key
                {"metric": "RestartCount", "baseline": "x", "post": "y"},
            ],
            truth,
        )
        assert contradictions == []
        assert unknown == []


# ---------------------------------------------------------------------------
# cross_check_evidence — main entry point (structured contract)
# ---------------------------------------------------------------------------


class TestCrossCheckEvidence:
    def _result(self, claims, level="verified", l2="passed", details="") -> dict:
        return {
            "level": level,
            "layer2": {"status": l2, "details": details},
            "metric_claims": claims,
            "warnings": [],
        }

    def _obs_timeline(self, *iter_metrics: tuple[int, dict[str, str]]) -> list[dict]:
        return [
            {"iteration": i, "timestamp": "t", "tool_call_id": f"tc-{i}",
             "tool_name": "kubectl", "metrics": m}
            for i, m in iter_metrics
        ]

    def test_no_observations_no_changes(self):
        result = self._result([{"metric": "RestartCount", "baseline": 7, "post": 8}])
        out = cross_check_evidence(result, [])
        assert out["level"] == "verified"
        assert out["warnings"] == []

    def test_no_claims_abstains(self):
        # fail-open: no structured binding → no cross-check, no downgrade,
        # EVEN IF free text cites a change the timeline contradicts. The
        # program no longer parses prose to recover a binding.
        result = {
            "level": "verified",
            "layer2": {"status": "passed", "details": "RestartCount 7→8 (Δ+1)"},
            "warnings": [],
        }
        observations = self._obs_timeline(
            (1, {"RestartCount": "7"}),
            (2, {"RestartCount": "7"}),
        )
        out = cross_check_evidence(result, observations)
        assert out["level"] == "verified"
        assert out["warnings"] == []

    def test_claim_matches_truth_no_downgrade(self):
        result = self._result([{"metric": "RestartCount", "baseline": 7, "post": 8}])
        observations = self._obs_timeline(
            (1, {"RestartCount": "7"}),
            (3, {"RestartCount": "8"}),
        )
        out = cross_check_evidence(result, observations)
        assert out["level"] == "verified"
        assert out["layer2"]["status"] == "passed"
        assert out["warnings"] == []

    def test_hallucinated_change_downgrades_to_partial(self):
        # Core E2 win, now via structured claim: LLM declares 7→8 but the
        # timeline shows RestartCount stayed at 7 across every iteration.
        result = self._result([{"metric": "RestartCount", "baseline": 7, "post": 8}])
        observations = self._obs_timeline(
            (1, {"RestartCount": "7"}),
            (2, {"RestartCount": "7"}),
            (3, {"RestartCount": "7"}),
        )
        out = cross_check_evidence(result, observations)
        assert out["level"] == "partial"
        assert out["layer2"]["status"] == "partial"
        assert any("RestartCount" in w for w in out["warnings"])
        assert any("downgraded" in w.lower() for w in out["warnings"])

    def test_claim_no_change_not_flagged(self):
        result = self._result([{"metric": "RestartCount", "baseline": 7, "post": 7}])
        observations = self._obs_timeline(
            (1, {"RestartCount": "7"}),
            (2, {"RestartCount": "7"}),
        )
        out = cross_check_evidence(result, observations)
        assert out["level"] == "verified"
        assert out["warnings"] == []

    def test_unknown_key_warns_but_does_not_downgrade(self):
        # Declared metric absent from timeline → warn-only (fail-open).
        result = self._result([{"metric": "Phantom", "baseline": 1, "post": 5}])
        observations = self._obs_timeline(
            (1, {"RestartCount": "7"}),
            (2, {"RestartCount": "7"}),
        )
        out = cross_check_evidence(result, observations)
        assert out["level"] == "verified"  # NOT downgraded
        assert any("Phantom" in w and "absent" in w for w in out["warnings"])
        assert not any("downgraded" in w.lower() for w in out["warnings"])

    def test_nonzero_truth_delta_only_flags_zero_truth(self):
        result = self._result([{"metric": "RestartCount", "baseline": 7, "post": 12}])
        observations = self._obs_timeline(
            (1, {"RestartCount": "7"}),
            (2, {"RestartCount": "8"}),  # real delta +1
        )
        out = cross_check_evidence(result, observations)
        assert out["level"] == "verified"
        assert out["warnings"] == []

    def test_status_already_failed_no_further_downgrade(self):
        result = self._result(
            [{"metric": "RestartCount", "baseline": 7, "post": 8}],
            level="unverified", l2="failed",
        )
        observations = self._obs_timeline(
            (1, {"RestartCount": "7"}),
            (2, {"RestartCount": "7"}),
        )
        out = cross_check_evidence(result, observations)
        assert out["layer2"]["status"] == "failed"
        assert any("RestartCount" in w for w in out["warnings"])

    def test_observations_none_safe(self):
        result = self._result([{"metric": "RestartCount", "baseline": 7, "post": 8}])
        out = cross_check_evidence(result, None)
        assert out["level"] == "verified"


# ---------------------------------------------------------------------------
# W-77R-5 regression — the real inject-0fc9a9a1 false downgrade
# ---------------------------------------------------------------------------


class TestW77R5Regression:
    """task inject-0fc9a9a1: a CORRECT ``updatedReplicas 3→1`` claim (with
    ``Replicas`` stuck at 3) was mis-bound to ``Replicas`` by the old
    substring matcher and falsely downgraded verified→partial. Structured
    exact-key binding makes the mis-binding structurally impossible."""

    def _sts_timeline(self) -> list[dict]:
        # Replicas stays 3 the whole time; updatedReplicas rolls 3→1.
        rows = ["3", "3", "2", "1", "1", "1"]
        return [
            {"iteration": i, "timestamp": f"t{i}", "tool_call_id": f"tc-{i}",
             "tool_name": "kubectl",
             "metrics": {"Replicas": "3", "updatedReplicas": upd}}
            for i, upd in enumerate(rows, start=1)
        ]

    def test_correct_updated_replicas_claim_not_downgraded(self):
        result = {
            "level": "verified",
            "layer2": {
                "status": "passed",
                "details": "updatedReplicas 3→1 with replicas=3/ready=3",
            },
            "metric_claims": [
                {"metric": "updatedReplicas", "baseline": 3, "post": 1},
            ],
            "warnings": [],
        }
        out = cross_check_evidence(result, self._sts_timeline())
        assert out["level"] == "verified", (
            f"W-77R-5 regression: correct updatedReplicas 3→1 was falsely "
            f"downgraded. warnings={out['warnings']}"
        )
        assert out["layer2"]["status"] == "passed"
        assert not any("downgraded" in w.lower() for w in out["warnings"])

    def test_genuine_replicas_hallucination_still_caught(self):
        # The fix must not neuter real detection: a claim that Replicas
        # itself moved 3→1 (it did NOT — it stayed 3) is still caught.
        result = {
            "level": "verified",
            "layer2": {"status": "passed", "details": "Replicas dropped 3→1"},
            "metric_claims": [
                {"metric": "Replicas", "baseline": 3, "post": 1},
            ],
            "warnings": [],
        }
        out = cross_check_evidence(result, self._sts_timeline())
        assert out["level"] == "partial"
        assert any("Replicas" in w for w in out["warnings"])


# ---------------------------------------------------------------------------
# Former Bug B (overlay/nodefs alias collision) under structured binding
# ---------------------------------------------------------------------------


class TestNodefsExactKeyScenario:
    """The old alias collision (overlay/nodefs both → 'disk usage') is moot
    under exact-key binding: the LLM declares the fully-qualified key."""

    def test_nodefs_change_not_falsely_flagged(self):
        result = {
            "level": "verified",
            "layer2": {
                "status": "passed",
                "details": "Disk usage (nodefs) 50%→80% confirms injection",
            },
            "metric_claims": [
                {"metric": "Disk usage (nodefs)", "baseline": 50, "post": 80},
            ],
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
        assert out["level"] == "verified"
        assert out["layer2"]["status"] == "passed"
        assert out["warnings"] == []


# ---------------------------------------------------------------------------
# D15 regression — the two real false-downgrade modes from Phase 4 6.1
# (fixtures are the actual metric_observations series read back from
# checkpoints.db for runs inject-7c25e537 / inject-92493ad8).
# ---------------------------------------------------------------------------


class TestD15ModeRegression:
    """Both runs had a CORRECT ``Partition 0→2`` claim falsely downgraded
    verified→partial by the old first-vs-last delta==0 rule. Each mode is a
    different timeline shape hitting the same consumer (L186)."""

    def _result(self) -> dict:
        return {
            "level": "verified",
            "layer2": {"status": "passed", "details": "Partition advanced 0→2"},
            "metric_claims": [
                {"metric": "Partition", "baseline": 0, "post": 2},
            ],
            "warnings": [],
        }

    def _timeline(self, partition_values: list[str]) -> list[dict]:
        return [
            {"iteration": i, "timestamp": f"t{i}", "tool_call_id": f"tc-{i}",
             "tool_name": "kubectl", "metrics": {"Partition": v}}
            for i, v in enumerate(partition_values, start=1)
        ]

    def test_mode_a_self_recovering_not_downgraded(self):
        # inject-7c25e537: injection advanced Partition 0→2, then recovery
        # pulled it back to 0 — full three-phase series [0,0,0,0,2,2,0].
        # Old rule: first==last==0 → delta 0 → "stayed at 0" → false
        # downgrade. New rule: series is non-constant (2 appeared) → the
        # claim is corroborated → verified preserved.
        out = cross_check_evidence(
            self._result(),
            self._timeline(["0", "0", "0", "0", "2", "2", "0"]),
        )
        assert out["level"] == "verified", (
            f"Mode A regression: self-recovering Partition 0→2 was falsely "
            f"downgraded. warnings={out['warnings']}"
        )
        assert not any("downgraded" in w.lower() for w in out["warnings"])

    def test_mode_b_single_phase_snapshot_not_downgraded(self):
        # inject-92493ad8: the pipe-delimited baseline read was dropped by
        # the extractor, leaving only the post-injection phase [2,2,2]. Old
        # rule: first==last==2 → delta 0 → "stayed at 2" → false downgrade.
        # New rule: series is constant AT the claimed post (2) → the
        # endpoint is corroborated, delta=0 is a data-gap artifact →
        # abstain, verified preserved.
        out = cross_check_evidence(
            self._result(), self._timeline(["2", "2", "2"]),
        )
        assert out["level"] == "verified", (
            f"Mode B regression: single-phase snapshot Partition 0→2 was "
            f"falsely downgraded. warnings={out['warnings']}"
        )
        assert not any("downgraded" in w.lower() for w in out["warnings"])

    def test_genuine_hallucination_still_downgrades(self):
        # Detection power must NOT regress: a claim whose post never appears
        # in a constant timeline is still caught. Partition pinned at 0 the
        # whole time, LLM claims it reached 2 → contradiction.
        result = {
            "level": "verified",
            "layer2": {"status": "passed", "details": "Partition advanced 0→2"},
            "metric_claims": [
                {"metric": "Partition", "baseline": 0, "post": 2},
            ],
            "warnings": [],
        }
        out = cross_check_evidence(result, self._timeline(["0", "0", "0"]))
        assert out["level"] == "partial"
        assert any("Partition" in w for w in out["warnings"])
