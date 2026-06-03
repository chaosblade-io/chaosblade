"""TUI internal events — plain @dataclass, framework-agnostic."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TUIEvent:
    """Base class for all TUI internal events."""


# ---------------------------------------------------------------------------
# Chat / LLM streaming events
# ---------------------------------------------------------------------------


@dataclass
class TokenReceived(TUIEvent):
    """LLM token streamed to chat area."""

    content: str = ""
    node: str = ""


@dataclass
class ThinkingReceived(TUIEvent):
    """LLM thinking/reasoning content."""

    content: str = ""
    node: str = ""


@dataclass
class ToolStarted(TUIEvent):
    """A tool call has started."""

    tool_name: str = ""
    node: str = ""


@dataclass
class ToolCompleted(TUIEvent):
    """A tool call has completed."""

    tool_name: str = ""
    content: str = ""
    node: str = ""


# ---------------------------------------------------------------------------
# Interrupt events (confirmation gate + ask_human)
# ---------------------------------------------------------------------------


@dataclass
class InterruptRequired(TUIEvent):
    """Graph paused at an interrupt point, needs user input."""

    interrupt_info: dict = field(default_factory=dict)
    task_id: str = ""


@dataclass
class TaskResumed(TUIEvent):
    """An interrupted task is being resumed (crash recovery)."""

    task_id: str = ""
    interrupt_info: Optional[dict] = None


# ---------------------------------------------------------------------------
# Task lifecycle events
# ---------------------------------------------------------------------------


@dataclass
class TaskResult(TUIEvent):
    """Task completed with final result."""

    data: dict = field(default_factory=dict)
    task_id: str = ""


@dataclass
class TaskError(TUIEvent):
    """Task failed with error."""

    message: str = ""
    task_id: str = ""


# ---------------------------------------------------------------------------
# Status / progress events
# ---------------------------------------------------------------------------


@dataclass
class PhaseChanged(TUIEvent):
    """Execution phase changed (e.g., planning -> safety_check)."""

    phase: str = ""
    source: str = ""
    message: str = ""


@dataclass
class ProgressUpdate(TUIEvent):
    """Progress update during execution."""

    source: str = ""
    message: str = ""
    duration_ms: int = 0


@dataclass
class PhaseCompleted(TUIEvent):
    """Execution phase completed."""

    source: str = ""
    duration_ms: int = 0


@dataclass
class PhaseFailed(TUIEvent):
    """Execution phase failed."""

    source: str = ""
    error: str = ""


@dataclass
class RecoveryTriggered(TUIEvent):
    """Recovery process triggered (interrupt or timeout)."""

    task_id: str = ""
    reason: str = ""


# ---------------------------------------------------------------------------
# UI interaction events
# ---------------------------------------------------------------------------


@dataclass
class PreflightAction(TUIEvent):
    """User selected a preflight action (install_helm/install_kubectl/skip)."""

    action: str = ""


@dataclass
class PermissionModeChanged(TUIEvent):
    """Permission mode has been changed via /plan or Shift+Tab."""

    new_mode: str = ""
