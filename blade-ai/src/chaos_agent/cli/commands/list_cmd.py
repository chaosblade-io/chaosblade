"""CLI command: blade-ai list

Reads a registry produced by `capabilities-sync` and outputs it. Registries are
per language, so this language-agnostic view takes zh first when both exist and
reports the chosen language and file in its payload.
Fast (reads one JSON file), no blade/LLM/agent calls.
"""

import json

import typer

from chaos_agent.cli.output import OutputFormat, format_output
from chaos_agent.skills.capabilities import existing_local_registries
from chaos_agent.skills.loader import resolve_skill_dir


def list_command(
    output: OutputFormat = typer.Option(OutputFormat.json, "--output", "-o", help="Output format: json|yaml"),
):
    """List supported fault capabilities (from last sync)."""
    # Language-agnostic display command: take whichever local registry exists
    # (zh first) instead of picking a locale, and fall back to the checked-in
    # default so the command still works before any sync has run.
    local_files = existing_local_registries()
    k8s_dir = resolve_skill_dir("k8s-chaos-skills")
    default = (
        k8s_dir / "references" / "skill_capabilities.default.json"
        if k8s_dir else None
    )

    src = local_files[0] if local_files else default

    if src is None or not src.exists():
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
        cats.setdefault(cat, {"category": cat, "description": f"{cat} fault-injection use cases", "faults": []})
        cats[cat]["faults"].append(c)

    result = {
        "status": "success",
        "code": 0,
        "message": "success",
        "data": {
            "total": len(cases),
            "blade_version": catalog.get("blade_version", ""),
            # Registries are per language now, so "the last sync" is ambiguous
            # the moment two files exist: this command takes zh first and would
            # otherwise report an en-only sync's shape in zh, or vice versa,
            # with no way for the caller to tell. Name what was actually read.
            "lang": catalog.get("lang", ""),
            "source": str(src),
            "categories": list(cats.values()),
        },
    }
    typer.echo(format_output(result, output))
