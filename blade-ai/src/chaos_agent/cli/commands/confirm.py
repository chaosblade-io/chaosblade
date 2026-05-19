"""CLI command: blade-ai confirm"""

from typing import Optional

import typer

from chaos_agent.cli.output import format_output
from chaos_agent.preflight import CONFIRM_CHECKS, run_command


def confirm_command(
    task_id: str = typer.Option(..., "--task-id", help="Task ID"),
    action: str = typer.Option(..., "--action", "-a", help="approve or reject"),
    reason: Optional[str] = typer.Option(None, "--reason", help="Reason"),
    output: str = typer.Option("json", "--output", "-o", help="Output format: json|yaml"),
):
    """Confirm or reject a pending task."""

    async def _local(backend):
        return await backend.confirm(task_id, action, reason or "")

    async def _server(backend):
        return await backend.confirm(task_id, action, reason or "")

    result = run_command(CONFIRM_CHECKS, _local, _server)
    typer.echo(format_output(result, output))
