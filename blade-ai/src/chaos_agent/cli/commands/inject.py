"""CLI command: blade-ai inject"""

import json
import sys
from typing import Optional

import typer

from chaos_agent.cli.output import format_output
from chaos_agent.config.settings import settings
from chaos_agent.preflight import INJECT_CHECKS, run_command


def inject_command(
    scope: Optional[str] = typer.Option(None, "--scope", help="ChaosBlade scope: node/pod/container"),
    target: Optional[str] = typer.Option(None, "--target", help="ChaosBlade target: cpu/network/disk/mem/process"),
    action: Optional[str] = typer.Option(None, "--action", help="ChaosBlade action: fullload/delay/loss/fill/kill/..."),
    target_name: Optional[str] = typer.Option(None, "--target-name", "-n", help="Resource name(s)"),
    namespace: Optional[str] = typer.Option(None, "--namespace", "--ns", help="K8s namespace"),
    duration: int = typer.Option(600, "--duration", "-d", help="Duration in seconds"),
    params: Optional[str] = typer.Option(None, "--params", "-p", help="Key=value params and boolean flags"),
    confirm: bool = typer.Option(False, "--confirm", help="Require confirmation"),
    labels: Optional[str] = typer.Option(None, "--labels", "-l", help="Custom labels"),
    input: Optional[str] = typer.Option(None, "--input", "-i", help="Natural language description"),
    direct: bool = typer.Option(False, "--direct", help="Skip LLM, execute blade directly"),
    kubeconfig: Optional[str] = typer.Option(None, "--kubeconfig", help="Path to kubeconfig file"),
    context: Optional[str] = typer.Option(None, "--context", help="Kubeconfig context name"),
    force_override: bool = typer.Option(False, "--force-override", help="Force proceed when confirm_required (P1: same-action overlay)"),
    stream: bool = typer.Option(False, "--stream", help="Stream output in real-time (NL mode only)"),
    output: str = typer.Option("json", "--output", "-o", help="Output format: json|yaml"),
):
    """Inject a fault into a Kubernetes target.

    Provide either --input/-i for natural language mode, or all structured params
    (--scope, --target, --action, --target-name, --namespace).
    """
    # Duration auto-boost: ensure minimum duration for reliable verification
    # This is the TOP layer of the three-layer duration guarantee.
    if scope and target and action:
        from chaos_agent.utils.fault_type import ensure_min_duration
        effective = ensure_min_duration(duration, scope, target, action)
        if effective != duration:
            if duration == 0:
                typer.echo(
                    f"No --duration specified. Auto-setting to {effective}s "
                    f"for {scope}-{target}-{action} (ensures verification window).",
                    err=True,
                )
            else:
                typer.echo(
                    f"Warning: --duration {duration}s is below the recommended minimum "
                    f"for {scope}-{target}-{action} ({effective}s). "
                    f"Auto-adjusting for reliable verification.",
                    err=True,
                )
            duration = effective
    # Validate: NL mode or structured mode, not both missing
    has_input = bool(input)
    # Node-scope does not require --namespace (ChaosBlade ignores it for node scope)
    _required_fields = [scope, target, action]
    if scope != "node":
        _required_fields.append(namespace)
    has_structured = all(_required_fields) and (target_name or labels)
    if not has_input and not has_structured:
        _ns_hint = ", --namespace" if scope != "node" else ""
        typer.echo(
            f"Error: Provide either --input/-i or all of --scope, --target, --action, "
            f"(--target-name or --labels){_ns_hint}",
            err=True,
        )
        raise typer.Exit(code=1)

    # Validate: --direct not compatible with --input
    if direct and input:
        typer.echo("Error: --direct is not compatible with --input/-i", err=True)
        raise typer.Exit(code=1)

    # Validate: --direct requires complete structured params
    if direct and not has_structured:
        _ns_hint = ", --namespace" if scope != "node" else ""
        typer.echo(
            f"Error: --direct requires all of --scope, --target, --action, "
            f"(--target-name or --labels){_ns_hint}",
            err=True,
        )
        raise typer.Exit(code=1)

    # Validate: --stream requires --input
    if stream and not input:
        typer.echo("Error: --stream requires --input/-i (natural language mode)", err=True)
        raise typer.Exit(code=1)

    # Validate: scope must be valid if provided
    if scope and scope not in {"node", "pod", "container"}:
        typer.echo(f"Error: Invalid scope '{scope}', must be node/pod/container", err=True)
        raise typer.Exit(code=1)

    # Parse params (supports bare keys for boolean flags)
    params_dict = {}
    params_flags = []
    if params:
        for item in params.split(","):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                k, v = item.split("=", 1)
                params_dict[k.strip()] = v.strip()
            else:
                params_flags.append(item)  # bare key = boolean flag

    # Parse labels
    labels_dict = {}
    if labels:
        for pair in labels.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                labels_dict[k.strip()] = v.strip()

    request_data = {
        "scope": scope,
        "target": target,
        "action": action,
        "target_name": target_name,
        "namespace": namespace,
        "duration": duration,
        "params": params_dict or None,
        "params_flags": params_flags or None,
        "confirm": confirm,
        "labels": labels_dict or None,
        "direct": direct,
        "force_override": force_override,
    }

    if input:
        request_data["input"] = input

    if kubeconfig:
        request_data["kubeconfig"] = kubeconfig
    if context:
        request_data["context"] = context

    # ═══ Phase 1 + 2: preflight check + execution via run_command ═══
    async def _local(backend):
        if stream:
            # Streaming mode: print events in real-time
            final_result = None

            async def _confirm_cb(plan_summary: str) -> str:
                """Interactive confirmation callback for streaming mode."""
                typer.echo(f"\nPlan Summary:\n{plan_summary}\n", err=True)
                approved = typer.confirm("Approve this injection?", default=False)
                return "approved" if approved else "rejected"

            async for event in backend.inject_stream(
                confirm_callback=_confirm_cb if confirm else None,
                **request_data
            ):
                if event.type == "thinking":
                    if settings.is_debug:
                        sys.stderr.write(f"\033[90m{event.content}\033[0m")
                        sys.stderr.flush()
                elif event.type == "token":
                    sys.stdout.write(event.content)
                    sys.stdout.flush()
                elif event.type == "tool_start":
                    typer.echo(f"\n  ⏳ Calling tool: {event.tool_name}", err=True)
                elif event.type == "tool_end":
                    content = event.content
                    if len(content) > 500:
                        content = content[:500] + "..."
                    typer.echo(f"  ✓ {event.tool_name}: {content}", err=True)
                elif event.type == "confirm":
                    pass
                elif event.type == "result":
                    final_result = json.loads(event.content)
                elif event.type == "error":
                    typer.echo(f"\n❌ Error: {event.content}", err=True)
            return final_result or {"code": 1, "message": "No result received", "data": None}
        else:
            result = await backend.inject(**request_data)

            # Interactive confirmation flow
            if (
                confirm
                and result["code"] == 0
                and result.get("data", {}).get("needs_confirm")
            ):
                plan = result["data"].get("plan_summary", "No plan summary available")
                typer.echo(f"\nPlan Summary:\n{plan}\n")
                approved = typer.confirm("Approve this injection?", default=False)
                task_id = result["data"]["task_id"]

                if approved:
                    result = await backend.confirm(task_id, "approve")
                else:
                    result = await backend.confirm(task_id, "reject", "User rejected")

            return result

    async def _server(backend):
        return await backend.post("/api/v1/inject", request_data)

    result = run_command(INJECT_CHECKS, _local, _server)

    # ═══ Phase 3: output ═══
    typer.echo(format_output(result, output))
