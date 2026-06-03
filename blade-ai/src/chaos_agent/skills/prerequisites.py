"""Prerequisites checker: verifies required CLI tools are available."""

import logging
import shutil
from collections import defaultdict
from typing import Optional

from chaos_agent.config.settings import settings
from chaos_agent.tools.shell import run_command

logger = logging.getLogger(__name__)


class PrerequisitesChecker:
    """Check that all CLI tools declared by skills are available."""

    async def check_all(self, registry) -> dict[str, list[str]]:
        """Check all required tools from the skill registry.

        Args:
            registry: SkillRegistry instance

        Returns:
            Dict of {tool_name: [skill_names_that_need_it]} for missing tools.
            Empty dict means all prerequisites are satisfied.
        """
        tool_to_skills: dict[str, list[str]] = defaultdict(list)

        for name, meta in registry.metadata.items():
            for tool_name in meta.required_tools:
                tool_to_skills[tool_name].append(name)

        missing: dict[str, list[str]] = {}
        for tool_name, skill_names in tool_to_skills.items():
            if not shutil.which(tool_name):
                missing[tool_name] = skill_names
                logger.warning(
                    f"Missing tool '{tool_name}' required by skills: {skill_names}"
                )

        return missing

    async def check_tool_version(self, tool_name: str) -> Optional[str]:
        """Get the version string of a CLI tool.

        Args:
            tool_name: Name of the CLI tool

        Returns:
            Version string, or None if the tool is not available.
        """
        if not shutil.which(tool_name):
            return None

        try:
            result = await run_command(
                [tool_name, "--version"],
                timeout=10,
                skip_guard=True,
            )
            return result.stdout.strip()
        except Exception:
            return None

    async def check_startup_prerequisites(self, registry) -> dict[str, list[str]]:
        """Run all prerequisite checks at server startup.

        Logs warnings for missing tools but does not block startup.
        Returns the missing tools dict for optional display.
        """
        missing = await self.check_all(registry)

        if missing:
            for tool_name, skill_names in missing.items():
                logger.warning(
                    f"Tool '{tool_name}' not found in PATH. "
                    f"Skills affected: {', '.join(skill_names)}"
                )
        else:
            logger.info("All prerequisite tools are available")

        # Also check blade and kubectl versions
        blade_version = await self.check_tool_version(settings.blade_path)
        kubectl_version = await self.check_tool_version(settings.kubectl_path)

        if blade_version:
            logger.info(f"ChaosBlade version: {blade_version}")
        if kubectl_version:
            logger.info(f"kubectl version: {kubectl_version}")

        return missing
