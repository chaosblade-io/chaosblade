"""ChaosBlade CLI tool wrappers for LangGraph @tool functions.

Lives INSIDE the ChaosBlade provider package (phase-11 carrier-import-
boundary): the generic layer reaches these wrappers only through the
provider's ``tools(phase)`` surface, never via ``chaos_agent.tools``.

Tool signatures faithfully map ChaosBlade K8s scenario parameters so the LLM
can naturally pass --namespace, --names, --labels, --kubeconfig etc. when needed.
Scene-specific flags (e.g. --time, --cpu-count) remain in the generic `flags` param.
"""

import logging
import shlex
from typing import Annotated, Literal

from langchain_core.tools import InjectedToolArg, tool
from langgraph.prebuilt import InjectedState

from chaos_agent.agent.spec.fault_registry import is_host_scope
from chaos_agent.config.settings import settings
from chaos_agent.tools._tool_profiles import profile_for_tool
from chaos_agent.tools.markers import UNCERTAIN_OUTCOME_MARKER  # re-exported
from chaos_agent.transports import (
    KUBEWIZ_CHANNELS,
    PROFILE_HOST,
    PROFILE_UNKNOWN,
    TransportRegistry,
    TransportTarget,
    execute_via_transport,
    profile_of,
    resolve_transport_target,
)
from chaos_agent.transports.protocol import explain_transport_anomaly

logger = logging.getLogger(__name__)

# Machine-readable marker embedded in blade_create returns whose outcome is
# UNKNOWN (transport exception / no-UID transient error). Defined in
# chaos_agent/tools/markers.py (neutral ground — the execute-loop scan
# consumes it without crossing the carrier-import boundary); re-exported
# above for historical import paths. Consumed by the execute_loop
# create-reconcile gate's three-state scan; the guidance text below is
# for the LLM. Wording is deliberately distinct from the gate's own
# interception marker ("[gate:reconcile-blocked]", also defined in
# chaos_agent/tools/markers.py): uncertain = real execution with
# unknown result, gate-blocked = interception that never executed.

# Guidance appended to uncertain returns: assert neither success nor
# failure, point at reconciliation first (mirrors the uid_in_error POLL
# precedent's "Do NOT try alternative" discipline).
_UNCERTAIN_GUIDANCE = (
    "Outcome UNKNOWN — the create request may have reached the cluster "
    "even though its response did not reach us. Result-uncertain is NOT "
    "failure: a blind retry can create a DUPLICATE experiment on the same "
    "target. Reconcile FIRST: check whether an experiment matching this "
    "request (namespace/labels/names/target-action) is already active — "
    "reuse its UID if it is; retry only after confirming it is absent."
)


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
    """Resolve the LOCAL blade binary path: explicit setting > bundled > PATH.

    Low-level primitive. The single ``profile → path`` authority is
    :func:`blade_bin_for` below, which delegates here for the local/k8s profile.
    ``blade_create`` also calls this directly for its scope-driven path (see the
    NOTE at its command-shape branch).
    """
    if settings.blade_path:
        return settings.blade_path
    return settings._resolve_blade_path()


def _get_host_blade_path() -> str:
    """Resolve the blade binary name for the HOST profile.

    Host-scope injections run blade ON the remote host (wrapped in
    ``wiz task exec``), so the path must be valid THERE, not locally. Never
    reuse ``_get_blade_path()`` / ``settings.blade_path`` here: those resolve a
    *local* absolute path (bundled / vendored), which is meaningless remotely
    and would leak a local path into the remote command (that leak is B1).
    Default to the bare ``blade`` on the remote PATH; override via
    ``settings.host_blade_path`` only when the remote binary lives outside PATH.

    Low-level primitive. The "never leak a local path into a host command"
    invariant is now guaranteed at a single point by :func:`blade_bin_for`,
    which delegates here for the host profile — so the four channel-driven tools
    can no longer forget the host branch. ``blade_create`` still calls this
    directly for its scope-driven path.
    """
    return settings.host_blade_path or "blade"


def blade_bin_for(target: TransportTarget) -> str:
    """Single ``profile → blade binary path`` authority for a resolved target.

    D3/B1: this mapping used to be duplicated as an inline
    ``profile_of(...) == HOST`` branch in each blade tool, and ``blade_help``
    simply forgot the host branch — baking a LOCAL vendor absolute path into a
    command meant to run ON a remote host (``/Users/…/vendor/chaosblade/blade:
    No such file or directory``). Collapsing the mapping here makes "forgot the
    host branch" structurally impossible: every channel-driven tool that reads
    ``blade_bin_for(target)`` gets the profile-correct path for free.

    - host profile → the REMOTE bare blade (:func:`_get_host_blade_path`), a
      path valid on the target host, never a local absolute path.
    - local / k8s   → the local vendor absolute path (:func:`_get_blade_path`).

    Layering: this resolver lives in the chaosblade PROVIDER package, not on
    ``TransportTarget`` in the transports layer, on purpose — the blade binary
    path is a chaosblade-carrier concern, and the phase-11 carrier-import
    boundary forbids the generic transports layer from importing a concrete
    provider subpackage (``test_provider_imports_confined_to_allowlist``). The
    provider may import transports (the sanctioned direction), so the resolver
    reads the target's profile from ``TransportRegistry`` and delegates to the
    two in-module primitives — which keeps the ``conftest`` stub of
    ``_get_blade_path`` honored and the local domain byte-identical.

    ``blade_create`` deliberately does NOT use this resolver: it keys both its
    command SHAPE and its path off the fault ``scope`` argument
    (``is_host_scope(scope)``), which ``fault_registry`` keeps distinct from a
    transport-channel ``profile == "host"`` test on purpose — a host-scope fault
    uses the remote blade even before a host channel is configured (pinned by
    ``test_host_create_uses_host_blade_path``). The four channel-driven tools
    (destroy/status/query_k8s/help) take no scope argument, so the resolved
    channel profile is their only — and authoritative — signal.
    """
    try:
        profile = getattr(TransportRegistry.resolve(target), "profile", PROFILE_UNKNOWN)
    except ValueError:
        # Under-specified scope / unknown channel_override: fail soft to the
        # local path, matching resolve_channel_name's PROFILE_UNKNOWN (not host)
        # branch — never resolve to a bare remote blade unless the channel is
        # affirmatively host.
        profile = PROFILE_UNKNOWN
    if profile == PROFILE_HOST:
        return _get_host_blade_path()
    return _get_blade_path()


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
    kubeconfig: Annotated[str, InjectedToolArg] = "",
    evict_count: str = "",
    evict_percent: str = "",
    flags: str = "",
    task_id: str = "",
    *,
    state: Annotated[dict | None, InjectedState] = None,
) -> str:
    """Phase 2 ONLY — mutating: create a ChaosBlade K8s/host fault
    experiment (real chaos on the target). NOT Phase 1 (inspect via
    kubectl_read/blade_status). Builds `blade create k8s <scope>-<target>
    <action> [flags]` (host: `blade create <target> <action> [flags]`).

    Inputs:
      - scope: pod|container|node|host. target: cpu|memory|network|disk|
        process|pod. action: fullload|drop|dns|occupy|fill|burn|kill|delete.
      - namespace/names/labels/evict_*: passthrough.
      - flags: scenario flags (catalog: `chaosblade-cli.md`); drop →
        "--interface eth0" (drops ALL packets; no --percent).

    Output: blade CLI JSON; success carries `result.uid` (for
    blade_destroy/blade_status); failure starts "Error:".

    Side effects: creates a CRD; injects the real fault.

    Constraints:
      - scope="pod" = whole Pod (never --container-names);
        scope="container" REQUIRES --container-ids/--container-names.
      - scope="node": --namespace/--labels rejected, auto-omitted; select
        via --names. scope="host": OS-executor; no namespace/labels;
        params in `flags`.
      - Memory: pod: --mem-percent|--mem-size; node: --mem-percent ONLY.
      - "unknown flag: --namespace" (host blade) = version issue — retry
        without it.
    """
    # Universal first-use trigger: pip-install users get a pure-Python wheel
    # with no blade binary. Ensure it exists before the first mutating
    # injection — off the event loop, best-effort. If the download fails
    # (offline), the host blade path below fails and callers (execute_loop)
    # fall back to kubectl exec into a cluster tool pod. This is the ONE
    # chokepoint every injection path funnels through (CLI, TUI, server
    # API), so it's the single place that needs the trigger.
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
    # NOTE: blade_create is the ONE blade tool that selects its binary path off
    # the fault ``scope`` argument (``_is_host = is_host_scope(scope)``) rather
    # than ``blade_bin_for(_target)`` (which resolves the transport-CHANNEL profile).
    # This is deliberate, not an oversight: the two axes are kept distinct (see
    # ``fault_registry.is_host_scope``) because a host-scope fault runs blade on
    # the remote host — needing the bare remote path — even before a host channel
    # is configured, and the command SHAPE below is scope-driven too. Pinned by
    # ``test_host_create_uses_host_blade_path``. The four channel-driven tools
    # (destroy/status/query_k8s/help) have no scope arg and read blade_bin.
    if _is_host:
        cmd = [_get_host_blade_path(), "create", target, action]
    else:
        cmd = [_get_blade_path(), "create", "k8s", f"{scope}-{target}", action]

    _target = resolve_transport_target(state)
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
        # Channel selection is fail-soft: an unresolvable channel degrades to
        # kubeconfig. Config validity is guaranteed upstream (settings
        # kube_connection_mode validator + preflight check_transport_config), so
        # an unresolvable channel never reaches this injection path in practice.
        # kubewiz mode: blade reaches KubeWiz Core via its own --kubewiz-url flags,
        # so it runs locally and must NOT be re-wrapped in `wiz task exec`
        # (bypass_channel=True below).
        # D3: derive kubewiz-ness from the SAME state-resolved ``_target`` this
        # command executes over (execute_via_transport below), not the
        # settings-only ``is_kubewiz_channel()``. A per-session state channel
        # override would otherwise build a --kubeconfig-shaped command yet run
        # it over the kubewiz channel. No-op when state carries no override.
        # Mirrors blade_status / blade_help.
        try:
            _kubewiz = TransportRegistry.resolve(_target).name in KUBEWIZ_CHANNELS
        except ValueError:
            _kubewiz = False
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

    # Auto-inject --timeout if not present; explicit values pass through verbatim
    # This is the BOTTOM layer of the three-layer duration guarantee,
    # ensuring ALL injection paths (every blade_create call) are covered.
    from chaos_agent.utils.fault_type import ensure_min_duration, normalize_timeout_flag

    timeout_value = normalize_timeout_flag(cmd)
    if timeout_value is None:
        # No timeout specified: auto-inject recommended minimum
        effective_timeout = ensure_min_duration(None, scope, target, action)
        cmd.extend(["--timeout", str(effective_timeout)])
        logger.info(f"Auto-injected --timeout {effective_timeout}s into blade create command")
    else:
        # Timeout specified (by LLM or CLI): passes through verbatim;
        # zero/malformed values normalize to the default.
        # ``normalize_timeout_flag`` also canonicalizes ``--timeout=<value>``.
        #
        # ``warn_explicit_below_floor=False``: a present ``--timeout`` here is
        # the engine-pinned RECOVERY TIMER (D+G, layer 3), not the observation
        # window D the floor is empirically derived from. The layer that owns D
        # (TOP inject.py / MIDDLE extract_planning_metadata / l4 adapter) has
        # already surfaced the requested-vs-recommended gap on the correct
        # number; re-warning here fires a second time on the larger D+G value
        # for the same fact (see fault_type.ensure_min_duration).
        timeout_idx = cmd.index("--timeout")
        try:
            current_int = int(timeout_value)
        except (ValueError, TypeError):
            current_int = 0
        effective_timeout = ensure_min_duration(
            timeout_value, scope, target, action,
            warn_explicit_below_floor=False,
        )
        if effective_timeout != current_int:
            cmd[timeout_idx + 1] = str(effective_timeout)
            logger.info(
                f"Normalized --timeout from {timeout_value}s to {effective_timeout}s "
                f"for {scope}-{target}-{action}"
            )

    try:
        result = await execute_via_transport(cmd, _target, timeout=settings.timeout_blade, task_id=task_id, bypass_channel=_kubewiz, expect_profile=profile_for_tool("blade_create"))
    except Exception as e:
        # Transport-level failure (timeout, connection loss): the request
        # MAY have been accepted — the outcome is UNKNOWN, not failed.
        # Embed the machine-readable marker for the create-reconcile gate
        # (execute_loop three-state scan) + reconciliation guidance. A
        # client-side distinction between "never sent" and "sent, response
        # lost" is not reliably possible, so every transport exception is
        # uncertain (a mis-block costs one read-only reconcile call; a
        # missed one can duplicate a fault experiment).
        return (
            f"Error: blade create transport failure: {e}\n"
            f"{UNCERTAIN_OUTCOME_MARKER} {_UNCERTAIN_GUIDANCE}"
        )

    if result.exit_code != 0:
        # Combine both streams: JSON (including 54000) may land on stdout
        # while error details go to stderr.  Include both so callers can
        # parse the experiment_uid from either stream.
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
        #
        # Round-16 single source: this shape (the blade CLI's raw failure JSON
        # top-level ``"uid": "<hex16>"`` key) is ALSO the exec channel's
        # failed-create dialect — verify.py's RAW_FAILED_CREATE_UID_RE. The
        # host face re-wraps the UID into the ``UID: ...`` wording below; the
        # exec face sees the raw key. One anchor, two faces.
        from .verify import RAW_FAILED_CREATE_UID_RE

        uid_match = RAW_FAILED_CREATE_UID_RE.search(combined)
        uid_in_error = uid_match.group(1) if uid_match else None
        if uid_in_error:
            # NOTE: no uncertain marker on this path — the UID is known, so
            # the outcome is NOT uncertain; the POLL/REPLAN guidance below
            # already IS the reconciliation path (create-reconcile gate
            # must not add a second interception here).
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
        # No UID in the output. Transient classes (connection reset,
        # channel timeout): whether the CRD was created is UNKNOWN — mark
        # uncertain for the create-reconcile gate. Terminal classes
        # (parameter/permission rejections): the command was never
        # admitted, retrying cannot duplicate anything — keep the plain
        # deterministic-failure return (no marker, no gate interception).
        # Classify on `combined` (raw output), NOT `_shown`: the anomaly
        # annotation may contain words like "timeout" that would flip
        # END_FAILED to SHORT_RETRY — same guard as the uid_in_error path.
        from chaos_agent.errors import classify_error, ErrorAction
        no_uid_class = classify_error(combined)
        if no_uid_class.action == ErrorAction.SHORT_RETRY:
            return (
                f"Error: blade create failed (exit {result.exit_code}): {_shown}\n"
                f"{UNCERTAIN_OUTCOME_MARKER} {_UNCERTAIN_GUIDANCE}"
            )
        return f"Error: blade create failed (exit {result.exit_code}): {_shown}"

    return result.stdout


@tool
async def blade_destroy(
    uid: str,
    kubeconfig: Annotated[str, InjectedToolArg] = "",
    *,
    state: Annotated[dict | None, InjectedState] = None,
) -> str:
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

    Output: JSON from blade CLI; failure starts with "Error:".

    Side effects: Removes the CRD; the target should return to normal.

    Constraints:
      - Always re-verify with blade_status — Status should flip to "Destroyed".
        See knowledge resource `failure-modes.md` (recovery failure) for the
        rare case where destroy returns success but the stress process lingers.
      - Phase 2 may only destroy UIDs returned by blade_create in the current
        task. The runtime rejects any other UID.
    """
    _target = resolve_transport_target(state)
    # D3: derive the channel from the SAME state-resolved ``_target`` that
    # ``blade_bin_for(_target)`` uses below, not from the settings-only
    # ``resolve_channel_name()``. A per-session state channel override would
    # otherwise make the destroy command SHAPE (host/kubewiz branch, --target
    # k8s) diverge from the channel it actually executes over. No-op when state
    # carries no override. Mirrors blade_status / blade_help.
    try:
        _channel_name = TransportRegistry.resolve(_target).name
    except ValueError:
        _channel_name = PROFILE_UNKNOWN
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
        # ``blade_bin_for(_target)`` is the single authority for the profile→path
        # mapping (host profile → bare remote blade); the branch here only
        # controls command SHAPE / bypass / args, never the binary path.
        cmd = [blade_bin_for(_target), "destroy", uid]
        _bypass = False
    else:
        cmd = [blade_bin_for(_target), "destroy", uid]
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
        # Merge when both carry content — `or` drops one side's evidence.
        _detail = "\n".join(p for p in (stdout, stderr) if p) or "(no output)"
        return f"Error: blade destroy failed (exit {result.exit_code}): {_detail}"

    return result.stdout


@tool
async def blade_status(
    uid: str = "",
    kubeconfig: Annotated[str, InjectedToolArg] = "",
    *,
    state: Annotated[dict | None, InjectedState] = None,
) -> str:
    """Phase 1 / Phase 2 read-only. Query a ChaosBlade experiment's CLI-side status.

    Runs `blade status --uid <UID>`, or `blade status --type create` when uid
    is empty. Read-only — listing experiments does not mutate cluster state.

    When to use:
      - Verifier Layer 1: confirm the experiment is "Success" after blade_create.
      - Recovery verification: confirm the experiment is "Destroyed" after blade_destroy.
      - Use `blade_query_k8s` instead when you need cluster-side state (which
        pods/nodes are actually affected).

    Inputs:
      - uid: experiment UID. Empty → lists all `create` experiments (a bare
        `blade status` errors 45000 "less parameter: type|uid", so `--type
        create` is supplied). Output may be large.

    Output: JSON with Uid / Command / Status / Error / CreateTime / UpdateTime.
            Status ∈ {Created, Success, Error, Destroyed}.

    Side effects: None (read-only).

    Constraints:
      - blade_status v1.8.0 ignores the --kubeconfig CLI flag; this tool passes
        kubeconfig via the KUBECONFIG env var instead. No action required from
        the caller.
    """
    _target = resolve_transport_target(state)
    # D3: derive the channel from the SAME resolved ``_target`` that
    # ``blade_bin_for(_target)`` uses below. ``resolve_channel_name()``
    # re-derives from settings only, ignoring any per-session state override —
    # so when state carries an explicit channel the host/kubewiz branch here
    # could pick a different channel than the binary path (branch says k8s
    # while blade_bin_for returns the host blade, or vice versa). When state
    # carries no override the two are identical, so this is a no-op for the
    # common path.
    try:
        _channel_name = TransportRegistry.resolve(_target).name
    except ValueError:
        _channel_name = PROFILE_UNKNOWN
    _is_host_channel = profile_of(_channel_name) == PROFILE_HOST
    if _is_host_channel:
        # Host scope: the experiment lives in the remote host's LOCAL DB.
        # Query it there (wiz-wrapped, bypass_channel=False) with the bare
        # remote blade — NO `blade query k8s` (no cluster CRD), NO KUBECONFIG env.
        cmd = [blade_bin_for(_target), "status"]
        if uid:
            cmd.extend(["--uid", uid])
        else:
            # Empty uid → LIST create experiments. A bare `blade status`
            # (no --uid, no --type) errors with code 45000 "less parameter:
            # type|uid", so fulfil the docstring's "lists all experiments"
            # promise by supplying --type create (D1 contract-honesty fix).
            cmd.extend(["--type", "create"])
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
        cmd = [blade_bin_for(_target), "query", "k8s", "create", uid]
        cmd.extend(_build_blade_kubewiz_args(_target))
        try:
            # blade reaches KubeWiz Core natively; run local, don't wiz-wrap.
            result = await execute_via_transport(cmd, _target, timeout=settings.timeout_blade, bypass_channel=True, expect_profile=profile_for_tool("blade_status"))
        except Exception as e:
            return f"Error: blade status (kubewiz) failed: {e}"
        stdout = result.stdout.strip() if result.stdout else ""
        stderr = result.stderr.strip() if result.stderr else ""
        return stdout or stderr or ""

    cmd = [blade_bin_for(_target), "status"]
    if uid:
        cmd.extend(["--uid", uid])
    else:
        # Empty uid → LIST create experiments (D1 contract-honesty fix): a
        # bare `blade status` errors 45000 "less parameter: type|uid", so
        # --type create makes the docstring's "lists all" promise real.
        cmd.extend(["--type", "create"])
    # kubeconfig mode: blade status v1.8.0 does NOT support --kubeconfig flag,
    # so pass via KUBECONFIG env var instead
    env_override = _build_kubeconfig_env(kubeconfig)

    try:
        result = await execute_via_transport(cmd, _target, timeout=settings.timeout_blade, env_override=env_override, bypass_channel=_kubewiz, expect_profile=profile_for_tool("blade_status"))
    except Exception as e:
        return f"Error: blade status failed: {e}"

    return result.stdout


@tool
async def blade_help(
    subcommand: str = "",
    *,
    state: Annotated[dict | None, InjectedState] = None,
) -> str:
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

    try:
        _target = resolve_transport_target(state)
        # B1 fix: the binary path comes from ``blade_bin_for(_target)`` (the single
        # profile→path authority), so a host-profile session gets the bare
        # remote blade instead of leaking a local vendor absolute path — the
        # host branch this tool used to forget.
        cmd = [blade_bin_for(_target)] + tokens + ["-h"]
        # blade -h is local help text; in kubewiz mode blade runs natively, so
        # don't wrap it in `wiz task exec`. D3: decide bypass from the SAME
        # resolved ``_target`` that blade_bin_for used, not from the
        # settings-only ``is_kubewiz_channel()`` — otherwise a per-session
        # state override could make the binary path and the wrap decision
        # diverge. No-op when state carries no override.
        try:
            _bypass = TransportRegistry.resolve(_target).name in KUBEWIZ_CHANNELS
        except ValueError:
            _bypass = False
        result = await execute_via_transport(cmd, _target, timeout=10, bypass_channel=_bypass, expect_profile=profile_for_tool("blade_help"))
    except Exception as e:
        return f"Error: blade help failed: {e}"

    output = result.stdout.strip()
    if not output and result.stderr:
        output = result.stderr.strip()
    return output or "(no help output)"


@tool
async def blade_query_k8s(
    uid: str = "",
    kubeconfig: Annotated[str, InjectedToolArg] = "",
    *,
    state: Annotated[dict | None, InjectedState] = None,
) -> str:
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
    _target = resolve_transport_target(state)
    # D3: derive the channel from the SAME state-resolved ``_target`` used for
    # execution below, not the settings-only ``resolve_channel_name()`` /
    # ``is_kubewiz_channel()``. A per-session state channel override would
    # otherwise make the host gate AND the kubewiz/kubeconfig command SHAPE
    # diverge from the channel the query actually executes over. No-op when
    # state carries no override. Mirrors blade_status / blade_help.
    try:
        _channel_name = TransportRegistry.resolve(_target).name
    except ValueError:
        _channel_name = PROFILE_UNKNOWN
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
    # Past the host gate above, the channel is non-host, so ``blade_bin_for(_target)``
    # resolves to the local blade — routed through the single profile→path
    # authority rather than a direct ``_get_blade_path()`` call.
    cmd = [blade_bin_for(_target), "query", "k8s"]
    if uid:
        cmd.extend(["create", uid])
    # kubewiz mode: blade reaches KubeWiz Core natively via --kubewiz-url, so it
    # runs locally (bypass_channel) and is NOT re-wrapped in `wiz task exec`.
    # D3: reuse the state-resolved ``_channel_name`` from the host gate above so
    # the command SHAPE matches the channel ``_target`` executes over.
    _kubewiz = _channel_name in KUBEWIZ_CHANNELS
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
