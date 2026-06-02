"""CLI command: blade-ai list

Reads the pre-generated skill_capabilities.json (from `capabilities-sync`)
and outputs it. Fast (reads one JSON file), no blade/LLM/agent calls.
"""

import json

import typer

from chaos_agent.cli.output import format_output
from chaos_agent.config.settings import settings
from chaos_agent.skills.loader import get_skills_dir


def list_command(
    output: str = typer.Option("json", "--output", "-o", help="Output format: json|yaml"),
):
    """List supported fault capabilities (from last sync)."""
    primary = settings.resolved_memory_dir / "skill_capabilities.json"
    default = get_skills_dir() / "k8s-chaos-skills" / "references" / "skill_capabilities.default.json"

    src = primary if primary.exists() else default

    if not src.exists():
        result = {
            "status": "success",
            "code": 0,
            "message": "No capabilities data found. Run `blade-ai capabilities-sync` to generate.",
            "data": {"total": 0, "categories": []},
        }
        typer.echo(format_output(result, output))
        return

    try:
        catalog = json.loads(src.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        result = {
            "status": "error",
            "code": 1,
            "message": f"Failed to read capabilities file ({src}): {e}. Try `blade-ai capabilities-sync` to regenerate.",
            "data": {"total": 0, "categories": []},
        }
        typer.echo(format_output(result, output))
        return
    cases = catalog.get("cases", [])

    cats: dict = {}
    for c in cases:
        cat = c.get("category", "unknown")
        cats.setdefault(cat, {"category": cat, "description": f"{cat} 故障注入用例", "faults": []})
        cats[cat]["faults"].append(c)

    result = {
        "status": "success",
        "code": 0,
        "message": "success",
        "data": {
            "total": len(cases),
            "blade_version": catalog.get("blade_version", ""),
            "categories": list(cats.values()),
        },
    }
    typer.echo(format_output(result, output))
