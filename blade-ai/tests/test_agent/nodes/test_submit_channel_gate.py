"""Channel compatibility gate at intent submission.

The submitted ``scope`` asserts a fault domain; the resolved transport decides
what can actually receive commands. A mismatch is certain to be rejected later
by ``agent_loop``'s capability gate, so ``intent_clarification`` rejects it at
submit time instead — the user learns why immediately rather than after a full
planning round (~4.3s of LLM work plus an English transport error).

The verdict is code-side on purpose and is the SAME call ``agent_loop`` makes
(``build_capability_context(state, "plan", tools)``), so the two gates cannot
disagree and neither can hallucinate the way a prompt-driven self-check could.

Tests stub ``resolve_channel_name`` rather than building states: real resolution
consults ``settings`` defaults and field inference, so a state-driven test would
silently depend on the developer's ``~/.blade-ai`` config — that exact trap
produced a false "ssh fields resolve to k8s" conclusion during development.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest

from chaos_agent.agent.nodes.planning import intent_clarification as ic


def _gate_blocks(state: dict, tools=()) -> bool:
    """The gate's verdict — literally the call the production code makes.

    Not a re-derivation from ``family_for_scope`` / ``profile_of``: a mirrored
    predicate would be a second source of truth and could drift from the node it
    is meant to describe. Sharing ``build_capability_context`` is the point.
    """
    from chaos_agent.agent.capabilities import build_capability_context

    return not build_capability_context(state, "plan", tools).supported


_TOOLS = (
    SimpleNamespace(name="kubectl_read"),
    SimpleNamespace(name="host_read"),
    SimpleNamespace(name="finish_planning"),
)


# ---------------------------------------------------------------------------
# The shared verdict helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "scope,channel_fields",
    [
        ("host", {"kube_connection_mode": "kubewiz_host", "host_name": "h1"}),
        ("host", {"kube_connection_mode": "ssh", "ssh_host": "10.0.0.5"}),
        ("pod", {"kube_connection_mode": "kubewiz_k8s"}),
        ("node", {"kube_connection_mode": "kubeconfig"}),
    ],
)
def test_helper_passes_compatible_pairs(scope, channel_fields):
    assert ic._capability_reject_message(channel_fields, scope, _TOOLS) is None


@pytest.mark.parametrize(
    "scope,channel_fields",
    [
        ("host", {"kube_connection_mode": "kubewiz_k8s"}),   # the reported case
        ("pod", {"kube_connection_mode": "ssh", "ssh_host": "10.0.0.5"}),
        ("node", {"kube_connection_mode": "kubewiz_host", "host_name": "h1"}),
    ],
)
def test_helper_rejects_conflicts_with_channel_named(scope, channel_fields):
    msg = ic._capability_reject_message(channel_fields, scope, _TOOLS)
    assert msg is not None
    assert "does not match the current drill environment" in msg
    assert scope in msg
    assert "Environment channel" in msg


def test_helper_uses_unconfigured_wording_for_shell_environments():
    """No connection field → do not name the process-default channel."""
    msg = ic._capability_reject_message({}, "host", _TOOLS)
    assert msg is not None
    assert "no usable connection configured" in msg
    assert "Environment connection: not configured" in msg
    # must NOT leak the settings default the user never chose
    assert "kubewiz" not in msg
    assert "Environment channel" not in msg


def test_helper_probes_with_the_submitted_scope():
    """Each call must judge the scope it is given, not whatever is in state.

    Required by the batch path: one submission can mix domains, so the helper is
    called per fault and must not read a single shared ``fault_spec.scope``.
    """
    state = {"kube_connection_mode": "kubewiz_k8s", "fault_spec": {"scope": "pod"}}
    # pod matches the k8s channel …
    assert ic._capability_reject_message(state, "pod", _TOOLS) is None
    # … but the same state must still reject a host fault
    assert ic._capability_reject_message(state, "host", _TOOLS) is not None


# ---------------------------------------------------------------------------
# Both submit paths must use it
# ---------------------------------------------------------------------------


def test_single_and_batch_paths_share_the_helper():
    """Batch submission goes through a different branch and was initially missed.

    ``submit_batch_intent`` produces a differently-named ToolMessage, so it does
    not enter the ``has_submit_tool_msg`` block at all — its faults reached
    ``agent_loop`` unchecked, i.e. the very round-trip the gate removes, and for
    a batch that means failing partway after earlier faults were injected.
    """
    src = textwrap.dedent(inspect.getsource(ic.make_intent_clarification))
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert code.count("_capability_reject_message(") >= 2, (
        "both the single-fault and batch submit paths must call the helper"
    )

    tree = ast.parse(src)

    def _calls_helper(node) -> bool:
        return any(
            isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "_capability_reject_message"
            for n in ast.walk(node)
        )

    for flag in ("has_submit_tool_msg", "has_batch_tool_msg"):
        branches = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == flag
        ]
        assert branches, f"{flag} branch not found"
        assert _calls_helper(branches[0]), f"{flag} path does not run the gate"


def test_batch_rejection_reports_which_fault_failed():
    """A batch is refused whole; the user needs to know which entry caused it."""
    src = inspect.getsource(ic.make_intent_clarification)
    assert "Fault #" in src
    assert "whole batch submission was cancelled" in src
    # checked per entry, and stops at the first offender
    idx = src.index("_batch_reject")
    block = src[idx:idx + 800]
    assert "for _idx, _spec in enumerate(existing_batch, 1)" in block
    assert "break" in block


# ---------------------------------------------------------------------------
# agent_loop's own message (last line of defence)
# ---------------------------------------------------------------------------


def test_agent_loop_message_distinguishes_the_two_causes():
    """The fallback message must not claim no transport was chosen.

    Its previous single sentence said intent recognition finished "without
    choosing a transport" — wrong for the common case, where a transport WAS
    resolved and merely cannot run the requested domain. That wording sent
    readers hunting for a missing selection instead of an incompatible pair.
    """
    from chaos_agent.agent.nodes.execute import agent_loop as al

    src = inspect.getsource(al)
    assert "Intent recognition completed without" not in src

    idx = src.index("capability_context.supported")
    block = src[idx:idx + 3000]
    # conflict branch names both sides (source strings are wrapped, so assert
    # on fragments that do not straddle a line break)
    assert "cannot run through the" in block
    assert "configured transport" in block
    assert "profile:" in block
    # unconfigured branch says what to configure
    assert "no usable transport" in block
    assert "kubeconfig, KubeWiz cluster, KubeWiz host" in block
    # and it is the same evidence set used by the submit-time helper
    for field in (
        "kube_connection_mode", "host_name", "ssh_host",
        "kubeconfig", "kubewiz_cluster_uuid",
    ):
        assert field in block


# ---------------------------------------------------------------------------
# Structural: the gate cannot be walked around
# ---------------------------------------------------------------------------


def test_gate_is_exception_guarded():
    """A resolution failure must let the submit through, not crash the turn.

    Verified via AST rather than a string match on "try:" so that wrapping some
    unrelated statement cannot satisfy it.
    """
    src = textwrap.dedent(inspect.getsource(ic.make_intent_clarification))
    tree = ast.parse(src)

    def _calls_gate(node) -> bool:
        return any(
            isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "_capability_reject_message"
            for n in ast.walk(node)
        )

    guarded = any(
        isinstance(node, ast.Try)
        and any(_calls_gate(b) for b in node.body)
        for node in ast.walk(tree)
    )
    assert guarded, "the capability verdict must sit inside a try block"


def test_rejection_message_names_both_sides_and_offers_exits():
    """The user must be able to act on the message without reading logs.

    The wording now lives in ``_capability_reject_message`` (shared by the
    single-fault and batch paths), not inline in the node.
    """
    src = inspect.getsource(ic._capability_reject_message)
    idx = src.index("does not match the current drill environment")
    block = src[idx:idx + 500]
    assert "Fault domain" in block
    assert "Environment channel" in block
    assert "Switch to a drill environment that matches this fault domain" in block
    assert "choose a fault type the current environment supports" in block


# ---------------------------------------------------------------------------
# The gate is only worth having if it cannot be walked around
# ---------------------------------------------------------------------------


def test_intent_node_has_exactly_one_inject_entry_and_the_gate_precedes_it():
    """``confirmed_intent="inject"`` must be reachable only past the gate.

    The gate lives on the submit fast-path. That is fine *because* the fast-path
    is the only route from ``submit_fault_intent`` to injection — but nothing in
    the code says so. If someone later adds a second place that promotes the
    turn to ``inject``, the gate becomes decorative and the mismatch resurfaces
    at planning with the old opaque transport error.

    Note ``cli/runner.py`` also sets ``confirmed_intent="inject"``; that one is
    not a second entry — it hands the *already-produced* intent-graph output
    (``iv = intent_final.values``) to the execution pipeline, so its fault_spec
    has necessarily passed this gate. Only this module is constrained here.
    """
    src = textwrap.dedent(inspect.getsource(ic))
    tree = ast.parse(src)

    inject_promotions: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "confirmed_intent"
                and isinstance(value, ast.Constant)
                and value.value == "inject"
            ):
                inject_promotions.append(key.lineno)

    assert len(inject_promotions) == 1, (
        "expected exactly one confirmed_intent='inject' site in this module, "
        f"found {len(inject_promotions)} at lines {inject_promotions} — a new "
        "one would bypass the channel compatibility gate"
    )

    gate_lines = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", None) == "_capability_reject_message"
    ]
    assert gate_lines, "gate call not found"
    assert min(gate_lines) < inject_promotions[0], (
        "the gate must run before the turn is promoted to inject"
    )


def test_gate_and_inject_share_the_submit_fast_path_block():
    """Both must sit under the same ``if has_submit_tool_msg:`` branch.

    If the promotion ever moves outside that branch, submitting could bypass the
    gate even while the assertion above still counts one promotion site.
    """
    src = textwrap.dedent(inspect.getsource(ic))
    tree = ast.parse(src)

    def _contains(node, pred) -> bool:
        return any(pred(n) for n in ast.walk(node))

    def _is_gate(n) -> bool:
        return (
            isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "_capability_reject_message"
        )

    def _is_inject(n) -> bool:
        return (
            isinstance(n, ast.Constant)
            and n.value == "inject"
        )

    guarding_branches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "has_submit_tool_msg"
    ]
    assert guarding_branches, "submit fast-path branch not found"
    branch = guarding_branches[0]
    assert _contains(branch, _is_gate), "gate is outside the submit fast-path"
    assert _contains(branch, _is_inject), (
        "inject promotion is outside the submit fast-path — it could now be "
        "reached without passing the gate"
    )
