"""ChaosBlade CLI tool wrappers for LangGraph @tool functions.

Tool signatures faithfully map ChaosBlade K8s scenario parameters so the LLM
can naturally pass --namespace, --names, --labels, --kubeconfig etc. when needed.
Scene-specific flags (e.g. --time, --cpu-count) remain in the generic `flags` param.
"""

import logging
import shlex
from typing import Literal

from langchain_core.tools import tool

from chaos_agent.agent.spec.fault_registry import is_host_scope
from chaos_agent.config.settings import settings
from chaos_agent.tools._tool_profiles import profile_for_tool
from chaos_agent.transports import (
    KUBEWIZ_CHANNELS,
    PROFILE_HOST,
    TransportTarget,
    execute_via_transport,
    is_kubewiz_channel,
    profile_of,
    resolve_channel_name,
)
from chaos_agent.transports.protocol import explain_transport_anomaly

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


def _get_host_blade_path() -> str:
    """Resolve the blade binary name for host scope.

    Host-scope injections run blade ON the remote host (wrapped in
    ``wiz task exec``), so the path must be valid THERE, not locally. Never
    reuse ``_get_blade_path()`` / ``settings.blade_path`` here: those resolve a
    *local* absolute path (bundled / vendored), which is meaningless remotely
    and would leak a local path into the remote command. Default to the bare
    ``blade`` on the remote PATH; override via ``settings.host_blade_path``
    only when the remote binary lives outside PATH.
    """
    return settings.host_blade_path or "blade"


def _build_kubeconfig_arg(kubeconfig: str = "") -> list[str]:
    """Build cluster connection flags for blade commands (kubeconfig mode only).

    NOTE: ChaosBlade v1.8.0 ``blade status`` does NOT support --kubeconfig.
    Only ``blade create``, ``blade destroy``, and ``blade query k8s`` accept it.
    For ``blade status``, the caller must set the KUBECONFIG env var instead.
    """
    kc = kubeconfig or settings.kubeconfig_path
    if kc:
        return ["--kubeconfig", kc]
    return []


def _build_blade_kubewiz_args(target: TransportTarget) -> list[str]:
    """Build kubewiz connection flags for blade commands."""
    args: list[str] = []
    if settings.kubewiz_url:
        args.extend(["--kubewiz-url", settings.kubewiz_url])
    if target.kubewiz_cluster_uuid:
        args.extend(["--cluster-uuid", target.kubewiz_cluster_uuid])
    if settings.kubewiz_token:
        args.extend(["--kubewiz-token", settings.kubewiz_token])
    return args


def _build_kubeconfig_env(kubeconfig: str = "") -> dict[str, str] | None:
    """Build env override with KUBECONFIG set for blade commands that don't
    support the --kubeconfig flag (e.g. ``blade status`` in v1.8.0).
    """
    kc = kubeconfig or settings.kubeconfig_path
    if kc:
        return {"KUBECONFIG": kc}
    return None


@tool
async def blade_create(
    scope: Literal["pod", "container", "node", "host"],
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
    """Phase 2 ONLY — mutating: create a ChaosBlade K8s/host fault
    experiment (real chaos on the target). NOT Phase 1 (inspect via
    kubectl_read/blade_status). Builds `blade create k8s <scope>-<target>
    <action> [flags]` (host: `blade create <target> <action> [flags]`).

    Inputs:
      - scope: pod|container|node|host. target: cpu|memory|network|disk|
        process|pod. action: fullload|drop|dns|occupy|fill|burn|kill|delete.
      - namespace/names/labels/kubeconfig/evict_*: passthrough.
      - flags: scenario flags (catalog: `chaosblade-cli.md`); drop →
        "--interface eth0" (drops ALL packets; no --percent).

    Output: blade CLI JSON; success carries `result.uid` (for
    blade_destroy/blade_status); failure starts "Error:".

    Side effects: creates a CRD; injects the real fault.

    Constraints:
      - scope="pod" = whole Pod (never --container-names);
        scope="container" REQUIRES --container-ids/--container-names.
      - scope="node": --namespace/--labels rejected, auto-omitted; select
        via --names. scope="host": OS-executor; no namespace/labels/
        kubeconfig; params in `flags`.
      - Memory: pod: --mem-percent|--mem-size; node: --mem-percent ONLY.
      - "unknown flag: --namespace" (host blade) = version issue — retry
        without it.
      - --timeout auto-injected/boosted to ≥600s; may lengthen, not
        shorten.
    """
    # Universal first-use trigger: pip-install users get a pure-Python wheel
    # with no blade binary. Ensure it exists before the first mutating
    # injection — off the event loop, best-effort. If the download fails
    # (offline), the host blade path below fails and callers (direct_execute)
    # fall back to kubectl exec into a cluster tool pod. This is the ONE
    # chokepoint every injection path funnels through (CLI direct, CLI NL,
    # TUI, server API), so it's the single place that needs the trigger.
    #
    # Host scope runs blade on the REMOTE host (not locally), so the bundled
    # local blade is irrelevant — skip the download to avoid needless work
    # and misleading "blade not found" warnings.
    _is_host = is_host_scope(scope)
    if not _is_host:
        try:
            from chaos_agent.chaosblade_installer import ensure_chaosblade_async
            await ensure_chaosblade_async()
        except Exception as e:
            logger.warning("ChaosBlade ensure failed (continuing to kubectl-exec fallback): %s", e)

    # ChaosBlade K8s format: blade create k8s <scope>-<target> <action>.
    # Host format is different — the OS executor takes `blade create
    # <target> <action>` with NO k8s domain / scope prefix / namespace /
    # labels / kubeconfig. Remote delivery is the agent's host transport
    # (SSH): it runs the command ON the target host, so blade records the
    # experiment in that host's local DB and blade_destroy over the same
    # transport recovers it (no blade `--channel ssh` needed here).
    if _is_host:
        cmd = [_get_host_blade_path(), "create", target, action]
    else:
        cmd = [_get_blade_path(), "create", "k8s", f"{scope}-{target}", action]

    _target = TransportTarget.from_state({})
    _kubewiz = False
    if not _is_host:
        # K8s scenario common flags
        # Node scope uses --names to identify targets; ChaosBlade does NOT accept
        # --namespace or --labels for node-scope commands.
        if namespace and scope != "node":
            cmd.extend(["--namespace", namespace])
        if names:
            cmd.extend(["--names", names])
        if labels and scope != "node":
            cmd.extend(["--labels", labels])
        # Channel selection is fail-soft: is_kubewiz_channel degrades to kubeconfig
        # if resolution fails. Config validity is guaranteed upstream (settings
        # kube_connection_mode validator + preflight check_transport_config), so an
        # unresolvable channel never reaches this injection path in practice.
        # kubewiz mode: blade reaches KubeWiz Core via its own --kubewiz-url flags,
        # so it runs locally and must NOT be re-wrapped in `wiz task exec`
        # (bypass_channel=True below).
        _kubewiz = is_kubewiz_channel()
        if _kubewiz:
            cmd.extend(_build_blade_kubewiz_args(_target))
        else:
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
    from chaos_agent.utils.fault_type import ensure_min_duration, normalize_timeout_flag

    timeout_value = normalize_timeout_flag(cmd)
    if timeout_value is None:
        # No timeout specified: auto-inject recommended minimum
        effective_timeout = ensure_min_duration(None, scope, target, action)
        cmd.extend(["--timeout", str(effective_timeout)])
        logger.info(f"Auto-injected --timeout {effective_timeout}s into blade create command")
    else:
        # Timeout specified (by LLM or CLI): check if it meets the minimum.
        # ``normalize_timeout_flag`` also canonicalizes ``--timeout=<value>``.
        timeout_idx = cmd.index("--timeout")
        try:
            current_int = int(timeout_value)
        except (ValueError, TypeError):
            current_int = 0
        effective_timeout = ensure_min_duration(timeout_value, scope, target, action)
        if effective_timeout != current_int:
            cmd[timeout_idx + 1] = str(effective_timeout)
            logger.info(
                f"Auto-boosted --timeout from {timeout_value}s to {effective_timeout}s "
                f"for {scope}-{target}-{action} (recommended minimum)"
            )

    try:
        result = await execute_via_transport(cmd, _target, timeout=settings.timeout_blade, task_id=task_id, bypass_channel=_kubewiz, expect_profile=profile_for_tool("blade_create"))
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

        # ``blade`` is a Go binary in the kubewiz path (bypass_channel skips wiz
        # protocol parsing), so a gateway HTML reply surfaces as
        # ``invalid character 'b' after top-level value``. task-46317228 #64:
        # the LLM read exactly that as "blade_create failed" and invented a
        # ``kubectl debug --image=stress-ng`` second injection, even though
        # blade_status already reported Running/Success. Name the real cause.
        #
        # The explanation is appended to what the LLM READS, and deliberately
        # NOT folded into ``combined``: everything below parses/classifies that
        # string, and prose containing words like "timeout" flips
        # ``classify_error`` from END_FAILED to SHORT_RETRY — the annotation
        # would silently change the failure-handling branch.
        _anomaly = explain_transport_anomaly(combined)
        _shown = f"{_anomaly}\n{combined}" if _anomaly else combined

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
            # Classify the error to decide whether polling makes sense.
            # Terminal errors (permission denied, tool not found, etc.) will
            # NEVER self-heal no matter how long the operator retries — tell
            # the agent immediately so it doesn't waste cycles polling.
            from chaos_agent.errors import classify_error, ErrorAction
            classification = classify_error(combined)

            if classification.action != ErrorAction.SHORT_RETRY:
                # Terminal error — no point polling.
                return (
                    f"Error: injection FAILED permanently "
                    f"(exit {result.exit_code}, class={classification.error_class.value}). "
                    f"Experiment CRD was created (UID: {uid_in_error}) but the fault "
                    f"CANNOT take effect — the operator will not recover from this. "
                    f"Matched pattern: {classification.matched_pattern or 'unknown'}. "
                    f"Do NOT poll or wait; either REPLAN with a different approach "
                    f"or report failure. "
                    f"To clean up, call blade_destroy(uid='{uid_in_error}').\n"
                    f"Raw output: {_shown}"
                )

            # Transient infra error (timeout, connection reset, etc.) — the
            # operator may retry with fallback mechanisms. Poll makes sense.
            return (
                f"Warning: blade create returned error (exit {result.exit_code}) "
                f"but experiment CRD was created (UID: {uid_in_error}). "
                f"The error appears TRANSIENT ({classification.matched_pattern}); "
                f"the ChaosBlade operator may retry with fallback mechanisms. "
                f"POLL the cluster state to check if the fault takes effect:\n"
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
                f"Raw output: {_shown}"
            )
        return f"Error: blade create failed (exit {result.exit_code}): {_shown}"

    return result.stdout


@tool
async def blade_destroy(uid: str, kubeconfig: str = "") -> str:
    """Mutating. Destroy a ChaosBlade experiment by UID to recover the fault.

    Runs `blade destroy <UID>`. NOT available in Phase 1 planning — the
    runtime classifies this as a mutation and the phase 1 screener will
    reject it. In Phase 2 it is limited to cleaning an experiment UID emitted
    by this task's own failed/partial blade_create call.

    When to use:
      - Recovery phase.
      - Phase 2 cleanup after blade_create reports that it created a CRD but
        the fault cannot take effect; clean it before trying another method.

    Inputs:
      - uid: experiment UID returned by blade_create (`result.uid`) or blade_status.
      - kubeconfig: optional override (defaults to settings + KUBECONFIG env).

    Output: JSON from blade CLI; failure starts with "Error:".

    Side effects: Removes the CRD; the target should return to normal.

    Constraints:
      - Always re-verify with blade_status — Status should flip to "Destroyed".
        See knowledge resource `failure-modes.md` (recovery failure) for the
        rare case where destroy returns success but the stress process lingers.
      - Phase 2 may only destroy UIDs returned by blade_create in the current
        task. The runtime rejects any other UID.
    """
    _target = TransportTarget.from_state({})
    _channel_name = resolve_channel_name()
    # Unlike blade_create (which keys host-ness off its ``scope`` argument),
    # destroy/status receive NO scope arg — the resolved channel is the only
    # signal, and it is authoritative: preflight (check_transport_config)
    # guarantees a coherent host config, so a host injection's channel resolves
    # to PROFILE_HOST here, matching the scope=host used at create time.
    _is_host_channel = profile_of(_channel_name) == PROFILE_HOST
    if _is_host_channel:
        # Host scope: blade ran ON the remote host and recorded the experiment
        # in that host's LOCAL DB. Destroy must run there too (wiz-wrapped,
        # bypass_channel=False) using the bare remote blade — NO --target k8s,
        # NO kubewiz/kubeconfig args (there is no cluster CRD to reach).
        cmd = [_get_host_blade_path(), "destroy", uid]
        _bypass = False
    else:
        cmd = [_get_blade_path(), "destroy", uid]
        _kubewiz = _channel_name in KUBEWIZ_CHANNELS
        # kubewiz mode: experiments are K8s CRDs, not local DB records.
        # Without --target k8s, blade looks in local DB and returns "record not found".
        if _channel_name == "kubewiz_k8s":
            cmd.extend(["--target", "k8s"])
        # kubewiz mode: blade reaches KubeWiz Core natively via --kubewiz-url, so it
        # runs locally (bypass_channel) and is NOT re-wrapped in `wiz task exec`.
        if _kubewiz:
            cmd.extend(_build_blade_kubewiz_args(_target))
        else:
            cmd.extend(_build_kubeconfig_arg(kubeconfig))
        _bypass = _kubewiz

    try:
        result = await execute_via_transport(cmd, _target, timeout=settings.timeout_blade, bypass_channel=_bypass, expect_profile=profile_for_tool("blade_destroy"))
    except Exception as e:
        return f"Error: blade destroy failed: {e}"

    if result.exit_code != 0:
        stderr = result.stderr.strip() if result.stderr else ""
        stdout = result.stdout.strip() if result.stdout else ""
        return f"Error: blade destroy failed (exit {result.exit_code}): {stderr or stdout}"

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

    Constraints:
      - blade_status v1.8.0 ignores the --kubeconfig CLI flag; this tool passes
        kubeconfig via the KUBECONFIG env var instead. No action required from
        the caller.
    """
    _target = TransportTarget.from_state({})
    _channel_name = resolve_channel_name()
    _is_host_channel = profile_of(_channel_name) == PROFILE_HOST
    if _is_host_channel:
        # Host scope: the experiment lives in the remote host's LOCAL DB.
        # Query it there (wiz-wrapped, bypass_channel=False) with the bare
        # remote blade — NO `blade query k8s` (no cluster CRD), NO KUBECONFIG env.
        cmd = [_get_host_blade_path(), "status"]
        if uid:
            cmd.extend(["--uid", uid])
        try:
            result = await execute_via_transport(cmd, _target, timeout=settings.timeout_blade, bypass_channel=False, expect_profile=profile_for_tool("blade_status"))
        except Exception as e:
            return f"Error: blade status failed: {e}"
        # Surface stderr when the remote status command fails (exit != 0):
        # returning a bare empty stdout would silently hide the error from the
        # verifier. Mirror the kubewiz path's stdout-or-stderr fallback.
        stdout = result.stdout.strip() if result.stdout else ""
        stderr = result.stderr.strip() if result.stderr else ""
        return stdout or stderr or ""

    _kubewiz = _channel_name in KUBEWIZ_CHANNELS
    # kubewiz mode: blade status does NOT support --kubewiz-url flags
    # (only local DB). Delegate to blade query k8s which queries the
    # remote cluster CRD via kubewiz.
    if _channel_name == "kubewiz_k8s" and uid:
        cmd = [_get_blade_path(), "query", "k8s", "create", uid]
        cmd.extend(_build_blade_kubewiz_args(_target))
        try:
            # blade reaches KubeWiz Core natively; run local, don't wiz-wrap.
            result = await execute_via_transport(cmd, _target, timeout=settings.timeout_blade, bypass_channel=True, expect_profile=profile_for_tool("blade_status"))
        except Exception as e:
            return f"Error: blade status (kubewiz) failed: {e}"
        stdout = result.stdout.strip() if result.stdout else ""
        stderr = result.stderr.strip() if result.stderr else ""
        return stdout or stderr or ""

    cmd = [_get_blade_path(), "status"]
    if uid:
        cmd.extend(["--uid", uid])
    # kubeconfig mode: blade status v1.8.0 does NOT support --kubeconfig flag,
    # so pass via KUBECONFIG env var instead
    env_override = _build_kubeconfig_env(kubeconfig)

    try:
        result = await execute_via_transport(cmd, _target, timeout=settings.timeout_blade, env_override=env_override, bypass_channel=_kubewiz, expect_profile=profile_for_tool("blade_status"))
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
        _target = TransportTarget.from_state({})
        # blade -h is local help text; in kubewiz mode blade runs natively, so
        # don't wrap it in `wiz task exec`.
        result = await execute_via_transport(cmd, _target, timeout=10, bypass_channel=is_kubewiz_channel(), expect_profile=profile_for_tool("blade_help"))
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

    Constraints:
      - This tool only handles `blade query k8s`. For host-side queries
        (disk / network interface / jvm) use the kubectl tool to invoke
        them inside a debug pod.
      - Host-scope experiments have NO cluster CRD/selector to query; this
        tool returns guidance pointing to blade_status instead of running.
    """
    _channel_name = resolve_channel_name()
    if profile_of(_channel_name) == PROFILE_HOST:
        # Host scope has no cluster CRD / selector to query — the experiment is
        # a local process on the remote host. `blade query k8s` is meaningless
        # here and would misroute a k8s-semantic command onto a host channel
        # (local blade + kubeconfig/kubewiz args). Steer the caller to
        # blade_status, which IS host-aware and reads the remote host's local
        # experiment DB.
        return (
            "blade_query_k8s does not apply to host-scope experiments (there is "
            "no cluster CRD/selector to query). Use blade_status to read the "
            "remote host's local experiment state instead."
        )
    cmd = [_get_blade_path(), "query", "k8s"]
    if uid:
        cmd.extend(["create", uid])
    _target = TransportTarget.from_state({})
    # kubewiz mode: blade reaches KubeWiz Core natively via --kubewiz-url, so it
    # runs locally (bypass_channel) and is NOT re-wrapped in `wiz task exec`.
    _kubewiz = is_kubewiz_channel()
    if _kubewiz:
        cmd.extend(_build_blade_kubewiz_args(_target))
    else:
        cmd.extend(_build_kubeconfig_arg(kubeconfig))
    # Also pass KUBECONFIG env var as fallback (belt-and-suspenders with --kubeconfig)
    env_override = _build_kubeconfig_env(kubeconfig)

    try:
        result = await execute_via_transport(cmd, _target, timeout=settings.timeout_blade, env_override=env_override, bypass_channel=_kubewiz, expect_profile=profile_for_tool("blade_query_k8s"))
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
