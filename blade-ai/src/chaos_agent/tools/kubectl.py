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
import time
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


def _debug_target_node_name(processed_args: list[str]) -> str:
    """The target node for a node-scoped ``kubectl debug`` call, else ``""``.

    Only node-scope calls create a discoverable ``node-debugger-*`` pod, so
    the parse-failure discovery fallback needs the node name to filter by
    ``spec.nodeName`` (project convention). Handles both ``node/<name>`` and
    the two-token ``node <name>`` forms. Flags that take a separate value
    (``-n default``, ``--image busybox``) must skip BOTH tokens — otherwise
    the flag's value is misread as the first positional and the discovery
    fallback is silently disabled for calls like ``-n default node/a``.
    """
    _VALUE_FLAGS = {
        "-n", "--namespace", "--image", "--profile",
        "-c", "--container", "--subresource",
    }
    i = 0
    while i < len(processed_args):
        tok = processed_args[i]
        if tok == "--":
            break
        if tok in _VALUE_FLAGS:
            i += 2  # skip flag AND its value
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if tok.startswith("node/"):
            return tok[len("node/"):]
        if tok == "node" and i + 1 < len(processed_args):
            return processed_args[i + 1]
        return ""  # first positional is a pod → pod-scoped debug
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

    ``--copy-to`` also returns ``""``: kubectl documents it as "Create a copy of
    the target Pod with this name", so it produces a NEW tool-owned pod (whose
    name kubectl does print) rather than an ephemeral container. Routing it here
    would look for ephemeral containers that never appear and leave the copy
    running with nothing tracking it.

    Value-consuming flags in SPACE form (``--image busybox``, ``-n ns``) must
    have their VALUE skipped — otherwise the value is mistaken for the pod name
    (e.g. ``--image busybox p0`` would return ``busybox``, or ``-n ns p0`` would
    return ``ns``). ``--flag=value`` form is a single ``-``-prefixed token and
    is already skipped as a flag.
    """
    # debug flags that take a SEPARATE value token (space form).
    value_flags = {
        "-n", "--namespace", "--image", "--target", "-c", "--container",
        "--profile", "--image-pull-policy", "--env", "--custom", "--copy-to",
        "--set-image",
    }
    # Copy mode creates a pod, not an ephemeral container (see docstring).
    for tok in processed_args:
        if tok == "--":
            break
        if tok == "--copy-to" or tok.startswith("--copy-to="):
            return ""
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
    # One-shot ``kubectl debug ... -- CMD`` pods end in Succeeded/Failed; the
    # command's exit code / termination reason live in the first container's
    # ``terminated`` state. ``None`` = container has not terminated yet.
    exit_code = None
    terminated_reason = ""
    for container_status in container_statuses:
        state_obj = container_status.get("state") or {}
        waiting = state_obj.get("waiting") or {}
        if waiting.get("reason"):
            waiting_reasons.append(waiting["reason"])
        terminated = state_obj.get("terminated") or {}
        if exit_code is None and terminated:
            exit_code = terminated.get("exitCode")
            terminated_reason = terminated.get("reason") or ""
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
        "exit_code": exit_code,
        "terminated_reason": terminated_reason,
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


def _debug_has_oneshot_command(processed_args: list[str]) -> bool:
    """Whether a debug call is ONE-SHOT COMMAND mode (``... -- CMD ...``).

    Command mode runs CMD once and the pod terminates (Succeeded/Failed) — the
    ``condition=Ready`` gate used by interactive mode NEVER becomes true there,
    so waiting for it is a guaranteed false negative (task-29848471 k3 false
    alarm). Two forms stay INTERACTIVE despite having tokens after ``--``:

      - ``-- sleep <N>`` — the project's documented convention for node debug
        (``MUST append -- sleep 3600``): a keep-alive placeholder so the
        caller can ``kubectl exec`` afterwards. The pod stays Running/Ready.
        Equivalent keep-alive spellings stay interactive too: an absolute
        path sleep (``/bin/sleep 3600``) and a PURE sleep wrapped in a shell
        (``sh -c 'sleep 3600'``). A composite script (``sh -c 'sleep 5 &&
        df -h'``) is still one-shot — it terminates on its own.
      - ``-it`` / ``-i`` / ``-t`` flags — interactive attach by intent.

    A trailing bare ``--`` (no tokens) is interactive too.
    """
    interactive_flags = {"-it", "-i", "-t", "--stdin", "--tty"}
    for tok in processed_args:
        if tok == "--":
            break
        if tok in interactive_flags:
            return False
    if "--" not in processed_args:
        return False
    command = processed_args[processed_args.index("--") + 1:]
    if not command:
        return False  # trailing bare `--` — interactive
    return not _is_keepalive_sleep(command)


def _is_keepalive_sleep(command: list[str]) -> bool:
    """Whether the debug CMD is a pure keep-alive sleep placeholder.

    Covers the documented bare form (``sleep 3600``), the absolute-path
    variant (``/bin/sleep``), and a shell wrapping NOTHING BUT a sleep
    (``sh -c 'sleep 3600'`` / ``bash -c "sleep 60"``). Anything composite
    (``sleep 30 && df -h``) returns False — it terminates on its own and
    belongs to one-shot mode. Misclassifying a keep-alive as one-shot
    kills the carrier pod the caller is about to exec into.
    """
    if not command:
        return False
    base = command[0].rsplit("/", 1)[-1]
    if base == "sleep":
        return True
    if base in ("sh", "bash") and "-c" in command:
        script = command[command.index("-c") + 1] if command.index("-c") + 1 < len(command) else ""
        return bool(re.fullmatch(r"\s*sleep\s+\d+\s*", script or ""))
    return False


async def _wait_for_debug_pod_terminal(
    pod_name: str,
    namespace: str,
    kubeconfig: str,
    context: str,
    cluster: str,
) -> tuple[bool, dict, str]:
    """Poll a one-shot debug pod until its phase is terminal.

    Returns ``(terminal, metadata, error)``. Terminal means ``Succeeded`` or
    ``Failed`` (the CMD ran to completion — success/failure of the COMMAND is
    in ``metadata['exit_code']``, not here); a still-running pod at the budget
    limit returns ``(False, last_metadata, ...)``. Budget is capped at 120s —
    debug one-shots are probes, not long jobs.
    """
    wait_seconds = min(120, max(1, int(settings.timeout_kubectl_exec)))
    deadline = asyncio.get_running_loop().time() + wait_seconds
    last_error = ""
    metadata: dict = {}
    while True:
        metadata, meta_error = await _debug_pod_metadata(
            pod_name, namespace, kubeconfig, context, cluster,
        )
        if meta_error:
            return False, {}, meta_error
        phase = metadata.get("phase") or "Unknown"
        if phase in ("Succeeded", "Failed"):
            return True, metadata, ""
        last_error = f"still in phase {phase}"
        if asyncio.get_running_loop().time() >= deadline:
            return False, metadata, (
                f"one-shot debug pod did not terminate within {wait_seconds}s "
                f"({last_error})"
            )
        await asyncio.sleep(2)


async def _debug_pod_logs_tail(
    pod_name: str,
    namespace: str,
    kubeconfig: str,
    context: str,
    cluster: str,
    tail: int = 20,
) -> str:
    """Best-effort last ``tail`` log lines of a terminated debug pod."""
    cmd = build_kubectl_cmd(
        "logs", [pod_name, "-n", namespace, f"--tail={tail}"],
        kubeconfig, context, cluster,
    )
    try:
        target = TransportTarget.from_state({})
        result = await execute_via_transport(
            cmd, target, timeout=settings.timeout_kubectl, expect_profile=PROFILE_K8S,
        )
    except Exception:
        logger.debug("Failed to fetch debug pod logs %s/%s", namespace, pod_name, exc_info=True)
        return ""
    if result.exit_code != 0:
        return ""
    return (result.stdout or "").strip()


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
    """Phase 2 (execution): full kubectl incl. mutation. Pick `subcommand`,
    pass remaining CLI args as `v_args`. NOT Phase 1 — use ``kubectl_read``.

    When to use:
      - Any inspection/mutation; probing inside containers/on nodes.
      - Non-workload resources (PV/PVC/Secret/ConfigMap) via ``apply`` +
        YAML in ``stdin_data``; workload creation blocked.

    Inputs:
      - subcommand: get|describe|top|logs|exec|delete|patch|set|scale|
        cordon|uncordon|taint|label|annotate|drain|debug|apply;
        ``edit``/``replace`` unavailable — use ``patch``.
      - v_args: shell-quoted args (recipes: `kubectl-recipes.md`).
      - stdin_data: YAML for ``apply -f -`` (not via v_args/exec heredoc).
      - kubeconfig/context/cluster: overrides; never --kubeconfig in
        v_args (auto-stripped).

    Output: stdout, or "Error: ..." on non-zero exit.

    Side effects: read verbs none; all else mutates cluster state.

    Constraints:
      - No shell features (`|`, `;`, `&&`, `>`, `$()`) — use
        `-l/--selector`, `--field-selector`, `-o jsonpath`.
      - `exec` rejects `-l/--selector` (auto-stripped) — resolve the pod
        via `get` first.
      - `debug node/<node>`: MUST append `-- sleep 3600`; never `-it`;
        host paths under `/host/...`; host mutation needs
        `--profile=sysadmin` + pullable image (recipes).
      - `exec ... blade create` auto-injects/boosts `--timeout` (may
        lengthen, not shorten).
      - `drain` refuses `--force`/`--disable-eviction`; recover via
        `uncordon`.
      - Unknown-flag error → `--help`; do NOT guess and retry.
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
    _debug_start_ts = 0.0
    if subcommand == "debug":
        # Wall-clock anchor BEFORE dispatch: the parse-failure discovery
        # fallback filters candidates by creationTimestamp recency.
        _debug_start_ts = time.time()
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

        # Single parsing source — shared with baseline/verifier/recover
        # (lazy import: _debug_pod imports build_kubectl_cmd from here).
        from chaos_agent.agent.nodes.execute._debug_pod import (
            discover_created_debug_pod,
            parse_debug_pod_name,
        )
        _debug_pod = parse_debug_pod_name(output)
        if not _debug_pod:
            # Discovery fallback (node scope only): one live get-pods filtered
            # by spec.nodeName + node-debugger- prefix + recency. Only runs on
            # the parse-failure path; the normal path pays zero extra cost.
            _node = _debug_target_node_name(processed_args)
            if _node:
                _debug_pod = await discover_created_debug_pod(
                    _node, _debug_ns, _debug_start_ts,
                    kubeconfig, context, cluster,
                )
                if _debug_pod:
                    output += (
                        f"\n(kubectl printed no pod name; discovered "
                        f"'{_debug_pod}' on node '{_node}' via live lookup.)"
                    )
        if not _debug_pod:
            # Parse AND discovery both failed. Exit 0 + no pod name means the
            # create may never have executed at all (transport drop,
            # API-server reject without event). Telling the model to retry
            # the SAME command is what burned the k3 budget three rounds in
            # a row (task-29848471).
            return (
                "Error: kubectl debug returned exit 0 but no debug pod name "
                "could be identified. The create may never have executed — "
                "this is NOT evidence the command is wrong. Do NOT retry the "
                "same command blindly; first verify the current cluster state "
                "or use a different path to reach the target. "
                f"Raw output: {output}"
            )
        _profile = _extract_debug_profile(v_args)

        # ---- One-shot COMMAND mode (`debug ... -- CMD`): the pod runs CMD
        # once and terminates. condition=Ready is never true there, so waiting
        # for it is a guaranteed false negative — poll the terminal phase and
        # report the COMMAND's exit code instead (task-29848471 false alarm).
        if _debug_has_oneshot_command(processed_args):
            terminal, metadata, terminal_error = await _wait_for_debug_pod_terminal(
                _debug_pod, _debug_ns, kubeconfig, context, cluster,
            )
            if metadata.get("namespace"):
                _debug_ns = metadata["namespace"]
            logs_tail = await _debug_pod_logs_tail(
                _debug_pod, _debug_ns, kubeconfig, context, cluster,
            )
            # A finished one-shot has served its purpose — remove it whether
            # the command succeeded or not; task-level cleanup remains the
            # second safety net.
            cleaned = await _delete_created_debug_pod(
                _debug_pod, _debug_ns, kubeconfig, context, cluster,
            )
            if terminal:
                _exit = metadata.get("exit_code")
                if _exit is None and metadata.get("phase") == "Failed":
                    _exit = 1  # Failed pod without an exitCode — non-zero by definition
                elif _exit is None and metadata.get("phase") == "Succeeded":
                    # containerStatuses can lag behind the phase; a Succeeded
                    # pod is exit 0 by definition (else it would be Failed).
                    _exit = 0
                meta_payload = {
                    **metadata,
                    "name": metadata.get("name") or _debug_pod,
                    "namespace": _debug_ns,
                    "ready": _exit == 0,
                    "cleaned": cleaned,
                    "debug_profile": _profile,
                    "oneshot": True,
                }
                meta_tag = json.dumps(meta_payload, ensure_ascii=True, separators=(",", ":"))
                _logs_section = f"\nCommand output (logs tail):\n{logs_tail}" if logs_tail else ""
                if _exit == 0:
                    return (
                        f"{output}\n\n[debug-pod-meta: {meta_tag}]"
                        f"\n[debug-pod-ns: {_debug_ns}]"
                        f"\nOne-shot debug command completed with exit_code=0."
                        f"{_logs_section}\n"
                        "The debug pod has been removed; there is NOTHING to clean up."
                    )
                return (
                    f"Error: one-shot debug command failed with exit_code={_exit}.\n"
                    f"[debug-pod-meta: {meta_tag}]\n"
                    f"The debug pod has been removed.{_logs_section}"
                )
            # Budget expired before termination — leave the pod for follow-up.
            meta_payload = {
                **metadata,
                "name": metadata.get("name") or _debug_pod,
                "namespace": _debug_ns,
                "ready": False,
                "cleaned": cleaned,
                "debug_profile": _profile,
                "oneshot": True,
            }
            meta_tag = json.dumps(meta_payload, ensure_ascii=True, separators=(",", ":"))
            return (
                f"Error: {terminal_error}.\n"
                f"[debug-pod-meta: {meta_tag}]\n"
                + (
                    "The still-running pod was cleaned up automatically."
                    if cleaned
                    else f"Cleanup failed; delete pod {_debug_pod} -n {_debug_ns} when done."
                )
            )

        # ---- INTERACTIVE mode (`debug ... -- sleep N` style, or no `--`):
        # the pod must become Ready so the caller can exec into it.
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
        # _profile was extracted above the oneshot branch; debug-pod-meta
        # carries it for carrier resolution and diagnostics.
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
    """READ-ONLY kubectl — the observation tool for every read-only phase
    (intent/planning/verification).

    Read-only BY ENFORCEMENT: read verbs always; ``exec``/``debug`` only
    for read-only probes; mutating inner commands are REJECTED — fault
    INJECTION is Phase 2.

    When to use:
      - Read-only inspection: `get`/`describe`/`top`/`logs`/
        `api-resources`/`explain`/`auth can-i`.
      - Read-only probes inside a pod (`exec`); pipes via read-only
        ``sh -c``.
      - Node host fs/kernel: ``debug node/<node> --image=<cluster-image>
        -- sleep 60`` then exec into it (paths ``/host/...``); allowed in
        EVERY read-only phase, auto-cleaned at phase end. In PLANNING,
        verify the image carries your plan's binaries BEFORE committing.

    Inputs:
      - subcommand: Literal-enforced (see signature).
      - v_args: same shape as the full ``kubectl`` tool.
      - kubeconfig/context/cluster: optional overrides.

    Output: same as the full ``kubectl`` tool (stdout / "Error: ...").

    Side effects: none on cluster state; `debug` creates an ephemeral
        probe Pod, auto-cleaned at phase end.

    Constraints:
      - No bare shell operators (shell=False); ``>``, ``;``, ``&&``, ``&``
        rejected.
      - exec: no ``-l/--selector`` (resolve the pod via `get`); no
        ``-it``; SHORT keep-alive for debug (``-- sleep 60``).
      - Unknown-flag error → ``--help`` in v_args rather than guessing.
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
