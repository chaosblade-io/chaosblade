"""Host-level command execution via transport channels.

Provides ``exec_host_command`` — a thin wrapper around
``execute_via_transport`` for running host-level shell commands
(iptables, stress-ng, dd, fallocate, etc.) through the appropriate
transport channel (kubewiz-host or SSH).

Also provides the two LLM-facing ``@tool`` bindings for the ``host_shell``
carrier:

- ``host_inject`` — Phase 2 host-native fault injection. Runs one fault
  command directly on the configured host. The command binary is gated by
  ``ToolGuard`` (inside ``execute_via_transport``), which whitelists the host
  fault binaries (iptables / tc / stress-ng / dd / fallocate / fio …) and
  rejects everything else; the tool call is additionally drift-guarded by the
  ``tool_screener`` (host-scope classification). Recovery is LLM-driven: the
  recover graph later executes the skill-case reverse command via this same
  tool — there is no artifact-based auto-reversal.
- ``host_read`` — read-only host diagnostics. Validated against the shared
  read-only classifier (``tools.readonly.is_readonly_host_command``) — the same
  vocabulary the kubectl-exec probe classifier uses, with argument-level guards
  for dual-use tools (``iptables -L`` read, ``iptables -A`` mutating; likewise
  ``ip``/``systemctl``/``mount``/``dmesg``) — then executed with
  ``skip_guard=True`` (the diag binaries are intentionally outside
  ``ToolGuard.ALLOWED_COMMANDS``).

Both tools resolve their transport target from the active session config via
``TransportTarget.from_state({})`` — the same bridge the blade tools use.
"""

from __future__ import annotations

import logging
import shlex

from langchain_core.tools import tool
from pydantic import Field

from chaos_agent.tools._strict_args import StrictToolArgs
from chaos_agent.tools._tool_profiles import profile_for_tool
from chaos_agent.tools.guard import CommandResult
from chaos_agent.transports import PROFILE_HOST, TransportTarget, execute_via_transport
from chaos_agent.transports.executor import PROFILE_MISMATCH_EXIT_CODE

logger = logging.getLogger(__name__)

# Both host tools run on the machine the CONFIGURED CHANNEL addresses; neither
# can be re-pointed per call. task-46317228: the LLM passed
# ``node=<target node>`` on all eight ``host_read`` calls, LangChain dropped it,
# and the command silently ran on the KubeWiz platform executor instead. The
# hint has to name the correct alternative, or a rejection is no more useful
# than the silent drop it replaces.
#
# And the correct alternative DEPENDS ON THE SESSION. On a host channel these
# tools are the right ones and the fix is to drop the argument; pointing at
# ``kubectl_read`` there would name a tool the capability gate refuses on that
# very session — one dead end leading to another. That branch is in fact the
# reachable one: on a k8s channel the runtime screen refuses ``host_read``
# before its arguments are ever validated.
_TARGETING_PREFIX = (
    "This tool runs on the machine addressed by the configured transport "
    "channel and cannot be redirected per call. "
)
_TARGETING_ON_HOST = (
    "This session is already connected to that machine, so drop the parameter "
    "and run the command as it is. To reach a DIFFERENT machine the channel "
    "itself must be reconfigured — there is no per-call target."
)
_TARGETING_ON_CLUSTER = (
    "To observe a specific Kubernetes node use kubectl_read (e.g. `kubectl top "
    "node <name>`, `kubectl describe node <name>`); to act on a specific "
    "machine, configure a host channel (ssh / kubewiz_host) pointing at that "
    "machine."
)


def _targeting_advice() -> str:
    """Pick the alternative that is actually usable in the current session."""
    from chaos_agent.transports import PROFILE_HOST, profile_of, resolve_channel_name

    try:
        on_host = profile_of(resolve_channel_name()) == PROFILE_HOST
    except Exception:  # never let advice construction break a rejection
        on_host = False
    return _TARGETING_PREFIX + (_TARGETING_ON_HOST if on_host else _TARGETING_ON_CLUSTER)


class _HostTargetingArgs(StrictToolArgs):
    """Shared base: refuse a per-call target and explain the usable alternative."""

    @classmethod
    def unknown_key_advice(cls) -> str:
        return _targeting_advice()


class _HostReadArgs(_HostTargetingArgs):
    tool_display_name = "host_read"

    command: str = Field(description="One read-only diagnostic command, no shell metacharacters.")
    timeout: int = Field(default=30, description="Max seconds to wait.")
    task_id: str = Field(default="", description="Internal task id; leave unset.")


class _HostInjectArgs(_HostTargetingArgs):
    tool_display_name = "host_inject"

    command: str = Field(description="One host fault-injection command.")
    timeout: int = Field(default=60, description="Max seconds to wait.")
    task_id: str = Field(default="", description="Internal task id; leave unset.")


async def exec_host_command(
    binary: str,
    args: list[str],
    target: TransportTarget,
    timeout: float = 60,
    task_id: str = "",
) -> CommandResult:
    """Execute a host-level shell command via the transport layer.

    Args:
        binary: Host binary to execute (e.g. ``"iptables"``, ``"stress-ng"``).
        args: Arguments to pass to the binary.
        target: TransportTarget carrying host connection parameters
            (``host_name`` for kubewiz-host, ``ssh_*`` for SSH).
        timeout: Command timeout in seconds.
        task_id: Task identifier for observability/audit.

    Returns:
        CommandResult with stdout, stderr, exit_code.
    """
    cmd = [binary] + args
    # Host-only by construction (the binaries above exist only on a host
    # shell), so assert the resolved channel really is one. Currently no
    # caller, but it is a ready-made bypass of the profile gate otherwise.
    return await execute_via_transport(
        cmd, target, timeout=timeout, task_id=task_id,
        expect_profile=PROFILE_HOST,
    )


@tool(args_schema=_HostInjectArgs)
async def host_inject(command: str, timeout: int = 60, task_id: str = "") -> str:
    """Phase 2 ONLY. Execute ONE host-native fault command on the target host.

    Runs on the machine addressed by the CONFIGURED transport channel
    (ssh / kubewiz_host) — it CANNOT be pointed at a different machine per
    call (no node/host/pod parameter; passing one is refused).

    Mutating: runs a real fault command (iptables / tc / stress-ng / dd /
    fallocate / fio …) directly on the configured host, bypassing
    ChaosBlade and kubectl. Use ONLY when the approved target is a host
    (``scope=host``) and the skill case prescribes a native command; K8s
    faults → blade_create / kubectl.

    When to use:
      - Phase 2 host-scope injection prescribed by the skill case.

    Safety: the binary is checked against the host fault whitelist by the
    tool guard — non-fault binaries (rm, curl, systemctl, …) rejected. The
    call is drift-guarded against the approved host target. Recovery is
    LLM-driven: the recover graph later runs the skill-case reverse command
    through this same tool (no artifact-based auto-reversal).

    Inputs:
      - command: the full host command, e.g. "tc qdisc add dev eth0 root
        netem delay 200ms", "stress-ng --cpu 4 --timeout 600s".
      - timeout: max seconds to wait for the command (default 60).

    Output: command stdout on success; "Error:" on failure (guard
            rejection, non-zero exit, or transport error).

    Side effects: injects a real fault on the host until reversed /
                  recovered.
    """
    target = TransportTarget.from_state({})
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    if not argv:
        return "Error: host_inject requires a non-empty command."

    # host_inject is the SUPERSET of host_read: a real fault command goes
    # through the ToolGuard fault-binary whitelist, while a read-only
    # diagnostic (host_read's domain) is admitted with ``skip_guard`` so a
    # single EXECUTE/RECOVER-phase tool can both inject AND observe. Detection
    # stays correct because the host-native injection scan is content-aware
    # (a read-only host_inject call is NOT attributed as an injection).
    from chaos_agent.tools.readonly import contains_shell_metachar, is_readonly_argv

    # The argv-level classifier alone cannot see writes hidden INSIDE a program
    # string (``awk '{print > "/tmp/x"}'``), so the read-only fast path also
    # applies the same raw-string metachar screen host_read applies. A metachar
    # makes the quoting layer deliver a useless literal anyway — refusing loses
    # nothing. A command that fails the screen falls through to ToolGuard's
    # fault-binary whitelist (fail-closed for non-fault binaries).
    read_only = is_readonly_argv(argv) and not contains_shell_metachar(command)
    try:
        result = await execute_via_transport(
            argv, target, timeout=timeout, task_id=task_id,
            source="host-inject", skip_guard=read_only,
            expect_profile=profile_for_tool("host_inject"),
            # Read-only calls skip the GUARD (diag binaries are outside
            # ALLOWED_COMMANDS), but they are LLM-originated and belong on the
            # audit trail all the same.
            audit=True,
        )
    except Exception as e:  # includes ToolGuardError from the guard check
        return f"Error: host_inject blocked or failed: {e}"

    if result.exit_code == PROFILE_MISMATCH_EXIT_CODE:
        return f"Error: host_inject {(result.stderr or '').strip()}"
    if result.exit_code != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        return f"Error: host_inject failed (exit {result.exit_code}): {stderr or stdout or '(no output)'}"

    return result.stdout or "(command completed, no output)"


@tool(args_schema=_HostReadArgs)
async def host_read(command: str, timeout: int = 30, task_id: str = "") -> str:
    """READ-ONLY host diagnostics. Run ONE read-only diagnostic on the target host.

    Runs on the machine addressed by the CONFIGURED transport channel
    (ssh / kubewiz_host) — it CANNOT be pointed at a different machine per
    call (no node/host/pod parameter; passing one is refused). To observe a
    specific Kubernetes node, use ``kubectl_read``.

    Host equivalent of ``kubectl_read``: inspects host state (disk usage,
    load, process list, network rules) to verify a host fault's effect or
    its recovery. READ-ONLY BY ENFORCEMENT — mutating commands
    (``ip link set``, ``systemctl stop``, ``iptables -A``, ``dd`` …) are
    REJECTED with the specific reason.

    Safety: validated by the shared read-only classifier — the leading
    binary must be a read-only diagnostic (df / ps / ls / cat / top /
    iostat / free / ss / netstat / ip show / systemctl status / …),
    dual-use tools are checked at argument level, shell metacharacters
    (pipe / redirect / chain / substitution) rejected; anything else
    returns the specific reason without executing.

    When to use:
      - Verifying a host fault's effect or its recovery.

    Inputs:
      - command: the full diagnostic command, e.g. "df -h /var/lib",
        "iostat -xd 1 2", "iptables -L -n".
      - timeout: max seconds to wait (default 30).

    Output: command stdout (or stderr) on success; "Error:" on
            rejection/failure.

    Side effects: none (read-only).
    """
    from chaos_agent.tools.readonly import host_command_rejection_reason

    reason = host_command_rejection_reason(command)
    if reason is not None:
        return (
            f"Error: host_read rejected this command — it is not read-only: "
            f"{reason}.\n"
            "host_read runs ONE read-only diagnostic with NO shell "
            "metacharacters (no pipe / redirect / ; / && / substitution). To fix:\n"
            "- Use a single read-only diagnostic "
            "(df/ps/ls/cat/top/iostat/free/ss/netstat/ip show/systemctl status/…) "
            "without a pipe.\n"
            "- To check whether a binary exists, prefer `command -v <name>`: it "
            "is a shell builtin, so it needs no extra package and does not "
            "depend on the install path. Read its result carefully: a PATH means "
            "installed, while `(no output)` means NOT installed (the probe "
            "succeeded — do not retry it). `ls` also works but must name the "
            "REAL path — fault-injection network binaries live in sbin, so probe "
            "all candidates at once, e.g. "
            "`ls /usr/sbin/iptables /usr/bin/iptables /sbin/iptables`. "
            "(`which` is a separate package and may be absent on minimal "
            "systems; `command -v` is not.)\n"
            "- Only if this is genuinely a FAULT-INJECTION command (not a "
            "diagnostic) should you use host_inject instead."
        )
    target = TransportTarget.from_state({})
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    if not argv:
        return "Error: host_read requires a non-empty command."

    try:
        # Diagnostic binaries live outside ToolGuard.ALLOWED_COMMANDS; the
        # shared read-only classifier above is the gate, so skip the injection
        # guard here. Skipping the guard must NOT skip the audit trail: this is
        # an LLM-originated command, not an internal probe.
        result = await execute_via_transport(
            argv, target, timeout=timeout, task_id=task_id,
            skip_guard=True, source="host-read",
            expect_profile=profile_for_tool("host_read"),
            audit=True,
        )
    except Exception as e:
        return f"Error: host_read failed: {e}"

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    # A profile refusal is a rejection, not command output: prefix it so the
    # LLM reads it as an error (this tool's contract is "Error:" on rejection)
    # and never mistakes the explanation for diagnostic data.
    if result.exit_code == PROFILE_MISMATCH_EXIT_CODE:
        return f"Error: host_read {stderr}"
    return stdout or stderr or "(no output)"
