"""Tests for _deterministic_rules.py — Layer 1.5 skeleton.

Anchors the frozen contract of the deterministic verdict layer:
  * the verdict enum is CLOSED to {passed, unknown} — no downgrade branch
    exists anywhere in the module's public surface;
  * families without a declared rule resolve to zero rules (the finalize
    pipeline then applies zero rule interference).
"""

import pytest

from chaos_agent.agent.nodes.verify import _deterministic_rules as rules_mod
from chaos_agent.agent.nodes.verify._deterministic_rules import (
    CPU_FULLLOAD_RULE,
    DEPLOYMENT_SCALE_DOWN_RULE,
    DISK_FILL_RULE,
    MEM_LOAD_RULE,
    PROCESS_KILL_RULE,
    STS_PARTITION_RULE,
    DeterministicRule,
    DeterministicVerdict,
    RuleContext,
    verdict_passed,
    verdict_unknown,
)
from chaos_agent.agent.nodes.verify._verification_profiles import (
    resolve_deterministic_rules,
)
from chaos_agent.agent.nodes.verify._metric_extractor import extract_metrics


class TestVerdictEnumClosed:
    """Spec: the output enum is closed to passed / unknown — there is no
    programmatic downgrade, enforced by the type itself."""

    @pytest.mark.parametrize("kind", ["failed", "partial", "recovered_before_observation", "FAILED", ""])
    def test_no_downgrade_kind_is_constructible(self, kind):
        with pytest.raises(ValueError, match=r"'passed' or 'unknown'"):
            DeterministicVerdict(kind=kind)

    def test_no_downgrade_factory_exists_in_module_surface(self):
        # The only verdict factories in the module namespace are the two
        # closed-enum constructors — a downgrade branch would have to be
        # a factory or a direct construction, and both are absent/blocked.
        factories = [
            name for name in dir(rules_mod)
            if name.startswith("verdict_")
        ]
        assert sorted(factories) == ["verdict_passed", "verdict_unknown"]

    def test_passed_factory_shape(self):
        v = verdict_passed("some_rule", ["line1", "line2"])
        assert v.is_passed
        assert v.kind == "passed"
        assert v.rule_name == "some_rule"
        assert v.evidence_lines == ("line1", "line2")
        assert v.reason == ""

    def test_unknown_factory_shape(self):
        v = verdict_unknown("numbers absent")
        assert not v.is_passed
        assert v.kind == "unknown"
        assert v.reason == "numbers absent"
        assert v.rule_name == ""
        assert v.evidence_lines == ()


class TestRuleContextAnchoring:
    """Design decision 5: mechanism anchor is a precondition of passed —
    the context names which of the two signals is in play."""

    def test_fault_handle_wins_first(self):
        ctx = RuleContext(
            fault_handle={"experiment_uid": "x"},
            layer1_passed=True,
        )
        assert ctx.anchoring_signal() == "fault_handle"

    def test_layer1_when_no_handle(self):
        ctx = RuleContext(layer1_passed=True)
        assert ctx.anchoring_signal() == "layer1_success"

    def test_no_anchor(self):
        assert RuleContext().anchoring_signal() == ""


class TestRuleResolution:
    """Families without a declared rule must resolve to zero rules — the
    guarantee behind 'no-rule family: zero behaviour change'."""

    def test_process_kill_matches(self):
        resolved = resolve_deterministic_rules("process", "kill")
        assert resolved == (PROCESS_KILL_RULE,)

    def test_disk_fill_matches(self):
        resolved = resolve_deterministic_rules("disk", "fill")
        assert resolved == (DISK_FILL_RULE,)

    def test_cpu_fullload_resolves_to_uncalibrated_placeholder(self):
        resolved = resolve_deterministic_rules("cpu", "fullload")
        assert resolved == (CPU_FULLLOAD_RULE,)
        # Declared but pinned to unknown — the declaration is a typed
        # extension point, never a runtime behaviour change.
        assert resolved[0].evaluate(RuleContext(
            metric_observations=[{"iteration": 1, "metrics": {"CPU": "95"}}],
            fault_handle={"experiment_uid": "x"},
        )).kind == "unknown"

    def test_mem_load_resolves_to_uncalibrated_placeholder(self):
        resolved = resolve_deterministic_rules("mem", "load")
        assert resolved == (MEM_LOAD_RULE,)
        assert resolved[0].evaluate(RuleContext(
            metric_observations=[{"iteration": 1, "metrics": {"MEM": "90"}}],
            fault_handle={"experiment_uid": "x"},
        )).kind == "unknown"

    @pytest.mark.parametrize(
        "target,action",
        [
            ("network", "loss"),    # undeclared family
            ("network", "delay"),
            ("process", "hang"),    # declared family, undeclared action
            ("node", "fill"),
            ("cpu", "burn"),       # declared family, undeclared action
            ("disk", "burn"),      # declared family, action carries no rule
            ("mem", "fill"),
            ("", "burn"),           # missing identity
            ("disk", ""),
            (None, "burn"),
            ("disk", None),
        ],
    )
    def test_undeclared_families_resolve_empty(self, target, action):
        assert resolve_deterministic_rules(target, action) == ()

    def test_declared_rules_carry_closed_verdict_contract(self):
        # Every rule declared on a profile must evaluate into the closed
        # enum — smoke-test each declared rule with an empty context (the
        # cheapest input that must yield a verdict, never raise).
        from chaos_agent.agent.nodes.verify._verification_profiles import (
            resolve_verification_profile,
        )
        for target in ("disk", "process", "network", "cpu", "mem", "node"):
            for rule in resolve_verification_profile(target).deterministic_rules():
                assert isinstance(rule, DeterministicRule)
                verdict = rule.evaluate(RuleContext())
                assert isinstance(verdict, DeterministicVerdict)
                assert verdict.kind in ("passed", "unknown")


class TestProcessKillRuleEvaluation:
    """Four-branch contract of the process kill rule (tasks 3.2):
    both legs + anchor → passed; missing anchor / missing numbers /
    unreplaced container → unknown (never a downgrade)."""

    @staticmethod
    def _obs(iteration: int, restarts: int, container_id: str) -> dict:
        return {
            "iteration": iteration,
            "timestamp": f"2026-09-06T02:3{iteration}:00Z",
            "tool_call_id": f"call_{iteration}",
            "tool_name": "kubectl",
            "metrics": {
                "RestartCount": str(restarts),
                "Container ID": container_id,
                "Pod Ready": "True",
            },
        }

    def test_anchor_and_numbers_both_present_passed(self):
        ctx = RuleContext(
            metric_observations=[
                self._obs(1, 8, "containerd://aaaaaaaa"),
                self._obs(5, 10, "containerd://bbbbbbbb"),
            ],
            fault_handle={"kind": "native", "method": "kubectl_native"},
        )
        verdict = PROCESS_KILL_RULE.evaluate(ctx)
        assert verdict.is_passed
        assert verdict.rule_name == "process_kill_restarts"
        # Evidence carries the rule name, the numeric line (baseline→post)
        # and the anchor signal type — the audit contract.
        assert any("8 → 10" in line and "Δ+2" in line for line in verdict.evidence_lines)
        assert any("mechanism anchor: fault_handle" in line for line in verdict.evidence_lines)

    def test_no_anchor_returns_unknown(self):
        ctx = RuleContext(
            metric_observations=[
                self._obs(1, 8, "containerd://aaaaaaaa"),
                self._obs(5, 10, "containerd://bbbbbbbb"),
            ],
            # no fault_handle, no layer1 — the numeric delta
            # may be an unrelated OOMKill (#25-R iter-5 attribution nuance)
        )
        verdict = PROCESS_KILL_RULE.evaluate(ctx)
        assert verdict.kind == "unknown"
        assert "anchor" in verdict.reason

    def test_numbers_absent_returns_unknown(self):
        ctx = RuleContext(
            metric_observations=[self._obs(1, 8, "containerd://aaaaaaaa")],
            fault_handle={"kind": "native"},
        )
        verdict = PROCESS_KILL_RULE.evaluate(ctx)
        assert verdict.kind == "unknown"
        assert "fewer than 2" in verdict.reason

    def test_container_id_unchanged_returns_unknown(self):
        ctx = RuleContext(
            metric_observations=[
                self._obs(1, 8, "containerd://aaaaaaaa"),
                self._obs(5, 10, "containerd://aaaaaaaa"),
            ],
            fault_handle={"kind": "native"},
        )
        verdict = PROCESS_KILL_RULE.evaluate(ctx)
        assert verdict.kind == "unknown"
        assert "no replacement" in verdict.reason

    def test_layer1_success_is_a_valid_anchor(self):
        ctx = RuleContext(
            metric_observations=[
                self._obs(1, 8, "containerd://aaaaaaaa"),
                self._obs(5, 10, "containerd://bbbbbbbb"),
            ],
            layer1_passed=True,
        )
        verdict = PROCESS_KILL_RULE.evaluate(ctx)
        assert verdict.is_passed
        assert any("mechanism anchor: layer1_success" in line for line in verdict.evidence_lines)

    def test_delta_below_signature_returns_unknown(self):
        # Δ+1: a single restart can be an unrelated transient — not the
        # calibrated kill signature.
        ctx = RuleContext(
            metric_observations=[
                self._obs(1, 8, "containerd://aaaaaaaa"),
                self._obs(5, 9, "containerd://bbbbbbbb"),
            ],
            fault_handle={"kind": "native"},
        )
        verdict = PROCESS_KILL_RULE.evaluate(ctx)
        assert verdict.kind == "unknown"
        assert "below" in verdict.reason

    def test_iteration_ordering_is_respected(self):
        # Observations arrive post-first (compaction can reorder arrival);
        # the timeline semantics is iteration-ordered earliest→latest.
        ctx = RuleContext(
            metric_observations=[
                self._obs(5, 17, "containerd://bbbbbbbb"),
                self._obs(1, 15, "containerd://aaaaaaaa"),
            ],
            fault_handle={"kind": "native"},
        )
        verdict = PROCESS_KILL_RULE.evaluate(ctx)
        assert verdict.is_passed
        assert any("15 → 17" in line for line in verdict.evidence_lines)

    def test_zero_baseline_is_kept_in_the_series(self):
        # Re-audit finding 5: RestartCount 0 is the MOST common production
        # baseline (the calibrated #25 run happened to sit on a non-zero
        # one). A truthiness skip dropped every zero reading, starving the
        # series (len<2 → unknown on the textbook kill shape) or shifting
        # the baseline to the first non-zero reading.
        ctx = RuleContext(
            metric_observations=[
                self._obs(1, 0, "containerd://aaaaaaaa"),
                self._obs(3, 2, "containerd://bbbbbbbb"),
            ],
            fault_handle={"kind": "native"},
        )
        verdict = PROCESS_KILL_RULE.evaluate(ctx)
        assert verdict.is_passed
        assert any("0 → 2" in line for line in verdict.evidence_lines)

    def test_blank_readings_are_still_skipped(self):
        # The zero fix must not admit blank/absent readings either — those
        # stay skipped so a single real observation cannot masquerade as a
        # series.
        ctx = RuleContext(
            metric_observations=[
                self._obs(1, "", "containerd://aaaaaaaa"),
                {"iteration": 2, "metrics": {}},
                self._obs(3, 3, "containerd://bbbbbbbb"),
            ],
            fault_handle={"kind": "native"},
        )
        verdict = PROCESS_KILL_RULE.evaluate(ctx)
        assert not verdict.is_passed
        assert "fewer than 2" in verdict.reason


class TestProcessKillFixtureReplay:
    """#25 / #25-R recorded shapes replayed through the rule (tasks 3.3).

    Both live runs' LLM verdicts were `verified` with the numbers below;
    the rule must reach the SAME conclusion on the SAME numbers — that
    agreement is the whole point (same evidence, one conclusion)."""

    @staticmethod
    def _obs(iteration: int, restarts: int, container_id: str, ready: str = "True") -> dict:
        return {
            "iteration": iteration,
            "timestamp": f"2026-09-06T02:{iteration:02d}:00Z",
            "tool_call_id": f"call_{iteration}",
            "tool_name": "kubectl",
            "metrics": {
                "RestartCount": str(restarts),
                "Container ID": container_id,
                "Pod Ready": ready,
                "Last termination reason": "Error",
            },
        }

    def test_case_25_shape_replays_verified(self):
        # #25 (2026-09-06): baseline RESTARTS=8, window-opening 90s Δ+2
        # (8→10), containerID replaced, exit 137 recorded by the pod.
        ctx = RuleContext(
            metric_observations=[
                # baseline probe (planning phase, iteration ~2)
                self._obs(2, 8, "containerd://5f8c1a9d"),
                # verify iterations 1-3 re-describe; iter 1 already sees Δ+2
                self._obs(4, 10, "containerd://7e2b4c0f"),
                self._obs(5, 10, "containerd://7e2b4c0f"),
            ],
            fault_handle={"kind": "native", "method": "systemd-run"},
        )
        verdict = PROCESS_KILL_RULE.evaluate(ctx)
        assert verdict.is_passed
        assert any("8 → 10 (Δ+2)" in line for line in verdict.evidence_lines)

    def test_case_25r_shape_replays_verified(self):
        # #25-R (2026-09-06 03:02): RESTARTS 15→17 立判, containerID 双变,
        # negative evidence all dismissed by the LLM in BOTH runs.
        ctx = RuleContext(
            metric_observations=[
                self._obs(2, 15, "containerd://c41d0b7e"),
                self._obs(4, 17, "containerd://9a6f3d21"),
                self._obs(5, 17, "containerd://9a6f3d21"),
            ],
            fault_handle={"kind": "native", "method": "systemd-run"},
        )
        verdict = PROCESS_KILL_RULE.evaluate(ctx)
        assert verdict.is_passed
        assert any("15 → 17 (Δ+2)" in line for line in verdict.evidence_lines)

    def test_baseline_six_days_stable_counter_is_not_a_kill(self):
        # #25's negative-evidence leg: baseline restartCount=8 ambiguity was
        # resolved by TIME-WINDOW discrimination (last baseline restart 10d
        # ago). The rule's own window is the observation timeline: a stable
        # counter across the timeline is not a kill, even with an anchor.
        ctx = RuleContext(
            metric_observations=[
                self._obs(2, 8, "containerd://5f8c1a9d"),
                self._obs(5, 8, "containerd://5f8c1a9d"),
            ],
            fault_handle={"kind": "native", "method": "systemd-run"},
        )
        assert PROCESS_KILL_RULE.evaluate(ctx).kind == "unknown"


class TestContainerIdExtraction:
    """The extractor leg of the kill signature — Container ID must land
    in the metric timeline for describe and get-json probes alike."""

    def test_describe_pod_extracts_container_id(self):
        from chaos_agent.agent.nodes.verify._metric_extractor import extract_metrics
        stdout = (
            "Containers:\n"
            "  manager:\n"
            "    Container ID:  containerd://a1b2c3d4e5f6\n"
            "    Image:         registry/app:latest\n"
            "    Restart Count:  8\n"
        )
        metrics = extract_metrics("kubectl", "describe pod p1 -n ns", stdout)
        assert metrics["Container ID"] == "containerd://a1b2c3d4e5f6"
        assert metrics["RestartCount"] == "8"

    def test_get_pod_json_extracts_container_id(self):
        import json as _json
        from chaos_agent.agent.nodes.verify._metric_extractor import extract_metrics
        pod = {
            "kind": "Pod",
            "status": {
                "phase": "Running",
                "containerStatuses": [{
                    "restartCount": 10,
                    "ready": True,
                    "containerID": "containerd://b2c3d4e5f6a1",
                    "lastState": {"terminated": {"reason": "Error", "exitCode": 137}},
                }],
            },
        }
        metrics = extract_metrics(
            "kubectl", "get pod p1 -n ns -o json", _json.dumps(pod),
        )
        assert metrics["Container ID"] == "containerd://b2c3d4e5f6a1"
        assert metrics["RestartCount"] == "10"

    def test_container_id_survives_fault_filter_for_process(self):
        from chaos_agent.agent.nodes.verify._metric_extractor import (
            _filter_metrics_by_fault,
        )
        metrics = {
            "RestartCount": "8",
            "Container ID": "containerd://a1b2c3",
            "CPU usage": "250m",
        }
        kept = _filter_metrics_by_fault(metrics, "process", "kill")
        assert "Container ID" in kept
        assert "RestartCount" in kept
        assert "CPU usage" not in kept

    def test_container_id_is_not_numeric_truth_delta_material(self):
        # Cross-check safety: a runtime-qualified ID must never parse as a
        # number, so it cannot enter _build_truth_deltas and interact with
        # the contradiction scanner.
        from chaos_agent.agent.nodes.verify._verifier_layer2_parse import (
            _parse_numeric,
        )
        assert _parse_numeric("containerd://a1b2c3") is None

    def test_fill_criteria_survive_fault_filter_for_disk(self):
        # The fill rule reads "Disk usage (overlay)/(nodefs)" from the
        # timeline; the extractor stores FILTERED metrics, so the
        # "Disk usage" prefix entry in _FAULT_METRICS["disk"] is the
        # load-bearing link — losing it silently turns the rule into a
        # permanent unknown (false negative).
        from chaos_agent.agent.nodes.verify._metric_extractor import (
            _filter_metrics_by_fault,
        )
        metrics = {
            "Disk usage (overlay)": "86% (96/112)",
            "Disk usage (nodefs)": "11% (1/100)",
            "CPU usage": "250m",
        }
        kept = _filter_metrics_by_fault(metrics, "disk", "fill")
        assert "Disk usage (overlay)" in kept
        assert "Disk usage (nodefs)" in kept
        assert "CPU usage" not in kept


class TestDiskFillRuleEvaluation:
    """Fill judgement: usage reaches the INJECTED target (percent/size
    derived from spec params — never a hardcoded absolute). Shapes from
    #9 (pod-scope overlay) and #29 (node-scope df 11%→86%, 85% target)."""

    @staticmethod
    def _obs(iteration: int, usage: str) -> dict:
        return {"iteration": iteration, "metrics": {"Disk usage (overlay)": usage}}

    def _ctx(self, observations, params, **kw):
        return RuleContext(
            metric_observations=observations,
            fault_handle={"experiment_uid": "exp"},
            spec_params=params,
            **kw,
        )

    def test_percent_target_reached_passed(self):
        ctx = self._ctx(
            [
                self._obs(1, "11% (12345678901/112277999680)"),
                self._obs(3, "86% (96636764160/112277999680)"),
            ],
            {"percent": "85"},
        )
        v = DISK_FILL_RULE.evaluate(ctx)
        assert v.is_passed
        assert v.rule_name == "disk_fill_usage_target"
        assert "86%" in " ".join(v.evidence_lines)
        assert "85%" in " ".join(v.evidence_lines)
        assert "mechanism anchor: fault_handle" in " ".join(v.evidence_lines)

    def test_percent_target_exactly_hit_is_passed(self):
        ctx = self._ctx(
            [self._obs(1, "85% (100/100)"), self._obs(2, "85% (100/100)")],
            {"percent": "85"},
        )
        assert DISK_FILL_RULE.evaluate(ctx).is_passed  # ≥, not >

    def test_percent_target_missed_is_unknown(self):
        ctx = self._ctx(
            [self._obs(1, "11% (1/100)"), self._obs(2, "84% (84/100)")],
            {"percent": "85"},
        )
        v = DISK_FILL_RULE.evaluate(ctx)
        assert not v.is_passed
        assert "below" in v.reason

    def test_size_target_reached_passed(self):
        # df -k raw byte pair: used grows by ~90GiB on a 10g request.
        ctx = self._ctx(
            [
                self._obs(1, "11% (37384168/112277999680)"),
                self._obs(3, "86% (96636764160/112277999680)"),
            ],
            {"size": "10g"},
        )
        v = DISK_FILL_RULE.evaluate(ctx)
        assert v.is_passed
        assert "used bytes grew by" in " ".join(v.evidence_lines)

    def test_size_target_exactly_hit_is_passed(self):
        ctx = self._ctx(
            [
                self._obs(1, "0% (0/100)"),
                self._obs(2, "90% (96636764160/107374182400)"),
            ],
            {"size": "96636764160"},  # bare bytes — exactly the growth
        )
        assert DISK_FILL_RULE.evaluate(ctx).is_passed

    def test_size_target_just_missed_is_unknown(self):
        ctx = self._ctx(
            [
                self._obs(1, "0% (0/100)"),
                self._obs(2, "90% (96636764160/107374182400)"),
            ],
            {"size": "96636764161"},  # one byte over the growth
        )
        v = DISK_FILL_RULE.evaluate(ctx)
        assert not v.is_passed
        assert "below" in v.reason

    def test_no_derivable_threshold_is_unknown(self):
        # Neither percent nor size in params — the rule has NO opinion
        # about what "full" means; hardcoding one is forbidden.
        ctx = self._ctx(
            [self._obs(1, "86% (96/112)")],
            {"path": "/tmp"},
        )
        v = DISK_FILL_RULE.evaluate(ctx)
        assert not v.is_passed
        assert "never hardcoded" in v.reason

    def test_degenerate_target_is_not_a_free_pass(self):
        # Re-audit finding 10: percent=0 parses to 0.0, and `peak >= 0`
        # is vacuously true — the rule would hand out a pass for an
        # injection that parked nothing. A degenerate target reads as
        # not derivable (same honest unknown as an absent one).
        ctx = self._ctx(
            [self._obs(1, "11% (1/100)"), self._obs(2, "12% (2/100)")],
            {"percent": "0"},
        )
        v = DISK_FILL_RULE.evaluate(ctx)
        assert not v.is_passed
        assert "never hardcoded" in v.reason

    def test_no_anchor_is_unknown(self):
        ctx = RuleContext(
            metric_observations=[
                self._obs(1, "11% (1/100)"), self._obs(2, "86% (86/100)"),
            ],
            spec_params={"percent": "85"},
        )
        v = DISK_FILL_RULE.evaluate(ctx)
        assert not v.is_passed
        assert "anchor" in v.reason

    def test_no_usage_observations_is_unknown(self):
        ctx = self._ctx(
            [{"iteration": 1, "metrics": {"CPU usage": "250m"}}],
            {"percent": "85"},
        )
        v = DISK_FILL_RULE.evaluate(ctx)
        assert not v.is_passed
        assert "no Disk usage" in v.reason

    def test_nodefs_key_carries_node_scope_fills(self):
        # node-scope fills land on "Disk usage (nodefs)" (df on /host).
        ctx = RuleContext(
            metric_observations=[
                {"iteration": 1, "metrics": {
                    "Disk usage (nodefs)": "11% (1/100)"}},
                {"iteration": 3, "metrics": {
                    "Disk usage (nodefs)": "86% (86/100)"}},
            ],
            fault_handle={"experiment_uid": "exp"},
            spec_params={"percent": "85"},
        )
        assert DISK_FILL_RULE.evaluate(ctx).is_passed


class TestMultiReplicaFlatTimeline:
    """Multi-replica shapes under the FLAT (pod-identity-less) timeline.

    The per-pod max contract in its flat-timeline form: any observation
    source reaching the target counts (fill), while a kill timeline that
    interleaves two pods refuses to adjudicate (monotonicity guard).
    """

    @staticmethod
    def _kill_obs(iteration: int, restarts: int, container_id: str) -> dict:
        return {"iteration": iteration, "metrics": {
            "RestartCount": restarts, "Container ID": container_id,
        }}

    def test_kill_interleaved_pods_refuse_to_adjudicate(self):
        # Two pods share the label selector: describe alternates between
        # them, so RestartCount dips (8→0) — a single pod never does.
        ctx = RuleContext(
            metric_observations=[
                self._kill_obs(1, 8, "containerd://aaa"),
                self._kill_obs(2, 0, "containerd://zzz"),  # the OTHER pod
                self._kill_obs(3, 9, "containerd://bbb"),
                self._kill_obs(4, 1, "containerd://yyy"),
                self._kill_obs(5, 10, "containerd://ccc"),
            ],
            fault_handle={"experiment_uid": "exp"},
        )
        v = PROCESS_KILL_RULE.evaluate(ctx)
        assert not v.is_passed
        assert "not monotonic" in v.reason

    def test_fill_any_source_reaching_target_counts(self):
        # 3 replicas, one filled to 86% — max(pct-series) hits the target
        # even with the two clean replicas' observations interleaved.
        observations = []
        for i, pct in enumerate(["11% (11/100)", "86% (86/100)", "12% (12/100)"]):
            observations.append({"iteration": i + 1, "metrics": {
                "Disk usage (overlay)": pct}})
        ctx = RuleContext(
            metric_observations=observations,
            fault_handle={"experiment_uid": "exp"},
            spec_params={"percent": "85"},
        )
        assert DISK_FILL_RULE.evaluate(ctx).is_passed

    def test_fill_diluted_clean_replicas_stay_unknown(self):
        # 3 replicas, NONE filled (natural drift only) — no false pass.
        observations = []
        for i, pct in enumerate(["11% (11/100)", "12% (12/100)", "11% (11/100)"]):
            observations.append({"iteration": i + 1, "metrics": {
                "Disk usage (overlay)": pct}})
        ctx = RuleContext(
            metric_observations=observations,
            fault_handle={"experiment_uid": "exp"},
            spec_params={"percent": "85"},
        )
        v = DISK_FILL_RULE.evaluate(ctx)
        assert not v.is_passed
        assert "below" in v.reason

    def test_uncalibrated_families_never_adjudicate_multi_replica(self):
        # The spec's per-pod-max scenarios (3-replica CPU) target families
        # whose rules are declared-but-uncalibrated — with perfect numbers
        # and a solid anchor the answer is STILL unknown (no guessed
        # threshold trades variance for systematic false verdicts).
        observations = [
            {"iteration": 1, "metrics": {"CPU usage": "3%", "MEM usage": "5%"}},
            {"iteration": 2, "metrics": {"CPU usage": "97%", "MEM usage": "90%"}},
        ]
        anchor_kw = {"fault_handle": {"experiment_uid": "exp"}}
        cpu_v = CPU_FULLLOAD_RULE.evaluate(RuleContext(
            metric_observations=observations, spec_params={}, **anchor_kw))
        mem_v = MEM_LOAD_RULE.evaluate(RuleContext(
            metric_observations=observations, spec_params={}, **anchor_kw))
        assert not cpu_v.is_passed and not mem_v.is_passed
        assert "not calibrated" in cpu_v.reason
        assert "not calibrated" in mem_v.reason


# ---------------------------------------------------------------------------
# STS partition rule (openspec mechanism-invariant-trust-boundary): the rule
# binds to a FROZEN author-declared key and is now ACTIVE — its three-leg
# criteria are wired live (pin LIFTED by the re-scoped calibration gate).
# ---------------------------------------------------------------------------

def _partition_ctx(*, expected="2", with_revisions=True, partition="2", anchor=True):
    """Build a RuleContext whose timeline already satisfies every leg."""
    metrics = {"Partition": partition}
    if with_revisions:
        metrics["Current revision"] = "web-abc111"
        metrics["Update revision"] = "web-def222"
    observations = [{"iteration": 1, "metrics": metrics}]
    return RuleContext(
        metric_observations=observations,
        fault_handle={"experiment_uid": "exp"} if anchor else None,
        declared_expected={"Partition": expected} if expected else {},
    )


class TestStsPartitionRuleSelection:
    def test_frozen_key_resolves_the_rule(self):
        rules = resolve_deterministic_rules("statefulset", "partition-stall")
        assert STS_PARTITION_RULE in rules

    def test_llm_triple_resolves_to_zero_rules(self):
        # Forensics (memory 66425653): #77's runtime LLM triple is
        # rollingupdate/partition-stuck — NOT the frozen key. Without the
        # declaration the verify layer resolves zero rules (byte-identical
        # to the pre-change behaviour).
        assert resolve_deterministic_rules("rollingupdate", "partition-stuck") == ()

    def test_match_key_is_the_frozen_declaration(self):
        assert STS_PARTITION_RULE.match == ("statefulset", "partition-stall")


class TestStsPartitionActive:
    """Pin LIFTED (gate re-scoped, tasks 5.1/5.4/5.5) — the production rule now
    delegates to the three-leg criteria: it PASSES when every leg holds, and
    still ABSTAINS to unknown when any leg is unmet (fail-safe, never a
    downgrade, never a guessed pass)."""

    def test_active_passes_with_all_legs_satisfied(self):
        v = STS_PARTITION_RULE.evaluate(_partition_ctx())
        assert v.is_passed
        assert v.kind == "passed"

    def test_active_abstains_without_expected_or_observations(self):
        # No declared N, no observations, no anchor — the ACTIVE rule still
        # abstains to unknown (fail-safe: unmet legs never manufacture a pass).
        assert STS_PARTITION_RULE.evaluate(RuleContext()).kind == "unknown"


class TestStsPartitionLegs:
    """Unit tests of the three-leg criteria directly (now wired live via
    _evaluate_sts_partition). Exercises each leg's pass/abstain boundary."""

    _legs = staticmethod(rules_mod._sts_partition_legs)

    def test_all_legs_hold_yields_passed(self):
        assert self._legs(_partition_ctx()).is_passed

    def test_missing_anchor_yields_unknown(self):
        v = self._legs(_partition_ctx(anchor=False))
        assert not v.is_passed and "anchor" in v.reason

    def test_missing_declared_expected_yields_unknown(self):
        # N comes from the frozen declaration, never spec_params.
        v = self._legs(_partition_ctx(expected=""))
        assert not v.is_passed and "declared" in v.reason

    def test_partition_below_declared_n_yields_unknown(self):
        v = self._legs(_partition_ctx(expected="2", partition="1"))
        assert not v.is_passed and "never reached" in v.reason

    def test_partition_series_across_timeline_reaches_n(self):
        # Baseline 0 → post 2: the timeline must contain the declared N.
        ctx = RuleContext(
            metric_observations=[
                {"iteration": 1, "metrics": {"Partition": "0"}},
                {"iteration": 2, "metrics": {
                    "Partition": "2", "Current revision": "a", "Update revision": "b"}},
            ],
            fault_handle={"experiment_uid": "exp"},
            declared_expected={"Partition": "2"},
        )
        assert self._legs(ctx).is_passed

    def test_no_partition_observations_yields_unknown(self):
        ctx = RuleContext(
            metric_observations=[{"iteration": 1, "metrics": {}}],
            fault_handle={"experiment_uid": "exp"},
            declared_expected={"Partition": "2"},
        )
        v = self._legs(ctx)
        assert not v.is_passed and "no Partition" in v.reason

    def test_missing_revision_observations_yields_unknown(self):
        v = self._legs(_partition_ctx(with_revisions=False))
        assert not v.is_passed and "revision" in v.reason

    def test_revisions_not_split_yields_unknown(self):
        # current == update means the rollout converged, not stalled.
        ctx = RuleContext(
            metric_observations=[{"iteration": 1, "metrics": {
                "Partition": "2", "Current revision": "same", "Update revision": "same"}}],
            fault_handle={"experiment_uid": "exp"},
            declared_expected={"Partition": "2"},
        )
        v = self._legs(ctx)
        assert not v.is_passed and "no rollout split" in v.reason

    def test_baseline_revision_read_does_not_mask_a_real_split(self):
        # Regression, REAL cluster data (drill-sts-partition-target, openspec
        # mechanism-invariant-trust-boundary Phase 2 re-test). A pre-injection
        # baseline read carries currentRevision == updateRevision == 7c99c5c4d
        # (the new revision does not exist yet). The pre-fix distinct-union
        # leg-2 read unioned that baseline value into updateRevision, so the
        # sets overlapped and a GENUINE stall (partition=2, updateRevision
        # advanced to 898f79484 while currentRevision stayed 7c99c5c4d) was
        # masked as "no rollout split" -> false unknown. The as-of-latest
        # snapshot read is immune: the latest observation carries the split.
        baseline = {
            "Partition": "0",
            "Update revision": "drill-sts-partition-target-7c99c5c4d",
            "Current revision": "drill-sts-partition-target-7c99c5c4d",
        }
        stalled = {
            "Partition": "2",
            "Update revision": "drill-sts-partition-target-898f79484",
            "Current revision": "drill-sts-partition-target-7c99c5c4d",
        }
        ctx = RuleContext(
            metric_observations=[
                {"iteration": 0, "metrics": baseline},
                {"iteration": 1, "metrics": stalled},
                {"iteration": 2, "metrics": stalled},
            ],
            fault_handle={"experiment_uid": "exp"},
            declared_expected={"Partition": "2"},
        )
        assert self._legs(ctx).is_passed

    def test_latest_snapshot_converged_after_a_split_yields_unknown(self):
        # The as-of-latest semantic is a snapshot, not "a split ever existed":
        # if the latest read shows currentRevision == updateRevision again
        # (rollout converged), the split is no longer in effect -> unknown.
        ctx = RuleContext(
            metric_observations=[
                {"iteration": 1, "metrics": {
                    "Partition": "2", "Current revision": "a", "Update revision": "b"}},
                {"iteration": 2, "metrics": {
                    "Partition": "2", "Current revision": "b", "Update revision": "b"}},
            ],
            fault_handle={"experiment_uid": "exp"},
            declared_expected={"Partition": "2"},
        )
        v = self._legs(ctx)
        assert not v.is_passed and "no rollout split" in v.reason


class TestStsPartitionFullChainCalibration:
    """Phase 3 (task 5.3) BINDING calibration — REAL stdout through the FULL
    chain ``parser -> metrics -> rule -> verdict`` in ONE assertion. The
    deterministic rule consumes the extractor's output trustingly (no raw
    stdout survives to cross-check at rule time), so the two MUST be
    calibrated together, never against synthetic parser output (the classes
    above feed pre-built metric dicts; this one feeds real bytes).

    Fixtures are VERBATIM from task ``inject-92493ad8`` (#77-R4, 2026-10-15):
    the real shell-QUOTED labeled-jsonpath command (the ``{"\n"}`` separators
    force the single quotes — the dominant production form this change's
    quote-gate fix un-starved), its real stdout, and the REAL production
    ``fault_handle`` (``kind=faultdrill_cr`` / ``method=faultdrill_carrier``,
    from result_summary) — so leg 3's anchor is a genuine carrier handle, not
    a synthetic ``experiment_uid`` stand-in.

    Calibration outcome (task 5.5, gate RE-SCOPED): the pin is LIFTED. The legs
    evaluate correctly on real data (stalled -> passed, converged -> unknown).
    Partition is a SINGLE case (#77) and the catalogue is one-case-per-form by
    design, so the original multi-CASE bar was structurally unsatisfiable; the
    gate is re-scoped for fail-safe unique-field families to multi-RUN real data
    (#77: 5 runs) + the fail-safe abstain structure (an unmet leg can never
    manufacture a false pass). ``STS_PARTITION_RULE`` is now ACTIVE.
    """

    _legs = staticmethod(rules_mod._sts_partition_legs)

    # VERBATIM real command (task inject-92493ad8, msg108) — machine-specific
    # kubeconfig path retained verbatim to keep the fixture byte-faithful.
    _REAL_CMD = (
        "get statefulset drill-sts-partition-target -n default -o "
        "'jsonpath=partition={.spec.updateStrategy.rollingUpdate.partition}"
        '{"\\n"}updateRevision={.status.updateRevision}'
        '{"\\n"}currentRevision={.status.currentRevision}'
        '{"\\n"}status.replicas={.status.replicas}'
        '{"\\n"}readyReplicas={.status.readyReplicas}'
        '{"\\n"}currentReplicas={.status.currentReplicas}'
        '{"\\n"}updatedReplicas={.status.updatedReplicas}'
        '{"\\n"}observedGeneration={.status.observedGeneration}'
        '{"\\n"}templateAnn={.spec.template.metadata.annotations}'
        '{"\\n"}\''
        " /Users/jiangzelin/.kube/config"
    )
    # VERBATIM real stdout — STALLED: partition=2, currentRevision != updateRevision.
    _REAL_STALLED_STDOUT = (
        "partition=2\n"
        "updateRevision=drill-sts-partition-target-c7fdff7c\n"
        "currentRevision=drill-sts-partition-target-7c99c5c4d\n"
        "status.replicas=3\n"
        "readyReplicas=3\n"
        "currentReplicas=2\n"
        "updatedReplicas=1\n"
        "observedGeneration=13\n"
        'templateAnn={"drill-partition-tick":"partition-stall-20261015T144247",'
        '"drill.example.com/baseline":"v0"}\n'
    )
    # REAL production fault_handle for this carrier injection (result_summary).
    _REAL_HANDLE = {"kind": "faultdrill_cr", "method": "faultdrill_carrier"}

    def _chain(self, stdout, *, anchor=True, expected="2"):
        """Run the real bytes through the full chain into a RuleContext."""
        metrics = extract_metrics("kubectl", self._REAL_CMD, stdout)
        return RuleContext(
            metric_observations=[{"iteration": 1, "metrics": metrics}],
            fault_handle=self._REAL_HANDLE if anchor else None,
            declared_expected={"Partition": expected} if expected else {},
        )

    def test_real_stalled_stdout_full_chain_passes(self):
        ctx = self._chain(self._REAL_STALLED_STDOUT)
        # sanity: the extractor really surfaced the three rule-critical keys
        # from the real quoted template (this is the un-starved data flow).
        m = ctx.metric_observations[0]["metrics"]
        assert m["Partition"] == "2"
        assert m["Current revision"] == "drill-sts-partition-target-7c99c5c4d"
        assert m["Update revision"] == "drill-sts-partition-target-c7fdff7c"
        assert self._legs(ctx).is_passed

    def test_real_carrier_handle_is_a_genuine_anchor(self):
        # leg 3 fires on the REAL carrier handle, not a synthetic experiment_uid.
        ctx = self._chain(self._REAL_STALLED_STDOUT)
        assert ctx.anchoring_signal() == "fault_handle"

    def test_no_anchor_on_real_data_yields_unknown(self):
        v = self._legs(self._chain(self._REAL_STALLED_STDOUT, anchor=False))
        assert not v.is_passed and "anchor" in v.reason

    def test_converged_real_shape_yields_unknown(self):
        # The recovered snapshot (partition back to 0, rollout converged): the
        # chain abstains on real bytes — never a false pass after recovery.
        converged = (
            "partition=0\n"
            "updateRevision=drill-sts-partition-target-7c99c5c4d\n"
            "currentRevision=drill-sts-partition-target-7c99c5c4d\n"
        )
        v = self._legs(self._chain(converged))
        assert not v.is_passed

    def test_production_rule_active_on_real_passing_data(self):
        # task 5.5 — the pin is LIFTED (gate re-scoped). The PRODUCTION rule now
        # fires passed on this VERBATIM real stalled data (inject-92493ad8,
        # #77-R4): the full chain parser -> metrics -> rule -> verdict yields a
        # deterministic ``passed``. CALIBRATION EVIDENCE recorded per the flip
        # discipline: legs proven across #77's 5 real runs (stalled -> passed;
        # TTL-self-recovered / converged -> unknown), fail-safe abstain structure
        # (a drifted/absent metric can never manufacture a false pass), and the
        # single case is structural (one-case-per-form catalogue) not a data gap.
        v = STS_PARTITION_RULE.evaluate(self._chain(self._REAL_STALLED_STDOUT))
        assert v.kind == "passed" and v.is_passed


# ---------------------------------------------------------------------------
# Deployment scale-down rule (openspec mechanism-invariant-trust-boundary task
# 6.3): second declarative field-state family. Binds to a FROZEN author-declared
# key and is now ACTIVE — three-leg criteria wired live (pin LIFTED by the
# re-scoped calibration gate), mirroring the STS partition rule.
# ---------------------------------------------------------------------------

def _scaledown_ctx(*, expected="1", series=("3", "1", "1"), anchor=True):
    """Build a RuleContext whose Replicas timeline already satisfies every leg
    (baseline 3 -> declared N=1 -> held at 1)."""
    observations = [
        {"iteration": i + 1, "metrics": {"Replicas": v}}
        for i, v in enumerate(series)
    ]
    return RuleContext(
        metric_observations=observations,
        fault_handle={"experiment_uid": "exp"} if anchor else None,
        declared_expected={"Replicas": expected} if expected else {},
    )


class TestDeploymentScaleDownRuleSelection:
    def test_frozen_key_resolves_the_rule(self):
        rules = resolve_deterministic_rules("deployment", "scale-down")
        assert DEPLOYMENT_SCALE_DOWN_RULE in rules

    def test_match_key_is_the_frozen_declaration(self):
        assert DEPLOYMENT_SCALE_DOWN_RULE.match == ("deployment", "scale-down")

    def test_wrong_action_resolves_to_zero_rules(self):
        # The rule fires only for its calibrated (target, action) key; a
        # different deployment action resolves nothing (byte-identical to the
        # pre-rule-layer fallback).
        assert resolve_deterministic_rules("deployment", "scale-up") == ()


class TestDeploymentScaleDownActive:
    """Pin LIFTED (gate re-scoped, tasks 5.1/5.4/5.5) — the production rule now
    delegates to the three-leg criteria: it PASSES when every leg holds, and
    still ABSTAINS to unknown when any leg is unmet (fail-safe, never a
    downgrade, never a guessed pass)."""

    def test_active_passes_with_all_legs_satisfied(self):
        v = DEPLOYMENT_SCALE_DOWN_RULE.evaluate(_scaledown_ctx())
        assert v.is_passed
        assert v.kind == "passed"

    def test_active_abstains_without_expected_or_observations(self):
        # No declared N, no observations, no anchor — the ACTIVE rule still
        # abstains to unknown (fail-safe: unmet legs never manufacture a pass).
        assert DEPLOYMENT_SCALE_DOWN_RULE.evaluate(RuleContext()).kind == "unknown"


class TestDeploymentScaleDownLegs:
    """Unit tests of the three-leg criteria directly (now wired live via
    _evaluate_deployment_scale_down). Exercises each leg's pass/abstain
    boundary."""

    _legs = staticmethod(rules_mod._deployment_scale_down_legs)

    def test_all_legs_hold_yields_passed(self):
        assert self._legs(_scaledown_ctx()).is_passed

    def test_missing_anchor_yields_unknown(self):
        v = self._legs(_scaledown_ctx(anchor=False))
        assert not v.is_passed and "anchor" in v.reason

    def test_missing_declared_expected_yields_unknown(self):
        # N comes from the frozen declaration, never spec_params.
        v = self._legs(_scaledown_ctx(expected=""))
        assert not v.is_passed and "declared" in v.reason

    def test_replicas_never_reaching_n_yields_unknown(self):
        # baseline 3 -> 2, declared N=1: the series never reached N.
        v = self._legs(_scaledown_ctx(expected="1", series=("3", "2", "2")))
        assert not v.is_passed and "never reached" in v.reason

    def test_no_replicas_observations_yields_unknown(self):
        ctx = RuleContext(
            metric_observations=[{"iteration": 1, "metrics": {}}],
            fault_handle={"experiment_uid": "exp"},
            declared_expected={"Replicas": "1"},
        )
        v = self._legs(ctx)
        assert not v.is_passed and "no Replicas" in v.reason

    def test_always_at_n_yields_unknown_no_came_down_proof(self):
        # NEGATIVE CONTROL: the target sat at N=1 the whole time (series
        # [1, 1]) — no descent from a higher baseline, so this is a no-op,
        # NOT a passed scale-down. The direction leg abstains.
        v = self._legs(_scaledown_ctx(expected="1", series=("1", "1")))
        assert not v.is_passed and "came-down" in v.reason

    def test_came_down_from_higher_baseline_yields_passed(self):
        # baseline 5 -> declared N=2 -> held: descended from above, reached N.
        assert self._legs(
            _scaledown_ctx(expected="2", series=("5", "2", "2"))
        ).is_passed


class TestDeploymentScaleDownFullChainCalibration:
    """BINDING calibration — REAL #34 stdout through the FULL chain
    ``parser -> metrics -> rule -> verdict`` in ONE assertion. The rule
    consumes the extractor's output trustingly (no raw stdout survives to
    cross-check at rule time), so the two MUST be calibrated together, never
    against synthetic parser output (the classes above feed pre-built metric
    dicts; this one feeds real bytes).

    Fixtures are VERBATIM from task ``inject-238fd5c3`` (#34-R, 2026-10-16):
    the real labeled-jsonpath read-back commands — both the lowercase
    ``replicas={.spec.replicas}{"\\n"}`` form (baseline / verifier) and the
    uppercase direct ``Replicas={.spec.replicas}`` form (landing) — their real
    stdout, and the real production ``fault_handle``. Both label forms humanize
    to the SAME named metric ``Replicas``, crossing the injection boundary
    baseline 3 -> landed 1 -> verified 1.

    Calibration outcome (gate RE-SCOPED): the pin is LIFTED. The legs evaluate
    correctly on real data (the descent 3 -> declared N=1 passes; an always-at-1
    timeline abstains). Scale-down is a SINGLE case (#34) — structural under the
    one-case-per-form catalogue, so the original multi-CASE bar was
    unsatisfiable; the gate is re-scoped to multi-RUN real data (#34: 3 runs) +
    the fail-safe abstain structure. ``DEPLOYMENT_SCALE_DOWN_RULE`` is ACTIVE.
    """

    _legs = staticmethod(rules_mod._deployment_scale_down_legs)

    # VERBATIM real commands + stdout (task inject-238fd5c3, msg21/114/128) —
    # machine-specific kubeconfig path retained verbatim for byte-faithfulness.
    _BASE_CMD = 'get deploy drill-scaledown-target -n default -o \'jsonpath=replicas={.spec.replicas}{"\\n"}ready={.status.readyReplicas}{"\\n"}updatedReplicas={.status.updatedReplicas}{"\\n"}available={.status.availableReplicas}{"\\n"}selector={.spec.selector.matchLabels}{"\\n"}uid={.metadata.uid}{"\\n"}generation={.metadata.generation}{"\\n"}\' /Users/jiangzelin/.kube/config'
    _BASE_STDOUT = 'replicas=3\nready=3\nupdatedReplicas=3\navailable=3\nselector={"app":"drill-scaledown-target"}\nuid=2e67d908-4441-477c-b000-d212142d6b78\ngeneration=1\n'
    _LAND_CMD = '/Users/jiangzelin/.kube/config get deployment drill-scaledown-target -n default -o \'jsonpath=Replicas={.spec.replicas} Ready={.status.readyReplicas} Updated={.status.updatedReplicas} Available={.status.availableReplicas} Unavailable={.status.unavailableReplicas} Generation={.metadata.generation}{"\\n"}\''
    _LAND_STDOUT = 'Replicas=1 Ready=1 Updated=1 Available=1 Unavailable= Generation=2\n'
    _VER_CMD = 'get deployment drill-scaledown-target -n default -o \'jsonpath=replicas={.spec.replicas}{"\\n"}ready={.status.readyReplicas}{"\\n"}updatedReplicas={.status.updatedReplicas}{"\\n"}available={.status.availableReplicas}{"\\n"}generation={.metadata.generation}{"\\n"}\' /Users/jiangzelin/.kube/config'
    _VER_STDOUT = 'replicas=1\nready=1\nupdatedReplicas=1\navailable=1\ngeneration=2\n'
    # REAL production fault_handle recorded for this run (result_summary).
    _REAL_HANDLE = {"kind": "native", "method": "kubectl_native"}

    def _chain(self, *, anchor=True, expected="1"):
        """Run the three VERBATIM real read-backs through the full chain into a
        Replicas timeline (baseline 3 -> landed 1 -> verified 1)."""
        obs = []
        for i, (cmd, out) in enumerate([
            (self._BASE_CMD, self._BASE_STDOUT),
            (self._LAND_CMD, self._LAND_STDOUT),
            (self._VER_CMD, self._VER_STDOUT),
        ]):
            metrics = extract_metrics("kubectl", cmd, out)
            obs.append({"iteration": i + 1, "metrics": metrics})
        return RuleContext(
            metric_observations=obs,
            fault_handle=self._REAL_HANDLE if anchor else None,
            declared_expected={"Replicas": expected} if expected else {},
        )

    def test_real_readbacks_surface_replicas_across_boundary(self):
        # sanity: the extractor really surfaced the named metric from all three
        # real read-back forms (both lowercase and uppercase label spellings).
        ctx = self._chain()
        series = [o["metrics"].get("Replicas") for o in ctx.metric_observations]
        assert series == ["3", "1", "1"]

    def test_real_full_chain_descends_to_declared_n_passes(self):
        assert self._legs(self._chain()).is_passed

    def test_real_handle_is_a_genuine_anchor(self):
        # leg 1 fires on the REAL production handle, not a synthetic stand-in.
        assert self._chain().anchoring_signal() == "fault_handle"

    def test_no_anchor_on_real_data_yields_unknown(self):
        v = self._legs(self._chain(anchor=False))
        assert not v.is_passed and "anchor" in v.reason

    def test_uppercase_landing_form_alone_produces_replicas(self):
        # The landing read-back uses the UPPERCASE direct form
        # ``Replicas={.spec.replicas}`` — humanize still yields "Replicas".
        m = extract_metrics("kubectl", self._LAND_CMD, self._LAND_STDOUT)
        assert m["Replicas"] == "1"

    def test_production_rule_active_on_real_passing_data(self):
        # The pin is LIFTED (gate re-scoped). The PRODUCTION rule now fires
        # passed on this VERBATIM real scale-down data (inject-238fd5c3, #34-R):
        # baseline 3 -> landed 1 -> verified 1, with the real label drift
        # (ready=/updatedReplicas= vs Ready=/Updated=) confined to NON-consumed
        # keys while ``Replicas`` stays stable across all three read-back forms.
        # CALIBRATION EVIDENCE per the flip discipline: legs proven across #34's
        # 3 real runs (descent -> passed; always-at-N / no-anchor -> unknown),
        # fail-safe abstain structure, single case is structural not a data gap.
        v = DEPLOYMENT_SCALE_DOWN_RULE.evaluate(self._chain())
        assert v.kind == "passed" and v.is_passed
