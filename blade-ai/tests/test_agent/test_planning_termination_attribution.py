"""Terminal-attribution contract for planning-loop terminations (W-56-6).

Provenance (adversarial review, 2026-09-20): ``reject`` single-sources the
reason it renders as ``safety_reason or outcome.error or "Unknown reason"``
(nodes/gates/reject.py) and documents the assumption that the field was
"written by whichever gate routes here THIS time". The planning loop's
terminal exits never wrote it, and the field is cleared only where a run moves
FORWARD — safety_check's safe branch, plan_change_confirm's approval, the
replan seam's attempt-scoped reset — none of which a same-attempt termination
reaches. Measured on the pre-fix code (real node → real reject, with and
without the residue): a run that died in planning rendered safety_check's
recoverable retry note ("No skill activated — returned to planner for
activation") as its terminal cause, and the status payload served the same
stale text.

Two legs, because either alone can hold without the contract holding:

1. behavioral — drive the REAL node to each reachable exit, then the REAL
   reject node and the REAL status builder, and assert the cause named is the
   exit's own (not the residue, and not a bare category word);
2. structural — every ``fail_state`` call in agent_loop.py must sit inside the
   ``_planning_termination`` seam, so no future exit can terminate without
   publishing a cause.

The exits covered: stall-exhausted and final forced iteration (both text-only
conclusions) and the budget backstop at the single exit (the guard
task-ff057e7f motivated). The ``count > MAX_AGENT_LOOP`` branch is absent by
design — the router rejects on ``count >= max_loop`` first, so it is
unreachable.
"""

from __future__ import annotations

import ast
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from chaos_agent.agent.nodes.execute.agent_loop import make_agent_loop
from chaos_agent.agent.nodes.gates.reject import reject
from chaos_agent.agent.state import build_status_data
from chaos_agent.config.settings import settings

# The residue exactly as safety_check's recoverable branch writes it
# (nodes/gates/safety_check.py: "No skill activated — returned to planner for
# activation" + safety_status "retry").
RESIDUE = "No skill activated — returned to planner for activation"

MAX = 10  # Small cap so every exit is one node call away.

_TEXT_ONLY = "I cannot plan this."

_EXITS: dict[str, dict] = {
    # count below the cap + the stall streak at its threshold → stall exit.
    "stall": {
        "state": {"agent_loop_count": 1, "_plan_text_stall_count": 2},
        "content": _TEXT_ONLY,
        "cause": "without tool use or skill activation",
    },
    # count at the cap → tools unbound, text is the expected handoff.
    "final": {
        "state": {"agent_loop_count": MAX - 1, "_plan_text_stall_count": 0},
        "content": _TEXT_ONLY,
        "cause": "without tool use or skill activation",
    },
    # Empty text skips the final-iteration branch on purpose: the single-exit
    # backstop is then the only thing that can stamp a cause (task-ff057e7f's
    # shape — a conclusion emitted where the branch does not look).
    "backstop": {
        "state": {"agent_loop_count": MAX - 1, "_plan_text_stall_count": 0},
        "content": "",
        "cause": "budget exhausted",
    },
}

_CASES = [
    (kind, residue_text)
    for kind in _EXITS
    for residue_text in (False, True)
]


def _text_only_llm(content: str):
    response = MagicMock()
    response.content = content
    response.tool_calls = []
    response.additional_kwargs = {}
    llm = MagicMock()
    bound_llm = MagicMock()
    bound_llm.ainvoke = AsyncMock(return_value=response)
    llm.bind_tools = MagicMock(return_value=bound_llm)
    llm.ainvoke = AsyncMock(return_value=response)
    return llm


def _patch(monkeypatch):
    import chaos_agent.agent.nodes.execute.agent_loop as loop_mod

    monkeypatch.setattr(loop_mod, "MAX_AGENT_LOOP", MAX)
    monkeypatch.setattr(settings, "max_agent_loop", MAX)
    monkeypatch.setattr(settings, "max_plan_text_stalls", 3)
    monkeypatch.setattr(loop_mod, "compute_env_info", AsyncMock(return_value=""))
    monkeypatch.setattr(loop_mod, "sync_to_store", AsyncMock())


def _base_state(**overrides) -> dict:
    state = {
        "task_id": "task-plan-termination",
        "operation": "inject",
        "messages": [],
        "target": {"namespace": "test-ns"},
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# Leg 1 — behavioral: the real node, the real reject, the real status payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,with_residue",
    _CASES,
    ids=[f"{kind}-{'residue' if residue else 'clean'}" for kind, residue in _CASES],
)
async def test_terminal_exit_names_its_own_cause(monkeypatch, kind, with_residue):
    """Every reachable exit must publish a fresh cause, and the operator-facing
    renderings must use it — with or without an earlier gate's note lying
    around in state."""
    spec = _EXITS[kind]
    _patch(monkeypatch)
    node = make_agent_loop(
        llm=_text_only_llm(spec["content"]), tools=[], skill_catalog="x"
    )
    state = _base_state(**spec["state"])
    if with_residue:
        state["safety_status"] = "retry"
        state["safety_reason"] = RESIDUE

    delta = await node(state)

    # (a) the exit published its own cause, in the same words the machine
    # channels carry (one source, so the two cannot drift apart).
    published = delta["safety_reason"]
    assert spec["cause"] in published, delta
    assert published in delta["error"], delta
    if with_residue:
        assert RESIDUE not in published

    # (b) the router really does route this delta to the reject node — the
    # chain under test is the production one, not a hand-built path.
    from chaos_agent.agent.router import should_continue_agent_loop

    merged = {**state, **delta}
    assert should_continue_agent_loop(merged) == "reject"

    # (c) the reject node renders THAT cause — safety_reason takes the first
    # slot, so a stale value here is a stale terminal reason.
    out = await reject(merged)
    assert out["result"]["reason"] == published
    assert out["error"] == published
    assert RESIDUE not in out["result"]["reason"]

    # (d) the served status payload names it too (CLI status + server route
    # both read this dict).
    payload = build_status_data(state["task_id"], {**merged, **out})
    assert payload["safety_reason"] == published
    assert RESIDUE not in payload["safety_reason"]
    # NOTE: ``safety_status`` is deliberately not asserted here — what a
    # terminal rejection should call itself ("rejected" vs the transient
    # "retry"/"pending" the gate left behind) is an open product decision,
    # and pinning either word in a contract test would pre-empt it.


@pytest.mark.asyncio
async def test_in_flight_iteration_publishes_no_cause(monkeypatch):
    """The backstop guard must stay silent for a loop that is still running:
    publishing a cause on a mid-flight iteration would name a termination that
    has not happened."""
    _patch(monkeypatch)
    node = make_agent_loop(
        llm=_text_only_llm(_TEXT_ONLY), tools=[], skill_catalog="x"
    )
    delta = await node(_base_state(agent_loop_count=1, _plan_text_stall_count=0))

    assert "safety_reason" not in delta
    assert not delta.get("error")


def test_safety_reason_clearers_stay_on_forward_paths():
    """The clearers ``_planning_termination`` names. This pins the replan seam
    (attempt-scoped reset); safety_check's safe branch is pinned in
    test_safety_check.py, and the whole point of the two behavioral legs above
    is that NO forward-path clearer runs on a same-attempt termination."""
    from chaos_agent.agent.state_mgmt.state_lifecycle import replan_reset_state

    reset = replan_reset_state()
    assert "safety_reason" in reset and reset["safety_reason"] is None


# ---------------------------------------------------------------------------
# Leg 2 — structural: no exit can bypass the seam
# ---------------------------------------------------------------------------


def _fail_state_call_sites(tree: ast.AST) -> list[tuple[str, int]]:
    """(enclosing function name, lineno) for every ``fail_state(`` call."""
    sites: list[tuple[str, int]] = []

    def walk(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            nested = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else enclosing
            )
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "fail_state"
            ):
                sites.append((nested, child.lineno))
            walk(child, nested)

    walk(tree, "")
    return sites


def test_agent_loop_builds_failures_only_through_the_seam():
    """A bare ``fail_state`` call terminates without publishing a cause — the
    exact shape of W-56-6, where three exits each remembered the machine
    channels and none remembered the rendered one."""
    from chaos_agent.agent.nodes.execute import agent_loop as loop_mod

    tree = ast.parse(inspect.getsource(loop_mod))
    sites = _fail_state_call_sites(tree)
    assert sites, "no fail_state call found — the module was restructured"

    outsiders = [(fn, line) for fn, line in sites if fn != "_planning_termination"]
    assert outsiders == [], (
        "agent_loop terminal exits must build their failure payload through "
        f"_planning_termination so safety_reason is published with it: {outsiders}"
    )

    # Non-vacuity on the other side: the exits must actually still route
    # through the seam (stall, final forced iteration, transport unsupported —
    # plus the unreachable count>MAX branch), and the budget-backstop exit must
    # still publish its cause by reading the router backstop's sentence back.
    seam_calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_planning_termination"
    ]
    assert len(seam_calls) >= 3, "terminal exits stopped going through the seam"

    read_back = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "setdefault"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "safety_reason"
    ]
    assert read_back, "the budget-backstop exit stopped publishing its cause"
