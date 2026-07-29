"""Contracts for environment-specific agent capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EnvironmentProfile:
    """Stable description of an environment in which a fault can run.

    The text is intentionally capability-oriented.  Concrete tool schemas
    remain the source of truth for arguments; this fragment tells the model
    which kind of target authority and observations are meaningful.
    """

    profile_id: str
    target_authority: str
    phase_fragments: dict[str, str]

    def prompt_fragment(self, phase: str) -> str:
        return self.phase_fragments.get(phase, self.phase_fragments.get("default", ""))


class EnvironmentProfileResolver(Protocol):
    """Optional protocol for future dynamic environment implementations."""

    profile_id: str

    def prompt_fragment(self, phase: str) -> str: ...
