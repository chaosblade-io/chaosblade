"""blade-ai update command: self-update via re-running the install script."""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import typer

# Release coordinates — MUST stay identical to ``scripts/install.sh``, which
# created the install this command replaces. blade-ai ships from the ChaosBlade
# repo under its own tag namespace; a mismatch here means ``update`` looks for
# artifacts that do not exist.
_RELEASES_API = "https://api.github.com/repos/chaosblade-io/chaosblade/releases"
_RELEASES_DOWNLOAD = "https://github.com/chaosblade-io/chaosblade/releases/download"
_TAG_PREFIX = "blade-ai-v"


#: What a version string may contain. It is interpolated into both a URL and a
#: filesystem path, so anything else is rejected up front: a value with a slash
#: made ``mkdtemp`` raise FileNotFoundError from OUTSIDE the try block, dumping a
#: traceback, and ``versions/blade-ai-v<value>`` would have resolved out of the
#: versions directory if it had got as far as the swap.
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
#: Network read timeout for the download. ``urlretrieve`` takes no timeout of its
#: own, so without this a stalled connection hangs the command indefinitely
#: rather than failing and cleaning up.
_DOWNLOAD_TIMEOUT_S = 60


class _UnsupportedPlatform(Exception):
    """No release artifact naming scheme exists for this OS."""

    def __init__(self, os_name: str) -> None:
        super().__init__(os_name)
        self.os_name = os_name


def _detect_platform() -> str:
    """Detect the current platform string for download URL construction."""
    os_name = platform.system().lower()
    arch = platform.machine().lower()
    # Normalize arch names
    arch_map = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    arch = arch_map.get(arch, arch)

    if os_name == "darwin":
        return f"darwin-{arch}"
    elif os_name == "linux":
        return f"linux-{arch}"
    elif os_name == "windows":
        # On ARM64 Windows running x64 Python, machine() returns "AMD64"
        # Use PROCESSOR_ARCHITEW6432 to detect true ARM64
        archite6432 = os.environ.get("PROCESSOR_ARCHITEW6432", "")
        if archite6432 == "ARM64" or arch == "arm64":
            arch = "arm64"
        else:
            arch = "x64"
        return f"windows-{arch}"
    else:
        # NOT a BadParameter: nothing the user typed is wrong. Raising that
        # printed "Invalid value" plus a usage block, which reads as "you passed
        # a bad argument" when the real answer is "your OS has no build".
        raise _UnsupportedPlatform(os_name)



def _is_standalone_install() -> bool:
    """Whether this process is the PyInstaller bundle that ``update`` can replace.

    ``sys.frozen`` is set by PyInstaller itself, so it answers the only question
    that matters — "am I a self-contained binary, or a pip/source install?" —
    from the running process, with nothing on disk to keep in sync.

    A ``receipt.json`` used to gate this. It was strictly worse: every fact it
    held was already available at runtime, a missing or hand-edited file blocked
    updating a healthy install, and this command rewrote the file's ``source``
    field on success, which then failed the very check it had just passed.
    """
    return bool(getattr(sys, "frozen", False))


def _current_version() -> str:
    """Installed version, read from the artifact that is running."""
    try:
        from chaos_agent import __version__
        return str(__version__) if __version__ else "unknown"
    except ImportError:
        return "unknown"


def _release_page(tag: str) -> str:
    """Human-facing release page for *tag* — the fallback when the API is down."""
    return f"{_RELEASES_DOWNLOAD.rsplit('/', 2)[0]}/releases/tag/{tag}"


def _available_versions() -> list[str]:
    """Published blade-ai versions, newest first. Empty when unreachable.

    MUST mirror ``scripts/install.sh``, which produced the install being updated.
    Three details are load-bearing and were each wrong here before, leaving the
    command unable to update anything:

    * The releases live in the **chaosblade** repo, not a ``blade-ai`` repo.
    * Their tags are namespaced ``blade-ai-v*`` so ChaosBlade's own ``v*`` tags do
      not collide — which makes ``/releases/latest`` useless: it returns whatever
      the repo published last, usually the ChaosBlade tool itself. The list has to
      be fetched and filtered by prefix.
    * ``BLADE_AI_MIRROR_API`` overrides the API base, the same knob install.sh
      offers for air-gapped mirrors.

    Returned as a list rather than just the newest so a failed download can tell
    the user which versions DO exist instead of pointing at a web page.
    """
    import urllib.error
    import urllib.request

    url = os.environ.get("BLADE_AI_MIRROR_API", _RELEASES_API) + "?per_page=100"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "blade-ai-update"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            releases = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(releases, list):
        return []

    candidates: list[tuple[tuple[int, ...], str]] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = str(release.get("tag_name") or "")
        if not tag.startswith(_TAG_PREFIX):
            continue
        semver = tag[len(_TAG_PREFIX):]
        # Sort on the leading integer components only, stopping at the first
        # non-numeric part — same rule install.sh uses.
        nums: list[int] = []
        for part in re.split(r"[.\-+]", semver):
            if part.isdigit():
                nums.append(int(part))
            else:
                break
        if nums:
            candidates.append((tuple(nums), semver))
    candidates.sort(reverse=True)
    return [semver for _nums, semver in candidates]


def _fetch_latest_version() -> str | None:
    """Newest published version, or ``None`` when it cannot be resolved."""
    versions = _available_versions()
    return versions[0] if versions else None


def _verify_checksum(archive: Path, base_url: str, tag: str, asset: str) -> bool:
    """Verify *archive* against the release's ``checksums.txt``.

    Returns False ONLY on a genuine mismatch — that is a corrupted or tampered
    download and must stop the update. An unreachable or entry-less
    ``checksums.txt`` warns and passes, matching ``install.sh``: older releases
    predate the checksum file, and failing closed there would make the command
    unable to update to them at all.

    ``install.sh`` verifies on first install; without this, every SELF-UPDATE
    afterwards replaced the binary with an unverified download.
    """
    import hashlib
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{base_url}/{tag}/checksums.txt", timeout=15) as resp:
            manifest = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        typer.echo(f"⚠ Skipping verification (checksums.txt unavailable: {exc})")
        return True

    # Match the FILENAME field, not the line. ``asset in line`` also matched
    # derived names — a ``blade-ai-darwin-arm64.tar.gz.sig`` entry listed first
    # handed back the signature's hash and reported a checksum MISMATCH for a
    # perfectly good download, blocking the update entirely.
    expected = ""
    for line in manifest.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        # sha256sum writes "<hex>  <name>" in text mode and "<hex> *<name>" in
        # binary mode; both appear in the wild.
        name = parts[1].strip().lstrip("*")
        if name == asset:
            expected = parts[0].strip()
            break
    if not expected:
        typer.echo(f"⚠ Skipping verification (no checksum entry for {asset})")
        return True

    digest = hashlib.sha256()
    with open(archive, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        typer.echo(
            f"✗ Checksum mismatch for {asset}\n"
            f"  Expected: {expected}\n  Actual:   {actual}",
            err=True,
        )
        return False
    typer.echo("✓ Checksum verified")
    return True


def _running_from(directory: Path) -> bool:
    """Whether THIS process's executable lives under *directory*.

    The binary ships in PyInstaller ``--onedir`` layout, so the running process
    lazily loads modules and data files out of ``<version dir>/_internal/``
    throughout its life. Renaming or deleting that directory mid-run makes every
    subsequent import fail — measured: ``ModuleNotFoundError`` for a module not
    yet imported at the time of the swap.

    Reinstalling the running version is a supported repair, so this cannot simply
    be refused; it has to be handled differently from replacing some other
    version.
    """
    if not getattr(sys, "frozen", False):
        return False
    try:
        exe = Path(sys.executable).resolve()
        exe.relative_to(directory.resolve())
    except (OSError, ValueError):
        return False
    return True


#: How old a work directory must be before another run may reclaim it. The sweep
#: cannot tell "abandoned by a killed process" from "in use right now" by name
#: alone — ``mkdtemp``'s suffix is random and carries no pid — so it goes by age.
#: Measured without this: a second ``update`` (or a concurrent ``install.sh``,
#: which uses the same ``versions/.tmp-*`` location) deleted the first one's
#: half-written archive, and that update failed with a tar error about a missing
#: file. An hour is far longer than any real download and short enough that
#: genuine litter does not accumulate.
_STALE_WORKDIR_AGE_S = 3600


def _sweep_stale_workdirs(versions_dir: Path) -> None:
    """Delete work directories left behind by earlier, finished runs.

    ``.tmp-*`` and ``*.replaced-*`` are normally cleaned up by the run that
    created them; a kill, or a deliberate keep for self-replacement, leaves them
    behind. Only ones older than ``_STALE_WORKDIR_AGE_S`` are touched, so a
    concurrent download is never pulled out from under another process.
    """
    now = time.time()
    for path in versions_dir.glob("*"):
        # Every filesystem probe below (``is_dir``/``stat``) can raise if the
        # entry is removed by a concurrent process mid-sweep. On Python 3.11
        # ``Path.is_dir()`` itself calls ``self.stat()``, so the guard must
        # wrap the whole classification block, not just the mtime check.
        try:
            if not path.is_dir():
                continue
            if not (path.name.startswith(".tmp-") or ".replaced-" in path.name):
                continue
            if _running_from(path):
                continue  # this process is still executing out of it
            if now - path.stat().st_mtime < _STALE_WORKDIR_AGE_S:
                continue  # possibly a live download by another process
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)


def _download_and_install(version: str, platform_str: str) -> bool:
    """Fetch the release artifact for *version* / *platform_str* and install it.

    Ordered so that nothing already working is disturbed until the replacement
    is proven good: download to a temp dir, verify the checksum, extract, confirm
    an executable came out, and only then swap directories and repoint the
    symlink. Every failure path leaves the previous install untouched and removes
    the temp dir — including ``KeyboardInterrupt``, which is why cleanup lives in
    ``finally`` rather than in the ``except`` clause.
    """
    import urllib.error
    import urllib.request

    if not _VERSION_RE.match(version):
        typer.echo(
            f"✗ Invalid version {version!r} — expected a plain version like 0.6.0.",
            err=True,
        )
        return False

    base_url = os.environ.get("BLADE_AI_MIRROR", _RELEASES_DOWNLOAD)
    tag = f"{_TAG_PREFIX}{version}"
    asset = f"blade-ai-{platform_str}.tar.gz"
    download_url = f"{base_url}/{tag}/{asset}"

    versions_dir = Path(os.path.expanduser("~/.blade-ai/versions"))
    # A first-ever update on a machine installed elsewhere has no versions dir;
    # mkdtemp below raises FileNotFoundError without this.
    try:
        versions_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        typer.echo(f"✗ Cannot create {versions_dir}: {exc}", err=True)
        return False

    final_dir = versions_dir / tag
    # Replacing the directory this process runs from is a special case, not an
    # error: it is how a corrupted install gets repaired. The displaced copy has
    # to stay on disk so the still-running process can keep loading from it.
    replacing_self = _running_from(final_dir)
    _sweep_stale_workdirs(versions_dir)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f".tmp-{tag}-", dir=str(versions_dir)))
    displaced: Path | None = None

    try:
        archive_path = tmp_dir / asset
        typer.echo(f"Downloading {download_url}")
        # ``urlretrieve`` has no timeout parameter, so read through urlopen with
        # one: a stalled connection otherwise hangs the command forever.
        with urllib.request.urlopen(download_url, timeout=_DOWNLOAD_TIMEOUT_S) as resp:
            with open(archive_path, "wb") as fh:
                shutil.copyfileobj(resp, fh)

        if not _verify_checksum(archive_path, base_url, tag, asset):
            return False

        subprocess.run(
            ["tar", "--strip-components=1", "-xzf", str(archive_path), "-C", str(tmp_dir)],
            check=True,
        )
        archive_path.unlink(missing_ok=True)

        # An archive can extract cleanly and still not contain the binary — a
        # truncated download that happened to gzip-decode, or a release asset
        # built wrong. Without this check the swap and symlink both "succeed" and
        # leave ``blade-ai`` pointing at a path that does not exist, which also
        # means the user can no longer run ``update`` to recover.
        new_binary = tmp_dir / "blade-ai"
        if not new_binary.is_file():
            typer.echo(
                f"✗ Archive for {asset} contains no 'blade-ai' executable "
                "— refusing to install it.",
                err=True,
            )
            return False
        # Extraction preserves mode, but a mirror may have stripped it.
        new_binary.chmod(new_binary.stat().st_mode | 0o755)

        # Swap, keeping the old version until the new one is in place. Deleting
        # first and renaming second was destructive in the gap between the two:
        # a failure there left neither version installed.
        if final_dir.exists():
            displaced = final_dir.with_name(f"{final_dir.name}.replaced-{os.getpid()}")
            if displaced.exists():
                shutil.rmtree(displaced, ignore_errors=True)
            final_dir.rename(displaced)
        try:
            tmp_dir.rename(final_dir)
        except OSError:
            if displaced is not None and not final_dir.exists():
                displaced.rename(final_dir)
                displaced = None
            raise

        # Repoint the symlink atomically. ``unlink`` followed by ``symlink_to``
        # has a window where nothing is on PATH: if the second call failed, the
        # user was left with a working binary on disk, no ``blade-ai`` command,
        # and an "Update failed" message implying nothing had changed. Building
        # the new link under a temp name and ``os.replace``-ing it over the old
        # one means the previous link survives every failure.
        symlink_dir = Path.home() / ".local" / "bin"
        symlink_dir.mkdir(parents=True, exist_ok=True)
        symlink_path = symlink_dir / "blade-ai"
        staged_link = symlink_dir / f".blade-ai.new-{os.getpid()}"
        staged_link.unlink(missing_ok=True)
        try:
            staged_link.symlink_to(final_dir / "blade-ai")
            os.replace(staged_link, symlink_path)
        finally:
            staged_link.unlink(missing_ok=True)

        typer.echo(f"✓ Updated to blade-ai v{version}")
        if replacing_self:
            # The files this process is executing from were just moved aside.
            # Anything it has not imported yet is now unreachable, so say plainly
            # that the result only takes effect on the next invocation.
            typer.echo(
                "  Note: this replaced the running binary — start a new "
                "'blade-ai' to use it."
            )
        return True

    except urllib.error.HTTPError as exc:
        # 404 is the release telling us this artifact does not exist. That is a
        # FACT about the release, not something to predict from a hardcoded
        # platform list — such a list would also reject a platform added to a
        # later release without any code change.
        if exc.code == 404:
            typer.echo(f"✗ Not published: {asset} for {tag}", err=True)
            typer.echo(f"  URL: {download_url}", err=True)
            # Answer the obvious next question here rather than sending the user
            # to a web page: the version list is one API call we already make for
            # ``--version``-less updates.
            versions = _available_versions()
            if versions:
                if version not in versions:
                    typer.echo(
                        f"  Version '{version}' is not among the published "
                        f"releases: {', '.join(versions[:10])}"
                        + (f" (+{len(versions) - 10} older)" if len(versions) > 10 else ""),
                        err=True,
                    )
                else:
                    # The version exists, so the missing piece is the platform.
                    typer.echo(
                        f"  Version {version} exists but has no build for "
                        f"'{platform_str}'.",
                        err=True,
                    )
            else:
                typer.echo(
                    "  Could not list published versions (network or mirror "
                    f"unreachable). Check {_release_page(tag)}",
                    err=True,
                )
        else:
            typer.echo(f"✗ Download failed (HTTP {exc.code}): {download_url}", err=True)
        return False

    except (urllib.error.URLError, subprocess.CalledProcessError, OSError) as exc:
        typer.echo(f"✗ Update failed: {exc}", err=True)
        return False

    finally:
        # Runs on success, failure AND KeyboardInterrupt. A Ctrl-C mid-download
        # used to strand a ``.tmp-*`` directory under versions/ forever.
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if displaced is not None and displaced.exists():
            if not final_dir.exists():
                displaced.rename(final_dir)  # swap never happened — restore
            elif replacing_self:
                # Keep it: this process is still lazily loading modules and data
                # out of that directory. Deleting it here caused ModuleNotFound
                # for anything imported after the swap. The next update's
                # ``_sweep_stale_workdirs`` reclaims it.
                pass
            else:
                shutil.rmtree(displaced, ignore_errors=True)  # swap completed


def _echo_non_standalone_update_paths() -> None:
    """Tell a pip/source install how to update, since it cannot self-update.

    Shown both when ``--check`` finds an update for such an install (so the next
    step is the right one, not ``blade-ai update`` which would just be refused)
    and when an actual update is attempted from one.
    """
    typer.echo(
        "This install is not the standalone binary, so it cannot self-update:\n"
        "  · pip install  → pip install --upgrade blade-ai\n"
        "  · from source  → git pull && make install\n"
        "  · standalone   → curl -fsSL https://chaosblade.io/install-agent.sh | bash"
    )


def update_command(
    version: str = typer.Option(None, "--version", "-v", help="Specific version to update to"),
    check: bool = typer.Option(False, "--check", help="Only check for updates, don't install"),
) -> None:
    """Update blade-ai to the latest version (standalone binary only).

    ``--version`` installs exactly that version — newer, older, or the one
    already running. Without it, updates to the latest release.

    ``--check`` only reports what is available and never mutates anything, so it
    works from any install; the self-update itself still requires the standalone
    binary and is refused otherwise right before the download.
    """
    # NOTE: the standalone gate is deliberately NOT here. ``--check`` is a
    # read-only query — "is there a newer release?" — and that answer is just as
    # useful to a pip or source install (they can then upgrade their own way).
    # Gating it up front refused to even look. The gate now sits directly in
    # front of the download, so only the mutation is restricted.
    standalone = _is_standalone_install()
    current_version = _current_version()

    # An EXPLICIT --version is an instruction, not a suggestion: install exactly
    # that, whether it is newer, older or the one already running. Downgrading
    # and reinstalling the same version are both legitimate — pinning back to a
    # known-good build, or repairing a corrupted install — and there is no
    # ``blade-ai install`` subcommand to redirect those cases to.
    explicit = version is not None
    target_version = version

    if explicit:
        # Reject malformed input here too: ``--check`` never reaches the download
        # that would otherwise catch it, so it would report "not published" for
        # what is really a typo.
        if not _VERSION_RE.match(target_version):
            typer.echo(
                f"Invalid version {target_version!r} — expected a plain version "
                "like 0.6.0.",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo(f"Installed: v{current_version}    Requested: v{target_version}")
        # ``--check`` promises to say what WOULD happen. Confirming the version
        # exists is part of that: without it the answer is "looks fine" and the
        # 404 only arrives during the real run.
        if check:
            published = _available_versions()
            if published and target_version not in published:
                typer.echo(
                    f"\nv{target_version} is not published. Available: "
                    + ", ".join(f"v{v}" for v in published[:10])
                    + (f" (+{len(published) - 10} older)" if len(published) > 10 else ""),
                )
                raise typer.Exit(1)
            # ``published`` is newest-first, so a LATER position is an older
            # release. Only claim a direction when both versions are in the list;
            # a locally-built or yanked current version makes the comparison
            # meaningless, and guessing there would be worse than staying quiet.
            if target_version == current_version:
                verdict = "would reinstall the running version (repairs a broken install)"
            elif current_version in published \
                    and published.index(target_version) > published.index(current_version):
                verdict = "would DOWNGRADE"
            else:
                verdict = "would install"
            typer.echo(f"\nThis {verdict}.")
            if standalone:
                typer.echo(f"Run: blade-ai update --version {target_version}")
            else:
                _echo_non_standalone_update_paths()
            raise typer.Exit(0)
    else:
        typer.echo(f"Installed: v{current_version}")
        typer.echo("Checking for the latest release...")
        target_version = _fetch_latest_version()
        if target_version is None:
            typer.echo(
                "\n✗ Could not reach the release list.\n"
                "  Check network access, or set BLADE_AI_MIRROR_API to a mirror.",
                err=True,
            )
            raise typer.Exit(1)
        if target_version == current_version:
            typer.echo(f"\nAlready on the latest release (v{current_version}). "
                       "Nothing to do.")
            raise typer.Exit(0)
        typer.echo(f"Latest:    v{target_version}")
        if check:
            typer.echo(f"\nAn update is available: v{current_version} → v{target_version}")
            if standalone:
                typer.echo("Run: blade-ai update")
            else:
                _echo_non_standalone_update_paths()
            raise typer.Exit(0)

    # The mutation gate: only the self-contained binary can replace itself. Every
    # ``--check`` path has already returned above, so this only stops an actual
    # install — a pip or source user reaches here only without ``--check``.
    if not standalone:
        _echo_non_standalone_update_paths()
        raise typer.Exit(1)

    # Perform update
    try:
        platform_str = _detect_platform()
    except _UnsupportedPlatform as exc:
        typer.echo(
            f"✗ No blade-ai build exists for {exc.os_name}.\n"
            "  Supported: Linux and macOS. On Windows, use WSL2.",
            err=True,
        )
        raise typer.Exit(1) from exc
    success = _download_and_install(target_version, platform_str)
    if not success:
        raise typer.Exit(1)