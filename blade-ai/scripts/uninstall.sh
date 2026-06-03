#!/usr/bin/env bash
# shellcheck shell=bash
#
# blade-ai uninstaller — companion to scripts/install.sh.
#
# Reverses the on-disk footprint that ``install.sh`` left behind:
#   * Per-version install dir(s) under ``$HOME/.blade-ai/versions/``
#   * Symlink at ``$HOME/.local/bin/blade-ai`` (or whatever the
#     install-manifest.json pointed to)
#   * Shell rc lines tagged ``# blade-ai`` (only those — never touches
#     unrelated PATH entries)
#   * Optionally the whole ``$HOME/.blade-ai/`` config dir
#     (config.json / memory / logs / skills / vendor)
#
# Usage:
#   bash uninstall.sh                       # remove ALL installed versions + config
#   bash uninstall.sh --version 0.1.0       # remove only blade-ai-v0.1.0
#   bash uninstall.sh --keep-config         # keep ~/.blade-ai/{config.json,memory,...}
#   bash uninstall.sh --dry-run             # show plan, do nothing
#   bash uninstall.sh --force               # skip y/N prompt
#
# Environment overrides:
#   BLADE_AI_HOME          base dir (default: ~/.blade-ai)
#   BLADE_AI_SYMLINK_DIR   symlink dir (default: ~/.local/bin)
#
# Robustness contract:
#   * No ``set -e`` — best-effort cleanup; one missing path never
#     aborts the whole uninstall. Each step prints its own ✓/⚠.
#   * Every filesystem touch is guarded against the file/dir not
#     existing, the dir being empty, the symlink being broken, or the
#     shell rc not having any blade-ai lines.
#   * Cross-platform: macOS (BSD sed), Linux (GNU sed), Windows
#     git-bash / WSL / Cygwin (path semantics via $HOME).
#   * Shell rc lines only deleted when the line ends in the literal
#     ``# blade-ai`` marker that install.sh added — no false positives.
#   * Each modified rc file gets a sibling ``.blade-ai-uninstall.bak``
#     so a misclick is recoverable.

# ── Re-exec with bash if running under sh / bash-posix ───────────────────────
#
# Users often invoke the script as ``sh uninstall.sh`` which bypasses
# the shebang. The script uses bash-only features (process substitution
# ``done < <(...)``, arrays, ``[[ ]]``); under POSIX /bin/sh it dies
# with ``syntax error near unexpected token '<'`` during the parse
# phase — *before* this guard can execute, if the unsupported syntax
# is at top level (not inside a function).
#
# Subtle case: on macOS, ``/bin/sh`` is actually bash running in POSIX
# mode. ``$BASH_VERSION`` is still set in that mode (because it IS
# bash), so a naive ``[ -z "${BASH_VERSION:-}" ]`` check would miss
# this scenario — the script would keep running with bash-posix and
# still die on ``< <(...)``. We additionally check POSIX mode flags
# (``$POSIXLY_CORRECT`` and ``$SHELLOPTS``) and re-exec via plain
# bash (no --posix) when either is set.
#
# The guard variable prevents an infinite loop if the re-exec itself
# somehow lands back in a constrained shell.
__blade_ai_needs_reexec() {
    [ -z "${BASH_VERSION:-}" ] && return 0
    [ -n "${POSIXLY_CORRECT:-}" ] && return 0
    case ":${SHELLOPTS:-}:" in
        *":posix:"*) return 0 ;;
    esac
    return 1
}

if __blade_ai_needs_reexec && [ -z "${__BLADE_AI_UNINSTALL_REEXEC:-}" ]; then
    if command -v bash >/dev/null 2>&1; then
        export __BLADE_AI_UNINSTALL_REEXEC=1
        exec bash "$0" "$@"
    else
        echo "Error: This script requires bash. Please install bash first." >&2
        exit 1
    fi
fi
unset -f __blade_ai_needs_reexec 2>/dev/null || true

# pipefail catches broken pipes; deliberately no -e (best-effort) and
# no -u (older bash on macOS 3.2 trips on empty arrays).
set -o pipefail

# ── Color support ──────────────────────────────────────────────────────────────
# Honour NO_COLOR (https://no-color.org) and skip ANSI when stdout is
# not a terminal — pipes / redirected logs stay grep-friendly.
if [ -n "${NO_COLOR+x}" ] || [ ! -t 1 ]; then
    GREEN='' YELLOW='' BLUE='' RED='' BOLD='' DIM='' NC=''
else
    GREEN='\033[0;32m'  YELLOW='\033[0;33m'  BLUE='\033[0;34m'
    RED='\033[0;31m'    BOLD='\033[1m'        DIM='\033[2m'
    NC='\033[0m'
fi

step() { echo -e "${BLUE}▸${NC} ${1}"; }
ok()   { echo -e "${GREEN}✓${NC} ${1}"; }
warn() { echo -e "${YELLOW}⚠${NC} ${1}"; }
err()  { echo -e "${RED}✗${NC} ${1}" >&2; exit 1; }
info() { echo -e "${DIM}${1}${NC}"; }

# ── Defaults & arg parsing ─────────────────────────────────────────────────────
BLADE_AI_HOME="${BLADE_AI_HOME:-$HOME/.blade-ai}"
SYMLINK_DIR="${BLADE_AI_SYMLINK_DIR:-$HOME/.local/bin}"
SYMLINK_PATH="${SYMLINK_DIR}/blade-ai"
VERSIONS_DIR="${BLADE_AI_HOME}/versions"

VERSION=""
KEEP_CONFIG=0
FORCE=0
DRY_RUN=0

print_help() {
    cat <<EOF
${BOLD}blade-ai uninstaller${NC} — undo install.sh's footprint.

Usage:
  $0 [--version VERSION] [--keep-config] [--force] [--dry-run]

Options:
  --version VERSION   Uninstall only this specific version (bare semver,
                      e.g. ${BOLD}0.1.0${NC}). Removes
                      \$BLADE_AI_HOME/versions/blade-ai-vVERSION.
                      If omitted, ${BOLD}ALL${NC} installed versions are removed.

  --keep-config       Keep config.json, logs/, memory/, skills/, vendor/
                      under \$BLADE_AI_HOME. Only removes binaries +
                      install metadata + PATH lines. Implies you may
                      reinstall later without losing your settings.

  --force, -f         Skip the y/N confirmation prompt.

  --dry-run           Print the plan, do not delete anything. Use this
                      first to make sure you agree with the scope.

  -h, --help          Show this help.

Environment overrides:
  BLADE_AI_HOME        Base directory (default: ~/.blade-ai).
  BLADE_AI_SYMLINK_DIR Symlink dir (default: ~/.local/bin).

Examples:
  $0                                  # remove every version + config
  $0 --version 0.1.0 --keep-config    # remove just v0.1.0, keep config
  $0 --dry-run                        # preview without deleting
  $0 --force                          # CI-friendly, no prompt
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            if [[ -z "${2:-}" ]] || [[ "$2" == -* ]]; then
                err "--version requires a value (e.g. 0.1.0)"
            fi
            VERSION="$2"; shift 2 ;;
        --keep-config)
            KEEP_CONFIG=1; shift ;;
        --force|-f)
            FORCE=1; shift ;;
        --dry-run)
            DRY_RUN=1; shift ;;
        -h|--help)
            print_help; exit 0 ;;
        *)
            err "Unknown option: $1 (see --help)" ;;
    esac
done

# ── Discover installed state ───────────────────────────────────────────────────
step "Scanning ${BLADE_AI_HOME}..."

# Nothing installed at all → exit cleanly. We don't treat this as an
# error because the user might run uninstall.sh defensively (e.g. in
# an automation script) without knowing whether install.sh ever ran.
if [[ ! -d "${BLADE_AI_HOME}" ]]; then
    warn "${BLADE_AI_HOME} does not exist — nothing to uninstall."
    exit 0
fi

# Enumerate versions/blade-ai-v* dirs (skip dotfiles + literal pattern
# when no glob match — bash defaults to that and would otherwise feed
# us a fake "blade-ai-v*" entry).
INSTALLED_VERSIONS=()
if [[ -d "${VERSIONS_DIR}" ]]; then
    for dir in "${VERSIONS_DIR}"/blade-ai-v*; do
        [[ -d "${dir}" ]] || continue
        INSTALLED_VERSIONS+=("$(basename "${dir}")")
    done
fi

# Decide which version tags this run will touch.
TARGET_VERSIONS=()
if [[ -n "${VERSION}" ]]; then
    TARGET_TAG="blade-ai-v${VERSION}"
    if [[ ! -d "${VERSIONS_DIR}/${TARGET_TAG}" ]]; then
        warn "Version ${VERSION} (${TARGET_TAG}) is not installed."
        if [[ ${#INSTALLED_VERSIONS[@]} -gt 0 ]]; then
            info "Currently installed:"
            for v in "${INSTALLED_VERSIONS[@]}"; do
                info "  - ${v}"
            done
        else
            info "No versions are currently installed."
        fi
        exit 0
    fi
    TARGET_VERSIONS=("${TARGET_TAG}")
else
    # All-versions mode. Empty INSTALLED_VERSIONS is fine — we still
    # want to clean orphan manifest / symlink / PATH lines below.
    if [[ ${#INSTALLED_VERSIONS[@]} -gt 0 ]]; then
        TARGET_VERSIONS=("${INSTALLED_VERSIONS[@]}")
    fi
fi

# ── Read manifest (best-effort, JSON parser via python3 with fallback) ─────────
MANIFEST="${BLADE_AI_HOME}/install-manifest.json"
MANIFEST_SYMLINK=""
MANIFEST_MODIFIED_FILES=()

read_manifest_field() {
    # Usage: read_manifest_field <python_expr>
    # python_expr operates on a dict-like ``m``; printed output is
    # captured. Failures (no python, malformed JSON, missing key) are
    # silently swallowed — the caller falls back to defaults.
    if ! command -v python3 >/dev/null 2>&1; then return 1; fi
    python3 - "$MANIFEST" <<PY 2>/dev/null
import json, sys
try:
    m = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(0)
$1
PY
}

if [[ -f "${MANIFEST}" ]]; then
    MANIFEST_SYMLINK="$(read_manifest_field 'print(m.get("symlink", ""))')"
    while IFS= read -r line; do
        [[ -n "${line}" ]] && MANIFEST_MODIFIED_FILES+=("${line}")
    done < <(read_manifest_field 'for f in m.get("modified_files", []): print(f)')
fi

# Default the symlink path if manifest didn't give us one. install.sh
# always points it at ~/.local/bin/blade-ai unless the user passed
# --prefix, so this is a sensible fallback.
[[ -z "${MANIFEST_SYMLINK}" ]] && MANIFEST_SYMLINK="${SYMLINK_PATH}"

# ── Decide scope flags ─────────────────────────────────────────────────────────
REMOVE_ALL_VERSIONS=0
[[ -z "${VERSION}" ]] && REMOVE_ALL_VERSIONS=1

REMOVE_BLADE_AI_HOME=0
if [[ ${REMOVE_ALL_VERSIONS} -eq 1 ]] && [[ ${KEEP_CONFIG} -eq 0 ]]; then
    REMOVE_BLADE_AI_HOME=1
fi

# ── Plan summary ──────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}Plan:${NC}"

if [[ ${#TARGET_VERSIONS[@]} -gt 0 ]]; then
    echo "  Versions to remove:"
    for v in "${TARGET_VERSIONS[@]}"; do
        DIR="${VERSIONS_DIR}/${v}"
        SIZE=""
        # du -sh is best-effort; suppress errors so a transient
        # permission glitch doesn't hide the plan line.
        if [[ -d "${DIR}" ]]; then
            SIZE="$(du -sh "${DIR}" 2>/dev/null | awk '{print $1}')"
        fi
        if [[ -n "${SIZE}" ]]; then
            echo "    - ${DIR}  (${SIZE})"
        else
            echo "    - ${DIR}"
        fi
    done
else
    echo "  (No versions detected to remove)"
fi

# Symlink — show only if it actually exists, including broken symlinks
# (where -L is true but -e is false).
if [[ -L "${MANIFEST_SYMLINK}" ]] || [[ -e "${MANIFEST_SYMLINK}" ]]; then
    LINK_TARGET=""
    if [[ -L "${MANIFEST_SYMLINK}" ]]; then
        LINK_TARGET=" → $(readlink "${MANIFEST_SYMLINK}" 2>/dev/null || echo '?')"
    fi
    echo "  Symlink: ${MANIFEST_SYMLINK}${LINK_TARGET}"
fi

# Shell rc cleanup is only applicable in all-versions mode (a single
# version uninstall doesn't need to remove PATH if other versions
# remain).
if [[ ${REMOVE_ALL_VERSIONS} -eq 1 ]]; then
    if [[ ${#MANIFEST_MODIFIED_FILES[@]} -gt 0 ]]; then
        echo "  PATH lines tagged '# blade-ai' will be cleaned from:"
        for f in "${MANIFEST_MODIFIED_FILES[@]}"; do
            [[ -f "${f}" ]] && echo "    - ${f}" || echo "    - ${f}  ${DIM}(missing — skip)${NC}"
        done
    else
        echo "  PATH lines tagged '# blade-ai' will be scanned in:"
        echo "    ~/.zshrc, ~/.zprofile, ~/.bashrc, ~/.bash_profile,"
        echo "    ~/.profile, ~/.config/fish/config.fish"
    fi
fi

# Config-dir headline. Visually highlight the destructive case so
# users see it before they hit Y.
if [[ ${REMOVE_BLADE_AI_HOME} -eq 1 ]]; then
    echo -e "  Config dir: ${RED}${BOLD}${BLADE_AI_HOME}${NC} ${RED}(WILL BE REMOVED — config / memory / skills / logs all gone)${NC}"
elif [[ ${KEEP_CONFIG} -eq 1 ]]; then
    echo -e "  Config dir: ${BLADE_AI_HOME}  ${DIM}(KEPT — --keep-config)${NC}"
else
    echo -e "  Config dir: ${BLADE_AI_HOME}  ${DIM}(KEPT — only specific version requested)${NC}"
fi

if [[ ${DRY_RUN} -eq 1 ]]; then
    echo ""
    info "Dry-run mode — nothing will be deleted. Re-run without --dry-run to apply."
    exit 0
fi

# ── Confirmation ──────────────────────────────────────────────────────────────
if [[ ${FORCE} -eq 0 ]]; then
    echo ""

    # Print the prompt explicitly via printf instead of ``read -p``.
    # ``read -p`` writes the prompt to stderr — and on some terminal
    # / buffering combinations users have reported the prompt never
    # reaches the screen, leaving the script silently "hanging" while
    # actually waiting for input. printf to stdout shows up reliably.
    #
    # If stdin is not a tty (e.g. invoked under a pipe / nohup / CI
    # without --force), reading would just receive EOF immediately and
    # we'd silently fall into the "Cancelled" branch — that's
    # surprising. Detect that case and print an actionable hint
    # instead.
    if [ ! -t 0 ]; then
        printf "Proceed with uninstall? [y/N] "
        echo ""
        warn "stdin is not a tty — cannot ask interactively."
        info "Re-run with --force to skip the prompt, or run from an interactive terminal."
        exit 1
    fi

    printf "Proceed with uninstall? [y/N] "
    ans=""
    read -r ans || true
    case "${ans}" in
        y|Y|yes|YES|Yes) ;;
        *)
            echo ""
            info "Cancelled."
            exit 0
            ;;
    esac
fi

echo ""

# ── 1. Remove version directories ──────────────────────────────────────────────
for v in "${TARGET_VERSIONS[@]}"; do
    DIR="${VERSIONS_DIR}/${v}"
    if [[ -d "${DIR}" ]]; then
        if rm -rf "${DIR}"; then
            ok "Removed: ${DIR}"
        else
            warn "Failed to remove ${DIR}"
        fi
    else
        info "Skip (not present): ${DIR}"
    fi
done

# ── 2. Remove symlink ──────────────────────────────────────────────────────────
# Only delete the symlink if all-versions mode was chosen, OR if the
# link points into one of the version dirs we just removed. This
# protects users who keep multiple versions side by side and only
# meant to delete one of them.
if [[ -L "${MANIFEST_SYMLINK}" ]] || [[ -e "${MANIFEST_SYMLINK}" ]]; then
    SHOULD_REMOVE_LINK=0
    if [[ ${REMOVE_ALL_VERSIONS} -eq 1 ]]; then
        SHOULD_REMOVE_LINK=1
    elif [[ -L "${MANIFEST_SYMLINK}" ]]; then
        LINK_TARGET="$(readlink "${MANIFEST_SYMLINK}" 2>/dev/null || true)"
        for v in "${TARGET_VERSIONS[@]}"; do
            if [[ "${LINK_TARGET}" == *"/${v}/"* ]] || [[ "${LINK_TARGET}" == *"/${v}" ]]; then
                SHOULD_REMOVE_LINK=1
                break
            fi
        done
    fi

    if [[ ${SHOULD_REMOVE_LINK} -eq 1 ]]; then
        if rm -f "${MANIFEST_SYMLINK}"; then
            ok "Removed symlink: ${MANIFEST_SYMLINK}"
        else
            warn "Failed to remove symlink ${MANIFEST_SYMLINK}"
        fi
    else
        info "Symlink kept (points elsewhere): ${MANIFEST_SYMLINK}"
    fi
fi

# Try to clean up the symlink dir if it became empty (~/.local/bin
# usually shared with other tools so this is mostly a no-op, but
# rmdir is safe — it errors silently when non-empty).
if [[ -d "${SYMLINK_DIR}" ]]; then
    rmdir "${SYMLINK_DIR}" 2>/dev/null && info "Removed empty: ${SYMLINK_DIR}" || true
fi

# ── 3. Clean shell rc files ────────────────────────────────────────────────────
if [[ ${REMOVE_ALL_VERSIONS} -eq 1 ]]; then
    SHELL_RC_CANDIDATES=()
    if [[ ${#MANIFEST_MODIFIED_FILES[@]} -gt 0 ]]; then
        SHELL_RC_CANDIDATES=("${MANIFEST_MODIFIED_FILES[@]}")
    else
        SHELL_RC_CANDIDATES=(
            "$HOME/.zshrc"
            "$HOME/.zprofile"
            "$HOME/.bashrc"
            "$HOME/.bash_profile"
            "$HOME/.profile"
            "$HOME/.config/fish/config.fish"
        )
    fi

    # Detect sed flavour once for the whole run. BSD sed (default on
    # macOS) requires ``-i ''`` for in-place edit; GNU sed accepts
    # plain ``-i``. Some Linux distros also ship busybox sed which
    # behaves like GNU's ``-i`` form — the GNU branch covers it.
    SED_FLAVOUR="gnu"
    if ! sed --version >/dev/null 2>&1; then
        SED_FLAVOUR="bsd"
    fi

    for rc in "${SHELL_RC_CANDIDATES[@]}"; do
        [[ -f "${rc}" ]] || { info "Skip (missing): ${rc}"; continue; }

        # Cheap pre-check so we don't write a backup for files that
        # have no blade-ai lines anyway. The grep pattern matches
        # exactly the marker install.sh emits — line ending in
        # ``# blade-ai``.
        if ! grep -qE '# blade-ai$' "${rc}" 2>/dev/null; then
            info "Skip (no blade-ai marker): ${rc}"
            continue
        fi

        BACKUP="${rc}.blade-ai-uninstall.bak"
        if ! cp "${rc}" "${BACKUP}"; then
            warn "Could not back up ${rc} → ${BACKUP}; aborting edit of this file"
            continue
        fi

        if [[ "${SED_FLAVOUR}" == "gnu" ]]; then
            sed -i -E '/# blade-ai$/d' "${rc}"
        else
            sed -i '' -E '/# blade-ai$/d' "${rc}"
        fi

        if [[ $? -eq 0 ]]; then
            ok "Cleaned PATH from: ${rc} (backup at ${BACKUP})"
        else
            warn "sed failed on ${rc}; original is at ${BACKUP}"
            # Restore from backup if sed left it in a weird state
            cp "${BACKUP}" "${rc}" 2>/dev/null || true
        fi
    done
fi

# ── 4. Remove or trim the config home ──────────────────────────────────────────
if [[ ${REMOVE_BLADE_AI_HOME} -eq 1 ]]; then
    if [[ -d "${BLADE_AI_HOME}" ]]; then
        if rm -rf "${BLADE_AI_HOME}"; then
            ok "Removed: ${BLADE_AI_HOME}"
        else
            warn "Failed to remove ${BLADE_AI_HOME}"
        fi
    fi
elif [[ ${REMOVE_ALL_VERSIONS} -eq 1 ]]; then
    # All versions removed but --keep-config asked us to leave the
    # config in place. At minimum drop the install metadata so a
    # future install starts from a clean state.
    for f in install-manifest.json receipt.json; do
        FPATH="${BLADE_AI_HOME}/${f}"
        if [[ -f "${FPATH}" ]]; then
            rm -f "${FPATH}" && ok "Removed: ${FPATH}" || warn "Failed to remove ${FPATH}"
        fi
    done
    # Try to remove the now-empty versions/ subdir. rmdir refuses to
    # touch non-empty dirs so this is safe.
    [[ -d "${VERSIONS_DIR}" ]] && rmdir "${VERSIONS_DIR}" 2>/dev/null \
        && info "Removed empty: ${VERSIONS_DIR}" || true
else
    # Single-version mode (--version was given): only drop that
    # version's directory above. Leave manifest/receipt alone — they
    # may still describe a different installed version.
    info "Config dir kept: ${BLADE_AI_HOME}"
fi

# ── Done ───────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}✨ blade-ai uninstall complete.${NC}"
if [[ ${REMOVE_ALL_VERSIONS} -eq 1 ]]; then
    echo "Restart your terminal (or run \`hash -r\`) to refresh shell PATH cache."
fi
exit 0
