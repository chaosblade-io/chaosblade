"""Skill registry: manages skill discovery, activation, and resource access.

Implements the three-tier progressive loading pattern:
- Tier 1 (Discovery): metadata always in memory after load_from_directory()
- Tier 2 (Activation): full instructions loaded on demand via activate()
- Tier 3 (Execution): resource files loaded on demand via read_resource()
"""

import logging
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Optional

from chaos_agent.config.settings import settings
from chaos_agent.errors import ScriptExecutionError, ScriptTimeoutError
from chaos_agent.skills.loader import (
    load_skill_instructions,
    load_skill_metadata,
    load_skill_resource,
    list_skill_resources,
    list_skill_scripts,
)
from chaos_agent.skills.models import ScriptInfo, Skill, SkillMetadata
from chaos_agent.skills.validator import SkillValidator
from chaos_agent.tools.guard import CommandResult, ToolGuard
from chaos_agent.tools.shell import run_command

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Central registry for all skills with progressive loading."""

    def __init__(self, validator: Optional[SkillValidator] = None):
        self._metadata: dict[str, SkillMetadata] = {}  # Tier 1: always in memory
        self._instructions_cache: dict[str, str] = {}  # Tier 2: loaded on demand
        self._skill_dirs: dict[str, Path] = {}
        self._validator = validator or SkillValidator()

    @property
    def metadata(self) -> dict[str, SkillMetadata]:
        return self._metadata

    def load_from_directory(self, skills_dir: Path) -> None:
        """Scan skills_dir and load Tier 1 metadata for all valid skills.

        Invalid skills are skipped with a warning log. Skills whose name
        appears in ``settings.disabled_skills`` are skipped silently with
        an INFO log so the user can re-enable them later.
        """
        if not skills_dir.exists():
            logger.warning(f"Skills directory not found: {skills_dir}")
            return

        disabled = set(settings.disabled_skills or [])

        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            if not (skill_dir / "SKILL.md").exists():
                continue

            # Validate skill structure
            is_valid, errors = self._validator.validate(skill_dir)
            if not is_valid:
                logger.warning(
                    f"Skill validation failed for {skill_dir.name}: {errors}"
                )
                continue

            try:
                meta = load_skill_metadata(skill_dir)
                if not meta.name:
                    logger.warning(f"Skill in {skill_dir} has empty name, skipping")
                    continue

                if meta.name in disabled:
                    logger.info(
                        f"Skill '{meta.name}' is disabled in config; skipping load"
                    )
                    continue

                # Duplicate skill name conflict detection
                if meta.name in self._metadata:
                    existing_dir = self._skill_dirs[meta.name]
                    logger.warning(
                        f"Duplicate skill name '{meta.name}': "
                        f"'{skill_dir}' conflicts with already-loaded '{existing_dir}', "
                        f"skipping"
                    )
                    continue

                self._metadata[meta.name] = meta
                self._skill_dirs[meta.name] = skill_dir
                logger.info(f"Loaded skill: {meta.name} ({meta.category})")
            except Exception as e:
                logger.warning(f"Failed to load skill from {skill_dir}: {e}")

    def reload(self, skills_dir: Optional[Path] = None) -> None:
        """Reload all skills (for hot-reload).

        Clears all caches and re-scans the directory.
        """
        self._metadata.clear()
        self._instructions_cache.clear()
        self._skill_dirs.clear()
        dir_to_scan = skills_dir or next(iter(self._skill_dirs.values()), None)
        if dir_to_scan is not None:
            # If we had skill_dirs, get the parent; otherwise use as-is
            if not dir_to_scan.exists():
                return
            # Determine if this is a single skill dir or the parent skills dir
            parent = dir_to_scan.parent
            if (parent / "SKILL.md").exists() or not any(
                d.is_dir() and (d / "SKILL.md").exists() for d in parent.iterdir()
            ):
                # It's a single skill dir, scan the parent
                pass
            # Always scan the parent directory that contains skill subdirs
            # Try the parent first
            if any(d.is_dir() and (d / "SKILL.md").exists() for d in dir_to_scan.iterdir()):
                self.load_from_directory(dir_to_scan)
            else:
                parent = dir_to_scan.parent
                if any(d.is_dir() and (d / "SKILL.md").exists() for d in parent.iterdir()):
                    self.load_from_directory(parent)

    def build_catalog_prompt(self) -> str:
        """Generate skill catalog text for system prompt / activate_skill tool description."""
        lines = []
        for name, meta in self._metadata.items():
            lines.append(f"- {name}: {meta.description}")
        return "\n".join(lines)

    def activate(self, skill_name: str) -> str:
        """Tier 2: Load full skill instructions on demand."""
        if skill_name not in self._skill_dirs:
            raise KeyError(f"Skill not found: {skill_name}")

        if skill_name not in self._instructions_cache:
            skill_dir = self._skill_dirs[skill_name]
            self._instructions_cache[skill_name] = load_skill_instructions(skill_dir)

        return self._instructions_cache[skill_name]

    def read_resource(self, skill_name: str, resource_path: str) -> str:
        """Tier 3: Read a resource file from a skill directory."""
        if skill_name not in self._skill_dirs:
            raise KeyError(f"Skill not found: {skill_name}")

        skill_dir = self._skill_dirs[skill_name]
        return load_skill_resource(skill_dir, resource_path)

    def list_resources(self, skill_name: str) -> list[str]:
        """List all resource files for a skill."""
        if skill_name not in self._skill_dirs:
            raise KeyError(f"Skill not found: {skill_name}")

        skill_dir = self._skill_dirs[skill_name]
        return list_skill_resources(skill_dir)

    def get_skill(self, skill_name: str) -> Optional[Skill]:
        """Get a full Skill object with metadata and instructions."""
        if skill_name not in self._metadata:
            return None

        meta = self._metadata[skill_name]
        instructions = self._instructions_cache.get(skill_name, "")
        skill_dir = str(self._skill_dirs.get(skill_name, ""))

        return Skill(
            metadata=meta,
            instructions=instructions,
            skill_dir=skill_dir,
        )

    def get_skill_dir(self, skill_name: str) -> Optional[Path]:
        """Get the directory path for a skill."""
        return self._skill_dirs.get(skill_name)

    def get_metadata(self, skill_name: str) -> Optional[SkillMetadata]:
        """Get Tier 1 metadata for a skill."""
        return self._metadata.get(skill_name)

    def list_skills(self) -> list[str]:
        """List all registered skill names."""
        return list(self._metadata.keys())

    # Scope keyword to catalogue directory prefix mapping
    _SCOPE_PREFIX_MAP = {
        "node": ("Node_", "节点"),
        "pod": ("Pod_",),
        "service": ("Service_",),
        "workload": ("workload_", "HPA_", "DaemonSet_"),
    }

    # Target keyword to catalogue directory keyword mapping
    _TARGET_KEYWORD_MAP = {
        "cpu": ("CPU", "cpu"),
        "mem": ("内存", "OOM"),
        "disk": ("磁盘",),
        "network": ("网络", "network"),
    }

    # Action keyword to catalogue file/directory name keyword mapping
    # Keywords serve dual purpose: file-level sorting (Step 3) and directory-level
    # filtering (Step 2.5).  Directory-level keywords distinguish different actions
    # under the same target (e.g., disk+fill vs disk+burn both under "磁盘").
    _ACTION_KEYWORD_MAP = {
        "fill": ("填充", "fill", "使用率", "空间"),      # 目录级：使用率、空间 → 磁盘使用率目录
        "fullload": ("fullload", "满载"),                 # 去除 "CPU"/"cpu"（与 target 交叉）
        "burn": ("burn", "IO", "读写"),                    # 目录级：IO、读写 → 磁盘IO目录
        "load": ("load", "加载", "压力"),                  # 内存压力场景
        "delay": ("delay", "延迟"),
        "loss": ("loss", "丢包"),
        "kill": ("kill", "杀死"),
        "dns": ("dns", "DNS", "域名"),
    }

    def match_use_case(self, scope: str, target: str, action: str) -> Optional[str]:
        """Find a catalogue use-case file matching the given fault parameters.

        Scans the skill's ``references/catalogue/`` directory for a use-case
        .md file that matches the fault scope and target.

        Args:
            scope: Blade scope (node/pod/service/workload/container).
            target: Blade target (cpu/mem/disk/network/process).
            action: Blade action (fullload/load/delay/loss/fill/kill/burn).

        Returns:
            Relative resource path (e.g. ``references/catalogue/Node_CPU使用率过高/Node_CPU使用率过高_异常进程占用.md``)
            or None if no match found.
        """
        # Find the skill that has a catalogue directory
        for skill_name, skill_dir in self._skill_dirs.items():
            catalogue_dir = skill_dir / "references" / "catalogue"
            if not catalogue_dir.exists():
                continue

            # Step 1: Match scope to directory prefix
            scope_prefixes = self._SCOPE_PREFIX_MAP.get(scope, (scope.capitalize() + "_",))
            matching_dirs = []
            for entry in sorted(catalogue_dir.iterdir()):
                if not entry.is_dir():
                    continue
                if any(entry.name.startswith(p) for p in scope_prefixes):
                    matching_dirs.append(entry)

            if not matching_dirs:
                continue

            # Step 2: If target keywords available, narrow down
            target_keywords = self._TARGET_KEYWORD_MAP.get(target, (target,))
            if target_keywords and len(matching_dirs) > 1:
                narrowed = [
                    d for d in matching_dirs
                    if any(kw in d.name for kw in target_keywords)
                ]
                if narrowed:
                    matching_dirs = narrowed

            # Step 2.5: Further narrow by action keywords (directory-level)
            # NOTE: Must be outside Step 2's if block so it executes even when
            # Step 2 is skipped (e.g., only 1 directory after Step 1).
            # When multiple directories match the target, action keywords
            # distinguish different fault phenomena (e.g., disk fill vs disk IO burn).
            action_keywords = self._ACTION_KEYWORD_MAP.get(action, ())
            if action_keywords:
                action_narrowed = [
                    d for d in matching_dirs
                    if any(kw in d.name for kw in action_keywords)
                ]
                if action_narrowed:  # only apply if it narrows (avoids zero-match)
                    matching_dirs = action_narrowed

            # Step 3: Pick the first matching directory and find .md files,
            # sorted by action keyword relevance (best action match first).
            # Files with no action match still appear at the end as fallbacks.
            action_keywords = self._ACTION_KEYWORD_MAP.get(action, (action,))
            for match_dir in matching_dirs:
                md_files = sorted(
                    (f for f in match_dir.iterdir() if f.suffix == ".md"),
                    key=lambda f: -sum(1 for kw in action_keywords if kw.lower() in f.name.lower()),
                )
                if md_files:
                    # Return relative path from skill_dir
                    return str(md_files[0].relative_to(skill_dir))

        return None

    def __len__(self) -> int:
        return len(self._metadata)

    def __contains__(self, skill_name: str) -> bool:
        return skill_name in self._metadata

    # ------------------------------------------------------------------
    # Script execution
    # ------------------------------------------------------------------

    # Allowed script extensions (executable via skill scripts)
    _ALLOWED_SCRIPT_EXTENSIONS = {".py", ".sh"}

    # Reuse ToolGuard's parameter blacklist for sanitization
    _PARAM_BLACKLIST_PATTERNS = [
        re.compile(p) for p in ToolGuard.PARAM_BLACKLIST_PATTERNS
    ]

    def list_scripts(self, skill_name: str) -> list[dict]:
        """Return script metadata for a skill.

        Combines frontmatter-declared scripts with auto-discovered
        scripts from the ``scripts/`` directory.
        """
        if skill_name not in self._skill_dirs:
            raise KeyError(f"Skill not found: {skill_name}")

        skill_dir = self._skill_dirs[skill_name]
        meta = self._metadata[skill_name]

        # Start with frontmatter-declared scripts
        declared_names = set()
        scripts = []
        for s in meta.scripts:
            declared_names.add(s.name)
            scripts.append({
                "name": s.name,
                "description": s.description,
                "parameters": [
                    {"name": p.name, "type": p.type, "required": p.required,
                     "description": p.description}
                    for p in s.parameters
                ],
            })

        # Auto-discover any scripts not already declared
        discovered = list_skill_scripts(skill_dir)
        for rel_path in discovered:
            filename = Path(rel_path).name
            if filename not in declared_names:
                scripts.append({"name": filename, "description": "", "parameters": []})

        return scripts

    def get_script_metadata(self, skill_name: str, script_name: str) -> Optional[ScriptInfo]:
        """Get metadata for a specific script."""
        if skill_name not in self._metadata:
            return None
        meta = self._metadata[skill_name]
        for s in meta.scripts:
            if s.name == script_name:
                return s
        return None

    async def execute_script(
        self,
        skill_name: str,
        script_name: str,
        params: str = "",
        timeout: Optional[int] = None,
    ) -> str:
        """Execute a script from a skill's scripts/ directory.

        Security measures:
        1. Path containment — resolved path must be under skill_dir/scripts/
        2. Extension allowlist — only .py and .sh
        3. Parameter sanitization — block dangerous patterns

        Args:
            skill_name: Registered skill name.
            script_name: Filename within scripts/ directory.
            params: Command-line arguments as a single string.
            timeout: Execution timeout in seconds. Falls back to
                     settings.timeout_skill_script.

        Returns:
            Formatted output string for LLM consumption.
        """
        # 1. Validate skill exists
        if skill_name not in self._skill_dirs:
            raise KeyError(f"Skill not found: {skill_name}")

        skill_dir = self._skill_dirs[skill_name]
        scripts_dir = (skill_dir / "scripts").resolve()

        # 2. Resolve and validate path containment
        script_path = (skill_dir / "scripts" / script_name).resolve()
        if not str(script_path).startswith(str(scripts_dir)):
            raise ScriptExecutionError(
                f"Path traversal blocked: '{script_name}' resolves outside scripts/ directory"
            )

        # 3. Extension allowlist
        if script_path.suffix not in self._ALLOWED_SCRIPT_EXTENSIONS:
            raise ScriptExecutionError(
                f"Script type not allowed: '{script_name}' "
                f"(allowed: {', '.join(sorted(self._ALLOWED_SCRIPT_EXTENSIONS))})"
            )

        # 4. Script must exist
        if not script_path.exists():
            raise ScriptExecutionError(
                f"Script not found: '{script_name}' in skill '{skill_name}'"
            )

        # 5. Parameter sanitization
        if params:
            for pattern in self._PARAM_BLACKLIST_PATTERNS:
                if pattern.search(params):
                    raise ScriptExecutionError(
                        f"Dangerous pattern detected in script parameters"
                    )

        # 6. Select interpreter
        script_meta = self.get_script_metadata(skill_name, script_name)
        if script_meta and script_meta.interpreter:
            interpreter = script_meta.interpreter
        elif script_path.suffix == ".py":
            interpreter = sys.executable
        else:  # .sh
            interpreter = shutil.which("bash") or shutil.which("sh") or "/bin/sh"

        # 7. Assemble command
        cmd = [interpreter, str(script_path)]
        if params:
            cmd.extend(shlex.split(params))

        # 8. Build env_override (inject KUBECONFIG if skill requires kubectl)
        env_override: Optional[dict[str, str]] = None
        meta = self._metadata.get(skill_name)
        if meta and "kubectl" in meta.required_tools:
            kubeconfig = settings.kubeconfig_path
            if kubeconfig:
                expanded = str(Path(kubeconfig).expanduser())
                env_override = {"KUBECONFIG": expanded}

        # 9. Determine timeout
        script_timeout = timeout
        if script_timeout is None and script_meta and script_meta.timeout:
            script_timeout = script_meta.timeout
        if script_timeout is None or script_timeout == 0:
            script_timeout = settings.timeout_skill_script

        # 10. Execute via run_command (skip ToolGuard — we have our own checks)
        try:
            result = await run_command(
                cmd,
                timeout=script_timeout,
                skip_guard=True,
                env_override=env_override,
            )
        except Exception as exc:
            # Re-raise timeout as ScriptTimeoutError
            from chaos_agent.errors import ToolTimeoutError
            if isinstance(exc, ToolTimeoutError):
                raise ScriptTimeoutError(str(exc)) from exc
            raise

        # 11. Format output for LLM consumption
        return self._format_script_output(script_name, result)

    @staticmethod
    def _format_script_output(script_name: str, result: CommandResult) -> str:
        """Format script execution result for LLM consumption."""
        max_stdout = settings.skill_script_max_output
        max_stderr = 1000

        # Header line
        header = f"[Script: {script_name}] Exit code: {result.exit_code} | Duration: {result.duration_ms:.0f}ms"

        # Truncate stdout
        stdout = result.stdout
        stdout_truncated = False
        if len(stdout) > max_stdout:
            stdout = stdout[:max_stdout]
            stdout_truncated = True

        parts = [header, "--- STDOUT ---", stdout]
        if stdout_truncated:
            parts.append(f"... (truncated, {len(result.stdout)} characters total)")

        # Include stderr only if non-empty
        stderr = result.stderr
        if stderr.strip():
            if len(stderr) > max_stderr:
                stderr = stderr[:max_stderr]
            parts.append("--- STDERR ---")
            parts.append(stderr)

        # Prefix errors
        if result.exit_code != 0:
            parts.insert(0, f"[ERROR] Script failed with exit code {result.exit_code}.")

        return "\n".join(parts)
