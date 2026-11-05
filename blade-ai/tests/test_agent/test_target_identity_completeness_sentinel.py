"""Completeness sentinel for the shared ``target_identity`` seam.

Root-fix guard for the #83 audit (the W-83-5 / #67 / W-61-1 bug CLASS): a
consumer that needs "fault target → the literal pod / node a probe must
address" hand-rolls the resolution instead of delegating to the seam, gets it
subtly wrong (a bare workload name for a pod, a pod name for a node), and the
failure launders into a silent skip or a green receipt.

Two invariants are enforced mechanically so the class cannot recur:

1. DELEGATION CONTRACT — every known consumer module imports the seam function
   it delegates to. The import is matched on the ORIGINAL name (``alias.name``)
   so an ``from … import resolve_node_of_pod as _seam_…`` rename cannot escape
   (the R12-1 as-alias lesson). Re-inlining a migrated consumer drops its
   import and fails here.

2. COMPLETENESS SWEEP — no OTHER module hand-rolls a cluster-object identity
   resolution jsonpath (a projection of ``.metadata.name`` or ``.spec.nodeName``)
   in ANY textual form — single-value ``{.items[0]…}``, victim/guard SET
   ``{.items[*]…}``, range-enumeration ``{range .items[*]}…{end}``, or a future
   filtered variant. The matcher keys on the PROJECTED IDENTITY FIELD, not an
   enumerated list of exact brace literals, so a consumer cannot escape by
   switching textual form while hand-rolling the same resolution. The sweep
   collects every NON-DOCSTRING string constant under ``src/`` matching that
   field projection and pins the exact allowlist. A new consumer that bypasses
   the seam adds a file to the set and fails here, forcing a conscious choice:
   delegate, or justify a new allowlist entry in code review.

Why AST string-constant scanning rather than an import/call check: a jsonpath
literal cannot be smuggled past an alias or a dynamic-call trick — the literal
is the literal regardless of how the query helper is named or imported.
Docstrings are excluded (they legitimately quote the jsonpath as prose).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
_SEAM_MODULE = "chaos_agent.agent.target_identity"

# The SEMANTIC criterion for "this literal resolves a cluster object's
# identity": a kubectl jsonpath that PROJECTS an identity field — an object's
# own name (``.metadata.name``) or its hosting node (``.spec.nodeName``) —
# terminated by the closing brace. Matching the projected FIELD rather than an
# enumerated set of exact brace literals closes the ENTIRE textual family at
# once: the single-value form ``{.items[0].metadata.name}``, the victim/guard
# SET form ``{.items[*].metadata.name}``, the range-enumeration form
# ``{range .items[*]}{.metadata.name}…{end}``, and any future filtered variant
# ``{.items[?(@…)].metadata.name}`` all project the same field and are all
# caught. A literal frozenset of brace strings is a brittle PROXY for this — a
# consumer could escape it by switching textual form while hand-rolling the very
# same resolution (the ``preflight`` range-form deployment-inventory scan was
# invisible to the earlier literal set; the tool_screener / target_health
# ``{.items[*].metadata.name}`` victim-set escapes were the direct-form instances
# that grew the seam's SET twin). Docstring prose quotes are excluded upstream
# (``_code_string_constants`` skips docstring nodes); the ``\}`` anchor keeps a
# bare namespace projection (``.metadata.namespace}``) from false-firing.
_IDENTITY_PROJECTION_RE = re.compile(r"\.(?:metadata\.name|spec\.nodeName)\}")

# Canonical resolution shapes the regex generalizes — documentation of the
# seam's own queries; the MATCHER is the regex above, not this list:
#   {.items[0].metadata.name}   selector → single pod name
#   {.items[*].metadata.name}   selector → ALL matching pod names (victim set)
#   {.spec.nodeName}            pod → its hosting node
#   {.items[0].spec.nodeName}   selector → first pod's node
#   {.items[*].spec.nodeName}   selector → ALL pods' nodes (guard set)

# Pinned allowlist: the ONLY files (relpath under src/) permitted to contain a
# resolution jsonpath outside the seam, each with the reason it is legitimately
# NOT a fault-target resolution the seam should own.
_ALLOWLIST = {
    "chaos_agent/agent/target_identity.py":
        "the seam itself — the single source of truth",
    "chaos_agent/agent/target_guard/freeze.py":
        "the approval snapshot enumerates ARBITRARY kinds' name / node / PVC "
        "SETS through the guard's own tri-state query_kubectl + "
        "_jsonpath_get_args single-render-point (B81) plumbing, fail-CLOSED — a "
        "different concern from the seam's pod-probe addressing. "
        "discover_victim_nodes needs EVERY victim node ({.spec.nodeName} per pod "
        "+ {.items[*].spec.nodeName}); discover_owner_names / "
        "discover_names_by_labels need EVERY owned/selected object name "
        "({.items[*].metadata.name}). Collapsing any to items[0] would let a "
        "mechanism write drift onto an un-approved object undetected — a "
        "security regression, documented in-code",
    "chaos_agent/tools/pod_discovery.py":
        "TOOL_POD_JSONPATH is a {range .items[*]} BULK enumeration of "
        "chaosblade-tool CARRIER pods (infrastructure), not fault-target "
        "resolution",
    "chaos_agent/tools/kubectl_cli.py":
        "user-facing help/error text showing the LLM how to resolve a pod "
        "before exec — a guidance string, not a query",
    "chaos_agent/agent/spec/_feasibility_checkers.py":
        "_find_tool_pod_on_node discovers the chaosblade-tool CARRIER pod on a "
        "node (an infra probe carrier for the network checker), NOT the fault "
        "target — the target itself is resolved via the seam "
        "(_resolve_first_pod / _fetch_pod_node delegate)",
    "chaos_agent/preflight.py":
        "check_chaosblade_operator's startup self-check range-enumerates ALL "
        "deployments cluster-wide (deploy -A: name|availableReplicas|images) to "
        "find the chaosblade-operator Deployment and capture its image tag — an "
        "environment-readiness INVENTORY scan resolving DEPLOYMENT (workload) "
        "names, NOT a fault target's victim-pod addressing. Caught only once the "
        "matcher generalized from literal brace strings to the identity-field "
        "projection (its {range .items[*]}{.metadata.name} form was invisible to "
        "the earlier literal set)",
}

# Delegation contract: consumer module → the seam functions it MUST import.
_CONTRACT = {
    "chaos_agent/agent/nodes/baseline/baseline_capture.py": {
        "resolve_pod_by_selector",
        "resolve_node_of_pod",
        "resolve_node_by_selector",
    },
    "chaos_agent/agent/spec/_feasibility_checkers.py": {
        "resolve_target_pod_name",
        "resolve_node_of_pod",
    },
    "chaos_agent/agent/nodes/planning/tool_screener.py": {
        "resolve_node_of_pod",
        "resolve_pods_by_selector",
    },
    "chaos_agent/agent/target_health.py": {
        "resolve_pods_by_selector",
    },
}


def _py_files() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _relpath(path: Path) -> str:
    return path.relative_to(_SRC).as_posix()


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids of Constant nodes sitting in docstring position (to be skipped)."""
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return docstrings


def _code_string_constants(path: Path) -> list[str]:
    """Non-docstring string-constant values in a module (executable literals)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_nodes(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
        ):
            out.append(node.value)
    return out


def _seam_imports(path: Path) -> set[str]:
    """ORIGINAL names imported from the seam module (alias-resolving)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _SEAM_MODULE:
            for alias in node.names:
                names.add(alias.name)  # original, NOT alias.asname
    return names


# ---------------------------------------------------------------------------
# Invariant 1 — delegation contract
# ---------------------------------------------------------------------------


def test_delegation_contract_consumers_import_seam():
    """Every migrated consumer still imports the seam function it delegates to.

    A regression that re-inlines a hand-rolled query in one of these modules
    drops the import and fails here — the mechanical half of "穷举消费面 + 收口
    到单一渲染器" for the identity-resolution axis.
    """
    missing: dict[str, set[str]] = {}
    for rel, required in _CONTRACT.items():
        path = _SRC / rel
        assert path.exists(), f"contract file vanished: {rel}"
        imported = _seam_imports(path)
        gap = required - imported
        if gap:
            missing[rel] = gap
    assert not missing, (
        "these consumers no longer import their seam function (a hand-rolled "
        f"resolution likely returned): {missing}"
    )


# ---------------------------------------------------------------------------
# Invariant 2 — completeness sweep (pinned allowlist)
# ---------------------------------------------------------------------------


def _files_with_resolution_jsonpath() -> set[str]:
    found: set[str] = set()
    for path in _py_files():
        for literal in _code_string_constants(path):
            if _IDENTITY_PROJECTION_RE.search(literal):
                found.add(_relpath(path))
                break
    return found


def test_no_new_handrolled_resolution_outside_allowlist():
    """The set of files carrying a resolution jsonpath is EXACTLY the pinned
    allowlist. A new offender (found − allowlist) means someone bypassed the
    seam; a stale entry (allowlist − found) means the pin needs updating."""
    found = _files_with_resolution_jsonpath()
    allowed = set(_ALLOWLIST)

    new_offenders = found - allowed
    stale = allowed - found

    assert not new_offenders, (
        "these modules hand-roll a target→pod/node resolution jsonpath instead "
        "of delegating to the target_identity seam (the #83 / W-83-5 bug class). "
        "Delegate to the seam, or — if genuinely a different concern (carrier "
        "discovery / bulk enumeration / guard node-set / help text) — add a "
        f"justified allowlist entry: {sorted(new_offenders)}"
    )
    assert not stale, (
        "these allowlisted modules no longer contain a resolution jsonpath — "
        f"prune the stale allowlist entries: {sorted(stale)}"
    )


def test_seam_itself_is_present_and_pinned():
    """Sanity anchor: the seam exists and is in the allowlist (guards against a
    rename silently turning the sweep vacuous)."""
    assert "chaos_agent/agent/target_identity.py" in _ALLOWLIST
    assert (_SRC / "chaos_agent/agent/target_identity.py").exists()
    assert "chaos_agent/agent/target_identity.py" in _files_with_resolution_jsonpath()


def test_sweep_catches_pod_name_set_shape_end_to_end(tmp_path, monkeypatch):
    """POSITIVE CONTROL (end-to-end): a module carrying ONLY the multi-pod-name
    SET shape ``{.items[*].metadata.name}`` is caught by the real sweep
    machinery. This is the shape the tool_screener selector cross-shape probe
    and target_health._resolve_pod_names once hand-rolled invisibly to the sweep
    (the escape that grew the seam's SET twin). Without this control the criteria
    could silently drop the shape and both offenders could recur undetected while
    every other test stayed green."""
    _this = sys.modules[__name__]
    offender = tmp_path / "chaos_agent" / "rogue_consumer.py"
    offender.parent.mkdir(parents=True)
    offender.write_text(
        'JSONPATH = "jsonpath={.items[*].metadata.name}"\n', encoding="utf-8",
    )
    monkeypatch.setattr(_this, "_SRC", tmp_path)
    found = _this._files_with_resolution_jsonpath()
    assert "chaos_agent/rogue_consumer.py" in found


def test_sweep_ignores_pod_name_set_shape_in_docstring(tmp_path, monkeypatch):
    """NEGATIVE CONTROL: the same shape inside a DOCSTRING is not an offender
    (docstrings legitimately quote the jsonpath as prose) — the exclusion that
    keeps baseline_capture's prose quotes from being false positives."""
    _this = sys.modules[__name__]
    benign = tmp_path / "chaos_agent" / "prose_only.py"
    benign.parent.mkdir(parents=True)
    benign.write_text(
        'def f():\n    """Run jsonpath={.items[*].metadata.name} to list."""\n'
        "    return 1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_this, "_SRC", tmp_path)
    found = _this._files_with_resolution_jsonpath()
    assert "chaos_agent/prose_only.py" not in found


def test_sweep_catches_range_form_name_enumeration(tmp_path, monkeypatch):
    """POSITIVE CONTROL (the textual-family closure the regex upgrade buys): a
    consumer that hand-rolls the SAME name resolution but in the RANGE form
    ``{range .items[*]}{.metadata.name}…{end}`` — textually different from
    ``{.items[*].metadata.name}`` — is STILL caught. A literal-frozenset matcher
    missed this (preflight's inventory scan escaped invisibly); keying on the
    projected identity field closes every textual variant at once, so a future
    consumer cannot dodge the sentinel by switching jsonpath syntax."""
    _this = sys.modules[__name__]
    offender = tmp_path / "chaos_agent" / "rogue_range.py"
    offender.parent.mkdir(parents=True)
    offender.write_text(
        "J = \"jsonpath={range .items[*]}{.metadata.name}|{.status.phase}{end}\"\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_this, "_SRC", tmp_path)
    found = _this._files_with_resolution_jsonpath()
    assert "chaos_agent/rogue_range.py" in found


def test_sweep_ignores_namespace_only_projection(tmp_path, monkeypatch):
    """NEGATIVE CONTROL (matcher precision): a jsonpath projecting ONLY
    ``.metadata.namespace`` is NOT an identity resolution and must not
    false-fire — the ``\\}`` anchor means ``.metadata.namespace}`` (name followed
    by ``space}``) does not match ``.metadata.name}``."""
    _this = sys.modules[__name__]
    benign = tmp_path / "chaos_agent" / "namespace_only.py"
    benign.parent.mkdir(parents=True)
    benign.write_text(
        'J = "jsonpath={.items[*].metadata.namespace}"\n', encoding="utf-8",
    )
    monkeypatch.setattr(_this, "_SRC", tmp_path)
    found = _this._files_with_resolution_jsonpath()
    assert "chaos_agent/namespace_only.py" not in found


def test_allowlist_entries_are_justified():
    """Every allowlist entry carries a non-trivial reason (no empty strings)."""
    for rel, reason in _ALLOWLIST.items():
        assert isinstance(reason, str) and len(reason.strip()) > 20, (
            f"allowlist entry {rel} lacks a real justification"
        )