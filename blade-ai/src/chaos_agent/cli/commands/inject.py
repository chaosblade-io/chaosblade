"""CLI command: blade-ai inject"""

import json
import sys
from typing import Optional

import typer

from chaos_agent.cli.output import OutputFormat, format_output
from chaos_agent.config.settings import settings
from chaos_agent.models.schemas import ResponseCode
from chaos_agent.preflight import INJECT_CHECKS, exit_for_envelope, run_command


def inject_command(
    scope: Optional[str] = typer.Option(None, "--scope", help="ChaosBlade scope: node/pod/container"),
    target: Optional[str] = typer.Option(None, "--target", help="ChaosBlade target: cpu/network/disk/mem/process"),
    action: Optional[str] = typer.Option(None, "--action", help="ChaosBlade action: fullload/delay/loss/fill/kill/..."),
    target_name: Optional[str] = typer.Option(None, "--target-name", "-n", help="Resource name(s)"),
    namespace: Optional[str] = typer.Option(None, "--namespace", "--ns", help="K8s namespace"),
    duration: Optional[int] = typer.Option(None, "--duration", "-d", help="Duration in seconds (omit in -i mode to let the agent extract it from the description)"),
    params: Optional[str] = typer.Option(None, "--params", "-p", help="Key=value params and boolean flags"),
    confirm: bool = typer.Option(False, "--confirm", help="Require confirmation"),
    labels: Optional[str] = typer.Option(None, "--labels", "-l", help="Custom labels"),
    input: Optional[str] = typer.Option(None, "--input", "-i", help="Natural language description"),
    kubeconfig: Optional[str] = typer.Option(None, "--kubeconfig", help="Path to kubeconfig file"),
    context: Optional[str] = typer.Option(None, "--context", help="Kubeconfig context name"),
    force_override: bool = typer.Option(False, "--force-override", help="Force proceed when confirm_required (P1: same-action overlay)"),
    stream: bool = typer.Option(False, "--stream", help="Stream output in real-time (NL mode only)"),
    output: OutputFormat = typer.Option(OutputFormat.json, "--output", "-o", help="Output format: json|yaml"),
):
    """Inject a fault into a Kubernetes target.

    Provide either --input/-i for natural language mode, or all structured params
    (--scope, --target, --action, --target-name, --namespace).
    """
    # Duration pre-fill: the TOP layer of the three-layer duration guarantee.
    # Structured mode only: in -i NL mode an unset duration must reach the
    # intent node as 0 so the value stated in natural language is extracted
    # there — a hardcoded CLI default would masquerade as a user-pinned
    # hard-pin and contradict the description (observed: "持续 300 秒"
    # intent arriving as duration_seconds=600).
    if scope and target and action:
        if duration is None:
            duration = 300
        from chaos_agent.utils.fault_type import ensure_min_duration
        effective = ensure_min_duration(duration, scope, target, action)
        if effective != duration:
            # Reachable only for a non-positive --duration (treated as
            # unspecified): ensure_min_duration injects the recommended
            # default. Explicit positive values pass through verbatim
            # (l4-contract-faithfulness) — a below-floor request is
            # honoured as-is with a warning inside ensure_min_duration,
            # so it never lands here.
            typer.echo(
                f"No --duration specified. Auto-setting to {effective}s "
                f"for {scope}-{target}-{action} (ensures verification window).",
                err=True,
            )
            duration = effective
    # Validate: NL mode or structured mode, not both missing
    has_input = bool(input)
    # Cluster-scoped faults (node / host …) are namespace-less — derive from the
    # fault registry so new namespace-less scopes don't need to touch the CLI.
    from chaos_agent.agent.spec.fault_registry import aggregate_cluster_scoped

    _namespace_optional = scope in aggregate_cluster_scoped()
    _required_fields = [scope, target, action]
    if not _namespace_optional:
        _required_fields.append(namespace)
    has_structured = all(_required_fields) and (target_name or labels)
    if not has_input and not has_structured:
        _ns_hint = "" if _namespace_optional else ", --namespace"
        typer.echo(
            f"Error: Provide either --input/-i or all of --scope, --target, --action, "
            f"(--target-name or --labels){_ns_hint}",
            err=True,
        )
        raise typer.Exit(code=1)

    # Validate: --stream requires --input
    if stream and not input:
        typer.echo("Error: --stream requires --input/-i (natural language mode)", err=True)
        raise typer.Exit(code=1)

    # Validate: scope must be valid if provided (derived from the fault registry)
    if scope:
        from chaos_agent.agent.spec.fault_registry import aggregate_scopes

        valid_scopes = aggregate_scopes()
        if scope not in valid_scopes:
            typer.echo(
                f"Error: Invalid scope '{scope}', must be one of: {', '.join(valid_scopes)}",
                err=True,
            )
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

    # NL mode without explicit -d: pass None through — from_cli_nl coerces
    # it to 0 (system-recommended channel), letting the intent node honour
    # the duration stated in the natural language description.
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

            async def _confirm_cb(confirm_payload) -> str:
                """Interactive confirmation callback for streaming mode.

                Receives the gate's interrupt payload (dict) — or a bare
                plan-summary string on legacy paths — and renders the
                widened-contract manifest entries verbatim when present,
                so the human approves what the CASE legislated, not just
                the plan prose.
                """
                from chaos_agent.agent.target_guard.mechanism_writes import (
                    format_mechanism_writes_for_display,
                )
                if isinstance(confirm_payload, dict):
                    summary = confirm_payload.get("plan_summary", "")
                    entries_block = format_mechanism_writes_for_display(
                        confirm_payload.get("mechanism_writes") or [],
                    )
                else:
                    summary = str(confirm_payload or "")
                    entries_block = ""
                typer.echo(f"\nPlan Summary:\n{summary}\n", err=True)
                if entries_block:
                    typer.echo(f"{entries_block}\n", err=True)
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
                elif event.type == "node_message":
                    # Programmatic status text from graph nodes (baseline
                    # capture progress, safety-check steps, verifier results).
                    # Write to stderr so stdout stays clean for LLM token output.
                    sys.stderr.write(event.content)
                    sys.stderr.flush()
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
        else:
            result = await backend.inject(**request_data)

            # Interactive confirmation flow. The paused run arrives as
            # AWAITING_CONFIRMATION (1003, status SUCCESS), not code 0 —
            # round-64 F3: the pre-fix gate ``code == 0`` could never fire
            # against the envelope the paused run actually produced, so the
            # two-phase confirm was dead on the non-stream path even once
            # ``needs_confirm`` rode the projection.
            if (
                confirm
                and result["code"] in (ResponseCode.OK, ResponseCode.AWAITING_CONFIRMATION)
                and (result.get("data") or {}).get("needs_confirm")
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
    exit_for_envelope(result)
