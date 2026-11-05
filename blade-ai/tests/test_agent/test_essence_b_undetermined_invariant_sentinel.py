"""Essence-B class invariant sentinel — "could not determine" must be a
FIRST-CLASS state, never laundered into a benign OK / empty / absent value.

BUG CLASS (essence-B): a probe / measure / parse / query that CAN FAIL
collapses its failure into the SAME return value as a legitimate benign,
empty, or absent result. Downstream then reads "the program could not
determine X" as "X is fine / nothing there" — a silent green. Every incident
in this repo's history was the same shape, fixed one face at a time:

  * target_health (R66)   — query failure → fake OK health report
  * side-effect (W-83-5)  — detector failure → "no side effect"
  * feasibility network (W-83-6) — unresolvable target → None ≡ OK
  * baseline observation (#16)  — empty spin counted as valid coverage
  * feasibility probe-read / checker-exception (this work) — read failure → None ≡ OK
  * baseline retry parse / exception / timeout (this work, #78-R2) — a bare-dict
    verdict or an unparseable answer → [] ≡ "LLM decided nothing" → empty bucket

None of those fixes enumerated the CLASS. This sentinel is the mechanical 收口
(穷举清单 + 单一 registry, per the横切-audit discipline): a PINNED registry of
every known face + a per-face assertion that the failure path yields the face's
FIRST-CLASS "undetermined" marker and NEVER the benign value it used to
launder into.

WHAT THIS SENTINEL GUARDS (and what it deliberately does not):
  It pins the ENUMERATED faces — the marker vocabulary must exist in each
  face's module, the faces fixed here are re-asserted at runtime, and the
  specific laundering signatures are AST/text-pinned absent so a revert trips.
  It does NOT attempt an open-ended "find every essence-B in the repo" sweep:
  unlike a jsonpath literal, "a failure return that downstream reads as benign"
  is semantic, and a syntactic ``return None`` sweep would drown in legitimate
  None-returning functions. Enumerating a NEW face is a conscious review act —
  add it to ``_FACES`` with its marker and (if fixed here) a behavioral test.
  Completeness is approached, never declared.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src"

# ---------------------------------------------------------------------------
# The pinned registry — EVERY known essence-B consumer face.
#
# face_id -> (module relpath under src/, undetermined-marker substring that
#             MUST be present in that module, the benign value the failure
#             must NOT collapse into).
#
# This dict IS the enumeration. It cannot silently shrink (test_registry_is_
# pinned) and every entry's marker must be found in source (test_every_face_
# carries_its_undetermined_marker). A new failure-returning consumer that is
# NOT here has no guard — review must confront that gap consciously.
# ---------------------------------------------------------------------------
_FACES: dict[str, tuple[str, str, str]] = {
    "target_health_query": (
        "chaos_agent/agent/target_health.py",
        "health_check_unknown",
        "an OK health report (false negative in the safety gate)",
    ),
    "side_effect_detector": (
        "chaos_agent/agent/nodes/side_effect/se_detect.py",
        "_skipped_detectors",
        "'no side effect' (a silent green on collateral damage)",
    ),
    "feasibility_network_target": (
        "chaos_agent/agent/spec/_feasibility_checkers.py",
        "Could not resolve a Running target Pod",
        "None ≡ OK — a network check that never ran reads as feasible",
    ),
    "feasibility_probe_read": (
        "chaos_agent/agent/spec/_feasibility_checkers.py",
        "_probe_read_skipped_report",
        "None ≡ OK — a failed usage/capacity read reads as feasible",
    ),
    "feasibility_checker_exception": (
        "chaos_agent/agent/spec/feasibility.py",
        "feasibility check errored",
        "None ≡ OK — an errored checker reads as feasible",
    ),
    "baseline_observation_empty": (
        "chaos_agent/agent/nodes/baseline/baseline_capture.py",
        "empty_count",
        "an empty spin counted as valid coverage / quality",
    ),
    "baseline_retry_parse": (
        "chaos_agent/agent/nodes/baseline/_llm_derive.py",
        "receive_failure",
        "[] ≡ 'the LLM decided nothing' → the observation launders to empty",
    ),
    "baseline_undetermined_count": (
        "chaos_agent/agent/nodes/baseline/baseline_capture.py",
        "undetermined_count",
        "the empty bucket — 'no verdict received' read as 'value was empty'",
    ),
}

# Faces fixed in THIS work get a direct runtime assertion below (the marker
# sweep alone only proves the vocabulary exists, not that the failure path
# actually routes to it). Pinned so the behavioral matrix cannot silently drop
# a face it is responsible for.
_BEHAVIORALLY_GUARDED = frozenset({
    "feasibility_probe_read",
    "feasibility_checker_exception",
    "baseline_retry_parse",
    "baseline_undetermined_count",
})


def _read(rel: str) -> str:
    path = _SRC / rel
    assert path.exists(), f"face module vanished: {rel}"
    return path.read_text(encoding="utf-8")


def _func(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _returns_none(ret: ast.Return) -> bool:
    return ret.value is None or (
        isinstance(ret.value, ast.Constant) and ret.value.value is None
    )


# ---------------------------------------------------------------------------
# Invariant 1 — the registry is the pinned enumeration (cannot silently drift)
# ---------------------------------------------------------------------------


def test_registry_is_pinned():
    """The face set is EXACTLY this pinned list. A shrink means a face lost its
    guard (fail-open — the class silently re-opens); a growth means a new face
    was added, which is fine but must be a conscious, reviewed act — update the
    pin deliberately, never let it drift."""
    assert set(_FACES) == {
        "target_health_query",
        "side_effect_detector",
        "feasibility_network_target",
        "feasibility_probe_read",
        "feasibility_checker_exception",
        "baseline_observation_empty",
        "baseline_retry_parse",
        "baseline_undetermined_count",
    }
    # Every behaviorally-guarded face must also be a registered face.
    assert _BEHAVIORALLY_GUARDED <= set(_FACES)


def test_registry_entries_are_well_formed():
    for face_id, entry in _FACES.items():
        assert isinstance(entry, tuple) and len(entry) == 3, face_id
        rel, marker, benign = entry
        assert rel and marker and len(benign.strip()) > 10, (
            f"face {face_id} lacks a real marker / benign-value description"
        )


# ---------------------------------------------------------------------------
# Invariant 2 — every face's module still carries its undetermined marker
# (the mechanical 收口: the first-class state exists in code, per face)
# ---------------------------------------------------------------------------


def test_every_face_carries_its_undetermined_marker():
    missing: dict[str, str] = {}
    for face_id, (rel, marker, _benign) in _FACES.items():
        if marker not in _read(rel):
            missing[face_id] = f"{marker!r} not found in {rel}"
    assert not missing, (
        "these essence-B faces lost their first-class undetermined marker — the "
        f"failure path may have reverted to laundering into the benign value: "
        f"{missing}"
    )


def test_marker_sweep_is_not_vacuous():
    """Cross-check against 静默全漏 (a broken read making every ``in`` test
    trivially pass/fail): the modules are non-empty AND a marker we KNOW is
    present is found. An empty sweep result is a red flag, not 'clean'."""
    for rel in {entry[0] for entry in _FACES.values()}:
        assert len(_read(rel).strip()) > 0, f"module read empty: {rel}"
    # Positive control: a marker added by this work must be found.
    assert "_probe_read_skipped_report" in _read(
        "chaos_agent/agent/spec/_feasibility_checkers.py"
    )
    assert "undetermined_count" in _read(
        "chaos_agent/agent/nodes/baseline/baseline_capture.py"
    )


# ---------------------------------------------------------------------------
# Invariant 3 — behavioral matrix: failure → FIRST-CLASS marker, NEVER benign.
# Each test asserts the marker is produced AND (旧值反向断言) the pre-fix
# benign value is NOT.
# ---------------------------------------------------------------------------


def _mem_spec():
    from chaos_agent.agent.spec.fault_spec import FaultSpec

    return FaultSpec(
        namespace="cms-demo", scope="pod", names=("accounting-6fbdb464c7-qn2vr",),
        fault_target="mem", fault_action="load", params={"mem-percent": "98"},
    )


@pytest.mark.asyncio
async def test_face_feasibility_probe_read_yields_skipped_not_ok():
    """feasibility_probe_read: a FAILED usage read (metrics-server returned
    nothing for this target) is a capability loss → SKIPPED, never None≡OK."""
    from chaos_agent.agent.spec.feasibility import (
        FeasibilitySeverity, assess_feasibility,
    )
    import chaos_agent.agent.spec._feasibility_checkers as _fc

    _fc._metrics_probe_cache = None
    with patch.object(_fc, "is_metrics_server_available",
                      new_callable=AsyncMock, return_value=True), \
         patch.object(_fc, "_fetch_memory_usage_mb",
                      new_callable=AsyncMock, return_value=None), \
         patch.object(_fc, "_fetch_memory_limit_mb",
                      new_callable=AsyncMock, return_value=240):
        report = await assess_feasibility(_mem_spec(), "/fake/kubeconfig")

    assert report is not None, "regression: read failure laundered into None≡OK"
    assert report.severity == FeasibilitySeverity.SKIPPED


@pytest.mark.asyncio
async def test_face_feasibility_checker_exception_yields_skipped_not_ok():
    """feasibility_checker_exception: a REGISTERED checker that raised could
    not determine feasibility → SKIPPED, never None≡OK."""
    from chaos_agent.agent.spec.feasibility import (
        FeasibilitySeverity, assess_feasibility,
    )
    import chaos_agent.agent.spec._feasibility_checkers as _fc

    _fc._metrics_probe_cache = None
    with patch.object(_fc, "is_metrics_server_available",
                      new_callable=AsyncMock, return_value=True), \
         patch.object(_fc, "_fetch_memory_usage_mb",
                      new_callable=AsyncMock, side_effect=RuntimeError("boom")):
        report = await assess_feasibility(_mem_spec(), "/fake/kubeconfig")

    assert report is not None, "regression: errored checker laundered into None≡OK"
    assert report.severity == FeasibilitySeverity.SKIPPED


def test_face_baseline_parse_receive_failure_is_none_not_empty():
    """baseline_retry_parse: non-empty-but-unparseable output is a RECEIVE
    FAILURE (None) — distinguishable from a legitimate empty verdict ([])."""
    from chaos_agent.agent.nodes.baseline._llm_derive import _parse_llm_json_output

    assert _parse_llm_json_output("not json at all") is None, (
        "regression: receive failure collapsed back into [] ≡ 'LLM decided nothing'"
    )
    # The two states MUST stay distinguishable.
    assert _parse_llm_json_output("[]") == []


def test_face_baseline_parse_bare_dict_verdict_not_dropped():
    """baseline_retry_parse: the N==1 bare-object shape is salvaged to a
    one-element list so the verdict inside is NOT silently dropped."""
    from chaos_agent.agent.nodes.baseline._llm_derive import _parse_llm_json_output

    result = _parse_llm_json_output(
        '{"verdict":"expected_absence","reason":"absent pre-injection"}'
    )
    assert result == [{"verdict": "expected_absence", "reason": "absent pre-injection"}]
    assert result != [], "regression: bare-dict verdict dropped back to []"


@pytest.mark.asyncio
async def test_face_baseline_retry_exception_marks_undetermined_not_empty():
    """baseline_retry_parse: a retry call that raised received NO verdict →
    receive_failure + undetermined, never the bare empty shape that let the
    observation launder into the empty bucket."""
    from chaos_agent.agent.nodes.baseline._llm_derive import _llm_retry_failed_commands

    mock_llm = AsyncMock()
    mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("API error"))
    result = await _llm_retry_failed_commands(
        mock_llm, "skill", "pod", "process", "kill",
        [{"command": "bad", "exit_code": 1, "stderr": "err"}],
    )
    assert result.get("receive_failure") is True
    assert result.get("undetermined") == [
        {"command": "bad", "exit_code": 1, "stderr": "err"}
    ]


def test_face_baseline_undetermined_excluded_from_empty_count():
    """baseline_undetermined_count: an observation whose verdict was never
    received is counted UNDETERMINED, not laundered into the empty bucket."""
    import chaos_agent.agent.nodes.baseline.baseline_capture as bc

    class _FakeSpec:
        scope = "deployment"
        fault_target = "process"
        fault_action = "kill"
        namespace = "default"
        names = ("drill-target",)
        labels = {}

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

    plain_empty = {
        "description": "d1", "command": "c1",
        "exit_code": 0, "stdout": "No resources found",
    }
    undetermined = {
        "description": "d2", "command": "c2",
        "exit_code": 0, "stdout": "No resources found",
        "undetermined_verdict": "retry 1 returned no parseable verdict",
    }
    with patch.object(bc, "EvidenceProfile", _EP), \
         patch.object(bc, "_target_coverage", return_value={"applicable": False}):
        bd = bc._assemble_baseline_result(
            _FakeSpec(), "k8s", "llm", [], [plain_empty, undetermined], {}, {},
        )["baseline_data"]

    assert bd["empty_count"] == 1, "regression: undetermined obs laundered into empty"
    assert bd["undetermined_count"] == 1
    assert bd["empty_count"] != 2, "both empties collapsed — undetermined not split out"


# ---------------------------------------------------------------------------
# Invariant 4 — structural anti-pattern sweep: the exact laundering signatures
# are pinned ABSENT, so a revert trips even if a behavioral test is edited.
# ---------------------------------------------------------------------------


def test_feasibility_checkers_no_collapsed_probe_failure():
    """The pre-fix collapse ``if m is None or m.limit is None or m.limit == 0:
    return None`` conflated a failed read with a legitimate no-ceiling. It must
    stay split: probe-read failure → _probe_read_skipped_report (≥ 1 def + 3
    call sites: mem / cpu / disk)."""
    src = _read("chaos_agent/agent/spec/_feasibility_checkers.py")
    assert "if m is None or m.limit is None or m.limit == 0:" not in src, (
        "regression: probe-read failure re-collapsed with the no-ceiling case"
    )
    assert src.count("_probe_read_skipped_report(") >= 4, (
        "expected the shared SKIPPED renderer def + mem/cpu/disk call sites"
    )


def test_assess_feasibility_exception_does_not_return_none():
    """AST pin: ``assess_feasibility``'s except handler must return a
    FeasibilityReport (SKIPPED), never a bare ``return None`` that launders an
    errored check into None≡OK."""
    tree = ast.parse(_read("chaos_agent/agent/spec/feasibility.py"))
    fn = _func(tree, "assess_feasibility")
    assert fn is not None, "assess_feasibility vanished — re-pin this sentinel"
    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "assess_feasibility lost its try/except — re-pin this sentinel"
    for handler in handlers:
        returns = [n for n in ast.walk(handler) if isinstance(n, ast.Return)]
        assert returns, "except handler no longer returns — verify the SKIPPED path"
        for ret in returns:
            assert not _returns_none(ret), (
                "regression: assess_feasibility's except handler returns None "
                "again — an errored checker would read as feasible"
            )
            assert isinstance(ret.value, ast.Call), (
                "except handler must return a FeasibilityReport(SKIPPED) call"
            )


def _returns_dict_with_key(ret: ast.Return, key: str) -> bool:
    """True iff ``ret`` returns a dict literal carrying ``key``."""
    val = ret.value
    if not isinstance(val, ast.Dict):
        return False
    return any(
        isinstance(k, ast.Constant) and k.value == key for k in val.keys
    )


def test_retry_exception_does_not_return_bare_empty_shape():
    """AST pin scoped to the EXCEPT handler of ``_llm_retry_failed_commands``:
    a retry call that raised received NO verdict, so its return must carry the
    ``receive_failure`` signal — never the bare ``{"expected": [], "replace":
    []}`` that was indistinguishable from 'the LLM judged nothing' and let the
    observation launder into the empty bucket.

    Scoped to the handler on purpose: the module ALSO returns the bare empty
    shape on the legitimate ``not llm or not failed_observations`` N/A guard
    (nothing was asked, so nothing is undetermined). A whole-file text search
    would flag that genuine N/A path — this pins only the failure path."""
    tree = ast.parse(_read("chaos_agent/agent/nodes/baseline/_llm_derive.py"))
    fn = _func(tree, "_llm_retry_failed_commands")
    assert fn is not None, "_llm_retry_failed_commands vanished — re-pin this sentinel"
    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "_llm_retry_failed_commands lost its try/except — re-pin this sentinel"
    for handler in handlers:
        returns = [n for n in ast.walk(handler) if isinstance(n, ast.Return)]
        assert returns, "except handler no longer returns — verify the receive_failure path"
        for ret in returns:
            assert _returns_dict_with_key(ret, "receive_failure"), (
                "regression: the retry except handler returns a shape without "
                "the receive_failure signal — an errored retry would read as "
                "'the LLM judged nothing' and launder into the empty bucket"
            )
