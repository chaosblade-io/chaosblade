"""Pause-projection contract — machine verification that every surface that
projects the inject ``task_state`` declares its pause semantics.

Why this file exists
--------------------
Round-64 F3: a run parked at ``confirmation_gate``'s ``interrupt()`` was
reported as ``failed``. The root cause was that "the graph is paused" had no
single word, so six consumption surfaces each re-invented the guard in five
different shapes and four surfaces omitted it — every omission translated
"waiting for a human" into "the injection failed".

The fix gave the pause a single source (``state.graph_is_paused`` /
``paused_task_state`` / ``resumable_pause``) landed at one projection point
(``operation_result.build_inject_data_from_state``). That builder is
**fail-closed**: a caller that passes NEITHER ``snapshot=`` (the engine
authority on pause) NOR ``paused=`` gets the terminal word, because the values
alone cannot tell "parked at the gate" from "ran and failed with a stale
``needs_confirmation``". So the whole single-source design holds only if every
call site that can observe a pause DECLARES it.

Nothing pinned that invariant — the seven declaring sites and the two
structurally-immune ones were reconciled by hand. A new call site added later
(e.g. a fresh result surface) would silently get the fail-closed terminal word
and re-introduce F3 for that surface, with no test turning red. This file is
the structural guard: it AST-enumerates every ``build_inject_data_from_state``
call under ``src/`` and forces each to either declare pause semantics or sit on
a documented, reason-bearing allowlist of provably pause-immune sites.

Shape of the contract (mirrors test_write_contracts.py)
-------------------------------------------------------
1. producer/declaration existence (AST) — every projection call site declares
   ``snapshot=``/``paused=`` or is allowlisted;
2. claim-vs-implementation — the allowlist is an EXACT picture: no stale entry
   may point at a site that vanished or started declaring;
3. anti-vacuity sentinel — the walk is pinned to actually find the known real
   surfaces, so a broken matcher cannot pass tests 1 and 2 on an empty set.
"""

from __future__ import annotations

import ast
from pathlib import Path

import chaos_agent

_TARGET = "build_inject_data_from_state"
_SRC_ROOT = Path(chaos_agent.__file__).parent

# Call sites that project the inject task_state WITHOUT a snapshot=/paused=
# declaration, yet are structurally unable to observe a confirmation-gate
# pause. Keyed by (relpath, enclosing function) — NOT line number, which
# drifts on any edit above the site. Each entry must carry the reason it is
# provably pause-immune; test_allowlist_reasons_are_documented enforces that.
_PAUSE_IMMUNE_ALLOWLIST: dict[tuple[str, str], str] = {
    ("agent/nodes/store/memory_nodes.py", "_finalize_session_store"): (
        "Reached only from the save_memory terminal node (it stamps "
        "finished_at and runs after verification, before END). "
        "confirmation_gate's interrupt() halts the graph upstream, so this "
        "node is never entered while the run is parked — the fail-closed "
        "terminal word is the only correct answer here."
    ),
    ("cli/runner.py", "lift_dry_run_and_run"): (
        "The call is gated by `if has_active_fault(values)`. paused_task_state "
        "returns None whenever has_active_fault is true (a committed fault is "
        "not a confirmation-gate pause), so is_paused resolves False with or "
        "without a snapshot — passing one would be a no-op. The site is "
        "structurally pause-immune."
    ),
}

# The seven terminal surfaces that MUST declare pause semantics. Pinned so a
# silently-broken AST matcher (finding zero sites) cannot turn the two
# contract tests vacuously green.
_KNOWN_DECLARING_SITES: frozenset[tuple[str, str]] = frozenset({
    ("cli/runner.py", "inject"),
    ("cli/runner.py", "confirm"),
    ("cli/result_builder.py", "_build_inject_result_events"),
    ("memory/session_finalizer.py", "finalize_inject_session"),
    ("server/routes/confirm.py", "confirm_task"),
    ("server/routes/inject_stream.py", "event_generator"),
    ("server/routes/turn_result.py", "build_result_payload"),
})


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _call_name(node: ast.Call) -> str | None:
    """The bare function name a Call invokes (Name or Attribute form)."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _projection_call_sites() -> list[tuple[str, str, int, bool]]:
    """(relpath, enclosing function, lineno, declares_pause) per call site.

    ``declares_pause`` is True when the call passes a ``snapshot=`` or
    ``paused=`` keyword — the two ways a caller tells the fail-closed builder
    whether the run is really at a terminal point.
    """
    sites: list[tuple[str, str, int, bool]] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node: ast.AST, func_name: str) -> None:
            for child in ast.iter_child_nodes(node):
                child_func = func_name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    child_func = child.name
                if isinstance(child, ast.Call) and _call_name(child) == _TARGET:
                    declares = any(
                        kw.arg in ("snapshot", "paused") for kw in child.keywords
                    )
                    sites.append((rel, child_func, child.lineno, declares))
                walk(child, child_func)

        walk(tree, "<module>")
    return sites


# ---------------------------------------------------------------------------
# Contract 1 — every projection site declares pause semantics or is allowlisted
# ---------------------------------------------------------------------------


def test_every_projection_site_declares_pause_or_is_allowlisted():
    undeclared: list[str] = []
    for rel, func, lineno, declares in _projection_call_sites():
        if declares:
            continue
        if (rel, func) in _PAUSE_IMMUNE_ALLOWLIST:
            continue
        undeclared.append(f"{rel}:{lineno} in {func}()")

    assert not undeclared, (
        "These build_inject_data_from_state call sites project the inject "
        "task_state WITHOUT declaring pause semantics (no snapshot=/paused= "
        "kwarg) and are not on the pause-immune allowlist. A site that passes "
        "neither gets the fail-closed terminal word, which reports a run "
        "parked at confirmation_gate as `failed` (round-64 F3). Pass the graph "
        "snapshot (engine authority) or the paused= flag; only if the site is "
        "STRUCTURALLY unable to observe a pause, add it to "
        "_PAUSE_IMMUNE_ALLOWLIST with a reason:\n  " + "\n  ".join(undeclared)
    )


# ---------------------------------------------------------------------------
# Contract 2 — the allowlist is an exact picture (no stale entries)
# ---------------------------------------------------------------------------


def test_allowlist_has_no_stale_entries():
    live_bare = {
        (rel, func)
        for rel, func, _lineno, declares in _projection_call_sites()
        if not declares
    }
    stale = set(_PAUSE_IMMUNE_ALLOWLIST) - live_bare
    assert not stale, (
        f"Allowlist entries no longer match a bare call site (deleted, renamed, "
        f"or now declaring pause semantics): {sorted(stale)}. Remove them so "
        f"the allowlist stays an exact picture of the pause-immune sites — a "
        f"stale entry would let a future bare site hide behind a dead name."
    )


def test_allowlist_reasons_are_documented():
    empty = [key for key, reason in _PAUSE_IMMUNE_ALLOWLIST.items() if not reason.strip()]
    assert not empty, (
        f"Every pause-immune allowlist entry must carry WHY it cannot observe "
        f"a pause; these are blank: {sorted(empty)}"
    )


# ---------------------------------------------------------------------------
# Contract 3 — anti-vacuity sentinel: the matcher really finds the surfaces
# ---------------------------------------------------------------------------


def test_walk_finds_the_known_declaring_surfaces():
    """If the AST matcher broke and found nothing, contracts 1 and 2 would
    pass on an empty set. Pin that the real terminal surfaces are found AND
    declare pause semantics, so the guard has teeth."""
    found = {
        (rel, func): declares
        for rel, func, _lineno, declares in _projection_call_sites()
    }
    missing = _KNOWN_DECLARING_SITES - set(found)
    assert not missing, (
        f"Known projection surfaces not found by the AST walk (matcher broke, "
        f"or a surface was renamed/moved — update _KNOWN_DECLARING_SITES): "
        f"{sorted(missing)}"
    )
    not_declaring = sorted(k for k in _KNOWN_DECLARING_SITES if not found[k])
    assert not not_declaring, (
        f"These surfaces stopped declaring pause semantics (dropped their "
        f"snapshot=/paused= kwarg) — that re-opens F3 for them: {not_declaring}"
    )
