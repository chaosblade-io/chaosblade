"""R67 namespace invariant: a package attribute must never shadow its submodule.

The retired defect class — ``chaos_agent.tools.kubectl`` and
``chaos_agent.tools.web_search``: ``tools/__init__`` re-exported tool
symbols whose names equalled their own submodule filenames, so ``from
chaos_agent.tools import kubectl`` (attribute face) yielded the tool object
while ``importlib.import_module("chaos_agent.tools.kubectl")`` (import face)
yielded the module. One name, two objects: attribute-walking seams (e.g.
pytest's dotted-string ``monkeypatch.setattr``) silently patched the wrong
object (loudly by default, SILENTLY with ``raising=False`` — the dangerous
form, since green tests would prove nothing).

Invariant enforced here: for every imported ``chaos_agent.*`` submodule,
``getattr(parent_package, leaf_name) is sys.modules[full_name]``.

Declaration domain (honest scope): modules importable via
``pkgutil.walk_packages`` in this environment. Dynamically constructed
module path strings are out of scope — a repo-wide grep finds none.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import types

import pytest

import chaos_agent

_PREFIX = "chaos_agent."


def find_namespace_violations(
    modules: dict, prefix: str = _PREFIX
) -> list[tuple[str, str]]:
    """Return ``(module_name, what_the_attribute_holds)`` for each violation.

    A module is in violation when its parent package exposes that leaf name
    but the attribute is NOT the module object itself. The synthetic-map
    signature keeps the checker independently testable (see the
    self-checks below).
    """
    violations: list[tuple[str, str]] = []
    for name, module in sorted(modules.items()):
        if not name.startswith(prefix) or not isinstance(module, types.ModuleType):
            continue
        parent_name, _, leaf = name.rpartition(".")
        parent = modules.get(parent_name)
        if parent is None or not leaf.isidentifier():
            continue
        attr = getattr(parent, leaf, None)
        if attr is not module:
            violations.append(
                (name, "MISSING" if attr is None else type(attr).__name__)
            )
    return violations


def _import_all_submodules() -> set[str]:
    imported: set[str] = set()
    for info in pkgutil.walk_packages(chaos_agent.__path__, prefix=_PREFIX):
        try:
            importlib.import_module(info.name)
        except Exception:
            continue  # optional dependency — treated as not-imported, not hidden
        imported.add(info.name)
    return imported


class TestPackageNamespaceInvariant:
    def test_no_package_attribute_shadows_a_submodule(self):
        imported = _import_all_submodules()
        # Positive control: the R67 rename targets must really be imported,
        # otherwise an import failure would masquerade as "clean".
        assert "chaos_agent.tools" in imported
        assert "chaos_agent.tools.kubectl_cli" in imported
        assert "chaos_agent.tools.web_search_tool" in imported

        violations = find_namespace_violations(dict(sys.modules))
        assert violations == [], (
            "package attribute shadows submodule (one name, two objects): "
            f"{violations} — rename the submodule file or the exported symbol"
        )

    def test_checker_flags_a_synthetic_shadowing(self):
        """Negative self-check: the checker must catch the collision shape
        it exists for (a guard that cannot fail proves nothing)."""
        parent = types.ModuleType("pkg")
        child = types.ModuleType("pkg.child")
        parent.child = object()  # the shadow: attribute is NOT the submodule

        violations = find_namespace_violations(
            {"pkg": parent, "pkg.child": child}, prefix="pkg."
        )

        assert violations == [("pkg.child", "object")]

    def test_checker_accepts_a_healthy_pair(self):
        """Positive self-check: a well-formed parent/child pair is not
        flagged (no false positives)."""
        parent = types.ModuleType("pkg")
        child = types.ModuleType("pkg.child")
        parent.child = child  # attribute IS the submodule — healthy

        assert (
            find_namespace_violations(
                {"pkg": parent, "pkg.child": child}, prefix="pkg."
            )
            == []
        )

    def test_retired_submodule_paths_stay_unimportable(self):
        """No shim revival: a compatibility module would resurrect the
        collision (its filename would again equal the exported symbol)."""
        for retired in ("chaos_agent.tools.kubectl", "chaos_agent.tools.web_search"):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(retired)

    def test_tool_symbol_names_are_unchanged_by_the_rename(self):
        """The rename touched file paths only — the LLM-facing tool names
        (prompts, skills and tool profiles key off these) must stay
        byte-identical."""
        from chaos_agent.tools import kubectl, kubectl_read, web_search

        assert (kubectl.name, kubectl_read.name, web_search.name) == (
            "kubectl",
            "kubectl_read",
            "web_search",
        )
