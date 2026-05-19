"""blade-ai update command: self-update via re-running the install script."""

import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

import typer


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
        raise typer.BadParameter(f"Unsupported OS: {os_name}")


def _read_receipt() -> dict | None:
    """Read the install receipt from ~/.blade-ai/receipt.json."""
    receipt_path = Path.home() / ".blade-ai" / "receipt.json"
    if not receipt_path.exists():
        return None
    try:
        return json.loads(receipt_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _is_standalone_install() -> bool:
    """Check if blade-ai was installed via curl|bash (standalone), not pip."""
    receipt = _read_receipt()
    if receipt is None:
        return False
    return receipt.get("source", "") in ("curl-bash", "irm-iex")


def _fetch_latest_version() -> str | None:
    """Fetch the latest release version from GitHub Releases API."""
    import urllib.request
    import urllib.error

    url = "https://api.github.com/repos/chaosblade-io/blade-ai/releases/latest"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "blade-ai-update"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            tag = data.get("tag_name", "")
            # Strip leading 'v' if present
            return tag.lstrip("v")
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


def _download_and_install(version: str, platform_str: str) -> bool:
    """Download the tar.gz for the given version + platform and install it."""
    import urllib.request
    import urllib.error

    base_url = os.environ.get(
        "BLADE_AI_MIRROR",
        "https://github.com/chaosblade-io/blade-ai/releases/download",
    )
    tag = f"v{version}"
    is_windows = platform_str.startswith("windows")
    ext = "zip" if is_windows else "tar.gz"
    download_url = f"{base_url}/{tag}/blade-ai-{platform_str}.{ext}"

    versions_dir = Path.home() / ".blade-ai" / "versions"
    final_dir = versions_dir / tag  # e.g. versions/v0.1.0
    tmp_dir = Path(tempfile.mkdtemp(prefix=f".tmp-{tag}-", dir=str(versions_dir)))

    try:
        # Download
        archive_path = tmp_dir / f"blade-ai.{ext}"
        urllib.request.urlretrieve(download_url, str(archive_path))

        # Extract
        if is_windows:
            import zipfile
            with zipfile.ZipFile(str(archive_path)) as zf:
                zf.extractall(str(tmp_dir))
        else:
            subprocess.run(
                ["tar", "--strip-components=1", "-xzf", str(archive_path), "-C", str(tmp_dir)],
                check=True,
            )
        archive_path.unlink(missing_ok=True)

        # Atomic move to final directory
        if final_dir.exists():
            shutil.rmtree(final_dir)
        tmp_dir.rename(final_dir)

        # Update symlink
        symlink_dir = Path.home() / ".local" / "bin"
        symlink_dir.mkdir(parents=True, exist_ok=True)
        symlink_path = symlink_dir / "blade-ai"
        if symlink_path.is_symlink() or symlink_path.exists():
            symlink_path.unlink()
        symlink_path.symlink_to(final_dir / "blade-ai")

        # Update receipt
        receipt_dir = Path.home() / ".blade-ai"
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = receipt_dir / "receipt.json"
        receipt = {
            "version": version,
            "platform": platform_str,
            "install_dir": str(final_dir),
            "symlink_dir": str(symlink_dir),
            "installed_at": _utc_now(),
            "source": "self-update",
        }
        receipt_path.write_text(json.dumps(receipt, indent=2))

        typer.echo(f"✓ Updated to blade-ai v{version}")
        return True

    except (urllib.error.URLError, subprocess.CalledProcessError, OSError) as e:
        typer.echo(f"✗ Update failed: {e}", err=True)
        # Cleanup tmp dir
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return False


def _utc_now() -> str:
    """Return current UTC time as ISO format string."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def update_command(
    version: str = typer.Option(None, "--version", "-v", help="Specific version to update to"),
    check: bool = typer.Option(False, "--check", help="Only check for updates, don't install"),
) -> None:
    """Update blade-ai to the latest version (standalone install only).

    Only works for curl|bash installed blade-ai. For pip installs, use:
      pip install --upgrade blade-ai
    """
    # Check install method
    if not _is_standalone_install():
        receipt = _read_receipt()
        if receipt is None:
            typer.echo(
                "No install receipt found. This command only works for "
                "curl|bash installed blade-ai.\n"
                "For pip installs, use: pip install --upgrade blade-ai"
            )
        else:
            typer.echo(
                f"Current install source: {receipt.get('source', 'unknown')}\n"
                "This command only works for curl|bash installed blade-ai.\n"
                "For pip installs, use: pip install --upgrade blade-ai"
            )
        raise typer.Exit(1)

    current_receipt = _read_receipt()
    current_version = current_receipt.get("version", "unknown")
    typer.echo(f"Current version: {current_version}")

    # Determine target version
    target_version = version
    if target_version is None:
        typer.echo("Checking for latest version...")
        target_version = _fetch_latest_version()
        if target_version is None:
            typer.echo("✗ Could not fetch latest version from GitHub", err=True)
            raise typer.Exit(1)

    if target_version == current_version:
        typer.echo(f"Already on the latest version: v{current_version}")
        raise typer.Exit(0)

    typer.echo(f"Latest version: {target_version}")

    if check:
        typer.echo(f"Update available: v{current_version} → v{target_version}")
        raise typer.Exit(0)

    # Perform update
    platform_str = _detect_platform()
    success = _download_and_install(target_version, platform_str)
    if not success:
        raise typer.Exit(1)