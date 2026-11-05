"""ChaosBlade Python-application (in-process agent) CLI tool wrappers.

Lives inside the ChaosBlade provider package next to ``cli.py`` (phase-11
carrier-import-boundary): reached only through the python provider's tool
surface, never via ``chaos_agent.tools``.

Separate module from ``cli.py`` on purpose: this fault domain has a different
command shape (``blade create python <target> <action>``), a different parameter
face (per-client matchers: Redis ``cmd``/``key``, SQL ``sql``/``sqltype``, HTTP
``url``/``method``, ...) and a different precondition (an in-process agent must
already be running inside the target application). Keeping it out of
``blade_create`` prevents the K8s/host injection tool's signature and description
from being diluted, and — because injection detection is keyed on TOOL NAME —
keeps the ChaosBlade OS carrier from mis-attributing a Python-agent experiment to
itself.

Agent lifecycle (verified against chaosblade 1.9.0-alpha, not inferred):
  1. ``blade prepare python --port P --python-path PY --target-script S`` writes a
     ``sitecustomize.py`` hook into the DIRECTORY OF ``S``. The hook prepends
     blade's own bundled agent library (``<blade-dir>/lib/python``) to
     ``sys.path`` and calls ``ChaosBladeAgent(port=P).start()``. No separate
     ``pip install`` is needed. ``P`` must be FREE — prepare refuses a port that
     is already listening.
  2. The application must then be (re)started with that hook directory on
     ``PYTHONPATH``; only then does the agent actually listen. Prepare returning
     ``success`` does NOT mean an agent is running, and ``blade status --type
     prepare`` reporting ``Running`` is a bookkeeping state, not liveness.
  3. ``blade create python ...`` resolves the port from a RUNNING prepare record
     and posts to ``http://127.0.0.1:<port>/create``. With several RUNNING records
     it was observed to pick the OLDEST, so a stale record shadows a newer
     prepare — revoke stale records before relying on a new port. The
     connection-refused error path below DETECTS this shadowing by enumerating
     the RUNNING records (read-only ``blade status --type prepare``) and presents
     the authoritative breakdown to the model, so it can self-correct in one step
     instead of guessing which record blade bound to.
  4. ``blade revoke <prepare-uid>`` DELETES the ``sitecustomize.py`` hook, so the
     application loses its agent on the next restart. It is not fault recovery.

Delivery: ``blade`` can only reach an agent on ITS OWN machine — no subcommand
takes an agent-host parameter (``prepare python`` only describes the LOCAL
interpreter/script), so it always talks to ``http://127.0.0.1:<port>``. The
injection command must therefore LAND on the machine hosting the target
application, which is a question of the channel's ADDRESSING GRANULARITY:
  - ``ssh`` / ``kubewiz_host``: address a specific machine (``ssh user@host``,
    ``wiz task exec --name <host>``) — the only supported setup.
  - ``kubewiz_k8s``: addresses a CLUSTER (``--cluster-uuid`` only, no ``--name``),
    so there is no way to say "run this on the machine where my app runs".
  - ``kubeconfig``: adds no remote hop (the command would run verbatim on the
    blade-ai machine), so it would physically work for a co-located application —
    but see the capability gate below: it is refused all the same.

Capability gate (fail-closed): this fault family declares ``profile=host``, and
``capabilities.context.resolve_profile_for_state`` resolves a request to
``PROFILE_UNKNOWN`` whenever the fault scope's profile disagrees with the
resolved transport's profile. An unknown profile yields NO tools at all, so a
python-scope drill is only reachable over a host-profile channel (``ssh`` /
``kubewiz_host``); both ``kubeconfig`` and ``kubewiz_k8s`` (profile ``k8s``) are
refused before the model ever sees this tool.

This tool additionally passes ``expect_profile=PROFILE_HOST`` to the
transport, so a mismatched channel is refused at dispatch too. That
deliberately gives up the co-located ``kubeconfig`` case: consistency across
paths is chosen over that convenience, because a silent cross-profile execution
returns data from the wrong machine (see task-46317228).

When the channel IS host-profile but the fault is still not delivered, the cause
is the agent precondition — the error path below explains it.
"""

import json
import logging
import re
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from chaos_agent.config.settings import settings
from chaos_agent.agent.providers.chaosblade.verify import HEX16_UID_SHAPE
from chaos_agent.tools._tool_profiles import profile_for_tool
from chaos_agent.agent.providers.chaosblade.cli import (
    _get_host_blade_path,
    _split_args,
)
from chaos_agent.transports import execute_via_transport, resolve_transport_target

logger = logging.getLogger(__name__)

# Failed-create UID anchor for the python-application face (round-19 N2b):
# the blade CLI's raw failure JSON carries the registered experiment's id
# under the ``uid`` (or legacy ``result``) key even when the CLI reported
# failure; mining it lets the caller clean up rather than leak an active
# interception. The shape composes from the module-family single source —
# the pre-round-19 open ``{16,}`` bound admitted 40-hex sha256-shaped
# garbage (same disease as verify.py's RAW_FAILED_CREATE_UID_RE, fixed in
# the same round).
PY_FAILED_CREATE_UID_RE = re.compile(
    r'"(?:uid|result)"\s*:\s*"(' + HEX16_UID_SHAPE + r')"'
)

# The agent port blade actually tried to reach, rendered in the raw CLI error as
# ``http://127.0.0.1:<port>/create`` and/or ``dial tcp 127.0.0.1:<port>: connect:
# connection refused``. Pinning it lets the connection-refused path name WHICH
# prepare record blade bound to when several are RUNNING (the OLDEST-pick
# shadowing documented in the module header) instead of leaving the model to
# guess. Deterministic fact-finding for the presentation layer; the judgment of
# what to do about a shadowing record stays with the model.
_PY_BOUND_PORT_RE = re.compile(r"127\.0\.0\.1:(\d+)")

# Matcher flags accepted per fault target, mirroring the ChaosBlade Python
# plugin spec. Single source for the tool's matcher mapping: only names present
# here are forwarded, so an irrelevant matcher (e.g. ``cmd`` on a MySQL fault)
# is dropped instead of producing an "unknown flag" CLI error.
_TARGET_MATCHERS: dict[str, tuple[str, ...]] = {
    "redis": ("cmd", "key"),
    "mysql": ("sql", "sqltype", "database"),
    "sqlalchemy": ("sql", "sqltype", "database"),
    "http": ("url", "method", "host"),
    "httpx": ("url", "method", "host", "path"),
    "grpc": ("service", "method"),
    "kafka": ("topic", "operation"),
}

# The intent scope this tool serves. Used for the duration policy lookup so the
# fault-type table stays keyed the same way as every other injection path.
_PYTHON_SCOPE = "python"


def _build_matcher_args(target: str, values: dict[str, str]) -> list[str]:
    """Return ``--<matcher> <value>`` args valid for ``target`` (non-empty only)."""
    args: list[str] = []
    for name in _TARGET_MATCHERS.get(target, ()):
        value = (values.get(name) or "").strip()
        if value:
            args.extend([f"--{name}", value])
    return args


async def _enumerate_running_prepare_records(
    target, task_id: str
) -> list[dict[str, str]]:
    """Best-effort READ-ONLY enumeration of RUNNING python prepare records.

    Runs ``blade status --type prepare`` on the SAME host channel the injection
    used and returns ``[{uid, port, script, createtime}, ...]`` for every record
    whose ``Status`` is ``Running``. ANY failure (transport error, non-zero exit,
    unparseable output, missing ``result`` list) yields ``[]`` so the caller
    falls back to the generic single-record precondition message — this
    diagnostic must never break the error path it is enriching.

    Read-only by construction: it queries state and never mutates it, so it is
    safe to run on the failure path without the model's involvement. Deciding
    WHAT to do about a shadowing record (revoke a stale one vs. report a dead
    agent) stays with the model — the program only surfaces the facts.
    """
    argv = [_get_host_blade_path(), "status", "--type", "prepare"]
    try:
        res = await execute_via_transport(
            argv,
            target,
            timeout=settings.timeout_blade,
            task_id=task_id,
            bypass_channel=False,
            expect_profile=profile_for_tool("blade_python_create"),
        )
    except Exception:  # noqa: BLE001 - best-effort diagnostic, never fatal
        return []
    if res.exit_code != 0 or not (res.stdout or "").strip():
        return []
    try:
        payload = json.loads(res.stdout)
    except (ValueError, TypeError):
        return []
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, list):
        return []
    records: list[dict[str, str]] = []
    for rec in result:
        if not isinstance(rec, dict):
            continue
        if str(rec.get("Status", "")).strip().lower() != "running":
            continue
        records.append({
            "uid": str(rec.get("Uid", "") or ""),
            "port": str(rec.get("Port", "") or ""),
            # blade's prepare status puts the target-script path in ``Pid``.
            "script": str(rec.get("Pid", "") or ""),
            "createtime": str(rec.get("CreateTime", "") or ""),
        })
    return records


def _render_shadow_diagnosis(
    records: list[dict[str, str]], bound_port: str, combined: str
) -> str | None:
    """Explain the stale-record-shadow failure when ≥2 prepare records are RUNNING.

    Returns ``None`` when there is nothing to disambiguate (fewer than two
    RUNNING records), so the caller keeps the single-record app-restart message
    — that IS the likely cause when only the target's own record exists.

    When several records are RUNNING, blade resolves the port from the OLDEST one
    (module header, verified against chaosblade 1.9.0-alpha), so a stale record
    for an app that no longer runs SHADOWS a newer live prepare. The generic
    "a prepare record exists but NO agent is listening" wording hides this: it
    reads as a single-record problem and points the model at an app restart,
    sending it on a guessing detour (observed: ~300 log lines in the Case #3
    run1 residue). This presents the authoritative enumeration + the selection
    rule + BOTH remedies, and lets the model match the script path to its target
    to decide in one step.
    """
    if len(records) < 2:
        return None
    lines = []
    for rec in records:
        marker = (
            "   <-- blade bound here (connection refused)"
            if bound_port and rec["port"] == bound_port
            else ""
        )
        lines.append(
            f"  - port {rec['port'] or '?'}, uid {rec['uid'] or '?'}, "
            f"script {rec['script'] or '?'}{marker}"
        )
    enumeration = "\n".join(lines)
    bound_clause = (
        f"blade bound to port {bound_port} and got connection-refused."
        if bound_port
        else "blade bound to one of these ports and got connection-refused."
    )
    return (
        "Error: blade resolved the agent port from a RUNNING prepare record, but "
        "nothing was listening there, so the injection was not delivered. This "
        f"host has {len(records)} RUNNING python prepare records; blade resolves "
        "the port from the OLDEST one, so a stale record for an app that is no "
        "longer running can SHADOW a newer, live prepare (blade's documented "
        "selection behavior — not a fault of your target).\n"
        f"RUNNING prepare records:\n{enumeration}\n"
        f"{bound_clause}\n"
        "Decide by matching each record's script path to the app you are "
        "drilling:\n"
        "  - If a DIFFERENT (stale) record is shadowing your target's record, "
        "remove the stale one with blade_python_revoke(uid='<stale_uid>') so "
        "blade resolves your target's port, then retry this injection. Do NOT "
        "revoke a record a concurrent drill may still depend on.\n"
        "  - If your target's OWN record is the one not listening, the agent is "
        "not running inside the application: the hook written by prepare only "
        "takes effect when the app is RESTARTED with the hook directory on "
        "PYTHONPATH — that cannot be done mid-drill, so report it as a "
        "prerequisite failure instead of retrying.\n"
        f"Raw output: {combined}"
    )


@tool
async def blade_python_create(
    target: str,
    action: str,
    cmd: str = "",
    key: str = "",
    sql: str = "",
    sqltype: str = "",
    database: str = "",
    url: str = "",
    method: str = "",
    host: str = "",
    path: str = "",
    service: str = "",
    topic: str = "",
    operation: str = "",
    flags: str = "",
    task_id: str = "",
    *,
    state: Annotated[dict | None, InjectedState] = None,
) -> str:
    """Phase 2 ONLY. Inject an in-process method fault into a Python app
    (a real library call misbehaves). NOT Phase 1; NOT resource faults
    (→ blade_create). Builds `blade create python <target> <action>
    [matchers] [flags]` on the app host.

    Inputs:
      - target: redis|mysql|sqlalchemy|http|httpx|grpc|kafka.
      - action: delay|throwCustomException|returnValue.
      - matchers/flags: `chaosblade-cli.md` §python-app-faults. No
        matchers = EVERY call of that client (wide blast); invalid ones
        ignored. Pitfalls: delay REQUIRES --time (ms); unresolvable
        --exception SILENTLY degrades to RuntimeError; --return-value has
        NO "nil" keyword (literal 3-char string).

    Output: blade CLI JSON; success carries `result.uid`; failure starts
    "Error:". Side effects: patches the intercepted method inside the
    RUNNING app process; no OS/container/K8s changes.

    Constraints:
      - PRECONDITION: agent LISTENING inside the app AND a RUNNING
        prepare record. Missing record → fixable (blade_python_prepare);
        missing agent → NOT (needs app restart) — tell by the CLI error.
      - Verification is application-level (latency/exception/return
        value); system metrics stay normal by design — NOT a failed
        injection.
    """
    argv = [_get_host_blade_path(), "create", "python", target, action]
    argv.extend(_build_matcher_args(target, {
        "cmd": cmd, "key": key,
        "sql": sql, "sqltype": sqltype, "database": database,
        "url": url, "method": method, "host": host, "path": path,
        "service": service, "topic": topic, "operation": operation,
    }))
    if flags:
        argv.extend(_split_args(flags))

    # Duration guarantee: same policy source as every other injection path.
    from chaos_agent.utils.fault_type import ensure_min_duration, normalize_timeout_flag

    timeout_value = normalize_timeout_flag(argv)
    # ``warn_explicit_below_floor=False``: a present ``--timeout`` here is the
    # engine-pinned RECOVERY TIMER (D+G, layer 3 of the three-layer duration
    # guarantee), not the observation window D the floor is derived from — the
    # D-owning layer already warned. The absent (None) branch is unaffected: it
    # still surfaces the configured-default warning (see ensure_min_duration).
    effective_timeout = ensure_min_duration(
        timeout_value, _PYTHON_SCOPE, target, action,
        warn_explicit_below_floor=False,
    )
    if timeout_value is None:
        argv.extend(["--timeout", str(effective_timeout)])
        logger.info(
            "Auto-injected --timeout %ss into blade create python command",
            effective_timeout,
        )
    else:
        try:
            current_int = int(timeout_value)
        except (ValueError, TypeError):
            current_int = 0
        if effective_timeout != current_int:
            argv[argv.index("--timeout") + 1] = str(effective_timeout)
            logger.info(
                "Normalized --timeout from %ss to %ss for python-%s-%s",
                timeout_value, effective_timeout, target, action,
            )

    try:
        result = await execute_via_transport(
            argv,
            resolve_transport_target(state),
            timeout=settings.timeout_blade,
            task_id=task_id,
            bypass_channel=False,
            expect_profile=profile_for_tool("blade_python_create"),
        )
    except Exception as e:
        return f"Error: blade create python failed: {e}"

    if result.exit_code != 0:
        parts = [
            stream.strip()
            for stream in (result.stdout, result.stderr)
            if stream and stream.strip()
        ]
        combined = "\n".join(parts) if parts else "(no output)"

        # Precondition surfacing: this is the ONE point in the pipeline that
        # knows the fault is a Python-application fault, so it is where the
        # agent precondition is explained. A generic preflight check cannot do
        # this — it has no fault scope and would warn every k8s/host drill.
        #
        # The two failure modes have DIFFERENT remedies and must not be merged.
        # Match strings are the CLI's verbatim wording (chaosblade 1.9.0-alpha):
        #   no record:  "invalid `port` parameter value: ``. no running python
        #                preparation record found"
        #   no agent:   "... connect: connection refused" / "python agent is not
        #                running"
        lowered = combined.lower()
        if "no running python preparation record" in lowered:
            return (
                "Error: no RUNNING python preparation record exists on this host, "
                "so blade could not resolve an agent port and the injection was "
                "not delivered. Call blade_python_prepare(port=..., "
                "python_path=..., target_script=...) and retry this injection.\n"
                "If prepare succeeds but this injection then fails with "
                "'connection refused', the agent is not running inside the "
                "application — that needs an application restart and cannot be "
                "fixed mid-drill.\n"
                f"Raw output: {combined}"
            )
        if "connection refused" in lowered or "agent is not running" in lowered:
            # Multi-record disambiguation FIRST: blade resolves the port from the
            # OLDEST RUNNING prepare record, so a stale record for a dead app can
            # SHADOW the target's live agent. The generic single-record wording
            # below hides this and points the model at an app restart, which is
            # the wrong remedy when the real cause is a shadowing record (Case #3
            # run1: ~300 log lines of guessing before the model found the two
            # records on its own). Enumerate the RUNNING records read-only and,
            # when there are ≥2, present the authoritative breakdown so the model
            # self-corrects in one step. Falls back to the single-record message
            # when there is nothing to disambiguate (or enumeration fails).
            bound_match = _PY_BOUND_PORT_RE.search(combined)
            bound_port = bound_match.group(1) if bound_match else ""
            records = await _enumerate_running_prepare_records(
                resolve_transport_target(state), task_id
            )
            shadow = _render_shadow_diagnosis(records, bound_port, combined)
            if shadow is not None:
                return shadow
            return (
                "Error: a prepare record exists but NO agent is listening on its "
                "port, so the injection was not delivered. The hook file written "
                "by prepare only takes effect when the application is RESTARTED "
                "with the hook directory on PYTHONPATH — that cannot be done "
                "mid-drill. Report this as a prerequisite failure instead of "
                "retrying.\n"
                "Two things to confirm before reporting: (a) the application was "
                "started with the hook on PYTHONPATH; (b) the command landed on "
                "the machine that runs the application — blade only reaches an "
                "agent on its OWN machine and takes no agent-host parameter. "
                "Reaching this tool at all means the capability gate already "
                "accepted a host-profile channel, so the command did land on a "
                "specific machine.\n"
                f"Raw output: {combined}"
            )

        # A UID in the output means the experiment WAS registered even though the
        # CLI reported failure — surface it so the caller can clean up rather
        # than leaking an active interception.
        uid_match = PY_FAILED_CREATE_UID_RE.search(combined)
        if uid_match:
            return (
                f"Error: blade create python failed (exit {result.exit_code}) but an "
                f"experiment was registered (UID: {uid_match.group(1)}). "
                f"Verify the application-side effect; if absent, clean up with "
                f"blade_destroy(uid='{uid_match.group(1)}').\n"
                f"Raw output: {combined}"
            )
        return f"Error: blade create python failed (exit {result.exit_code}): {combined}"

    return result.stdout


@tool
async def blade_python_prepare(
    target_script: str,
    port: int = 9526,
    python_path: str = "",
    task_id: str = "",
    *,
    state: Annotated[dict | None, InjectedState] = None,
) -> str:
    """Write the ChaosBlade Python agent startup hook for a target app.
    Runs `blade prepare python --port <port> --target-script <script>` on
    the target host. PRECONDITION step, not a fault.

    What it does (verified): writes `sitecustomize.py` into the DIRECTORY
    OF `target_script` (adds blade's agent library to sys.path, starts
    the agent on `port`). Does NOT start an agent itself or touch the
    running application.

    When to use:
      - An injection fails with "no running python preparation record
        found"; retry the injection after this succeeds.

    Inputs:
      - target_script: REQUIRED app entry-script path (CLI rejects
        without it). The hook lands in its directory, which must be on
        the app's PYTHONPATH.
      - port: agent port (default 9526); must be FREE (in-use refused).
      - python_path: optional interpreter path running the app.

    Output: JSON carrying the prepare UID (only for blade_python_revoke).
    Failure starts "Error:". Side effects: writes a file next to the
    target script + host state shared by every drill on that host.

    Constraints:
      - Success does NOT mean an agent is running: the hook executes only
        when the app (re)starts with its directory on PYTHONPATH.
        `blade status --type prepare` "Running" is bookkeeping, not
        liveness. If the app cannot restart, report prerequisite failure
        — more prepare calls won't fix it.
      - Still "connection refused" after success → the agent is not in
        the process: stop and report, do not retry.
      - Writes shared host state: do NOT revoke during recovery — a
        concurrent drill may still use it.
    """
    if not (target_script or "").strip():
        return (
            "Error: target_script is required — `blade prepare python` rejects the "
            "command without --target-script. Provide the target application's "
            "entry script path; its directory is where the agent hook file is "
            "written."
        )
    argv = [_get_host_blade_path(), "prepare", "python", "--port", str(port)]
    argv.extend(["--target-script", target_script])
    if python_path:
        argv.extend(["--python-path", python_path])

    try:
        result = await execute_via_transport(
            argv,
            resolve_transport_target(state),
            timeout=settings.timeout_blade,
            task_id=task_id,
            bypass_channel=False,
            expect_profile=profile_for_tool("blade_python_prepare"),
        )
    except Exception as e:
        return f"Error: blade prepare python failed: {e}"

    if result.exit_code != 0:
        stderr = result.stderr.strip() if result.stderr else ""
        stdout = result.stdout.strip() if result.stdout else ""
        # Merge when both carry content — `or` drops one side's evidence.
        _detail = "\n".join(p for p in (stdout, stderr) if p) or "(no output)"
        return (
            f"Error: blade prepare python failed (exit {result.exit_code}): "
            f"{_detail}"
        )
    return result.stdout


@tool
async def blade_python_revoke(
    uid: str,
    task_id: str = "",
    *,
    state: Annotated[dict | None, InjectedState] = None,
) -> str:
    """Undo a ChaosBlade Python agent preparation (`blade revoke <PREPARE UID>`).

    Removes the preparation created by blade_python_prepare. This is teardown of
    a PRECONDITION, not fault recovery — to recover a fault, use blade_destroy
    with the EXPERIMENT uid.

    What it actually does (verified): it DELETES the `sitecustomize.py` hook file
    written by prepare. An already-running agent stays alive in the current
    process, but the application loses its agent on the next restart, and the
    drill capability cannot be restored without another prepare plus a restart.

    When to use:
      - Only when explicitly decommissioning the drill environment on a host.

    Inputs:
      - uid: the PREPARE uid returned by blade_python_prepare (NOT an
        experiment uid).

    Output: JSON from the blade CLI; failure starts with "Error:".

    Side effects: deletes the hook file and removes host-level state shared by
                  every drill on that host.

    Constraints:
      - Do NOT call this during recovery. It does not stop any fault, and it
        deletes shared setup a concurrent drill may still depend on. Destroying
        the experiment (blade_destroy) is what removes the fault.
      - Deleting the hook affects every application whose hook lives in that
        directory, not only the drill that created the record.
    """
    argv = [_get_host_blade_path(), "revoke", uid]
    try:
        result = await execute_via_transport(
            argv,
            resolve_transport_target(state),
            timeout=settings.timeout_blade,
            task_id=task_id,
            bypass_channel=False,
            expect_profile=profile_for_tool("blade_python_revoke"),
        )
    except Exception as e:
        return f"Error: blade revoke failed: {e}"

    if result.exit_code != 0:
        stderr = result.stderr.strip() if result.stderr else ""
        stdout = result.stdout.strip() if result.stdout else ""
        # Merge when both carry content — `or` drops one side's evidence.
        _detail = "\n".join(p for p in (stdout, stderr) if p) or "(no output)"
        return (
            f"Error: blade revoke failed (exit {result.exit_code}): "
            f"{_detail}"
        )
    return result.stdout


__all__ = [
    "blade_python_create",
    "blade_python_prepare",
    "blade_python_revoke",
]
