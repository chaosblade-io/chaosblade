"""Turn-accumulator funnel invariant — the O1 root cure, pinned by AST.

``PreReasoningHook._manage`` has several return branches (no-compaction,
budget-warning, strip-only, compaction-success, compaction-exception) that
MUTUALLY BYPASS each other. The cross-turn accumulators — ``obs_update``
(``metric_observations`` + ``evidence_fingerprints``) and ``retired_update``
(absorbed experiment UIDs) — are computed ONCE up front and must ride on
WHICHEVER branch fires, or the field is lost for that turn and never reaches
its consumer (the verification timeline / D17 value-presence oracle).

The original code hand-spread ``{**obs_update, **retired_update, ...}`` at
every branch. That is a human-discipline invariant: the builders are unit
tested in isolation (``test_hook_evidence_fingerprint`` exercises
``_build_fingerprint_update`` alone), so a branch that forgot a spread would
drop the field silently and NO test would notice — the loss is at the
EMISSION site, not the computation. This is the project's established
cross-turn-state-field defect class: the write-back sites must funnel through
a single helper AND carry an active tooth that machine-scans for hand-written
residue, otherwise the field is inevitably missed at some future branch.

The cure collapses all five emissions through ``_merge_turn_accumulators``.
These tests are the tooth:

1. EVERY ``return`` in ``_manage`` funnels through the helper (structural).
2. The helper is the single source of truth for the merge (behavioral).
3. The checker BITES — a raw-dict or bare return is flagged (mutation proof
   that the tooth is not vacuous).
4. End-to-end: a harvested fingerprint actually survives the REAL ``_manage``
   no-compaction branch, not just the isolated builder.
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.memory import hook as hook_mod
from chaos_agent.memory.hook import PreReasoningHook, _merge_turn_accumulators

SOURCE = Path(hook_mod.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

FUNNEL = "_merge_turn_accumulators"


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _find_manage() -> ast.AsyncFunctionDef:
    for node in ast.walk(TREE):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_manage":
            return node
    raise AssertionError("_manage not found in hook.py — renamed?")


def _direct_returns(fn: ast.AST) -> list[ast.Return]:
    """Return statements belonging DIRECTLY to ``fn`` (nested scopes pruned).

    ``_manage`` currently has no nested def/lambda, but pruning keeps the
    guard correct if one is ever introduced — a nested helper's returns are
    not ``_manage``'s emission sites.
    """
    out: list[ast.Return] = []
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            continue  # prune nested scope
        if isinstance(node, ast.Return):
            out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _is_funnel_call(value: ast.expr | None) -> bool:
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == FUNNEL
    )


def _spread_names(dict_node: ast.Dict) -> set[str]:
    """Names brought in via ``**name`` unpacking inside a dict literal."""
    names = set()
    for key, val in zip(dict_node.keys, dict_node.values):
        if key is None and isinstance(val, ast.Name):
            names.add(val.id)
    return names


# ---------------------------------------------------------------------------
# 1. The structural tooth
# ---------------------------------------------------------------------------


class TestManageFunnelsEveryReturn:
    def test_every_return_funnels_through_merge(self):
        fn = _find_manage()
        returns = _direct_returns(fn)
        # Premise: the invariant is only meaningful because branches bypass
        # each other. If _manage ever collapses to a single return the funnel
        # rationale changes — update this test rather than delete the guard.
        assert len(returns) >= 2, (
            f"expected multiple bypassing return branches, found {len(returns)}"
        )
        offenders = [r for r in returns if not _is_funnel_call(r.value)]
        assert not offenders, (
            "every _manage return branch MUST funnel through "
            f"{FUNNEL}, or the cross-turn accumulators "
            "(metric_observations / evidence_fingerprints / retired UIDs) are "
            "silently dropped on that branch and never reach the verification "
            f"timeline. Offending return line(s): {[r.lineno for r in offenders]}"
        )

    def test_no_return_hand_spreads_accumulators(self):
        # Belt-and-suspenders: name the exact forbidden regression form (the
        # pre-cure ``{**obs_update, **retired_update, ...}`` literal) so the
        # diagnostic points straight at it rather than at "not a funnel call".
        fn = _find_manage()
        for r in _direct_returns(fn):
            if isinstance(r.value, ast.Dict):
                leaked = {"obs_update", "retired_update"} & _spread_names(r.value)
                assert not leaked, (
                    f"line {r.lineno} hand-spreads {sorted(leaked)} instead of "
                    f"funnelling through {FUNNEL}"
                )


# ---------------------------------------------------------------------------
# 2. The helper is the single source of truth
# ---------------------------------------------------------------------------


class TestMergeHelperSemantics:
    def test_merges_all_three_sources(self):
        assert _merge_turn_accumulators(
            {"a": 1}, {"b": 2}, {"c": 3}
        ) == {"a": 1, "b": 2, "c": 3}

    def test_payload_optional(self):
        assert _merge_turn_accumulators({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
        assert _merge_turn_accumulators({}, {}, None) == {}

    def test_accumulators_survive_empty_payload(self):
        # The branch that computed nothing still carries the accumulators —
        # the whole point: a no-op turn must not drop a harvested field.
        assert _merge_turn_accumulators(
            {"evidence_fingerprints": {"tc": 1}}, {}
        ) == {"evidence_fingerprints": {"tc": 1}}

    def test_payload_wins_on_collision(self):
        # Payload spreads LAST, matching the pre-cure return order where
        # messages/compressed_summary were explicit keys after the accumulator
        # spreads. The accumulators never carry those keys, so this pins the
        # ordering invariant rather than a live collision.
        merged = _merge_turn_accumulators(
            {"messages": "acc"}, {}, {"messages": "payload"}
        )
        assert merged["messages"] == "payload"


# ---------------------------------------------------------------------------
# 3. Mutation proof — the tooth bites
# ---------------------------------------------------------------------------


_MUTANT_RAW_SPREAD = '''
async def _manage(self, state):
    obs_update, retired_update = {}, {}
    if state.get("x"):
        return {**obs_update, **retired_update, "messages": []}
    return _merge_turn_accumulators(obs_update, retired_update)
'''

_MUTANT_BARE_RETURN = '''
async def _manage(self, state):
    obs_update, retired_update = {}, {}
    if state.get("x"):
        return
    return _merge_turn_accumulators(obs_update, retired_update)
'''


def _offenders_in(src: str) -> list[ast.Return]:
    fn = ast.parse(src).body[0]
    return [r for r in _direct_returns(fn) if not _is_funnel_call(r.value)]


class TestToothIsNotVacuous:
    def test_flags_raw_dict_spread(self):
        # A future branch that reverts to hand-spreading MUST be caught.
        bad = _offenders_in(_MUTANT_RAW_SPREAD)
        assert len(bad) == 1, "the guard must flag exactly the hand-spread branch"
        assert isinstance(bad[0].value, ast.Dict)

    def test_flags_bare_return(self):
        # A bare ``return`` drops every accumulator — the silent-loss shape.
        bad = _offenders_in(_MUTANT_BARE_RETURN)
        assert len(bad) == 1
        assert bad[0].value is None

    def test_passes_clean_funnel(self):
        # The checker is not simply "flag everything": a fully-funnelled
        # function has zero offenders.
        clean = '''
async def _manage(self, state):
    obs_update, retired_update = {}, {}
    if state.get("x"):
        return _merge_turn_accumulators(obs_update, retired_update, {"messages": []})
    return _merge_turn_accumulators(obs_update, retired_update)
'''
        assert _offenders_in(clean) == []


# ---------------------------------------------------------------------------
# 4. End-to-end — the field rides a REAL _manage branch
# ---------------------------------------------------------------------------


_NETEM_CMD_ARGS = {
    "subcommand": "exec",
    "v_args": "drill-c3-dup-0 -n default -- tc -s qdisc show dev eth0",
}
_NETEM_OUT = (
    "qdisc netem 8001: dev eth0 root refcnt 2 limit 1000 duplicate 30%\n"
    " Sent 369 bytes 3 pkt (errored 0, dropped 10, overlimits 0, requeues 0)\n"
)


def _ai_tool_call(tool_call_id: str, name: str, args: dict) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": tool_call_id,
                     "type": "tool_call"}],
    )


def _tool(tool_call_id: str, content: str, name: str) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id, name=name)


class TestFingerprintRidesLiveBranch:
    """The builders being correct in isolation is NOT enough — the O1 gap was
    precisely that nothing proved the field survives _manage's emission. Drive
    the real no-compaction branch (the most common turn shape) and assert the
    harvested fingerprint surfaces in the returned state update."""

    async def test_fingerprint_survives_manage_no_compaction_branch(self):
        cm = MagicMock()
        # check_context -> (to_compact, to_keep, valid); empty to_compact hits
        # the ``if not to_compact:`` branch, which returns the funnel merge.
        cm.check_context.return_value = ([], [], True)
        cm.max_tokens = 128_000
        cm.compact_ratio = 0.8
        tc = MagicMock()
        tc.compact.side_effect = lambda msgs, *a, **k: list(msgs)

        hook = PreReasoningHook(
            context_manager=cm,
            tool_compactor=tc,
            session_store=MagicMock(),
        )
        ai = _ai_tool_call("tc-1", "kubectl", _NETEM_CMD_ARGS)
        tm = _tool("tc-1", _NETEM_OUT, "kubectl")

        result = await hook._manage({"messages": [ai, tm], "task_id": "t1"})

        assert "evidence_fingerprints" in result, (
            "the no-compaction branch dropped the harvested fingerprint — the "
            "funnel invariant is violated at the emission site"
        )
        assert "tc-1" in result["evidence_fingerprints"]
        assert "369" in result["evidence_fingerprints"]["tc-1"]["tokens"]
