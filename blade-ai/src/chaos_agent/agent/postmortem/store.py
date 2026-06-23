"""Postmortem filesystem persistence: write + read.

Path layout: ``~/.blade-ai/postmortems/<task_id>.md``

Decisions:
- Separate directory (not under memory/) — postmortems are user-facing
  shareable artefacts, not internal agent state. Easier to gitignore /
  rsync / scrub independently of the agent's memory.
- Plain markdown on disk (not JSON-wrapped) — users can ``cat`` /
  ``glow`` / ``less`` directly without unwrapping.
- File header (`# Postmortem: ...`) added here, not by the LLM, so
  the body always starts with the standardised `## Summary` and the
  header stays consistent across calls.

Safety hardening (R1/R2/R3 — 2026-05-26 round 2 review):
- Files written with ``0o600`` (owner read/write only). Postmortems
  may contain user prompts / pod names / LLM-inferred root-cause text;
  default 0o644 lets co-tenants read them on shared boxes.
- Directory created with ``0o700`` for the same reason.
- Writes are atomic: tmp + ``os.replace`` so an interrupted save
  (Ctrl+C / SSE disconnect) leaves either the OLD complete file or
  the NEW complete file, never a truncated half.
- Target path's ``lstat`` is checked before write: if a symlink sits
  at the target, it's removed first so we never silently follow an
  attacker-planted link out of POSTMORTEM_DIR.

Privacy note:
- ``generate_postmortem`` sends fault_spec / user input / message
  summaries to the configured LLM provider. When that provider is
  cloud-hosted (DashScope / OpenAI / Anthropic), this data LEAVES the
  local host. Set ``BLADE_AI_POSTMORTEM_ENABLED=false`` to opt out.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)

# R12 — postmortem dir follows the same memory_dir convention every
# other on-disk subsystem uses (memory/tasks, memory/sessions, etc.).
# This way a user who sets BLADE_AI_MEMORY_DIR=~/custom-data sees
# postmortems land under that dir too, not silently scattered to the
# default ``~/.blade-ai/postmortems/``.
#
# Resolved lazily via a function so the tests can monkeypatch
# ``settings.memory_dir`` without re-importing the module. Module-level
# ``POSTMORTEM_DIR`` is kept as a compatibility alias pointing at the
# default location for callers that imported it directly before R12.
def get_postmortem_dir() -> Path:
    """Resolve the postmortems root directory from current settings."""
    try:
        from chaos_agent.config.settings import settings
        return settings.resolved_memory_dir.parent / "postmortems"
    except Exception:
        # Settings unavailable (e.g. during very early import) → safe default
        return Path(os.path.expanduser("~/.blade-ai/postmortems"))


POSTMORTEM_DIR = Path(os.path.expanduser("~/.blade-ai/postmortems"))
"""Default postmortem root. Prefer ``get_postmortem_dir()`` for runtime
resolution that follows BLADE_AI_MEMORY_DIR; this constant is kept for
import-time backward compatibility (tests / external callers)."""

# Permission constants. 0o600 = owner read/write only; 0o700 = owner
# rwx only. Locks shared-host scenarios down.
_FILE_MODE = 0o600
_DIR_MODE = 0o700
_TASK_ID_RE = re.compile(r"^task-[a-z0-9][a-z0-9-]*$")

def _validate_task_id(task_id: str) -> None:
    """Validate generated task ids before using them in filesystem paths.

    Task ids are generated internally (``task-`` + uuid-like suffix), not
    user-supplied input. This still rejects malformed values so tests or a
    corrupted state can never escape POSTMORTEM_DIR or create odd filenames.
    """
    if not isinstance(task_id, str) or not task_id:
        raise ValueError(f"task_id must be a non-empty string, got {task_id!r}")
    if "/" in task_id or "\\" in task_id or ".." in task_id:
        raise ValueError(f"task_id contains unsafe path characters: {task_id!r}")
    if not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError(f"task_id must match task-[a-z0-9][a-z0-9-]*, got {task_id!r}")


def _path_for(task_id: str, root: Optional[Path] = None) -> Path:
    """Resolve the postmortem file path for ``task_id`` under ``root``.

    ``root`` is the test override; production code passes None so the
    settings-aware ``get_postmortem_dir()`` resolves the location based
    on the user's ``BLADE_AI_MEMORY_DIR`` configuration.
    """
    _validate_task_id(task_id)
    base = root if root is not None else get_postmortem_dir()
    return base / f"{task_id}.md"


def save_postmortem(
    task_id: str,
    markdown_body: str,
    *,
    root: Optional[Path] = None,
    header_meta: Optional[dict] = None,
) -> Path:
    """Write the postmortem markdown to disk and return the absolute path.

    ``markdown_body`` is the LLM output (starts with ``## Summary``).
    A standardised ``# Postmortem: <skill> on <namespace>`` header +
    metadata line is prepended here so the on-disk file is self-
    contained even when shared out of context.

    ``header_meta`` (optional) populates the metadata line. Recognised
    keys: skill_name, namespace, status, duration, generated_at. Missing
    keys collapse silently — header just omits the unknown bits.
    """
    path = _path_for(task_id, root)
    # R1 — directory locked down to owner-only.
    path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    # mkdir's ``mode`` is honoured ONLY on creation; existing dirs keep
    # their permissions. Chmod defensively so an older 0o755 directory
    # from a pre-R1 install gets tightened next time we write.
    try:
        os.chmod(path.parent, _DIR_MODE)
    except OSError:
        pass  # best-effort; failure on shared FS is non-fatal

    meta = header_meta or {}
    skill = meta.get("skill_name", "") or "unknown"
    namespace = meta.get("namespace", "") or "unknown"
    status = meta.get("status", "") or "unknown"
    duration = meta.get("duration", "") or "unknown"
    generated = meta.get("generated_at", "") or now_iso()

    header = (
        f"# Postmortem: {skill} on {namespace}\n\n"
        f"**Task**: `{task_id}` | "
        f"**Status**: {status} | "
        f"**Duration**: {duration} | "
        f"**Generated**: {generated}\n\n"
    )
    content = header + markdown_body.lstrip() + "\n"

    # R3 — symlink guard. If a symlink sits at the target path (legit
    # use is rare; attacker-planted symlink would otherwise redirect
    # our write to an arbitrary location reachable by the user), drop
    # it so the atomic write below targets a real regular file.
    try:
        st = os.lstat(path)
        import stat as _stat
        if _stat.S_ISLNK(st.st_mode):
            logger.warning("Removing pre-existing symlink at %s before write", path)
            path.unlink()
    except FileNotFoundError:
        pass  # normal case — first write
    except OSError as e:
        logger.warning("lstat check failed for %s: %s", path, e)

    # R2 — atomic write via tmp + os.replace. ``write_text`` directly
    # to the final path can leave a truncated file when an asyncio
    # CancelledError fires mid-write (Ctrl+C / SSE disconnect during
    # the postmortem save step). os.replace is POSIX-atomic — the
    # path either points at the OLD file or the NEW file, never an
    # in-progress one.
    fd, tmp_str = tempfile.mkstemp(
        prefix=f"{task_id}.", suffix=".md.tmp", dir=str(path.parent),
    )
    tmp_path = Path(tmp_str)
    try:
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                fp.write(content)
        except Exception:
            # fdopen owns fd close on exit; if write fails, fall through
            # to cleanup below.
            raise
        # R1 — restrict perms BEFORE the atomic rename so the final
        # path never exists in a world-readable state, even momentarily.
        try:
            os.chmod(tmp_path, _FILE_MODE)
        except OSError:
            pass
        os.replace(tmp_path, path)  # atomic on POSIX
    except OSError as e:
        # Clean up the tmp file on failure so /tmp doesn't accumulate.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        logger.warning("save_postmortem write failed for %s: %s", task_id, e)
        raise

    logger.info("Postmortem saved: %s (%d bytes)", path, len(content))
    return path


def read_postmortem(task_id: str, *, root: Optional[Path] = None) -> str:
    """Read a postmortem markdown by task_id. Raises FileNotFoundError."""
    path = _path_for(task_id, root)
    return path.read_text(encoding="utf-8")


def postmortem_exists(task_id: str, *, root: Optional[Path] = None) -> bool:
    """True if a postmortem file exists for this task_id."""
    try:
        path = _path_for(task_id, root)
    except ValueError:
        return False
    return path.is_file()
