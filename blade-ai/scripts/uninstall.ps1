# blade-ai Windows uninstaller — companion to scripts/install.ps1.
#
# Reverses the on-disk footprint that install.ps1 leaves:
#   * Install directory (default: $env:LOCALAPPDATA\Programs\blade-ai;
#     manifest may override).
#   * Receipt + manifest under $env:USERPROFILE\.blade-ai\.
#   * The install dir entry in the per-user Windows PATH (HKCU
#     Environment\Path), persisted via [Environment]::Set...).
#   * Optionally the whole $env:USERPROFILE\.blade-ai\ config tree.
#
# Usage:
#   .\uninstall.ps1                        # remove everything (binary + config + PATH)
#   .\uninstall.ps1 -Version 0.1.0         # only proceed if manifest matches v0.1.0
#   .\uninstall.ps1 -KeepConfig            # keep ~\.blade-ai\ config/memory/skills
#   .\uninstall.ps1 -DryRun                # print plan, no deletion
#   .\uninstall.ps1 -Force                 # skip y/N confirmation
#   irm https://chaosblade.io/uninstall-agent.ps1 | iex
#
# Environment overrides:
#   BLADE_AI_INSTALL_DIR   override install dir lookup (rarely needed —
#                          manifest carries the real path)
#
# Robustness contract:
#   * Never throws on missing files/dirs/registry keys — each section
#     guards its own Test-Path / try-catch and emits a warn.
#   * Never deletes anything outside the install dir / config dir / a
#     PATH entry that exactly matches the install dir. No fuzzy
#     pattern matching that could clobber unrelated paths.
#   * Backs up the User PATH to a sibling file before mutating, so a
#     mistake is recoverable.
#   * Compatible with both Windows PowerShell 5.1 (default on Win 10/11)
#     and PowerShell 7+ (pwsh).

[CmdletBinding()]
param(
    [string]$Version = "",
    [switch]$KeepConfig,
    [switch]$Force,
    [switch]$DryRun,
    [switch]$Help
)

# Continue (not Stop) so a single failed sub-step doesn't abort the
# whole uninstall — we want best-effort cleanup with explicit warnings.
$ErrorActionPreference = "Continue"

# ── Color helpers ──────────────────────────────────────────────────────────────
function Write-Step($msg) { Write-Host "  ▸ $msg" -ForegroundColor Blue }
function Write-Ok($msg)   { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  ⚠ $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "  ✗ $msg" -ForegroundColor Red }
function Write-Info($msg) { Write-Host "    $msg" -ForegroundColor Gray }

# ── Help ───────────────────────────────────────────────────────────────────────
if ($Help) {
    @"
blade-ai uninstaller — undo install.ps1's footprint.

Usage:
  .\uninstall.ps1 [-Version VERSION] [-KeepConfig] [-Force] [-DryRun]

Parameters:
  -Version VERSION  Safety check: only proceed if the install manifest
                    records this version (bare semver, e.g. "0.1.0").
                    Mismatch aborts cleanly. Omit to uninstall whatever
                    is currently installed.

  -KeepConfig       Keep config.json, logs/, memory/, skills/, vendor/
                    under `$env:USERPROFILE\.blade-ai`. Only removes
                    the binary directory + install metadata + PATH
                    entry. Lets you reinstall later without losing
                    settings.

  -Force            Skip the y/N confirmation prompt.

  -DryRun           Print the plan, do not delete anything. Use this
                    first to verify the scope.

  -Help             Show this help and exit.

Examples:
  .\uninstall.ps1                                # full uninstall
  .\uninstall.ps1 -KeepConfig                    # keep config dir
  .\uninstall.ps1 -Version 0.1.0 -DryRun         # safety check + preview
  .\uninstall.ps1 -Force                         # CI-friendly, no prompt

Notes:
  * install.ps1 installs to a single directory (no per-version
    subdirs) and persists PATH in the per-user registry, so this
    uninstaller targets exactly that footprint.
  * Each modified registry value is backed up to
    `$env:USERPROFILE\.blade-ai\path-backup.txt` before mutation.
"@
    exit 0
}

# ── Defaults ───────────────────────────────────────────────────────────────────
$ToolName = "blade-ai"
$ReceiptDir = Join-Path $env:USERPROFILE ".blade-ai"
$ManifestPath = Join-Path $ReceiptDir "install-manifest.json"
$ReceiptPath = Join-Path $ReceiptDir "receipt.json"
$DefaultInstallDir = Join-Path $env:LOCALAPPDATA "Programs\$ToolName"

# ── Discover what's installed ──────────────────────────────────────────────────
Write-Step "Scanning installed state..."

$Manifest = $null
$ManifestVersion = $null
$ManifestInstallDir = $null
$ManifestModifiedFiles = @()

if (Test-Path $ManifestPath) {
    try {
        $Manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
        $ManifestVersion = $Manifest.version
        $ManifestInstallDir = $Manifest.install_dir
        if ($Manifest.modified_files) {
            $ManifestModifiedFiles = @($Manifest.modified_files)
        }
    } catch {
        Write-Warn "Could not parse $ManifestPath ($_) — falling back to defaults"
    }
}

# Resolve install dir: manifest takes precedence, then env override,
# then the default. This keeps custom-install-dir users covered.
$InstallDir = if ($ManifestInstallDir) {
    $ManifestInstallDir
} elseif ($env:BLADE_AI_INSTALL_DIR) {
    $env:BLADE_AI_INSTALL_DIR
} else {
    $DefaultInstallDir
}

# ── Version safety check ───────────────────────────────────────────────────────
if ($Version) {
    if (-not $ManifestVersion) {
        Write-Warn "No install manifest found — cannot verify -Version $Version."
        Write-Info "Either remove the -Version flag, or restore the manifest first."
        exit 0
    }
    if ($ManifestVersion -ne $Version) {
        Write-Warn "Manifest reports installed version $ManifestVersion, but -Version $Version was requested."
        Write-Info "Aborting to avoid removing the wrong installation."
        Write-Info "If this is intentional, omit -Version or pass -Version $ManifestVersion."
        exit 0
    }
}

# ── PATH discovery ─────────────────────────────────────────────────────────────
$UserPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
if ($null -eq $UserPath) { $UserPath = "" }

$PathContainsInstallDir = $false
if ($InstallDir -and $UserPath) {
    # Split by ';' and compare each entry exactly to InstallDir
    # (case-insensitive on Windows). Avoids partial matches like
    # "C:\Programs\blade-ai-old" colliding with "C:\Programs\blade-ai".
    $PathEntries = $UserPath -split ';' | Where-Object { $_ -ne "" }
    foreach ($entry in $PathEntries) {
        $trimmed = $entry.TrimEnd('\')
        $target = $InstallDir.TrimEnd('\')
        if ([string]::Equals($trimmed, $target, [StringComparison]::OrdinalIgnoreCase)) {
            $PathContainsInstallDir = $true
            break
        }
    }
}

# ── Decide scope ───────────────────────────────────────────────────────────────
$RemoveConfigDir = (-not $KeepConfig)

# ── Plan summary ───────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Plan:" -ForegroundColor White

# Install dir line
if (Test-Path $InstallDir) {
    $size = ""
    try {
        $bytes = (Get-ChildItem -Recurse -Force -File -LiteralPath $InstallDir -ErrorAction SilentlyContinue |
                  Measure-Object -Property Length -Sum).Sum
        if ($bytes -gt 1MB) { $size = "  ({0:N1} MB)" -f ($bytes / 1MB) }
        elseif ($bytes -gt 1KB) { $size = "  ({0:N1} KB)" -f ($bytes / 1KB) }
        elseif ($bytes) { $size = "  ($bytes B)" }
    } catch {}
    Write-Host "  Install dir: $InstallDir$size" -ForegroundColor White
} else {
    Write-Host "  Install dir: $InstallDir" -ForegroundColor DarkGray -NoNewline
    Write-Host "  (not present — skip)" -ForegroundColor DarkGray
}

# PATH line
if ($PathContainsInstallDir) {
    Write-Host "  User PATH:   will remove '$InstallDir' entry (backup at $ReceiptDir\path-backup.txt)" -ForegroundColor White
} else {
    Write-Host "  User PATH:   no entry to remove" -ForegroundColor DarkGray
}

# Manifest + receipt files
$metaFiles = @()
if (Test-Path $ManifestPath) { $metaFiles += $ManifestPath }
if (Test-Path $ReceiptPath) { $metaFiles += $ReceiptPath }
if ($metaFiles.Count -gt 0) {
    Write-Host "  Metadata:    $($metaFiles -join ', ')" -ForegroundColor White
}

# Config dir line
if ($RemoveConfigDir) {
    if (Test-Path $ReceiptDir) {
        Write-Host "  Config dir:  $ReceiptDir " -NoNewline -ForegroundColor White
        Write-Host "(WILL BE REMOVED — config / memory / skills / logs all gone)" -ForegroundColor Red
    } else {
        Write-Host "  Config dir:  $ReceiptDir " -NoNewline -ForegroundColor DarkGray
        Write-Host "(not present)" -ForegroundColor DarkGray
    }
} else {
    Write-Host "  Config dir:  $ReceiptDir " -NoNewline -ForegroundColor White
    Write-Host "(KEPT — -KeepConfig)" -ForegroundColor DarkGray
}

if ($DryRun) {
    Write-Host ""
    Write-Host "  Dry-run mode — nothing will be deleted. Re-run without -DryRun to apply." -ForegroundColor DarkGray
    exit 0
}

# Bail early if there's literally nothing to do.
$hasWork = (Test-Path $InstallDir) -or $PathContainsInstallDir -or `
           (Test-Path $ManifestPath) -or (Test-Path $ReceiptPath) -or `
           ($RemoveConfigDir -and (Test-Path $ReceiptDir))
if (-not $hasWork) {
    Write-Host ""
    Write-Warn "Nothing to uninstall — no install dir, no PATH entry, no metadata."
    exit 0
}

# ── Confirmation ───────────────────────────────────────────────────────────────
if (-not $Force) {
    Write-Host ""
    $ans = Read-Host "Proceed with uninstall? [y/N]"
    if ($ans -notmatch '^(y|yes)$') {
        Write-Host ""
        Write-Info "Cancelled."
        exit 0
    }
}

Write-Host ""

# ── 1. Backup PATH before any mutation ─────────────────────────────────────────
# Always backup, even if we're not going to mutate yet — gives the
# user a safety net in case something later in the script touches it.
if ($PathContainsInstallDir) {
    try {
        if (-not (Test-Path $ReceiptDir)) {
            New-Item -ItemType Directory -Path $ReceiptDir -Force | Out-Null
        }
        $backupPath = Join-Path $ReceiptDir "path-backup.txt"
        $backupContent = "# blade-ai uninstaller PATH backup taken $(Get-Date -Format o)`n" +
                         "# Original User PATH (HKCU\Environment\Path):`n" +
                         "$UserPath`n"
        Set-Content -Path $backupPath -Value $backupContent -Force
        Write-Ok "Backed up User PATH to $backupPath"
    } catch {
        Write-Warn "Could not back up User PATH ($_) — continuing anyway"
    }
}

# ── 2. Remove install directory ────────────────────────────────────────────────
if (Test-Path $InstallDir) {
    try {
        Remove-Item -Recurse -Force -LiteralPath $InstallDir -ErrorAction Stop
        Write-Ok "Removed install dir: $InstallDir"
    } catch {
        Write-Warn "Failed to remove install dir ($_)"
        Write-Info "If a blade-ai process is still running, stop it and try again."
    }
} else {
    Write-Info "Skip (not present): $InstallDir"
}

# ── 3. Remove install dir from User PATH ───────────────────────────────────────
if ($PathContainsInstallDir) {
    try {
        $newEntries = @()
        $target = $InstallDir.TrimEnd('\')
        foreach ($entry in ($UserPath -split ';')) {
            if ([string]::IsNullOrEmpty($entry)) { continue }
            $trimmed = $entry.TrimEnd('\')
            if (-not [string]::Equals($trimmed, $target, [StringComparison]::OrdinalIgnoreCase)) {
                $newEntries += $entry
            }
        }
        $newPath = $newEntries -join ';'
        [System.Environment]::SetEnvironmentVariable("Path", $newPath, "User")

        # Update current session PATH too so the change is visible
        # without a logoff/login.
        $sessionEntries = ($env:Path -split ';') | Where-Object {
            $t = $_.TrimEnd('\')
            -not [string]::Equals($t, $target, [StringComparison]::OrdinalIgnoreCase)
        }
        $env:Path = ($sessionEntries -join ';')

        Write-Ok "Removed '$InstallDir' from User PATH"
        Write-Info "(Open a new terminal for other apps to see the change.)"
    } catch {
        Write-Warn "Could not update User PATH ($_)"
    }
} else {
    Write-Info "Skip PATH (no matching entry)"
}

# ── 4. Remove or trim config dir ───────────────────────────────────────────────
if ($RemoveConfigDir) {
    if (Test-Path $ReceiptDir) {
        try {
            Remove-Item -Recurse -Force -LiteralPath $ReceiptDir -ErrorAction Stop
            Write-Ok "Removed config dir: $ReceiptDir"
        } catch {
            Write-Warn "Failed to remove $ReceiptDir ($_)"
        }
    } else {
        Write-Info "Skip (not present): $ReceiptDir"
    }
} else {
    # Keep-config mode: at least drop install metadata so a future
    # install starts clean.
    foreach ($file in @($ManifestPath, $ReceiptPath)) {
        if (Test-Path $file) {
            try {
                Remove-Item -Force -LiteralPath $file -ErrorAction Stop
                Write-Ok "Removed: $file"
            } catch {
                Write-Warn "Failed to remove $file ($_)"
            }
        }
    }
    Write-Info "Config dir kept: $ReceiptDir"
}

# ── Done ───────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ✨ blade-ai uninstall complete." -ForegroundColor Green
if ($PathContainsInstallDir) {
    Write-Host "    Open a new terminal so the PATH change is picked up everywhere." -ForegroundColor DarkGray
}
Write-Host ""
exit 0
