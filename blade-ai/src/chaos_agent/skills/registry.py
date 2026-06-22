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

    def _scan_directory(
        self, skills_dir: Path,
    ) -> tuple[dict[str, SkillMetadata], dict[str, Path]]:
        """Scan ``skills_dir`` and return ``(metadata, skill_dirs)`` dicts.

        Pure function: does not mutate ``self``. Validation, duplicate
        detection within this scan, and ``disabled_skills`` filtering all
        happen inline. Returns empty dicts when the directory is missing.

        Shared by ``load_from_directory`` (additive init path) and
        ``reload`` (atomic swap path) — keeping the scan logic in one place
        means both paths apply identical validation/filtering rules.
        """
        new_metadata: dict[str, SkillMetadata] = {}
        new_skill_dirs: dict[str, Path] = {}

        if not skills_dir.exists():
            logger.warning(f"Skills directory not found: {skills_dir}")
            return new_metadata, new_skill_dirs

        disabled = set(settings.disabled_skills or [])

        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            if not (skill_dir / "SKILL.md").exists():
                continue

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

                # Intra-scan duplicate (two skill dirs in same scan declare
                # the same name) — keep the first, warn about the rest.
                if meta.name in new_metadata:
                    existing_dir = new_skill_dirs[meta.name]
                    logger.warning(
                        f"Duplicate skill name '{meta.name}': "
                        f"'{skill_dir}' conflicts with already-loaded '{existing_dir}', "
                        f"skipping"
                    )
                    continue

                new_metadata[meta.name] = meta
                new_skill_dirs[meta.name] = skill_dir
                logger.info(f"Loaded skill: {meta.name} ({meta.category})")
            except Exception as e:
                logger.warning(f"Failed to load skill from {skill_dir}: {e}")

        return new_metadata, new_skill_dirs

    def load_from_directory(self, skills_dir: Path) -> None:
        """Scan ``skills_dir`` and ADD newly-discovered skills to the registry.

        Additive: does not remove or replace existing skills already in
        the registry. For full hot-reload semantics (atomically replace the
        registry contents with what's currently on disk), use ``reload()``.

        Invalid skills are skipped with a warning log. Skills whose name
        appears in ``settings.disabled_skills`` are skipped silently with
        an INFO log so the user can re-enable them later.
        """
        scanned_meta, scanned_dirs = self._scan_directory(skills_dir)
        # Additive merge — preserve already-loaded entries, add the rest.
        for name, meta in scanned_meta.items():
            if name in self._metadata:
                # Cross-scan duplicate (caller is loading from a second dir
                # that overlaps with what's already loaded).
                existing_dir = self._skill_dirs.get(name)
                logger.warning(
                    f"Duplicate skill name '{name}': '{scanned_dirs[name]}' "
                    f"conflicts with already-loaded '{existing_dir}', skipping"
                )
                continue
            self._metadata[name] = meta
            self._skill_dirs[name] = scanned_dirs[name]

    def reload(self, skills_dir: Optional[Path] = None) -> None:
        """Hot-reload via copy-and-swap atomic replacement.

        Builds fresh ``metadata`` / ``skill_dirs`` dicts off ``self``, then
        atomically reassigns them. Concurrent readers (``activate`` /
        ``get_skill`` / ``__contains__``) running in another thread always
        see either the fully-old or the fully-new state — never a
        half-cleared one. This fixes the race window the legacy
        ``clear() + rescan`` had against ``SkillWatcher``'s Timer thread.

        Tier 2 ``_instructions_cache`` is reset: any cached instructions
        may reference now-stale content; the next ``activate()`` call
        repopulates from disk.

        If ``skills_dir`` is not provided, falls back to the parent of
        any currently-loaded skill (preserves prior reload signature).
        """
        # Resolve which parent directory to scan.
        dir_to_scan = skills_dir or next(iter(self._skill_dirs.values()), None)
        if dir_to_scan is None or not dir_to_scan.exists():
            return

        # Normalise: ``skill_dirs`` values point at individual skill dirs;
        # we need to scan their parent (the directory CONTAINING skill
        # subdirs). When ``dir_to_scan`` is already such a parent, its
        # immediate children include at least one ``SKILL.md``-bearing dir.
        has_skill_subdirs = any(
            d.is_dir() and (d / "SKILL.md").exists()
            for d in dir_to_scan.iterdir()
        )
        if not has_skill_subdirs:
            dir_to_scan = dir_to_scan.parent
            if not any(
                d.is_dir() and (d / "SKILL.md").exists()
                for d in dir_to_scan.iterdir()
            ):
                return  # nothing to scan anywhere

        new_metadata, new_skill_dirs = self._scan_directory(dir_to_scan)

        # Atomic swap. Reader-visible state moves from old-snapshot to
        # new-snapshot in two single-bytecode reference assignments. A
        # reader that interleaves with these only observes a skill
        # appearing/disappearing — the correct behaviour when the user
        # has just added/removed a skill on disk.
        self._skill_dirs = new_skill_dirs
        self._metadata = new_metadata
        self._instructions_cache = {}

    def build_catalog_prompt(self) -> str:
        """Generate skill catalog text for system prompt / activate_skill tool description."""
        lines = []
        for name, meta in self._metadata.items():
            lines.append(f"- {name} [{meta.skill_type}]: {meta.description}")
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
        "process": ("进程", "process"),
    }

    # Action keyword to catalogue file/directory name keyword mapping
    # Keywords serve dual purpose: file-level sorting (Step 3) and directory-level
    # filtering (Step 2.5).  Directory-level keywords distinguish different actions
    # under the same target (e.g., disk+fill vs disk+burn both under "磁盘").
    _ACTION_KEYWORD_MAP = {
        "fill": ("填充", "fill", "使用率", "空间"),      # 目录级：使用率、空间 → 磁盘使用率目录
        "fullload": ("fullload", "满载", "使用率"),          # "使用率" 区分 cpu使用率过高 vs CPU_Throttling
        "burn": ("burn", "IO", "读写"),                    # 目录级：IO、读写 → 磁盘IO目录
        "load": ("load", "加载", "压力"),                  # 内存压力场景
        "drop": ("drop", "丢包", "丢弃"),
        "loss": ("loss", "丢包", "丢失"),                   # 丢包 == packet loss → Pod_网络丢包
        "kill": ("kill", "杀死"),
        "delete": ("delete", "删除"),                       # pod-pod delete → Pod_被删除
        "fail": ("fail", "失败", "篡改"),                  # pod-pod fail → Pod_镜像拉取失败
        "delay": ("delay", "延迟"),                         # pod-network delay → 网络延迟
        "dns": ("dns", "DNS", "域名"),
    }

    def match_use_cases(self, scope: str, target: str, action: str) -> list[str]:
        """Find ALL catalogue use-case files matching the given fault parameters.

        Scans the skill's ``references/catalogue/`` directory for use-case
        .md files that match the fault scope and target. Returns ALL matches
        (not just the first) so callers can disambiguate — e.g. the LLM can
        choose the most relevant one when multiple skill cases share the same
        (scope, target, action) combination.

        Additionally searches **related scopes**: when scope="pod" and
        target="cpu", also checks the "workload" scope to find composite
        scenarios (like HPA) that USE pod-cpu-fullload as their injection
        mechanism but have a different verification methodology.

        Args:
            scope: Blade scope (node/pod/service/workload/container).
            target: Blade target (cpu/mem/disk/network/process).
            action: Blade action (fullload/load/delay/loss/fill/kill/burn).

        Returns:
            List of relative resource paths. Empty list if no match found.
            Ordered by relevance (primary scope first, then related scopes).
        """
        results: list[str] = []
        seen: set[str] = set()

        # Search the primary scope first, then related scopes.
        # Related scopes catch composite scenarios (e.g. HPA uses pod-cpu
        # but lives under workload scope).
        scopes_to_search = [scope]
        # Related scopes catch cross-layer fault chains:
        #   pod → workload: e.g. pod-cpu-fullload triggers HPA (workload scope)
        #   pod → service: e.g. pod-process kill → Service Endpoints removal
        #   node → workload: e.g. node-cordon affects DaemonSet scheduling
        #   node → pod: e.g. node-process stop containerd → Pod ContainerCreating
        _RELATED_SCOPES = {
            "pod": ["workload", "service"],
            "node": ["workload", "pod", "service"],
        }
        scopes_to_search.extend(_RELATED_SCOPES.get(scope, []))

        for search_scope in scopes_to_search:
            is_related = search_scope != scope
            for skill_name, skill_dir in self._skill_dirs.items():
                catalogue_dir = skill_dir / "references" / "catalogue"
                if not catalogue_dir.exists():
                    continue

                # Step 1: Match scope to directory prefix
                scope_prefixes = self._SCOPE_PREFIX_MAP.get(
                    search_scope, (search_scope.capitalize() + "_",)
                )
                matching_dirs = []
                for entry in sorted(catalogue_dir.iterdir()):
                    if not entry.is_dir():
                        continue
                    if any(entry.name.startswith(p) for p in scope_prefixes):
                        matching_dirs.append(entry)

                if not matching_dirs:
                    continue

                # Step 2: Narrow by target keywords.
                # For related scopes, filter at FILE level (not directory):
                # only individual .md files whose content mentions BOTH the
                # target AND action keywords are kept. Requiring both avoids
                # generic words (e.g. "process") causing false positives.
                if is_related:
                    target_keywords = self._TARGET_KEYWORD_MAP.get(target, (target,))
                    action_keywords = self._ACTION_KEYWORD_MAP.get(action, (action,))
                    if target_keywords:
                        _related_file_matches: list[Path] = []
                        for d in matching_dirs:
                            for f in sorted(d.iterdir()):
                                if f.suffix != ".md":
                                    continue
                                try:
                                    text = f.read_text(encoding="utf-8")[:2000]
                                    text_lower = text.lower()
                                    has_target = any(kw.lower() in text_lower for kw in target_keywords)
                                    has_action = any(kw.lower() in text_lower for kw in action_keywords)
                                    if has_target and has_action:
                                        _related_file_matches.append(f)
                                except Exception:
                                    pass
                        # Inject matched files directly into results,
                        # bypassing Step 2.5 and Step 3 (which are
                        # directory-level and would re-expand).
                        for mf in _related_file_matches:
                            rel = str(mf.relative_to(skill_dir))
                            if rel not in seen:
                                seen.add(rel)
                                results.append(rel)
                        continue  # skip Step 2.5 + Step 3 for this scope
                else:
                    target_keywords = self._TARGET_KEYWORD_MAP.get(target, (target,))
                    if target_keywords:
                        narrowed = [
                            d for d in matching_dirs
                            if any(kw in d.name for kw in target_keywords)
                        ]
                        if narrowed:
                            matching_dirs = narrowed
                        elif target in self._TARGET_KEYWORD_MAP:
                            continue

                # Step 2.5: Further narrow by action keywords (directory-level)
                # Also skip for related scopes (same rationale).
                if not is_related:
                    action_keywords = self._ACTION_KEYWORD_MAP.get(action, ())
                    if action_keywords:
                        action_narrowed = [
                            d for d in matching_dirs
                            if any(kw in d.name for kw in action_keywords)
                        ]
                        if action_narrowed:
                            matching_dirs = action_narrowed

                # Step 3: Collect ALL matching .md files (not just the first)
                action_keywords = self._ACTION_KEYWORD_MAP.get(action, (action,))
                for match_dir in matching_dirs:
                    md_files = sorted(
                        (f for f in match_dir.iterdir() if f.suffix == ".md"),
                        key=lambda f: -sum(
                            1 for kw in action_keywords if kw.lower() in f.name.lower()
                        ),
                    )
                    for md_file in md_files:
                        rel = str(md_file.relative_to(skill_dir))
                        if rel not in seen:
                            seen.add(rel)
                            results.append(rel)

        return results

    def match_use_case(self, scope: str, target: str, action: str) -> Optional[str]:
        """Find a catalogue use-case file matching the given fault parameters.

        Convenience wrapper around ``match_use_cases`` that returns only the
        first (most relevant) match. Backward compatible with all existing
        callers.
        """
        matches = self.match_use_cases(scope, target, action)
        return matches[0] if matches else None

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
