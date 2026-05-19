"""blade-ai uninstall command: remove blade-ai from the system."""

import json
import shutil
from pathlib import Path

import typer


def _read_manifest() -> dict | None:
    """Read the install manifest from ~/.blade-ai/install-manifest.json."""
    manifest_path = Path.home() / ".blade-ai" / "install-manifest.json"
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _remove_path_from_profiles(profiles: list[str], marker: str = "# blade-ai") -> list[str]:
    """Remove blade-ai PATH lines from shell profile files."""
    removed_from = []
    for profile_path in profiles:
        p = Path(profile_path)
        if not p.exists():
            continue
        try:
            lines = p.read_text().splitlines()
            new_lines = [line for line in lines if marker not in line]
            if len(new_lines) < len(lines):
                p.write_text("\n".join(new_lines) + "\n")
                removed_from.append(str(p))
        except OSError:
            pass
    return removed_from


def _remove_windows_path(install_dir: str) -> bool:
    """Remove install_dir from Windows User PATH."""
    import platform
    if platform.system() != "Windows":
        return False

    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE)
        path_value, _ = winreg.QueryValueEx(key, "Path")
        entries = [e for e in path_value.split(";") if install_dir not in e]
        new_path = ";".join(entries)
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def uninstall_command(
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt"),
    keep_config: bool = typer.Option(False, "--keep-config", help="Keep ~/.blade-ai/ config directory"),
) -> None:
    """Uninstall blade-ai from the system.

    Removes the binary, version directory, symlink, PATH configuration,
    and optionally the config directory (~/.blade-ai/).
    """
    manifest = _read_manifest()
    if manifest is None:
        typer.echo(
            "No install manifest found. Cannot determine install details.\n"
            "If you installed via pip, use: pip uninstall blade-ai"
        )
        raise typer.Exit(1)

    install_dir = manifest.get("install_dir", "")
    symlink = manifest.get("symlink", "")
    platform_str = manifest.get("platform", "")

    # Confirmation
    if not force:
        typer.echo("Will uninstall blade-ai:")
        typer.echo(f"  Binary dir: {install_dir}")
        typer.echo(f"  Symlink:    {symlink}")
        if not keep_config:
            typer.echo(f"  Config:     {Path.home() / '.blade-ai'}")
        else:
            typer.echo(f"  Config:     {Path.home() / '.blade-ai'} (KEPT)")
        confirm = typer.prompt("Proceed? [y/N]", default="n")
        if confirm.lower() != "y":
            typer.echo("Cancelled.")
            raise typer.Exit(0)

    # 1. Remove symlink
    if symlink:
        symlink_path = Path(symlink)
        if symlink_path.is_symlink() or symlink_path.exists():
            symlink_path.unlink()
            typer.echo(f"✓ Removed symlink: {symlink}")

    # 2. Remove version directory
    if install_dir:
        install_path = Path(install_dir)
        if install_path.exists():
            shutil.rmtree(install_path)
            typer.echo(f"✓ Removed install dir: {install_dir}")

    # 3. Remove PATH from shell profiles (Unix)
    modified_files = manifest.get("modified_files", [])
    if modified_files:
        removed_from = _remove_path_from_profiles(modified_files)
        if removed_from:
            typer.echo(f"✓ Cleaned PATH from: {', '.join(removed_from)}")

    # 4. Remove from Windows PATH
    if platform_str.startswith("windows"):
        if _remove_windows_path(install_dir):
            typer.echo("✓ Cleaned PATH from Windows registry")

    # 5. Remove config directory (unless --keep-config)
    config_dir = Path.home() / ".blade-ai"
    if not keep_config:
        if config_dir.exists():
            shutil.rmtree(config_dir)
            typer.echo(f"✓ Removed config dir: {config_dir}")
    else:
        # Remove only manifest + receipt, keep config.json, skills, etc.
        for fname in ("install-manifest.json", "receipt.json"):
            fpath = config_dir / fname
            if fpath.exists():
                fpath.unlink()
        typer.echo(f"✓ Kept config dir: {config_dir}")

    typer.echo("")
    typer.echo("✨ blade-ai has been uninstalled.")
    typer.echo("Restart your terminal to apply PATH changes.")