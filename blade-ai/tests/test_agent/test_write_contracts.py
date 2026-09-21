"""Write-contract suite — machine verification for four cross-module field
contracts that until now existed only as docstrings (W-56-8, 2026-09-20).

Why this file exists
--------------------
The W-56-8 review found four fields whose "who writes it, who reads it, when
it is cleared" contract was *documented but never machine-verified*. Each had
silently drifted away from its documentation, and the drift was invisible
because the tests either pre-fabricated the input (the wall-clock suite stamps
``pipeline_started_at`` by hand) or only exercised helper functions (nothing
asserts that a loop node CALLS the wall-clock publisher):

  * ``pipeline_started_at`` — ``_wall_clock_exceeded``'s docstring said
    "stamped on first agent_loop entry"; there was NO writer anywhere in
    ``src/`` since the 2026-06 agent_loop refactor dropped the stamp — the
    whole Patch-C guard (router checks, four loop consumers,
    ``WALL_CLOCK_TIMEOUT`` category, ``max_inject_seconds`` config) was dead
    code: 4 consumers, 0 producers. FIXED (P1): ``stamp_pipeline_start``
    lives beside the reset table and every run-origin constructor stamps.
    [F1, closed]
  * ``mark_wall_clock_timeout`` coverage — its docstring claims "Each
    LLM-loop node (agent_loop, execute_loop, verifier, recover_verifier)
    calls this just before returning"; it measured 2/4 at review time:
    agent_loop published ONLY on the ``llm is None`` test path, and
    ``nodes/recover/`` had zero call sites while the router comment still
    said "the node stamped a failure". FIXED (P2): agent_loop's main exit
    and both of its read-back paths publish. FIXED (P2b): three recover
    exits publish — which the review's SECOND round measured to be
    insufficient, not wrong: ``_run_layer1_recovery`` has SEVEN round-trip
    exits and only one of them (the Layer 1 failure terminal) published a
    cause, so an expired budget ended a recover run with no reason and the
    session envelope recorded the aborted recovery as ``status="success"``
    while its own session row said ``failed``. FIXED (P2b round 2): all six
    Layer-1 round-trip exits publish, before the delta is persisted, and
    contract 3b drives every exit so a deletion cannot pass unseen.
    [F2 / F2b, closed]
  * ``safety_reason`` freshness on the wall-clock reject edge — reject
    renders ``safety_reason or outcome.error or "Unknown reason"``, but no
    publisher existed on the router-level wall-clock rejection, so the
    operator read an earlier gate's stale note (or "Unknown reason"). FIXED
    (P2): agent_loop reads the stamped cause back into ``safety_reason`` at
    both exits, so the reject's first-priority slot names THIS termination.
    [F2, closed]
  * ``safety_status="rejected"`` write authority — no definition existed;
    two domains wrote it (the safety gates and the agent_loop transport
    exit). ADJUDICATED (P3): the double-write is by design, documented at
    the field declaration in state.py; this file PINS the authority set so
    any change must be a deliberate edit here.  [F3, closed]

Shape of each contract test
---------------------------
1. producer existence (AST) — a field a guard reads must have a writer;
2. claim-vs-implementation — the set of publishers the docstring claims
   must equal the set the AST measures (this catches "the docstring lies");
3. end-to-end sentinel — drive the REAL node -> REAL router -> REAL reject
   and assert the operator-facing reason names THIS termination. Contract 3b
   repeats this shape once per recover-verifier exit: a per-domain call-site
   count (contract 2) cannot see one exit of seven lose its publisher.

The sentinel supplies the expired stamp directly instead of waiting out a
budget: F1's producers now exist, but the wall-clock condition is time
passing, which a test cannot produce deterministically — the injected stamp
stands in for that clock, not for the producer (contract 1 measures the
producer separately).

This suite ran red first: every assertion here failed against the reviewed
code and turned green with the P1/P2 fixes in the same work stream, so a
regression that re-breaks any of them turns this file red again. The F3
entry pins the authority registry for a design that is now documented (see
the field declaration in state.py) — it exists so any future change to the
writer set must be a deliberate edit, not a silent drift.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage

import chaos_agent
from chaos_agent.agent.nodes.execute.agent_loop import make_agent_loop
from chaos_agent.agent.nodes.gates.reject import reject
from chaos_agent.agent.router import (
    mark_wall_clock_timeout,
    should_continue_agent_loop,
)
from chaos_agent.config.settings import settings

# The residue exactly as safety_check's recoverable branch writes it.
RESIDUE = "No skill activated — returned to planner for activation"

BUDGET = 60  # seconds of wall-clock budget enabled for the sentinels

_SRC_ROOT = Path(chaos_agent.__file__).parent

# The four loops mark_wall_clock_timeout's docstring claims to cover, mapped
# to where their publishers may live. Single files for the two execute-loop
# domains (their sibling files are different loops), packages for the verify
# and recover domains (a publisher may sit in the loop file or finalize).
_PUBLISHER_SCOPE: dict[str, list[Path]] = {
    "agent_loop": [_SRC_ROOT / "agent/nodes/execute/agent_loop.py"],
    "execute_loop": [_SRC_ROOT / "agent/nodes/execute/execute_loop.py"],
    "verifier": sorted((_SRC_ROOT / "agent/nodes/verify").rglob("*.py")),
    "recover_verifier": sorted((_SRC_ROOT / "agent/nodes/recover").rglob("*.py")),
}
_CLAIMED_LOOP_NAMES = tuple(_PUBLISHER_SCOPE)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _write_sites(src: str, key: str) -> list[int]:
    """Lines that actually WRITE ``key``: dict-literal entries and
    ``state[key] = ...`` assignments.

    Deliberately blind to the forms that only *mention* the field:
    ``AnnAssign`` (the TypedDict declaration in state.py) and positional
    registrations (the lifecycle ``_p("name", ...)`` table) are not writes.
    """
    tree = ast.parse(src)
    sites: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == key
                ):
                    sites.append(node.lineno)
        elif isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and k.value == key:
                    sites.append(node.lineno)
    return sites


def _call_sites(src: str, func: str) -> list[tuple[int, str]]:
    """(lineno, nearest enclosing ``if`` test source) for every ``func(`` call."""
    tree = ast.parse(src)
    out: list[tuple[int, str]] = []

    def walk(node: ast.AST, if_stack: list[ast.If]) -> None:
        for child in ast.iter_child_nodes(node):
            stack = if_stack + [child] if isinstance(child, ast.If) else if_stack
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == func
            ):
                guard = ast.unparse(stack[-1].test) if stack else ""
                out.append((child.lineno, guard))
            walk(child, stack)

    walk(tree, [])
    return out


def _publisher_call_sites(loop: str) -> list[str]:
    """Where ``loop``'s domain calls ``mark_wall_clock_timeout``.

    For ``agent_loop`` the ``llm is None`` lightweight branch is excluded:
    it lives in the same function as the production path, so a bare
    existence check would go green on the test-only call while the real
    planning loop still publishes nothing.
    """
    sites: list[str] = []
    for path in _PUBLISHER_SCOPE[loop]:
        src = path.read_text(encoding="utf-8")
        rel = path.relative_to(_SRC_ROOT).as_posix()
        for line, guard in _call_sites(src, "mark_wall_clock_timeout"):
            if loop == "agent_loop" and "llm is None" in guard:
                continue
            sites.append(f"{rel}:{line}")
    return sites


def _dict_kv_sites(
    src: str, key: str, value: str, *, skip_keywords: tuple[str, ...] = ()
) -> list[int]:
    """Lines holding a dict literal ``{key: value}``, minus dicts passed as
    ``skip_keywords`` arguments (e.g. ``detail={...}`` session syncs are
    notifications, not state writes)."""
    tree = ast.parse(src)
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in skip_keywords and isinstance(kw.value, ast.Dict):
                    exempt.add(id(kw.value))
    sites: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict) or id(node) in exempt:
            continue
        for k, v in zip(node.keys, node.values):
            if (
                isinstance(k, ast.Constant)
                and k.value == key
                and isinstance(v, ast.Constant)
                and v.value == value
            ):
                sites.append(node.lineno)
    return sites


# ---------------------------------------------------------------------------
# Contract 1 (F1) — pipeline_started_at must have a producer
# ---------------------------------------------------------------------------


def test_pipeline_started_at_has_a_producer():
    """``_wall_clock_exceeded`` reads the stamp; at review time its docstring
    claimed a writer ("stamped on first agent_loop entry") while nothing in
    src/ wrote the field — a guard whose input has no producer is dead code
    no matter how green its unit tests are (they stamped the field by hand).
    P1 restored the producer; this asserts it stays."""
    producers: list[str] = []
    mentions: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        if "pipeline_started_at" not in src:
            continue
        rel = path.relative_to(_SRC_ROOT).as_posix()
        producers += [f"{rel}:{line}" for line in _write_sites(src, "pipeline_started_at")]
        mentions += [
            f"{rel}:{i}"
            for i, ln in enumerate(src.splitlines(), start=1)
            if "pipeline_started_at" in ln
        ]
    assert producers, (
        "no producer: `_wall_clock_exceeded` (router.py) reads "
        "state['pipeline_started_at'] and depends on a run-origin writer "
        "(stamp_pipeline_start in state_lifecycle, applied by the run-origin "
        "constructors) — with the producer gone the whole wall-clock guard "
        "(4 consumers, 1 config knob, 1 FailureCategory) is dead code again. "
        f"Mentions without a write: {mentions}"
    )


# ---------------------------------------------------------------------------
# Contract 1b (F1) — the run-origin builders must actually open the clock
# ---------------------------------------------------------------------------


def test_run_origin_builders_open_the_clock(monkeypatch):
    """Every state constructor that opens a run must stamp a LIVE wall-clock
    origin: the single-inject initial state (CLI/TUI/L4), each batch fault
    iteration, and each recover. Measured without producers: all three leave
    the field at its reset value (0.0) or missing entirely, which the guard
    treats as "no stamp" — the exact reason the whole subsystem was dead."""
    from chaos_agent.agent.router import _wall_clock_exceeded
    from chaos_agent.agent.spec.fault_spec import FaultSpec
    from chaos_agent.agent.state_mgmt.recovery_state import (
        build_recover_initial_from_checkpoint,
    )
    from chaos_agent.agent.state_mgmt.state_builders import build_inject_initial_state
    from chaos_agent.agent.state_mgmt.state_lifecycle import (
        build_batch_iteration_state,
    )

    spec = FaultSpec(
        namespace="write-contract-ns",
        scope="pod",
        names=("pod-a",),
        fault_target="cpu",
        fault_action="fullload",
        params={},
    )
    now = time.time()
    origins = {
        "single-inject": build_inject_initial_state(
            task_id="t-write-single", fault_spec=spec
        ),
        "batch-iteration": build_batch_iteration_state(
            task_id="t-write-batch",
            spec=spec,
            batch_args={},
            created_at="2026-09-20T00:00:00+08:00",
            messages=[],
        ),
        "recover": build_recover_initial_from_checkpoint({}, "task-inject"),
    }

    for name, delta in origins.items():
        stamp = delta.get("pipeline_started_at", 0.0) or 0.0
        assert stamp > 0, f"{name}: no wall-clock origin was stamped (got {stamp!r})"
        assert abs(stamp - now) < 5, f"{name}: stamp is not 'now' ({stamp!r})"

    # The guard's contract must hold on every fresh origin: an opened run can
    # never already be timed out.
    monkeypatch.setattr(settings, "max_inject_seconds", BUDGET)
    for name, delta in origins.items():
        assert _wall_clock_exceeded(delta) is False, name


# ---------------------------------------------------------------------------
# Contract 2 (F2/F2b) — the publish claim must match the implementation
# ---------------------------------------------------------------------------


def test_wall_clock_publisher_coverage_matches_the_claim():
    """The publisher's docstring claims all four LLM loops call it before
    returning. At review time it measured 2/4 — agent_loop published only on
    its ``llm is None`` test path and ``nodes/recover/`` had zero call sites;
    the P2 fixes added the missing exits. Both sides stay pinned so they can
    only agree by keeping the call sites: weakening the docstring is not a
    fix, and a refactor that drops one turns this red."""
    doc = mark_wall_clock_timeout.__doc__ or ""
    claimed = [name for name in _CLAIMED_LOOP_NAMES if name in doc]
    assert set(claimed) == set(_CLAIMED_LOOP_NAMES), (
        "the docstring must keep claiming all four loops — weakening the "
        f"claim instead of adding the call sites is not a fix (claimed: {claimed})"
    )

    measured = {loop: _publisher_call_sites(loop) for loop in claimed}
    missing = [loop for loop, sites in measured.items() if not sites]
    assert not missing, (
        f"claimed to publish but measured no call site: {missing}. "
        f"Full measurement: {measured}"
    )


# ---------------------------------------------------------------------------
# Contract 3 (F2) — the wall-clock reject edge must name ITS termination
# ---------------------------------------------------------------------------


def _tool_call_llm() -> MagicMock:
    """A production-path LLM stub: one ordinary tool call, so the node runs a
    normal iteration and triggers none of its own terminal exits."""
    response = AIMessage(
        content="Check the node first.",
        tool_calls=[
            {
                "name": "kubectl_read",
                "args": {"subcommand": "get", "v_args": "node n1"},
                "id": "tc-wc-1",
            }
        ],
    )
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=response)
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=bound)
    return llm


def _tool_stub() -> MagicMock:
    tool = MagicMock()
    tool.name = "kubectl_read"
    return tool


@pytest.mark.asyncio
@pytest.mark.parametrize("with_residue", [True, False], ids=["residue", "clean"])
async def test_wall_clock_reject_edge_names_the_timeout(monkeypatch, with_residue):
    """Drive the REAL node (one normal iteration, no terminal exit of its
    own) with an expired stamp, then the REAL router and the REAL reject:
    the operator-facing reason must name the wall-clock termination — not an
    earlier gate's note and not "Unknown reason".

    The expired stamp is supplied by the harness instead of waiting out a
    budget: the wall-clock condition is elapsed time, which no test can
    produce deterministically. Contract 1 measures the producers; this
    contract measures that the reject edge, once the budget IS expired,
    names the termination.
    """
    import chaos_agent.agent.nodes.execute.agent_loop as loop_mod

    monkeypatch.setattr(settings, "max_inject_seconds", BUDGET)
    monkeypatch.setattr(settings, "max_agent_loop", 10)
    monkeypatch.setattr(loop_mod, "MAX_AGENT_LOOP", 10)
    monkeypatch.setattr(loop_mod, "compute_env_info", AsyncMock(return_value=""))
    monkeypatch.setattr(loop_mod, "sync_to_store", AsyncMock())

    node = make_agent_loop(
        llm=_tool_call_llm(), tools=[_tool_stub()], skill_catalog="x"
    )
    state = {
        "task_id": "task-write-contract",
        "operation": "inject",
        "agent_loop_count": 1,
        "messages": [],
        "skill_name": "k8s-chaos-skills",
        "pipeline_started_at": time.time() - (BUDGET + 60),  # injected — see docstring
    }
    if with_residue:
        state["safety_status"] = "retry"
        state["safety_reason"] = RESIDUE

    delta = await node(state)
    merged = {**state, **delta}

    # The production router really does end this run at the reject node.
    assert should_continue_agent_loop(merged) == "reject"

    out = await reject(merged)
    reason = out["result"]["reason"]
    assert "wall-clock" in reason or "wall clock" in reason, reason
    if with_residue:
        assert RESIDUE not in reason


@pytest.mark.asyncio
async def test_wall_clock_reject_edge_lightweight_exit(monkeypatch):
    """The ``llm is None`` exit is the other read-back path — and F2's
    original half-measure: it published ``error`` but never ``safety_reason``.
    Pin that an expired budget on this exit also rides both channels, so the
    reject cannot fall back to a stale note on the lightweight path either."""
    import chaos_agent.agent.nodes.execute.agent_loop as loop_mod

    monkeypatch.setattr(settings, "max_inject_seconds", BUDGET)
    state = {
        "task_id": "task-write-contract-lite",
        "operation": "inject",
        "agent_loop_count": 1,
        "messages": [],
        "pipeline_started_at": time.time() - (BUDGET + 60),  # injected — see docstring
    }

    delta = await loop_mod.agent_loop(state)
    assert "wall-clock" in delta.get("error", ""), delta
    assert "wall-clock" in delta.get("safety_reason", ""), delta

    merged = {**state, **delta}
    assert should_continue_agent_loop(merged) == "reject"
    out = await reject(merged)
    assert "wall-clock" in out["result"]["reason"]


# ---------------------------------------------------------------------------
# Contract 3b (F2b) — EVERY recover-verifier round-trip exit publishes
# ---------------------------------------------------------------------------
#
# Contract 2 counts call sites per DOMAIN; this one drives the exits. The
# difference is not academic: with only contract 2 in place, deleting the
# recover domain's Layer-2 publisher kept the whole 983-test recover corpus
# green (measured, W-56-8 review round 2) — the same "documented but
# unverified" shape this file exists to kill, one granularity down. The
# measured gap was worse than a missing reason: an expired budget cut the
# run through ``done`` -> END, and the persisted session envelope recorded
# the aborted recovery as ``status="success"`` while the session row beside
# it said ``failed`` (a self-contradicting audit record).


class _RecoverProviderStub:
    """Native-carrier stub: no deterministic recover, no destroy capability.

    The five hooks below are the whole surface ``_run_layer1_recovery``
    touches on this path; anything else raising AttributeError is the point —
    a new provider hook must be acknowledged here, not silently no-op'd.
    """

    has_deterministic_recover = False
    handle_kind = ""
    is_multi_step = False

    def blocks_deterministic_destroy(self, state, messages) -> bool:
        return False

    def was_fault_create_attempted(
        self, messages, injection_method=None, is_teardown=None
    ) -> bool:
        return False

    def recovery_facts_render(self, state, spec_params=None) -> str:
        return ""

    def layer1_recover_guidance(
        self, state, experiment_uid, combo_native=None, combo_part=None
    ) -> str:
        return ""

    def merge_deterministic_recover_verdict(self, layer1, state, part_override=None):
        return layer1


def _recover_mutating_history() -> list:
    """One mutating kubectl call, no post-mutation observation.

    The Layer-1 success guard is a no-op without a mutating call
    (``last_mutating_idx < 0`` -> nothing to confirm), so its exits are only
    reachable with this history seeded — the guard's own unit tests use the
    same fixture shape.
    """
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "kubectl",
                    "args": {
                        "command": [
                            "scale", "deployment/app", "--replicas", "3",
                            "-n", "default",
                        ]
                    },
                    "id": "tc-rv-mut-1",
                    "type": "tool_call",
                }
            ],
        )
    ]


def _recovery_text_llm(status: str) -> MagicMock:
    """A Layer-1 verdict text: ``success`` reaches the success guard, and
    ``failed`` reaches the Layer-2 transition (Layer 2 verifies either way)."""
    return _bound_llm(
        AIMessage(
            content=(
                "RECOVERY_EXECUTION_RESULT:\n"
                f"- Status: {status}\n"
                "- Actions: reverted the injected field\n"
                "- Details: write-contract fixture\n"
            )
        )
    )


def _bound_llm(response: AIMessage) -> MagicMock:
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=response)
    llm = MagicMock()
    llm.bind_tools = MagicMock(return_value=bound)
    return llm


def _recover_l1_state(count: int, *, guard: bool = False) -> dict:
    state = _recover_base_state()
    state.update({"recover_phase": "layer1_recovery", "verifier_loop_count": count - 1})
    if guard:
        state["messages"] = _recover_mutating_history()
    if count > 1:
        state["layer1_iteration_count"] = 1
        state["recover_layer1_cache"] = {
            "status": "in_progress",
            "details": "",
            "raw_output": "",
            "system_prompt": "layer1 system prompt (cached)",
        }
    return state


def _recover_l2_state() -> dict:
    state = _recover_base_state()
    state.update(
        {
            "recover_phase": "layer2_verification",
            "verifier_loop_count": 1,  # -> count = 2 (continuation round)
            "layer2_context_added": True,
            "layer2_start_count": 2,
            "recover_layer1_cache": {
                "status": "passed",
                "details": "undo landed",
                "raw_output": "",
                "system_prompt": "layer1 system prompt (cached)",
            },
        }
    )
    return state


def _recover_base_state() -> dict:
    return {
        "task_id": "task-write-contract-recover",
        "operation": "recover",
        "recover_task_id": "task-write-contract-inject",
        "parent_task_id": "task-write-contract-inject",
        "messages": [],
        "skill_name": "k8s-chaos-skills",
        "fault_type": "cpu-fullload",
        "injection_method": None,
        "kubeconfig": "",
        "kube_context": "",
        "inject_context": (
            "Injected blade create k8s pod-cpu fullload --cpu-percent 80 on "
            "pod app=cpu-demo in namespace demo; baseline CPU 3%."
        ),
        # Injected expired stamp — see the section docstring.
        "pipeline_started_at": time.time() - (BUDGET + 120),
    }


# The seven round-trip exits of the recover verifier: six in
# ``_run_layer1_recovery`` (first/continuation round x tool-calls /
# success-guard / Layer-2 transition) plus the Layer-2 hot exit. Every one
# of them returns to ``should_continue_recover_verifier``, which cuts an
# expired budget to ``done`` -> END.
_RECOVER_EXIT_CASES = {
    "l1-first-tool-calls": (_recover_l1_state, 1, _tool_call_llm),
    "l1-first-guard": (_recover_l1_state, 1, lambda: _recovery_text_llm("success")),
    "l1-first-to-layer2": (_recover_l1_state, 1, lambda: _recovery_text_llm("failed")),
    "l1-cont-tool-calls": (_recover_l1_state, 2, _tool_call_llm),
    "l1-cont-guard": (_recover_l1_state, 2, lambda: _recovery_text_llm("success")),
    "l1-cont-to-layer2": (_recover_l1_state, 2, lambda: _recovery_text_llm("failed")),
    "l2-round-trip": (_recover_l2_state, None, _tool_call_llm),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case_name", list(_RECOVER_EXIT_CASES), ids=list(_RECOVER_EXIT_CASES)
)
async def test_recover_verifier_exit_publishes_the_wall_clock(monkeypatch, case_name):
    """Drive each REAL recover-verifier exit with an expired stamp, then the
    REAL router and the REAL envelope builders: every exit must name the
    wall-clock termination and persist a FAILURE, never a success.

    Without this, the run reaches ``done`` with an empty cause — the reason
    read back is "" (no ``failure_detail`` to derive from either), the
    status surface reports ``in_progress`` with no ``finished_at``, and
    ``build_recover_session_summary`` writes ``status="success"`` onto a
    recovery the budget cut in half.
    """
    import chaos_agent.agent.nodes.recover._recover_verifier_loop as rvl
    from chaos_agent.agent.result.operation_outcome import read_failure_reason
    from chaos_agent.agent.result.operation_result import recover_task_state_from_values
    from chaos_agent.agent.router import should_continue_recover_verifier
    from chaos_agent.memory.session_finalizer import (
        RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
        build_recover_session_summary,
    )

    monkeypatch.setattr(settings, "max_inject_seconds", BUDGET)
    monkeypatch.setattr(rvl, "sync_to_store", AsyncMock())
    monkeypatch.setattr(
        rvl, "_provider_for_recover", lambda state, handle=None: _RecoverProviderStub()
    )
    monkeypatch.setattr(
        rvl, "_resolve_recover_dispatch", lambda state: (_RecoverProviderStub(), None)
    )

    state_builder, count, llm_builder = _RECOVER_EXIT_CASES[case_name]
    state = (
        state_builder(count) if count is not None else state_builder()
    )

    node = rvl.make_recover_verifier(
        hook=None, llm=llm_builder(), tools=[_tool_stub()], registry=None
    )
    delta = await node(state)
    merged = {**state, **delta}

    # The production router really does end this run on the expired budget.
    assert should_continue_recover_verifier(merged) == "done", delta

    # The operator-facing reason names THIS termination.
    reason = read_failure_reason(merged)
    assert "wall_clock_timeout" in reason, (case_name, delta)

    # The word beside it is the failure word, not a mid-flight one.
    assert recover_task_state_from_values(merged) == "failed", (case_name, delta)

    # The persisted envelope is a failure — the audited defect recorded
    # "success" here, and the session row recorded "failed" in the same
    # record.
    envelope = build_recover_session_summary(
        merged,
        recover_task_id="task-write-contract-recover",
        inject_task_id="task-write-contract-inject",
        inject_state_values={},
        mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
    )
    assert isinstance(envelope, dict) and envelope.get("status") == "fail", envelope


# The router's THIRD cut condition reaches ``done`` only while Layer 2 context
# is absent, so only the Layer-1 exits can be cut by the count. The Layer-2
# exit carrying the same count routes to ``finalize`` instead — a live
# hand-off to a node that judges the run itself, asserted at the publisher
# below.
_RECOVER_L1_CAP_CASES = {
    "l1-first-tool-calls": (1, _tool_call_llm),
    "l1-first-guard": (1, lambda: _recovery_text_llm("success")),
    "l1-first-to-layer2": (1, lambda: _recovery_text_llm("failed")),
    "l1-cont-tool-calls": (2, _tool_call_llm),
    "l1-cont-guard": (2, lambda: _recovery_text_llm("success")),
    "l1-cont-to-layer2": (2, lambda: _recovery_text_llm("failed")),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case_name", list(_RECOVER_L1_CAP_CASES), ids=list(_RECOVER_L1_CAP_CASES)
)
async def test_recover_l1_exit_publishes_the_loop_cap(monkeypatch, case_name):
    """A run that spends its loop budget EXACTLY (``count == cap``) is cut by
    the router's count arm, from an exit carrying no verdict — the same
    no-cause ``done`` the wall-clock arm produced, one condition over, and it
    bypasses every terminal node too.

    Measured (W-56-8 review round 3, probe K1-K3): ``failure_reason=""``, the
    status surface at ``in_progress`` / ``finished_at=''`` / ``duration_ms=0``,
    the session envelope at ``success``, and the CLI data at ``failed`` with no
    error — four surfaces, four different stories. The clock is deliberately
    INTACT here: with an expired stamp the wall-clock arm fires first and this
    test would pass without the publisher under test.
    """
    import chaos_agent.agent.nodes.recover._recover_verifier_loop as rvl
    from chaos_agent.agent.result.operation_outcome import read_failure_reason
    from chaos_agent.agent.result.operation_result import recover_task_state_from_values
    from chaos_agent.agent.result.verdict import FailureCategory
    from chaos_agent.agent.router import should_continue_recover_verifier
    from chaos_agent.memory.session_finalizer import (
        RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
        build_recover_session_summary,
    )

    cap = settings.max_recover_verifier_loop
    monkeypatch.setattr(settings, "max_inject_seconds", BUDGET)
    monkeypatch.setattr(rvl, "sync_to_store", AsyncMock())
    monkeypatch.setattr(
        rvl, "_provider_for_recover", lambda state, handle=None: _RecoverProviderStub()
    )
    monkeypatch.setattr(
        rvl, "_resolve_recover_dispatch", lambda state: (_RecoverProviderStub(), None)
    )

    count, llm_builder = _RECOVER_L1_CAP_CASES[case_name]
    state = _recover_l1_state(count)
    state["pipeline_started_at"] = time.time()  # intact budget — see docstring
    state["verifier_loop_count"] = cap - 1  # -> this round IS the cap

    node = rvl.make_recover_verifier(
        hook=None, llm=llm_builder(), tools=[_tool_stub()], registry=None
    )
    delta = await node(state)
    merged = {**state, **delta}

    # The production router really does end this run on the spent budget.
    assert should_continue_recover_verifier(merged) == "done", delta

    reason = read_failure_reason(merged)
    assert reason == FailureCategory.RECOVERY_VERIFICATION_TIMEOUT.value, (
        case_name,
        delta,
    )
    assert f"recover verifier loop budget exhausted ({cap}/{cap}" in merged["error"], (
        case_name,
        delta,
    )

    # The word beside it is the failure word, not a mid-flight one.
    assert recover_task_state_from_values(merged) == "failed", (case_name, delta)

    # The persisted envelope is a failure, not the "success" the audited
    # defect recorded while the session row beside it said "failed".
    envelope = build_recover_session_summary(
        merged,
        recover_task_id="task-write-contract-recover",
        inject_task_id="task-write-contract-inject",
        inject_state_values={},
        mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
    )
    assert isinstance(envelope, dict) and envelope.get("status") == "fail", envelope


@pytest.mark.asyncio
async def test_recover_cut_publisher_reads_the_delta_first():
    """The router judges the MERGED snapshot; a Layer-2 first round carries
    ``layer2_context_added`` in its delta while ``state`` still holds the
    Layer-1 value. A publisher reading only ``state`` would stamp a cause onto
    a run the router actually routes to ``finalize`` — labelling a task that is
    still running as failed.
    """
    import chaos_agent.agent.nodes.recover._recover_verifier_loop as rvl
    from chaos_agent.agent.router import should_continue_recover_verifier

    cap = settings.max_recover_verifier_loop
    state = {"pipeline_started_at": time.time(), "layer2_context_added": False}

    # Layer-2 first round: the flag lives in the delta only.
    live = rvl._publish_recover_cut_causes(
        state, {"verifier_loop_count": cap, "layer2_context_added": True}, cap
    )
    assert not live.get("failure_reason"), live
    assert not live.get("error"), live
    assert should_continue_recover_verifier({**state, **live}) == "finalize"

    # The same count with no Layer-2 context IS the cut.
    cut = rvl._publish_recover_cut_causes(
        state, {"verifier_loop_count": cap}, cap
    )
    assert cut["failure_reason"] == "recovery_verification_timeout", cut
    assert f"({cap}/{cap}" in cut["error"], cut
    assert should_continue_recover_verifier({**state, **cut}) == "done"


@pytest.mark.asyncio
async def test_recover_clock_cut_keeps_its_word_over_the_loop_cap(monkeypatch):
    """Both cut conditions can hold at once — a run that burns its budget is
    usually near the cap too (probe K5). The clock is the more specific cause,
    so the count arm may add nothing; otherwise the envelope names a loop
    budget as the reason for a run the wall clock ended.
    """
    import chaos_agent.agent.nodes.recover._recover_verifier_loop as rvl
    from chaos_agent.agent.result.operation_outcome import read_failure_reason

    cap = settings.max_recover_verifier_loop
    monkeypatch.setattr(settings, "max_inject_seconds", BUDGET)
    state = {
        "pipeline_started_at": time.time() - (BUDGET + 120),  # expired
        "verifier_loop_count": cap - 1,  # ...and at the cap
    }

    delta = rvl._publish_recover_cut_causes(state, {"verifier_loop_count": cap}, cap)

    assert delta["error"].startswith("wall-clock"), delta
    # Not relabelled — the reason still derives from the clock's own detail.
    assert not delta.get("failure_reason"), delta
    assert "wall_clock_timeout" in read_failure_reason(delta)


# ---------------------------------------------------------------------------
# Contract 4 (F3) — safety_status="rejected" write authority (registry pin)
# ---------------------------------------------------------------------------

# The authority set. The double-write is BY DESIGN (adjudicated in W-56-8 P3
# and documented at the field declaration in state.py): the safety gates hold
# a safety verdict; agent_loop's transport exit reports a configuration
# failure. This registry pins who exists so any change must show up here as a
# deliberate edit. Note agent_loop's two entries: the count>MAX branch (dead
# under the shared max_agent_loop config — the router rejects first) and the
# transport exit ("The requested fault domain cannot run through the
# configured transport" is a configuration failure, not a safety rejection).
REJECTED_WRITE_AUTHORITY: dict[str, int] = {
    "agent/nodes/execute/agent_loop.py": 2,
    "agent/nodes/gates/_write_set_boundary.py": 1,
    "agent/nodes/gates/confirmation_gate.py": 2,
    "agent/nodes/gates/safety_check.py": 3,
}


def test_safety_status_rejected_write_authority_registry():
    """Every new (or removed) writer of the terminal word must be a conscious
    edit of this registry — the field drives task_state, status payloads and
    operator reports, and the two writer domains (safety gates, agent_loop's
    transport exit) are documented at the field declaration in state.py."""
    measured: dict[str, int] = {}
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        if '"rejected"' not in src:
            continue
        sites = _dict_kv_sites(
            src, "safety_status", "rejected", skip_keywords=("detail",)
        )
        if sites:
            measured[path.relative_to(_SRC_ROOT).as_posix()] = len(sites)
    assert measured == REJECTED_WRITE_AUTHORITY, (
        "the set of writers of safety_status='rejected' changed — if this is "
        "deliberate (e.g. the F3 adjudication), update REJECTED_WRITE_AUTHORITY "
        f"in the same change. Measured: {measured}"
    )
