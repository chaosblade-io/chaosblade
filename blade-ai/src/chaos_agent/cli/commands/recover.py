"""CLI command: blade-ai recover"""

from typing import Optional

import typer

from chaos_agent.cli.output import format_output
from chaos_agent.preflight import RECOVER_CHECKS, run_command


def recover_command(
    task_id: str = typer.Option(..., "--task-id", help="Task ID to recover"),
    target_name: Optional[str] = typer.Option(None, "--target-name", "-n", help="Specific target"),
    force: bool = typer.Option(False, "--force", help="Force recovery"),
    output: str = typer.Option("json", "--output", "-o", help="Output format: json|yaml"),
):
    """Recover a fault injection by task ID."""

    async def _local(backend):
        return await backend.recover(task_id, target_name=target_name, force=force)

    async def _server(backend):
        return await backend.post("/api/v1/recover", {
            "task_id": task_id, "target_name": target_name, "force": force,
        })

    result = run_command(RECOVER_CHECKS, _local, _server)
    typer.echo(format_output(result, output))
