"""File reading tool: safe, unified file access for the LLM agent.

Provides a single `read_file` tool that both the agent and skill resource
loading can use.  Paths are validated against a deny-list of sensitive
locations to prevent reading secrets or system-critical files.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

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

    Args:
        file_path: Absolute or relative path to the file/directory.

    Returns:
        File content as string, or a directory listing.

    Raises:
        FileNotFoundError: If the path does not exist.
        PermissionError: If the path is in the deny-list.
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

    # File: read content
    return p.read_text(encoding="utf-8")
