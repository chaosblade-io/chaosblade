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
import re
import shlex
from typing import Literal

from langchain_core.tools import tool

from chaos_agent.config.settings import settings
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
        args.extend(["--kubeconfig", kc])

    # --context: tool param > settings fallback
    ctx = context or settings.kube_context
    if ctx:
        args.extend(["--context", ctx])

    # --cluster
    if cluster:
        args.extend(["--cluster", cluster])

    return args


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
      - Do NOT use `apply`, `create`, `replace`, `edit`, `expose`, `run`,
        `autoscale`, `rollout` — those create/mutate workloads outside the
        chaos scope and are blocked by ToolGuard.

    Inputs:
      - subcommand: one of {get, describe, top, logs, exec, delete, patch, set,
                            scale, cordon, uncordon, taint, debug}.
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
        See knowledge resource `kubectl-recipes.md` for the long-tail catalogue.
      - kubeconfig / context / cluster: optional overrides
        (do NOT embed --kubeconfig in v_args — it is auto-stripped).

    Output: stdout from kubectl, or an "Error: ..." string on non-zero exit.
            Large `get -o json` output gets a "⚠️ LARGE_OUTPUT" hint footer.
            Empty `get -l ...` output gets a label-discovery hint footer.

    Side effects:
      - get / describe / top / logs / exec (read-only commands inside containers): none.
      - delete / patch / set / scale / cordon / uncordon / taint / debug: mutate
        cluster state. Treat as Phase 2 actions and verify aftermath.

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
    cmd = [settings.kubectl_path]
    cmd.extend(_build_kubectl_global_args(kubeconfig, context, cluster))
    cmd.append(subcommand)
    if v_args:
        # Defensive: strip --kubeconfig embedded in v_args by LLM mistake.
        # kubeconfig should be passed via the dedicated 'kubeconfig' parameter.
        if "--kubeconfig" in v_args:
            v_args = re.sub(r"--kubeconfig\s+\S+", "", v_args).strip()
            logger.warning(
                "kubeconfig should be passed via dedicated 'kubeconfig' parameter, "
                "not embedded in v_args. The embedded value has been removed."
            )

        # Validate exec subcommand: reject -l/--selector (not supported by kubectl exec)
        selector_removed = False
        if subcommand == "exec":
            selector_pattern = re.compile(r"(?:^|\s)(-l|--selector)\s+\S+")
            if selector_pattern.search(v_args):
                v_args = selector_pattern.sub("", v_args).strip()
                selector_removed = True
                logger.warning(
                    "kubectl exec does not support -l/--selector. "
                    "Removed from v_args. Use kubectl get to discover the pod name first."
                )

        cmd.extend(_split_args(v_args))

    # Auto-inject/boost --timeout for kubectl exec blade create commands.
    # When the LLM falls back to kubectl exec to run blade create (bypassing the
    # blade_create tool's auto-timeout logic), we must ensure --timeout is present
    # AND meets the minimum recommended duration.  This mirrors blade_create's
    # auto-injection/boost logic (blade.py).
    # IMPORTANT: Must match "blade create" as a contiguous token sequence, not
    # "blade" + "create" separately (e.g., "blade status --type create" would
    # be a false positive and --timeout is invalid for blade status).
    if subcommand == "exec" and v_args and re.search(r"\bblade\s+create\b", v_args):
        # Extract scope/target/action from "blade create k8s <scope>-<target> <action>"
        _fault_match = re.search(
            r"blade\s+create\s+k8s\s+(pod|node|container)-(\w+)\s+(\w+)", v_args
        )
        _scope, _target, _action = (
            (_fault_match.group(1), _fault_match.group(2), _fault_match.group(3))
            if _fault_match else (None, None, None)
        )
        from chaos_agent.utils.fault_type import ensure_min_duration
        if "--timeout" not in v_args:
            # No timeout specified: auto-inject recommended minimum
            effective_timeout = ensure_min_duration(None, _scope, _target, _action)
            cmd.extend(["--timeout", str(effective_timeout)])
            logger.info(
                f"Auto-injected --timeout {effective_timeout}s into "
                f"kubectl exec blade create command"
            )
        else:
            # Timeout specified: check if it meets the minimum
            try:
                _timeout_match = re.search(r"--timeout\s+(\d+)", v_args)
                if _timeout_match:
                    _current_val = _timeout_match.group(1)
                    _effective = ensure_min_duration(_current_val, _scope, _target, _action)
                    if _effective != int(_current_val):
                        # Replace in cmd list (which was built from _split_args)
                        for i, token in enumerate(cmd):
                            if token == "--timeout" and i + 1 < len(cmd) and cmd[i + 1] == _current_val:
                                cmd[i + 1] = str(_effective)
                                logger.info(
                                    f"Auto-boosted --timeout from {_current_val}s to {_effective}s "
                                    f"for {_scope}-{_target}-{_action} (recommended minimum)"
                                )
                                break
            except (ValueError, TypeError):
                pass

    # exec/debug subcommands use longer timeout (container commands may be slow;
    # debug needs to pull images and create ephemeral containers)
    timeout = settings.timeout_kubectl_exec if subcommand in ("exec", "debug") else settings.timeout_kubectl

    try:
        result = await run_command(cmd, timeout=timeout)
    except Exception as e:
        return f"Error: kubectl {subcommand} failed: {e}"

    if result.exit_code != 0:
        return f"Error: kubectl {subcommand} failed: {result.stderr}"

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
        output += (
            "\n\n💡 The debug container is ephemeral. "
            "If you are done with debugging, clean up with: "
            "kubectl(subcommand='delete', v_args='pod <debug-pod-name> -n <ns>'). "
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
    # Delegate to the full kubectl tool, which already handles all the
    # global args, output formatting, large-output hints, etc.
    return await kubectl.ainvoke({
        "subcommand": subcommand,
        "v_args": v_args,
        "kubeconfig": kubeconfig,
        "context": context,
        "cluster": cluster,
    })
