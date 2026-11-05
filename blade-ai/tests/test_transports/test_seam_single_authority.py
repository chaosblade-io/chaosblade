"""Seam single-authority guards (ground-truth-single-authority task 3.7).

Phase 2 collapsed three coexisting transport-target acquisition paths onto one
authoritative seam — ``resolve_transport_target`` in ``transports/registry.py``:

* the retired ``sync_kubewiz_runtime`` settings side-effect (Phase 2c),
* the retired ``TransportTarget.from_state`` public bridge — now the private
  ``_from_state`` hydration primitive that only the seam module may call
  (Phase 2d),
* hand-built ``TransportTarget(...)`` construction outside the transports layer.

These guards pin the collapse as a *structural property* scanned from source
(AST), so a future caller cannot silently re-open any of the three paths.  They
assert the ABSENCE of every retired path and the PRESENCE/location of the single
authority — properties, not line numbers or hard-coded counts (which would let
an author "legalize" a self-introduced regression by bumping the pinned value).

AST scanning deliberately ignores comments and docstrings: the historical
rationale prose that still *mentions* ``sync_kubewiz_runtime`` / ``from_state``
(past tense, explaining what the seam replaced) is documentation, not a live
reference, and must not trip these guards.
"""

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "chaos_agent"
_TRANSPORTS_REL = "transports"


def _src_files() -> list[Path]:
    return sorted(_SRC.rglob("*.py"))


def _rel(path: Path) -> Path:
    return path.relative_to(_SRC)


def _in_transports(path: Path) -> bool:
    return _rel(path).parts[0] == _TRANSPORTS_REL


def _walk(path: Path):
    return ast.walk(ast.parse(path.read_text()))


def _refs_name(node: ast.AST, name: str) -> bool:
    """True when *node* is a live code reference to the symbol *name*.

    Covers every binding/use form an AST can express so a retired symbol cannot
    hide behind an import alias, a method call, or a re-definition.  Comments
    and docstrings are not AST nodes, so prose mentions never match.
    """
    if isinstance(node, ast.Name):
        return node.id == name
    if isinstance(node, ast.Attribute):
        return node.attr == name
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == name
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return any(a.name == name or a.asname == name for a in node.names)
    return False


def _refs_bridge(node: ast.AST, name: str) -> bool:
    """Narrower matcher for a retired *method* bridge (``X.from_state``).

    Excludes a bare ``ast.Name``: ``from_state`` is also an ordinary local
    variable name elsewhere in the codebase (e.g. ``resolve_hint_count`` binds
    ``from_state = 0`` for a state-vs-history count), which is unrelated to the
    ``TransportTarget.from_state`` classmethod.  A classmethod bridge is only
    ever *used* as an attribute access, *re-exposed* as a method definition, or
    (never legitimately) imported — so those three forms are the ones that
    signal the retired public bridge is back.
    """
    if isinstance(node, ast.Attribute):
        return node.attr == name
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == name
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return any(a.name == name or a.asname == name for a in node.names)
    return False


def _offenders(name: str, *, where=None, matcher=_refs_name) -> list[str]:
    locs = []
    for path in _src_files():
        if where is not None and not where(path):
            continue
        for node in _walk(path):
            if matcher(node, name):
                locs.append(f"{_rel(path)}:{node.lineno}")
    return sorted(locs)


class TestSyncKubewizRuntimeRetired:
    """Phase 2c: the settings-mutating pre-sync is gone — zero live references.

    If this turns red, a caller has re-introduced the per-session settings
    side-effect that the seam exists to replace, re-opening the exact
    order-dependence (sync-then-read) the retirement closed.
    """

    def test_zero_code_references_remain(self):
        offenders = _offenders("sync_kubewiz_runtime")
        assert not offenders, (
            "sync_kubewiz_runtime was retired in ground-truth-single-authority "
            "Phase 2c — the seam resolve_transport_target(state) replaces its "
            "settings side-effect. These live references re-open it:\n  "
            + "\n  ".join(offenders)
        )


class TestFromStateNoLongerPublicBridge:
    """Phase 2d: ``from_state`` is not a public name anywhere in src.

    The public bridge was privatized to ``_from_state``; every consumer must
    obtain a target through ``resolve_transport_target``.  A red here means
    someone re-exposed or re-called the old public bridge, bypassing the seam's
    ported kubewiz-hijack guard.
    """

    def test_no_public_from_state_reference(self):
        offenders = _offenders("from_state", matcher=_refs_bridge)
        assert not offenders, (
            "TransportTarget.from_state is no longer a public bridge "
            "(ground-truth-single-authority Phase 2d) — call "
            "resolve_transport_target(state) instead. These references use the "
            "retired public name:\n  " + "\n  ".join(offenders)
        )


class TestPrivatePrimitiveConfinedToSeamModule:
    """``_from_state`` may be *called* only from the seam module (registry.py).

    ``base.py`` defines it (a FunctionDef, not an attribute call) and
    ``registry.py`` calls it twice — once in ``resolve_channel_name`` (the
    anti-recursion primitive the guard depends on) and once in
    ``resolve_transport_target`` itself.  Any *other* module calling
    ``TransportTarget._from_state(...)`` is using the private primitive as a
    de-facto bridge and skipping the seam's guard.
    """

    def test_from_state_primitive_called_only_in_registry(self):
        offenders = []
        for path in _src_files():
            if _rel(path) == Path(_TRANSPORTS_REL) / "registry.py":
                continue  # the authorized caller
            for node in _walk(path):
                if isinstance(node, ast.Attribute) and node.attr == "_from_state":
                    offenders.append(f"{_rel(path)}:{node.lineno}")
        assert not offenders, (
            "TransportTarget._from_state is a private implementation detail of "
            "the seam — only transports/registry.py may call it. These modules "
            "reach past resolve_transport_target:\n  " + "\n  ".join(sorted(offenders))
        )

    def test_primitive_is_defined_in_base_and_used_by_seam(self):
        """Positive anchor: the guard above is not vacuous.

        ``_from_state`` must actually exist (base.py) and actually be delegated
        to by the seam module (registry.py); otherwise the confinement guard
        would pass on an empty set and cover nothing.
        """
        base_defs = [
            n.name
            for n in _walk(_SRC / _TRANSPORTS_REL / "base.py")
            if isinstance(n, ast.FunctionDef)
        ]
        assert "_from_state" in base_defs, (
            "TransportTarget._from_state is not defined in transports/base.py — "
            "the confinement guard would be vacuous"
        )
        reg_attrs = [
            n.attr
            for n in _walk(_SRC / _TRANSPORTS_REL / "registry.py")
            if isinstance(n, ast.Attribute)
        ]
        assert "_from_state" in reg_attrs, (
            "transports/registry.py never delegates to _from_state — the seam "
            "no longer hydrates through the primitive it is supposed to own"
        )


class TestTargetsAcquiredThroughSeam:
    """No hand-built ``TransportTarget(...)`` outside the transports layer.

    The transports layer owns the type and may construct it internally; every
    *consumer* (graph nodes, tools, providers) must go through the seam so the
    settings-authoritative channel selection and the ported kubewiz guard apply
    uniformly.  A direct construction in a consumer re-opens the pre-seam path
    where each call site re-derived the target its own way.
    """

    def test_no_direct_target_construction_outside_transports(self):
        offenders = []
        for path in _src_files():
            if _in_transports(path):
                continue  # the layer that owns the type
            for node in _walk(path):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "TransportTarget"
                ):
                    offenders.append(f"{_rel(path)}:{node.lineno}")
        assert not offenders, (
            "acquire a TransportTarget through resolve_transport_target(state) "
            "(or thread one down from the owning node) — do not construct "
            "TransportTarget(...) directly outside the transports layer. "
            "Offending call sites:\n  " + "\n  ".join(sorted(offenders))
        )


class TestSeamIsTheSingleAuthority:
    """Positive anchor: the one authority these guards funnel everything to."""

    def test_resolve_transport_target_defined_in_registry(self):
        funcs = [
            n.name
            for n in _walk(_SRC / _TRANSPORTS_REL / "registry.py")
            if isinstance(n, ast.FunctionDef)
        ]
        assert "resolve_transport_target" in funcs, (
            "resolve_transport_target is not defined in transports/registry.py — "
            "the single authority the retirement guards depend on is missing"
        )

    def test_resolve_transport_target_is_exported_and_is_the_registry_func(self):
        from chaos_agent.transports import resolve_transport_target as exported
        from chaos_agent.transports.registry import (
            resolve_transport_target as canonical,
        )

        assert exported is canonical, (
            "chaos_agent.transports.resolve_transport_target must re-export the "
            "registry seam — consumers import it from the package root"
        )
