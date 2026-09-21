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

from chaos_agent.config.settings import settings
from chaos_agent.errors import ToolTimeoutError
from chaos_agent.tools._strict_args import StrictToolArgs
from chaos_agent.tools._tool_profiles import profile_for_tool
from chaos_agent.tools.guard import CommandResult
from chaos_agent.transports import PROFILE_HOST, TransportTarget, execute_via_transport
from chaos_agent.transports.executor import PROFILE_MISMATCH_EXIT_CODE
from chaos_agent.utils.truncation import apply_output_safety_valve

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

    command: str = Field(description="One read-only diagnostic command, no unquoted shell operators.")
    timeout: int = Field(default=30, description="Max seconds to wait.")
    task_id: str = Field(default="", description="Internal task id; leave unset.")


class _HostInjectArgs(_HostTargetingArgs):
    tool_display_name = "host_inject"

    command: str = Field(description="One host fault-injection command.")
    timeout: int = Field(default=60, description="Max seconds to wait.")
    task_id: str = Field(default="", description="Internal task id; leave unset.")


def _effective_host_timeout(requested: int) -> tuple[int, str]:
    """Clamp the LLM-supplied wait into ``[1, settings.timeout_host_cmd]``.

    The ``timeout`` args on these two tools are the ONLY LLM-writable
    timeouts in src — every other tool reads ``settings.timeout_*``. R60
    measured the unbounded path end-to-end (``_r60_timeout_ceiling.py``):
    the requested value flows verbatim into ``run_command``'s
    ``asyncio.wait_for`` (no clamp on either layer), and on the SSH face
    the wrap carries no timeout flag and there is NO server-side budget —
    a hung command plus an absurd LLM value parked the turn with no
    lower-layer bail-out (the wall-clock guard ships disabled and is a
    node-boundary check that cannot interrupt an in-flight tool call).
    The 2026-09-20 user ruling keeps the per-call parameter (the LLM may
    express intent) but bounds it: effective wait = ``min(requested,
    ceiling)`` with the ceiling defaulting to 600s — aligned with the
    kubewiz ``--timeout`` server task budget, beyond which the kubewiz
    face gains nothing anyway.

    R61 audited the clamp itself (``_r61_timeout_floor.py``) and closed
    the FLOOR half: a requested 0 or negative passed through unclamped
    while the local face's ``wait_for`` treats ``<=0`` as an IMMEDIATE
    timeout (measured 0.000s) — every call died before the command could
    start and landed in the R58 reconcile branch, and the kubewiz face
    mapped the same value to its own 10s fallback (the SAME LLM value
    carried different semantics per channel). The floor is 1s: no
    legitimate call wants to wait zero, and the schema has no ``ge``
    constraint to stop one.

    Returns ``(effective_timeout, clamp_note)``. The note is empty when
    no clamp happened (zero noise inside the window) and is a plain
    trailing note (NOT an "Error:" prefix — the call itself succeeds and
    must not flip the error classification) for the caller to append to
    execution-result paths only: guard/profile rejections never execute,
    so they carry no "waited differently than requested" fact to disclose.
    """
    ceiling = max(1, int(settings.timeout_host_cmd))
    effective = min(max(1, requested), ceiling)
    if effective == requested:
        return requested, ""
    if requested < 1:
        return effective, (
            f"\n\nNote: the requested wait ({requested}s) was raised to the "
            f"minimum wait of {effective}s; the call waited {effective}s."
        )
    return effective, (
        f"\n\nNote: the requested wait ({requested}s) was clamped to the "
        f"configured ceiling of {effective}s "
        f"(BLADE_AI_TIMEOUT_HOST_CMD); the call waited {effective}s."
    )


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

    Safety: the binary is checked against the host fault whitelist —
    non-fault binaries (rm, curl, systemctl, …) rejected; the call is
    drift-guarded against the approved host target; recovery is
    LLM-driven (no auto-reversal).

    Inputs:
      - command: the full host command, e.g. "tc qdisc add dev eth0 root
        netem delay 200ms".
      - timeout: max seconds the LOCAL call waits (default 60) — NOT the
        command's own runtime; a longer-running command returns timed-out
        with outcome UNKNOWN while it keeps running on the host. Values
        above the BLADE_AI_TIMEOUT_HOST_CMD ceiling (default 600) are
        clamped, with a note in the output.

    Output: stdout on success; "Error:" on failure (guard rejection,
            non-zero exit, or transport error).

    Side effects: injects a real fault until reversed / recovered.
    """
    # R60: bound the only LLM-writable wait in src. Rebinding ``timeout``
    # here routes EVERY execution path below (success, failed, R58/R59
    # timeout shapes, transport errors) through the effective budget with
    # no per-path changes; the note rides only the paths that actually
    # executed (see the helper's docstring).
    timeout, clamp_note = _effective_host_timeout(timeout)
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
    except ToolTimeoutError as e:
        # R58: a caller-budget expiry is outcome-UNKNOWN. Only the local wait
        # was killed — the fault command may STILL be running on the host
        # (R57 measured this exact mechanism on the shared transport: ghost
        # marker t+72s), and on the host face the edge is the NORM, not the
        # exception: this tool's own docstring example (``stress-ng --timeout
        # 600s``) outlives the default 60s budget. Keep the "Error:" prefix
        # (load-bearing for carrier attribution), keep the raw "timed out"
        # text (classify stays SHORT_RETRY; the census/budget layer still
        # caps retries), keep no "failed" verdict, and append reconcile-first
        # advice — advice, never a gate (same license as the completed-pod
        # note in kubectl.py).
        return apply_output_safety_valve(
            f"Error: host_inject: {e}\n"
            "Outcome UNKNOWN: only the local wait was killed — the fault "
            "command may STILL be running on the host. A blind retry can "
            "double-execute the fault. Reconcile first: use host_read to "
            "check the host's actual state (e.g. `ps -ef` for a "
            "still-running fault process, `iptables -L -n` for installed "
            "rules), then retry only what is genuinely missing."
            + clamp_note,
            kind="error",
        )
    except Exception as e:  # includes ToolGuardError from the guard check
        return f"Error: host_inject blocked or failed: {e}"

    if result.exit_code == PROFILE_MISMATCH_EXIT_CODE:
        return f"Error: host_inject {(result.stderr or '').strip()}"
    if result.exit_code != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        # Merge when both carry content — `or` drops one side's evidence.
        _detail = "\n".join(p for p in (stdout, stderr) if p) or "(no output)"
        # R59: receipt-form timeout — the sibling of the R58 exception
        # branch above. The caller timeout feeds BOTH the local run_command
        # kill AND the wiz CLI's --wait-timeout mirror (executor.py passes
        # one timeout to both); when the CLI's wait expires first (explicit
        # kubewiz_wait_timeout override below the caller budget), the CLI
        # exits non-zero with the platform's fixed receipt — "Error: task
        # timed out after Ns" — and parse_wiz_output passes it through: no
        # ToolTimeoutError is raised, so the R58 exception branch never
        # sees this shape. The fault command may STILL be running
        # server-side; the "failed" verdict plus the bare SHORT_RETRY shape
        # invites a blind retry (double-execute). The receipt is a
        # closed-set platform format (measured verbatim in R56 on the
        # isomorphic k8s channel), so matching it is legitimate
        # paired-prescription feedback (B38/B40): on a wording change this
        # silently degrades to the raw error below — advice, never a gate.
        # Known over-conservative sub-shape: when the caller budget exceeds
        # the 600s server task budget the command really is dead and a
        # retry is safe — the two forms are text-indistinguishable, so both
        # get the reconcile-first advice (harmlessly conservative:
        # reconcile finds nothing missing).
        if "task timed out" in _detail:
            return apply_output_safety_valve(
                f"Error: host_inject (exit {result.exit_code}): {_detail}\n"
                "Outcome UNKNOWN: the CLI's own wait expired — the fault "
                "command may STILL be running on the host. A blind retry "
                "can double-execute the fault. Reconcile first: use "
                "host_read to check the host's actual state (e.g. `ps -ef` "
                "for a still-running fault process, `iptables -L -n` for "
                "installed rules), then retry only what is genuinely "
                "missing."
                + clamp_note,
                kind="error",
            )
        return f"Error: host_inject failed (exit {result.exit_code}): {_detail}" + clamp_note

    return (result.stdout or "(command completed, no output)") + clamp_note


@tool(args_schema=_HostReadArgs)
async def host_read(command: str, timeout: int = 30, task_id: str = "") -> str:
    """READ-ONLY host diagnostics. Run ONE read-only diagnostic on the target host.

    Runs on the machine addressed by the CONFIGURED transport channel
    (ssh / kubewiz_host) — it CANNOT be pointed at a different machine per
    call (no node/host/pod parameter; passing one is refused). To observe a
    specific Kubernetes node, use ``kubectl_read``.

    Host equivalent of ``kubectl_read``: inspects host state (disk / load /
    processes / network rules) to verify a host fault's effect or recovery.

    Safety: validated by the shared read-only classifier — the leading
    binary must be a read-only diagnostic (df / ps / ls / cat / top /
    iostat / free / ss / netstat / ip show / systemctl status / …),
    dual-use tools are checked at argument level, UNQUOTED shell operators
    (pipe / redirect / chain / substitution) rejected — a quoted literal
    like 'a|b' is fine; anything else
    returns the specific reason without executing.

    When to use:
      - Verifying a host fault's effect or its recovery.

    Inputs:
      - command: the full diagnostic command, e.g. "df -h /var/lib",
        "iostat -xd 1 2", "iptables -L -n".
      - timeout: max seconds to wait (default 30). Values above the
        configured ceiling (BLADE_AI_TIMEOUT_HOST_CMD, default 600) are
        clamped to it and the output says so.

    Output: command stdout (or stderr) on success; "Error:" on
            rejection/failure.

    Side effects: none (read-only).
    """
    from chaos_agent.tools.readonly import host_command_rejection_reason

    # R60: same bound as host_inject — rebind before ANY execution path;
    # the note rides only the paths that actually executed.
    timeout, clamp_note = _effective_host_timeout(timeout)

    reason = host_command_rejection_reason(command)
    if reason is not None:
        return (
            f"Error: host_read rejected this command — it is not read-only: "
            f"{reason}.\n"
            "host_read runs ONE read-only diagnostic with no UNQUOTED shell "
            "operators (no pipe / redirect / ; / && / substitution; a quoted "
            "literal like 'a|b' is fine). To fix:\n"
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
        # May be a ToolTimeoutError (the wait DID happen) — the clamp fact
        # belongs here just as on the success path.
        return f"Error: host_read failed: {e}" + clamp_note

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    # A profile refusal is a rejection, not command output: prefix it so the
    # LLM reads it as an error (this tool's contract is "Error:" on rejection)
    # and never mistakes the explanation for diagnostic data.
    if result.exit_code == PROFILE_MISMATCH_EXIT_CODE:
        return f"Error: host_read {stderr}"
    return (stdout or stderr or "(no output)") + clamp_note
