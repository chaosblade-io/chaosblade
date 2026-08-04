"""blade-ai uninstall command: remove blade-ai from the system.

Everything this needs is a fixed convention laid down by ``scripts/install.sh``:

    ~/.blade-ai/versions/    every installed version
    ~/.local/bin/blade-ai    the symlink on PATH
    ~/.blade-ai/             config, skills, memory, logs

So uninstall derives the paths instead of reading ``install-manifest.json``. The
manifest was worse in two concrete ways:

* Losing or corrupting the file made uninstall impossible — it exited 1 and left
  the user to delete directories by hand.
* It recorded ONE ``install_dir``, and ``update`` never rewrote it. After a
  single update the manifest still pointed at the previous version, so uninstall
  deleted the old directory and left the one actually in use behind.

Sweeping ``versions/`` removes every version, which is what "uninstall" means.
"""

import os
import platform
import re
import shutil
import sys
from pathlib import Path

import typer

#: Laid down by install.sh; also where ``update`` places new versions.
VERSIONS_DIR = "~/.blade-ai/versions"
SYMLINK_PATH = "~/.local/bin/blade-ai"
CONFIG_DIR = "~/.blade-ai"
#: Shell profiles install.sh may have appended a PATH line to. MUST cover every
#: file its ``add_to_profile`` calls can touch — ``~/.zprofile`` was missing, so
#: on macOS (zsh by default) with a login-shell profile present, install.sh wrote
#: the PATH line there and uninstall left it behind while reporting PATH cleaned.
PROFILE_CANDIDATES = (
    "~/.bashrc", "~/.bash_profile",
    "~/.zshrc", "~/.zprofile",
    "~/.profile", "~/.config/fish/config.fish",
)
#: install.sh tags its PATH line with this comment, so removal is exact.
PATH_MARKER = "# blade-ai"
#: A real version directory: ``blade-ai-v`` plus a numeric-led version and
#: nothing else. ``update`` also creates ``.tmp-blade-ai-v*-xxxx`` while
#: downloading and ``blade-ai-v*.replaced-<pid>`` while swapping; a crash or
#: Ctrl-C can strand either. Without this pattern they counted as installed
#: versions — and ``.replaced-<pid>`` even sorted equal to the real release it
#: displaced, so it could be picked as "newest remaining" and have the PATH
#: symlink pointed inside it.
_VERSION_DIR_RE = re.compile(r"^blade-ai-v\d+(?:\.\d+)*$")
#: What ``--version`` may contain. The value is interpolated into a path, so a
#: value like ``0.6.0/../../memory`` would resolve OUTSIDE ``versions/`` and
#: rmtree whatever it landed on — measured: it deleted ``~/.blade-ai/memory/``
#: and reported success.
_VERSION_ARG_RE = re.compile(r"^\d+(?:\.\d+)*$")


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def _version_sort_key(path: Path) -> tuple[int, ...]:
    """Numeric sort key for a version directory name.

    Sorting the directory names as STRINGS orders "0.10.0" before "0.9.0",
    because '1' < '9' character-wise. That silently picked the wrong "newest
    remaining" version when the active one was removed. Compare the leading
    integer components instead, stopping at the first non-numeric part — the same
    rule ``update`` uses to resolve the latest release.
    """
    semver = path.name.removeprefix("blade-ai-v")
    nums: list[int] = []
    for part in re.split(r"[.\-+]", semver):
        if part.isdigit():
            nums.append(int(part))
        else:
            break
    return tuple(nums)


def _installed_versions() -> list[Path]:
    """Installed version directories, oldest first.

    Only names matching ``_VERSION_DIR_RE`` count, so ``update``'s in-flight
    directories are ignored rather than mistaken for releases. Uninstall removes
    all of them: ``update`` accumulates versions there, and leaving any behind
    means the next ``blade-ai`` on PATH may still resolve.
    """
    versions = _expand(VERSIONS_DIR)
    if not versions.is_dir():
        return []
    return sorted(
        (p for p in versions.iterdir()
         if p.is_dir() and _VERSION_DIR_RE.match(p.name)),
        key=_version_sort_key,
    )


def _active_version_dir() -> Path | None:
    """The version directory the PATH symlink currently resolves to.

    Removing that directory without repointing the symlink leaves ``blade-ai`` on
    PATH resolving to nothing, so ``--version`` needs to know which one it is.
    """
    symlink = _expand(SYMLINK_PATH)
    if not symlink.is_symlink():
        return None
    try:
        target = Path(os.readlink(symlink))
    except OSError:
        return None
    parent = target.parent
    return parent if parent.parent == _expand(VERSIONS_DIR) else None


def _out_of_tree_install() -> Path | None:
    """Install directory the symlink points at, when it is NOT under ``versions/``.

    ``install.sh`` accepts ``--prefix PATH`` and ``BLADE_AI_INSTALL_DIR``, which
    put the binary somewhere of the user's choosing while still linking it from
    ``~/.local/bin``. Sweeping ``versions/`` finds nothing in that case, so
    uninstall used to remove the symlink, report "✨ blade-ai has been
    uninstalled", and leave the entire install on disk.

    The directory is REPORTED, not deleted: a custom prefix may be a shared
    location (``~/bin``, ``/usr/local``) holding unrelated files, and there is no
    way to tell that apart from a dedicated one. Naming the path fixes the actual
    defect, which was silence.
    """
    symlink = _expand(SYMLINK_PATH)
    if not symlink.is_symlink():
        return None
    try:
        parent = Path(os.readlink(symlink)).parent
    except OSError:
        return None
    # A dangling symlink names a directory that is no longer there — reporting
    # "its files are still at X" would send the user looking for nothing.
    if not parent.is_dir():
        return None
    try:
        parent.relative_to(_expand(VERSIONS_DIR))
    except ValueError:
        return parent
    return None


def _profiles_with_path_entry() -> list[Path]:
    """Shell profiles that currently carry the blade-ai PATH line."""
    found = []
    for candidate in PROFILE_CANDIDATES:
        path = _expand(candidate)
        try:
            if path.is_file() and PATH_MARKER in path.read_text():
                found.append(path)
        except OSError:
            continue
    return found


def _strip_path_lines(profiles: list[Path]) -> list[Path]:
    """Drop the marked PATH lines. Returns the files actually modified."""
    modified = []
    for path in profiles:
        try:
            lines = path.read_text().splitlines()
            kept = [line for line in lines if PATH_MARKER not in line]
            if len(kept) < len(lines):
                path.write_text("\n".join(kept) + "\n")
                modified.append(path)
        except OSError:
            continue
    return modified


def _running_from(directory: Path) -> bool:
    """Whether THIS process's executable lives under *directory*.

    The binary ships in PyInstaller ``--onedir`` layout, so it lazily loads
    modules out of ``<version dir>/_internal/`` for as long as it runs. Deleting
    that directory mid-run makes later imports fail with ModuleNotFoundError, so
    removing the running version is worth announcing even though it is allowed.
    """
    if not getattr(sys, "frozen", False):
        return False
    try:
        exe = Path(sys.executable).resolve()
        exe.relative_to(directory.resolve())
    except (OSError, ValueError):
        return False
    return True


def _remove_windows_path_entry(install_dir: str) -> bool:
    """Remove *install_dir* from the Windows user PATH."""
    if platform.system() != "Windows":
        return False
    import winreg
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        )
        path_value, _ = winreg.QueryValueEx(key, "Path")
        entries = [e for e in path_value.split(";") if install_dir not in e]
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, ";".join(entries))
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def _remove_one_version(version: str, force: bool) -> None:
    """Delete a single installed version, leaving the install otherwise intact.

    A different operation from uninstalling: PATH, config and the other versions
    stay. ``update`` accumulates versions under ``versions/``, so this is how the
    old ones get reclaimed without tearing down the install.

    When the version being removed is the one the symlink resolves to, the
    symlink is moved to the newest remaining version rather than left dangling —
    a dangling ``blade-ai`` on PATH is worse than either outcome, because it also
    takes away the ``update`` that could repair it.
    """
    versions_dir = _expand(VERSIONS_DIR)

    # The value lands in a path that gets rmtree'd, so reject anything that is
    # not a plain version number. Measured before this check: ``--version
    # 0.6.0/../../memory`` resolved out of versions/ and deleted the task
    # records, exiting 0 as if it had removed a version.
    if not _VERSION_ARG_RE.match(version):
        typer.echo(
            f"Invalid version {version!r} — expected a plain version like 0.6.0.",
            err=True,
        )
        raise typer.Exit(1)

    target = versions_dir / f"blade-ai-v{version}"
    installed = _installed_versions()

    if not target.is_dir():
        names = [p.name.removeprefix("blade-ai-v") for p in installed]
        typer.echo(f"v{version} is not installed.", err=True)
        if names:
            typer.echo(f"  Installed: {', '.join('v' + n for n in names)}", err=True)
        else:
            typer.echo(f"  No versions found under {versions_dir}", err=True)
        raise typer.Exit(1)

    active = _active_version_dir()
    is_active = active is not None and active == target
    remaining = [p for p in installed if p != target]

    if is_active and not remaining:
        # Nothing left to point the symlink at, so this is a full uninstall in
        # disguise. Say so instead of silently leaving a broken command.
        typer.echo(
            f"v{version} is the only installed version and is currently in use.\n"
            "  Removing it uninstalls blade-ai entirely — run 'blade-ai "
            "uninstall' for that\n"
            "  (it also cleans up the symlink and PATH entry).",
            err=True,
        )
        raise typer.Exit(1)

    if not force:
        typer.echo(f"Will remove version v{version} ({target})")
        if is_active:
            newest = remaining[-1].name.removeprefix("blade-ai-v")
            typer.echo(f"  This version is IN USE — will switch to v{newest}")
        elif not remaining:
            # Not active only because the symlink is missing or points
            # elsewhere. Removing it still leaves no binary at all.
            typer.echo(
                "  This is the LAST installed version — no binary will remain."
            )
        if _running_from(target):
            typer.echo(
                "  This is the version you are running right now — the current "
                "process keeps\n"
                "  working, but use a fresh 'blade-ai' afterwards."
            )
        if typer.prompt("Proceed? [y/N]", default="n").lower() != "y":
            typer.echo("Cancelled.")
            raise typer.Exit(0)

    if is_active:
        # Repoint BEFORE deleting, and do it atomically: ``unlink`` then
        # ``symlink_to`` has a window with nothing on PATH, and a failure in the
        # second call would remove the command while both versions still exist.
        newest = remaining[-1]
        symlink = _expand(SYMLINK_PATH)
        staged = symlink.with_name(f".blade-ai.new-{os.getpid()}")
        try:
            staged.unlink(missing_ok=True)
            staged.symlink_to(newest / "blade-ai")
            os.replace(staged, symlink)
            typer.echo(
                f"✓ Switched to v{newest.name.removeprefix('blade-ai-v')}"
            )
        except OSError as exc:
            staged.unlink(missing_ok=True)
            typer.echo(f"✗ Could not repoint symlink: {exc}", err=True)
            raise typer.Exit(1) from exc

    running_self = _running_from(target)
    try:
        shutil.rmtree(target)
        typer.echo(f"✓ Removed v{version}")
        if running_self:
            typer.echo(
                "  That was the running version — start a fresh 'blade-ai' "
                "before doing anything else."
            )
    except OSError as exc:
        typer.echo(f"✗ Could not remove {target}: {exc}", err=True)
        raise typer.Exit(1) from exc

    # Removing the last version leaves config and the PATH entry behind with no
    # binary to run — a state worth naming, since ``--force`` shows no prompt.
    if not remaining:
        typer.echo(
            "\nNo versions remain. Config and the PATH entry are still in place;\n"
            "  'blade-ai uninstall' removes those too, or reinstall to get a "
            "binary back."
        )


def uninstall_command(
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt"),
    purge: bool = typer.Option(
        False, "--purge",
        help="Also delete ~/.blade-ai/ (config, task records, postmortems)",
    ),
    version: str = typer.Option(
        None, "--version", "-v",
        help="Remove only this version, keeping the rest of the install",
    ),
) -> None:
    """Uninstall blade-ai from the system.

    Removes every installed version, the PATH symlink and the shell PATH entry.
    ``~/.blade-ai/`` is KEPT unless ``--purge`` is given — it holds config, task
    records and postmortems, and unlike the binaries none of that can be
    downloaded again. Reinstalling picks up where you left off.

    With ``--version``, removes just that one version and leaves everything else
    alone — the way to reclaim space from versions ``update`` accumulated.
    """
    if version is not None:
        _remove_one_version(version, force)
        return

    versions = _installed_versions()
    symlink = _expand(SYMLINK_PATH)
    config_dir = _expand(CONFIG_DIR)
    profiles = _profiles_with_path_entry()
    # Read this BEFORE the symlink is removed — it is the only record of where a
    # ``--prefix`` install actually lives.
    external = _out_of_tree_install()

    # Nothing to do is not a failure — say so and leave a working system alone.
    # ``profiles`` counts: a PATH line is a leftover in its own right, and when
    # the versions dir and symlink were already gone by hand, claiming "nothing to
    # uninstall" left that line in the user's shell config permanently.
    if not versions and not symlink.exists() and not symlink.is_symlink() \
            and not config_dir.exists() and not profiles:
        typer.echo(
            "Nothing to uninstall: no standalone install found.\n"
            "If you installed via pip, use: pip uninstall blade-ai"
        )
        raise typer.Exit(0)

    if not force:
        typer.echo("Will uninstall blade-ai:")
        if versions:
            typer.echo(f"  Versions:   {len(versions)} in {_expand(VERSIONS_DIR)}")
            for path in versions:
                typer.echo(f"                · {path.name}")
        if symlink.is_symlink() or symlink.exists():
            typer.echo(f"  Symlink:    {symlink}")
        if external is not None:
            typer.echo(
                f"  Binary dir: {external}  ← custom --prefix, NOT removed"
            )
        if profiles:
            typer.echo(f"  PATH entry: {', '.join(str(p) for p in profiles)}")
        # Name the irreversible part explicitly. Binaries and PATH entries can be
        # restored by reinstalling; task records and postmortems cannot.
        if purge:
            typer.echo(f"  Config:     {config_dir}  ← WILL BE DELETED")
        else:
            typer.echo(f"  Config:     {config_dir}  (kept — use --purge to delete)")
        if typer.prompt("Proceed? [y/N]", default="n").lower() != "y":
            typer.echo("Cancelled.")
            raise typer.Exit(0)

    # 1. Symlink first: drop the PATH entry before the target disappears, so an
    #    interrupted uninstall never leaves a symlink dangling into nothing.
    if symlink.is_symlink() or symlink.exists():
        try:
            symlink.unlink()
            typer.echo(f"✓ Removed symlink: {symlink}")
        except OSError as exc:
            typer.echo(f"⚠ Could not remove symlink {symlink}: {exc}", err=True)

    # 2. Every installed version, not just the one a manifest happened to name.
    for path in versions:
        try:
            shutil.rmtree(path)
            typer.echo(f"✓ Removed version: {path.name}")
        except OSError as exc:
            typer.echo(f"⚠ Could not remove {path}: {exc}", err=True)

    # 3. Shell PATH lines.
    for path in _strip_path_lines(profiles):
        typer.echo(f"✓ Cleaned PATH from: {path}")

    # 4. Windows user PATH.
    if platform.system() == "Windows":
        if _remove_windows_path_entry(str(_expand(VERSIONS_DIR))):
            typer.echo("✓ Cleaned PATH from Windows registry")

    # 5. Config directory — only on explicit request. Everything above can be
    #    restored by reinstalling; this cannot, so it does not happen by default.
    if purge:
        if config_dir.exists():
            try:
                shutil.rmtree(config_dir)
                typer.echo(f"✓ Purged {config_dir}")
            except OSError as exc:
                typer.echo(f"⚠ Could not remove {config_dir}: {exc}", err=True)
    elif config_dir.exists():
        typer.echo(f"✓ Kept {config_dir} (use --purge to delete it)")

    typer.echo("")
    typer.echo("✨ blade-ai has been uninstalled.")
    if external is not None:
        # Do not claim a clean sweep that did not happen.
        typer.echo(
            f"  Note: this install lives outside {_expand(VERSIONS_DIR)} "
            "(custom --prefix).\n"
            f"  Its files are still at {external} — remove that directory "
            "yourself if it holds nothing else."
        )
    typer.echo("Restart your terminal to apply PATH changes.")
