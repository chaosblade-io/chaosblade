# blade-ai Windows Installer — PowerShell one-click installation script.
#
# Usage:
#   irm https://chaosblade.io/install-agent.ps1 | iex
#   irm https://chaosblade.io/install-agent.ps1 | iex; Install-BladeAI -Version "0.1.0"
#
# Environment variables:
#   BLADE_AI_VERSION       — override the version to install (default: hardcoded below)
#   BLADE_AI_INSTALL_DIR   — override the installation directory
#   BLADE_AI_MIRROR        — override the download base URL
#   BLADE_AI_SKIP_VERIFY   — set to "1" to skip SHA256 verification (NOT recommended)
#
# Note: ``BLADE_AI_VERSION`` accepts the bare semver (``0.1.0``); the
# script internally prefixes it with ``blade-ai-v`` to match the
# chaosblade monorepo's tag scheme.

$ErrorActionPreference = "Stop"
$ToolName = "blade-ai"
$Version = if ($env:BLADE_AI_VERSION) { $env:BLADE_AI_VERSION } else { "0.1.0" }
# Tag namespace: chaosblade monorepo hosts blade-ai under the
# ``blade-ai-v*`` tag namespace so it doesn't collide with chaosblade's
# own ``v*`` tags. Bump this prefix together with
# ``release-blade-ai.yml``'s trigger.
$Tag = "blade-ai-v$Version"
$SkipVerify = if ($env:BLADE_AI_SKIP_VERIFY -eq "1") { $true } else { $false }

# ── Force TLS 1.2 ──────────────────────────────────────────────────────────────
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072

# ── Color helpers ──────────────────────────────────────────────────────────────
function Write-Step($msg) { Write-Host "  ▸ $msg" -ForegroundColor Blue }
function Write-Ok($msg)   { Write-Host "  ✓ $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  ⚠ $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "  ✗ $msg" -ForegroundColor Red; exit 1 }

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