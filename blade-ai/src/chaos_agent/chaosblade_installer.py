"""ChaosBlade runtime installer — download on first use.

pip install blade-ai ships a pure-Python wheel (py3-none-any) without
native binaries. This module downloads the matching ChaosBlade release
tarball from GitHub on first run and extracts it to
``~/.blade-ai/vendor/chaosblade/``.

Triggers (NOT pip-install time — pip just unpacks the wheel):
  - ``blade_create`` (tools/blade.py) calls ``ensure_chaosblade_async()``
    before the first mutating injection — the universal chokepoint that
    covers every path (CLI direct / CLI NL / TUI / server API), off the
    event loop via ``asyncio.to_thread``.
  - The CLI (``run_command`` in preflight.py) calls ``ensure_chaosblade``
    pre-emptively with a stderr progress line for inject/recover.

Preflight checks are deliberately side-effect-free (no download) so the
async TUI preflight stays within its 8s budget. PyInstaller and source-
tree users already have a resolvable blade binary — ``ensure_chaosblade``
detects that and never downloads.
"""

import asyncio
import hashlib
import logging
import platform
import shutil
import stat
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from chaos_agent.config.settings import settings

logger = logging.getLogger(__name__)

CHAOSBLADE_VERSION = "1.9.0-alpha"

_DOWNLOAD_URL_TEMPLATE = (
    "https://chaosblade.oss-cn-hangzhou.aliyuncs.com/agent/github/"
    "{version}/chaosblade-{version}-{platform}.tar.gz"
)

_CHECKSUMS: dict[str, str] = {
    "darwin_amd64": "70c06b465f9d8cf40106ce9d88e960ae64a7db1577bc0b79147ee019e311851d",
    "darwin_arm64": "2f41dc14b22fe18840cca0a12931fd73e2eef10e41ebd01d5fe7279c34cce213",
    "linux_amd64": "dc6ab90244015af34cb4f4722653c1506a428aab9f0b18435aca64f8866316fd",
    "linux_arm64": "5a496e8f377aac9a9eef80ffd0073d7cc90ccba060604aa245990cc3ee8b7bcc",
}

# Canonical map: ChaosBlade release arch → pip wheel platform tag. Used by
# ``make wheel-platform`` (cross-target builds) and mirrored inline in
# .github/workflows/release.yml (CI can't import this package at build time).
# A wheel is just packaging, not compilation, so any host can produce any
# target's wheel — only the bundled binary differs.
_WHEEL_PLATFORM_TAGS: dict[str, str] = {
    "darwin_arm64": "macosx_11_0_arm64",
    "darwin_amd64": "macosx_10_9_x86_64",
    "linux_amd64": "manylinux2014_x86_64",
    "linux_arm64": "manylinux2014_aarch64",
}


def wheel_platform_tag(target: str) -> str:
    """Map a ChaosBlade release arch (e.g. ``darwin_amd64``) to its wheel tag."""
    tag = _WHEEL_PLATFORM_TAGS.get(target)
    if not tag:
        raise RuntimeError(
            f"No wheel platform tag for target {target!r}. "
            f"Known: {sorted(_WHEEL_PLATFORM_TAGS)}"
        )
    return tag


def _detect_platform() -> str:
    """Detect current platform as ``{os}_{arch}`` matching release naming."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    os_map = {"darwin": "darwin", "linux": "linux"}
    arch_map = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}

    os_name = os_map.get(system)
    arch_name = arch_map.get(machine)

    if not os_name or not arch_name:
        raise RuntimeError(
            f"Unsupported platform: {system}/{machine}. "
            f"ChaosBlade only supports: {', '.join(_CHECKSUMS.keys())}"
        )

    return f"{os_name}_{arch_name}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _set_executable(path: Path) -> None:
    """Add executable permission to a file."""
    st = path.stat()
    path.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _get_vendor_dir() -> Path:
    return settings.chaosblade_vendor_dir.expanduser()


def _blade_available() -> bool:
    """True if a usable ``blade`` binary is resolvable anywhere.

    Reuses ``settings._resolve_blade_path()`` so this matches exactly what
    the agent will invoke at runtime — covers the runtime vendor dir
    (~/.blade-ai/vendor), PyInstaller bundle, source tree, an explicit
    ``blade_path`` setting, and the system PATH. Returns False only when
    the resolver falls through to its bare ``"blade"`` sentinel with
    nothing on PATH — i.e. a genuine pip-install-with-no-binary state.
    """
    from chaos_agent.utils.blade_paths import is_executable
    return is_executable(settings._resolve_blade_path())


def download_chaosblade(
    dest_dir: Path,
    on_progress: Optional[Callable[[int, int], None]] = None,
    target: Optional[str] = None,
) -> Path:
    """Download + extract ChaosBlade into ``dest_dir``.

    ``dest_dir`` is the ``chaosblade`` directory itself — after this call it
    contains ``blade``, ``bin/``, ``yaml/``, ``lib/``. Any existing contents
    are replaced. Returns the path to the ``blade`` binary.

    ``target`` selects which platform's binary to fetch (a key of
    ``_CHECKSUMS``, e.g. ``darwin_amd64``). Defaults to the host platform.
    A non-host target enables cross-target packaging — e.g. building a
    macOS x86_64 wheel on an arm64 host — because nothing here executes the
    binary; it's just downloaded and packed. ``make wheel-platform TARGET=``
    and CI both use this.

    Shared by ``ensure_chaosblade`` (targets ``~/.blade-ai/vendor/chaosblade``
    at runtime) and the ``make build`` / ``make wheel-platform`` targets.
    Version + checksums live in this module so all callers stay in lockstep.

    ``on_progress(downloaded_bytes, total_bytes)`` is invoked during the
    download for callers that want to render a progress line (the CLI).
    ``total_bytes`` is 0 when the server omits Content-Length.

    Raises RuntimeError on unsupported platform or download/checksum failure.
    """
    dest_dir = Path(dest_dir)
    plat = target or _detect_platform()
    expected_hash = _CHECKSUMS.get(plat)
    if not expected_hash:
        raise RuntimeError(
            f"No checksum for platform {plat!r}. Known: {sorted(_CHECKSUMS)}"
        )

    url = _DOWNLOAD_URL_TEMPLATE.format(version=CHAOSBLADE_VERSION, platform=plat)
    logger.info("Downloading ChaosBlade v%s for %s ...", CHAOSBLADE_VERSION, plat)

    reporthook = None
    if on_progress is not None:
        def reporthook(block_num: int, block_size: int, total_size: int) -> None:
            done = block_num * block_size
            if total_size > 0:
                done = min(done, total_size)
            try:
                on_progress(done, max(total_size, 0))
            except Exception:
                pass  # progress UI must never break the download

    with tempfile.TemporaryDirectory() as tmpdir:
        tarball = Path(tmpdir) / "chaosblade.tar.gz"

        try:
            urllib.request.urlretrieve(url, str(tarball), reporthook)
        except Exception as e:
            raise RuntimeError(f"Failed to download ChaosBlade from {url}: {e}") from e

        actual_hash = _sha256_file(tarball)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"Checksum mismatch for {url}: "
                f"expected {expected_hash}, got {actual_hash}"
            )

        with tarfile.open(tarball, "r:gz") as tf:
            tf.extractall(tmpdir)

        extracted = Path(tmpdir) / f"chaosblade-{CHAOSBLADE_VERSION}-{plat}"
        if not extracted.is_dir():
            raise RuntimeError(
                f"Unexpected archive structure: {extracted} not found after extraction"
            )

        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.move(str(extracted), str(dest_dir))

    blade_path = dest_dir / "blade"
    _set_executable(blade_path)
    bin_dir = dest_dir / "bin"
    if bin_dir.is_dir():
        for f in bin_dir.iterdir():
            if f.is_file():
                _set_executable(f)

    # Stamp which arch this vendor dir holds. Lets `make build` /
    # `make wheel-platform` tell "the binary already here is the one I need"
    # from "it's a leftover from a different target" — without this, a prior
    # cross-build (e.g. TARGET=darwin_amd64) would leave a foreign-arch blade
    # that a later host `make build` would silently bundle.
    try:
        (dest_dir / ".arch").write_text(plat, encoding="utf-8")
    except Exception:
        pass  # marker is an optimization, not load-bearing

    logger.info("ChaosBlade v%s (%s) installed to %s", CHAOSBLADE_VERSION, plat, dest_dir)
    return blade_path


def ensure_chaosblade(
    on_progress: Optional[Callable[[int, int], None]] = None
) -> Path:
    """Ensure a usable ChaosBlade binary exists; download to ~/.blade-ai if not.

    Idempotent and resolution-aware: if blade is already resolvable anywhere
    (PyInstaller bundle, system PATH, source-tree vendor, explicit
    ``blade_path``, or a prior ~/.blade-ai download) it returns that path
    without downloading. Only a genuine pip-install-with-no-binary state
    triggers a download into ``~/.blade-ai/vendor/chaosblade/``.

    ``on_progress`` is forwarded to ``download_chaosblade`` for progress UI.

    Raises RuntimeError on unsupported platform or download/checksum failure.
    """
    if _blade_available():
        resolved = settings._resolve_blade_path()
        logger.debug("ChaosBlade already available at %s", resolved)
        return Path(resolved)

    return download_chaosblade(_get_vendor_dir() / "chaosblade", on_progress=on_progress)


async def ensure_chaosblade_async(
    on_progress: Optional[Callable[[int, int], None]] = None
) -> Path:
    """Async wrapper: run the blocking ``ensure_chaosblade`` off the event loop.

    Used by ``blade_create`` (and any other async caller) so the 51MB
    download never blocks the asyncio loop that serves the TUI / API.
    """
    return await asyncio.to_thread(ensure_chaosblade, on_progress)
