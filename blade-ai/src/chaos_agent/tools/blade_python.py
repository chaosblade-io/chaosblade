"""ChaosBlade Python-application (in-process agent) CLI tool wrappers.

Separate module from ``blade.py`` on purpose: this fault domain has a different
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
     prepare — revoke stale records before relying on a new port.
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

``--direct`` mode is gated identically (``direct_execute`` runs the same
profile check before any injection preparation), and this tool additionally
passes ``expect_profile=PROFILE_HOST`` to the transport, so a mismatched channel
is refused at dispatch too. That deliberately gives up the co-located
``kubeconfig`` case that direct mode used to allow: consistency across paths was
chosen over that convenience, because a silent cross-profile execution returns
data from the wrong machine (see task-46317228).

When the channel IS host-profile but the fault is still not delivered, the cause
is the agent precondition — the error path below explains it.
"""

import logging

from langchain_core.tools import tool

from chaos_agent.config.settings import settings
from chaos_agent.tools._tool_profiles import profile_for_tool
from chaos_agent.tools.blade import _get_host_blade_path, _split_args
from chaos_agent.transports import TransportTarget, execute_via_transport

logger = logging.getLogger(__name__)

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
      - --timeout auto-injected/boosted; may lengthen, not shorten.
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
    effective_timeout = ensure_min_duration(
        timeout_value, _PYTHON_SCOPE, target, action,
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
                "Auto-boosted --timeout from %ss to %ss for python-%s-%s",
                timeout_value, effective_timeout, target, action,
            )

    try:
        result = await execute_via_transport(
            argv,
            TransportTarget.from_state({}),
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
        import re

        uid_match = re.search(r'"(?:uid|result)"\s*:\s*"([a-f0-9]{16,})"', combined)
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
            TransportTarget.from_state({}),
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
        return (
            f"Error: blade prepare python failed (exit {result.exit_code}): "
            f"{stderr or stdout or '(no output)'}"
        )
    return result.stdout


@tool
async def blade_python_revoke(uid: str, task_id: str = "") -> str:
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
            TransportTarget.from_state({}),
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
        return (
            f"Error: blade revoke failed (exit {result.exit_code}): "
            f"{stderr or stdout or '(no output)'}"
        )
    return result.stdout


__all__ = [
    "blade_python_create",
    "blade_python_prepare",
    "blade_python_revoke",
]
