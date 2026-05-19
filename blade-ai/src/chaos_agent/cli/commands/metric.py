"""CLI command: blade-ai metric"""

import asyncio

import typer

from chaos_agent.cli.config_manager import get_backend
from chaos_agent.cli.output import format_output


def metric_command(
    task_id: str = typer.Option("", "--task-id", help="Task ID (omit to list all tasks)"),
    output: str = typer.Option("json", "--output", "-o", help="Output format: json|yaml"),
):
    """Query task status and execution metrics.

    Without --task-id, lists ALL tasks with status and metrics summary.

    With --task-id, shows detailed status (task_state, phase, verification,
    etc.) and execution metrics (spans, token usage, tool calls) for a
    specific task. This replaces the former 'status' command.
    """
    backend = get_backend()

    async def _run():
        try:
            return await backend.metric(task_id)
        finally:
            await backend.cleanup()

    result = asyncio.run(_run())
    typer.echo(format_output(result, output))
