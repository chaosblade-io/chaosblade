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

    1. PyInstaller bundle: ``<base>/vendor/chaosblade/blade``
    2. Source tree: ``<project-root>/vendor/chaosblade/blade``
    3. Environment variable ``BLADE_AI_BLADE_PATH``
    4. System PATH (just ``"blade"``)
    """
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
