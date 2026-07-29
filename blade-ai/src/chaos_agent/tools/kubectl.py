"""kubectl CLI tool wrapper for LangGraph @tool function.

Unified kubectl tool that supports all subcommands via a single entry point.
Tool signature faithfully maps kubectl global flags so the LLM can naturally
pass --kubeconfig, --context, --cluster etc. when needed.

Two flavours bound at the graph layer:
  - ``kubectl`` (this module) — full surface (exec, delete, patch, ...);
    used in phase 2 / recover where mutation is expected. Being the superset,
    it also covers every read a read-only phase would do.
  - ``kubectl_read`` (this module) — read-only tool (get/describe/top/logs/... +
    read-only ``exec``/``debug``); the single observation tool for every
    read-only phase (intent / planning / verification). Constrains the
    subcommand at the signature level and gates ``exec``/``debug`` inner
    commands to read-only probes so no mutation slips through.
"""

import asyncio
import json
import logging
import os
import re
import shlex
from typing import Literal

from langchain_core.tools import tool

from chaos_agent.config.settings import settings
from chaos_agent.errors import ToolGuardError, ToolTimeoutError
from chaos_agent.tools._tool_profiles import profile_for_tool
from chaos_agent.tools.guard import CommandResult
from chaos_agent.transports import (
    PROFILE_K8S,
    TransportTarget,
    TransportRegistry,
    display_via_transport,
    execute_via_transport,
)

logger = logging.getLogger(__name__)

_K8S_NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _split_args(args: str) -> list[str]:
    """Split args string respecting shell quoting.

    Uses shlex.split to properly handle quoted arguments like
    jsonpath='{.spec.replicas}' or -p '{"key":"value"}'.
    Falls back to str.split() if shlex encounters unmatched quotes
    (e.g. LLM-generated malformed args).
    """
    if not args:
        return []
    try:
        return shlex.split(args)
    except ValueError:
        return args.split()


def _namespace_from_args(args: list[str]) -> str:
    """Return an explicit kubectl namespace flag, if present."""
    for index, token in enumerate(args):
        if token == "--":
            break
        if token in ("-n", "--namespace") and index + 1 < len(args):
            return args[index + 1]
        if token.startswith("--namespace="):
            return token.split("=", 1)[1]
    return ""


def _parse_debug_pod_name(output: str) -> str:
    """Extract the pod created by ``kubectl debug`` across kubectl versions."""
    if not output:
        return ""
    for pattern in (
        r"Creating debugging pod\s+(\S+)",
        r"pod/(\S+)\s+created",
        r"Starting debugging pod\s+(\S+)",
    ):
        match = re.search(pattern, output)
        if match:
            return match.group(1).rstrip(".,;:")
    return ""


def _debug_target_pod_name(processed_args: list[str]) -> str:
    """The target Pod name for a Pod-scoped ``kubectl debug`` (ephemeral
    container) call, or ``""`` for a node-scoped debug.

    ``kubectl debug <pod> --image=... --target=<c> -- <cmd>`` attaches an
    EPHEMERAL CONTAINER to an EXISTING pod. The first positional token is that
    pod. A node debug (``kubectl debug node/<node> ...``) is NOT pod-scoped and
    returns ``""``. Distinguishing the two is a SAFETY boundary: the target pod
    is the user's workload and must NEVER be deleted during cleanup, unlike a
    node-debugger pod which the tool creates and owns.

    Value-consuming flags in SPACE form (``--image busybox``, ``-n ns``) must
    have their VALUE skipped — otherwise the value is mistaken for the pod name
    (e.g. ``--image busybox p0`` would return ``busybox``, or ``-n ns p0`` would
    return ``ns``). ``--flag=value`` form is a single ``-``-prefixed token and
    is already skipped as a flag.
    """
    # debug flags that take a SEPARATE value token (space form).
    value_flags = {
        "-n", "--namespace", "--image", "--target", "-c", "--container",
        "--profile", "--image-pull-policy", "--env", "--custom",
    }
    i = 0
    while i < len(processed_args):
        tok = processed_args[i]
        if tok == "--":
            break
        if tok in value_flags:
            i += 2  # skip flag AND its value
            continue
        if tok.startswith("-"):
            i += 1  # ``--flag=value`` or a valueless flag
            continue
        # First positional. ``node/<name>`` (or ``node <name>``) is node-scope.
        if tok == "node" or tok.startswith("node/"):
            return ""
        return tok
    return ""


def _parse_ephemeral_container_name(pod_json: str) -> str:
    """Newest ephemeral container name from a target pod's status JSON.

    Pod-scoped ``kubectl debug`` prints no container name on stdout — kubectl
    generates a random ``debugger-xxxxx`` and records it only in
    ``.status.ephemeralContainerStatuses``. The LAST entry is the one this call
    just created (kubectl appends).
    """
    try:
        data = json.loads(pod_json)
    except (TypeError, json.JSONDecodeError):
        return ""
    statuses = (data.get("status") or {}).get("ephemeralContainerStatuses") or []
    names = [s.get("name", "") for s in statuses if isinstance(s, dict) and s.get("name")]
    return names[-1] if names else ""


def _extract_debug_profile(v_args: str) -> str:
    """The ``--profile`` value from a debug ``v_args`` (``""`` if absent).

    Carried into debug-pod-meta so carrier resolution and diagnostics see the
    profile the Agent requested.
    """
    try:
        tokens = shlex.split(v_args)
    except ValueError:
        return ""
    for i, tok in enumerate(tokens):
        if tok == "--profile" and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith("--profile="):
            return tok.split("=", 1)[1]
    return ""




async def _resolve_effective_namespace(
    kubeconfig: str,
    context: str,
    cluster: str,
) -> str:
    """Resolve the namespace selected by the active transport context.

    This works for both direct kubectl and kubewiz because it asks the same
    transport that will create the debug pod.  Falling back to ``default`` is
    Kubernetes-compatible, but only after the live context produced no value.
    """
    cmd = build_kubectl_cmd(
        "config",
        ["view", "--minify", "-o", "jsonpath={..namespace}"],
        kubeconfig,
        context,
        cluster,
    )
    try:
        target = TransportTarget.from_state({})
        result = await execute_via_transport(
            cmd, target, timeout=settings.timeout_kubectl, expect_profile=PROFILE_K8S,
        )
    except Exception:
        logger.debug("Failed to resolve active kubectl namespace", exc_info=True)
        return "default"
    namespace = result.stdout.strip().strip("'\"") if result.exit_code == 0 else ""
    if namespace and _K8S_NAMESPACE_RE.fullmatch(namespace):
        return namespace
    return "default"


async def _debug_pod_metadata(
    pod_name: str,
    namespace: str,
    kubeconfig: str,
    context: str,
    cluster: str,
) -> tuple[dict, str]:
    """Read the authoritative identity and status of a created debug pod."""
    cmd = build_kubectl_cmd(
        "get", ["pod", pod_name, "-n", namespace, "-o", "json"],
        kubeconfig, context, cluster,
    )
    try:
        target = TransportTarget.from_state({})
        result = await execute_via_transport(
            cmd, target, timeout=settings.timeout_kubectl, expect_profile=PROFILE_K8S,
        )
    except Exception as exc:
        return {}, str(exc)
    if result.exit_code != 0:
        return {}, result.stderr or result.stdout
    try:
        data = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        return {}, f"invalid pod JSON: {exc}"

    metadata = data.get("metadata") or {}
    spec = data.get("spec") or {}
    status = data.get("status") or {}
    containers = spec.get("containers") or []
    container_statuses = status.get("containerStatuses") or []
    waiting_reasons = []
    for container_status in container_statuses:
        waiting = ((container_status.get("state") or {}).get("waiting") or {})
        if waiting.get("reason"):
            waiting_reasons.append(waiting["reason"])
    return {
        "name": metadata.get("name") or pod_name,
        "namespace": metadata.get("namespace") or namespace,
        "uid": metadata.get("uid") or "",
        "node": spec.get("nodeName") or "",
        "privileged": any(
            (container.get("securityContext") or {}).get("privileged") is True
            for container in containers
            if isinstance(container, dict)
        ),
        "phase": status.get("phase") or "Unknown",
        "ready": bool(container_statuses) and all(
            container_status.get("ready") is True
            for container_status in container_statuses
        ),
        "waiting_reasons": waiting_reasons,
    }, ""


async def _wait_for_created_debug_pod(
    pod_name: str,
    namespace: str,
    kubeconfig: str,
    context: str,
    cluster: str,
) -> tuple[bool, dict, str]:
    """Wait until a created debug pod is executable and return its identity."""
    wait_seconds = min(60, max(1, int(settings.timeout_kubectl_exec)))
    wait_cmd = build_kubectl_cmd(
        "wait",
        [
            "--for=condition=Ready",
            f"pod/{pod_name}",
            "-n", namespace,
            f"--timeout={wait_seconds}s",
        ],
        kubeconfig,
        context,
        cluster,
    )
    try:
        target = TransportTarget.from_state({})
        wait_result = await execute_via_transport(
            wait_cmd, target, timeout=wait_seconds + 10, expect_profile=PROFILE_K8S,
        )
        wait_error = "" if wait_result.exit_code == 0 else (
            wait_result.stderr or wait_result.stdout
        )
    except Exception as exc:
        wait_result = None
        wait_error = str(exc)

    metadata, metadata_error = await _debug_pod_metadata(
        pod_name, namespace, kubeconfig, context, cluster,
    )
    if metadata_error:
        return False, {}, metadata_error
    if not metadata.get("uid") or not metadata.get("node"):
        return False, metadata, "debug pod identity is incomplete"
    if (
        wait_result is not None
        and wait_result.exit_code == 0
        and metadata.get("phase") == "Running"
        and metadata.get("ready") is True
    ):
        return True, metadata, ""
    reasons = ", ".join(metadata.get("waiting_reasons") or [])
    detail = reasons or metadata.get("phase") or wait_error or "not ready"
    return False, metadata, f"debug pod did not become Ready: {detail}"


async def _wait_for_ephemeral_container(
    target_pod: str,
    namespace: str,
    kubeconfig: str,
    context: str,
    cluster: str,
) -> tuple[bool, str, dict, str]:
    """Resolve + await the ephemeral container a Pod-scoped debug just created.

    Returns ``(running, container_name, target_pod_metadata, error)``.

    Unlike a node-debugger POD (which has its own Ready condition), an ephemeral
    container has no Ready gate — it is executable once its ``state`` is
    ``running``. We poll the TARGET pod's ``ephemeralContainerStatuses`` for the
    newest entry and check that state. The pod identity returned is the TARGET
    pod's (uid/node/namespace) so carrier resolution can pin the host, but the
    carrier's executable handle is the container NAME, not a separate pod.
    """
    wait_seconds = min(60, max(1, int(settings.timeout_kubectl_exec)))
    deadline = asyncio.get_running_loop().time() + wait_seconds
    last_error = ""
    container_name = ""
    while True:
        cmd = build_kubectl_cmd(
            "get", ["pod", target_pod, "-n", namespace, "-o", "json"],
            kubeconfig, context, cluster,
        )
        try:
            target = TransportTarget.from_state({})
            result = await execute_via_transport(
                cmd, target, timeout=settings.timeout_kubectl,
                expect_profile=PROFILE_K8S,
            )
        except Exception as exc:
            last_error = str(exc)
            result = None
        if result is not None and result.exit_code == 0:
            container_name = _parse_ephemeral_container_name(result.stdout)
            if container_name:
                try:
                    data = json.loads(result.stdout)
                except (TypeError, json.JSONDecodeError):
                    data = {}
                meta = data.get("metadata") or {}
                spec = data.get("spec") or {}
                # privileged from the ephemeral container's own securityContext
                # (a --profile=netadmin container adds NET_ADMIN via capabilities,
                # NOT privileged=true; report the true spec value and let carrier
                # resolution decide what it accepts).
                _priv = False
                for ec in spec.get("ephemeralContainers") or []:
                    if isinstance(ec, dict) and ec.get("name") == container_name:
                        _sc = ec.get("securityContext") or {}
                        _priv = _sc.get("privileged") is True
                        break
                for st in (data.get("status") or {}).get(
                    "ephemeralContainerStatuses") or []:
                    if st.get("name") != container_name:
                        continue
                    state = st.get("state") or {}
                    tgt_meta = {
                        "name": meta.get("name") or target_pod,
                        "namespace": meta.get("namespace") or namespace,
                        "uid": meta.get("uid") or "",
                        "node": spec.get("nodeName") or "",
                        "privileged": _priv,
                        "phase": (data.get("status") or {}).get("phase") or "Unknown",
                    }
                    if "running" in state:
                        return True, container_name, tgt_meta, ""
                    waiting = (state.get("waiting") or {}).get("reason", "")
                    terminated = (state.get("terminated") or {}).get("reason", "")
                    last_error = waiting or terminated or "ephemeral container not running"
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(2)
    return False, container_name, {}, (last_error or "ephemeral container did not start")


async def _delete_created_debug_pod(
    pod_name: str,
    namespace: str,
    kubeconfig: str,
    context: str,
    cluster: str,
) -> bool:
    """Best-effort removal for a debug pod that never became executable."""
    cmd = build_kubectl_cmd(
        "delete",
        ["pod", pod_name, "-n", namespace, "--ignore-not-found"],
        kubeconfig,
        context,
        cluster,
    )
    try:
        target = TransportTarget.from_state({})
        result = await execute_via_transport(
            cmd, target, timeout=settings.timeout_kubectl, expect_profile=PROFILE_K8S,
        )
        return result.exit_code == 0
    except Exception:
        logger.warning(
            "Failed to clean unready debug pod %s/%s",
            namespace,
            pod_name,
            exc_info=True,
        )
        return False


def _build_kubectl_global_args(
    kubeconfig: str = "",
    context: str = "",
    cluster: str = "",
) -> list[str]:
    """Build kubectl global flags list.

    Priority: explicit parameter > settings (includes KUBECONFIG env via AliasChoices).
    Only non-empty values are included.
    """
    args: list[str] = []

    # --kubeconfig: tool param > settings fallback
    kc = kubeconfig or settings.kubeconfig_path
    if kc:
        kc = os.path.expanduser(kc)
        args.extend(["--kubeconfig", kc])

    # --context: tool param > settings fallback
    ctx = context or settings.kube_context
    if ctx:
        args.extend(["--context", ctx])

    # --cluster
    if cluster:
        args.extend(["--cluster", cluster])

    return args


def build_kubectl_cmd(
    subcommand: str,
    v_args: "list[str] | str" = "",
    kubeconfig: str = "",
    context: str = "",
    cluster: str = "",
    settings=None,
) -> list[str]:
    """Build a raw kubectl command (no transport wrapper).

    Returns ``[kubectl, --kubeconfig, ..., subcommand, ...args]``.
    Transport wrapping (wiz/ssh) is handled by the transport layer.
    """
    if isinstance(v_args, str) and v_args:
        args_list = _split_args(v_args)
    elif isinstance(v_args, list):
        args_list = v_args
    else:
        args_list = []

    runtime_settings = settings or globals()["settings"]

    cmd = [runtime_settings.kubectl_path]
    cmd.extend(_build_kubectl_global_args(kubeconfig, context, cluster))
    cmd.append(subcommand)
    cmd.extend(args_list)
    return cmd


async def exec_kubectl_raw(
    subcommand: str,
    v_args: "list[str] | str" = "",
    kubeconfig: str = "",
    context: str = "",
    cluster: str = "",
    timeout: float = 30.0,
) -> CommandResult:
    """Execute kubectl via transport layer (lightweight internal checks).

    Use this for preflight, env_info, safety_check — internal calls that
    don't need LLM tool-call overhead.  For LLM-driven tool calls, use
    _kubectl_impl() instead.
    """
    cmd = build_kubectl_cmd(subcommand, v_args, kubeconfig, context, cluster)
    target = TransportTarget.from_state({})
    try:
        return await execute_via_transport(cmd, target, timeout=timeout, expect_profile=PROFILE_K8S)
    except ToolGuardError as exc:
        return CommandResult(exit_code=-1, stdout="", stderr=f"guard rejected: {exc}")
    except ToolTimeoutError:
        return CommandResult(exit_code=-1, stdout="", stderr=f"timed out after {timeout}s")
    except FileNotFoundError:
        return CommandResult(exit_code=-1, stdout="", stderr="kubectl/wiz not found")
    except Exception as exc:
        return CommandResult(exit_code=-1, stdout="", stderr=str(exc))


def display_cmd(cmd: list[str]) -> str:
    """Return a human/LLM-facing command string.

    Delegates to the transport layer to strip any transport wrapper
    (wiz/ssh) and show only the inner semantic command.
    """
    target = TransportTarget.from_state({})
    return display_via_transport(cmd, target)


def _is_json_output(v_args: str) -> bool:
    """Check whether v_args requests JSON output (-o json, not jsonpath or yaml)."""
    parts = _split_args(v_args)
    for i, part in enumerate(parts):
        if part == "-o" and i + 1 < len(parts):
            fmt = parts[i + 1]
            return fmt == "json"
        if part.startswith("-o=") or part.startswith("-ojson"):
            fmt = part.split("=", 1)[-1] if "=" in part else part[2:]
            return fmt == "json"
    return False


@tool
async def kubectl(
    subcommand: str,
    v_args: str = "",
    stdin_data: str = "",
    kubeconfig: str = "",
    context: str = "",
    cluster: str = "",
) -> str:
    """Phase 2 (execution) tool. Full kubectl with mutation subcommands bound.

    Mutating: supports exec / delete / patch / label / annotate / apply /
    scale / set / taint / cordon / uncordon / drain / rollout / debug and more.
    ``edit`` and ``replace`` are NOT available (interactive / whole-object
    overwrite) — express the change as ``patch``. ChaosBlade-aware:
    auto-injects ``--timeout`` for ``exec ... blade create``.

    NOT available in Phase 1 (planning); use ``kubectl_read`` there for
    read-only inspection.

    Single entry point covering all kubectl subcommands. Pick `subcommand` and
    pass the rest of the CLI args as `v_args`.

    When to use:
      - Cluster inspection in any phase (get / describe / top / logs).
      - Phase 2 mutation (delete / patch / set / scale / cordon / uncordon /
        taint / label / annotate / drain).
      - Verification probing inside containers or on nodes (exec / debug).
      - Creating non-workload resources (PV / PVC / Secret / ConfigMap):
        use ``subcommand="apply"`` with ``v_args="-f -"`` and pass the
        YAML via ``stdin_data``. Workload resources (Deployment, Pod, Job,
        etc.) are blocked.
      - Do NOT use ``exec ... | kubectl apply`` or ``exec ... kubectl create``
        to create resources — this causes namespace drift and will be rejected.

    Inputs:
      - subcommand: one of {get, describe, top, logs, exec, delete, patch, set,
                            scale, cordon, uncordon, taint, label, annotate,
                            drain, debug, apply}.
      - v_args: subcommand arguments as a single shell-quoted string. Examples:
          get      → "pods -n <ns> -o json"
                     "pods -n <ns> -l app=nginx --field-selector=status.phase=Pending"
                     "events -n <ns> --sort-by=.lastTimestamp"
          describe → "pod <pod> -n <ns>"   |   "node <node>"
          top      → "pod -n <ns> --sort-by=cpu"   |   "node <node>"
          logs     → "<pod> -n <ns> --tail=50 --previous -c <container>"
          exec     → "<pod> -n <ns> -- <cmd>"
                     "<pod> -n chaosblade -- blade create k8s pod-cpu fullload --cpu-percent 80"
          debug    → "node/<node> --profile=sysadmin --image=<cluster-image> -- sleep 3600"
                     (then exec into the returned debug pod)
          delete   → "pod <pod> -n <ns> --force --grace-period=0"
          patch    → "pod <pod> -n <ns> --type=json -p '[{\\"op\\":\\"add\\",\\"path\\":\\"/metadata/labels/x\\",\\"value\\":\\"y\\"}]'"
          label    → "node <node> chaos-target=<app> --overwrite"   |   "node <node> chaos-target-"
          annotate → "node <node> <key>=<value> --overwrite"
          scale    → "deployment <name> -n <ns> --replicas=0"
          taint    → "nodes <node> key=value:NoSchedule"   |   "nodes <node> key-"
          drain    → "<node> --ignore-daemonsets --delete-emptydir-data
                      --grace-period=30 --timeout=120s"
                     (``--force`` / ``--disable-eviction`` are refused: they
                      delete pods no controller recreates, or bypass
                      PodDisruptionBudgets. Recover with ``uncordon``.)
          apply    → "-f -" (with stdin_data containing PV/PVC/Secret/ConfigMap YAML)
        See knowledge resource `kubectl-recipes.md` for the long-tail catalogue.
      - stdin_data: YAML content for ``apply -f -``. Pass the full YAML here
        instead of embedding it in v_args or using exec heredoc.
      - kubeconfig / context / cluster: optional overrides
        (do NOT embed --kubeconfig in v_args — it is auto-stripped).

    Output: stdout from kubectl, or an "Error: ..." string on non-zero exit.
            Large `get -o json` output gets a "⚠️ LARGE_OUTPUT" hint footer.
            Empty `get -l ...` output gets a label-discovery hint footer.

    Side effects:
      - get / describe / top / logs / exec (read-only commands inside containers): none.
      - delete / patch / set / scale / cordon / uncordon / taint / label /
        annotate / drain / debug: mutate cluster state. Treat as Phase 2
        actions and verify aftermath.

    Self-help (IMPORTANT — use this instead of guessing from memory):
      - Pass `--help` or `-h` in v_args to see the real usage of any subcommand.
        Example: kubectl(subcommand="get", v_args="--help")
      - This returns the live kubectl help text, which is ALWAYS more accurate
        than documentation, skill instructions, or knowledge resources.
      - When a command fails with an unknown flag or argument error, call
        `--help` BEFORE retrying — do NOT guess from prior context.

    Constraints (MUST READ before calling):
      - No shell features: `|`, `;`, `&&`, `>`, `$()` are NOT supported. Use
        `-l/--selector`, `--field-selector`, `-o jsonpath` instead of pipelines.
      - `kubectl exec` rejects `-l/--selector` — first run `kubectl get` to
        resolve a concrete pod name, then exec on that name. The flag is
        auto-stripped with a warning if you forget.
      - `kubectl debug node/<node> --image=busybox` MUST include `-- sleep 3600`
        (or another keep-alive) — bare invocations exit immediately. Never pass
        `-it` (verifier is non-interactive). Host paths inside the debug pod
        live under `/host/...`.
      - A debug pod used for host mutation MUST include `--profile=sysadmin`.
        Use an image already verified as pullable in the current cluster; do
        not assume a public image is reachable. Wait for this tool's structured
        debug-pod result before exec and use the returned name/namespace.
      - `exec ... blade create` auto-injects / auto-boosts `--timeout` to the
        recommended minimum, mirroring blade_create's behavior. You can pass a
        longer --timeout but cannot make it shorter.

    Recovery patterns (translating manual operations to programmatic kubectl):
      - "kubectl edit Pod" → patch with --type flag (strategic merge / json merge / json patch)
      - "manually delete finalizers" → patch with --type=json -p '[{"op":"remove","path":"/metadata/finalizers"}]'
      - "force delete a stuck Pod" → delete with --force --grace-period=0
      - "remove a taint" → taint with the taint key followed by '-' (e.g., "nodes <node> key-")
    """
    return await _kubectl_impl(subcommand, v_args, kubeconfig, context, cluster, stdin_data=stdin_data)


async def _kubectl_impl(
    subcommand: str,
    v_args: str = "",
    kubeconfig: str = "",
    context: str = "",
    cluster: str = "",
    stdin_data: str = "",
) -> str:
    """Shared kubectl execution logic used by both kubectl and kubectl_read."""
    # kubewiz mode does not support stdin piping
    _target = TransportTarget.from_state({})
    _channel = TransportRegistry.resolve(_target)
    if _channel.name == "kubewiz_k8s" and stdin_data:
        return (
            "Error: kubewiz mode does not support stdin piping. "
            "Use imperative commands instead: "
            "kubectl create configmap NAME --from-literal=key=value, "
            "kubectl create secret generic NAME --from-literal=key=value"
        )

    selector_removed = False
    processed_args: list[str] = []

    if v_args:
        # Defensive: strip --kubeconfig embedded in v_args by LLM mistake.
        if "--kubeconfig" in v_args:
            v_args = re.sub(r"--kubeconfig\s+\S+", "", v_args).strip()
            logger.warning(
                "kubeconfig should be passed via dedicated 'kubeconfig' parameter, "
                "not embedded in v_args. The embedded value has been removed."
            )

        # Validate exec subcommand: reject -l/--selector (not supported by kubectl exec)
        if subcommand == "exec":
            selector_pattern = re.compile(r"(?:^|\s)(-l|--selector)\s+\S+")
            if selector_pattern.search(v_args):
                v_args = selector_pattern.sub("", v_args).strip()
                selector_removed = True
                logger.warning(
                    "kubectl exec does not support -l/--selector. "
                    "Removed from v_args. Use kubectl get to discover the pod name first."
                )

        processed_args = _split_args(v_args)

    debug_namespace = ""
    if subcommand == "debug":
        debug_namespace = _namespace_from_args(processed_args)
        if not debug_namespace:
            debug_namespace = await _resolve_effective_namespace(
                kubeconfig, context, cluster,
            )
            if "--" in processed_args:
                separator = processed_args.index("--")
                processed_args[separator:separator] = ["-n", debug_namespace]
            else:
                processed_args.extend(["-n", debug_namespace])

    # Auto-inject/boost --timeout for kubectl exec blade create commands.
    # Must happen BEFORE build_kubectl_cmd so --timeout is in processed_args.
    if subcommand == "exec" and v_args and re.search(r"\bblade\s+create\b", v_args):
        _fault_match = re.search(
            r"blade\s+create\s+k8s\s+(pod|node|container)-(\w+)\s+(\w+)", v_args
        )
        _scope, _fault_target, _action = (
            (_fault_match.group(1), _fault_match.group(2), _fault_match.group(3))
            if _fault_match else (None, None, None)
        )
        from chaos_agent.utils.fault_type import ensure_min_duration
        if "--timeout" not in v_args:
            effective_timeout = ensure_min_duration(None, _scope, _fault_target, _action)
            processed_args.extend(["--timeout", str(effective_timeout)])
            logger.info(
                f"Auto-injected --timeout {effective_timeout}s into "
                f"kubectl exec blade create command"
            )
        else:
            try:
                _timeout_match = re.search(r"--timeout\s+(\d+)", v_args)
                if _timeout_match:
                    _current_val = _timeout_match.group(1)
                    _effective = ensure_min_duration(_current_val, _scope, _fault_target, _action)
                    if _effective != int(_current_val):
                        for i, token in enumerate(processed_args):
                            if token == "--timeout" and i + 1 < len(processed_args) and processed_args[i + 1] == _current_val:
                                processed_args[i + 1] = str(_effective)
                                logger.info(
                                    f"Auto-boosted --timeout from {_current_val}s to {_effective}s "
                                    f"for {_scope}-{_fault_target}-{_action} (recommended minimum)"
                                )
                                break
            except (ValueError, TypeError):
                pass

    cmd = build_kubectl_cmd(subcommand, processed_args, kubeconfig, context, cluster)

    # exec/debug subcommands use longer timeout (container commands may be slow;
    # debug needs to pull images and create ephemeral containers)
    timeout = settings.timeout_kubectl_exec if subcommand in ("exec", "debug") else settings.timeout_kubectl

    try:
        result = await execute_via_transport(
            cmd, _target, timeout=timeout, stdin_data=stdin_data,
            # Both LLM-facing kubectl tools funnel through here, and both need
            # cluster access. A host-profile channel (ssh / kubewiz_host) gives
            # a shell on one machine and cannot serve cluster operations.
            expect_profile=profile_for_tool("kubectl"),
        )
    except Exception as e:
        # Surface the raw signal without a "failed" verdict. For a self-severing
        # injection (e.g. node network isolation) THIS exec times out ON SUCCESS;
        # editorializing it as "failed" misleads the LLM. Keep the "Error:" prefix
        # (the framework's failure-marker contract used by downstream detection)
        # but let the raw text — e.g. "Command timed out after 10s" — speak.
        return f"Error: kubectl {subcommand}: {e}"

    if result.exit_code != 0:
        # kubewiz 模式下错误信息在 stdout，直接模式在 stderr
        error_detail = result.stderr or result.stdout
        # Report the exit code + raw output verbatim; no "failed" verdict word.
        return f"Error: kubectl {subcommand} (exit {result.exit_code}): {error_detail}"

    output = result.stdout

    # Append large output hint for get subcommand with JSON output
    if subcommand == "get" and _is_json_output(v_args) and settings.kubectl_max_output_bytes > 0:
        output_bytes = len(output.encode("utf-8", errors="replace"))
        if output_bytes > settings.kubectl_max_output_bytes:
            size_kb = output_bytes // 1024
            output += (
                f"\n\n⚠️ LARGE_OUTPUT: Output is large ({size_kb}KB). Narrow the scope using:\n"
                f"- Use --field-selector to filter (e.g., --field-selector spec.nodeName=<node>)\n"
                f"- Use -o name to get resource names only\n"
                f"- Specify a resource name to query a single resource\n"
                f"- Use -o jsonpath to extract specific fields"
            )

    # Append exec parameter correction warning
    if subcommand == "exec" and selector_removed:
        output += (
            "\n\n⚠️ kubectl exec does NOT support -l/--selector. "
            "The flag was removed from your command. "
            "Use kubectl(subcommand='get') to discover the pod name first, "
            "then use kubectl(subcommand='exec', v_args='<pod-name> -n <ns> -- <command>')."
        )

    # Debug pod lifecycle — creation alone is not execution readiness. Resolve
    # the authoritative namespace/UID/node and fail early on image pull errors.
    if subcommand == "debug":
        _debug_ns = debug_namespace or _namespace_from_args(processed_args) or "default"

        # Pod-scoped debug (``kubectl debug <pod> --target=<c> ...``) attaches an
        # EPHEMERAL CONTAINER to an existing pod. kubectl prints no container
        # name on stdout — it is only in the target pod's
        # ``ephemeralContainerStatuses``. The target pod is the USER'S workload:
        # it must never be treated as a created debug pod (no delete on
        # cleanup). Node-scoped debug (``node/<node>``) keeps the original path.
        _target_pod = _debug_target_pod_name(processed_args)
        if _target_pod:
            running, container_name, tgt_meta, ec_error = (
                await _wait_for_ephemeral_container(
                    _target_pod, _debug_ns, kubeconfig, context, cluster,
                )
            )
            if not container_name:
                # Distinguish "not created" from "created but not parsed": tell
                # the model exactly where the name lives so it does not loop.
                return (
                    "Error: kubectl debug attached no ephemeral container to "
                    f"pod '{_target_pod}'. Raw output: {output}\n"
                    "If the debug command reported 'Targeting container ...', the "
                    "ephemeral container WAS created but its name is only in "
                    "``.status.ephemeralContainerStatuses`` (kubectl does not "
                    "print it). Read it with: kubectl(subcommand='get', "
                    f"v_args='pod {_target_pod} -n {_debug_ns} -o "
                    "jsonpath={.status.ephemeralContainerStatuses[*].name}')."
                )
            if not running:
                return (
                    f"Error: ephemeral container '{container_name}' on pod "
                    f"'{_target_pod}' did not start: {ec_error}. "
                    "Do NOT delete the target pod — it is the user's workload; "
                    "the ephemeral container is bound to its lifecycle. Retry "
                    "with a pullable image if this was an image-pull failure."
                )
            _profile = _extract_debug_profile(v_args)
            meta_payload = {
                **tgt_meta,
                "name": _target_pod,
                "namespace": _debug_ns,
                "ephemeral_container": container_name,
                "ready": True,
                "cleaned": False,
                "debug_profile": _profile,
            }
            meta_tag = json.dumps(meta_payload, ensure_ascii=True, separators=(",", ":"))
            output += (
                f"\n\n[debug-pod-meta: {meta_tag}]"
                f"\n[debug-pod-ns: {_debug_ns}]"
                f"\nThe ephemeral container is running. Exec into it with: "
                f"kubectl(subcommand='exec', v_args='{_target_pod} -n {_debug_ns} "
                f"-c {container_name} -- <command>')."
                "\nIt shares the target pod's namespaces (network/pid); operate on "
                "eth0 there to affect exactly this pod. There is NOTHING to clean "
                "up — an ephemeral container is removed only when the pod is "
                "recreated; do NOT delete the pod."
            )
            return output

        _debug_pod = _parse_debug_pod_name(output)
        if not _debug_pod:
            return (
                "Error: kubectl debug created no identifiable pod. "
                f"Raw output: {output}"
            )
        ready, metadata, ready_error = await _wait_for_created_debug_pod(
            _debug_pod, _debug_ns, kubeconfig, context, cluster,
        )
        if metadata.get("namespace"):
            _debug_ns = metadata["namespace"]
        cleaned = False
        if not ready:
            cleaned = await _delete_created_debug_pod(
                _debug_pod, _debug_ns, kubeconfig, context, cluster,
            )
        # Extract --profile value from v_args so debug-pod-meta carries
        # the profile the Agent requested (needed by carrier resolution
        # and useful for debugging).
        _profile = _extract_debug_profile(v_args)
        meta_payload = {
            **metadata,
            "name": metadata.get("name") or _debug_pod,
            "namespace": _debug_ns,
            "ready": ready,
            "cleaned": cleaned,
            "debug_profile": _profile,
        }
        meta_tag = json.dumps(meta_payload, ensure_ascii=True, separators=(",", ":"))
        if not ready:
            return (
                f"Error: {ready_error}.\n"
                f"[debug-pod-meta: {meta_tag}]\n"
                "The pod object exists but is not executable. Do NOT call kubectl exec. "
                + (
                    "The failed pod was cleaned up automatically."
                    if cleaned
                    else f"Cleanup failed; delete pod {_debug_pod} -n {_debug_ns}."
                )
            )
        output += (
            f"\n\n[debug-pod-meta: {meta_tag}]"
            f"\n[debug-pod-ns: {_debug_ns}]"
            "\nThe debug pod is Ready. Clean it up after use with: "
            f"kubectl(subcommand='delete', v_args='pod {_debug_pod} -n {_debug_ns}')."
        )

    # Label discovery hint for empty get results with label selector
    if (
        subcommand == "get"
        and not output.strip()
        and ("-l " in v_args or "--selector " in v_args)
    ):
        output += (
            "\n\n💡 No resources matched the label selector. "
            "Try running without -l to discover available pods, "
            "then inspect their actual labels with: "
            "kubectl(subcommand='get', v_args='pod <name> -n <ns> -o jsonpath={.metadata.labels}')"
        )

    return output


# ── Phase 1 read-only kubectl flavour ──────────────────────────────────
#
# Background (task-ce9647931ce1): planning-phase agent_loop had the full
# ``kubectl`` bound, and the LLM — once it撞 the ``blade_create`` black-
# list — pivoted to ``kubectl exec <chaosblade-controller-pod> -- blade
# create ...`` to inject anyway. The whole point of the agent_loop →
# safety_check → confirmation_gate → execute_loop pipeline is that
# planning has zero side effects, so the user's reject at
# confirmation_gate actually leaves the cluster untouched.
#
# Mitigation strategy (multi-layer, see design plan):
#   - Layer A (THIS): physically remove mutation subcommands from the
#     Phase 1 tool surface. The LLM cannot call what's not in the schema.
#   - Layer D: ToolNode error handler refuses to list "try one of [...]"
#     alternatives that would re-suggest the bypass.
#   - Layer F: a phase1_screener as last-resort runtime check.
#
# The ``Literal`` type below is enforced by LangChain's tool argument
# validation; passing any other subcommand returns a Pydantic
# ValidationError that ToolNode catches and surfaces via the Layer D
# error handler.
READONLY_SUBCOMMANDS: tuple[str, ...] = (
    "get", "describe", "top", "logs",
    "version", "cluster-info", "api-resources", "explain", "auth",
    "exec", "debug",
)


@tool
async def kubectl_read(
    subcommand: Literal[
        "get", "describe", "top", "logs",
        "version", "cluster-info", "api-resources", "explain", "auth",
        "exec", "debug",
    ],
    v_args: str = "",
    kubeconfig: str = "",
    context: str = "",
    cluster: str = "",
) -> str:
    """READ-ONLY kubectl — the single observation tool for every read-only
    phase (intent / planning / verification).

    Read-only BY ENFORCEMENT: read verbs (get/describe/top/logs/version/
    cluster-info/api-resources/explain/auth) always; ``exec``/``debug`` only when
    the inner command is a read-only probe. A mutating inner command
    (``iptables -A``, ``ip link set``, ``systemctl stop``, ``dd``, ``dmesg -C``,
    redirection/chaining, ``chroot``/``nsenter`` escapes) is REJECTED with the
    specific reason — fault INJECTION is Phase 2 (full ``kubectl``, after the
    plan is approved).

    When to use:
      - Confirm a target exists / inspect state, labels, events (`get`, `describe`).
      - Capture / compare metrics (`top`); read logs (`logs`).
      - Discover the API (`api-resources`, `explain`); check perms (`auth can-i`).
      - Probe INSIDE a pod, read-only (`exec`):
          binary/file exists → ``exec <pod> -n <ns> -- ls /usr/bin/stress-ng``
          dns / http / procs → ``-- nslookup <svc>`` · ``-- wget -qO- <url>`` · ``-- ps aux``
          netfilter rules    → ``-- iptables -L``
          pipe / filter      → ``-- sh -c "ps aux | grep java"`` (every stage read-only)
      - Probe a node's host fs / kernel / image CAPABILITY via a debug pod
        (`debug` — allowed in EVERY read-only phase, INCLUDING planning/intent,
        as a capability probe; it creates an ephemeral Pod that is auto-cleaned
        when the phase ends):
          ``debug node/<node> --image=<cluster-image> -- sleep 60`` then
          ``exec <debug-pod> -n default -- which sh chroot`` or
          ``exec <debug-pod> -n default -- cat /host/proc/loadavg`` (host paths
          under ``/host/...``). Use a SHORT keep-alive (``-- sleep 60``) for
          probes; never ``-it``. Use this in PLANNING to verify a candidate
          debug image actually carries the binaries your plan needs
          (e.g. ``sh``/``chroot``) BEFORE committing to it.

    exec/debug constraints:
      - Bare shell operators between args don't work (exec is shell=False); to
        pipe/filter, wrap in ``sh -c "..."`` and keep EVERY stage read-only.
        Redirection ``>``, chaining ``;`` / ``&&``, backgrounding ``&`` are rejected.
      - No ``-l/--selector`` (resolve the pod name via `get` first); no ``-it``.

    Self-help: pass ``--help`` / ``-h`` in v_args for the live usage of any
    subcommand — ALWAYS more accurate than memory; call it BEFORE retrying an
    unknown-flag/argument error rather than guessing.

    Inputs / Output: same shape as the full ``kubectl`` tool. Thin wrapper that
    re-uses ``kubectl``'s execution logic with the subcommand domain
    constrained and ``exec``/``debug`` inner commands gated to read-only.
    """
    # Belt-and-braces: even if Literal validation is bypassed, reject any
    # subcommand outside the read-only set at runtime.
    if subcommand not in READONLY_SUBCOMMANDS:
        return (
            f"Error: kubectl_read does not accept subcommand '{subcommand}'.\n"
            f"kubectl_read is read-only by enforcement. Allowed subcommands: "
            f"{', '.join(READONLY_SUBCOMMANDS)}.\n"
            f"Mutation subcommands (delete/patch/scale/...) run in Phase 2 via "
            f"the full kubectl tool after your plan is approved."
        )
    # ``exec`` / ``debug`` inner command must be a read-only probe — the shared
    # classifier judges it (same vocabulary the guard-scope screeners use), and
    # a rejection carries the SPECIFIC reason so the model can self-correct.
    if subcommand in ("exec", "debug"):
        from chaos_agent.tools.readonly import kubectl_exec_rejection_reason

        reason = kubectl_exec_rejection_reason(v_args)
        if reason is not None:
            return (
                f"Error: kubectl_read rejected this {subcommand} — its inner "
                f"command is not read-only: {reason}.\n"
                f"kubectl_read only runs read-only probes. To fix:\n"
                f"- To CHECK a binary/file exists, use "
                f"`{subcommand} ... -- ls <absolute-path>` "
                f"(e.g. `-- ls /usr/bin/stress-ng`).\n"
                f"- To READ rules/state, use a read form "
                f"(`-- iptables -L`, `-- ip addr show`, `-- systemctl status`, "
                f"`-- cat <path>`).\n"
                f"- If this is genuinely a FAULT-INJECTION command, it belongs "
                f"to Phase 2 (execution), not to a read-only phase."
            )
    # Call the shared implementation directly — NOT kubectl.ainvoke(), which
    # would emit a nested on_tool_start event (duplicate TUI tool card).
    return await _kubectl_impl(subcommand, v_args, kubeconfig, context, cluster)
