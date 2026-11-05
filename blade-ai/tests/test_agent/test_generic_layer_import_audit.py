"""AST audit: the generic layer must not import concrete provider modules.

Phase-8 structural guard (D6). Word-list scans are structurally blind to
dependency-direction violations: a read-through of ``K8sNativeProvider``
carries no "blade" token, and a ``TOOL_POD_NAMESPACES`` value literal is
data, not code. The audit therefore parses every generic-layer module and
walks ALL import nodes — including function-body lazy imports, which is
exactly where the phase-7 leftovers hid — asserting none references a
concrete provider module. The legal seam targets (``providers.registry``,
``providers.base``, the carrier-family facility modules such as
``providers.chaosblade.verify``, the carrier-neutral flat module
``providers.message_scanning``, and the package ``__init__``)
are not audited.

Since providers-group-by-carrier the violation surface is PACKAGE-PREFIX
based (D3), matching the physical layout: a carrier subpackage import at
package top level (``providers.chaosblade`` — indistinguishable in syntax
from the retired flat module, which is why the carrier enumeration exists:
it separates carrier subpackages from ``registry``/``base``/the package
``__init__`` at the same depth). That enumeration is DISCOVERED FROM DISK
(the sub-directories of ``providers`` that are themselves packages), NOT
hardcoded — so BOTH arms catch a future carrier with zero rule change: the
package-top-level arm via disk discovery, and the REGISTERED PROVIDER module
arm via its fixed name (``<carrier>.provider`` / ``<carrier>.python_provider``).
The family facility modules (``verify`` /
``recover`` / ``classifier``) stay seam targets, carrying forward the
phase-8 verdict that carrier-shared knowledge modules are legal for the
generic layer.

The ``tools/`` layer is likewise out of scope BY DIRECTION: it is the
tool-implementation layer, not a provider module, and generic code
importing a tool on demand (e.g. ``verify``'s lazy ``blade_destroy`` for
deterministic replan cleanup) is a legal direction — recorded as the
phase-9 D4 design decision (fault-handle-phase9 design.md). The audit
ban covers imports of concrete *provider* modules only.

The ALLOWLIST is shrink-only: each entry cites its origin and is removed
in the same change that closes the violation. The exact-equality
assertion forces the allowlist to move in lock-step — a stale entry
fails, a new violation fails.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "chaos_agent"

# Generic (carrier-agnostic) layer subtrees under audit. Providers, tools,
# TUI and CLI entry modules are out of scope — they are allowed to know
# concrete backends.
GENERIC_DIRS = (
    "agent/nodes",
    "agent/target_guard",
    "agent/state_mgmt",
    "agent/result",
    "l4",
)

# Carrier subpackages under ``agent/providers`` — DISCOVERED FROM DISK, not
# hardcoded. At package top level the physical layout is the only thing
# separating a carrier from the flat seam modules: a carrier is a SUBPACKAGE (a
# directory carrying its own ``__init__.py``), whereas the legal seam surface at
# the same depth (``registry`` / ``base`` / ``message_scanning`` / ``uid_shapes``)
# are flat MODULES (files). So "sub-directory of ``agent/providers`` that is
# itself a package" IS the normative carrier criterion.
_PROVIDERS_DIR = SRC_ROOT / "agent" / "providers"


def _discover_carrier_packages(providers_dir: Path = _PROVIDERS_DIR) -> frozenset:
    """Every sub-directory of ``providers_dir`` that is a package (carries an
    ``__init__.py``) — the carrier subpackages.

    Discovering rather than enumerating means a new carrier package lands in the
    violation surface with ZERO rule change: ``faultdrill`` is caught today and
    any future family tomorrow, matching the extensibility the fixed-name
    provider-module arm (``PROVIDER_MODULES``) already guarantees — before this,
    the package-top-level arm was the ONE place a new carrier could be added on
    disk yet silently escape the audit (a hardcoded frozenset drifts the instant
    a carrier directory appears and stops auditing that carrier's top-level
    imports). Non-package directories (``__pycache__``) are excluded by the
    ``__init__.py`` test; flat seam modules are excluded by ``is_dir()``.
    """
    if not providers_dir.is_dir():
        return frozenset()
    return frozenset(
        entry.name
        for entry in providers_dir.iterdir()
        if entry.is_dir() and (entry / "__init__.py").is_file()
    )


CARRIER_PACKAGES = _discover_carrier_packages()

# Registered provider module names inside a carrier package (fixed-name
# arm — catches future carrier packages too, no rule change needed).
PROVIDER_MODULES = frozenset(
    {
        "provider",
        "python_provider",
    }
)


def _is_concrete_provider_module(parts: tuple[str, ...]) -> bool:
    """Violation test for a resolved dotted import target.

    True for a carrier package import at package top level (``len == 4``;
    the retired flat modules ``chaosblade``/``k8s_native``/``host_shell``
    were syntactically identical, so historical violations of that shape
    keep being caught) or for a registered provider module by fixed name
    (``provider`` / ``python_provider`` — any carrier, present or future).
    False for the seam surface: ``registry`` / ``base`` / the package
    ``__init__`` (len < 4) and the family facility modules
    (``verify`` / ``recover`` / ``classifier``).
    """
    if len(parts) < 4 or parts[:3] != ("chaos_agent", "agent", "providers"):
        return False
    if len(parts) == 4:
        return parts[3] in CARRIER_PACKAGES
    return parts[4] in PROVIDER_MODULES

# Shrink-only allowlist of (posix-relative path, imported module) pairs that
# still import a concrete provider module. Each entry cited its origin and
# was DELETED in the same change that closed the violation — never appended.
# EMPTY since phase-8 T4.1: the last entry (execute_loop's lazy
# ``extract_kubectl_exec_pod_name`` import, closed by the registry dispatch
# seam) completed the shrink journey 3 keys -> 1 -> 0. The exact-equality
# assertion plus the empty-set pin below keep it at zero — a future entry
# may only appear together with the change that will remove it.
ALLOWLIST: set[tuple[str, str]] = set()


def _iter_violations(root: Path) -> Iterator[tuple[str, str, int]]:
    """Yield ``(relpath, module, lineno)`` for every import of a concrete
    provider module found anywhere (module level, class body, function
    body) in the generic layer under ``root``."""
    for sub in GENERIC_DIRS:
        base = root / sub
        if not base.is_dir():
            continue
        for py in sorted(base.rglob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            rel = py.relative_to(root).as_posix()
            for node in ast.walk(tree):
                candidates: list[tuple[str, int]] = []
                if isinstance(node, ast.ImportFrom) and node.module is not None:
                    candidates.append((node.module, node.level))
                elif isinstance(node, ast.Import):
                    candidates.extend((alias.name, 0) for alias in node.names)
                for module, level in candidates:
                    if level == 0:
                        if _is_concrete_provider_module(tuple(module.split("."))):
                            yield rel, module, node.lineno
                    else:
                        # Relative import (level >= 1): resolve it the way
                        # Python does — walk up ``level - 1`` package levels
                        # from the importing module, then append the module
                        # segments — and audit the resolved target. This
                        # covers every relative spelling (e.g.
                        # ``..agent.providers.x`` from ``l4``, or
                        # ``...providers.x`` from ``agent/nodes/execute``).
                        seg = tuple((module or "").split("."))
                        dirname_parts = py.parent.relative_to(root).parts
                        full = ("chaos_agent",) + dirname_parts
                        drop = level - 1
                        if drop >= len(full):
                            continue
                        resolved = full[: len(full) - drop] + seg
                        if _is_concrete_provider_module(resolved):
                            yield rel, "." * level + (module or ""), node.lineno


def test_generic_layer_imports_no_concrete_provider_module():
    violations = {
        (rel, module) for rel, module, _lineno in _iter_violations(SRC_ROOT)
    }
    unexpected = violations - ALLOWLIST
    stale = ALLOWLIST - violations
    assert not unexpected and not stale, (
        "Generic-layer -> concrete-provider import audit failed.\n"
        f"  NEW violations (must be refactored onto the registry/base seam,"
        f" never allowlisted): {sorted(unexpected)}\n"
        f"  STALE allowlist entries (violation already closed — delete the"
        f" entry): {sorted(stale)}"
    )


def test_allowlist_has_reached_the_empty_set():
    """Phase-8 T4 closing pin: the generic layer imports NO concrete
    provider module anywhere — the shrink-only journey (3 keys at
    phase-8 start -> 1 after T3.5 -> 0 after T4.1) is complete. Re-grow
    only alongside the change that will remove the entry again."""
    assert ALLOWLIST == set()


def test_audit_catches_module_level_and_lazy_imports(tmp_path):
    """Positive control: the scanner itself detects both import shapes,
    in both address forms (package top level — syntactically identical to
    the retired flat module — and the fixed provider-module names), while
    the seam/facility surface stays exempt."""
    nodes_dir = tmp_path / "agent" / "nodes"
    nodes_dir.mkdir(parents=True)
    (nodes_dir / "sample_node.py").write_text(
        # provider-module form (module level) + package-top-level form:
        # both hit the violation surface.
        "from chaos_agent.agent.providers.k8s_native.provider import K8sNativeProvider\n"
        "from chaos_agent.agent.providers.chaosblade import ChaosbladeProvider\n"
        "from chaos_agent.agent.providers.chaosblade.python_provider import (\n"
        "    ChaosbladePythonProvider,\n"
        ")\n"
        "\n"
        "\n"
        "def f():\n"
        "    from chaos_agent.agent.providers.host_shell.provider import HostShellProvider\n"
        "    return HostShellProvider\n",
        encoding="utf-8",
    )
    # Legal seam imports must NOT be flagged: registry / base / package
    # ``__init__`` plus every family facility module.
    (nodes_dir / "seam_user.py").write_text(
        "from chaos_agent.agent.providers import FaultProviderRegistry\n"
        "from chaos_agent.agent.providers.base import FaultProvider\n"
        "from chaos_agent.agent.providers.message_scanning import build_tool_call_args_lookup\n"
        "from chaos_agent.agent.providers.chaosblade.verify import was_blade_create_attempted\n"
        "from chaos_agent.agent.providers.chaosblade.recover import run_layer1_destroy\n"
        "from chaos_agent.agent.providers.k8s_native.classifier import ALLOWED_MANIFEST_KINDS\n"
        "from chaos_agent.agent.providers.registry import FaultProviderRegistry as R\n",
        encoding="utf-8",
    )
    # Relative concrete imports must be flagged too, in both real-world
    # spellings: level-2 from a top-level package (l4) via
    # ``..agent.providers.x``, and level-3 from a nested package
    # (agent/nodes/execute) via ``...providers.x``.
    l4_dir = tmp_path / "l4"
    l4_dir.mkdir()
    (l4_dir / "rel.py").write_text(
        "from ..agent.providers.chaosblade.provider import extract_kubectl_exec_pod_name\n",
        encoding="utf-8",
    )
    deep_dir = nodes_dir / "execute"
    deep_dir.mkdir()
    (deep_dir / "deep.py").write_text(
        "from ...providers.k8s_native.provider import K8sNativeProvider\n",
        encoding="utf-8",
    )
    got = {(rel, module) for rel, module, _ in _iter_violations(tmp_path)}
    assert got == {
        ("agent/nodes/sample_node.py", "chaos_agent.agent.providers.k8s_native.provider"),
        ("agent/nodes/sample_node.py", "chaos_agent.agent.providers.chaosblade"),
        ("agent/nodes/sample_node.py", "chaos_agent.agent.providers.chaosblade.python_provider"),
        ("agent/nodes/sample_node.py", "chaos_agent.agent.providers.host_shell.provider"),
        ("l4/rel.py", "..agent.providers.chaosblade.provider"),
        ("agent/nodes/execute/deep.py", "...providers.k8s_native.provider"),
    }


def test_audit_flags_future_carrier_package_provider_modules(tmp_path):
    """Extensibility pin (providers-group-by-carrier T3.2): a NEW carrier
    package's registered provider modules land in the violation surface
    via the fixed-name arm — ``<any>.provider`` / ``<any>.python_provider``
    — with no rule change, while its facility modules (any other
    submodule name) stay seam exactly like the built-in families."""
    nodes_dir = tmp_path / "agent" / "nodes"
    nodes_dir.mkdir(parents=True)
    (nodes_dir / "future_user.py").write_text(
        "from chaos_agent.agent.providers.cloud_api.provider import CloudProvider\n"
        "from chaos_agent.agent.providers.cloud_api.python_provider import (\n"
        "    CloudPythonProvider,\n"
        ")\n"
        "from chaos_agent.agent.providers.cloud_api.detection import scan_cloud_uid\n"
        "from chaos_agent.agent.providers.cloud_api.verify import verify_cloud_state\n",
        encoding="utf-8",
    )
    got = {(rel, module) for rel, module, _ in _iter_violations(tmp_path)}
    assert got == {
        ("agent/nodes/future_user.py", "chaos_agent.agent.providers.cloud_api.provider"),
        ("agent/nodes/future_user.py", "chaos_agent.agent.providers.cloud_api.python_provider"),
    }


def test_carrier_packages_discovered_from_disk():
    """The carrier set is the REAL on-disk subpackage layout, not a hardcoded
    list: every directory under ``agent/providers`` that is a package is a
    carrier (including ``faultdrill`` — the one the retired hardcoded frozenset
    omitted, letting its package-top-level imports escape the audit), while the
    flat seam modules and ``__pycache__`` are excluded."""
    discovered = _discover_carrier_packages()
    # Carriers = subpackages on disk. Asserted as membership (not equality with
    # a hardcoded set) so the pin tracks disk truth rather than re-freezing it.
    for carrier in ("chaosblade", "k8s_native", "host_shell", "faultdrill"):
        assert carrier in discovered, f"{carrier} is a carrier subpackage on disk"
    # Flat seam MODULES (files) and non-package dirs are NOT carriers.
    for seam in ("registry", "base", "message_scanning", "uid_shapes", "__pycache__"):
        assert seam not in discovered, f"{seam} is a flat module / non-package"
    # The module-level constant is the discovery output (single source).
    assert CARRIER_PACKAGES == discovered


def test_discovery_tracks_new_carrier_without_rule_change(tmp_path):
    """Extensibility pin for the package-top-level arm: point discovery at a
    providers dir containing a NEW carrier subpackage plus a flat seam module
    and a ``__pycache__`` — only the carrier is discovered. This is the property
    the hardcoded frozenset lacked: a carrier added on disk is audited with zero
    edit to this file."""
    providers = tmp_path / "providers"
    (providers / "cloud_api").mkdir(parents=True)
    (providers / "cloud_api" / "__init__.py").write_text("", encoding="utf-8")
    (providers / "registry.py").write_text("", encoding="utf-8")  # flat seam module
    (providers / "__pycache__").mkdir()  # non-package dir
    assert _discover_carrier_packages(providers) == frozenset({"cloud_api"})


def test_package_top_level_import_of_disk_carrier_is_flagged(tmp_path):
    """Regression for the LATENT escape the hardcoded set allowed: a generic
    -layer module importing a disk carrier at PACKAGE TOP LEVEL
    (``providers.faultdrill`` — len 4, syntactically identical to the retired
    flat module) is now flagged, because ``faultdrill`` is discovered as a
    carrier. Under the old frozenset this import was invisible to the audit."""
    nodes_dir = tmp_path / "agent" / "nodes"
    nodes_dir.mkdir(parents=True)
    (nodes_dir / "leaky.py").write_text(
        "from chaos_agent.agent.providers.faultdrill import something\n",
        encoding="utf-8",
    )
    got = {(rel, module) for rel, module, _ in _iter_violations(tmp_path)}
    assert ("agent/nodes/leaky.py", "chaos_agent.agent.providers.faultdrill") in got
