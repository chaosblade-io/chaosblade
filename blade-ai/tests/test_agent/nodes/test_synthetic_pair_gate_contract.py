"""Cross-module contract of the synthetic-pair dedup gate.

``apply_synthetic_pair_gate`` is unit-tested where it lives
(``tests/test_utils/test_message_integrity.py``). What cannot be tested there
is the agreement between the shared gate and the TWO nodes that call it — and
that agreement is where the recover side used to have no coverage at all:

* each node declares an id set and has a builder that must emit exactly that
  set. If they drift, the gate can never reach ``intact`` and rebuilds — and
  logs — on every single turn, forever;
* each node must hand the gate ITS OWN set and phase, and must pass a COPY of
  ``state["messages"]``, because the node keeps appending to whatever comes
  back;
* the two id sets must stay disjoint. verify and recover_verify both persist
  their synthetic pairs into the same ``state["messages"]``, and recover runs
  after verify, so a shared id would make the recover gate's
  ``drop_messages_with_tool_call_ids`` delete the verify baseline evidence.

These are contract tests in the same spirit as the repo's other ``*_contract``
files: they assert an agreement between modules that no single module's tests
can see, and they are meant to fail loudly on a rename or a refactor that
breaks the agreement rather than on a formatting change.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.message import add_messages

from chaos_agent.agent.nodes.baseline._commands import _is_observation_success
from chaos_agent.agent.nodes.execute.react_helpers import extract_synthetic_messages
from chaos_agent.agent.nodes.recover import _recover_verifier_loop
from chaos_agent.agent.nodes.recover._recover_layer1 import (
    _RECOVER_BASELINE_MSG_ID_CALLER,
    _RECOVER_BASELINE_MSG_ID_RESULT,
    _RECOVER_SYNTHETIC_TOOL_CALL_IDS,
    _build_recover_baseline_tool_messages,
)
from chaos_agent.agent.nodes.verify import _verifier_messages
from chaos_agent.agent.nodes.verify._verifier_messages import (
    _SYNTHETIC_TOOL_CALL_IDS,
    _build_baseline_tool_messages,
)
from chaos_agent.utils.message_integrity import (
    PAIR_DAMAGED,
    PAIR_INTACT,
    apply_synthetic_pair_gate,
    diagnose_synthetic_pairs,
    sanitize_tool_pairing,
)

BASELINE = {
    "captured_at": "2026-05-09T10:00:00",
    "source": "registry",
    "success_count": 1,
    "total_count": 1,
    "observations": [{
        "exit_code": 0,
        "stdout": "NAME   CPU%  MEM%\nmyapp  5%    30%",
        "description": "pod resources",
        "command": "kubectl top pod",
        "resource_name": "myapp-pod",
        "resource_type": "pod",
        "namespace": "default",
    }],
}


def _emitted_tool_call_ids(msgs: list) -> set:
    """Every tool_call id the builder put on the wire, from both halves."""
    ids: set = set()
    for m in msgs:
        if isinstance(m, AIMessage):
            ids.update(tc.get("id") for tc in (m.tool_calls or []))
        elif isinstance(m, ToolMessage):
            ids.add(m.tool_call_id)
    return ids


def _gate_calls(module, func_name: str) -> list[tuple]:
    """Every ``apply_synthetic_pair_gate(...)`` in ``func_name`` as
    ``(id_set_name, phase)``.

    AST, not a string match: what matters is WHICH id set and WHICH phase the
    node hands the shared gate — that is the drift which would silently break
    it — and the assertion has to survive reformatting, comment edits and the
    call moving to another line.
    """
    tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
    funcs = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name
    ]
    assert len(funcs) == 1, f"{func_name} not uniquely found in {module.__name__}"

    found = []
    for node in ast.walk(funcs[0]):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.attr if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", None))
        if name != "apply_synthetic_pair_gate":
            continue
        ids = (node.args[1] if len(node.args) > 1 else next(
            (k.value for k in node.keywords if k.arg == "required_ids"), None))
        phase = next((k.value for k in node.keywords if k.arg == "phase"), None)
        found.append((getattr(ids, "id", None), getattr(phase, "value", None)))
    return found


def _protocol_legal(msgs: list) -> bool:
    """The check a strict provider applies, written independently of the code
    under test: every tool result must answer a PRECEDING call.
    """
    asked: set = set()
    for m in msgs:
        if isinstance(m, AIMessage):
            asked.update(tc.get("id") for tc in (m.tool_calls or []))
        elif isinstance(m, ToolMessage):
            if m.tool_call_id not in asked:
                return False
    return True


# ---------------------------------------------------------------------------
# recover_verify — the side that had zero coverage of this logic
# ---------------------------------------------------------------------------


class TestRecoverPairGate:
    def test_id_set_matches_what_the_builder_emits(self):
        """The invariant whose breakage is silent and permanent.

        An id declared but never emitted → the gate can never reach ``intact``
        and rebuilds every turn forever. An id emitted but not declared → the
        gate never inspects it, and its damage ships. Both directions matter,
        so this is a set equality, not a subset check.
        """
        built = _build_recover_baseline_tool_messages(BASELINE)
        assert _emitted_tool_call_ids(built) == set(_RECOVER_SYNTHETIC_TOOL_CALL_IDS)

    def test_builder_output_is_intact_by_construction(self):
        """A rebuild must produce a set the gate accepts, or it re-triggers
        itself on the next turn — the forever-rebuild failure mode again."""
        built = _build_recover_baseline_tool_messages(BASELINE)
        assert diagnose_synthetic_pairs(built, _RECOVER_SYNTHETIC_TOOL_CALL_IDS) == PAIR_INTACT
        assert sanitize_tool_pairing(built) is built, "and clean for the send side"
        assert _protocol_legal(built)

    def test_builder_uses_stable_message_ids(self):
        """The premise of every non-growth claim in this change: ``add_messages``
        merges by id, so a rebuild only stays idempotent while the ids are
        fixed. Fresh UUIDs would append a whole pair set per turn."""
        first = _build_recover_baseline_tool_messages(BASELINE)
        second = _build_recover_baseline_tool_messages(BASELINE)
        assert [m.id for m in first] == [
            _RECOVER_BASELINE_MSG_ID_CALLER, _RECOVER_BASELINE_MSG_ID_RESULT,
        ]
        assert [m.id for m in second] == [m.id for m in first]

    @pytest.mark.parametrize("baseline,live", [
        ({}, False),
        ({"success_count": 0, "observations": BASELINE["observations"]}, False),
        ({**BASELINE, "observations": []}, False),
        ({**BASELINE, "observations": [{**BASELINE["observations"][0], "exit_code": 1}]}, False),
        ({**BASELINE, "observations": [{**BASELINE["observations"][0], "stdout": ""}]}, True),
    ], ids=["empty", "no_success", "no_observations", "all_observations_failed",
            "success_with_empty_stdout"])
    def test_builder_returns_nothing_for_an_unusable_baseline(self, baseline, live):
        """Every shape that makes both builders return ``[]``.

        These five are NOT equally reachable, and pretending otherwise hides
        the one that matters:

        * ``success_with_empty_stdout`` is the shape that genuinely occurs with
          a self-consistent ``baseline_data``. ``_is_observation_success``
          counts ``exit_code == 0`` with EMPTY stdout as a success — it rejects
          only a non-zero exit or a kubectl error marker inside the text — so
          ``success_count`` is 1 and the node's ``success_count > 0``
          precondition opens the gate, while both builders skip an observation
          with no stdout and return ``[]``. Measured against the real
          predicates, not constructed.
        * ``no_observations`` and ``all_observations_failed`` are INTERNALLY
          INCONSISTENT: they keep ``success_count: 1`` next to observations
          that could not have produced it, because ``_assemble_baseline_result``
          derives ``success_count = len(successful_observations)`` FROM that
          same list. A non-zero exit never reaches the count in the first
          place. They are pinned anyway — ``baseline_data`` can arrive from an
          older schema via resume or a partial write, and the gate must not
          depend on the two fields agreeing.
        * ``empty`` and ``no_success`` never open the gate at all.

        What the gate logs on this branch depends on the SEQUENCE, not on the
        baseline: with a fragment of the pair set present it is a real damage
        WARNING ("NO rebuild"), with no fragment it is the benign INFO from
        ``log_pair_absent``. Both directions are pinned in the helper's tests.
        """
        assert _build_recover_baseline_tool_messages(baseline) == []

        # The reachability claim above, made EXECUTABLE instead of prose.
        # ``live`` means: a baseline_data of this shape that is SELF-CONSISTENT
        # — i.e. its ``success_count`` equals what ``_assemble_baseline_result``
        # would derive from these very observations via
        # ``_is_observation_success`` — would satisfy the node's
        # ``success_count > 0`` precondition and really open the gate.
        # Only the empty-stdout shape qualifies: a non-zero exit never reaches
        # the derived count, an empty observation list cannot produce one, and
        # ``no_success`` pairs healthy observations with a hand-zeroed count.
        # If ``_is_observation_success`` ever tightens to require non-empty
        # stdout, this assertion goes red and the docstring must be re-argued
        # rather than silently left claiming a path that closed.
        derived = sum(
            1 for o in baseline.get("observations", []) if _is_observation_success(o)
        )
        self_consistent = baseline.get("success_count", 0) == derived
        gate_opens = baseline.get("success_count", 0) > 0
        assert (self_consistent and gate_opens) is live, (
            f"reachability drifted: derived={derived} "
            f"success_count={baseline.get('success_count', 0)} "
            f"self_consistent={self_consistent} gate_opens={gate_opens} live={live}"
        )

    def test_three_turns_never_grow_state_and_never_ship_the_fragment(self):
        """The recover gate through the REAL reducer, from a damaged state.

        Mirrors the verify-side characterisation: a fragment whose message id
        predates the stable-id fix can never be replaced in place, so the gate
        reports ``damaged`` every turn without converging. What must hold is
        that it costs nothing but the rebuild — state settles at a fixed point
        and the fragment never reaches the provider.
        """
        tc_id = next(iter(_RECOVER_SYNTHETIC_TOOL_CALL_IDS))
        state = [HumanMessage(content="recover turn 1"),
                 ToolMessage(content="LEGACY-FRAGMENT", tool_call_id=tc_id)]
        ids, shipped = [], []

        for _ in range(3):
            ids.append([m.id for m in state])
            assert diagnose_synthetic_pairs(state, _RECOVER_SYNTHETIC_TOOL_CALL_IDS) == PAIR_DAMAGED, \
                "the gate must keep firing, or the rest of this proves nothing"
            out = apply_synthetic_pair_gate(
                list(state),
                _RECOVER_SYNTHETIC_TOOL_CALL_IDS,
                lambda: _build_recover_baseline_tool_messages(BASELINE),
                phase="recover_verify",
            )
            shipped.append(out)
            state = list(add_messages(
                state, extract_synthetic_messages(out, _RECOVER_SYNTHETIC_TOOL_CALL_IDS),
            ))

        assert ids[1] == ids[2], "state did not settle: it is still churning"
        for out in shipped:
            assert diagnose_synthetic_pairs(out, _RECOVER_SYNTHETIC_TOOL_CALL_IDS) == PAIR_INTACT
            assert _protocol_legal(out)
            assert sanitize_tool_pairing(out) is out, "Layer 1 has nothing left to do"
            assert not any(getattr(m, "content", "") == "LEGACY-FRAGMENT" for m in out), \
                "pinned in state, but it must NEVER reach the provider"
        assert any(getattr(m, "content", "") == "LEGACY-FRAGMENT" for m in state)

    def test_gate_is_wired_with_the_recover_id_set_and_phase(self):
        """The wiring itself, pinned on the AST: the call is present with THIS
        node's id set and THIS node's phase — the drift a copy-paste from the
        verify side would introduce. The node IS reachable at runtime (a mocked
        LLM plus ``baseline_data`` in state opens the gate), and
        ``TestRecoverBaselineGateEndToEnd`` in test_recover_verifier.py drives
        it end to end; this assertion complements that by pinning the wiring
        precisely, independent of the 600-line function's other behaviour.
        """
        calls = _gate_calls(_recover_verifier_loop, "_run_layer2_verification")
        assert calls == [("_RECOVER_SYNTHETIC_TOOL_CALL_IDS", "recover_verify")], calls

    def test_gate_receives_a_copy_of_state_messages(self):
        """The precondition stated in the helper's docstring: the node keeps
        appending to whatever the gate returns, so it must not be the state
        list itself. Asserted on the source to pin the exact assignment shape;
        the runtime path through the node is covered end to end by
        ``TestRecoverBaselineGateEndToEnd`` in test_recover_verifier.py.
        """
        src = Path(inspect.getfile(_recover_verifier_loop)).read_text(encoding="utf-8")
        assert 'messages = list(state.get("messages", []))' in src


# ---------------------------------------------------------------------------
# verify — the same invariants, which its node-level tests did not cover
# ---------------------------------------------------------------------------


class TestVerifyPairGateInvariants:
    def test_id_set_matches_what_the_builder_emits(self):
        """Two pairs here, not one: the metrics pair was the half the old
        single-id probe never looked at."""
        built = _build_baseline_tool_messages(BASELINE, "cpu", "fullload", injection_parsed={})
        assert _emitted_tool_call_ids(built) == set(_SYNTHETIC_TOOL_CALL_IDS)
        assert len(_SYNTHETIC_TOOL_CALL_IDS) == 2
        assert len(built) == 2 * len(_SYNTHETIC_TOOL_CALL_IDS), "one caller + one result per id"

    def test_builder_output_is_intact_by_construction(self):
        built = _build_baseline_tool_messages(BASELINE, "cpu", "fullload", injection_parsed={})
        assert diagnose_synthetic_pairs(built, _SYNTHETIC_TOOL_CALL_IDS) == PAIR_INTACT
        assert sanitize_tool_pairing(built) is built
        assert _protocol_legal(built)

    def test_builder_uses_stable_message_ids(self):
        first = _build_baseline_tool_messages(BASELINE, "cpu", "fullload", injection_parsed={})
        second = _build_baseline_tool_messages(BASELINE, "cpu", "fullload", injection_parsed={})
        assert [m.id for m in first] == [m.id for m in second]
        assert all(m.id and m.id.startswith("synthetic:") for m in first)

    def test_gate_is_wired_with_the_verify_id_set_and_phase(self):
        calls = _gate_calls(_verifier_messages, "_build_layer2_messages")
        assert calls == [("_SYNTHETIC_TOOL_CALL_IDS", "verify")], calls

    def test_gate_receives_a_copy_of_state_messages(self):
        src = Path(inspect.getfile(_verifier_messages)).read_text(encoding="utf-8")
        assert 'messages = list(state.get("messages", []))' in src


# ---------------------------------------------------------------------------
# The two gates must not drift into one another
# ---------------------------------------------------------------------------


class TestTheTwoGatesCannotDrift:
    def test_id_sets_are_disjoint(self):
        """Both nodes persist their synthetic pairs into the SAME
        ``state["messages"]`` and recover runs after verify, so a shared id
        would let the recover gate's ``drop_messages_with_tool_call_ids``
        delete the verify baseline evidence — silently, and only in a full
        inject-then-recover run, which no single-node test exercises.
        """
        assert not (set(_SYNTHETIC_TOOL_CALL_IDS) & set(_RECOVER_SYNTHETIC_TOOL_CALL_IDS))

    def test_message_ids_are_disjoint_too(self):
        """Same reason one level down: ``add_messages`` merges by message id, so
        a collision would make one node's pair silently overwrite the other's
        in place."""
        verify_ids = {
            m.id for m in _build_baseline_tool_messages(
                BASELINE, "cpu", "fullload", injection_parsed={},
            )
        }
        recover_ids = {
            m.id for m in _build_recover_baseline_tool_messages(BASELINE)
        }
        assert not (verify_ids & recover_ids)

    def test_neither_node_reimplements_the_gate(self):
        """The point of extracting it. If a node grows its own inline
        diagnose/drop/rebuild again, the two copies drift and only one gets
        fixed — which is how the recover side ended up untested in the first
        place.
        """
        for module in (_verifier_messages, _recover_verifier_loop):
            src = Path(inspect.getfile(module)).read_text(encoding="utf-8")
            for inlined in ("diagnose_synthetic_pairs(", "drop_messages_with_tool_call_ids("):
                assert inlined not in src, f"{module.__name__} re-inlined {inlined}"
            assert "apply_synthetic_pair_gate(" in src

    def test_both_gates_survive_a_full_inject_then_recover_history(self):
        """The only place the two id sets meet: one state carrying BOTH nodes'
        synthetic pairs. Each gate must repair its own set and leave the other
        node's evidence alone — the disjointness assertions above, executed
        rather than merely declared.
        """
        verify_pair = _build_baseline_tool_messages(
            BASELINE, "cpu", "fullload", injection_parsed={},
        )
        recover_pair = _build_recover_baseline_tool_messages(BASELINE)
        state = [HumanMessage(content="inject"), *verify_pair,
                 HumanMessage(content="recover"), *recover_pair]

        # damage the RECOVER pair only, then run the recover gate
        damaged = [m for m in state if m.id != _RECOVER_BASELINE_MSG_ID_CALLER]
        out = apply_synthetic_pair_gate(
            list(damaged),
            _RECOVER_SYNTHETIC_TOOL_CALL_IDS,
            lambda: _build_recover_baseline_tool_messages(BASELINE),
            phase="recover_verify",
        )

        assert diagnose_synthetic_pairs(out, _RECOVER_SYNTHETIC_TOOL_CALL_IDS) == PAIR_INTACT
        # Filtered by MESSAGE id, not tool_call id: tool_call id is exactly what
        # a collision between the two sets would erase, so filtering on it made
        # this assertion blind to the very drift it exists to catch (measured —
        # with the sets collided, the recover gate really does drop the verify
        # evidence, and a tool_call-id filter counted the fresh recover pair as
        # a survivor and passed).
        verify_msg_ids = {m.id for m in verify_pair}
        verify_survivors = [m for m in out if m.id in verify_msg_ids]
        assert len(verify_survivors) == len(verify_pair), \
            "the recover gate must not touch the verify baseline evidence"
        assert diagnose_synthetic_pairs(out, _SYNTHETIC_TOOL_CALL_IDS) == PAIR_INTACT, \
            "and the verify set is still intact afterwards"
        assert _protocol_legal(out)
        assert sanitize_tool_pairing(out) is out
