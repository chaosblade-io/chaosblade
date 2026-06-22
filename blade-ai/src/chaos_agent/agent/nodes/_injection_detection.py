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

logger = logging.getLogger(__name__)

# kubectl subcommands that can perform fault injection (non-ChaosBlade methods).
# Covers all mutating operations that an LLM might use as alternative injection
# after blade_create fails: resource removal (delete), node evacuation (drain),
# selector/label manipulation (label), and the original set (scale/patch/cordon/taint/set).
_KUBECTL_INJECT_SUBCOMMANDS = {"scale", "patch", "cordon", "taint", "set", "delete", "drain", "label"}

# Label selector for ChaosBlade tool pods
_TOOL_POD_LABEL_SELECTOR = "app=otel-c-tool"
_TOOL_POD_NAMESPACE = "chaosblade"

# Known tool pod label selectors (tried in order)
_TOOL_POD_LABEL_CANDIDATES = ["app=chaosblade-tool", "app=otel-c-tool"]


def _build_tool_call_args_lookup(messages: list) -> dict:
    """Build a mapping from tool_call_id to tool call args.

    Scans AIMessages for tool_calls and creates a lookup dict that
    allows cross-referencing a ToolMessage.tool_call_id back to the
    original tool call arguments (e.g., subcommand, v_args).

    Returns:
        dict mapping tool_call_id (str) to args (dict).
        Entries with missing/empty id are skipped.
    """
    lookup: dict[str, dict] = {}
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in tool_calls:
            if isinstance(tc, dict):
                tc_id = tc.get("id", "")
                args = tc.get("args", {})
            else:
                tc_id = getattr(tc, "id", "")
                args = getattr(tc, "args", {})
            if tc_id:
                lookup[tc_id] = args
    return lookup


def _was_kubectl_blade_injection_successful(messages: list) -> bool:
    """Check if kubectl exec was used to successfully inject a ChaosBlade experiment.

    When the blade_create tool fails (e.g. host blade binary too old), the LLM
    may bypass it by using kubectl exec to run blade commands directly inside a
    cluster pod. This function detects that scenario by:

    1. Finding kubectl ToolMessages with ChaosBlade success JSON
       {"code":200,"success":true,"result":"<uid>"}
    2. Cross-referencing with the AIMessage tool_calls to verify the call was
       specifically subcommand='exec' with 'blade' and 'create' in v_args.

    This avoids false positives from other kubectl operations (get, patch, scale,
    etc.) that might also be present in the message history.

    Backward compatibility: if tool_call_id is missing (older sessions), falls
    back to content-only detection.
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

        if not (isinstance(data, dict)
                and data.get("success") is True
                and data.get("code") == 200
                and isinstance(data.get("result"), str)
                and data["result"]):
            continue

        # ChaosBlade success JSON found in kubectl ToolMessage
        tc_id = getattr(msg, "tool_call_id", "")
        if tc_id and tc_id in lookup:
            args = lookup[tc_id]
            subcommand = args.get("subcommand", "")
            v_args = args.get("v_args", "")
            if subcommand == "exec" and "blade" in v_args and "create" in v_args:
                return True
            # JSON matches but args don't → NOT a blade injection
            continue

        # tool_call_id missing or not in lookup (e.g., direct_execute
        # synthetic IDs, older session format) → accept based on content
        logger.debug(
            "kubectl ToolMessage with ChaosBlade success JSON: "
            "tool_call_id=%s not in AIMessage lookup, using content-only detection",
            tc_id or "(none)",
        )
        return True

    return False


def _was_kubectl_injection_attempted(messages: list) -> bool:
    """Check if kubectl write operations were used for fault injection.

    Detects kubectl ToolMessages with injective subcommands (scale, patch,
    cordon, taint, set) that were called AFTER blade_create failures,
    indicating the agent switched to an alternative injection method.

    Returns True only if a successful kubectl write operation follows
    blade_create attempts, ensuring that:
    - kubectl calls BEFORE blade_create don't count (normal verification)
    - Failed kubectl calls don't count
    """
    lookup = _build_tool_call_args_lookup(messages)

    # Find the index of the last blade_create ToolMessage
    last_blade_create_idx = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, ToolMessage) and getattr(msg, "name", "") == "blade_create":
            last_blade_create_idx = i

    for i, msg in enumerate(messages):
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") != "kubectl":
            continue

        # Must come AFTER blade_create attempts
        if i <= last_blade_create_idx:
            continue

        tc_id = getattr(msg, "tool_call_id", "")
        if tc_id and tc_id in lookup:
            args = lookup[tc_id]
            subcommand = args.get("subcommand", "")
            if subcommand in _KUBECTL_INJECT_SUBCOMMANDS:
                # Check if the kubectl call succeeded (no error in content)
                content = msg.content or ""
                if not content.startswith("Error:"):
                    return True
    return False


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
    from chaos_agent.tools.shell import run_command
    from chaos_agent.tools.kubectl import build_kubectl_cmd, _adapt_kubewiz_result
    from chaos_agent.config.settings import settings

    for label in _TOOL_POD_LABEL_CANDIDATES:
        cmd = build_kubectl_cmd("get", [
            "pods", "-A", "-l", label, "--no-headers", "-o", "wide",
        ], kubeconfig=kubeconfig)
        try:
            result = await run_command(
                cmd,
                timeout=settings.timeout_kubectl,
                task_id=task_id,
                source="baseline-capture",
            )
            result = _adapt_kubewiz_result(result)
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
    from chaos_agent.tools.shell import run_command
    from chaos_agent.tools.kubectl import build_kubectl_cmd, _adapt_kubewiz_result
    from chaos_agent.config.settings import settings

    for label in _TOOL_POD_LABEL_CANDIDATES:
        cmd = build_kubectl_cmd("get", [
            "pods", "-A", "-l", label, "--no-headers",
        ], kubeconfig=kubeconfig)
        result = await run_command(
            cmd,
            timeout=settings.timeout_kubectl,
            task_id=task_id,
            source="conflict-check",
        )
        result = _adapt_kubewiz_result(result)
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
    from chaos_agent.tools.shell import run_command
    from chaos_agent.tools.kubectl import build_kubectl_cmd, _adapt_kubewiz_result
    from chaos_agent.config.settings import settings

    for label in _TOOL_POD_LABEL_CANDIDATES:
        cmd = build_kubectl_cmd("get", [
            "pods", "-A", "-l", label, "--no-headers", "-o", "wide",
        ], kubeconfig=kubeconfig)
        try:
            result = await run_command(
                cmd,
                timeout=settings.timeout_kubectl,
                task_id=task_id,
                source="tool-pod-discovery",
            )
            result = _adapt_kubewiz_result(result)
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
# Injection step completeness check
# ---------------------------------------------------------------------------

# kubectl verbs that appear in skill case 演练步骤
_STEP_KUBECTL_VERBS = frozenset({
    "cordon", "uncordon", "taint", "delete", "scale", "patch",
    "drain", "label", "annotate",
})


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


# Chinese verb → kubectl subcommand mapping
_CHINESE_VERB_MAP: dict[str, str] = {
    "删除": "delete",
    "缩容": "scale",
    "扩容": "scale",
    "标记为不可调度": "cordon",
    "取消不可调度": "uncordon",
    "添加污点": "taint",
    "移除污点": "taint",
}


def _extract_kubectl_verbs_from_step(step_text: str) -> set[str]:
    """Extract kubectl verb keywords from a single 演练步骤 text."""
    found: set[str] = set()
    lower = step_text.lower()
    for verb in _STEP_KUBECTL_VERBS:
        if verb in lower:
            found.add(verb)
    for cn, en in _CHINESE_VERB_MAP.items():
        if cn in step_text:
            found.add(en)
    return found


def _get_executed_kubectl_verbs(messages: list) -> set[str]:
    """Scan ToolMessages to find which kubectl write subcommands were executed."""
    lookup = _build_tool_call_args_lookup(messages)
    executed: set[str] = set()
    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") != "kubectl":
            continue
        content = msg.content or ""
        if content.startswith("Error:"):
            continue
        tc_id = getattr(msg, "tool_call_id", "")
        if tc_id and tc_id in lookup:
            sub = lookup[tc_id].get("subcommand", "")
            if sub in _KUBECTL_INJECT_SUBCOMMANDS:
                executed.add(sub)
    return executed


def check_injection_step_completeness(
    skill_case: str,
    messages: list,
) -> str | None:
    """Check if all kubectl injection steps from 演练步骤 have been executed.

    Returns a nudge message listing remaining steps, or None if complete.
    """
    if not skill_case:
        return None

    steps = _extract_drill_steps(skill_case)

    # Primary: extract from 演练步骤 section
    # Fallback: scan injection-related content (before recovery sections)
    if steps:
        required_verbs: dict[str, str] = {}
        for step in steps:
            verbs = _extract_kubectl_verbs_from_step(step)
            for v in verbs:
                first_line = step.split('\n')[0].strip()
                required_verbs[v] = first_line
    else:
        # Truncate before recovery sections to avoid matching recovery commands
        injection_content = skill_case
        for marker in ("注入恢复", "恢复验证", "**注入恢复**", "**恢复验证**"):
            idx = injection_content.find(marker)
            if idx >= 0:
                injection_content = injection_content[:idx]
                break
        required_verbs = {}
        all_verbs = _extract_kubectl_verbs_from_step(injection_content)
        for v in all_verbs:
            required_verbs[v] = f"kubectl {v} (from skill case)"

    if not required_verbs:
        return None

    executed = _get_executed_kubectl_verbs(messages)
    missing = {v: desc for v, desc in required_verbs.items() if v not in executed}

    if not missing:
        return None

    lines = [
        "**INCOMPLETE INJECTION**: The skill case requires multiple "
        "injection steps, but you only completed some of them. "
        "You MUST execute the remaining steps before concluding:\n",
    ]
    for verb, desc in missing.items():
        lines.append(f"- **{verb}**: {desc}")
    lines.append(
        "\nExecute these remaining steps now using kubectl. "
        "Do NOT skip any step — each is required to produce the full fault effect."
    )
    return "\n".join(lines)
