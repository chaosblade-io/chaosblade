"""CLI command: blade-ai version"""

import asyncio

import typer

from chaos_agent import __version__
from chaos_agent.cli.config_manager import get_backend, get_mode
from chaos_agent.cli.output import format_output


def version_command(
    output: str = typer.Option("json", "--output", "-o", help="Output format: json|yaml"),
):
    """Show version information."""
    backend = get_backend()

    # Wrap in try/finally so backend.cleanup() runs after version() returns.
    # Without this, AgentRunner's aiosqlite checkpointer + TaskStore backend
    # threads stay alive and the process hangs after printing the JSON
    # (asyncio.run shuts down the loop, but non-daemon worker threads hold
    # the interpreter). Mirrors the pattern in metric.py / preflight.run_command.
    # AgentClient.cleanup is a no-op so this is safe in server mode too.
    async def _run():
        try:
            return await backend.version()
        finally:
            await backend.cleanup()

    result = asyncio.run(_run())

    # Fallback for server mode when server is unreachable
    if get_mode() == "server" and result.get("code") == 5001:
        typer.echo(
            format_output(
                {
                    "code": 0,
                    "message": "success",
                    "data": {
                        "version": __version__,
                        "supported_fault_count": "N/A (server not available)",
                    },
                },
                output,
            )
        )
        return

    typer.echo(format_output(result, output))
