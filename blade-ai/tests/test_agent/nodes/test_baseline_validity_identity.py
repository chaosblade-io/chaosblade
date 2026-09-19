"""#16 root-cause fix tests: the Identity / Temporal / Validity axioms.

Three bugs, one missing first-class concept — "a baseline must be a VALID
observation". R10 live-cluster replay proved all three:

  * Validity (fix C) — an exit-0 "No resources found" counted into
    ``success_count`` and coverage as if it had observed something;
    4/4 "succeeded" was really 1 valid + 3 empty spins.
  * Identity (fix A) — with ``spec.labels`` structurally empty on the
    workload-scope route, the derive LLM invented ``app=<deployment-name>``
    because nothing authoritative anchored the pod selector.
  * Temporal (fix B) — baseline_capture runs BEFORE execute_loop, yet the
    approved plan declares its baseline "after step 3 completes" (the CM is
    created during execute); the plan's temporal contract was never read.

These tests pin each axiom's mechanics plus one end-to-end R10 replay
through the real node function.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from chaos_agent.agent.nodes.baseline._commands import (
    _is_empty_observation,
    _is_observation_success,
)
from chaos_agent.agent.nodes.baseline import baseline_capture as bc
from chaos_agent.agent.nodes.baseline.baseline_capture import (
    _BaselineCtx,
    _assemble_baseline_result,
    _discover_pod_selector,
    _extract_planned_creations,
    _mark_planned_creation_absence,
    make_baseline_capture,
)
from chaos_agent.agent.nodes.baseline._llm_derive import (
    _build_target_context,
    _identity_rules_block,
)
from chaos_agent.agent.nodes.verify._verifier_messages import (
    _build_baseline_tool_messages,
)
from chaos_agent.agent.nodes.verify._verifier_shared import (
    _compute_baseline_confidence,
)
from chaos_agent.agent.state import AgentState


# ---------------------------------------------------------------------------
# Fix C — Validity: the empty-observation predicate
# ---------------------------------------------------------------------------


class TestIsEmptyObservation:
    """R10-reproduced shapes: emptiness is a refinement of success."""

    def test_empty_merged_output(self):
        assert _is_empty_observation({"exit_code": 0, "stdout": "", "stderr": ""})

    def test_marker_in_stdout(self):
        obs = {"exit_code": 0, "stdout": "No resources found in default namespace.", "stderr": ""}
        assert _is_empty_observation(obs)
        # ... and it is still a "success" by the execution predicate:
        assert _is_observation_success(obs)

    def test_marker_in_stderr(self):
        # Local kubectl puts "No resources found" on stderr with exit 0
        # in some shapes; both streams count.
        assert _is_empty_observation({"exit_code": 0, "stdout": "", "stderr": "No resources found"})

    def test_compact_empty_list_json(self):
        assert _is_empty_observation({"exit_code": 0, "stdout": '{"items": []}', "stderr": ""})

    def test_pretty_empty_list_json(self):
        # R10 probe round-2 fix: pretty-printed empty List carries
        # newlines between the brackets.
        assert _is_empty_observation({"exit_code": 0, 'stdout': '{\n  "items": [\n  ]\n}', "stderr": ""})

    def test_nonzero_exit_is_failure_not_empty(self):
        assert not _is_empty_observation({"exit_code": 1, "stdout": "", "stderr": "x"})

    def test_real_value_not_empty(self):
        assert not _is_empty_observation({"exit_code": 0, "stdout": "NAME AGE\np1 1d", "stderr": ""})

    def test_kubectl_error_marker_is_failure_not_empty(self):
        obs = {"exit_code": 0, "stdout": "Error from server (NotFound)", "stderr": ""}
        assert not _is_empty_observation(obs)
        assert not _is_observation_success(obs)

    def test_nonempty_items_list_not_empty(self):
        assert not _is_empty_observation({"exit_code": 0, "stdout": '{"items": [{"name": "p1"}]}', "stderr": ""})


# ---------------------------------------------------------------------------
# Fix C — Validity: executor chokepoint stamps the flag
# ---------------------------------------------------------------------------


class TestExecutorEmptyStamp:
    """The single append chokepoint stamps ``empty_observation``."""

    @pytest.mark.asyncio
    async def test_empty_success_gets_stamped(self):
        resolved = [{
            "description": "Pods",
            "command": "kubectl get pods -n default -l app=x",
            "subcommand": "get",
            "v_args": ["get", "pods", "-n", "default"],
            "mode": "simple",
        }]
        fake_result = MagicMock(exit_code=0, stdout="No resources found", stderr="")
        with patch("chaos_agent.agent.nodes.baseline._executors.execute_via_transport",
                   new_callable=AsyncMock, return_value=fake_result), \
             patch("chaos_agent.agent.nodes.baseline._executors.dispatch_node_message",
                   new_callable=AsyncMock), \
             patch("chaos_agent.agent.nodes.baseline._executors.get_tracker", return_value=None):
            from chaos_agent.agent.nodes.baseline._executors import _execute_observations
            obs = await _execute_observations(resolved, "kubeconfig", "t")
        assert obs[0]["empty_observation"] is True

    @pytest.mark.asyncio
    async def test_valuable_success_not_stamped(self):
        resolved = [{
            "description": "Deployment",
            "command": "kubectl get deployment d1",
            "subcommand": "get",
            "v_args": ["get", "deployment", "d1"],
            "mode": "simple",
        }]
        fake_result = MagicMock(exit_code=0, stdout="NAME READY\nd1 1/1", stderr="")
        with patch("chaos_agent.agent.nodes.baseline._executors.execute_via_transport",
                   new_callable=AsyncMock, return_value=fake_result), \
             patch("chaos_agent.agent.nodes.baseline._executors.dispatch_node_message",
                   new_callable=AsyncMock), \
             patch("chaos_agent.agent.nodes.baseline._executors.get_tracker", return_value=None):
            from chaos_agent.agent.nodes.baseline._executors import _execute_observations
            obs = await _execute_observations(resolved, "kubeconfig", "t")
        assert "empty_observation" not in obs[0]

    @pytest.mark.asyncio
    async def test_nonzero_failure_not_stamped(self):
        resolved = [{
            "description": "CM",
            "command": "kubectl get cm missing",
            "subcommand": "get",
            "v_args": ["get", "cm", "missing"],
            "mode": "simple",
        }]
        fake_result = MagicMock(exit_code=1, stdout="", stderr="Error from server (NotFound)")
        with patch("chaos_agent.agent.nodes.baseline._executors.execute_via_transport",
                   new_callable=AsyncMock, return_value=fake_result), \
             patch("chaos_agent.agent.nodes.baseline._executors.dispatch_node_message",
                   new_callable=AsyncMock), \
             patch("chaos_agent.agent.nodes.baseline._executors.get_tracker", return_value=None):
            from chaos_agent.agent.nodes.baseline._executors import _execute_observations
            obs = await _execute_observations(resolved, "kubeconfig", "t")
        assert "empty_observation" not in obs[0]


# ---------------------------------------------------------------------------
# Fix C — Validity: assembly keeps valid / empty counts separate
# ---------------------------------------------------------------------------


def _r10_observations():
    """R10 live replay shape: 4/4 'succeeded' = 1 valid + 1 planned absence
    + 2 unexplained empty spins (wrong invented label)."""
    return [
        {"description": "Deployment status",
         "command": "kubectl get deployment drill-pvc-target -n default",
         "exit_code": 0, "stdout": "NAME READY\ndrill-pvc-target 1/1", "stderr": ""},
        {"description": "Pods",
         "command": "kubectl get pods -n default -l app=drill-pvc-target -o wide",
         "exit_code": 0, "stdout": "No resources found in default namespace.", "stderr": ""},
        {"description": "Top pods",
         "command": "kubectl top pods -n default -l app=drill-pvc-target",
         "exit_code": 0, "stdout": "", "stderr": ""},
        {"description": "ConfigMap",
         "command": "kubectl get cm drill-app-config -n default",
         "exit_code": 0, "stdout": "", "stderr": "",
         "expected_absence": "planned creation: the approved plan creates "
                             "configmap 'drill-app-config' during execute"},
    ]


class _FakeSpec:
    scope = "deployment"
    fault_target = "process"
    fault_action = "kill"
    namespace = "default"
    names = ("drill-pvc-target",)
    labels = {}


class TestValidityCounts:

    def test_r10_counts_are_split(self):
        class _EP:
            @staticmethod
            def for_fault(spec, profile):
                return _EP()
            def coverage(self, obs):
                class _C:
                    missing = []
                    profile_id = "x"
                    def as_dict(self):
                        return {}
                return _C()
        with patch.object(bc, "_target_coverage", return_value={"applicable": False}):
            bd = _assemble_baseline_result(
                _FakeSpec(), "k8s", "llm", [], _r10_observations(), {}, {},
            )["baseline_data"]
        # The old single number said "4/4 succeeded". The honest split:
        assert bd["success_count"] == 4
        assert bd["valid_count"] == 2   # deployment value + CM absence
        assert bd["empty_count"] == 2   # wrong-label pods/top spins

    def test_coverage_receives_only_valid_observations(self):
        seen = {}

        class _EP:
            @staticmethod
            def for_fault(spec, profile):
                return _EP()
            def coverage(self, obs):
                seen["n"] = len(obs)
                class _C:
                    missing = []
                    profile_id = "x"
                    def as_dict(self):
                        return {}
                return _C()

        def _fake_target_coverage(spec, resolved, obs):
            seen["cov"] = obs
            return {"applicable": False}

        with patch.object(bc, "EvidenceProfile", _EP), \
             patch.object(bc, "_target_coverage", side_effect=_fake_target_coverage):
            _assemble_baseline_result(
                _FakeSpec(), "k8s", "llm", [], _r10_observations(), {}, {},
            )
        # evidence coverage: only the 2 value-carrying observations
        assert seen["n"] == 2
        # target coverage: same valid-only set
        assert len(seen["cov"]) == 2


# ---------------------------------------------------------------------------
# Fix C — Validity: baseline confidence is honesty-aware
# ---------------------------------------------------------------------------


class TestBaselineConfidence:

    def test_r10_partial(self):
        bd = {"success_count": 4, "total_count": 4,
              "valid_count": 2, "empty_count": 2,
              "observations": _r10_observations()}
        assert _compute_baseline_confidence(AgentState({"baseline_data": bd})) == "partial"

    def test_all_empty_is_none(self):
        bd = {"success_count": 4, "total_count": 4, "observations": [
            {"exit_code": 0, "stdout": "No resources found", "stderr": ""} for _ in range(4)]}
        assert _compute_baseline_confidence(AgentState({"baseline_data": bd})) == "none"

    def test_all_valid_is_high(self):
        bd = {"success_count": 2, "total_count": 2, "observations": [
            {"exit_code": 0, "stdout": "data", "stderr": ""} for _ in range(2)]}
        assert _compute_baseline_confidence(AgentState({"baseline_data": bd})) == "high"

    def test_pure_precheck_baseline_is_partial_not_none(self):
        """#31 shape: every observation is a non-zero judged absence — the
        absences ARE the values, so the old ``success_count <= 0`` early
        exit misclassified the whole baseline as unusable."""
        bd = {"success_count": 0, "total_count": 1, "observations": [
            {"description": "pre-check", "command": "ls /etc/hosts.bak",
             "exit_code": 2, "stdout": "", "stderr": "No such file",
             "expected_absence": "residue pre-check"}]}
        assert _compute_baseline_confidence(AgentState({"baseline_data": bd})) == "partial"

    def test_all_failure_without_marks_is_none(self):
        bd = {"success_count": 0, "total_count": 2, "observations": [
            {"exit_code": 1, "stdout": "", "stderr": "err"} for _ in range(2)]}
        assert _compute_baseline_confidence(AgentState({"baseline_data": bd})) == "none"


# ---------------------------------------------------------------------------
# Fix C + B — verifier blob honesty & existence baselines
# ---------------------------------------------------------------------------


class TestBaselineBlobHonesty:

    def test_r10_blob_header_and_entries(self):
        bd = {"captured_at": "t", "source": "llm", "total_count": 4,
              "success_count": 4, "valid_count": 2, "empty_count": 2,
              "observations": _r10_observations()}
        pairs = _build_baseline_tool_messages(bd, None, None)
        blob = pairs[1].content
        # Honest header: the usable set is smaller than the success count
        assert "2 valid + 2 empty" in blob
        # The one value-carrying observation renders
        assert "drill-pvc-target 1/1" in blob
        # The planned-creation absence renders as an existence baseline
        assert "PRE-INJECTION ABSENCE (existence baseline)" in blob
        assert "planned creation" in blob
        # The two empty spins do NOT render as authoritative values
        assert blob.count("### ") == 2

    def test_clean_baseline_header_unchanged(self):
        bd = {"captured_at": "t", "source": "llm", "total_count": 2,
              "success_count": 2, "valid_count": 2, "empty_count": 0,
              "observations": [
                  {"description": "d", "command": "c", "exit_code": 0,
                   "stdout": "NAME AGE\np1 1d", "stderr": ""} for _ in range(2)]}
        pairs = _build_baseline_tool_messages(bd, None, None)
        assert "2/2 succeeded" in pairs[1].content
        assert "valid + " not in pairs[1].content

    def test_legacy_baseline_without_total_count_renders_honest_denominator(self):
        """Pre-total_count baselines (the exact persisted key-set of the
        #13/#10 audit tasks: captured_at/source/success_count/observations)
        must render N/len(observations), never the "N/0" cosmetic receipt
        the 0-fallback produced."""
        bd = {"captured_at": "t", "source": "llm", "success_count": 2,
              "observations": [
                  {"description": "d", "command": "c", "exit_code": 0,
                   "stdout": "NAME AGE\np1 1d", "stderr": ""} for _ in range(2)]}
        pairs = _build_baseline_tool_messages(bd, None, None)
        assert pairs
        assert "2/2 succeeded" in pairs[1].content
        assert "/0 succeeded" not in pairs[1].content

    def test_pure_absence_baseline_passes_gate(self):
        """Blob gate must not require success_count > 0 when absences exist."""
        bd = {"captured_at": "t", "source": "llm", "total_count": 1,
              "success_count": 0,
              "observations": [
                  {"description": "pre-check", "command": "ls /etc/hosts.bak",
                   "exit_code": 2, "stdout": "", "stderr": "No such file",
                   "expected_absence": "residue pre-check"}]}
        pairs = _build_baseline_tool_messages(bd, None, None)
        assert pairs
        assert "existence baseline" in pairs[1].content

    def test_all_empty_baseline_has_no_blob(self):
        bd = {"captured_at": "t", "source": "llm", "total_count": 2,
              "success_count": 2,
              "observations": [
                  {"description": "d", "command": "c", "exit_code": 0,
                   "stdout": "No resources found", "stderr": ""} for _ in range(2)]}
        assert _build_baseline_tool_messages(bd, None, None) == []


# ---------------------------------------------------------------------------
# Fix B — Temporal: planned-creation extraction & machine marking
# ---------------------------------------------------------------------------


_R10_PLAN = """\
## Execution Steps

**A. 资源准备（接线 + 基线滚动）**
1. `kubectl create cm drill-app-config -n default --from-literal=LOG_LEVEL=INFO`
2. `kubectl set env deployment/drill-pvc-target -n default --from=configmap/drill-app-config`
3. `kubectl rollout status deployment/drill-pvc-target -n default --timeout=120s`

**B. 恢复载体建栈（四对象同名 drill-rc-cfg600）**
4. `kubectl create serviceaccount drill-rc-cfg600 -n default`
5. `kubectl create role drill-rc-cfg600 -n default --verb=get,patch --resource=configmaps,deployments`
6. `kubectl create rolebinding drill-rc-cfg600 -n default --role=drill-rc-cfg600 --serviceaccount=default:drill-rc-cfg600`
7. `kubectl run drill-rc-cfg600 -n default --image=registry.example.com/img:v1 --restart=Never`

## Rollback and Recovery
1. `kubectl delete cm drill-app-config -n default`
2. `kubectl delete pod drill-rc-cfg600 -n default --ignore-not-found`
"""


class TestExtractPlannedCreations:

    def test_r10_plan_extraction(self):
        creations = _extract_planned_creations(_R10_PLAN)
        # Four same-named carrier objects (sa/role/rolebinding + kubectl
        # run's pod) collapse onto the one drill-scoped name; the run line
        # is last so it wins — what matters is BOTH drill assets are found
        # and the existing deployment is NOT.
        assert creations["drill-app-config"] == "configmap"
        assert "drill-rc-cfg600" in creations
        assert "drill-pvc-target" not in creations

    def test_kubectl_run_maps_to_pod(self):
        plan = "7. `kubectl run drill-rc-cfg600 -n default --image=x:v1`"
        assert _extract_planned_creations(plan) == {"drill-rc-cfg600": "pod"}

    def test_kind_alias_normalised(self):
        plan = "`kubectl create deploy my-deploy -n default`"
        assert _extract_planned_creations(plan) == {"my-deploy": "deployment"}

    def test_file_forms_skipped(self):
        plan = "`kubectl apply -f manifest.yaml` and `kubectl create -f other.json`"
        assert _extract_planned_creations(plan) == {}

    def test_delete_and_set_env_never_match(self):
        # set env / rollout / delete are not creations
        assert "drill-pvc-target" not in _extract_planned_creations(_R10_PLAN)

    def test_empty_plan(self):
        assert _extract_planned_creations("") == {}


class TestMarkPlannedCreationAbsence:

    def test_nonzero_notfound_marked(self):
        creations = {"drill-app-config": "configmap"}
        obs = [{"description": "CM", "command": "kubectl get cm drill-app-config -n default",
                "exit_code": 1, "stdout": "",
                "stderr": 'Error from server (NotFound): configmaps "drill-app-config" not found'}]
        marked = _mark_planned_creation_absence(obs, creations, None)
        assert marked == 1
        assert "planned creation" in obs[0]["expected_absence"]
        assert "drill-app-config" in obs[0]["expected_absence"]

    def test_empty_success_marked(self):
        creations = {"drill-app-config": "configmap"}
        obs = [{"description": "CM", "command": "kubectl get cm drill-app-config -n default",
                "exit_code": 0, "stdout": "", "stderr": ""}]
        assert _mark_planned_creation_absence(obs, creations, None) == 1
        assert obs[0]["expected_absence"]

    def test_asset_with_value_not_marked(self):
        # The asset already exists with a value (e.g. a re-run after a
        # crashed first attempt left it behind) — temporal mismatch of the
        # other kind; leave it unmarked.
        creations = {"drill-app-config": "configmap"}
        obs = [{"description": "CM", "command": "kubectl get cm drill-app-config -n default",
                "exit_code": 0, "stdout": '{"data": {"LOG_LEVEL": "INFO"}}', "stderr": ""}]
        assert _mark_planned_creation_absence(obs, creations, None) == 0
        assert "expected_absence" not in obs[0]

    def test_unrelated_empty_not_marked(self):
        creations = {"drill-app-config": "configmap"}
        obs = [{"description": "Pods", "command": "kubectl get pods -n default -l app=wrong",
                "exit_code": 0, "stdout": "No resources found", "stderr": ""}]
        assert _mark_planned_creation_absence(obs, creations, None) == 0
        assert "expected_absence" not in obs[0]

    def test_already_marked_not_double_marked(self):
        creations = {"drill-app-config": "configmap"}
        obs = [{"description": "CM", "command": "kubectl get cm drill-app-config",
                "exit_code": 1, "stdout": "", "stderr": "NotFound",
                "expected_absence": "existing reason"}]
        assert _mark_planned_creation_absence(obs, creations, None) == 0
        assert obs[0]["expected_absence"] == "existing reason"


# ---------------------------------------------------------------------------
# Fix A — Identity: authoritative pod-selector discovery
# ---------------------------------------------------------------------------


def _ctx(**overrides):
    base = dict(
        llm=object(), state={}, task_id="t", tracker=MagicMock(),
        spec=_FakeSpec(), scope="deployment", target="process", action="kill",
        skill_case="case", kubeconfig="k", channel="kubeconfig", profile="k8s",
        pod_selector=None,
    )
    base.update(overrides)
    return _BaselineCtx(**base)


class TestDiscoverPodSelector:

    @pytest.mark.asyncio
    async def test_workload_selector_discovered(self):
        fake = MagicMock(exit_code=0, stdout='{"app":"drill-pvc"}', stderr="")
        with patch("chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
                   new_callable=AsyncMock, return_value=fake) as mock_exec:
            selector = await _discover_pod_selector(_ctx())
        assert selector == {"app": "drill-pvc"}
        cmd = mock_exec.await_args.args[0]
        # The query targets the workload object with the selector jsonpath
        assert "deployment" in cmd
        assert "drill-pvc-target" in cmd
        assert "jsonpath={.spec.selector.matchLabels}" in cmd

    @pytest.mark.asyncio
    async def test_nonzero_exit_fail_open(self):
        fake = MagicMock(exit_code=1, stdout="", stderr="NotFound")
        with patch("chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
                   new_callable=AsyncMock, return_value=fake):
            assert await _discover_pod_selector(_ctx()) is None

    @pytest.mark.asyncio
    async def test_unparseable_output_fail_open(self):
        fake = MagicMock(exit_code=0, stdout="not json", stderr="")
        with patch("chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
                   new_callable=AsyncMock, return_value=fake):
            assert await _discover_pod_selector(_ctx()) is None

    @pytest.mark.asyncio
    async def test_guards_skip_discovery(self):
        # pod scope: names ARE pod names — no selector to discover
        with patch("chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
                   new_callable=AsyncMock) as mock_exec:
            assert await _discover_pod_selector(_ctx(scope="pod")) is None
        # host profile: no cluster to query
        with patch("chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
                   new_callable=AsyncMock) as mock_exec:
            assert await _discover_pod_selector(_ctx(profile="host")) is None
        # no LLM strategy possible — discovery has no consumer
        with patch("chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
                   new_callable=AsyncMock) as mock_exec:
            assert await _discover_pod_selector(_ctx(llm=None)) is None
        # labels already set — spec identity is authoritative already
        class _SpecWithLabels(_FakeSpec):
            labels = {"app": "existing"}
        with patch("chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
                   new_callable=AsyncMock) as mock_exec:
            assert await _discover_pod_selector(_ctx(spec=_SpecWithLabels())) is None
        mock_exec.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_selector_fail_open(self):
        # Selectorless service returns empty/None jsonpath
        fake = MagicMock(exit_code=0, stdout="", stderr="")
        with patch("chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
                   new_callable=AsyncMock, return_value=fake):
            assert await _discover_pod_selector(_ctx()) is None


class TestTargetContextIdentity:

    def test_kind_semantics_and_selector_line(self):
        ctx = _build_target_context(
            "deployment", "process", "kill", "default",
            ("drill-pvc-target",), None, {"app": "drill-pvc"},
        )
        assert "Resource names (kind=deployment): drill-pvc-target" in ctx
        assert "Pod label selector (authoritative, read from the deployment's own spec.selector): app=drill-pvc" in ctx

    def test_rules_with_selector(self):
        rules = _identity_rules_block("deployment", ("drill-pvc-target",), {"app": "drill-pvc"})
        assert "AUTHORITATIVE" in rules
        assert "NEVER invent" in rules

    def test_rules_without_selector_forbid_guessing(self):
        rules = _identity_rules_block("deployment", ("drill-pvc-target",), None)
        assert "Do NOT guess" in rules
        assert "workload objects" in rules

    def test_pod_scope_has_no_identity_rules(self):
        # pod-scope names ARE pod names — no cross-kind translation, no rules
        assert _identity_rules_block("pod", ("p1",), None) == ""


# ---------------------------------------------------------------------------
# End-to-end: R10 live replay through the real node function
# ---------------------------------------------------------------------------

_R10_DERIVE_OUTPUT = json.dumps([
    {"description": "Deployment status",
     "command": "kubectl get deployment drill-pvc-target -n default", "mode": "simple"},
    {"description": "Pods",
     "command": "kubectl get pods -n default -l app=drill-pvc-target -o wide", "mode": "simple"},
    {"description": "Top pods",
     "command": "kubectl top pods -n default -l app=drill-pvc-target", "mode": "simple"},
    {"description": "ConfigMap",
     "command": "kubectl get cm drill-app-config -n default", "mode": "simple"},
])

_R10_RETRY_OUTPUT = json.dumps([
    {"verdict": "replace", "reason": "wrong label selector: deployment name used as label value",
     "command": "kubectl get pods -n default -l app=drill-pvc -o wide",
     "description": "Pods"},
    {"verdict": "replace", "reason": "wrong label selector: deployment name used as label value",
     "command": "kubectl top pods -n default -l app=drill-pvc",
     "description": "Top pods"},
])


class TestR10ReplayEndToEnd:
    """The full #16 scenario through make_baseline_capture's node.

    Inputs reproduce the R10 live state verbatim: deployment exists with
    selector app=drill-pvc; pods/top/CM queries spin empty (invented label
    / planned creation); the approved plan (in message history) declares
    the CM is created during execute. Expected post-fix behaviour:

      * the derive prompt carries the AUTHORITATIVE selector (fix A) —
        the LLM never needed to invent one;
      * the CM observation is machine-marked expected_absence BEFORE any
        retry judgment (fix B) — no retry round is burned on it;
      * the two wrong-label empty spins enter the retry loop (fix C),
        get replaced, and the final counts are honest: 4 success =
        4 valid + 0 empty.
    """

    @pytest.mark.asyncio
    async def test_full_chain(self):
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(side_effect=[
            MagicMock(content=_R10_DERIVE_OUTPUT),   # 1: derive
            MagicMock(content=_R10_RETRY_OUTPUT),    # 2: retry (empty spins)
        ])
        node = make_baseline_capture(llm=llm, registry=None)

        state = {
            "task_id": "r16-replay",
            "fault_scope": "deployment",
            "fault_target": "process",
            "fault_action": "kill",
            "target": {"namespace": "default", "names": ["drill-pvc-target"], "labels": {}},
            "skill_case_content": "skill case content",
            "messages": [AIMessage(
                content="",
                tool_calls=[{"name": "save_fault_plan", "type": "tool_call",
                             "id": "tc1", "args": {"plan_content": _R10_PLAN}}],
            )],
        }

        exec_calls = {"n": 0}

        async def fake_exec(resolved, kubeconfig, task_id):
            exec_calls["n"] += 1
            if exec_calls["n"] == 1:
                return _r10_observations()
            # retry round: the corrected commands anchor on the real selector
            return [
                {"description": "Pods", "command": "kubectl get pods -n default -l app=drill-pvc -o wide",
                 "exit_code": 0, "stdout": "NAME READY\ndrill-pvc-target-xxx 1/1", "stderr": ""},
                {"description": "Top pods", "command": "kubectl top pods -n default -l app=drill-pvc",
                 "exit_code": 0, "stdout": "NAME CPU\ndrill-pvc-target-xxx 5m", "stderr": ""},
            ]

        selector_result = MagicMock(exit_code=0, stdout='{"app":"drill-pvc"}', stderr="")
        with patch("chaos_agent.agent.nodes.baseline.baseline_capture._execute_observations",
                   new=fake_exec), \
             patch("chaos_agent.agent.nodes.baseline.baseline_capture._lookup_baseline_commands",
                   return_value=[]), \
             patch("chaos_agent.agent.nodes.baseline.baseline_capture.execute_via_transport",
                   new_callable=AsyncMock, return_value=selector_result), \
             patch("chaos_agent.agent.nodes.baseline.baseline_capture.sync_to_store",
                   new_callable=AsyncMock), \
             patch("chaos_agent.agent.nodes.baseline.baseline_capture.sync_node_status_to_session"), \
             patch("chaos_agent.agent.nodes.baseline.baseline_capture.get_tracker") as mock_tracker, \
             patch("chaos_agent.agent.nodes.baseline.baseline_capture.dispatch_node_message",
                   new_callable=AsyncMock):
            mock_tracker.return_value = MagicMock()
            result = await node(state)

        bd = result["baseline_data"]

        # ── Fix A: the derive prompt carried the authoritative selector ──
        first_human = llm.ainvoke.call_args_list[0].args[0][1].content
        assert "Pod label selector (authoritative" in first_human
        assert "app=drill-pvc" in first_human
        assert "Resource names (kind=deployment): drill-pvc-target" in first_human

        # ── Fix B: the CM observation is machine-marked before retry ──
        cm_obs = [o for o in bd["observations"]
                  if "drill-app-config" in (o.get("command") or "")]
        assert len(cm_obs) == 1
        assert cm_obs[0].get("expected_absence")
        assert "planned creation" in cm_obs[0]["expected_absence"]
        # The retry round judged ONLY the two wrong-label empties (2 entries)
        # — the CM never entered retry judgment.
        retry_human = llm.ainvoke.call_args_list[1].args[0][1].content
        assert "2 baseline command(s) exited non-zero or completed with EMPTY output" in retry_human
        assert "drill-app-config" not in retry_human

        # ── Fix C: honest final counts ──
        # All 4 observations end up carrying value (deployment + CM absence
        # + 2 corrected selector queries); nothing spins empty any more.
        assert bd["success_count"] == 4
        assert bd["valid_count"] == 4
        assert bd["empty_count"] == 0
        assert _compute_baseline_confidence(AgentState({"baseline_data": bd})) == "high"

        # Execution ran twice: initial + one retry round (the CM was never
        # re-run — machine-marked absences do not re-enter the loop).
        assert exec_calls["n"] == 2
