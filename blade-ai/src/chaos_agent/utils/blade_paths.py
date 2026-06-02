"""Path resolution for bundled third-party binaries (e.g. ChaosBlade).

When running from a PyInstaller bundle, resources are extracted to
sys._MEIPASS.  When running from source, they are relative to the
project root.  This module provides a unified way to locate them.
"""

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_base_path() -> Path:
    """Return the base path for locating bundled resources.

    - PyInstaller bundle: sys._MEIPASS
    - Source / editable install: project root (contains vendor/ and src/)
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    # Walk up from this file to find project root (contains vendor/ and pyproject.toml)
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
    # at chaos_agent/_vendor/chaosblade/). ``parents[1]`` from this file
    # (utils/blade_paths.py) is the ``chaos_agent`` package dir. pip may
    # strip the +x bit from zip members, so re-add it here.
    wheel_blade = Path(__file__).resolve().parents[1] / "_vendor" / "chaosblade" / "blade"
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


def is_executable(cmd: str) -> bool:
    """Return True if ``cmd`` resolves to a usable executable.

    ``cmd`` may be either shape a binary resolver returns:
      - a full / relative path (bundled, runtime vendor, repo, configured)
      - a bare command name to resolve on PATH

    These need different checks, and crucially they must NOT be conflated
    by passing a path to ``shutil.which``. On Windows before Python 3.12,
    ``shutil.which`` mishandles a ``cmd`` that contains a directory
    component and returns ``None`` even for a valid executable — and the
    project's ``requires-python = ">=3.11"`` puts 3.11-on-Windows in scope.
    So: anything with a directory component is checked as a file directly;
    only a bare name goes through ``shutil.which``.

    Shared by the blade and kubectl presence checks (preflight) and the
    runtime blade-availability probe (chaosblade_installer).
    """
    if not cmd:
        return False
    if os.path.dirname(cmd):
        # Has a path component → check the file directly, never via which().
        return os.path.isfile(cmd)
    # Bare command name → PATH lookup (no directory component, Windows-safe).
    return shutil.which(cmd) is not None
