"""Shared injection detection utilities for verifier and recover_verifier.

Provides precise detection of kubectl-exec-based ChaosBlade injection by
cross-referencing ToolMessage responses with the original AIMessage tool_calls,
verifying subcommand='exec' and blade command in v_args.

Also detects kubectl-native injection methods (scale, patch, cordon, taint, set)
used as alternatives when blade_create fails on the host.
"""

import json
import logging
import re

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.providers._detection import (
    build_tool_call_args_lookup as _build_tool_call_args_lookup,
)
from chaos_agent.agent.providers._detection import (
    scan_kubectl_blade_success as _scan_kubectl_blade_success,
)
from chaos_agent.agent.providers._detection import (
    scan_kubectl_injection_after_blade as _scan_kubectl_injection_after_blade,
)
from chaos_agent.agent.providers.chaosblade_python import (
    ChaosbladePythonProvider as _ChaosbladePythonProvider,
)
from chaos_agent.agent.providers.host_shell import (
    HostShellProvider as _HostShellProvider,
)
from chaos_agent.agent.providers.k8s_native import (
    K8sNativeProvider as _K8sNativeProvider,
)

logger = logging.getLogger(__name__)

# kubectl subcommands that can perform fault injection (non-ChaosBlade methods).
# Single source of truth is ``K8sNativeProvider.inject_kubectl_subcommands`` —
# this read-through alias keeps the module-level name for existing callers
# while the provider owns the vocabulary.
_KUBECTL_INJECT_SUBCOMMANDS = _K8sNativeProvider.inject_kubectl_subcommands

# Raw-shell injection tool names (host-native carrier). Single source of truth
# is ``HostShellProvider.inject_tool_names``.
_HOST_NATIVE_INJECT_TOOLS = _HostShellProvider.inject_tool_names

# Host injection binaries (action vocabulary for the host step self-check).
# Single source of truth is ``HostShellProvider.injection_binaries``.
_HOST_NATIVE_INJECT_BINARIES = _HostShellProvider.injection_binaries


def classify_issue_time_method(
    tool_name: str, tool_args: dict, *, is_host: bool
) -> str | None:
    """Map a SINGLE freshly-issued tool_call to the injection_method it enacts.

    Direction B: ``injection_method`` is recorded at the moment the injection is
    ISSUED (from the AIMessage tool_call), not reverse-reconstructed from the
    (possibly severed / truncated) message history later. Every shape is
    deterministic from the tool name + subcommand + channel EXCEPT a
    ``kubectl exec``/``debug`` inner command, whose read/mutate judgement is
    delegated to the same fail-safe classifier the reverse scan uses
    (:func:`~chaos_agent.agent.providers.k8s_native._exec_inner_command_mutates`).

    Returns the method, or ``None`` when the call is not an injection (read-only
    probe, verification, or an unrelated tool). The caller decides the
    commit policy: native methods (``kubectl_native`` / ``host_native``) have no
    experiment UID so the attempt IS the injection and can be recorded at issue
    time; the experiment methods (``host_blade`` / ``kubectl_exec``) are
    classified here for completeness but the execute node defers committing them
    to the ``blade_uid`` path (proof the ChaosBlade experiment succeeded), so a
    failed blade attempt followed by a kubectl-native fallback is not
    mis-recorded.
    """
    from chaos_agent.agent.providers.k8s_native import _exec_inner_command_mutates

    if not isinstance(tool_args, dict):
        return None

    # ChaosBlade experiment carrier via the dedicated blade tool.
    if tool_name == "blade_create":
        return "host_blade"

    # ChaosBlade Python-agent carrier: an in-process application fault issued
    # through its own tool. Like the blade methods above it produces an
    # experiment UID, so the execute node still defers the COMMIT to the
    # ``blade_uid`` path; classifying it here keeps issue-time attribution
    # complete (and stops the method reading as "no injection issued").
    if tool_name in _ChaosbladePythonProvider.inject_tool_names:
        return "python_agent"

    if tool_name == "kubectl":
        subcommand = tool_args.get("subcommand", "")
        v_args = tool_args.get("v_args", "") or ""
        # kubectl exec ... blade create → ChaosBlade delivered through a pod.
        if (
            subcommand in _K8sNativeProvider.inject_command_subcommands
            and isinstance(v_args, str)
            and "blade" in v_args
            and "create" in v_args
        ):
            return "kubectl_exec"
        # Object-write verb — the verb itself IS the mutation.
        if subcommand in _KUBECTL_INJECT_SUBCOMMANDS:
            return "kubectl_native"
        # Command-mode exec/debug — injection only when the inner command mutates.
        if (
            subcommand in _K8sNativeProvider.inject_command_subcommands
            and isinstance(v_args, str)
            and _exec_inner_command_mutates(v_args)
        ):
            return "kubectl_native"
        return None

    # Host raw-shell carrier (only meaningful on a resolved host channel).
    if is_host and tool_name in _HOST_NATIVE_INJECT_TOOLS:
        return "host_native"

    return None

# Label selector for ChaosBlade tool pods
_TOOL_POD_LABEL_SELECTOR = "app=otel-c-tool"
_TOOL_POD_NAMESPACE = "chaosblade"

# Known tool pod label selectors (tried in order)
_TOOL_POD_LABEL_CANDIDATES = ["app=chaosblade-tool", "app=otel-c-tool"]


def _was_kubectl_blade_injection_successful(messages: list) -> bool:
    """Check if kubectl exec was used to successfully inject a ChaosBlade experiment.

    Thin wrapper over
    :func:`chaos_agent.agent.providers._detection.scan_kubectl_blade_success`
    (the single source of the scan logic). Kept as a module-level name for
    existing callers (verifier layers, ``_was_blade_create_attempted``).
    """
    return _scan_kubectl_blade_success(messages)


def _was_kubectl_injection_attempted(messages: list) -> bool:
    """Check if kubectl write operations were used for fault injection.

    Thin wrapper over
    :func:`chaos_agent.agent.providers._detection.scan_kubectl_injection_after_blade`,
    passing the provider-owned subcommand vocabulary
    :data:`_KUBECTL_INJECT_SUBCOMMANDS`. Kept as a module-level name for
    existing callers.
    """
    return _scan_kubectl_injection_after_blade(messages, _KUBECTL_INJECT_SUBCOMMANDS)


def _was_blade_create_attempted(messages: list) -> bool:
    """Check if ChaosBlade injection was attempted but ultimately failed.

    Returns False (not "attempted-and-failed") if:
      - kubectl exec successfully injected a blade experiment (bypassing blade_create)
      - kubectl-native injection was used as an alternative after blade_create failed
    Returns True only if blade_create was called AND no successful injection
    was detected via any method.

    This distinguishes two scenarios when blade_uid is empty:
      - True:  ChaosBlade injection was attempted but failed → Layer 1 returns "failed"
      - False: Non-ChaosBlade fault, OR kubectl-based injection succeeded → Layer 1 returns "skipped"
    """
    # If kubectl-based blade injection succeeded, injection was NOT "attempted and failed"
    if _was_kubectl_blade_injection_successful(messages):
        return False

    # If kubectl-native injection was used as alternative after blade_create
    # failed, treat as non-ChaosBlade fault (Layer 1 = "skipped")
    if _was_kubectl_injection_attempted(messages):
        return False

    for msg in messages:
        if isinstance(msg, ToolMessage) and getattr(msg, "name", "") == "blade_create":
            return True
    return False


def _parse_all_ns_pods(output: str) -> list[tuple[str, str]]:
    """Parse kubectl get pods -A --no-headers output.

    Format: NAMESPACE  NAME  READY  STATUS  RESTARTS  AGE

    Returns:
        List of (pod_name, namespace) tuples for Running pods.
    """
    if not output or not isinstance(output, str):
        return []

    result: list[tuple[str, str]] = []
    for line in output.strip().splitlines():
        parts = line.split()
        if len(parts) >= 4:
            namespace = parts[0]
            pod_name = parts[1]
            status = parts[3]
            if status == "Running":
                result.append((pod_name, namespace))
    return result


def _parse_all_ns_pods_wide(output: str) -> list[tuple[str, str, str]]:
    """Parse kubectl get pods -A --no-headers -o wide output.

    Wide format columns: NAMESPACE  NAME  READY  STATUS  RESTARTS  AGE  IP  NODE  ...
    Returns: List of (pod_name, namespace, node_name) tuples for Running pods.
    """
    if not output or not isinstance(output, str):
        return []
    result: list[tuple[str, str, str]] = []
    for line in output.strip().splitlines():
        parts = line.split()
        if len(parts) >= 8:  # Wide format has at least 8 columns
            namespace = parts[0]
            pod_name = parts[1]
            status = parts[3]
            node_name = parts[7]
            if status == "Running":
                result.append((pod_name, namespace, node_name))
    return result


async def discover_tool_pod_on_node(
    node_name: str, kubeconfig: str, task_id: str = "",
) -> tuple[str, str] | None:
    """Find a Running ChaosBlade tool pod on the specified node (cluster-wide).

    Tries known label selectors in order with -A (all-namespaces) and -o wide
    to match pods by their hosting node.

    Returns:
        (pod_name, namespace) tuple if found, None otherwise.
    """
    from chaos_agent.transports import (
        PROFILE_K8S,
        TransportTarget,
        execute_via_transport,
    )
    from chaos_agent.tools.kubectl import build_kubectl_cmd
    from chaos_agent.config.settings import settings

    _target = TransportTarget.from_state({})
    for label in _TOOL_POD_LABEL_CANDIDATES:
        cmd = build_kubectl_cmd("get", [
            "pods", "-A", "-l", label, "--no-headers", "-o", "wide",
        ], kubeconfig=kubeconfig)
        try:
            result = await execute_via_transport(
                cmd, _target,
                timeout=settings.timeout_kubectl,
                task_id=task_id,
                source="baseline-capture",
                expect_profile=PROFILE_K8S,
            )
        except Exception as e:
            logger.warning(
                "Failed to discover tool pods on node %s with label %s: %s",
                node_name, label, e,
            )
            continue
        pods = _parse_all_ns_pods_wide(result.stdout)
        for pod_name, ns, node in pods:
            if node == node_name:
                return (pod_name, ns)
    return None


async def discover_tool_pods_cluster_wide(
    kubeconfig: str, task_id: str = "",
) -> list[tuple[str, str]]:
    """Discover ChaosBlade tool pods across all namespaces.

    Tries known label selectors in order, returns on first success.
    Uses -A (all-namespaces) to avoid hardcoding the namespace.

    Returns:
        List of (pod_name, namespace) tuples for Running pods.
    """
    from chaos_agent.transports import (
        PROFILE_K8S,
        TransportTarget,
        execute_via_transport,
    )
    from chaos_agent.tools.kubectl import build_kubectl_cmd
    from chaos_agent.config.settings import settings

    _target = TransportTarget.from_state({})
    for label in _TOOL_POD_LABEL_CANDIDATES:
        cmd = build_kubectl_cmd("get", [
            "pods", "-A", "-l", label, "--no-headers",
        ], kubeconfig=kubeconfig)
        result = await execute_via_transport(
            cmd, _target,
            timeout=settings.timeout_kubectl,
            task_id=task_id,
            source="conflict-check",
            expect_profile=PROFILE_K8S,
        )
        pods = _parse_all_ns_pods(result.stdout)
        if pods:
            return pods
    return []


async def discover_tool_pods_cluster_wide_with_nodes(
    kubeconfig: str, task_id: str = "",
) -> list[tuple[str, str, str]]:
    """Discover ChaosBlade tool pods across all namespaces with node info.

    Tries known label selectors in order, returns on first success.
    Uses -A (all-namespaces) and -o wide to include node placement.

    Returns:
        List of (pod_name, namespace, node_name) tuples for Running pods.
    """
    from chaos_agent.transports import (
        PROFILE_K8S,
        TransportTarget,
        execute_via_transport,
    )
    from chaos_agent.tools.kubectl import build_kubectl_cmd
    from chaos_agent.config.settings import settings

    _target = TransportTarget.from_state({})
    for label in _TOOL_POD_LABEL_CANDIDATES:
        cmd = build_kubectl_cmd("get", [
            "pods", "-A", "-l", label, "--no-headers", "-o", "wide",
        ], kubeconfig=kubeconfig)
        try:
            result = await execute_via_transport(
                cmd, _target,
                timeout=settings.timeout_kubectl,
                task_id=task_id,
                source="tool-pod-discovery",
                expect_profile=PROFILE_K8S,
            )
        except Exception as e:
            logger.warning("Failed to discover tool pods with label %s: %s", label, e)
            continue
        pods = _parse_all_ns_pods_wide(result.stdout)
        if pods:
            return pods
    return []


def _extract_kubectl_exec_pod_name(messages: list) -> str | None:
    """Extract the tool pod name used for kubectl exec blade injection.

    When the LLM injects a fault via `kubectl exec <pod> -n chaosblade -- blade create ...`,
    the pod name is the first token in the v_args field of the AIMessage's tool_calls.

    This function scans messages in reverse to find the most recent kubectl exec
    blade create call that succeeded (ChaosBlade success JSON in ToolMessage),
    then extracts the pod name from the corresponding AIMessage's v_args.

    Returns:
        Pod name string if found, None otherwise.
    """
    lookup = _build_tool_call_args_lookup(messages)

    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") != "kubectl":
            continue
        content = msg.content
        if not isinstance(content, str):
            continue
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue

        # Must be a successful ChaosBlade injection
        if not (isinstance(data, dict)
                and data.get("success") is True
                and data.get("code") == 200
                and isinstance(data.get("result"), str)
                and data["result"]):
            continue

        tc_id = getattr(msg, "tool_call_id", "")
        if tc_id and tc_id in lookup:
            args = lookup[tc_id]
            subcommand = args.get("subcommand", "")
            v_args = args.get("v_args", "") or ""
            if subcommand == "exec" and "blade" in v_args and "create" in v_args:
                pod_name = _parse_pod_name_from_v_args(v_args)
                if pod_name:
                    return pod_name
            continue
        elif tc_id:
            continue

        # No tool_call_id (older session format) — scan AIMessages directly
        pod_name = _find_pod_name_from_aimessages(messages, v_args_hint="blade")
        if pod_name:
            return pod_name

    return None


# Pod name pattern: lowercase alphanumeric with hyphens (Kubernetes naming)
_POD_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _parse_pod_name_from_v_args(v_args: str) -> str | None:
    """Extract the pod name from kubectl exec v_args.

    v_args format: "<pod-name> -n <namespace> -- <command>"
    The pod name is the first positional token (not starting with '-').

    Returns:
        Pod name if valid, None if v_args is empty or first token is a flag.
    """
    if not v_args:
        return None
    tokens = v_args.strip().split()
    if not tokens:
        return None
    first = tokens[0]
    # Reject if the first token looks like a flag
    if first.startswith("-"):
        return None
    # Validate pod name pattern
    if _POD_NAME_RE.match(first):
        return first
    return None


def _find_pod_name_from_aimessages(messages: list, *, v_args_hint: str = "") -> str | None:
    """Fallback: scan AIMessages for kubectl exec blade create tool calls.

    Used when ToolMessage lacks tool_call_id (older session format).
    Returns the pod name from the most recent matching AIMessage.
    """
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in reversed(tool_calls):
            if isinstance(tc, dict):
                name = tc.get("name", "")
                args = tc.get("args", {})
            else:
                name = getattr(tc, "name", "")
                args = getattr(tc, "args", {})
            if name != "kubectl":
                continue
            subcommand = args.get("subcommand", "")
            v_args = args.get("v_args", "") or ""
            if subcommand == "exec" and v_args_hint in v_args and "create" in v_args:
                pod_name = _parse_pod_name_from_v_args(v_args)
                if pod_name:
                    return pod_name
    return None


# ---------------------------------------------------------------------------
# Injection step self-check (heuristic)
# ---------------------------------------------------------------------------


def _extract_drill_steps(skill_case: str) -> list[str]:
    """Extract 演练步骤 from skill case content.

    Returns the text of each numbered step.
    """
    if "演练步骤" not in skill_case:
        return []
    start = skill_case.index("演练步骤")
    remainder = skill_case[start:]
    header_end = remainder.find('\n')
    if header_end < 0:
        return []
    body = remainder[header_end:]
    next_section = re.search(r'\n\*\*[^*]+\*\*', body)
    section = body[:next_section.start()] if next_section else body
    steps = re.findall(r'^\s*\d+\.\s+(.+)', section, re.MULTILINE)
    return [s.split('\n')[0].strip() for s in steps if s.strip()]


# Markers that mean a command NEVER reached the target (pre-execution
# rejection): guard reject, phase-1 read-only enforcement, unknown subcommand,
# arg validation error. Everything else — success, timeout, non-zero exit —
# counts as an ATTEMPTED action. This is the HIGH-TOLERANCE rule: a mutation
# that reached the cluster/host (even if it timed out or failed) is treated as
# "done" for step-skip detection, so an ambiguous timeout no longer produces a
# false "missing step" (the original INCOMPLETE-INJECTION false-positive).
_PRE_EXEC_REJECTION_MARKERS = (
    "[target_guard]",
    "phase1_readonly_violation",
    "does not accept subcommand",
    "validationerror",
    "validation error",
)


def _reached_target(content: object) -> bool:
    """True unless the ToolMessage content is a pre-execution rejection."""
    low = (content if isinstance(content, str) else "").lower()
    return not any(m in low for m in _PRE_EXEC_REJECTION_MARKERS)


# Leading-intent markers for a BASELINE / OBSERVATION step, which merely NAMES
# a verb or binary instead of injecting. Requiring those as injection actions is
# a false positive — most visible on the host side, whose vocabulary is binary
# names that read and write under the SAME name (``systemctl status`` vs
# ``systemctl stop``, ``date`` vs ``date -s``), unlike the write-only kubectl
# verbs. Matched only at the START of the step: a step's opening clause declares
# its purpose, so an action step that merely MENTIONS observation later
# ("删除该节点上的 Pod，观察是否被重建") is correctly kept as an action.
_READONLY_STEP_PREFIXES = (
    "记录",
    "查看",
    "观察",
    "确认",
    "检查",
    "获取",
    "统计",
    "采集",
    "record",
    "observe",
    "inspect",
    "check",
    "verify",
    "baseline",
    "capture",
)


def _injection_intent_steps(steps: list[str]) -> list[str]:
    """Drop baseline / observation steps before extracting REQUIRED actions.

    A read-only step (``确认目标服务当前状态：systemctl status <svc>``) names a
    binary without injecting anything; treating it as a required injection
    action is the host-side analogue of the label-vs-patch false positive.
    Applied to both backends — harmless for kubectl (write-only verbs) and
    decisive for host. Never returns more steps than it was given.

    Only the step's LEADING intent is inspected, so a genuine action step that
    also mentions observing the outcome keeps contributing its verb.
    """
    kept: list[str] = []
    for step in steps:
        head = step.lstrip(" \t-*。.、：:").lower()
        if any(head.startswith(p) for p in _READONLY_STEP_PREFIXES):
            continue
        kept.append(step)
    return kept


def _required_kubectl_verbs(steps: list[str]) -> dict[str, str]:
    """REQUIRED kubectl write verbs mentioned in the drill steps (token -> step)."""
    verbs = _K8sNativeProvider.step_kubectl_verbs
    cmap = _K8sNativeProvider.chinese_verb_map
    required: dict[str, str] = {}
    for step in steps:
        first = step.split('\n')[0].strip()
        lower = step.lower()
        for v in verbs:
            if re.search(rf"\b{re.escape(v)}\b", lower):
                required.setdefault(v, first)
        for cn, en in cmap.items():
            if cn in step:
                required.setdefault(en, first)
    return required


def _patch_equivalent_verbs(v_args: str) -> set[str]:
    """Dedicated verbs a ``kubectl patch`` is semantically equivalent to.

    A drill step may name the dedicated verb (``kubectl label node ...``) while
    the agent achieves the identical mutation via
    ``kubectl patch node -p '{"metadata":{"labels":{...}}}'``. Crediting both
    keeps the self-check from flagging an action that WAS performed. Loose
    field-name match by design: over-crediting only shrinks the missing set,
    matching this module's high-tolerance / under-report bias.
    """
    lower = v_args.lower()
    return {
        verb
        for field, verbs in _K8sNativeProvider.patch_equivalent_verbs.items()
        if field in lower
        for verb in verbs
    }


def _patch_expressible_verbs() -> frozenset[str]:
    """Dedicated verbs that ARE a field patch — executing one credits ``patch``.

    The mirror of :func:`_patch_equivalent_verbs`: a step may be spelled
    ``kubectl patch ...`` while the agent reaches the same state with the
    dedicated verb. Derived from the SAME provider table so both directions
    stay in sync from one source of truth.
    """
    return frozenset(
        verb
        for verbs in _K8sNativeProvider.patch_equivalent_verbs.values()
        for verb in verbs
    )


def _executed_kubectl_verbs(messages: list) -> set[str]:
    """kubectl inject verbs ATTEMPTED (high tolerance: reached-cluster counts,
    incl. timeout / non-zero exit; only pre-exec rejections are excluded).

    Credits ``patch`` ↔ dedicated verb in both directions, so a step documented
    as ``label`` / ``taint`` / ``scale`` is not reported missing when carried out
    via ``patch`` (and a step documented as ``patch`` is not reported missing
    when carried out with the dedicated verb).
    """
    lookup = _build_tool_call_args_lookup(messages)
    executed: set[str] = set()
    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") != "kubectl":
            continue
        if not _reached_target(msg.content):
            continue
        tc_id = getattr(msg, "tool_call_id", "")
        args = lookup.get(tc_id) or {}
        sub = args.get("subcommand", "")
        if sub in _KUBECTL_INJECT_SUBCOMMANDS:
            executed.add(sub)
            if sub == "patch":
                executed |= _patch_equivalent_verbs(
                    str(args.get("v_args", "") or "")
                )
            elif sub in _patch_expressible_verbs():
                executed.add("patch")
    return executed


def _required_host_binaries(steps: list[str]) -> dict[str, str]:
    """REQUIRED host injection binaries mentioned in the drill steps.

    Word-boundary match against ``HostShellProvider.injection_binaries`` so a
    short binary (``dd`` / ``ip`` / ``cp``) does not false-match inside another
    word (``add`` / ``script``). High tolerance = avoid false requirements.
    """
    bins = _HOST_NATIVE_INJECT_BINARIES
    required: dict[str, str] = {}
    for step in steps:
        first = step.split('\n')[0].strip()
        lower = step.lower()
        for b in bins:
            if re.search(rf"\b{re.escape(b)}\b", lower):
                required.setdefault(b, first)
    return required


def _executed_host_binaries(messages: list) -> set[str]:
    """host_inject command binaries ATTEMPTED (high tolerance, same rule)."""
    lookup = _build_tool_call_args_lookup(messages)
    bins = _HOST_NATIVE_INJECT_BINARIES
    executed: set[str] = set()
    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") not in _HOST_NATIVE_INJECT_TOOLS:
            continue
        if not _reached_target(msg.content):
            continue
        tc_id = getattr(msg, "tool_call_id", "")
        cmd = str((lookup.get(tc_id) or {}).get("command", "") or "").lower()
        for b in bins:
            if re.search(rf"\b{re.escape(b)}\b", cmd):
                executed.add(b)
    return executed


def build_injection_step_selfcheck(
    skill_case: str,
    messages: list,
    injection_method: str | None,
) -> str | None:
    """HIGH-TOLERANCE step-skip detection → SOFT, one-shot reminder, or ``None``.

    Third condition of the multi-step self-check (after the ``is_multi_step``
    switch and "scenario has >= 2 drill steps"): a fault-tolerant judgement of
    whether an injection action documented in the skill case looks NOT-yet
    -performed. Because string mapping is imprecise, it errs toward
    UNDER-reporting (executed counts any attempted action incl. timeouts; only
    genuinely-absent actions are flagged) and the returned message is a soft
    heuristic asking the LLM to RECONSIDER — the LLM may still conclude if it
    judges the injection complete. Returns ``None`` (no reminder) when the
    scenario is single-step, no required action is recognised, or nothing looks
    missing.

    Backend-aware token vocabulary: kubectl write verbs for kubectl_native, host
    injection binaries for host_native.
    """
    if not skill_case:
        return None
    steps = _extract_drill_steps(skill_case)
    if len(steps) < 2:
        return None

    # REQUIRED actions come from injection steps only — a baseline/observation
    # step that merely names a verb or binary is not an injection action. The
    # full step list is still shown to the LLM below for context.
    action_steps = _injection_intent_steps(steps)
    if injection_method == "host_native":
        required = _required_host_binaries(action_steps)
        executed = _executed_host_binaries(messages)
    else:  # kubectl_native (and any other multi-step no-UID k8s backend)
        required = _required_kubectl_verbs(action_steps)
        executed = _executed_kubectl_verbs(messages)

    if not required:
        return None
    missing = {tok: desc for tok, desc in required.items() if tok not in executed}
    if not missing:
        return None

    lines = [
        "[Step self-check] This multi-step skill case has an injection action "
        "that may not have been performed yet. Steps outlined:",
    ]
    for i, step in enumerate(steps, 1):
        lines.append(f"{i}. {step}")
    lines.append(
        "\nPossibly not yet performed: "
        + ", ".join(f"{tok} ({desc})" for tok, desc in missing.items())
    )
    lines.append(
        "\nReconsider (this check is heuristic and may be inaccurate): if you "
        "have ALREADY performed the actions needed for the fault effect — a tool "
        "may have timed out but still applied — STOP calling tools and let "
        "verification confirm it. Do NOT repeat actions already done, and do NOT "
        "loop deleting/observing to watch the effect (observation is the "
        "verification phase's job). If an action was genuinely SKIPPED, do it now."
    )
    return "\n".join(lines)
