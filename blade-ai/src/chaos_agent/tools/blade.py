"""ChaosBlade CLI tool wrappers for LangGraph @tool functions.

Tool signatures faithfully map ChaosBlade K8s scenario parameters so the LLM
can naturally pass --namespace, --names, --labels, --kubeconfig etc. when needed.
Scene-specific flags (e.g. --time, --cpu-count) remain in the generic `flags` param.
"""

import logging
import shlex
from typing import Literal

from langchain_core.tools import tool

from chaos_agent.config.settings import settings
from chaos_agent.tools.shell import run_command

logger = logging.getLogger(__name__)


def _split_args(args: str) -> list[str]:
    """Split args string respecting shell quoting.

    Uses shlex.split to properly handle quoted arguments.
    Falls back to str.split() if shlex encounters unmatched quotes
    (e.g. LLM-generated malformed args).
    """
    if not args:
        return []
    try:
        return shlex.split(args)
    except ValueError:
        return args.split()


def _get_blade_path() -> str:
    """Resolve blade binary path: explicit setting > bundled > system PATH."""
    if settings.blade_path:
        return settings.blade_path
    return settings._resolve_blade_path()


def _build_kubeconfig_arg(kubeconfig: str = "") -> list[str]:
    """Build --kubeconfig flag for blade commands.

    Priority: explicit parameter > settings (includes KUBECONFIG env via AliasChoices).

    NOTE: ChaosBlade v1.8.0 ``blade status`` does NOT support --kubeconfig.
    Only ``blade create``, ``blade destroy``, and ``blade query k8s`` accept it.
    For ``blade status``, the caller must set the KUBECONFIG env var instead.
    """
    kc = kubeconfig or settings.kubeconfig_path
    if kc:
        return ["--kubeconfig", kc]
    return []


def _build_kubeconfig_env(kubeconfig: str = "") -> dict[str, str] | None:
    """Build env override with KUBECONFIG set for blade commands that don't
    support the --kubeconfig flag (e.g. ``blade status`` in v1.8.0).

    Returns None if no kubeconfig override is needed (let existing env pass through).
    """
    kc = kubeconfig or settings.kubeconfig_path
    if kc:
        return {"KUBECONFIG": kc}
    return None


@tool
async def blade_create(
    scope: Literal["pod", "container", "node"],
    target: str,
    action: str,
    namespace: str = "",
    names: str = "",
    labels: str = "",
    kubeconfig: str = "",
    evict_count: str = "",
    evict_percent: str = "",
    flags: str = "",
    task_id: str = "",
) -> str:
    """Phase 2 ONLY. Create a ChaosBlade K8s fault injection experiment.

    Mutating: triggers actual chaos against the target. NOT available in
    Phase 1 (planning). Returns the experiment UID for tracking and
    later destroy.

    Generates `blade create k8s <scope>-<target> <action> [flags]`.

    When to use:
      - Phase 2 execution, after the plan is approved.
      - Do NOT use during planning to "test" — Phase 1 must only inspect with
        kubectl/blade_status; injection is execution-only.

    Inputs:
      - scope: "pod" | "container" | "node".
      - target: fault target — "cpu" | "memory" | "network" | "disk" | "process" | "pod".
      - action: "fullload" | "drop" | "dns" | "occupy" | "fill" | "burn" | "kill" | "delete".
      - namespace / names / labels / kubeconfig / evict_count / evict_percent: passthrough.
      - flags: scenario-specific CLI string. Examples:
          pod-cpu fullload    → "--cpu-percent 80"
          pod-network drop    → "--interface eth0" (drops all packets, no --percent)
          node-disk fill      → "--path /tmp --size 1024"
        See knowledge resource `chaosblade-cli.md` for the full flag catalog.

    Output: JSON from blade CLI. Success carries `result.uid` (use it for
            blade_destroy / blade_status). Failure starts with "Error:".

    Side effects: Creates a CRD in the target namespace; injects real fault
                  into the target pod/container/node.

    Constraints (MUST READ before calling):
      - scope="pod": targets the whole Pod; do NOT pass --container-names.
      - scope="container": requires --container-ids or --container-names in flags.
      - scope="node": ChaosBlade rejects --namespace and --labels for node scope —
        this tool auto-omits them. Use --names to identify the node.
      - Memory flags: pod scope accepts --mem-percent or --mem-size; node scope
        accepts --mem-percent ONLY (--mem-size is rejected on node).
      - --namespace compatibility: host-installed blade binaries may reject
        --namespace on k8s subcommands. If you see "unknown flag: --namespace",
        retry without it (this is a version issue, not a syntax error).
      - --timeout is auto-injected / auto-boosted to the recommended minimum for
        the fault type (≥ 600s). You only need to set --timeout if you want a
        longer duration; you cannot make it shorter.
    """
    # Universal first-use trigger: pip-install users get a pure-Python wheel
    # with no blade binary. Ensure it exists before the first mutating
    # injection — off the event loop, best-effort. If the download fails
    # (offline), the host blade path below fails and callers (direct_execute)
    # fall back to kubectl exec into a cluster tool pod. This is the ONE
    # chokepoint every injection path funnels through (CLI direct, CLI NL,
    # TUI, server API), so it's the single place that needs the trigger.
    try:
        from chaos_agent.chaosblade_installer import ensure_chaosblade_async
        await ensure_chaosblade_async()
    except Exception as e:
        logger.warning("ChaosBlade ensure failed (continuing to kubectl-exec fallback): %s", e)

    # ChaosBlade K8s format: blade create k8s <scope>-<target> <action>
    cmd = [_get_blade_path(), "create", "k8s", f"{scope}-{target}", action]

    # K8s scenario common flags
    # Node scope uses --names to identify targets; ChaosBlade does NOT accept
    # --namespace or --labels for node-scope commands.
    if namespace and scope != "node":
        cmd.extend(["--namespace", namespace])
    if names:
        cmd.extend(["--names", names])
    if labels and scope != "node":
        cmd.extend(["--labels", labels])
    cmd.extend(_build_kubeconfig_arg(kubeconfig))
    if evict_count:
        cmd.extend(["--evict-count", evict_count])
    if evict_percent:
        cmd.extend(["--evict-percent", evict_percent])

    # Scene-specific flags
    if flags:
        cmd.extend(_split_args(flags))

    # Auto-inject --timeout if not present, or boost if below minimum
    # This is the BOTTOM layer of the three-layer duration guarantee,
    # ensuring ALL paths (CLI, direct_execute, NL execute_loop) are covered.
    from chaos_agent.utils.fault_type import ensure_min_duration

    if "--timeout" not in cmd:
        # No timeout specified: auto-inject recommended minimum
        effective_timeout = ensure_min_duration(None, scope, target, action)
        cmd.extend(["--timeout", str(effective_timeout)])
        logger.info(f"Auto-injected --timeout {effective_timeout}s into blade create command")
    else:
        # Timeout specified (by LLM or CLI): check if it meets the minimum
        timeout_idx = cmd.index("--timeout")
        if timeout_idx + 1 < len(cmd):
            current_val = cmd[timeout_idx + 1].rstrip("sS")
            cmd[timeout_idx + 1] = current_val
            try:
                current_int = int(current_val)
            except (ValueError, TypeError):
                current_int = 0
            effective_timeout = ensure_min_duration(current_val, scope, target, action)
            if effective_timeout != current_int:
                cmd[timeout_idx + 1] = str(effective_timeout)
                logger.info(
                    f"Auto-boosted --timeout from {current_val}s to {effective_timeout}s "
                    f"for {scope}-{target}-{action} (recommended minimum)"
                )

    try:
        result = await run_command(cmd, timeout=settings.timeout_blade, task_id=task_id)
    except Exception as e:
        return f"Error: blade create failed: {e}"

    if result.exit_code != 0:
        # Combine both streams: JSON (including 54000) may land on stdout
        # while error details go to stderr.  Include both so callers can
        # parse the blade_uid from either stream.
        parts = []
        if result.stdout and result.stdout.strip():
            parts.append(result.stdout.strip())
        if result.stderr and result.stderr.strip():
            parts.append(result.stderr.strip())
        combined = "\n".join(parts) if parts else "(no output)"

        # If a UID is present in the output, the CRD was created even though
        # execution reported an error. The experiment may actually be in effect
        # (e.g., ChaosBlade used a fallback mechanism like tc instead of iptables).
        # Use raw JSON parsing here — NOT extract_blade_uid, which intentionally
        # rejects 54000+success=false UIDs. We want the UID regardless of
        # blade's self-reported success status, because the CRD exists in the
        # cluster and may be causing real effects.
        import re
        uid_match = re.search(r'"uid"\s*:\s*"([a-f0-9]{16,})"', combined)
        uid_in_error = uid_match.group(1) if uid_match else None
        if uid_in_error:
            return (
                f"Warning: blade create returned error (exit {result.exit_code}) "
                f"but experiment CRD was created (UID: {uid_in_error}). "
                f"The experiment MAY be in effect despite the error "
                f"(ChaosBlade operator may retry with fallback mechanisms). "
                f"Do NOT conclude failure or attempt alternative injection methods. "
                f"Instead, POLL the cluster state repeatedly to check if the "
                f"fault takes effect (the operator needs time to retry):\n"
                f"  1. Call time_wait(seconds=30) to give the operator time to retry\n"
                f"  2. Check the target's actual status with kubectl get node/pod\n"
                f"  3. If fault effect is visible, the injection SUCCEEDED "
                f"— report success with UID {uid_in_error}\n"
                f"  4. If not visible, call time_wait(seconds=30) once more "
                f"and check again\n"
                f"  5. Only after 2 waits + checks with NO fault effect, "
                f"conclude failure\n"
                f"  6. Do NOT try alternative injection methods before "
                f"completing these checks\n"
                f"Raw output: {combined}"
            )
        return f"Error: blade create failed (exit {result.exit_code}): {combined}"

    return result.stdout


@tool
async def blade_destroy(uid: str, kubeconfig: str = "") -> str:
    """Mutating. Destroy a ChaosBlade experiment by UID to recover the fault.

    Runs `blade destroy <UID>`. NOT available in Phase 1 planning — the
    runtime classifies this as a mutation and the phase 1 screener will
    reject it. Use this in the recover graph or via framework-controlled
    cleanup paths only.

    When to use:
      - Recovery phase, or to abort an in-progress injection.
      - Do NOT use in Phase 2 execution — destruction is framework-controlled
        (recover graph or replan).

    Inputs:
      - uid: experiment UID returned by blade_create (`result.uid`) or blade_status.
      - kubeconfig: optional override (defaults to settings + KUBECONFIG env).

    Output: JSON from blade CLI; failure starts with "Error:".

    Side effects: Removes the CRD; the target should return to normal.

    Constraints (MUST READ before calling):
      - Always re-verify with blade_status — Status should flip to "Destroyed".
        See knowledge resource `failure-modes.md` (recovery failure) for the
        rare case where destroy returns success but the stress process lingers.
    """
    cmd = [_get_blade_path(), "destroy", uid]
    cmd.extend(_build_kubeconfig_arg(kubeconfig))

    try:
        result = await run_command(cmd, timeout=settings.timeout_blade)
    except Exception as e:
        return f"Error: blade destroy failed: {e}"

    if result.exit_code != 0:
        return f"Error: blade destroy failed (exit {result.exit_code}): {result.stderr}"

    return result.stdout


@tool
async def blade_status(uid: str = "", kubeconfig: str = "") -> str:
    """Phase 1 / Phase 2 read-only. Query a ChaosBlade experiment's CLI-side status.

    Runs `blade status [--uid <UID>]`. Read-only — listing existing
    experiments does not mutate cluster state.

    When to use:
      - Verifier Layer 1: confirm the experiment is "Success" after blade_create.
      - Recovery verification: confirm the experiment is "Destroyed" after blade_destroy.
      - Use `blade_query_k8s` instead when you need cluster-side state (which
        pods/nodes are actually affected).

    Inputs:
      - uid: experiment UID. Empty → lists all experiments (output may be large).
      - kubeconfig: optional override.

    Output: JSON with Uid / Command / Status / Error / CreateTime / UpdateTime.
            Status ∈ {Created, Success, Error, Destroyed}.

    Side effects: None (read-only).

    Constraints (MUST READ before calling):
      - blade_status v1.8.0 ignores the --kubeconfig CLI flag; this tool passes
        kubeconfig via the KUBECONFIG env var instead. No action required from
        the caller.
    """
    cmd = [_get_blade_path(), "status"]
    if uid:
        cmd.extend(["--uid", uid])
    # blade status in v1.8.0 does NOT support --kubeconfig flag;
    # pass via KUBECONFIG env var instead
    env_override = _build_kubeconfig_env(kubeconfig)

    try:
        result = await run_command(cmd, timeout=settings.timeout_blade, env_override=env_override)
    except Exception as e:
        return f"Error: blade status failed: {e}"

    return result.stdout


@tool
async def blade_help(subcommand: str = "") -> str:
    """Phase 1 / Phase 2 read-only. Query ChaosBlade CLI help for any subcommand.

    Runs `blade [subcommand...] -h`. Read-only — only prints help text,
    never creates or modifies experiments.

    When to use:
      - Phase 1 planning: verify correct flags before writing the plan.
        Skill docs and knowledge resources may be outdated; this tool
        gives the ground truth from the installed blade binary.
      - Phase 2 execution: double-check flag syntax before blade_create.

    Inputs:
      - subcommand: space-separated subcommand path. Examples:
          ""                            → `blade -h` (top-level help)
          "create"                      → `blade create -h`
          "create k8s"                  → `blade create k8s -h`
          "create k8s pod-network"      → `blade create k8s pod-network -h`
          "create k8s pod-network drop" → `blade create k8s pod-network drop -h`

    Output: Help text from the blade CLI.

    Side effects: None (read-only).
    """
    tokens = _split_args(subcommand)
    tokens = [t for t in tokens
              if t not in ("-h", "--help") and not t.startswith("--")]
    cmd = [_get_blade_path()] + tokens + ["-h"]

    try:
        result = await run_command(cmd, timeout=10)
    except Exception as e:
        return f"Error: blade help failed: {e}"

    output = result.stdout.strip()
    if not output and result.stderr:
        output = result.stderr.strip()
    return output or "(no help output)"


@tool
async def blade_query_k8s(uid: str = "", kubeconfig: str = "") -> str:
    """Phase 2 read-only. Query the cluster-side status of a ChaosBlade K8s experiment.

    Runs `blade query k8s create <UID>`. Returns which pods / nodes the
    experiment actually selected, distinct from blade_status which only shows
    CLI-side state.

    When to use:
      - Verifier Layer 1: confirm the selector matched the intended targets.
      - Diagnose "blade returned Success but nothing happened" — check whether
        statuses[].kind / identifier match the expected resources.

    Inputs:
      - uid: experiment UID (required; empty UID returns an error).
      - kubeconfig: optional override.

    Output: JSON, e.g. `{"code":200,"success":true,"result":{"uid":"...",
            "statuses":[{"state":"Success","kind":"pod","identifier":"ns/node/pod/container/runtime"}]}}`.

    Side effects: None (read-only).

    Constraints (MUST READ before calling):
      - This tool only handles `blade query k8s`. For host-side queries
        (disk / network interface / jvm) use the kubectl tool to invoke
        them inside a debug pod.
    """
    cmd = [_get_blade_path(), "query", "k8s"]
    if uid:
        cmd.extend(["create", uid])
    cmd.extend(_build_kubeconfig_arg(kubeconfig))
    # Also pass KUBECONFIG env var as fallback (belt-and-suspenders with --kubeconfig)
    env_override = _build_kubeconfig_env(kubeconfig)

    try:
        result = await run_command(cmd, timeout=settings.timeout_blade, env_override=env_override)
    except Exception as e:
        return f"Error: blade query k8s failed: {e}"

    if result.exit_code != 0:
        # blade query k8s may write JSON error details to stderr
        err = result.stderr.strip() if result.stderr else ""
        stdout = result.stdout.strip() if result.stdout else ""
        # Some versions put full JSON response on stderr even on error
        combined = stdout or err
        if combined and not combined.startswith("Error") and not combined.startswith("`"):
            return combined
        if err:
            return f"Error: blade query k8s failed (exit {result.exit_code}): {err}"
        return ""

    output = result.stdout.strip()
    # Some ChaosBlade versions write JSON to stderr instead of stdout
    if not output and result.stderr and not result.stderr.startswith("Error"):
        output = result.stderr.strip()
    return output
