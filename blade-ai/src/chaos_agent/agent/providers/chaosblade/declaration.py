"""ChaosBlade carrier declaration — vocabulary, preview and binary lookup.

Phase-12 (spec-import-retirement): this module is the carrier's *lightweight
knowledge surface*. The spec layer (``fault_registry`` / ``fault_spec`` /
``plan_generator``) consumes carrier vocabulary and the command preview via
the providers assembly point, which reads this module — never the heavy
provider implementation. Keeping this surface cheap to import is what lets
``fault_spec`` derive ``INTENT_*`` at import time without pulling the whole
execution stack.

Dependency discipline (pinned by the declaration guard in
``tests/test_agent/test_phase9_rename_guards.py``): stdlib, typing,
``chaos_agent.transports`` and ``chaos_agent.config.settings`` ONLY — no
imports of the providers assembly layer, ``agent.spec``, or sibling provider
implementation modules.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Carrier vocabulary
# ---------------------------------------------------------------------------

#: Carrier id of the ChaosBlade backend (k8s + host modes).
CARRIER_ID = "chaosblade"

#: OS-subsystem target types this carrier can attack. Single source: the
#: provider class reads these tuples as its ``supported_targets`` /
#: ``supported_actions`` class attributes, so the runtime provider surface and
#: the ``fault_registry`` aggregation can never drift apart.
SUPPORTED_TARGETS = ("cpu", "mem", "network", "disk", "process")

#: ChaosBlade action verbs.
SUPPORTED_ACTIONS = (
    "fullload",
    "load",
    "delay",
    "loss",
    "drop",
    "fill",
    "kill",
    "burn",
    "stop",
)

#: Carrier id of the in-process Python application backend (``blade create
#: python``), whose provider lives in this same sub-package.
PYTHON_CARRIER_ID = "chaosblade_python"

#: Middleware clients the in-process agent can intercept.
PYTHON_SUPPORTED_TARGETS = (
    "redis",
    "mysql",
    "http",
    "httpx",
    "grpc",
    "kafka",
    "sqlalchemy",
)

#: Method-level fault verbs the in-process agent applies.
PYTHON_SUPPORTED_ACTIONS = ("delay", "throwCustomException", "returnValue")

# ---------------------------------------------------------------------------
# Binary path resolution — the blade-binary lookup is this carrier's domain
# knowledge, migrated from utils/blade_paths.py (phase-9 T3.2) and from
# provider.py (phase-12). The generic helpers that shared that file
# (resolve_exec_path / is_executable) live in utils/exec_path.py: their
# kubectl / git / shell consumers are carrier-agnostic and must not depend on
# this module.
# ---------------------------------------------------------------------------

def _get_base_path() -> Path:
    """Return the base path for locating bundled resources.

    - PyInstaller bundle: sys._MEIPASS
    - Source / editable install: project root (contains vendor/ and src/)
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    # Walk up from this file to find project root (contains vendor/ and
    # pyproject.toml)
    this_dir = Path(__file__).resolve().parent
    for parent in [this_dir, *this_dir.parents]:
        if (parent / "vendor").is_dir() and (parent / "pyproject.toml").is_file():
            return parent
    # Fallback: current working directory
    return Path.cwd()


def get_bundled_blade_path() -> str:
    """Return the path to the bundled ``blade`` binary, or ``"blade"`` if
    not found (falls back to system PATH).

    The lookup order is:

    1. Runtime vendor dir: ``~/.blade-ai/vendor/chaosblade/blade``
    2. Wheel-bundled (platform wheel): ``<chaos_agent>/_vendor/chaosblade/blade``
    3. PyInstaller bundle: ``<base>/vendor/chaosblade/blade``
    4. Source tree: ``<project-root>/vendor/chaosblade/blade``
    5. Environment variable ``BLADE_AI_BLADE_PATH``
    6. System PATH (just ``"blade"``)
    """
    # Check runtime vendor dir (pip install + first-use download)
    from chaos_agent.config.settings import settings
    runtime_blade = settings.chaosblade_vendor_dir.expanduser() / "chaosblade" / "blade"
    if runtime_blade.is_file():
        runtime_blade.chmod(runtime_blade.stat().st_mode | 0o111)
        logger.debug(f"Using runtime vendor blade: {runtime_blade}")
        return str(runtime_blade)

    # Check wheel-bundled location (platform wheel force-includes the binary
    # at chaos_agent/_vendor/chaosblade/). ``parents[3]`` from this file
    # (agent/providers/chaosblade/declaration.py) is the ``chaos_agent``
    # package dir — same depth as the provider.py layout, so the phase-12
    # move from provider.py kept this computation unchanged. pip may strip
    # the +x bit from zip members, so re-add it here.
    wheel_blade = Path(__file__).resolve().parents[3] / "_vendor" / "chaosblade" / "blade"
    if wheel_blade.is_file():
        wheel_blade.chmod(wheel_blade.stat().st_mode | 0o111)
        logger.debug(f"Using wheel-bundled blade: {wheel_blade}")
        return str(wheel_blade)

    base = _get_base_path()

    # Check bundled location
    bundled = base / "vendor" / "chaosblade" / "blade"
    if bundled.is_file():
        # Ensure executable
        bundled.chmod(bundled.stat().st_mode | 0o111)
        logger.debug(f"Using bundled blade: {bundled}")
        return str(bundled)

    # Also check relative to the executable (for one-file PyInstaller)
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        bundled = exe_dir / "vendor" / "chaosblade" / "blade"
        if bundled.is_file():
            bundled.chmod(bundled.stat().st_mode | 0o111)
            logger.debug(f"Using bundled blade (exe dir): {bundled}")
            return str(bundled)

    # Check env override
    env_path = os.environ.get("BLADE_AI_BLADE_PATH", "")
    if env_path and Path(env_path).is_file():
        return env_path

    # Check system PATH
    found = shutil.which("blade")
    if found:
        logger.debug(f"Using system blade: {found}")
        return found

    logger.warning("blade binary not found (not bundled, not in PATH)")
    return "blade"


# ---------------------------------------------------------------------------
# Create-args construction — the blade_create tool-argument shape is this
# carrier's domain knowledge, migrated from utils/fault_type.py (phase-9
# T3.3) and from provider.py (phase-12). Consumers: the command preview
# below and (for the preview's contract) whatever actually invokes
# blade_create — both resolve through this single construction.
# ---------------------------------------------------------------------------

def build_blade_create_args(
    scope: str,
    target: str,
    action: str,
    namespace: str = "",
    names: str = "",
    labels: str = "",
    kubeconfig: str = "",
    params: dict = None,
    params_flags: list = None,
    duration: int = 0,
) -> dict:
    """Build blade_create.ainvoke() arguments from structured parameters.

    Construction logic:
    1. params key-value pairs → "--key value" in flags
    2. params_flags bare keys → "--key" in flags (boolean flags)
    3. duration > 0 → "--timeout <duration>" appended to flags
    4. evict_count/evict_percent left empty (not needed for structured previews)

    Note: MUTATES the passed ``params`` dict when duration > 0 and params
    already carries a "timeout" key (the single --timeout guarantee).
    Callers must pass a copy they own — e.g. ``dict(spec.params)`` — never
    a dict they expect to stay untouched.

    Returns:
        Dict matching blade_create tool signature:
        {scope, target, action, namespace, names, labels, kubeconfig,
         evict_count, evict_percent, flags}
    """
    # Duration contract precedence: the structured ``duration`` field is the
    # single authority — if params already has a "timeout" key, override its
    # value; otherwise append --timeout after params. Either way, only one
    # --timeout. The value is written as declared (no floor adjustment).
    if duration > 0 and params and "timeout" in params:
        params["timeout"] = str(duration)

    flags_parts = []
    if params:
        for k, v in params.items():
            flags_parts.extend([f"--{k}", str(v)])
    if params_flags:
        for flag in params_flags:
            flags_parts.append(f"--{flag}")
    if duration > 0 and (not params or "timeout" not in params):
        flags_parts.extend(["--timeout", str(duration)])

    return {
        "scope": scope,
        "target": target,
        "action": action,
        "namespace": namespace,
        "names": names,
        "labels": labels,
        "kubeconfig": kubeconfig,
        "evict_count": "",
        "evict_percent": "",
        "flags": " ".join(flags_parts),
    }


def _split_flags(flags_str: str) -> list[str]:
    """Split a flags string into ``--key value`` pairs."""
    if not flags_str:
        return []
    parts = flags_str.split()
    result = []
    i = 0
    while i < len(parts):
        if parts[i].startswith("--") and i + 1 < len(parts) and not parts[i + 1].startswith("--"):
            result.append(f"{parts[i]} {parts[i + 1]}")
            i += 2
        else:
            result.append(parts[i])
            i += 1
    return result


def build_command_preview(
    scope: str,
    target: str,
    action: str,
    namespace: str = "",
    names: str = "",
    labels: str = "",
    kubeconfig: str = "",
    params: dict = None,
    params_flags: list = None,
    duration: int = 0,
) -> str:
    """Render the ``## Injection Command`` markdown section for a blade
    create.

    Whole-function migration of the carrier knowledge that used to live in
    ``plan_generator._section_inject_command`` (phase-12): the args
    construction, the ``blade create k8s ...`` command prefix, the flag
    formatting and the kubewiz-channel ``--kubeconfig`` gate. The preview
    must keep matching what actually executes — same construction, same
    flags, byte-for-byte.
    """
    args = build_blade_create_args(
        scope=scope,
        target=target,
        action=action,
        namespace=namespace,
        names=names,
        labels=labels,
        kubeconfig=kubeconfig,
        params=params,
        params_flags=params_flags,
        # Preview must match what actually executes: duration translates to
        # the single --timeout flag (params never carry it under the contract).
        duration=duration,
    )

    # Format as human-readable command
    parts = [f"blade create k8s {scope}-{target} {action}"]
    if args.get("namespace"):
        parts.append(f"  --namespace {args['namespace']}")
    if args.get("names"):
        parts.append(f"  --names {args['names']}")
    if args.get("labels"):
        parts.append(f"  --labels {args['labels']}")
    if args.get("flags"):
        for flag_pair in _split_flags(args["flags"]):
            parts.append(f"  {flag_pair}")
    # Resolved at CALL time, not module import — the channel can change
    # between import and use, and callers (tests included) patch
    # ``chaos_agent.transports.is_kubewiz_channel`` expecting the gate to
    # honour it (the migrated plan_generator code did the same).
    from chaos_agent.transports import is_kubewiz_channel
    if not is_kubewiz_channel() and kubeconfig:
        parts.append(f"  --kubeconfig {kubeconfig}")

    cmd_str = " \\\n".join(parts)
    return f"## Injection Command\n\n```bash\n{cmd_str}\n```"
