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

from chaos_agent.skills.models import SkillMetadata, SkillParameter, ScriptInfo


def _wheel_bundled_skills_dir() -> Optional[Path]:
    """Return the wheel-bundled default skills dir, or None.

    pip-install users get the default skill pack force-included into the
    wheel at ``chaos_agent/_skills/`` (see pyproject.toml). This is the
    source-of-truth that gets materialized into ``~/.blade-ai/skills`` on
    first run. ``Path(__file__)`` is ``chaos_agent/skills/loader.py`` →
    ``parent.parent`` is the ``chaos_agent`` package dir.
    """
    bundled = Path(__file__).parent.parent / "_skills"
    return bundled if _has_skills(bundled) else None


def _materialize_bundled_skills(target: Path) -> bool:
    """Copy each wheel-bundled skill into ``target`` if missing.

    Returns True if ``target`` ends up containing at least one skill.
    Per-skill copy (not a top-level copytree) so a user who later adds
    their own skills alongside the defaults never has them clobbered, and
    a partially-populated target is topped up rather than skipped.
    """
    import shutil

    bundled = _wheel_bundled_skills_dir()
    if bundled is None:
        return False
    target.mkdir(parents=True, exist_ok=True)
    for skill_dir in bundled.iterdir():
        if not (skill_dir.is_dir() and (skill_dir / "SKILL.md").is_file()):
            continue
        dest = target / skill_dir.name
        if dest.exists():
            continue
        try:
            shutil.copytree(skill_dir, dest)
        except Exception:
            # Best-effort: a copy failure (perms, race) shouldn't crash
            # skill resolution — fall through to whatever's already there.
            pass
    return _has_skills(target)


def get_skills_dir() -> Path:
    """Resolve skills directory with PyInstaller + wheel-bundle support.

    Priority: config.json > env var > PyInstaller bundle > dev path >
    wheel-bundled defaults (materialized into config dir).
    A directory is only considered a match if it exists AND contains at
    least one sub-directory with a SKILL.md file (i.e. has actual skills).
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

    # 5. Wheel-bundled defaults → materialize into the writable config dir.
    #    pip-install users land here on first run: skills ship inside the
    #    wheel (chaos_agent/_skills) but get copied into ~/.blade-ai/skills
    #    so they're user-editable, hot-reloadable (SkillWatcher), and
    #    ``blade-ai skills install`` can add more alongside. Mirrors the
    #    ChaosBlade ``~/.blade-ai/vendor`` materialization pattern.
    if config_dir:
        config_path = Path(str(config_dir)).expanduser()
        if _materialize_bundled_skills(config_path):
            return config_path

    # 6. Fallback: create the default config dir so there is something to use
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


def load_skill_resource(skill_dir: Path, resource_path: str) -> str:
    """Tier 3: Load a resource file from scripts/references/assets.

    If the path is a directory, returns a listing of files within it
    instead of raising an error.
    """
    full_path = skill_dir / resource_path
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
