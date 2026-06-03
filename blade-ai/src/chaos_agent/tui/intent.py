"""IntentRouter — minimal TUI-level intent classification.

Only distinguishes three types: slash command, exit, and agent input.
Fine-grained intent classification (inject vs recover vs query vs chat)
is delegated to the LangGraph intent_clarification node where the LLM
can make much better decisions.
"""

from __future__ import annotations

import re
from enum import Enum


class IntentType(Enum):
    """TUI-level intent types (minimal set)."""

    SLASH_COMMAND = "slash_command"
    EXIT = "exit"
    AGENT_INPUT = "agent_input"


_EXIT_PATTERNS = re.compile(
    r"^(退出|exit|quit|再见|bye|goodbye)\s*$",
    re.IGNORECASE,
)


class IntentRouter:
    """Minimal TUI-level intent classifier.

    Only routes slash commands and exit requests locally.
    Everything else goes to the LangGraph agent for classification.
    """

    def classify(self, text: str) -> IntentType:
        text = text.strip()
        if text.startswith("/"):
            return IntentType.SLASH_COMMAND
        if _EXIT_PATTERNS.search(text):
            return IntentType.EXIT
        return IntentType.AGENT_INPUT
