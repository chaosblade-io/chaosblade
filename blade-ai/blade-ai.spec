# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for blade-ai binary.

Uses --onedir mode (EXE + COLLECT) instead of --onefile for:
  - Fast startup (no extraction to /tmp on each launch)
  - macOS codesign + notarization compatibility
  - Stable data file paths via sys._MEIPASS -> _internal/
  - Long-running TUI safety (no /tmp cleanup risk)

Submodule strategy
------------------
Earlier revisions hand-maintained a ``hiddenimports`` list. With
chaos_agent's growth (server routes, agent nodes, tools, memory
modules, prompts/sections, …) that list went stale within weeks and
shipped binaries with ``ModuleNotFoundError`` at runtime — PyInstaller's
static tracer misses dynamic ``importlib.import_module`` calls in
plugin-style loaders (skills, tool registry, prompt sections).

The current spec uses ``collect_submodules`` from PyInstaller's hooks
API to recursively pull EVERY submodule of the relevant packages into
``hiddenimports``. Adding a new file under ``src/chaos_agent/...``
no longer requires a spec edit.

Per-platform vendor handling
-----------------------------
The ChaosBlade tool binary is platform-specific (Mach-O on macOS, ELF
on Linux). CI's ``release.yml`` downloads the correct tarball from
``github.com/chaosblade-io/chaosblade/releases`` for each matrix
target and extracts it into ``vendor/chaosblade/`` BEFORE pyinstaller
runs, so this spec just bundles whatever's there. Windows skips the
vendor bundle entirely — chaosblade has no Windows release; the
Windows binary is informational/TUI-only.
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# Determine project root (where this spec file lives)
PROJECT_ROOT = Path(SPECPATH)

# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------
#
# Static data the binary needs at runtime. Each tuple is
# (source-relative-to-PROJECT_ROOT, target-inside-bundle).
#
# Vendor binary is conditionally included — Windows has no chaosblade
# release upstream, so bundling whatever happens to sit in
# ``vendor/chaosblade/`` would smuggle a wrong-arch ELF/Mach-O into
# the .exe. Detect via the host OS at spec-evaluation time, which on
# CI matches the matrix target (each runner is a real native host
# for the platform it builds for, or a Docker container with matching
# arch via QEMU/buildx — never cross-compile).
datas = [
    ('skills', 'skills'),
    # The TS TUI bundle. CI's build-tui job produces this; for local
    # builds run ``cd tui && npm install && npm run build`` once first.
    # Missing → PyInstaller errors at Analysis time, which is the
    # right fail-loud behaviour (silently shipping without TUI
    # would mean every binary user falls back to the legacy Python
    # TUI that never shipped this version's behaviour).
    ('tui/dist/cli.js', 'chaos_agent/_tui_assets'),
    # Marker so Node parses cli.js as ESM without walking up the
    # directory tree (which can hit the user's home ``package.json``
    # in a PyInstaller bundle and trigger ``MODULE_TYPELESS_PACKAGE_JSON``).
    ('tui/dist/package.json', 'chaos_agent/_tui_assets'),
]

# chaosblade tool — per platform. Skip on Windows (no upstream build).
# CI's matrix step extracts the right tarball into ``vendor/chaosblade/``
# before invoking pyinstaller; we just need to detect "is the dir
# populated with a usable binary" and either bundle or skip.
_is_windows = sys.platform.startswith("win") or os.name == "nt"
_vendor_blade = PROJECT_ROOT / "vendor" / "chaosblade"
if not _is_windows and _vendor_blade.exists() and (_vendor_blade / "blade").exists():
    datas.append(('vendor/chaosblade', 'vendor/chaosblade'))

# Auto-collect data files shipped inside any chaos_agent submodule
# (e.g. prompt section markdown bundled alongside Python code).
# Empty result is fine — collect_data_files returns [] when the
# package has no non-Python files.
datas += collect_data_files('chaos_agent')

# ---------------------------------------------------------------------------
# Hidden imports
# ---------------------------------------------------------------------------
#
# ``collect_submodules`` walks the package tree and yields every
# importable name under it. Pulls plugin-style modules (skills,
# tool registry, prompt sections) that PyInstaller's tracer would
# otherwise miss.
#
# The third-party ``collect_submodules`` calls defend against the
# 1.x reorganisations in langgraph / langchain — both shipped major
# subpackage churn between 0.x and 1.x, so manual lists rot fast.
hiddenimports = []
hiddenimports += collect_submodules('chaos_agent')
hiddenimports += collect_submodules('langgraph')
hiddenimports += collect_submodules('langchain_core')
hiddenimports += collect_submodules('langchain_openai')
hiddenimports += collect_submodules('uvicorn')
hiddenimports += collect_submodules('starlette')
hiddenimports += collect_submodules('pydantic')
hiddenimports += collect_submodules('pydantic_settings')
hiddenimports += collect_submodules('watchdog')
# Stragglers that aren't subpackages but ARE referenced via
# ``importlib.import_module`` in third-party code:
hiddenimports += [
    'aiosqlite',
    'asyncpg',
    'httpx',
    'typer',
    'click',
    'yaml',
]

# ---------------------------------------------------------------------------
# Target arch — required for clean macOS builds.
# ---------------------------------------------------------------------------
#
# ``release.yml`` exports BLADE_TARGET_ARCH per matrix entry; we
# honour it so a macos-13 (Intel) runner can't accidentally produce
# an arm64 binary or vice versa. ``None`` (the historical default)
# defers to host arch, which is fragile when GitHub re-points
# ``macos-latest`` to a different architecture between releases.
target_arch = os.environ.get('BLADE_TARGET_ARCH') or None

# ---------------------------------------------------------------------------
# Analysis / EXE / COLLECT
# ---------------------------------------------------------------------------
a = Analysis(
    ['src/chaos_agent/cli/main.py'],
    pathex=[str(PROJECT_ROOT / 'src')],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Heavy scientific stack PyInstaller may pull via langchain
        # transitively. We don't use any of these — exclude saves
        # ~80 MB on the binary.
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
        'IPython',
        'notebook',
        'pytest',
        'pytest_asyncio',
        'pytest_mock',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # --onedir: binaries go into COLLECT, not EXE
    name='blade-ai',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # macOS: UPX breaks codesign; Linux: minimal benefit
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=target_arch,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,  # macOS: UPX breaks codesign
    upx_exclude=[],
    name='blade-ai',
)
