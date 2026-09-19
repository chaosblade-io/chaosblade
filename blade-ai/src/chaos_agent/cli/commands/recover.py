"""CLI command: blade-ai recover"""

import json
import sys
from typing import Optional

import typer

from chaos_agent.cli.output import OutputFormat, format_output
from chaos_agent.config.settings import settings
from chaos_agent.preflight import RECOVER_CHECKS, exit_for_envelope, run_command


async def _drain_recover_stream(events) -> dict:
    """Render streaming recover events to the terminal, return the envelope.

    Event rendering mirrors inject.py's --stream branch: LLM token output
    on stdout (final answer stays pipeable), progress/tool events on
    stderr, and the terminal ``result`` event as the machine-readable
    envelope consumed by format_output downstream.
    """
    final_result = None
    async for event in events:
        if event.type == "thinking":
            if settings.is_debug:
                sys.stderr.write(f"\033[90m{event.content}\033[0m")
                sys.stderr.flush()
        elif event.type == "token":
            sys.stdout.write(event.content)
            sys.stdout.flush()
        elif event.type == "node_message":
            # Programmatic status text from recover graph nodes — stderr
            # so stdout stays clean for LLM token output.
            sys.stderr.write(event.content)
            sys.stderr.flush()
        elif event.type == "tool_start":
            typer.echo(f"\n  ⏳ Calling tool: {event.tool_name}", err=True)
        elif event.type == "tool_end":
            content = event.content
            if len(content) > 500:
                content = content[:500] + "..."
            typer.echo(f"  ✓ {event.tool_name}: {content}", err=True)
        elif event.type == "result":
            try:
                final_result = json.loads(event.content)
            except json.JSONDecodeError:
                # A malformed result event must degrade to an error
                # envelope, not a bare traceback out of the CLI.
                final_result = {
                    "code": 1,
                    "message": "Received a malformed result event from the agent",
                    "data": None,
                }
        elif event.type == "error":
            typer.echo(f"\n❌ Error: {event.content}", err=True)
    return final_result or {"code": 1, "message": "No result received", "data": None}


def recover_command(
    task_id: str = typer.Option(..., "--task-id", help="Task ID to recover"),
    target_name: Optional[str] = typer.Option(None, "--target-name", "-n", help="Specific target"),
    force: bool = typer.Option(False, "--force", help="Force recovery"),
    stream: bool = typer.Option(False, "--stream", help="Stream output in real-time"),
    output: OutputFormat = typer.Option(OutputFormat.json, "--output", "-o", help="Output format: json|yaml"),
):
    """Recover a fault injection by task ID."""

    async def _local(backend):
        if stream:
            return await _drain_recover_stream(
                backend.recover_stream(task_id, target_name=target_name, force=force)
            )
        return await backend.recover(task_id, target_name=target_name, force=force)

    async def _server(backend):
        if stream:
            return await _drain_recover_stream(
                backend.recover_stream(task_id, target_name=target_name, force=force)
            )
        return await backend.recover(task_id, target_name=target_name, force=force)

    result = run_command(RECOVER_CHECKS, _local, _server)
    typer.echo(format_output(result, output))
    exit_for_envelope(result)
