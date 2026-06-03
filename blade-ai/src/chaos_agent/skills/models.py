"""Skill data models for the three-tier progressive loading system."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SkillParameter:
    """A single parameter definition in SKILL.md frontmatter."""

    name: str
    type: str = "string"
    required: bool = False
    default: Optional[str] = None
    description: str = ""
    example: Optional[str] = None


@dataclass
class ScriptInfo:
    """A script declaration in SKILL.md frontmatter (optional metadata).

    Used to enrich the execute_skill_script tool description so the LLM
    knows which scripts are available and their expected parameters.
    The actual trigger to run a script comes from SKILL.md Markdown body
    instructions, not from this declaration.
    """

    name: str                          # Filename, e.g. "list_scenarios.py"
    description: str = ""              # Human-readable description
    parameters: list[SkillParameter] = field(default_factory=list)
    interpreter: Optional[str] = None  # Override default interpreter: "python3" | "bash"
    timeout: Optional[int] = None      # Per-script timeout in seconds


@dataclass
class SkillMetadata:
    """Tier 1: Only metadata loaded at startup.

    This is the lightweight representation used in skill catalog
    to minimize token consumption.
    """

    name: str
    description: str
    version: str = "1.0"
    category: str = ""
    target: str = ""
    required_tools: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    parameters: list[SkillParameter] = field(default_factory=list)
    scripts: list[ScriptInfo] = field(default_factory=list)


@dataclass
class Skill:
    """Full skill representation with all tiers loaded."""

    metadata: SkillMetadata
    instructions: str = ""  # Tier 2: SKILL.md Markdown body
    skill_dir: str = ""  # Path to skill directory for Tier 3 resource access
