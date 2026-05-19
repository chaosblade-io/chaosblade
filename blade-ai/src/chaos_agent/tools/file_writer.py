"""File writing tool: safe file creation/overwrite for the LLM agent.

Allows the agent to write content to files (e.g. saving experiment plans,
generating reports).  Uses a deny-list strategy: system directories are
blocked, everything else is allowed.  Same sensitive-path deny-list as
file_reader is also applied.
"""

import logging
from pathlib import Path

from chaos_agent.tools.file_reader import _is_denylisted

logger = logging.getLogger(__name__)

# Directories where writing is NEVER allowed (system-critical)
# Both original and resolved paths are checked to handle symlinks (e.g. /etc -> /private/etc on macOS)
_WRITE_DENIED_ROOTS = [
    "/etc",
    "/private/etc",
    "/usr",
    "/bin",
    "/private/bin",
    "/sbin",
    "/private/sbin",
    "/boot",
    "/sys",
    "/proc",
    "/dev",
    "/root",
    "/var/log",
    "/var/db",
    "/var/run",
    "/private/var/log",
    "/private/var/db",
    "/private/var/run",
]

# Paths that are exceptions to the deny-list (user-accessible temp dirs)
_WRITE_DENIED_EXCEPTIONS = [
    "/var/folders",      # macOS temp dirs (pytest tmp_path)
    "/var/tmp",          # POSIX temp dir
    "/private/var/folders",
    "/private/var/tmp",
]


def _is_write_denied(path: Path) -> tuple[bool, str]:
    """Check if a path should be denied for writing.

    Strategy: deny-list of system directories + sensitive path patterns.
    Exceptions are made for user-accessible temp directories.
    Everything else is allowed by default.
    """
    try:
        resolved = path.resolve()
    except OSError as e:
        return False, f"Cannot resolve path: {e}"

    resolved_str = str(resolved)

    # 0. Check exceptions first - user temp dirs are always allowed
    for exc in _WRITE_DENIED_EXCEPTIONS:
        if resolved_str.startswith(exc + "/") or resolved_str == exc:
            # This path is in an exception zone, skip deny checks
            break
    else:
        # 1. Absolute deny - system directories
        for denied in _WRITE_DENIED_ROOTS:
            if resolved_str.startswith(denied + "/") or resolved_str == denied:
                return True, f"Writing to system directory is not allowed: {denied}"

    # 2. Check deny-list (same as read - no .ssh, .aws, etc.)
    denied, reason = _is_denylisted(path)
    if denied:
        return True, reason

    return False, ""


def safe_write_file(file_path: str, content: str) -> str:
    """Write content to a file safely.

    Creates parent directories if they don't exist.
    Fails if the path is in a denied or system directory.

    Args:
        file_path: Absolute or relative path to the file.
        content: Text content to write.

    Returns:
        Confirmation message with the path and size.

    Raises:
        PermissionError: If the path is not writable.
        IsADirectoryError: If the path points to a directory.
    """
    p = Path(file_path).expanduser()

    # Resolve relative paths against cwd
    if not p.is_absolute():
        p = Path.cwd() / p

    # Cannot write to a directory
    if p.exists() and p.is_dir():
        raise IsADirectoryError(f"Cannot write: path is a directory: {p}")

    # Security check - deny-list
    denied, reason = _is_write_denied(p)
    if denied:
        raise PermissionError(reason)

    # Create parent directories
    p.parent.mkdir(parents=True, exist_ok=True)

    # Write content
    p.write_text(content, encoding="utf-8")
    size = len(content.encode("utf-8"))
    logger.info(f"File written: {p} ({size} bytes)")

    return f"Successfully wrote {size} bytes to {p}"
