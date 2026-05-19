"""TaskTracker — active task lifecycle management."""

from __future__ import annotations

import logging

from chaos_agent.tui.state import SessionState

logger = logging.getLogger(__name__)


class TaskTracker:
    """Tracks active task state and surfaces interrupted tasks on startup."""

    def __init__(self, state: SessionState, runner, renderer=None) -> None:
        self._state = state
        self._runner = runner
        self._renderer = renderer
        self._injection_active: bool = False

    @property
    def injection_active(self) -> bool:
        return self._injection_active

    def mark_injection_active(self) -> None:
        self._injection_active = True
        self._state.set_active_task_count(1)

    def mark_injection_done(self) -> None:
        self._injection_active = False
        self._state.set_active_task_count(0)

    async def recover_interrupted_tasks(self, conversation=None) -> None:
        """Surface interrupted tasks from the checkpoint store as system hints.

        Always renders the interrupted-tasks panel — even when no tasks
        exist, the panel is shown with a "no pending tasks" message.
        Does NOT auto-resume — the user picks up via /recover <task_id>.
        """
        if not self._runner:
            return

        try:
            interrupted = await self._runner.list_interrupted_tasks()
            self._render_interrupted(interrupted or [])
        except Exception as e:
            logger.warning(f"Failed to check interrupted tasks: {e}")

    def _render_interrupted(self, tasks: list[dict]) -> None:
        if self._renderer is not None:
            try:
                self._renderer.interrupted_tasks(tasks)
            except Exception:
                pass

    def _post_system(self, message: str) -> None:
        if self._renderer is not None:
            try:
                self._renderer.system(message)
            except Exception:
                pass
