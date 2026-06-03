#!/usr/bin/env bash
# shellcheck shell=bash
#
# blade-ai installer — curl | bash one-click installation script.
#
# Usage:
#   curl -fsSL https://chaosblade.io/install-agent.sh | bash
#   curl -fsSL https://chaosblade.io/install-agent.sh | bash -s -- --version 0.1.0
#   curl -fsSL https://chaosblade.io/install-agent.sh | bash -s -- --prefix /opt/blade-ai
#
# Note: ``--version`` and ``BLADE_AI_VERSION`` accept the bare semver
# (``0.1.0``); the script internally prefixes it with ``blade-ai-v``
# to match the chaosblade monorepo's tag scheme.
#
# Environment variables:
#   BLADE_AI_VERSION           — override the version to install (default: hardcoded below)
#   BLADE_AI_INSTALL_DIR       — override the installation directory
#   BLADE_AI_NO_MODIFY_PATH    — set to 1 to skip shell profile PATH modification
#   BLADE_AI_MIRROR            — override the download base URL (default: GitHub Releases)
#   BLADE_AI_SKIP_VERIFY       — set to 1 to skip SHA256 checksum verification (NOT recommended)

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────────

APP_NAME="blade-ai"
# Empty default → resolved later from GitHub API (latest blade-ai-v*
# release). User can override via ``--version 0.1.0`` or
# ``BLADE_AI_VERSION=0.1.0`` to pin a specific version.
APP_VERSION="${BLADE_AI_VERSION:-}"
# Tag namespace: chaosblade monorepo hosts blade-ai under a prefixed
# tag so it doesn't collide with chaosblade's own ``v*`` tags. Bump
# this prefix together with ``release-blade-ai.yml``'s trigger.
# TAG is computed AFTER version resolution (see below).
TAG=""
RELEASES_API="https://api.github.com/repos/chaosblade-io/chaosblade/releases"
RECEIPT_HOME="$HOME/.blade-ai"
VERSIONS_DIR="$RECEIPT_HOME/versions"
NO_MODIFY_PATH="${BLADE_AI_NO_MODIFY_PATH:-0}"
SKIP_VERIFY="${BLADE_AI_SKIP_VERIFY:-0}"

# ── Re-exec with bash if running under sh / bash-posix ───────────────────────
#
# Users often invoke the script as ``sh install.sh`` which bypasses
# the shebang. The script uses bash-only features (arrays, ``[[ ]]``,
# process substitution); under POSIX /bin/sh it dies at parse time
# with cryptic ``syntax error`` messages.
#
# Subtle case: on macOS, ``/bin/sh`` is actually bash running in POSIX
# mode. ``$BASH_VERSION`` is still set in that mode (because it IS
# bash), so a naive ``[ -z "${BASH_VERSION:-}" ]`` check would miss
# this scenario — the script would keep running with bash-posix and
# still die on bash-only syntax. We additionally check the POSIX mode
# flags (``$POSIXLY_CORRECT`` and ``$SHELLOPTS``) and re-exec via
# plain bash (no --posix) when either is set.
#
# The guard variable prevents an infinite loop if the re-exec itself
# somehow lands back in a constrained shell.
__blade_ai_install_needs_reexec() {
    [ -z "${BASH_VERSION:-}" ] && return 0
    [ -n "${POSIXLY_CORRECT:-}" ] && return 0
    case ":${SHELLOPTS:-}:" in
        *":posix:"*) return 0 ;;
    esac
    return 1
}

if __blade_ai_install_needs_reexec && [ -z "${__BLADE_AI_INSTALL_REEXEC:-}" ]; then
    if command -v bash >/dev/null 2>&1; then
        export __BLADE_AI_INSTALL_REEXEC=1
        exec bash "$0" "$@"
    else
        echo "Error: This script requires bash. Please install bash first." >&2
        exit 1
    fi
fi
unset -f __blade_ai_install_needs_reexec 2>/dev/null || true

# ── Color support ──────────────────────────────────────────────────────────────

if [ -n "${NO_COLOR+x}" ] || [ ! -t 1 ]; then
    CYAN='' GREEN='' YELLOW='' BLUE='' RED='' BOLD='' DIM='' NC=''
else
    CYAN='\033[0;36m'  GREEN='\033[0;32m'  YELLOW='\033[0;33m'
    BLUE='\033[0;34m'   RED='\033[0;31m'    BOLD='\033[1m'
    DIM='\033[2m'       NC='\033[0m'
fi

# ── Logging ────────────────────────────────────────────────────────────────────

step()   { echo -e "${BLUE}▸${NC} ${1}"; }
ok()     { echo -ne "\033[1A\033[2K"; echo -e "${GREEN}✓${NC} ${1}"; }
warn()   { echo -e "${YELLOW}⚠${NC} ${1}"; }
err()    { echo -e "${RED}✗${NC} ${1}" >&2; exit 1; }
info()   { echo -e "${DIM}${1}${NC}"; }

# ── Parse arguments ────────────────────────────────────────────────────────────

FORCE_INSTALL_DIR=""
FORCE_OVERWRITE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            if [[ -z "${2:-}" ]] || [[ "$2" == -* ]]; then err "--version requires a value"; fi
            APP_VERSION="$2"; shift 2 ;;
        --prefix)
            if [[ -z "${2:-}" ]] || [[ "$2" == -* ]]; then err "--prefix requires a value"; fi
            FORCE_INSTALL_DIR="$2"; shift 2 ;;
        --no-modify-path)
            NO_MODIFY_PATH=1; shift ;;
        --skip-verify)
            SKIP_VERIFY=1; warn "Skipping checksum verification"; shift ;;
        --yes|-y)
            FORCE_OVERWRITE=1; shift ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo "  --version VERSION   Install specific version (default: latest blade-ai-v* release"
            echo "                      resolved from GitHub API). Accepts bare semver, e.g. 0.1.0."
            echo "  --prefix PATH       Install to custom directory"
            echo "  --no-modify-path    Don't modify shell profile PATH"
            echo "  --skip-verify       Skip SHA256 verification (NOT recommended)"
            echo "  --yes, -y           Auto-confirm overwriting an existing same-version install"
            echo "                      (no effect when the target dir does not exist)"
            echo "  -h, --help          Show this help"
            echo ""
            echo "Environment variables:"
            echo "  BLADE_AI_VERSION    Same as --version (set to specific semver to pin)"
            echo "  BLADE_AI_MIRROR     Override download base URL"
            echo "  BLADE_AI_INSTALL_DIR  Override install directory"
            echo "  BLADE_AI_FORCE_OVERWRITE  Set to 1 to auto-confirm overwrites (same as --yes)"
            exit 0 ;;
        *) err "Unknown option: $1" ;;
    esac
done

# Env var form for the overwrite override — matches the other
# BLADE_AI_* knobs so Dockerfiles / CI can set it without re-quoting.
if [[ "${BLADE_AI_FORCE_OVERWRITE:-0}" == "1" ]]; then
    FORCE_OVERWRITE=1
fi

# ── Detect download tool ───────────────────────────────────────────────────────

downloader() {
    local _url="$1" _file="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fSL --progress-bar "${_url}" -o "${_file}"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --show-progress -O "${_file}" "${_url}"
    else
        err "Neither curl nor wget found. Install one first."
    fi
}

# Quiet downloader for small JSON fetches (no progress bar, swallow
# errors so the caller can decide how to react).
downloader_quiet() {
    local _url="$1" _file="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "${_url}" -o "${_file}" 2>/dev/null
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "${_file}" "${_url}" 2>/dev/null
    else
        return 1
    fi
}

# Python helper used by resolve_latest_version below. Stored as a
# string constant rather than a heredoc inside the function so we
# don't run into ``$(... <<'PY' ... PY ...)`` parser quirks. The body
# only uses double quotes, so single-quoted assignment preserves it
# verbatim.
_PY_RESOLVE_LATEST='import json, re, sys
try:
    releases = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
PREFIX = "blade-ai-v"
candidates = []
for r in releases:
    if r.get("draft") or r.get("prerelease"):
        continue
    tag = r.get("tag_name", "") or ""
    if not tag.startswith(PREFIX):
        continue
    semver = tag[len(PREFIX):]
    # Build a tuple of leading integer components for sorting; stop at
    # the first non-numeric (so ``0.1.0-rc.1`` becomes (0,1,0) and is
    # ordered consistently before the same line plain ``0.1.0``).
    # Pre-release filtering above usually catches RCs but
    # belt-and-braces here.
    nums = []
    for part in re.split(r"[.\-+]", semver):
        if part.isdigit():
            nums.append(int(part))
        else:
            break
    if nums:
        candidates.append((tuple(nums), semver))
if not candidates:
    sys.exit(1)
candidates.sort(reverse=True)
print(candidates[0][1])
'

# ── Resolve latest version from GitHub API ─────────────────────────────────────
#
# Picks the highest-semver published release whose tag starts with
# ``blade-ai-v``, skipping drafts and prereleases. Used when the user
# does NOT pass --version / BLADE_AI_VERSION.
#
# We can't just hit ``/releases/latest`` because that endpoint returns
# the chaosblade monorepo's own latest tag (``v1.x.x``), not our
# blade-ai sub-stream. Instead we list ``/releases?per_page=100`` and
# filter client-side. blade-ai release cadence is slow; one page is
# plenty.
#
# Outputs the bare semver (e.g. ``0.1.0``) on stdout when successful.
# Returns non-zero on any failure (no network, malformed JSON, no
# matching releases) so the caller can fall back to a clear error.
resolve_latest_version() {
    local _tmp
    _tmp="$(mktemp -t blade-ai-releases.XXXXXX 2>/dev/null || mktemp)" || return 1

    if ! downloader_quiet "${BLADE_AI_MIRROR_API:-${RELEASES_API}}?per_page=100" "${_tmp}"; then
        rm -f "${_tmp}"
        return 1
    fi

    local _version=""
    if command -v python3 >/dev/null 2>&1; then
        # Strict path: parse JSON, filter by tag prefix, sort by
        # semver tuple descending, pick #1. The Python source lives in
        # ${_PY_RESOLVE_LATEST} (defined above) — embedding a heredoc
        # inside ``$(...)`` confuses some bash parsers, so we pass the
        # script via -c instead.
        _version="$(python3 -c "${_PY_RESOLVE_LATEST}" "${_tmp}" 2>/dev/null)"
    fi

    # Fallback path: no python3. The GitHub API returns releases in
    # creation-time descending order, so the first ``"tag_name":
    # "blade-ai-v..."`` line in the body is usually the latest. This
    # CAN pick a prerelease — caller is expected to verify via the
    # later download attempt.
    if [[ -z "${_version}" ]]; then
        _version="$(grep -oE '"tag_name":[[:space:]]*"blade-ai-v[^"]+"' "${_tmp}" 2>/dev/null \
                    | sed -E 's/.*"blade-ai-v([^"]+)".*/\1/' \
                    | head -1)"
    fi

    rm -f "${_tmp}"

    [[ -n "${_version}" ]] || return 1
    echo "${_version}"
}

# Resolve APP_VERSION now (if user didn't pin one) and derive TAG.
# Doing this AFTER arg parsing means --version always wins over the
# API lookup; doing it BEFORE platform detection means we can fail
# fast without making the user wait through the platform banner.
if [[ -z "${APP_VERSION}" ]]; then
    step "Resolving latest blade-ai version from GitHub..."
    APP_VERSION="$(resolve_latest_version)" || \
        err "Could not resolve the latest version (no network, GitHub API rate-limit, or no published blade-ai-v* release yet).\n  Pass an explicit version: --version 0.1.0 or BLADE_AI_VERSION=0.1.0"
    ok "Resolved latest: ${APP_VERSION}"
fi
TAG="blade-ai-v${APP_VERSION}"

# ── OS & Architecture detection ────────────────────────────────────────────────

step "Detecting system architecture..."

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"

case "${OS}-${ARCH}" in
    linux-x86_64|linux-amd64)   PLATFORM="linux-amd64" ;;
    linux-aarch64|linux-arm64)  PLATFORM="linux-arm64" ;;
    darwin-x86_64|darwin-amd64) PLATFORM="darwin-amd64" ;;
    darwin-arm64)               PLATFORM="darwin-arm64" ;;
    *) err "Unsupported platform: ${OS}-${ARCH}" ;;
esac

ok "Detected ${PLATFORM}"

# ── Determine install directory ────────────────────────────────────────────────

if [[ -n "${FORCE_INSTALL_DIR}" ]]; then
    INSTALL_DIR="${FORCE_INSTALL_DIR}"
    NO_MODIFY_PATH=1  # Custom install dir: user manages PATH themselves
else
    INSTALL_DIR="${BLADE_AI_INSTALL_DIR:-${VERSIONS_DIR}/${TAG}}"
fi

BIN_DIR="${INSTALL_DIR}"
SYMLINK_DIR="$HOME/.local/bin"

# ── Existing-install overwrite check ──────────────────────────────────────────
#
# Done BEFORE the download so the user's "no" answer costs zero
# network traffic and zero filesystem mutation. Resolution rules:
#
#   * Target dir doesn't exist → nothing to ask, fall through.
#   * --yes / -y / BLADE_AI_FORCE_OVERWRITE=1 → log + overwrite silently.
#   * stdin is not a tty (e.g. ``curl ... | bash`` / Dockerfile / CI)
#     → overwrite silently to preserve the historical "one-liner =
#     unattended" semantics. Scripts that want strict checking
#     should pass --yes explicitly.
#   * Otherwise (interactive tty) → prompt y/N. Anything other than
#     y/yes (case-insensitive) cancels with exit 0 — nothing was
#     downloaded, nothing was deleted.
#
# The actual removal still happens later (after download +
# verification succeed). This block only decides whether we're
# allowed to remove at all.
if [[ -d "${INSTALL_DIR}" ]]; then
    if [[ ${FORCE_OVERWRITE} -eq 1 ]]; then
        info "Existing install detected at ${INSTALL_DIR} — will overwrite (--yes)."
    elif [ ! -t 0 ]; then
        info "Existing install detected at ${INSTALL_DIR} — will overwrite (non-interactive)."
    else
        echo ""
        warn "Existing install detected at: ${INSTALL_DIR}"
        info "  (Same version ${APP_VERSION}; the directory will be removed and re-created.)"
        info "  (User config / memory / skills under ~/.blade-ai are NOT touched.)"
        echo ""
        printf "Overwrite the existing install? [y/N] "
        ans=""
        read -r ans || true
        case "${ans}" in
            y|Y|yes|YES|Yes) ;;
            *)
                echo ""
                info "Cancelled — nothing was downloaded or modified."
                exit 0
                ;;
        esac
    fi
fi

# ── Download URL construction ──────────────────────────────────────────────────

BASE_URL="${BLADE_AI_MIRROR:-https://github.com/chaosblade-io/chaosblade/releases/download}"
DOWNLOAD_URL="${BASE_URL}/${TAG}/blade-ai-${PLATFORM}.tar.gz"
CHECKSUM_URL="${BASE_URL}/${TAG}/checksums.txt"

info "Download URL: ${DOWNLOAD_URL}"

# ── Atomic download + verification ─────────────────────────────────────────────

step "Downloading ${APP_NAME} ${APP_VERSION}..."

mkdir -p "${VERSIONS_DIR}"
TMP_DIR="$(mktemp -d "${VERSIONS_DIR}/.tmp-${TAG}-XXXXXXXXXXXX")"

cleanup() { rm -rf "${TMP_DIR}"; }
trap cleanup EXIT

TMP_TAR="${TMP_DIR}/blade-ai.tar.gz"

downloader "${DOWNLOAD_URL}" "${TMP_TAR}"

ok "Download complete"

# ── SHA256 checksum verification ──────────────────────────────────────────────

if [[ "${SKIP_VERIFY}" != "1" ]]; then
    step "Verifying checksum..."

    TMP_CHECKSUM="${TMP_DIR}/checksums.txt"
    downloader "${CHECKSUM_URL}" "${TMP_CHECKSUM}" 2>/dev/null || {
        warn "Checksum file not found at ${CHECKSUM_URL}"
        warn "Skipping verification (checksums.txt unavailable for this release)"
        SKIP_VERIFY=1
    }

    if [[ "${SKIP_VERIFY}" != "1" ]]; then
        EXPECTED_HASH="$(grep "blade-ai-${PLATFORM}.tar.gz" "${TMP_CHECKSUM}" | awk '{print $1}')"
        if [[ -z "${EXPECTED_HASH}" ]]; then
            err "No checksum entry for blade-ai-${PLATFORM}.tar.gz in checksums.txt"
        fi

        ACTUAL_HASH="$(command -v sha256sum >/dev/null 2>&1 && sha256sum "${TMP_TAR}" | awk '{print $1}' || shasum -a 256 "${TMP_TAR}" | awk '{print $1}')"

        if [[ "${EXPECTED_HASH}" != "${ACTUAL_HASH}" ]]; then
            err "Checksum mismatch!\n  Expected: ${EXPECTED_HASH}\n  Actual:   ${ACTUAL_HASH}"
        fi

        ok "Checksum verified"
    fi
fi

# ── Extract ────────────────────────────────────────────────────────────────────

step "Extracting package..."

# --strip-components=1 removes the top-level "blade-ai/" directory from the tar
tar --strip-components=1 -xzf "${TMP_TAR}" -C "${TMP_DIR}"

ok "Extraction complete"

# ── Atomic move to final destination ──────────────────────────────────────────

step "Installing to ${INSTALL_DIR}..."

# Remove previous version directory if it exists
if [[ -d "${INSTALL_DIR}" ]]; then
    rm -rf "${INSTALL_DIR}"
fi

# Atomic move: on same filesystem this is instant; cross-FS is still safe
if ! mv "${TMP_DIR}" "${INSTALL_DIR}" 2>/dev/null; then
    # Cross-filesystem move: fall back to copy + remove
    mkdir -p "${INSTALL_DIR}"
    cp -r "${TMP_DIR}/." "${INSTALL_DIR}/"
    rm -rf "${TMP_DIR}"
fi

ok "Installed to ${INSTALL_DIR}"

# Disable cleanup trap (files already in final location)
trap - EXIT

# ── Create symlink ────────────────────────────────────────────────────────────

step "Creating symlink..."

mkdir -p "${SYMLINK_DIR}"
ln -sf "${INSTALL_DIR}/blade-ai" "${SYMLINK_DIR}/blade-ai"

ok "Symlink created: ${SYMLINK_DIR}/blade-ai -> ${INSTALL_DIR}/blade-ai"

# ── Write receipt ─────────────────────────────────────────────────────────────

mkdir -p "${RECEIPT_HOME}"
cat > "${RECEIPT_HOME}/receipt.json" <<EOF
{
  "version": "${APP_VERSION}",
  "platform": "${PLATFORM}",
  "install_dir": "${INSTALL_DIR}",
  "symlink_dir": "${SYMLINK_DIR}",
  "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "source": "curl-bash"
}
EOF

# ── Write install manifest (for uninstall) ────────────────────────────────────

cat > "${RECEIPT_HOME}/install-manifest.json" <<EOF
{
  "version": "${APP_VERSION}",
  "platform": "${PLATFORM}",
  "install_dir": "${INSTALL_DIR}",
  "symlink": "${SYMLINK_DIR}/blade-ai",
  "receipt": "${RECEIPT_HOME}/receipt.json",
  "modified_files": []
}
EOF

# ── PATH configuration ────────────────────────────────────────────────────────

# Check if ~/.local/bin is already in PATH
if [[ ":${PATH}:" == *":${SYMLINK_DIR}:"* ]]; then
    NO_MODIFY_PATH=1  # Already configured
fi

if [[ "${NO_MODIFY_PATH}" == "0" ]]; then
    step "Configuring PATH..."

    # Determine which shell rc files exist and are writable
    CURRENT_SHELL="$(basename "${SHELL}")"

    modified_profiles=()

    add_to_profile() {
        local _profile="$1" _line="$2"
        if [[ -f "${_profile}" ]] && ! grep -qF "${_line}" "${_profile}" 2>/dev/null; then
            echo "${_line}" >> "${_profile}"
            modified_profiles+=("${_profile}")
        fi
    }

    case "${CURRENT_SHELL}" in
        bash)
            add_to_profile "$HOME/.bashrc" "export PATH=\"\$HOME/.local/bin:\$PATH\"  # blade-ai"
            # .bash_profile for login shells (macOS Terminal)
            if [[ -f "$HOME/.bash_profile" ]]; then
                add_to_profile "$HOME/.bash_profile" "export PATH=\"\$HOME/.local/bin:\$PATH\"  # blade-ai"
            fi
            ;;
        zsh)
            add_to_profile "$HOME/.zshrc" "export PATH=\"\$HOME/.local/bin:\$PATH\"  # blade-ai"
            # .zprofile for login shells
            if [[ -f "$HOME/.zprofile" ]]; then
                add_to_profile "$HOME/.zprofile" "export PATH=\"\$HOME/.local/bin:\$PATH\"  # blade-ai"
            fi
            ;;
        fish)
            mkdir -p "$HOME/.config/fish"
            FISH_CONF="$HOME/.config/fish/config.fish"
            if ! grep -qF 'fish_add_path $HOME/.local/bin' "${FISH_CONF}" 2>/dev/null; then
                echo 'fish_add_path $HOME/.local/bin  # blade-ai' >> "${FISH_CONF}"
                modified_profiles+=("${FISH_CONF}")
            fi
            ;;
        *)
            # Fallback: try .profile
            add_to_profile "$HOME/.profile" "export PATH=\"\$HOME/.local/bin:\$PATH\"  # blade-ai"
            ;;
    esac

    # Update install manifest with modified files
    if [[ ${#modified_profiles[@]} -gt 0 ]]; then
        MANIFEST_PATH="${RECEIPT_HOME}/install-manifest.json"
        # Build JSON array of modified profile paths
        MODIFIED_JSON="$(printf '%s\n' "${modified_profiles[@]}" | python3 -c "import sys,json; print(json.dumps([l.strip() for l in sys.stdin]))" 2>/dev/null)"
        if [[ -n "${MODIFIED_JSON}" ]]; then
            python3 -c "
import json
m = json.load(open('${MANIFEST_PATH}'))
m['modified_files'] = json.loads('${MODIFIED_JSON}')
json.dump(m, open('${MANIFEST_PATH}', 'w'), indent=2)
" 2>/dev/null || true
        fi

        # Make PATH available in current session
        export PATH="${SYMLINK_DIR}:${PATH}"

        ok "PATH configured (${#modified_profiles[@]} profile(s) updated)"
    else
        warn "Could not modify any shell profile"
        warn "Please manually add ${SYMLINK_DIR} to your PATH"
    fi
fi

# ── GitHub Actions PATH support ────────────────────────────────────────────────

if [[ -n "${GITHUB_PATH:-}" ]]; then
    echo "${SYMLINK_DIR}" >> "${GITHUB_PATH}"
fi

# ── Success message ────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}${GREEN}✨ blade-ai ${APP_VERSION} installed!${NC}"
echo ""

if [[ ":${PATH}:" == *":${SYMLINK_DIR}:"* ]]; then
    echo -e "${BOLD}Start using blade-ai:${NC}"
    echo -e "   ${BOLD}blade-ai${NC}"
else
    echo -e "${BOLD}Next steps:${NC}"
    echo ""
    echo -e "  1. Add ~/.local/bin to your PATH:"
    echo -e "     ${BLUE}export PATH=\"\$HOME/.local/bin:\$PATH\"${NC}"
    echo ""
    echo -e "  2. Then start blade-ai:"
    echo -e "     ${BLUE}blade-ai${NC}"
    echo ""
    echo -e "  Or restart your terminal to apply PATH changes."
fi

echo ""
echo -e "${DIM}Uninstall: blade-ai uninstall${NC}"
echo -e "${DIM}Update:    blade-ai update${NC}"
echo ""