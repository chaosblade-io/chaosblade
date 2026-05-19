"""File searching tool: glob-based file search for the LLM agent.

Provides a `search_files` tool that finds files matching a pattern
under a given directory.  Results are limited in count to avoid
overwhelming the LLM context.
"""

import logging
from pathlib import Path

from chaos_agent.tools.file_reader import _is_denylisted

logger = logging.getLogger(__name__)

# Maximum number of results to return
MAX_RESULTS = 50


def safe_search_files(
    directory: str,
    pattern: str = "*",
    max_results: int = MAX_RESULTS,
) -> str:
    """Search for files matching a glob pattern under a directory.

    Recursively searches for files whose names match the given pattern.
    Uses Python glob syntax (e.g. "*.yaml", "**/*.py", "config.*").

    Args:
        directory: Root directory to search in.
        pattern: Glob pattern to match filenames against. Default "*" matches all.
                 Supports ** for recursive matching (e.g. "**/*.py").
        max_results: Maximum number of results to return (capped at 50).

    Returns:
        Formatted listing of matching files, or a "no matches" message.

    Raises:
        FileNotFoundError: If the directory does not exist.
        PermissionError: If the directory is in the deny-list.
        NotADirectoryError: If the path is not a directory.
    """
    p = Path(directory).expanduser()

    # Resolve relative paths against cwd
    if not p.is_absolute():
        p = Path.cwd() / p

    # Security check
    denied, reason = _is_denylisted(p)
    if denied:
        raise PermissionError(reason)

    if not p.exists():
        raise FileNotFoundError(f"Directory not found: {p}")

    if not p.is_dir():
        raise NotADirectoryError(f"Path is not a directory: {p}")

    # Cap max_results
    max_results = min(max_results, MAX_RESULTS)

    # Search with glob pattern
    matches = []
    try:
        # Ensure ** is in pattern for recursive search
        if "**" not in pattern:
            # Non-recursive: search in immediate directory only
            for f in sorted(p.glob(pattern)):
                if f.is_file():
                    matches.append(f)
        else:
            # Recursive search
            for f in sorted(p.glob(pattern)):
                if f.is_file():
                    matches.append(f)
    except PermissionError:
        return f"Search error: permission denied for some paths under {p}"

    if not matches:
        return f"No files matching '{pattern}' found under {p}"

    total = len(matches)
    truncated = matches[:max_results]
    lines = [f"Found {total} file(s) matching '{pattern}' under {p}:"]
    if total > max_results:
        lines.append(f"(showing first {max_results} of {total})")

    for f in truncated:
        try:
            rel = f.relative_to(p)
            size = f.stat().st_size
            lines.append(f"  - {rel} ({size} bytes)")
        except (ValueError, OSError):
            lines.append(f"  - {f}")

    return "\n".join(lines)
