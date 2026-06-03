"""CLI command: blade-ai capabilities sync"""

from __future__ import annotations

from pathlib import Path

import typer

from chaos_agent.cli.output import format_output
from chaos_agent.config.settings import settings
from chaos_agent.preflight import LIST_CHECKS, check_blade, run_command

CAPABILITIES_CHECKS = [*LIST_CHECKS, check_blade]


def _get_output_path() -> Path:
    return settings.resolved_memory_dir / "skill_capabilities.json"


def capabilities_sync(
    output: str = typer.Option("json", "--output", "-o", help="Output format: json|yaml"),
):
    """Sync skill capabilities: probe blade + LLM generate commands for each case.

    This is a slow command — it probes `blade -h` and calls LLM for each
    catalogue case. Run it when you add/remove/edit skill cases or upgrade blade.
    """

    async def _local(backend):
        from chaos_agent.agent.factory import make_llm
        from chaos_agent.skills.case_sync import sync_capabilities
        from chaos_agent.skills.loader import get_skills_dir

        blade_path = settings._resolve_blade_path()
        catalogue_root = get_skills_dir() / "k8s-chaos-skills" / "references" / "catalogue"

        if not catalogue_root.exists():
            return {
                "status": "error",
                "code": 1,
                "message": f"Catalogue not found: {catalogue_root}",
            }

        llm = make_llm(read_timeout=120, enable_thinking=False)
        out_path = _get_output_path()

        typer.echo("Syncing capabilities (this may take a minute)...", err=True)
        catalog = await sync_capabilities(blade_path, catalogue_root, llm, out_path)

        blade_count = sum(1 for c in catalog["cases"] if c["inject_kind"] == "blade")
        kubectl_count = sum(1 for c in catalog["cases"] if c["inject_kind"] == "kubectl")
        mixed_count = sum(1 for c in catalog["cases"] if c["inject_kind"] == "mixed")

        typer.echo(
            f"\n✓ Sync complete: {catalog['total']} cases "
            f"(blade={blade_count}, kubectl={kubectl_count}, mixed={mixed_count})\n"
            f"  Written to: {out_path}",
            err=True,
        )

        return {
            "status": "success",
            "code": 0,
            "message": "success",
            "data": {
                "total": catalog["total"],
                "blade_version": catalog["blade_version"],
                "blade_count": blade_count,
                "kubectl_count": kubectl_count,
                "mixed_count": mixed_count,
                "output_path": str(out_path),
            },
        }

    async def _server(backend):
        return await backend.post("/api/v1/capabilities/sync", {})

    result = run_command(CAPABILITIES_CHECKS, _local, _server)
    typer.echo(format_output(result, output))


