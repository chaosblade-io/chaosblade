"""Host-level command execution via transport channels.

Provides ``exec_host_command`` — a thin wrapper around
``execute_via_transport`` for running host-level shell commands
(iptables, stress-ng, dd, fallocate, etc.) through the appropriate
transport channel (kubewiz-host or SSH).

Also provides the two LLM-facing ``@tool`` bindings for the ``host_shell``
carrier:

- ``host_inject`` — Phase 2 host command runner: it injects a fault AND
  observes its live effect. It is the SUPERSET of ``host_read`` and the only
  tool bound in EXECUTE/RECOVER, so it admits TWO kinds of command: (1) a fault
  command run directly on the configured host, whose binary is gated by
  ``ToolGuard`` (inside ``execute_via_transport``) against the host fault
  whitelist (iptables / tc / stress-ng / dd / fallocate / fio …), non-fault
  MUTATING binaries rejected; and (2) a SINGLE read-only diagnostic (no pipe or
  redirect), admitted with ``skip_guard`` so the model can sample a
  self-recovering fault's primary effect INSIDE the fault window (host_read is
  not bound here). The fault call is additionally drift-guarded by the
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

Both tools resolve their transport target through the single authoritative
seam ``resolve_transport_target(state)`` — the graph state is supplied by the
runtime via ``InjectedState`` (hidden from the LLM schema); a direct/test call
with no state falls back to the settings-authoritative target.
"""

from __future__ import annotations

import hashlib
import logging
import shlex
from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState
from pydantic import Field

from chaos_agent.config.settings import settings
from chaos_agent.errors import ToolTimeoutError
from chaos_agent.tools._strict_args import StrictToolArgs
from chaos_agent.tools._tool_profiles import profile_for_tool
# ``_PAYLOAD_INTERPRETERS`` is imported private-on-purpose: it is the guard's
# single definition of "which binaries carry an interpreter payload", and the
# fidelity receipt below must recognise exactly the payload shapes the guard
# admits — restating the set here would let the two drift (same pattern as
# guard.py importing ``_READONLY_BINARIES`` from readonly).
from chaos_agent.tools.guard import _PAYLOAD_INTERPRETERS, CommandResult
from chaos_agent.transports import (
    PROFILE_HOST,
    TransportTarget,
    execute_via_transport,
    resolve_transport_target,
)
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


# Shell composition other than a single pipe. A command carrying any of these is
# NEVER re-cast as an observation (it may hide a mutating stage or a file write),
# so it falls through to ToolGuard unchanged.
_NON_PIPE_OPERATORS = (";", "&&", "||", ">", "<", "&", "$(", "`")


def _pipe_only_readonly_head(command: str) -> str | None:
    """Leading stage of a PURE read-only observation pipeline, else ``None``.

    Returns the first ``|``-stage only when the command's sole shell composition
    is a pipe AND every stage is a read-only probe — e.g. ``top -bn1 | head``,
    which the exec-form transport cannot honor (the pipe is delivered as a
    useless literal). Any redirect / chain / substitution, or any mutating stage
    (``... | tee /tmp/x``, ``... | rm -rf ...``), returns ``None`` so the call
    falls through to ToolGuard unchanged: the read-only fast path never becomes
    a second gate, and a mutating tail is never silently re-cast as an
    observation. ``is_readonly_argv`` is blind past the head binary, so EVERY
    stage is judged separately — the whole point of splitting here.
    """
    from chaos_agent.tools.readonly import is_readonly_argv

    if "|" not in command or any(op in command for op in _NON_PIPE_OPERATORS):
        return None
    head: str | None = None
    for stage in command.split("|"):
        stage = stage.strip()
        if not stage:
            return None
        try:
            argv = shlex.split(stage)
        except ValueError:
            return None
        if not argv or not is_readonly_argv(argv):
            return None
        if head is None:
            head = stage
    return head


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

    # The runtime injects the graph state here (InjectedState) so both host
    # tools can resolve their transport target through the single seam. It
    # MUST be declared on the schema: StrictToolArgs forbids unknown keys, so
    # an UNDECLARED injected ``state`` is refused at validation — every real
    # ToolNode call to host_read/host_inject then dies with "does not accept
    # these parameters: state" (surfaced by A/A run inject-cda2987d; direct
    # ``.ainvoke({"command": ...})`` unit calls never inject state, so they
    # could not see it). Declared with the InjectedState marker the field stays
    # HIDDEN from the LLM-facing tool_call_schema — the model still cannot pass
    # it and unknown keys like ``node`` are still refused — while the
    # framework-injected value passes validation and reaches the tool.
    state: Annotated[dict | None, InjectedState] = None

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

    command: str = Field(description=(
        "One host command: a fault-injection command, OR a single read-only "
        "diagnostic (no pipe/redirect) to observe the fault's live effect."
    ))
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


def _merge_success_streams(stdout: str, stderr: str, *, empty: str) -> str:
    """Success-path (exit 0) output preserving BOTH streams.

    Mirrors the kubectl Case #63 fix (``kubectl_cli`` success branch) and
    ``execute_skill_script``: a stdout-only receipt turns "ran fine" into
    "no output" for the whole POSIX family that writes its confirmation /
    statistics to **stderr** — ``systemd-run`` ("Running as unit: …"),
    ``systemctl mask/unmask/enable/disable`` ("Created symlink … →
    /dev/null"), and the ``dd``/``tar``/``curl``/``wget`` timing family.
    On the host face this is the NORM for mutations: Case #12's execute
    loop armed a recovery carrier and masked a unit — both succeeded, both
    confirmed on stderr, both came back ``(command completed, no output)``,
    and the model burned two extra fail-closed read-only probes (~31s of
    LLM turn) to re-confirm state the transport had already reported but
    dropped. This is the SAME invariant host_inject's own failure branch
    upholds ("``or`` drops one side's evidence") and host_read's
    ``stdout or stderr`` only half-upholds (it drops stderr whenever stdout
    is non-empty).

    Non-empty stderr rides along under the ``--- STDERR ---`` separator the
    other two channels use; when stderr is empty this is byte-identical to
    returning ``stdout or empty``, so every clean read is unchanged. This
    helper only MERGES — it does not bound. Both callers wrap its result in
    ``apply_output_safety_valve`` (the ceiling every tool success path
    obeys), so the merge stays a pure stream-preserving step and the size
    bound lives at the call site, where the valve ``kind`` is known and the
    budget sees the merged total (not a single stream).
    """
    stderr = (stderr or "").strip()
    if not stderr:
        return stdout or empty
    if (stdout or "").strip():
        return f"{stdout}\n--- STDERR ---\n{stderr}"
    return f"--- STDERR ---\n{stderr}"


def _interpreter_payload_indices(cmd: list[str]) -> list[int]:
    """Indices of interpreter-script payload elements, per the guard's own rule.

    ``ToolGuard._admit_command_region`` recognises exactly one interpreter
    payload shape — ``sh -c '<script>'``, three tokens, head in
    ``_PAYLOAD_INTERPRETERS``, second token literally ``-c`` — and refuses
    every other interpreter form (a bare ``sh <file>`` runs a script no gate
    can read; a fourth ``$0`` token is a shape no skill teaches). So the same
    window scan is the whole detection rule here, and ``_PAYLOAD_INTERPRETERS``
    is imported rather than restated: if the guard ever widens the payload
    grammar, this widens with it instead of silently under-reporting.

    The scan is a WINDOW over the full argv, not a head check, because the
    payload region sits behind a carrier's own flags
    (``systemd-run --on-active=240s --unit=x sh -c '…'``).

    A payload with no whitespace is skipped: ``argv[]=sh -c true`` is a
    lossless space-join, so there is no boundary ambiguity to settle and a
    receipt would be noise. Whitespace is what makes the rendering ambiguous.
    """
    out = []
    for i in range(len(cmd) - 2):
        if (
            Path(cmd[i]).name in _PAYLOAD_INTERPRETERS
            and cmd[i + 1] == "-c"
            and any(ch.isspace() for ch in cmd[i + 2])
        ):
            out.append(i + 2)
    return out


def payload_fidelity_receipt(cmd: list[str]) -> str | None:
    """State, as a machine fact, that a carrier payload survived delivery.

    THE PROBLEM THIS CLOSES (measured on host Case #13, timeoffset, 2227s
    run): every host channel rebuilds the cleared argv with
    ``" ".join(shlex.quote(p) for p in cmd)`` — ``SSHChannel.wrap_command``,
    ``_wiz_preserve_stderr`` and ``embed_stdin_in_command`` all three — so a
    ``sh -c '<script>'`` payload reaches the remote as ONE intact element and
    its internal syntax is parsed there, never by the transport. Fidelity is
    by construction. But the readback a model naturally reaches for,
    ``systemctl show <unit>.service -p ExecStart``, renders
    ``argv[]=/usr/bin/sh -c systemctl start chrony; systemctl start X ...``
    **space-joined without quotes**, which is byte-identical whether the
    script arrived intact or was split into ``["sh", "-c", "systemctl",
    "start", ...]``. Recognising that ambiguity, the model has exactly two
    moves and both are bad: probe it (interpreters are admitted only inside a
    carrier's payload region, never at top level, so the probe is refused —
    Case #13 burned a turn on ``sh -c 'echo PROBE_A PROBE_B'``), or re-arm in
    a form whose readback IS unambiguous. Case #13 took the second: it
    ``systemctl stop``'d a carrier that had already armed successfully,
    re-armed as three bare-argv units — which cannot carry ``|| true``, so the
    ``chronyc makestep`` unit exited 1 and landed ``failed``, a state systemd
    does not GC — and spent ~331s (14.9% of the run) doing it.

    The transport is the only party that KNOWS the boundaries, because it
    applied the quoting; so it says so, at the moment the doubt would form.
    This is the same lesson ``_merge_success_streams`` records for Case #12
    (state the transport had already reported but dropped cost two extra
    fail-closed probes), applied to payload shape instead of exit status.

    FACTS ONLY — no instruction about which carrier form to prefer. The
    re-arm was motivated by unverifiability, not by taste: remove the doubt
    and the motivation goes with it. Legislation about form belongs to the
    skill's carrier standard, not to a tool receipt.

    ``None`` for the common case, so this adds nothing to a bare-argv call —
    which is the overwhelming majority of host calls, and the reason a receipt
    that fired unconditionally would train the model to skip reading it.
    """
    idxs = _interpreter_payload_indices(cmd)
    if not idxs:
        return None
    described = "; ".join(
        f"element {i + 1} of {len(cmd)} "
        f"({len(cmd[i].encode('utf-8'))} bytes, "
        f"sha256:{hashlib.sha256(cmd[i].encode('utf-8')).hexdigest()[:12]})"
        for i in idxs
    )
    return (
        "[payload fidelity — by construction] This transport shlex.quoted "
        f"each argv element individually before delivery, so {described} "
        "arrived as ONE intact token; the shell syntax inside it is parsed "
        "by the REMOTE shell, never by this transport, so it cannot have "
        "been split en route. Fidelity therefore needs no probe and admits "
        "none — an interpreter is admissible only inside a carrier's payload "
        "region, never as a top-level command, so there is no dispatchable "
        "form that could echo this script back. Note also that an "
        "`ExecStart=` / `argv[]=` readback renders elements SPACE-JOINED "
        "WITHOUT QUOTES, so it can neither confirm nor deny that boundary — "
        "ambiguity there is a property of the rendering, not evidence of a "
        "split."
    )


@tool(args_schema=_HostInjectArgs)
async def host_inject(
    command: str,
    timeout: int = 60,
    task_id: str = "",
    *,
    state: Annotated[dict | None, InjectedState] = None,
) -> str:
    """Phase 2 host command runner — inject a fault AND observe its live effect.

    Runs on the machine the CONFIGURED transport (ssh / kubewiz_host) addresses;
    it CANNOT be re-pointed per call (no node/host/pod parameter — refused).

    The host analogue of the full ``kubectl`` tool: ONE entry point, TWO kinds
    of command —
      1. A MUTATING fault command (iptables / tc / stress-ng / dd / fallocate /
         fio …), run on the host, bypassing ChaosBlade and kubectl, and
         drift-guarded against the approved target. Use ONLY for ``scope=host``;
         K8s faults → blade_create/kubectl.
      2. A SINGLE read-only diagnostic (no pipe/redirect), admitted to OBSERVE
         the fault's live effect here. Admission is by command SHAPE, not
         binary: every guard-cleared read-only shape passes — incl. a read-verb
         network probe (HTTP GET via curl/wget), the only app-level probe for an
         in-process (``python``) target — while a mutating shape (write flag,
         rm/systemctl, pipe/redirect) is rejected.

    (2) is the ONLY in-window observation channel — host_read is NOT bound here.
    For a self-recovering fault (ChaosBlade ``--timeout``, or one leaving no
    durable residue) the primary effect exists ONLY while the fault is live, so
    sample it HERE (``top -bn1`` for a CPU burn — one command, no pipe); by
    VERIFY time it has auto-recovered and the direct evidence is gone. Recovery
    is LLM-driven (no auto-reversal).

    Inputs:
      - command: a fault command OR a single read-only diagnostic. No shell
        operators — the transport runs exec-form, so a pipe/redirect is a
        useless literal, not honored.
      - timeout: max seconds the LOCAL call waits (default 60) — NOT the
        command's own runtime; a longer command returns timed-out (outcome
        UNKNOWN) while it keeps running on the host. Values above
        BLADE_AI_TIMEOUT_HOST_CMD (default 600) are clamped, with a note.

    Output: stdout on success; "Error:" on real failure (guard rejection, a
            FAULT command's non-zero exit, or transport error). A READ-ONLY
            diagnostic exiting non-zero is NOT a failure — it returns
            OBSERVATION data whether or not it produced output (the exit code IS
            the finding: ``cat <deleted>`` proves the file gone).

    Side effects: a fault command injects a real fault until reversed/recovered;
            a read-only diagnostic has none.
    """
    # R60: bound the only LLM-writable wait in src. Rebinding ``timeout``
    # here routes EVERY execution path below (success, failed, R58/R59
    # timeout shapes, transport errors) through the effective budget with
    # no per-path changes; the note rides only the paths that actually
    # executed (see the helper's docstring).
    timeout, clamp_note = _effective_host_timeout(timeout)
    target = resolve_transport_target(state)
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

    # Phase-correct dead-end guard. A PURE read-only observation pipeline
    # (``top -bn1 | head``) fails the metachar screen above (read_only=False)
    # and would otherwise fall through to ToolGuard, whose generic
    # UNKNOWN_BINARY hint points the model at ``host_read`` — but host_read is
    # NOT bound in this EXECUTE/RECOVER phase, so that pointer is a dead end
    # (guard.py legislates a hard floor may redirect only to a REACHABLE path).
    # The transport runs exec-form, so the pipe is a useless literal anyway: the
    # honest fix is to drop it and run a SINGLE read-only diagnostic HERE —
    # host_inject admits it read-only in this same phase. The gate fires ONLY for
    # an every-stage-read-only pipe (see the helper); a redirect / chain /
    # substitution / mutating tail falls through to the guard unchanged, so the
    # fast path never becomes a second gate. Returns guidance only; never runs
    # the piped form.
    if not read_only:
        suggestion = _pipe_only_readonly_head(command)
        if suggestion is not None:
            return (
                "Error: host_inject: this command pipes a read-only diagnostic, "
                "but the transport runs exec-form — the pipe is delivered as a "
                "useless literal, not honored, so it cannot run as written. To "
                "observe in THIS phase, run a SINGLE read-only command with no "
                f"pipe/redirect, e.g. `{suggestion}` — host_inject admits "
                "read-only diagnostics here, so you do NOT need host_read (it is "
                "not bound in this phase). If you need filtering, use one command "
                "that does it (e.g. `pgrep -a NAME`, not `ps -ef | grep NAME`)."
            )
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
            "double-execute the fault. Reconcile first: run a SINGLE "
            "read-only command through host_inject itself (it admits "
            "read-only diagnostics in this phase; host_read is NOT bound "
            "here) to check the host's actual state (e.g. `ps -ef` for a "
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
                "can double-execute the fault. Reconcile first: run a "
                "SINGLE read-only command through host_inject itself (it "
                "admits read-only diagnostics in this phase; host_read is "
                "NOT bound here) to check the host's actual state (e.g. "
                "`ps -ef` for a still-running fault process, `iptables -L -n` "
                "for installed rules), then retry only what is genuinely "
                "missing."
                + clamp_note,
                kind="error",
            )
        # Probe-mode discriminator (first-principles #31, mirrors
        # host_http_probe_cli / _kubectl_impl(probe_mode=True) /
        # baseline_capture: "a non-zero exit is channel signal, not a
        # semantic verdict"). ``read_only`` was computed above to skip the
        # guard; it ALSO decides exit-code semantics, because host_inject
        # is the SUPERSET tool that both injects AND observes in this
        # phase. A READ-ONLY diagnostic SPEAKS IN EXIT CODES — for an
        # absence/health probe the non-zero exit IS the observation, not a
        # tool failure: ``cat <deleted>`` → ENOENT exit 1 proves the file
        # is gone; ``systemctl status <missing-unit>`` → exit 4; ``pgrep
        # <killed-proc>`` → exit 1 (empty result = not running). Framing
        # that as "Error: ... failed" mislabels fault evidence as a
        # malfunction, and because the single-source verdict gate keys on
        # the "Error" prefix (tool_verdicts.GENERIC_ERROR_PREFIXES) it
        # would route a SUCCESSFUL observation into classify_error →
        # introspection hint / auto-replan. Return it as observation data
        # WITHOUT the prefix so the verdict reads "not a failure"; the
        # semantic call (fault evidence vs. probe-couldn't-run) belongs to
        # the model, which holds the skill-case context.
        #
        # ROOT FIX (design-level, 2026-11-02): a read-only non-zero exit is
        # OBSERVATION data whether or not it produced output text — so the
        # EMPTY case is NO LONGER carved out as an "Error:". The former code
        # split on output-text presence and framed empty output as "the probe
        # never ran: the host may be unreachable or lack the binary". That
        # discriminator axis was BACKWARDS (measured): the absence-probe family
        # (``pgrep``/``test -e``/``grep -q`` no-match) is SILENT by design —
        # exit!=0 with BOTH streams empty IS its positive observation ("not
        # running" / "not present"), exactly as the ``pgrep <killed-proc>``
        # example above and the sibling host_read guidance ("`(no output)` means
        # NOT installed — the probe succeeded") already state; host_read itself
        # returns "(no output)" for this shape with no Error at all. Conversely
        # a genuine transport drop or missing binary WRITES stderr
        # (``Connection refused`` exit 255 / ``command not found`` exit 127), so
        # _detail is NON-empty and it lands in the observation branch for the
        # model to read — the empty branch never actually caught the "host
        # unreachable" case it claimed to. Reserving "Error:" for empty output
        # therefore asserted a FALSE "host unreachable" over the common
        # absence-probe success (run6: ``pgrep -a strace`` on a dud injection
        # returned exit 1 + empty = "no strace attached", the fault evidence,
        # yet was framed "the host may be unreachable"). Both sub-cases now
        # return observation data (no "Error:" prefix); the empty/non-empty
        # split only tailors the guidance wording, since an empty result carries
        # no host text to read. The injection path (read_only=False) is
        # untouched: a fault command's non-zero exit really is a failure and
        # keeps the "failed" verdict + "Error:" prefix (load-bearing for carrier
        # attribution and SHORT_RETRY).
        if read_only:
            if _detail != "(no output)":
                # NOTE: the guidance prose below deliberately avoids the literal
                # strings classify_error keys on ("command not found", "no such
                # file or directory"). The verdict gate already protects this
                # return (no "Error" prefix → gate returns None → classify_error
                # is never invoked), but embedding a replan-trigger substring in
                # our OWN prose would be a latent hazard for any future consumer
                # that classified raw tool text without the gate. The verbatim
                # ``_detail`` (the host's own absence wording) is genuine
                # evidence and stays untouched.
                return (
                    f"(read-only observation, exit {result.exit_code}) — for "
                    "an absence/health diagnostic a non-zero exit is the "
                    "OBSERVATION, not a tool failure (e.g. a missing-target "
                    "error confirms the file is gone; an empty result confirms "
                    "a process is not running). Judge it against the skill "
                    "case. Treat it as a probe failure ONLY if the output shows "
                    "the diagnostic EXECUTABLE itself was absent or could not be "
                    "invoked, rather than reporting on the target:\n"
                    f"{_detail}" + clamp_note
                )
            # EMPTY output + non-zero exit: for an absence/health probe this is
            # the POSITIVE observation (``pgrep``/``test -e``/``grep -q`` are
            # silent when they find nothing), NOT "the probe never ran". No
            # "Error:" prefix → the verdict gate reads "not a failure" and
            # classify_error is never invoked. The wording states the ambiguity
            # HONESTLY instead of asserting a false "host unreachable": a
            # genuine transport drop or missing binary leaves stderr (non-empty,
            # handled above), so an empty result is a successful absence
            # observation unless the case expected output. It also avoids the
            # literal substrings classify_error keys on (same latent-hazard
            # discipline as the non-empty branch).
            return (
                f"(read-only observation, exit {result.exit_code}, empty "
                "result) — for an absence/health probe an EMPTY result with a "
                "non-zero exit is normally the POSITIVE OBSERVATION, not a tool "
                "failure: `pgrep`/`test -e`/`grep -q` are silent by design when "
                "they find nothing, so an empty result confirms the target is "
                "absent (process not running / path not present). A genuine "
                "transport drop or missing binary almost always leaves stderr "
                "text, which is absent here — so judge this as a successful "
                "absence observation against the skill case. ONLY if the case "
                "EXPECTED output should you corroborate the host's reachability "
                "before relying on it."
                + clamp_note
            )
        return f"Error: host_inject failed (exit {result.exit_code}): {_detail}" + clamp_note

    # The fidelity receipt rides the SUCCESS receipt only. That is where the
    # doubt forms (the arm worked, so the model goes looking for proof the
    # payload landed intact) and it is the one place the doubt can be settled
    # by a fact instead of a probe. A read-only call never reaches here with an
    # opaque element — the metachar screen above refuses shell composition —
    # so the receipt stays silent on the observation path by construction, not
    # by a special case.
    fidelity = payload_fidelity_receipt(argv)
    # Merge BEFORE the valve so the 64KB budget sees the true total, then
    # bound the merged body — mirroring kubectl_cli / registry /
    # host_http_probe_cli, whose success paths ALL carry this valve.
    # host_cmd was the ONE tool success path missing it: it kept the merge
    # half of the Case #63 fix but OMITTED the paired valve, breaking
    # truncation.py's explicit contract that "success and error outputs
    # share the SAME ceiling — a one-sided valve would just move the crash
    # to the other side". host_inject runs ARBITRARY shell, so a runaway
    # payload (`dd if=/dev/zero` with no count, `cat /dev/urandom`, an
    # unbounded loop) returned its full byte stream, which then entered
    # state.messages, the compactor's FULL on-disk cache, and the off-graph
    # consumers (metric extractor, verifier) that read a tool return before
    # compaction — all unbounded. The valve pins that to 64KB head-tail at
    # the source. passthrough (<64KB) is byte-identical, so every routine
    # host command is unchanged. fidelity + clamp_note ride INSIDE the
    # valved body (at the tail, always preserved by the head-tail cut) so
    # the returned message never exceeds the ceiling the valve enforces
    # (the ~1% overrun kubectl_cli explicitly avoids by not appending
    # hints post-valve).
    #
    # This is boundedness at the source, NOT a guarantee that a tail-borne
    # summary reaches the LLM: the compactor governs the LLM-visible form
    # head-only (16KB recent / 1KB old — tool_compactor.truncate_text keeps
    # ``encoded[:max_bytes]``) and re-cuts this valved body, so a
    # load-bearing line at the merged tail (e.g. dd's stderr timing) still
    # falls out of the compacted context. Surviving THAT is the metric
    # extractor's `[Auto-extracted]` HEAD-prefix job, a separate coverage
    # question — the valve does not and cannot answer it.
    return apply_output_safety_valve(
        _merge_success_streams(
            result.stdout, result.stderr, empty="(command completed, no output)"
        )
        + (f"\n{fidelity}" if fidelity else "")
        + clamp_note,
        kind="success-output",
    )


@tool(args_schema=_HostReadArgs)
async def host_read(
    command: str,
    timeout: int = 30,
    task_id: str = "",
    *,
    state: Annotated[dict | None, InjectedState] = None,
) -> str:
    """READ-ONLY host diagnostics. Run ONE read-only diagnostic on the target host.

    Runs on the machine addressed by the CONFIGURED transport channel
    (ssh / kubewiz_host) — it CANNOT be pointed at a different machine per
    call (no node/host/pod parameter; passing one is refused). To observe a
    specific Kubernetes node, use ``kubectl_read``.

    Host equivalent of ``kubectl_read``: inspects host state (disk / load /
    processes / network rules) to verify a host fault's effect or recovery.

    Safety: validated by the shared read-only classifier BY COMMAND SHAPE, not
    by binary identity — a single diagnostic in an inspection/read form passes
    (incl. a dual-use binary on its read verb: `blade status`, `iptables -L`,
    `systemctl status`, an HTTP GET via curl/wget); any mutating form is
    refused. UNQUOTED shell operators (pipe / redirect / chain / substitution)
    rejected — a quoted literal like 'a|b' is fine; anything else returns the
    specific reason without executing.

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
    from chaos_agent.tools.readonly import (
        host_command_rejection_reason,
        is_unregistered_binary_reason,
    )

    # R60: same bound as host_inject — rebind before ANY execution path;
    # the note rides only the paths that actually executed.
    timeout, clamp_note = _effective_host_timeout(timeout)

    reason = host_command_rejection_reason(command)
    if reason is not None:
        # The unregistered-binary terminal gets its OWN text because its fix
        # path is the opposite of every other refusal's. A shape refusal (pipe
        # / redirect / write flag) is fixed by reshaping; this one says the
        # binary has no registered read-only form, so no shape of it can ever
        # be admitted here. Rendering the generic reshape advice for it — and
        # the generic text offers "check whether a binary exists" — sends the
        # model at a probe that cannot change the verdict: host Case #13 ran
        # `command -v chronyc`, got `/usr/bin/chronyc`, and learned nothing
        # (installed ≠ registered). Dispatch is on the shared verdict constant,
        # not on substring-matched prose.
        #
        # No pointer to host_inject here, even though its separate whitelist
        # does admit some of these binaries: host_read is bound in the
        # read-only phases where host_inject is NOT, and a hard floor may
        # redirect only to a REACHABLE path (the rule guard.py states for its
        # own pipe gate).
        if is_unregistered_binary_reason(reason):
            return (
                f"Error: host_read rejected this command — it is not read-only: "
                f"{reason}.\n"
                "This verdict is about the classifier's REGISTERED diagnostic "
                "vocabulary, not about the host, and reshaping cannot fix it: "
                "this binary has no registered read-only form, so no flag or "
                "argument of it can be admitted here. Do NOT probe whether it "
                "is installed — that is a different question (`command -v` "
                "reports a PATH for a binary this classifier still refuses), "
                "and its answer cannot change this verdict. To fix:\n"
                "- Reach the same fact through a REGISTERED diagnostic. The "
                "vocabulary covers the usual inspection tools (df, ps, ss, "
                "cat, ls, stat, find, date, uname, /proc paths), and a "
                "dual-use binary counts on its read verb (`systemctl status`, "
                "`timedatectl status`, `iptables -L`, `blade status`).\n"
                "- If the fact is reachable ONLY through this binary, record "
                "that in your reasoning and proceed without it — do not spend "
                "turns on probes that cannot be admitted.\n"
                # Covers a genuinely MUTATING intent that lands on this verdict
                # (``rm`` is unregistered rather than listed in
                # ``_MUTATING_BINARIES``, which holds only the fault-injection
                # binaries). Deliberately NOT a pointer to host_inject: the two
                # tools are never bound in the same phase, so the pointer would
                # be unreachable — say where the action belongs instead.
                "- If what you meant was a MUTATING action rather than a "
                "diagnostic, it does not belong in a read-only phase at all; "
                "carry it into the execution phase."
            )
        return (
            f"Error: host_read rejected this command — it is not read-only: "
            f"{reason}.\n"
            "host_read runs ONE read-only diagnostic with no UNQUOTED shell "
            "operators (no pipe / redirect / ; / && / substitution; a quoted "
            "literal like 'a|b' is fine). To fix:\n"
            "- Use a single read-only diagnostic in an inspection form — "
            "admitted by SHAPE, not by binary (a dual-use binary on its read "
            "verb counts: `blade status`, `iptables -L`, `systemctl status`) — "
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
            # NOT "use host_inject instead": host_read is bound only in the
            # read-only phases (PLAN/VERIFY) and host_inject only in
            # EXECUTE/RECOVER_VERIFY — the provider's phase map returns one or
            # the other, never both (host_shell/provider.py; pinned by
            # test_factory). Naming an unbound tool sends the model at a call
            # that cannot dispatch, the dead end this file already refuses to
            # build for the pipe gate above. Say where the action belongs.
            "- If this is genuinely a FAULT-INJECTION command rather than a "
            "diagnostic, it belongs to the execution phase — the injection tool "
            "is not bound here, so there is nothing to re-issue it as in this "
            "phase."
        )
    target = resolve_transport_target(state)
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
    # Bound the merged body with the same safety valve host_inject and every
    # other tool success path carries (truncation.py: success and error share
    # the SAME ceiling). host_read is read-only, but a diagnostic can still
    # emit unbounded stdout (`cat /var/log/...`, `journalctl`, `dmesg`), which
    # without the valve enters state.messages + the compactor's FULL on-disk
    # cache raw. clamp_note rides INSIDE the valved body so the returned
    # message never exceeds the ceiling it enforces (the ~1% overrun
    # kubectl_cli avoids by not appending hints post-valve). See host_inject's
    # success path for the full rationale and the honest note on what the
    # valve does NOT do (the compactor still governs the LLM view head-only).
    return apply_output_safety_valve(
        _merge_success_streams(stdout, stderr, empty="(no output)") + clamp_note,
        kind="success-output",
    )
