"""Skill structure validator.

Validates SKILL.md files on import, ensuring required fields and
correct directory structure. Invalid skills are skipped with warnings.
"""

import re
from pathlib import Path

from chaos_agent.skills.loader import parse_frontmatter


class SkillValidator:
    """Validates skill directory structure and SKILL.md frontmatter."""

    REQUIRED_FRONTMATTER = ["name", "description"]
    OPTIONAL_FRONTMATTER = [
        "version",
        "category",
        "target",
        "required_tools",
        "tags",
        "parameters",
        "scripts",
    ]
    ALLOWED_SCRIPT_EXTENSIONS = {".py", ".sh", ".yaml", ".yml", ".json"}
    NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

    def validate(self, skill_dir: Path) -> tuple[bool, list[str]]:
        """Validate a skill directory structure.

        Returns (is_valid, list_of_errors).
        """
        errors = []
        skill_md = skill_dir / "SKILL.md"

        # 1. SKILL.md must exist
        if not skill_md.exists():
            errors.append(f"SKILL.md not found in {skill_dir}")
            return False, errors

        # 2. Must have valid YAML frontmatter
        content = skill_md.read_text(encoding="utf-8")
        frontmatter = parse_frontmatter(content)
        if frontmatter is None:
            errors.append("Invalid or missing YAML frontmatter")
            return False, errors

        # 3. Required fields check
        for field_name in self.REQUIRED_FRONTMATTER:
            value = frontmatter.get(field_name)
            if not value:
                errors.append(f"Missing required field: {field_name}")

        # 4. Name format check (lowercase letters, digits, hyphens)
        name = frontmatter.get("name", "")
        if name and not self.NAME_PATTERN.match(name):
            errors.append(f"Invalid name format: {name} (use lowercase letters, digits, hyphens)")

        # 5. Scripts directory file extension check
        scripts_dir = skill_dir / "scripts"
        if scripts_dir.exists() and scripts_dir.is_dir():
            for f in scripts_dir.iterdir():
                if f.is_file() and f.suffix not in self.ALLOWED_SCRIPT_EXTENSIONS:
                    errors.append(f"Unexpected script file type: {f.name} (allowed: {self.ALLOWED_SCRIPT_EXTENSIONS})")

        # 6. Validate declared scripts exist in scripts/ directory
        declared_scripts = frontmatter.get("scripts", [])
        if declared_scripts and isinstance(declared_scripts, list):
            for s in declared_scripts:
                if isinstance(s, dict):
                    script_name = s.get("name", "")
                    if script_name:
                        script_path = scripts_dir / script_name
                        if not script_path.exists():
                            errors.append(f"Declared script not found: {script_name}")

        # 7. Check for unknown frontmatter fields (informational, not an error)
        known_fields = set(self.REQUIRED_FRONTMATTER + self.OPTIONAL_FRONTMATTER)
        unknown = set(frontmatter.keys()) - known_fields
        if unknown:
            # Not an error, just note it
            pass

        return len(errors) == 0, errors
