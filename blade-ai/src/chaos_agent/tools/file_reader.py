"""File reading tool: safe, unified file access for the LLM agent.

Provides a single `read_file` tool that both the agent and skill resource
loading can use.  Paths are validated against a deny-list of sensitive
locations to prevent reading secrets or system-critical files.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Maximum file size to read in bytes (50 KB).
# Files larger than this are truncated to avoid overwhelming the LLM context,
# mirroring the MAX_RESULTS cap already used in file_search.py.
MAX_FILE_BYTES = 51_200

# Paths that must NEVER be read (security-sensitive)
_DENIED_PATHS = [
    "/etc/shadow",
    "/etc/ssh",
    "/etc/kubernetes",
    ".ssh",
    ".gnupg",
    ".aws",
    ".kube/config",  # kubeconfig may contain tokens - use kubectl tools instead
]

# File patterns that should not be read
_DENIED_SUFFIXES = (
    ".pem", ".key", ".p12", ".pfx", ".jks",
)


def _is_denylisted(path: Path) -> tuple[bool, str]:
    """Check if a path is in the deny-list of sensitive locations."""
    resolved = str(path.resolve())
    path_str = str(path)

    for denied in _DENIED_PATHS:
        if denied in resolved or denied in path_str:
            return True, f"Access denied: path matches restricted pattern '{denied}'"

    if path.suffix in _DENIED_SUFFIXES:
        return True, f"Access denied: {path.suffix} files may contain private keys"

    return False, ""


def safe_read_file(file_path: str) -> str:
    """Read a file safely, with deny-list filtering and directory listing support.

    If *file_path* points to a directory, returns a listing of its contents.
    If it points to a file, returns the file content.
    Sensitive paths (SSH keys, K8s secrets, private keys) are blocked.

    Files larger than ``MAX_FILE_BYTES`` are truncated to the first
    ``MAX_FILE_BYTES`` bytes with a truncation notice appended, to avoid
    overwhelming the LLM context window.  Non-UTF-8 bytes are replaced
    with the U+FFFD replacement character instead of raising.

    Non-regular files (character devices, FIFOs, sockets) are rejected:
    reading them can block or never terminate, and neither is catchable
    as an exception by the caller.

    Args:
        file_path: Absolute or relative path to the file/directory.

    Returns:
        File content as string, or a directory listing.

    Raises:
        FileNotFoundError: If the path does not exist.
        PermissionError: If the path is in the deny-list.
        ValueError: If the path is not a regular file or directory.
    """
    p = Path(file_path).expanduser()

    # Resolve relative paths against cwd
    if not p.is_absolute():
        p = Path.cwd() / p

    # Security check - deny-list
    denied, reason = _is_denylisted(p)
    if denied:
        raise PermissionError(reason)

    if not p.exists():
        raise FileNotFoundError(f"Path not found: {p}")

    # Directory: return listing
    if p.is_dir():
        items = []
        for child in sorted(p.iterdir()):
            if child.is_dir():
                items.append(f"{child.name}/")
            else:
                items.append(child.name)
        header = f"Directory: {p}\nContents:"
        if items:
            return header + "\n" + "\n".join(f"  - {i}" for i in items)
        return f"{header} (empty)"

    # Non-regular files: reject before opening.
    #
    # ``open()`` on a FIFO blocks until a writer appears — that happens
    # *before* any read, so a read-size cap cannot prevent it. Character
    # devices (/dev/zero, /dev/urandom) and sockets are equally useless to
    # read and equally hazardous. Neither hang is an exception, so
    # ``factory.read_file``'s try/except cannot contain it.
    # ``is_file()`` is true only for regular files; ``/proc`` entries are
    # regular files, so host diagnostics still work.
    if not p.is_file():
        raise ValueError(
            f"Not a regular file (device, FIFO or socket): {p}"
        )

    # File: read content (with size cap and encoding safety).
    #
    # The cap is enforced by reading at most MAX_FILE_BYTES + 1 bytes rather
    # than by trusting ``st_size``: character devices (/dev/zero,
    # /dev/urandom) and FIFOs report ``st_size == 0``, so a size-based branch
    # would fall through to an unbounded read and hang / exhaust memory. That
    # failure mode is NOT an exception, so the caller's try/except in
    # ``factory.read_file`` cannot contain it.
    with open(p, "rb") as f:
        raw = f.read(MAX_FILE_BYTES + 1)

    if len(raw) > MAX_FILE_BYTES:
        text = raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
        # ``st_size`` is only meaningful for regular files; omit the total
        # when it is unavailable or unreliable (0 for special files).
        size = p.stat().st_size
        total = f" of {size}" if size > MAX_FILE_BYTES else ""
        return text + f"\n\n[truncated: showing first {MAX_FILE_BYTES}{total} bytes]"

    return raw.decode("utf-8", errors="replace")
