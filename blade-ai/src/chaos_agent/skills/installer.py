"""Skill installer — copy skill bundles into ``settings.skills_dir``.

Two source kinds are supported:

* **Local path** — a directory containing one ``SKILL.md`` (single-skill
  bundle) or a directory whose subdirectories each contain a ``SKILL.md``
  (multi-skill bundle).  The contents are copied verbatim.
* **Git URL** — anything ``git`` can clone.  We clone into a temp dir, then
  treat it as a local path bundle.

The installer **only copies files**.  It never executes any setup script
that may ship with the skill — the user can review the new directory
before activation.  Each installed skill's source path and SHA-256 of
``SKILL.md`` are returned so callers can surface a "verify the source"
prompt before users run the skill.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from chaos_agent.skills.loader import load_skill_metadata
from chaos_agent.skills.validator import SkillValidator

logger = logging.getLogger(__name__)


class SkillInstallError(RuntimeError):
    """Raised when a skill cannot be installed."""


@dataclass
class InstalledSkill:
    """One skill that was successfully copied into ``skills_dir``."""

    name: str
    source: str          # Original git url or filesystem path the user passed.
    target_dir: Path     # Destination inside ``skills_dir``.
    skill_md_sha256: str # Hex digest of SKILL.md for verification.


def _looks_like_git_url(source: str) -> bool:
    s = source.strip()
    if s.startswith(("git@", "git://", "ssh://", "http://", "https://")) and (
        s.endswith(".git") or "/" in s
    ):
        return True
    return False


def _hash_skill_md(skill_dir: Path) -> str:
    skill_md = skill_dir / "SKILL.md"
    h = hashlib.sha256()
    h.update(skill_md.read_bytes())
    return h.hexdigest()


def _candidate_skill_dirs(root: Path) -> list[Path]:
    """Return every dir under ``root`` (inclusive) that has a SKILL.md."""
    out: list[Path] = []
    if (root / "SKILL.md").is_file():
        out.append(root)
        return out
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and (entry / "SKILL.md").is_file():
            out.append(entry)
    return out


async def _git_clone(url: str, dest: Path) -> None:
    """Clone *url* into *dest* with depth=1.  Raises on failure."""
    from chaos_agent.utils.exec_path import resolve_exec_path
    git_bin = resolve_exec_path("git")
    proc = await asyncio.create_subprocess_exec(
        git_bin,
        "clone",
        "--depth",
        "1",
        url,
        str(dest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError as exc:
        # Reap the killed child deterministically (kill + await wait), matching
        # the other asyncio spawn sites (shell / env_info / preflight /
        # wizard_validators). Not payload-bearing under ThreadedChildWatcher —
        # the watcher thread reaps the zombie and the loop closes the pipes on
        # child-exit EOF regardless of this await — but it makes fd release
        # synchronous instead of deferred to the next loop pass, and removes
        # the lone asymmetry (probe: .b4tmp/_gitclone_reap_probe.py).
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        raise SkillInstallError(f"git clone timed out after 120s: {url}") from exc
    if proc.returncode != 0:
        msg = stderr.decode(errors="replace").strip()[:400]
        raise SkillInstallError(f"git clone failed ({proc.returncode}): {msg}")


def _copy_skill(
    src_dir: Path, target_root: Path, *, dest_name: str, overwrite: bool,
) -> Path:
    """Copy *src_dir* under *target_root* as *dest_name*; return the new dir.

    The destination directory is named after the skill's canonical
    frontmatter ``name`` (``dest_name``), NOT the source directory's
    basename. This upholds the system-wide invariant ``dirname ==
    frontmatter name`` at the write side, so the on-disk layout always
    matches the registry key and ``resolve_skill_dir`` (which resolves by
    frontmatter name). It also stops two differently-named source dirs that
    declare the SAME skill name from aliasing to different installed dirs.
    ``dest_name`` is already validated by ``SkillValidator.NAME_PATTERN``
    (``^[a-z0-9][a-z0-9-]*$``) before install reaches here, so it is
    filesystem-safe; the separator/``..`` guard is defence-in-depth on an
    install path.
    """
    safe = (dest_name or "").strip()
    if not safe or safe in (".", "..") or "/" in safe or "\\" in safe:
        raise SkillInstallError(
            f"Refusing to install skill under an unsafe directory name: {dest_name!r}"
        )
    target_dir = target_root / safe
    if target_dir.exists():
        if not overwrite:
            raise SkillInstallError(
                f"Skill directory already exists: {target_dir}. "
                f"Use overwrite=True to replace."
            )
        shutil.rmtree(target_dir)
    shutil.copytree(src_dir, target_dir)
    return target_dir


async def install_skill(
    source: str,
    *,
    skills_dir: Optional[Path] = None,
    overwrite: bool = False,
    validator: Optional[SkillValidator] = None,
) -> list[InstalledSkill]:
    """Install one or more skills from *source* into *skills_dir*.

    *source* can be:
      - A local directory containing one ``SKILL.md`` (single-skill bundle).
      - A local directory whose children each contain ``SKILL.md`` (bulk).
      - A git url (cloned to a temp dir, then treated as a local path).

    Returns the list of skills that were successfully installed (post copy +
    metadata validation).  Skills that fail validation in the source are
    skipped with a warning and not included.

    Raises :class:`SkillInstallError` for hard failures (no SKILL.md found,
    git clone failed, destination conflict without ``overwrite``).
    """
    from chaos_agent.skills.loader import get_skills_dir

    target_root = (skills_dir or get_skills_dir()).expanduser()
    target_root.mkdir(parents=True, exist_ok=True)
    validator = validator or SkillValidator()

    work_root: Path
    cleanup: Optional[Path] = None

    if _looks_like_git_url(source):
        cleanup = Path(tempfile.mkdtemp(prefix="blade-skill-"))
        try:
            await _git_clone(source, cleanup)
        except Exception:
            shutil.rmtree(cleanup, ignore_errors=True)
            raise
        work_root = cleanup
    else:
        work_path = Path(source).expanduser().resolve()
        if not work_path.exists():
            raise SkillInstallError(f"Source path does not exist: {work_path}")
        work_root = work_path

    try:
        skill_dirs = _candidate_skill_dirs(work_root)
        if not skill_dirs:
            raise SkillInstallError(
                f"No SKILL.md found in {source} (looked at {work_root})"
            )

        installed: list[InstalledSkill] = []
        for src_dir in skill_dirs:
            ok, errors = validator.validate(src_dir)
            if not ok:
                logger.warning(
                    "Skipping invalid skill at %s: %s", src_dir, errors
                )
                continue
            # Read metadata from the SOURCE before copying, so the installed
            # directory can be named after the canonical frontmatter name
            # (see _copy_skill) rather than the source dir's basename.
            try:
                meta = load_skill_metadata(src_dir)
            except Exception as exc:
                logger.warning(
                    "Skipping skill at %s: unreadable metadata (%s)", src_dir, exc
                )
                continue
            target_dir = _copy_skill(
                src_dir, target_root, dest_name=meta.name, overwrite=overwrite,
            )
            installed.append(
                InstalledSkill(
                    name=meta.name,
                    source=source,
                    target_dir=target_dir,
                    skill_md_sha256=_hash_skill_md(target_dir),
                )
            )
            logger.info("Installed skill '%s' from %s -> %s", meta.name, source, target_dir)
        return installed
    finally:
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)
