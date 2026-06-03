# blade-ai Windows Installer — PowerShell one-click installation script.
#
# Usage:
#   # Latest release (default — resolved from GitHub API):
#   irm https://chaosblade.io/install-agent.ps1 | iex
#
#   # Pin a specific version (download then call with -Version):
#   irm https://chaosblade.io/install-agent.ps1 -OutFile install.ps1
#   .\install.ps1 -Version 0.1.0
#
#   # Or via env var (works with irm | iex):
#   $env:BLADE_AI_VERSION = "0.1.0"
#   irm https://chaosblade.io/install-agent.ps1 | iex
#
# Parameters / environment:
#   -Version VERSION       Pin a specific version (bare semver, e.g. 0.1.0).
#                          If omitted AND $env:BLADE_AI_VERSION is unset,
#                          the script queries GitHub for the latest
#                          ``blade-ai-v*`` release.
#   BLADE_AI_VERSION       Same as -Version (env var form).
#   BLADE_AI_INSTALL_DIR   Override the install directory.
#   BLADE_AI_MIRROR        Override the download base URL (GitHub Releases).
#   BLADE_AI_MIRROR_API    Override the GitHub API endpoint used by
#                          latest-version resolution (rare; for testing).
#   BLADE_AI_SKIP_VERIFY   Set to "1" to skip SHA256 verification (NOT recommended).
#
# Note: ``BLADE_AI_VERSION`` / ``-Version`` accept bare semver
# (``0.1.0``); the script internally prefixes with ``blade-ai-v`` to
# match the chaosblade monorepo's tag scheme.

[CmdletBinding()]
param(
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"

# ── Windows not yet supported ──────────────────────────────────────────────────
#
# The release-blade-ai.yml build matrix currently ships only
# linux-{amd64,arm64} and darwin-{amd64,arm64} tarballs — there is no
# blade-ai-windows-x64.zip published for any tag. Running the rest of
# this script would just fetch a 404 and corrupt the user's terminal
# with red errors. Bail out early with an explicit message until a
# Windows matrix entry lands.
#
# Track restoration in:
#   chaosblade/.github/workflows/release-blade-ai.yml (build matrix)
#   chaosblade/blade-ai/blade-ai.spec  (Windows codepath audit)
Write-Host ""
Write-Host "  ✗ blade-ai does not yet ship a Windows binary." -ForegroundColor Red
Write-Host "    Linux / macOS users: use the bash installer instead:" -ForegroundColor Yellow
Write-Host "      curl -fsSL https://chaosblade.io/install-agent.sh | bash" -ForegroundColor Yellow
Write-Host "    Windows users: please track" -ForegroundColor Yellow
Write-Host "      https://github.com/chaosblade-io/chaosblade/issues" -ForegroundColor Yellow
Write-Host "    for Windows support, or build from source via WSL2." -ForegroundColor Yellow
Write-Host ""
exit 1

$ToolName = "blade-ai"

# Version resolution order:
#   1. -Version param (highest precedence)
#   2. $env:BLADE_AI_VERSION
#   3. GitHub API: latest blade-ai-v* release (deferred — see
#      Resolve-LatestVersion call below; runs after color helpers
#      so we can show a progress line).
#
# Tag is computed AFTER the version is finalized.
if (-not $Version) {
    $Version = $env:BLADE_AI_VERSION
}
$Tag = ""  # set after resolution
$SkipVerify = if ($env:BLADE_AI_SKIP_VERIFY -eq "1") { $true } else { $false }
$ReleasesApi = if ($env:BLADE_AI_MIRROR_API) { $env:BLADE_AI_MIRROR_API } else {
    "https://api.github.com/repos/chaosblade-io/chaosblade/releases"
}

# ── Force TLS 1.2 ──────────────────────────────────────────────────────────────
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072

# ── Color helpers ──────────────────────────────────────────────────────────────
function Write-Step($msg) { Write-Host "  ▸ $msg" -ForegroundColor Blue }
function Write-Ok($msg)   { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  ⚠ $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "  ✗ $msg" -ForegroundColor Red; exit 1 }

# ── Resolve latest version from GitHub API ─────────────────────────────────────
#
# Mirrors install.sh's resolve_latest_version: list the chaosblade
# monorepo's releases, filter by the ``blade-ai-v`` tag prefix, drop
# drafts and prereleases, then sort by semver descending and return
# the top tag's bare semver (e.g. ``0.3.0``).
#
# We can't hit ``/releases/latest`` because that returns chaosblade's
# OWN top tag (``v1.x.x``), not our blade-ai sub-stream — so we paginate
# /releases and filter client-side. Releases per call are bounded by
# ``per_page=100``; blade-ai cadence is slow so one page is plenty.
#
# Returns $null on any failure (no network, malformed response, no
# matching releases). Caller decides how to react.
function Resolve-LatestVersion {
    $url = "${ReleasesApi}?per_page=100"
    $releases = $null
    try {
        # -UseBasicParsing keeps us off IE engine on Win PowerShell 5.1
        # where the parsed-DOM path can throw on certain user profiles.
        # -TimeoutSec 15 is generous; the API is cheap and small.
        $releases = Invoke-RestMethod -Uri $url -UseBasicParsing -TimeoutSec 15 `
                        -Headers @{ "Accept" = "application/vnd.github+json" }
    } catch {
        return $null
    }

    if (-not $releases) { return $null }

    $prefix = "blade-ai-v"
    $candidates = New-Object System.Collections.ArrayList
    foreach ($r in $releases) {
        if ($r.draft -or $r.prerelease) { continue }
        $tag = [string]$r.tag_name
        if (-not $tag.StartsWith($prefix)) { continue }
        $semver = $tag.Substring($prefix.Length)

        # Build a [Version] for sorting. [Version] only accepts plain
        # ``A.B[.C[.D]]`` numeric forms — strip non-numeric suffixes
        # (e.g. ``-rc.1``, ``+build.42``) by walking the pieces.
        # Pre-release filter above usually catches RCs but be defensive.
        $nums = New-Object System.Collections.ArrayList
        foreach ($part in [regex]::Split($semver, "[.\-+]")) {
            if ($part -match '^\d+$') {
                [void]$nums.Add([int]$part)
            } else {
                break
            }
        }
        if ($nums.Count -eq 0) { continue }
        # [Version] requires at least 2 components; pad with zero.
        while ($nums.Count -lt 2) { [void]$nums.Add(0) }
        $vstr = ($nums -join ".")
        try {
            $v = [Version]$vstr
            [void]$candidates.Add(([PSCustomObject]@{ Version = $v; Semver = $semver }))
        } catch {
            # Unparseable; skip this entry rather than abort.
        }
    }

    if ($candidates.Count -eq 0) { return $null }

    $sorted = $candidates | Sort-Object Version -Descending
    return $sorted[0].Semver
}

# ── Architecture detection ──────────────────────────────────────────────────────
# Note: On ARM64 Windows running x64 PowerShell via emulation,
# $env:PROCESSOR_ARCHITECTURE returns "AMD64" but
# $env:PROCESSOR_ARCHITEW6432 returns "ARM64".
$Arch = if ([System.Environment]::Is64BitOperatingSystem) {
    if ($env:PROCESSOR_ARCHITEW6432 -eq "ARM64" -or $env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "x64" }
} else {
    Write-Err "32-bit Windows is not supported"
}

Write-Step "Detecting system architecture..."
$Platform = "windows-$Arch"
Write-Ok "Detected $Platform"

# ── Resolve version (lazy) ─────────────────────────────────────────────────────
# At this point either:
#   - $Version was set via -Version / $env:BLADE_AI_VERSION (use as-is), OR
#   - $Version is empty → query GitHub API for the latest blade-ai-v* release.
# Doing this AFTER arch detection means failures interleave with the
# rest of the progressive UI rather than printing before the banner.
if (-not $Version) {
    Write-Step "Resolving latest blade-ai version from GitHub..."
    $Version = Resolve-LatestVersion
    if (-not $Version) {
        Write-Err ("Could not resolve the latest version. Reasons: no network, " +
                   "GitHub API rate-limit, or no published blade-ai-v* release yet. " +
                   "Pin a version explicitly: -Version 0.1.0 or `$env:BLADE_AI_VERSION = '0.1.0'.")
    }
    Write-Ok "Resolved latest: $Version"
}
# Tag namespace: chaosblade monorepo hosts blade-ai under
# ``blade-ai-v*`` so it doesn't collide with chaosblade's own
# ``v*`` tags. Bump together with release-blade-ai.yml's trigger.
$Tag = "blade-ai-v$Version"

# ── Determine install directory ────────────────────────────────────────────────
$DefaultInstallDir = "$env:LOCALAPPDATA\Programs\$ToolName"
$InstallDir = if ($env:BLADE_AI_INSTALL_DIR) { $env:BLADE_AI_INSTALL_DIR } else { $DefaultInstallDir }
$ReceiptDir = "$env:USERPROFILE\.blade-ai"

# ── Download URL construction ──────────────────────────────────────────────────
$BaseUrl = if ($env:BLADE_AI_MIRROR) { $env:BLADE_AI_MIRROR } else { "https://github.com/chaosblade-io/chaosblade/releases/download" }
$DownloadUrl = "$BaseUrl/$Tag/blade-ai-$Platform.zip"
$ChecksumUrl = "$BaseUrl/$Tag/checksums.txt"

Write-Step "Downloading $ToolName $Version..."
Write-Host "  URL: $DownloadUrl" -ForegroundColor Gray

# ── Download ────────────────────────────────────────────────────────────────────
$TempDir = Join-Path $env:TEMP "blade-ai-install-$Version-$([guid]::NewGuid().ToString('N').Substring(0,8))"
New-Item -ItemType Directory -Path $TempDir -Force | Out-Null

$TempZip = Join-Path $TempDir "blade-ai.zip"
try {
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $TempZip -UseBasicParsing
    Write-Ok "Download complete"
} catch {
    Write-Err "Download failed: $_"
}

# ── SHA256 checksum verification ──────────────────────────────────────────────
if (-not $SkipVerify) {
    Write-Step "Verifying checksum..."

    $TempChecksum = Join-Path $TempDir "checksums.txt"
    try {
        Invoke-WebRequest -Uri $ChecksumUrl -OutFile $TempChecksum -UseBasicParsing
    } catch {
        Write-Warn "Checksum file not available; skipping verification"
        $SkipVerify = $true
    }

    if (-not $SkipVerify) {
        $ChecksumContent = Get-Content $TempChecksum -Raw
        $ExpectedHash = ($ChecksumContent | Select-String "blade-ai-$Platform.zip" | ForEach-Object {
            if ($_.Line -match '^\s*([a-fA-F0-9]{64})') { $matches[1] }
        })

        if (-not $ExpectedHash) {
            Write-Err "No checksum entry found for blade-ai-$Platform.zip"
        }

        $ActualHash = (Get-FileHash -Path $TempZip -Algorithm SHA256).Hash.ToLower()
        $ExpectedHash = $ExpectedHash.ToLower()

        if ($ActualHash -ne $ExpectedHash) {
            Write-Err "Checksum mismatch!`n  Expected: $ExpectedHash`n  Actual:   $ActualHash"
        }

        Write-Ok "Checksum verified"
    }
}

# ── Extract ────────────────────────────────────────────────────────────────────
Write-Step "Extracting package..."

try {
    Expand-Archive -Path $TempZip -DestinationPath $InstallDir -Force
    Write-Ok "Extraction complete"
} catch {
    Write-Err "Extraction failed: $_"
}

# ── Write receipt ──────────────────────────────────────────────────────────────
New-Item -ItemType Directory -Path $ReceiptDir -Force | Out-Null

$Receipt = @{
    version     = $Version
    platform    = $Platform
    install_dir = $InstallDir
    installed_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    source      = "irm-iex"
} | ConvertTo-Json

$ReceiptPath = Join-Path $ReceiptDir "receipt.json"
Set-Content -Path $ReceiptPath -Value $Receipt

$Manifest = @{
    version        = $Version
    platform       = $Platform
    install_dir    = $InstallDir
    receipt        = $ReceiptPath
    modified_files = @()
} | ConvertTo-Json

$ManifestPath = Join-Path $ReceiptDir "install-manifest.json"
Set-Content -Path $ManifestPath -Value $Manifest

# ── PATH configuration ────────────────────────────────────────────────────────
Write-Step "Configuring PATH..."

$UserPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$InstallDir*") {
    $NewPath = "$UserPath;$InstallDir"
    [System.Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
    # Update current session PATH too
    $env:Path = "$env:Path;$InstallDir"

    # Update manifest
    $ManifestObj = Get-Content $ManifestPath | ConvertFrom-Json
    $ManifestObj.modified_files = @("User:Path")
    $ManifestObj | ConvertTo-Json | Set-Content $ManifestPath

    Write-Ok "PATH configured (User-level)"
} else {
    Write-Ok "PATH already configured"
}

# ── Cleanup ────────────────────────────────────────────────────────────────────
Remove-Item -Recurse -Force $TempDir

# ── Success message ────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ✨ blade-ai $Version installed!" -ForegroundColor Green
Write-Host ""

$BladeAiExe = Join-Path $InstallDir "blade-ai.exe"
if (Test-Path $BladeAiExe) {
    Write-Host "  Start using blade-ai:" -ForegroundColor White
    Write-Host "    blade-ai" -ForegroundColor Cyan
} else {
    Write-Host "  Start a new terminal, then run:" -ForegroundColor White
    Write-Host "    blade-ai" -ForegroundColor Cyan
}

Write-Host ""
Write-Host "  Uninstall: blade-ai uninstall" -ForegroundColor Gray
Write-Host "  Update:    blade-ai update" -ForegroundColor Gray
Write-Host ""