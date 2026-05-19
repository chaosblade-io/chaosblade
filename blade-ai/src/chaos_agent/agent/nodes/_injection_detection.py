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

# kubectl subcommands that can perform fault injection (non-ChaosBlade methods)
_KUBECTL_INJECT_SUBCOMMANDS = {"scale", "patch", "cordon", "taint", "set"}

# Label selector for ChaosBlade tool pods
_TOOL_POD_LABEL_SELECTOR = "app=otel-c-tool"
_TOOL_POD_NAMESPACE = "chaosblade"


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


def discover_tool_pods(kubectl_output: str) -> list[str]:
    """Parse kubectl get pods output to find Running tool pod names.

    Used by the verifier to find an available tool pod for Layer 1
    kubectl-exec-based blade_status checks (when injection_method
    is "kubectl_exec" and host blade_status is unavailable).

    Args:
        kubectl_output: Output from `kubectl get pods -n chaosblade -l app=otel-c-tool`.
            Expected format::

                NAME                   READY   STATUS    RESTARTS   AGE
                otel-c-tool-xxxxx     1/1     Running   0          1d
                otel-c-tool-yyyyy     1/1     Running   0          2d

    Returns:
        List of pod names with STATUS = "Running". Empty list if no
        running pods found or output is unparseable.
    """
    if not kubectl_output or not isinstance(kubectl_output, str):
        return []

    lines = kubectl_output.strip().splitlines()
    if len(lines) < 2:
        return []

    running_pods = []
    for line in lines[1:]:  # Skip header
        line = line.strip()
        if not line:
            continue
        # Parse kubectl table output: NAME READY STATUS RESTARTS AGE
        # Use regex to handle variable whitespace
        match = re.match(r"^(\S+)\s+\S+\s+(\S+)", line)
        if match:
            pod_name = match.group(1)
            status = match.group(2)
            if status == "Running":
                running_pods.append(pod_name)

    return running_pods


def discover_tool_pods_with_nodes(kubectl_output: str) -> list[tuple[str, str]]:
    """Parse kubectl get pods -o wide output to find Running tool pods with their node names.

    Used by the verifier to find a tool pod on a specific target node for
    Layer 2 verification of node-level faults.

    Args:
        kubectl_output: Output from `kubectl get pods -n chaosblade -l app=otel-c-tool -o wide`.
            Expected format::

                NAME                READY   STATUS    RESTARTS   AGE   IP           NODE
                otel-c-tool-xxxxx  1/1     Running   0          1d    10.0.2.145   cn-hongkong.10.0.2.145

    Returns:
        List of (pod_name, node_name) tuples for pods with STATUS = "Running".
        Empty list if no running pods found or output is unparseable.
    """
    if not kubectl_output or not isinstance(kubectl_output, str):
        return []

    lines = kubectl_output.strip().splitlines()
    if len(lines) < 2:
        return []

    running_pods = []
    for line in lines[1:]:  # Skip header
        line = line.strip()
        if not line:
            continue
        # -o wide format: NAME READY STATUS RESTARTS AGE IP NODE ...
        match = re.match(r"^(\S+)\s+\S+\s+(\S+)\s+\S+\s+\S+\s+\S+\s+(\S+)", line)
        if match:
            pod_name = match.group(1)
            status = match.group(2)
            node_name = match.group(3)
            if status == "Running":
                running_pods.append((pod_name, node_name))

    return running_pods


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
