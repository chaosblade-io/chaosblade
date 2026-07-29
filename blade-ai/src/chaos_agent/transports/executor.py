"""Unified transport executor with Guard + Audit integration.

``execute_via_transport`` is the single entry point for all command
execution through transport channels.  It:

1. Guard-checks the **raw** semantic command (not the wrapped one)
2. Resolves the transport channel from the target
3. Runs preflight checks
4. Wraps the command and executes via ``run_command(skip_guard=True)``
5. Adapts the result (wiz protocol parsing / passthrough)
6. Writes the audit log with the raw semantic command

This ensures:
- ToolGuard always sees the semantic intent (kubectl/iptables/etc.),
  never the transport wrapper (wiz/ssh)
- Audit logs record what was intended, not how it was transported
- StatusEvent and SessionStore (inside run_command) still record the
  actual OS-level command for operational observability
"""

from __future__ import annotations

from chaos_agent.errors import ToolGuardError
from chaos_agent.models.command_result import CommandResult

from .base import TransportTarget
from .registry import (
    PROFILE_HOST,
    PROFILE_K8S,
    TransportRegistry,
    profile_of,
)

# Sentinel exit code returned when a channel's preflight fails (i.e. the
# transport itself is misconfigured, before the inner command ever runs).
# Distinct from a real command failure so callers can tell "channel not
# configured" apart from "command executed and returned non-zero".
PREFLIGHT_FAILED_EXIT_CODE = -1

# Sentinel for the profile gate below. Distinct from PREFLIGHT_FAILED so a
# caller can tell "this command shape cannot travel this channel" apart from
# "the channel is misconfigured".
PROFILE_MISMATCH_EXIT_CODE = -2

# Why a mismatch is refused rather than attempted — task-46317228: a
# host-profile ``uptime`` was dispatched through the ``kubewiz_k8s`` channel,
# which addresses a CLUSTER (``--cluster-uuid``, no ``--name``). The KubeWiz
# platform ran it on its own executor pod (``kubewiz-executor-...``) and
# returned ``load average 0.02`` — a syntactically fine, semantically wrong
# answer from an unrelated machine. Nothing failed, so nothing warned, and the
# verifier spent most of its budget reconciling that number against the target
# node's real 90% CPU. Returning an error beats returning wrong data.
_PROFILE_MISMATCH_HINTS = {
    PROFILE_HOST: (
        "A host-profile command must reach one specific machine. A 'k8s' "
        "profile channel addresses a CLUSTER (kubeconfig context / "
        "--cluster-uuid), not a machine, so this command would run wherever "
        "that channel's executor lives — not on your target host. To observe "
        "or act on a Kubernetes node use the kubectl tools; to reach a "
        "specific machine configure a host channel (ssh / kubewiz_host)."
    ),
    PROFILE_K8S: (
        "A k8s-profile command needs cluster access. A 'host' profile channel "
        "(ssh / kubewiz_host) delivers a plain shell on a single machine and "
        "cannot serve kubectl/blade cluster operations. Configure a k8s "
        "channel (kubeconfig / kubewiz_k8s)."
    ),
}


async def execute_via_transport(
    cmd: list[str],
    target: TransportTarget,
    timeout: float = 60,
    task_id: str = "",
    stdin_data: str = "",
    env_override: dict[str, str] | None = None,
    source: str = "",
    skip_guard: bool = False,
    bypass_channel: bool = False,
    expect_profile: str = "",
    audit: bool | None = None,
) -> CommandResult:
    """Execute a command through the transport layer.

    Args:
        cmd: Raw semantic command (e.g. ``["kubectl", "get", "pods"]``).
        target: Transport target with connection parameters.
        timeout: Per-command timeout in seconds.
        task_id: Task ID for audit logging and status tracking.
        stdin_data: If non-empty, piped to the subprocess stdin.
        env_override: If provided, merged into the subprocess environment.
        source: Override the status tracker source name (e.g.
            ``"verifier-L1"``).  Defaults to ``cmd[0]`` basename when
            empty — pass a descriptive name to distinguish programmatic
            pre-checks from LLM-initiated tool calls in status events.
        skip_guard: When True, bypass both the ToolGuard check and the audit
            log.  For internal, high-frequency probes (e.g. feasibility
            checks) that opt out of guard machinery — mirrors the old
            ``run_command(skip_guard=True)`` behavior.
        bypass_channel: When True, skip channel resolution / preflight / wrap /
            adapt and run the raw command locally (passthrough result).  For
            commands that reach the target through their OWN native mechanism
            rather than the transport wrapper — notably ``blade`` in kubewiz
            modes, which connects to KubeWiz Core via its own ``--kubewiz-url``
            flags and must NOT be re-wrapped in ``wiz task exec`` (that would
            double-route).  ToolGuard check + audit still apply.
        expect_profile: Capability profile this command's SHAPE requires
            (``PROFILE_HOST`` for a bare shell diagnostic/fault on one machine,
            ``PROFILE_K8S`` for kubectl/blade cluster operations).  When set and
            the resolved channel's profile differs, the command is REFUSED
            before dispatch — see ``_PROFILE_MISMATCH_HINTS`` for why silence
            here is worse than an error.  Empty means "do not check" (kept for
            call sites where the shape genuinely depends on runtime data).
        audit: Write the audit-log record, independent of ``skip_guard``.
            Defaults to ``not skip_guard``.  The two were fused, which silently
            dropped LLM-facing read-only tools (``host_read``, and a read-only
            ``host_inject``) from the audit trail: they skip the guard because
            the diag binaries sit outside ``ALLOWED_COMMANDS``, not because they
            are internal probes.  Pass ``audit=True`` to keep an
            LLM-originated command on the record while still skipping the check.

    Returns:
        CommandResult with the inner command's exit_code, stdout, stderr.

    Raises:
        ToolGuardError: If the raw command is blocked by ToolGuard.
    """
    # Lazy import to avoid circular dependency:
    # transports → tools.shell → tools.__init__ → tools.blade → transports
    from chaos_agent.tools.guard_gateway import get_guard_gateway
    from chaos_agent.tools.shell import get_tool_guard, run_command

    # 1. Guard check on raw semantic command (no transport wrapper).
    # skip_guard=True is for internal, high-frequency probes (e.g. feasibility
    # checks) that opt out of both the guard check and the audit log — it
    # restores the pre-transport ``run_command(skip_guard=True)`` semantics.
    guard = get_tool_guard()
    if not skip_guard:
        feedback = get_guard_gateway().check_command(cmd)
        if not feedback.allowed:
            raise ToolGuardError(feedback.render_for_llm())

    # 1b. Profile gate — runs BEFORE channel resolution / preflight / wrap so a
    # mismatched command is never dispatched, not even in the
    # ``bypass_channel`` path (that path skips the wrapper, but the profile
    # semantics still hold: a host shell command reaches nothing useful through
    # a cluster-addressing channel).
    if expect_profile:
        # Resolve from the TARGET, not from a state dict — ``resolve_channel_name``
        # takes state and would re-derive the target from settings, discarding
        # any per-call target the caller built.
        #
        # When resolution itself fails the channel is MISCONFIGURED, which is a
        # different problem with a better message: step 2 below returns the
        # ValueError verbatim ("host scope requires host_name or ssh_host")
        # instead of this gate's vaguer "channel is 'unknown'". Defer to it —
        # nothing is dispatched either way, so deferring costs no safety.
        try:
            actual_channel = TransportRegistry.resolve(target).name
        except ValueError:
            actual_channel = ""
        if actual_channel:
            actual_profile = profile_of(actual_channel)
            if actual_profile != expect_profile:
                hint = _PROFILE_MISMATCH_HINTS.get(expect_profile, "")
                return CommandResult(
                    exit_code=PROFILE_MISMATCH_EXIT_CODE,
                    stdout="",
                    stderr=(
                        f"refused: this command requires a '{expect_profile}' "
                        f"profile channel, but the configured channel is "
                        f"'{actual_channel}' (profile '{actual_profile}'). "
                        f"{hint}"
                    ).strip(),
                    duration_ms=0,
                )

    # 2. Channel selection + preflight (skipped when bypass_channel — the
    # command reaches its target natively and must run locally, unwrapped).
    # resolve() raises ValueError on an unknown channel_override / under-
    # specified scope.  Entry points validate kube_connection_mode, but harden
    # the hot path too: surface it as a clean PREFLIGHT_FAILED result instead
    # of an uncaught traceback (mirrors preflight.py / resolve_channel_name).
    if bypass_channel:
        channel = None
    else:
        try:
            channel = TransportRegistry.resolve(target)
        except ValueError as exc:
            return CommandResult(
                exit_code=PREFLIGHT_FAILED_EXIT_CODE,
                stdout="",
                stderr=str(exc),
                duration_ms=0,
            )
        errors = channel.preflight(target)
        if errors:
            return CommandResult(
                exit_code=PREFLIGHT_FAILED_EXIT_CODE,
                stdout="",
                stderr="; ".join(errors),
                duration_ms=0,
            )

    # 3. Wrap command + execute (skip_guard=True — already checked in step 1)
    wrapped = channel.wrap_command(cmd, target, timeout=timeout) if channel is not None else cmd
    result = await run_command(
        wrapped,
        timeout=timeout,
        task_id=task_id,
        stdin_data=stdin_data,
        skip_guard=True,
        env_override=env_override,
        source=source,
        # Record WHERE this ran. ``source`` is a semantic label chosen by the
        # caller, so without this the status event cannot say which machine
        # answered — the fact task-46317228 turned on.
        channel=channel.name if channel is not None else "local",
    )

    # 4. Parse transport output protocol (wiz / passthrough).  Native-bypass
    # commands run unwrapped, so their output needs no protocol adaptation.
    if channel is not None:
        result = channel.adapt_result(result, target)

    # 5. Audit log — record the raw semantic command (more useful for auditing).
    # Skipped for opted-out internal probes to avoid audit-trail pollution;
    # ``audit`` overrides that for guard-skipping LLM-facing tools.
    should_audit = not skip_guard if audit is None else audit
    if should_audit:
        guard.audit_log(cmd, result, task_id)

    return result


def display_via_transport(cmd: list[str], target: TransportTarget) -> str:
    """Return a human-readable command string for display.

    Strips the transport wrapper (wiz/ssh) but appends the execution location,
    so the semantic command stays readable AND the destination stays visible.
    Channels written before the ``target`` parameter existed still work.
    """
    channel = TransportRegistry.resolve(target)
    try:
        return channel.display_command(cmd, target)
    except TypeError:
        # Legacy channel with the single-argument signature.
        return channel.display_command(cmd)
