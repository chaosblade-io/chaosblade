"""kubectl CLI tool wrapper for LangGraph @tool function.

Unified kubectl tool that supports all subcommands via a single entry point.
Tool signature faithfully maps kubectl global flags so the LLM can naturally
pass --kubeconfig, --context, --cluster etc. when needed.

Two flavours bound at the graph layer:
  - ``kubectl`` (this module) — full surface (exec, delete, patch, ...);
    used in phase 2 / verifier where mutation is expected.
  - ``kubectl_ro`` (this module) — read-only subset (get/describe/top/
    logs/...); used in phase 1 planning. Constrains the subcommand at
    the tool signature level so the LLM cannot accidentally call a
    mutating verb in the planning phase.
"""

import logging
import os
import re
import shlex
from typing import Literal

from langchain_core.tools import tool

from chaos_agent.config.settings import settings
from chaos_agent.tools.guard import CommandResult
from chaos_agent.tools.shell import run_command

logger = logging.getLogger(__name__)


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
) -> list[str]:
    """Unified kubectl command builder.

    kubeconfig mode: [kubectl, --kubeconfig, ..., subcommand, ...args]
    kubewiz mode: [wiz, task, exec, --command, "kubectl subcommand args", --cluster-uuid, ..., --profile, ...]

    In kubewiz mode, the kubectl portion is baked into a single --command string.
    Callers must pass ALL kubectl arguments at call time; appending to the
    returned list will NOT add args to the kubectl command.
    """
    if isinstance(v_args, str) and v_args:
        args_list = _split_args(v_args)
    elif isinstance(v_args, list):
        args_list = v_args
    else:
        args_list = []

    if settings.kube_connection_mode == "kubewiz":
        kubectl_parts = ["kubectl", subcommand] + args_list
        kubectl_cmd_str = " ".join(shlex.quote(p) for p in kubectl_parts)
        cmd = [
            settings.wiz_path, "task", "exec",
            "--command", kubectl_cmd_str,
            "--cluster-uuid", settings.kubewiz_cluster_uuid,
            "--profile", settings.kubewiz_profile,
        ]
        return cmd

    cmd = [settings.kubectl_path]
    cmd.extend(_build_kubectl_global_args(kubeconfig, context, cluster))
    cmd.append(subcommand)
    cmd.extend(args_list)
    return cmd


def _adapt_kubewiz_result(result: CommandResult) -> CommandResult:
    """Parse wiz stdout protocol and return corrected CommandResult.

    Wiz protocol:
    - wiz exit_code=0: stdout first line is 'exit_code: N', rest is kubectl output
    - wiz exit_code!=0: wiz itself failed, stderr has error message

    Returns CommandResult with kubectl's real exit_code and clean stdout.
    """
    if settings.kube_connection_mode != "kubewiz":
        return result

    # wiz 自身异常
    if result.exit_code != 0:
        return result  # stderr 已包含 wiz 错误信息

    # wiz 正常，解析 stdout 第一行 exit_code: N
    stdout = result.stdout
    lines = stdout.split('\n', 1)

    if not lines or not lines[0].startswith("exit_code:"):
        # 协议违规：stdout 必须以 exit_code: 开头
        return CommandResult(
            exit_code=1,
            stdout="",
            stderr=f"wiz protocol error: stdout missing exit_code prefix. raw={stdout[:200]}",
            duration_ms=result.duration_ms,
        )

    try:
        real_exit_code = int(lines[0].split(':', 1)[1].strip())
    except (ValueError, IndexError):
        real_exit_code = 1
    clean_stdout = lines[1] if len(lines) > 1 else ""

    return CommandResult(
        exit_code=real_exit_code,
        stdout=clean_stdout,
        stderr=result.stderr,
        duration_ms=result.duration_ms,
    )


async def exec_kubectl_raw(
    subcommand: str,
    v_args: "list[str] | str" = "",
    kubeconfig: str = "",
    context: str = "",
    cluster: str = "",
    timeout: float = 30.0,
) -> CommandResult:
    """Execute kubectl with kubewiz protocol handling (no audit overhead).

    Layer 1: protocol translation only.
    - Builds correct command (with shlex.quote in kubewiz mode)
    - Runs subprocess directly
    - Parses wiz output protocol
    - Returns clean CommandResult

    Use this for lightweight internal checks (preflight, env_info, safety_check).
    For LLM-driven tool calls, use _kubectl_impl() which adds audit/guard.
    """
    import asyncio as _asyncio

    cmd = build_kubectl_cmd(subcommand, v_args, kubeconfig, context, cluster)

    try:
        proc = await _asyncio.create_subprocess_exec(
            *cmd,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await _asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except _asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return CommandResult(exit_code=-1, stdout="", stderr=f"timed out after {timeout}s")
    except FileNotFoundError:
        return CommandResult(exit_code=-1, stdout="", stderr="kubectl/wiz not found")

    raw_result = CommandResult(
        exit_code=proc.returncode or 0,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
    )
    return _adapt_kubewiz_result(raw_result)


def display_cmd(cmd: list[str]) -> str:
    """Return a human/LLM-facing command string.

    In kubewiz mode the actual cmd is [wiz, task, exec, --command, "kubectl ..."].
    This function extracts the kubectl portion so callers never expose wiz internals.
    In kubeconfig mode it simply joins the list.
    """
    if (
        settings.kube_connection_mode == "kubewiz"
        and len(cmd) >= 5
        and cmd[1:3] == ["task", "exec"]
    ):
        try:
            idx = cmd.index("--command")
            return cmd[idx + 1]
        except (ValueError, IndexError):
            pass
    return " ".join(cmd)


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

    Mutating: supports exec / delete / patch / apply / scale / taint /
    cordon / drain / rollout / debug / edit / replace and more. ChaosBlade-
    aware: auto-injects ``--timeout`` for ``exec ... blade create``.

    NOT available in Phase 1 (planning); use ``kubectl_ro`` there for
    read-only inspection.

    Single entry point covering all kubectl subcommands. Pick `subcommand` and
    pass the rest of the CLI args as `v_args`.

    When to use:
      - Cluster inspection in any phase (get / describe / top / logs).
      - Phase 2 mutation (delete / patch / set / scale / cordon / uncordon / taint).
      - Verification probing inside containers or on nodes (exec / debug).
      - Creating non-workload resources (PV / PVC / Secret / ConfigMap):
        use ``subcommand="apply"`` with ``v_args="-f -"`` and pass the
        YAML via ``stdin_data``. Workload resources (Deployment, Pod, Job,
        etc.) are blocked.
      - Do NOT use ``exec ... | kubectl apply`` or ``exec ... kubectl create``
        to create resources — this causes namespace drift and will be rejected.

    Inputs:
      - subcommand: one of {get, describe, top, logs, exec, delete, patch, set,
                            scale, cordon, uncordon, taint, debug, apply}.
      - v_args: subcommand arguments as a single shell-quoted string. Examples:
          get      → "pods -n <ns> -o json"
                     "pods -n <ns> -l app=nginx --field-selector=status.phase=Pending"
                     "events -n <ns> --sort-by=.lastTimestamp"
          describe → "pod <pod> -n <ns>"   |   "node <node>"
          top      → "pod -n <ns> --sort-by=cpu"   |   "node <node>"
          logs     → "<pod> -n <ns> --tail=50 --previous -c <container>"
          exec     → "<pod> -n <ns> -- <cmd>"
                     "<pod> -n chaosblade -- blade create k8s pod-cpu fullload --cpu-percent 80"
          debug    → "node/<node> --image=busybox -- sleep 3600"  (then exec into the debug pod)
          delete   → "pod <pod> -n <ns> --force --grace-period=0"
          patch    → "pod <pod> -n <ns> --type=json -p '[{\\"op\\":\\"add\\",\\"path\\":\\"/metadata/labels/x\\",\\"value\\":\\"y\\"}]'"
          scale    → "deployment <name> -n <ns> --replicas=0"
          taint    → "nodes <node> key=value:NoSchedule"   |   "nodes <node> key-"
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
      - delete / patch / set / scale / cordon / uncordon / taint / debug: mutate
        cluster state. Treat as Phase 2 actions and verify aftermath.

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
    """Shared kubectl execution logic used by both kubectl and kubectl_ro."""
    # kubewiz mode does not support stdin piping
    if settings.kube_connection_mode == "kubewiz" and stdin_data:
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

    # Auto-inject/boost --timeout for kubectl exec blade create commands.
    # Must happen BEFORE build_kubectl_cmd (kubewiz mode bakes args into a string).
    if subcommand == "exec" and v_args and re.search(r"\bblade\s+create\b", v_args):
        _fault_match = re.search(
            r"blade\s+create\s+k8s\s+(pod|node|container)-(\w+)\s+(\w+)", v_args
        )
        _scope, _target, _action = (
            (_fault_match.group(1), _fault_match.group(2), _fault_match.group(3))
            if _fault_match else (None, None, None)
        )
        from chaos_agent.utils.fault_type import ensure_min_duration
        if "--timeout" not in v_args:
            effective_timeout = ensure_min_duration(None, _scope, _target, _action)
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
                    _effective = ensure_min_duration(_current_val, _scope, _target, _action)
                    if _effective != int(_current_val):
                        for i, token in enumerate(processed_args):
                            if token == "--timeout" and i + 1 < len(processed_args) and processed_args[i + 1] == _current_val:
                                processed_args[i + 1] = str(_effective)
                                logger.info(
                                    f"Auto-boosted --timeout from {_current_val}s to {_effective}s "
                                    f"for {_scope}-{_target}-{_action} (recommended minimum)"
                                )
                                break
            except (ValueError, TypeError):
                pass

    cmd = build_kubectl_cmd(subcommand, processed_args, kubeconfig, context, cluster)

    # exec/debug subcommands use longer timeout (container commands may be slow;
    # debug needs to pull images and create ephemeral containers)
    timeout = settings.timeout_kubectl_exec if subcommand in ("exec", "debug") else settings.timeout_kubectl

    try:
        result = await run_command(cmd, timeout=timeout, stdin_data=stdin_data)
    except Exception as e:
        return f"Error: kubectl {subcommand} failed: {e}"

    result = _adapt_kubewiz_result(result)

    if result.exit_code != 0:
        # kubewiz 模式下错误信息在 stdout，直接模式在 stderr
        error_detail = result.stderr or result.stdout
        return f"Error: kubectl {subcommand} failed: {error_detail}"

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

    # Debug pod cleanup hint — ephemeral debug containers should be deleted after use
    if subcommand == "debug":
        # Extract the namespace used for this debug pod (needed by cleanup scanner)
        _ns_match = re.search(r'(?:-n\s+|--namespace[=\s])(\S+)', v_args)
        _debug_ns = _ns_match.group(1) if _ns_match else "default"
        output += (
            f"\n\n[debug-pod-ns: {_debug_ns}]"
            "\n💡 The debug container is ephemeral. "
            "If you are done with debugging, clean up with: "
            f"kubectl(subcommand='delete', v_args='pod <debug-pod-name> -n {_debug_ns}'). "
            "The debug pod name usually starts with the target node/pod name followed by '-debug'."
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
PHASE1_READONLY_SUBCOMMANDS: tuple[str, ...] = (
    "get", "describe", "top", "logs",
    "version", "cluster-info", "api-resources", "explain", "auth",
)


@tool
async def kubectl_ro(
    subcommand: Literal[
        "get", "describe", "top", "logs",
        "version", "cluster-info", "api-resources", "explain", "auth",
    ],
    v_args: str = "",
    kubeconfig: str = "",
    context: str = "",
    cluster: str = "",
) -> str:
    """Phase 1 ONLY. Read-only kubectl for planning observations.

    When to use (Phase 1 = planning, observation only):
      - Verify the target Pod / Node / Namespace exists (`get`, `describe`).
      - Inspect labels / status / events (`describe`, `get -o json`).
      - Capture baseline metrics (`top`).
      - Read application logs for symptom evidence (`logs`).
      - Discover available API resources (`api-resources`, `explain`).
      - Check effective permissions (`auth can-i`).

    Self-help (IMPORTANT — use this instead of guessing from memory):
      - Pass `--help` or `-h` in v_args to see the real usage of any subcommand.
        Example: kubectl_ro(subcommand="get", v_args="--help")
      - This returns the live kubectl help text, which is ALWAYS more accurate
        than documentation, skill instructions, or knowledge resources.
      - When a command fails with an unknown flag or argument error, call
        `--help` BEFORE retrying — do NOT guess from prior context.

    Constraints:
      - This tool ONLY accepts the read-only subcommands above. Any
        attempt to call ``exec``, ``delete``, ``patch``, ``apply``,
        ``scale``, ``taint``, ``cordon``, ``drain``, ``rollout``,
        ``debug``, ``edit``, ``replace``, etc. is REJECTED at the
        argument-validation layer (Pydantic ``Literal`` enforcement).
      - For mutating operations, the full ``kubectl`` tool is bound
        automatically in Phase 2 after the user approves your plan —
        you don't need them here. Just verify the target and assess
        blast radius.

    Inputs / Output: same shape as the full ``kubectl`` tool. This is a
    thin wrapper that re-uses ``kubectl``'s execution logic with the
    subcommand domain constrained.
    """
    # Belt-and-braces defense: even if Literal validation is bypassed
    # (e.g. some future schema serialization quirk), runtime check
    # rejects mutating subcommands. Returns a structured error that
    # the Layer D handler-style message mirrors.
    if subcommand not in PHASE1_READONLY_SUBCOMMANDS:
        return (
            f"Error: kubectl_ro does not accept subcommand '{subcommand}'.\n"
            f"Phase 1 (planning) is read-only by enforcement. Allowed "
            f"subcommands: {', '.join(PHASE1_READONLY_SUBCOMMANDS)}.\n"
            f"Mutation subcommands (exec/delete/patch/apply/scale/...) "
            f"are bound in Phase 2 after your plan is approved."
        )
    # Call the shared implementation directly — NOT kubectl.ainvoke(),
    # which would emit a nested on_tool_start event causing the TUI to
    # render a duplicate tool card.
    return await _kubectl_impl(subcommand, v_args, kubeconfig, context, cluster)


VERIFIER_SUBCOMMANDS: tuple[str, ...] = (
    "get", "describe", "top", "logs",
    "version", "cluster-info", "api-resources", "explain", "auth",
    "exec", "debug",
)


@tool
async def kubectl_verify(
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
    """Verifier phase ONLY. Observation kubectl with ``exec`` for in-pod probing.

    When to use (verification = observe fault effects AFTER injection):
      - Check pod / node / endpoint state changes (`get`, `describe`).
      - Compare current metrics against pre-injection baseline (`top`).
      - Read application logs for error evidence (`logs`).
      - Probe inside containers for fault symptoms (`exec`):
        DNS resolution: ``exec <pod> -n <ns> -- nslookup <service>``
        HTTP health:    ``exec <pod> -n <ns> -- wget -qO- --timeout=3 <url>``
        Process state:  ``exec <pod> -n <ns> -- ps aux``
        Network:        ``exec <pod> -n <ns> -- ping -c 3 <target>``
      - Probe host filesystem / kernel state on a node (`debug`):
        ``debug node/<node> --image=busybox -- sleep 3600`` then
        ``exec <debug-pod> -n default -- cat /host/proc/loadavg``.
        Host paths inside the debug pod live under ``/host/...``.
        Always pass ``-- sleep 3600`` (or another keep-alive); never ``-it``.
        Clean up the debug pod with ``delete`` is NOT possible from this
        tool (no ``delete`` here) — the framework's verifier finalization
        scans message history and removes the debug pod automatically.

    Self-help (IMPORTANT — use this instead of guessing from memory):
      - Pass `--help` or `-h` in v_args to see the real usage of any subcommand.
        Example: kubectl_verify(subcommand="get", v_args="--help")
      - This returns the live kubectl help text, which is ALWAYS more accurate
        than documentation, skill instructions, or knowledge resources.
      - When a command fails with an unknown flag or argument error, call
        `--help` BEFORE retrying — do NOT guess from prior context.

    Constraints:
      - This tool accepts read-only subcommands plus ``exec`` (in-pod
        observation) and ``debug`` (host-level observation via an
        ephemeral debug pod). Any attempt to call ``scale``, ``delete``,
        ``patch``, ``apply``, ``cordon``, ``drain``, ``taint``,
        ``rollout``, ``edit``, ``replace``, ``create``, ``run``, etc. is
        REJECTED. The verifier's job is to OBSERVE fault effects, not
        to inject faults — injection is done in the execution phase.
      - ``kubectl exec`` constraints: no shell features (`|`, `;`, `&&`),
        no `-l/--selector` (resolve pod name first via `get`), no `-it`
        (non-interactive context).

    Inputs / Output: same shape as the full ``kubectl`` tool. This is a
    thin wrapper that re-uses ``kubectl``'s execution logic with the
    subcommand domain constrained.
    """
    if subcommand not in VERIFIER_SUBCOMMANDS:
        return (
            f"Error: kubectl_verify does not accept subcommand '{subcommand}'.\n"
            f"The verifier phase is observation-only. Allowed "
            f"subcommands: {', '.join(VERIFIER_SUBCOMMANDS)}.\n"
            f"Mutation subcommands (scale/delete/patch/...) are only "
            f"available in the execution phase."
        )
    return await _kubectl_impl(subcommand, v_args, kubeconfig, context, cluster)
