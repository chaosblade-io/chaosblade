"""CLI command: blade-ai list"""

from typing import Optional

import typer

from chaos_agent.cli.output import format_output
from chaos_agent.preflight import LIST_CHECKS, run_command


def list_command(
    category: Optional[str] = typer.Option(None, "--category", "-c", help="Filter by category"),
    target_type: Optional[str] = typer.Option(None, "--target-type", help="Filter by target type"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Force regenerate, bypass cache"),
    output: str = typer.Option("json", "--output", "-o", help="Output format: json|yaml"),
):
    """List supported fault capabilities."""

    async def _local(backend):
        return await backend.list_skills(
            category=category, target_type=target_type, no_cache=no_cache)

    async def _server(backend):
        return await backend.list_skills(
            category=category, target_type=target_type, no_cache=no_cache)

    result = run_command(LIST_CHECKS, _local, _server)
    typer.echo(format_output(result, output))
