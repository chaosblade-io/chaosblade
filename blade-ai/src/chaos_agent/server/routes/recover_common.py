"""Shared setup logic for the streaming recover endpoint."""

import logging

from chaos_agent.config.settings import settings
from chaos_agent.models.schemas import JSONEnvelope, ResponseCode

logger = logging.getLogger(__name__)


class RecoverSetupError(Exception):
    """Raised when recover initial state cannot be built."""

    def __init__(self, envelope):
        self.envelope = envelope
        super().__init__(str(envelope))


async def build_recover_initial_state(
    agents: dict,
    inject_task_id: str,
    record_task_id: str,
    req_id: str = "",
):
    """Build recover graph initial state from inject graph checkpoint.

    Returns:
        (initial_state, state_values) tuple.
    Raises:
        RecoverSetupError with a JSONEnvelope if state cannot be resolved.
    """
    config = {
        "configurable": {"thread_id": inject_task_id},
        "recursion_limit": settings.recursion_limit,
    }

    current_state = await agents["pipeline"].aget_state(config)
    checkpoint_values = current_state.values if current_state and current_state.values else {}

    from chaos_agent.agent.task_snapshot import resolve_recover_initial_state

    resolution = await resolve_recover_initial_state(
        inject_task_id,
        record_task_id=record_task_id,
        agents=agents,
        checkpoint_values=checkpoint_values,
    )
    if resolution is None:
        raise RecoverSetupError(
            JSONEnvelope.fail(
                code=ResponseCode.TASK_NOT_FOUND,
                message=f"Task not found: {inject_task_id}",
                request_id=req_id,
            )
        )

    return resolution.initial_state, resolution.source_values
