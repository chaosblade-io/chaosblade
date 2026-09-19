"""Tests for _deterministic_rules.py — Layer 1.5 skeleton.

Anchors the frozen contract of the deterministic verdict layer:
  * the verdict enum is CLOSED to {passed, unknown} — no downgrade branch
    exists anywhere in the module's public surface;
  * families without a declared rule resolve to zero rules (the finalize
    pipeline then applies zero rule interference);
  * the migrated disk_burn rule adjudicates exactly the signal the legacy
    ``_enforce_disk_burn_facts`` consumed.
"""

import pytest

from chaos_agent.agent.nodes.verify import _deterministic_rules as rules_mod
from chaos_agent.agent.nodes.verify._deterministic_rules import (
    CPU_FULLLOAD_RULE,
    DISK_BURN_RULE,
    DISK_FILL_RULE,
    MEM_LOAD_RULE,
    PROCESS_KILL_RULE,
    DeterministicRule,
    DeterministicVerdict,
    RuleContext,
    verdict_passed,
    verdict_unknown,
)
from chaos_agent.agent.nodes.verify._verification_profiles import (
    resolve_deterministic_rules,
)


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
    the context names which of the three signals is in play."""

    def test_fault_handle_wins_first(self):
        ctx = RuleContext(
            fault_handle={"experiment_uid": "x"},
            layer1_passed=True,
            post_check={"burn_io_detected": True},
        )
        assert ctx.anchoring_signal() == "fault_handle"

    def test_layer1_when_no_handle(self):
        ctx = RuleContext(layer1_passed=True, post_check={"any": True})
        assert ctx.anchoring_signal() == "layer1_success"

    def test_post_check_last(self):
        assert RuleContext(post_check={"any": 1}).anchoring_signal() == "post_check"

    def test_no_anchor(self):
        assert RuleContext().anchoring_signal() == ""


class TestRuleResolution:
    """Families without a declared rule must resolve to zero rules — the
    guarantee behind 'no-rule family: zero behaviour change'."""

    def test_disk_burn_matches(self):
        resolved = resolve_deterministic_rules("disk", "burn")
        assert resolved == (DISK_BURN_RULE,)

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


class TestDiskBurnRuleEvaluation:
    """Judgement half of the disk_burn migration — mirrors the exact
    signal the legacy _enforce_disk_burn_facts gated on."""

    def test_active_io_yields_passed_with_throughput_evidence(self):
        ctx = RuleContext(post_check={
            "burn_io_detected": True,
            "active_partitions": [
                {"name": "/dev/vdb", "write_throughput_mb_s": 120},
                {"name": "/dev/vdb3", "write_throughput_mb_s": 45},
            ],
        })
        verdict = DISK_BURN_RULE.evaluate(ctx)
        assert verdict.is_passed
        assert verdict.rule_name == "disk_burn_io_active"
        assert any("/dev/vdb: ~120 MB/s" in line for line in verdict.evidence_lines)

    def test_active_io_without_partitions_falls_back_to_measured(self):
        ctx = RuleContext(post_check={"burn_io_detected": True, "active_partitions": []})
        verdict = DISK_BURN_RULE.evaluate(ctx)
        assert verdict.is_passed
        assert any("measured" in line for line in verdict.evidence_lines)

    def test_post_check_without_detection_is_unknown(self):
        ctx = RuleContext(post_check={"burn_io_detected": False})
        assert DISK_BURN_RULE.evaluate(ctx).kind == "unknown"

    def test_missing_post_check_is_unknown(self):
        assert DISK_BURN_RULE.evaluate(RuleContext()).kind == "unknown"


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
            # no fault_handle, no layer1, no post_check — the numeric delta
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

    def test_post_check_result_is_a_valid_anchor(self):
        # #29's fallocate shape names its fill file app-archive.log —
        # fill_file_found=False — yet the PostCheckSpec RESULT being in
        # play is the anchor; the usage numbers carry the judgement.
        ctx = RuleContext(
            metric_observations=[
                self._obs(1, "11% (1/100)"), self._obs(2, "86% (86/100)"),
            ],
            post_check={"fill_file_found": False, "df_output": "..."},
            spec_params={"percent": "85"},
        )
        v = DISK_FILL_RULE.evaluate(ctx)
        assert v.is_passed
        assert "mechanism anchor: post_check" in " ".join(v.evidence_lines)


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
