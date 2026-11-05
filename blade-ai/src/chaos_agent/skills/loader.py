"""Skill loader: parses SKILL.md files for three-tier progressive loading.

Tier 1: load_skill_metadata() - Only reads YAML frontmatter
Tier 2: load_skill_instructions() - Reads the Markdown body
Tier 3: load_skill_resource() - Reads files in scripts/references/assets
"""

import os
import sys
from pathlib import Path
from typing import Optional

import yaml

from chaos_agent.skills.models import SkillMetadata, SkillParameter, ScriptInfo, SKILL_TYPE_FAULT_INJECTION


def _wheel_bundled_skills_dir() -> Optional[Path]:
    """Return the wheel-bundled default skills dir, or None.

    pip-install users get the default skill pack force-included into the
    wheel at ``chaos_agent/_skills/`` (see pyproject.toml). This is the
    read-only built-in pack, loaded IN PLACE by ``get_builtin_skills_dir()``
    and merge-loaded alongside the writable user root — never copied out.
    ``Path(__file__)`` is ``chaos_agent/skills/loader.py`` →
    ``parent.parent`` is the ``chaos_agent`` package dir.
    """
    bundled = Path(__file__).parent.parent / "_skills"
    return bundled if _has_skills(bundled) else None


def get_skills_dir() -> Path:
    """Resolve the WRITABLE USER skill root (where user-installed skills live).

    Priority: config.json > env var > PyInstaller bundle > dev path >
    create/return the (empty) config dir.
    A directory is only considered a match if it exists AND contains at
    least one sub-directory with a SKILL.md file (i.e. has actual skills).

    The read-only built-in pack is NOT resolved here — it is loaded IN PLACE
    by ``get_builtin_skills_dir()`` and merge-loaded alongside this user root
    (see ``get_skill_roots()``). This function used to copy the built-in pack
    into the user root on first run ("materialization"); that copy never
    refreshed and, being same-named, shadowed the fresh built-in pack in the
    merge — so it was removed. An empty user root is the correct fresh-install
    state: built-in skills load from source, user skills are added by
    ``blade-ai skills install``.
    """
    # 1. config.json setting (highest priority, managed by `blade-ai config`)
    from chaos_agent.config.settings import settings
    config_dir = settings.skills_dir
    if config_dir:
        config_path = Path(str(config_dir)).expanduser()
        if _has_skills(config_path):
            return config_path

    # 2. Environment variable
    env_dir = os.environ.get("BLADE_AI_SKILLS_DIR")
    if env_dir:
        env_path = Path(env_dir)
        if _has_skills(env_path):
            return env_path

    # 3. PyInstaller bundled directory
    if getattr(sys, 'frozen', False):
        bundled = Path(sys._MEIPASS) / "skills"
        if _has_skills(bundled):
            return bundled

    # 4. Development mode (relative to this file)
    dev_skills = Path(__file__).parent.parent.parent.parent / "skills"
    if _has_skills(dev_skills):
        return dev_skills

    # 5. Fallback: create the writable user root (starts EMPTY on a fresh
    #    install). The built-in pack is merge-loaded read-only via
    #    get_builtin_skills_dir(); nothing is copied out here.
    if config_dir:
        config_path = Path(str(config_dir)).expanduser()
        config_path.mkdir(parents=True, exist_ok=True)
        return config_path
    return Path("skills")


def _has_skills(path: Path) -> bool:
    """Check whether a directory exists and contains at least one skill."""
    if not path.is_dir():
        return False
    return any((d / "SKILL.md").is_file() for d in path.iterdir() if d.is_dir())


def get_builtin_skills_dir() -> Optional[Path]:
    """Resolve the pristine, read-only built-in skill pack root.

    This is the source-of-truth pack that ships with the distribution,
    independent of any user-configured ``skills_dir``. Unlike
    :func:`get_skills_dir` — which returns the single *writable* dir the
    user's own skills live in — this always points at the packaged copy so
    the built-in cases are loadable even when the user redirected
    ``skills_dir`` elsewhere. Priority mirrors the built-in branches of
    :func:`get_skills_dir`:

    1. PyInstaller bundle (``sys._MEIPASS/skills``) when frozen
    2. wheel-bundled defaults (``chaos_agent/_skills``)
    3. dev path (repo ``skills/`` next to the package)

    Returns None when no built-in pack is present (e.g. a stripped
    install); callers treat None as "no built-in root to merge".
    """
    if getattr(sys, "frozen", False):
        bundled = Path(sys._MEIPASS) / "skills"
        if _has_skills(bundled):
            return bundled
    wheel = _wheel_bundled_skills_dir()
    if wheel is not None:
        return wheel
    dev = Path(__file__).parent.parent.parent.parent / "skills"
    if _has_skills(dev):
        return dev
    return None


def get_skill_roots() -> list[Path]:
    """Return the ordered, de-duplicated skill roots to merge-load.

    Order encodes precedence: the USER root comes first, the built-in root
    second. :meth:`SkillRegistry.load_from_roots` loads them in order and
    its additive merge keeps the first-seen skill on a name clash — so a
    user skill *shadows* a built-in one of the same name, while built-in
    skills the user did not override are still loaded ("built-in fills the
    gaps"). The user root is :func:`get_skills_dir` (the writable dir:
    config > env > dev path > created-empty config dir); the built-in root
    is :func:`get_builtin_skills_dir` (read-only pack).

    When both resolve to the same directory — the common dev case (repo
    ``skills/``), and a frozen bundle with no user override — it is
    returned once, so nothing is scanned or loaded twice.
    """
    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in (get_skills_dir(), get_builtin_skills_dir()):
        if candidate is None:
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(candidate)
    return roots


def resolve_skill_dir(skill_name: str) -> Optional[Path]:
    """Resolve a skill's directory across the merged roots (user root first).

    Used by safety-critical readers (e.g. ``target_guard.mechanism_writes``)
    that deliberately re-read a case file from disk instead of trusting the
    registry cache. It answers ONE question — *which directory* holds the
    skill named ``skill_name`` — and the caller then re-reads that
    directory's file content fresh, so "don't trust the cache" is preserved:
    this unifies the DIRECTORY authority, not the content.

    There is exactly ONE name→dir authority in the codebase:
    ``SkillRegistry._scan_directory`` (frontmatter-``name`` key + validation
    + ``disabled_skills`` + empty-name skip + first-wins). This function does
    NOT re-implement it — it delegates to ``SkillRegistry.resolve_dir`` over
    a THROWAWAY registry, so the mapping is recomputed from disk on every
    call (cache-free) yet returns the very directory ``load_from_roots``
    would load for that name, applying the identical contract.

    History: an earlier version matched the frontmatter name inline here.
    That was still a SECOND implementation — it silently omitted the
    validation and disabled filters — so a dir sharing a name could resolve
    differently from the registry. Measured: an invalid user-root ``foo``
    (missing ``description``) shadowed a valid built-in ``foo``; the registry
    loaded the built-in while this returned the invalid user dir, so the
    guard read a different case than the model ran — the exact
    authorization-surface mismatch this exists to prevent. A fuller copy
    would only drift on the next filter the registry gains; delegation makes
    divergence structurally impossible.

    Returns None when no root loads that name (the caller fails closed).
    Names carrying a path separator or ``..`` are rejected outright (this
    sits on an authorization path where ``skill_name`` may be LLM-influenced);
    the resolved dir is always confined to a fixed skills root, and the
    actual file read still routes through :func:`load_skill_resource` for
    escape-proof containment.
    """
    name = (skill_name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return None
    # Lazy import: registry.py imports this module at load time, so importing
    # it at module scope here would be circular. A throwaway instance keeps
    # the lookup cache-free (no reuse of any live registry's cached maps).
    from chaos_agent.skills.registry import SkillRegistry

    return SkillRegistry().resolve_dir(name, get_skill_roots())


def parse_frontmatter(content: str) -> Optional[dict]:
    """Extract YAML frontmatter from SKILL.md content.

    Returns None if frontmatter is missing or invalid.
    """
    if not content.startswith("---"):
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        return None

    try:
        return yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None


def _parse_parameters(params_data: Optional[list]) -> list[SkillParameter]:
    """Parse parameter definitions from frontmatter."""
    if not params_data:
        return []

    parameters = []
    for p in params_data:
        if isinstance(p, dict):
            parameters.append(
                SkillParameter(
                    name=p.get("name", ""),
                    type=p.get("type", "string"),
                    required=p.get("required", False),
                    default=p.get("default"),
                    description=p.get("description", ""),
                    example=p.get("example"),
                )
            )
    return parameters


def _parse_scripts(scripts_data: Optional[list]) -> list[ScriptInfo]:
    """Parse script declarations from frontmatter.

    The ``scripts`` field is optional metadata — it enriches the
    execute_skill_script tool description so the LLM knows which scripts
    are available and their expected parameters.
    """
    if not scripts_data:
        return []

    scripts = []
    for s in scripts_data:
        if isinstance(s, dict):
            scripts.append(
                ScriptInfo(
                    name=s.get("name", ""),
                    description=s.get("description", ""),
                    parameters=_parse_parameters(s.get("parameters")),
                    interpreter=s.get("interpreter"),
                    timeout=s.get("timeout"),
                )
            )
    return scripts


def list_skill_scripts(skill_dir: Path) -> list[str]:
    """Scan the scripts/ directory for .py and .sh files.

    Returns relative paths (e.g. "scripts/list_scenarios.py") for all
    discovered scripts.  This is the auto-discovery fallback when
    frontmatter does not declare scripts explicitly.
    """
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.exists() or not scripts_dir.is_dir():
        return []

    result = []
    for f in sorted(scripts_dir.iterdir()):
        if f.is_file() and f.suffix in (".py", ".sh"):
            result.append(str(f.relative_to(skill_dir)))
    return result


def load_skill_metadata(skill_dir: Path) -> SkillMetadata:
    """Tier 1: Load only metadata from YAML frontmatter.

    Does NOT read the Markdown body to minimize token consumption.
    """
    skill_md = skill_dir / "SKILL.md"
    content = skill_md.read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(content)

    if frontmatter is None:
        raise ValueError(f"Invalid or missing YAML frontmatter in {skill_md}")

    return SkillMetadata(
        name=frontmatter.get("name", ""),
        description=frontmatter.get("description", ""),
        version=str(frontmatter.get("version", "1.0")),
        category=frontmatter.get("category", ""),
        target=frontmatter.get("target", ""),
        required_tools=frontmatter.get("required_tools", []) or [],
        tags=frontmatter.get("tags", []) or [],
        skill_type=frontmatter.get("skill_type", SKILL_TYPE_FAULT_INJECTION),
        parameters=_parse_parameters(frontmatter.get("parameters")),
        scripts=_parse_scripts(frontmatter.get("scripts")),
    )


def load_skill_instructions(skill_dir: Path) -> str:
    """Tier 2: Load the full Markdown body from SKILL.md."""
    skill_md = skill_dir / "SKILL.md"
    content = skill_md.read_text(encoding="utf-8")
    parts = content.split("---", 2)
    if len(parts) < 3:
        return ""
    return parts[2].strip()


def _resolve_within(base: Path, relative_path: str) -> Path:
    """Resolve ``base / relative_path`` and assert it stays inside ``base``.

    ``relative_path`` is attacker-influenced input: ``read_skill_resource`` is
    an LLM-callable tool whose path argument comes straight from a model tool
    call. Both ``..`` traversal and absolute-path escapes (``Path(base) /
    "/etc/hosts"`` == ``Path("/etc/hosts")``) must be rejected. ``.resolve()``
    also collapses symlinks, so a symlink inside the skill dir that points out
    of it is rejected too — the safe default for a read-any-file tool.
    """
    root = base.resolve()
    target = (base / relative_path).resolve()
    if target != root and not target.is_relative_to(root):
        raise ValueError(
            f"Resource path escapes skill directory: {relative_path!r}. "
            "Use a path relative to the skill root (e.g. 'references/...', "
            "'scripts/...'); '..' segments and absolute paths are not allowed."
        )
    return target


def load_skill_resource(skill_dir: Path, resource_path: str) -> str:
    """Tier 3: Load a resource file from scripts/references/assets.

    If the path is a directory, returns a listing of files within it
    instead of raising an error.
    """
    full_path = _resolve_within(skill_dir, resource_path)
    if not full_path.exists():
        raise FileNotFoundError(f"Resource not found: {full_path}")
    if full_path.is_dir():
        items: list[str] = []
        for child in sorted(full_path.iterdir()):
            if child.name.startswith("."):
                continue
            rel = str(child.relative_to(skill_dir))
            if child.is_file():
                items.append(rel)
            elif child.is_dir():
                items.append(rel + "/")
                for sub_file in sorted(child.iterdir()):
                    if sub_file.is_file() and not sub_file.name.startswith("."):
                        items.append(str(sub_file.relative_to(skill_dir)))
        return f"Directory: {resource_path}/\nContents:\n" + "\n".join(f"  - {i}" for i in items) if items else f"Directory: {resource_path}/ (empty)"
    return full_path.read_text(encoding="utf-8")


def list_skill_resources(skill_dir: Path) -> list[str]:
    """List all resource files under scripts/, references/, and assets/."""
    resources = []
    for subdir in ("scripts", "references", "assets"):
        sub_path = skill_dir / subdir
        if sub_path.exists() and sub_path.is_dir():
            for f in sub_path.rglob("*"):
                if f.is_file():
                    resources.append(str(f.relative_to(skill_dir)))
    return resources
