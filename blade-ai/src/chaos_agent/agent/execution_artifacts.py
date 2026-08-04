"""Structured side-effect artifacts discovered from executed tool results.

The current graph remains message-driven.  This module adds a small durable
index over those messages so safety and recovery code do not have to infer a
debug pod's identity from prose every time.  It intentionally records facts
only after a ToolMessage exists; an LLM-proposed tool call is never an artifact.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from copy import deepcopy

from langchain_core.messages import AIMessage, ToolMessage

logger = logging.getLogger(__name__)

# Debug pods are created with a bounded lifetime (entrypoint ``-- sleep 3600``),
# so cleanup is fire-and-forget: attempt each delete exactly once and mark the
# artifact ``cleaned`` regardless of outcome. A delete that does not land under
# an in-progress network fault is intentionally NOT retried — the pod's own
# ``sleep`` bound lets it lapse on its own, and retrying would ride the very API
# path the fault is severing (slow, unbounded teardown).
#
# Debug-pod deletes are independent and idempotent, so a whole-zone fan-out
# (dozens of node-debugger pods) is cleaned concurrently rather than one-at-a
# -time — bounded so we never open an unbounded burst of API/transport calls.
# Serial cleanup made an AZ-partition drill's teardown take minutes — task-76c59364.
_CLEANUP_CONCURRENCY = 10


_DEBUG_META_RE = re.compile(r"\[debug-pod-meta:\s*(\{.*?\})\]")


def parse_debug_pod_metadata(content: str) -> dict:
    """Parse the structured marker emitted by ``tools.kubectl``."""
    if not isinstance(content, str):
        return {}
    match = _DEBUG_META_RE.search(content)
    if not match:
        return {}
    try:
        value = json.loads(match.group(1))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def is_vehicle_name(name: str, state: dict | None) -> bool:
    """True if ``name`` is a transient injection vehicle, not a fault target.

    Task-29848471: a k3-class replan once quoted the ``kubectl debug`` pod
    name as the fault target and the verifier validated against the vehicle.
    Data sources first, naming-convention heuristic last:

      1. ``execution_artifacts`` — any registered ``debug_pod`` artifact name
         (durable fact; survives message trimming).
      2. ``kubectl_exec_pod_name`` — the tool pod used for exec-injection.
      3. ``debug-pod-meta`` tags in message history (covers artifacts not yet
         collected this iteration).
      4. Heuristic: the ``node-debugger-`` creation prefix.
    """
    if not name or not isinstance(state, dict):
        return bool(name) and str(name).startswith("node-debugger-")
    for artifact in state.get("execution_artifacts") or []:
        if (
            isinstance(artifact, dict)
            and artifact.get("type") == "debug_pod"
            and artifact.get("name") == name
        ):
            return True
    if state.get("kubectl_exec_pod_name") == name:
        return True
    for message in state.get("messages") or []:
        content = getattr(message, "content", None)
        if not isinstance(content, str) or "debug-pod-meta" not in content:
            continue
        if parse_debug_pod_metadata(content).get("name") == name:
            return True
    from chaos_agent.agent.nodes.execute._debug_pod import DEBUG_POD_NAME_PREFIX
    return str(name).startswith(DEBUG_POD_NAME_PREFIX)


def collect_execution_artifacts(
    messages: list,
    existing: list[dict] | None = None,
    *,
    task_id: str = "",
    operation_family: str = "",
) -> list[dict]:
    """Merge artifact facts from tool results into the current durable list."""
    artifacts: dict[str, dict] = {}
    for item in existing or []:
        if not isinstance(item, dict):
            continue
        key = _artifact_key(item)
        if key:
            artifacts[key] = deepcopy(item)

    tool_calls = _tool_call_lookup(messages)
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        call = tool_calls.get(getattr(message, "tool_call_id", ""), {})
        tool_name = call.get("name")

        if tool_name != "kubectl":
            continue
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        subcommand = args.get("subcommand", "")
        content = message.content if isinstance(message.content, str) else ""

        if subcommand == "debug":
            metadata = parse_debug_pod_metadata(content)
            artifact = _debug_pod_artifact(
                metadata,
                task_id=task_id,
                operation_family=operation_family,
                tool_call_id=getattr(message, "tool_call_id", ""),
                debug_v_args=str(args.get("v_args") or ""),
            )
            key = _artifact_key(artifact)
            if key:
                existing_artifact = artifacts.get(key)
                if existing_artifact is None:
                    # Stamp the freshness marker exactly once, when the pod
                    # first registers as active. It is a durable fact like
                    # ``uid``/``node``: message replay rebuilds this artifact
                    # every loop, so we must NOT re-derive ``time.time()`` on
                    # each rebuild or the liveness window would never expire.
                    # Subsequent rebuilds take the merge branch below, which
                    # preserves existing non-empty fields.
                    if (
                        artifact.get("status") == "active"
                        and not artifact.get("confirmed_live_epoch")
                    ):
                        artifact["confirmed_live_epoch"] = time.time()
                    artifacts[key] = artifact
                else:
                    _merge_discovered_artifact(existing_artifact, artifact)
            continue

        if subcommand == "delete" and not _tool_result_failed(message):
            pod_name, namespace = _deleted_pod_identity(args.get("v_args", ""))
            if not pod_name:
                continue
            for artifact in artifacts.values():
                if artifact.get("type") != "debug_pod":
                    continue
                if artifact.get("name") != pod_name:
                    continue
                if namespace and artifact.get("namespace") != namespace:
                    continue
                artifact["status"] = "cleaned"
                artifact["cleanup_tool_call_id"] = getattr(
                    message, "tool_call_id", "",
                )
            continue

        if subcommand == "exec" and not _tool_result_failed(message):
            _mark_bounded_host_recovery(
                artifacts,
                args.get("v_args", ""),
                getattr(message, "tool_call_id", ""),
            )

    return list(artifacts.values())


def find_active_debug_pod(
    artifacts: list[dict] | None,
    pod_name: str,
    namespace: str,
) -> dict | None:
    """Return a registered executable debug pod matching exact identity."""
    matches: list[dict] = []
    for artifact in reversed(artifacts or []):
        if not isinstance(artifact, dict):
            continue
        if (
            artifact.get("type") != "debug_pod"
            or artifact.get("status") not in ("active", "recovery_armed")
        ):
            continue
        if artifact.get("name") != pod_name:
            continue
        if namespace and (artifact.get("namespace") or "default") != namespace:
            continue
        if not artifact.get("uid") or not (artifact.get("target") or {}).get("name"):
            continue
        matches.append(artifact)
    # An omitted namespace means "the active transport namespace". The debug
    # pod was created through that same transport, so a unique registered name
    # is authoritative. Ambiguity still fails closed.
    return matches[0] if len(matches) == 1 else None


async def cleanup_debug_pod_artifacts(
    artifacts: list[dict] | None,
    *,
    kubeconfig: str,
    task_id: str,
) -> tuple[list[dict], list[str]]:
    """Fire-and-forget cleanup of tracked debug pods.

    Attempts each delete exactly once (no retry, no backoff) and marks the
    artifact ``cleaned`` regardless of whether removal was confirmed. Debug
    pods are created with a bounded lifetime (``-- sleep 3600``), so a delete
    that does not land under an in-progress network fault is left to lapse on
    its own rather than retried. Deletes fan out concurrently (bounded by
    ``_CLEANUP_CONCURRENCY``) so a whole-zone teardown stays fast.
    """
    from chaos_agent.agent.nodes.execute._debug_pod import delete_debug_pod

    updated = deepcopy(artifacts or [])
    cleaned: list[str] = []
    sem = asyncio.Semaphore(_CLEANUP_CONCURRENCY)

    async def _clean_one(artifact: dict) -> None:
        if not isinstance(artifact, dict) or artifact.get("type") != "debug_pod":
            return
        if artifact.get("status") == "cleaned":
            return
        recovery_deadline = artifact.get("recovery_deadline_epoch")
        if (
            artifact.get("status") == "recovery_armed"
            and isinstance(recovery_deadline, (int, float))
            and recovery_deadline > time.time()
        ):
            return
        name = str(artifact.get("name") or "")
        namespace = str(artifact.get("namespace") or "")
        if not name or not namespace:
            return
        # Fire-and-forget: one delete attempt, bounded by the shared semaphore
        # so at most ``_CLEANUP_CONCURRENCY`` are in flight at once.
        async with sem:
            outcome = await delete_debug_pod(
                name, kubeconfig, task_id, namespace=namespace,
            )
        # Mark cleaned regardless of outcome — the pod's ``-- sleep 3600`` bound
        # lets an unlanded delete lapse on its own; we never retry it.
        artifact["status"] = "cleaned"
        cleaned.append(name)
        if outcome != "confirmed":
            logger.info(
                "debug pod delete not confirmed; leaving it to expire "
                "(not retried): %s/%s", namespace, name,
            )

    # ``gather`` runs the per-artifact coroutines concurrently; each mutates its
    # own artifact dict in place (single event loop → no lock needed).
    await asyncio.gather(*(_clean_one(a) for a in updated))
    return updated, cleaned


def _debug_pod_artifact(
    metadata: dict,
    *,
    task_id: str,
    operation_family: str,
    tool_call_id: str,
    debug_v_args: str,
) -> dict:
    if not metadata:
        return {}
    name = str(metadata.get("name") or "")
    namespace = str(metadata.get("namespace") or "")
    uid = str(metadata.get("uid") or "")
    node = str(metadata.get("node") or "")
    if not name or not namespace:
        return {}
    # Pod-scoped debug attaches an EPHEMERAL CONTAINER to an existing pod. That
    # pod is the USER'S workload — ``name`` is NOT a tool-created debug pod. It
    # must never be registered with a delete cleanup: an ephemeral container is
    # removed only when the pod itself is recreated, and firing
    # ``kubectl delete pod <name>`` here would destroy the user's workload. So a
    # pod-scoped debug produces NO durable debug_pod artifact (the ephemeral
    # container is not a separately-managed carrier — the tc/exec runs in the
    # target pod's own namespaces, screened as a plain scope=pod call).
    if metadata.get("ephemeral_container"):
        return {}
    ready = metadata.get("ready") is True
    cleaned = metadata.get("cleaned") is True
    return {
        "artifact_id": uid or f"debug_pod:{namespace}/{name}",
        "type": "debug_pod",
        "status": (
            "cleaned" if cleaned else "active" if ready and uid and node else "failed"
        ),
        "task_id": task_id,
        "name": name,
        "namespace": namespace,
        "uid": uid,
        "target": {"scope": "node", "name": node},
        "operation_family": operation_family,
        "debug_profile": _option_value(debug_v_args, "--profile") or str(metadata.get("debug_profile") or ""),
        "privileged": metadata.get("privileged") is True,
        "phase": metadata.get("phase") or "Unknown",
        "created_tool_call_id": tool_call_id,
        "cleanup": {
            "tool": "kubectl",
            "subcommand": "delete",
            "v_args": f"pod {name} -n {namespace} --ignore-not-found",
        },
    }


def _tool_call_lookup(messages: list) -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls or []:
            call_id = call.get("id", "") if isinstance(call, dict) else ""
            if call_id:
                lookup[call_id] = call
    return lookup


def _merge_discovered_artifact(current: dict, discovered: dict) -> None:
    """Fill durable facts without rewinding lifecycle state on replay.

    Message history is replayed on every execute-loop iteration and after TUI
    cancellation. The same debug creation event must not turn ``cleaned`` back
    into ``active`` or move an already-armed recovery deadline forward.
    """
    for key, value in discovered.items():
        if key == "status":
            continue
        if key not in current or current[key] in (None, "", [], {}):
            current[key] = deepcopy(value)


def _tool_result_failed(message: ToolMessage) -> bool:
    if getattr(message, "status", None) == "error":
        return True
    content = message.content if isinstance(message.content, str) else ""
    return content.startswith("Error:") or content.startswith("[target_guard]")


def _mark_bounded_host_recovery(
    artifacts: dict[str, dict],
    v_args: str,
    tool_call_id: str,
) -> None:
    """Keep a debug carrier alive while its node-local rollback timer runs."""
    try:
        args = shlex.split(v_args)
    except ValueError:
        return
    if "--" not in args:
        return
    separator = args.index("--")
    outer = args[:separator]
    inner = " ".join(args[separator + 1:])
    from chaos_agent.agent.target_guard.carriers import (
        classify_host_operation,
        host_operation_has_bounded_recovery,
    )

    family = classify_host_operation(inner)
    if not family or not host_operation_has_bounded_recovery(inner, family):
        return
    timer = re.search(r"\bsleep\s+([1-9][0-9]*)\b", inner)
    if not timer:
        return
    pod_name, namespace = _exec_pod_identity(outer)
    if not pod_name:
        return
    matches = [
        artifact for artifact in artifacts.values()
        if artifact.get("type") == "debug_pod"
        and artifact.get("name") == pod_name
        and (not namespace or artifact.get("namespace") == namespace)
    ]
    if len(matches) != 1:
        return
    artifact = matches[0]
    if artifact.get("host_exec_tool_call_id") == tool_call_id:
        return
    timeout_seconds = int(timer.group(1))
    artifact["status"] = "recovery_armed"
    artifact["host_exec_tool_call_id"] = tool_call_id
    artifact["recovery_timeout_seconds"] = timeout_seconds
    artifact["recovery_deadline_epoch"] = time.time() + timeout_seconds


def _option_value(v_args: str, option: str) -> str:
    try:
        args = shlex.split(v_args)
    except ValueError:
        return ""
    for index, token in enumerate(args):
        if token == option and index + 1 < len(args):
            return args[index + 1]
        if token.startswith(f"{option}="):
            return token.split("=", 1)[1]
    return ""


def _exec_pod_identity(args: list[str]) -> tuple[str, str]:
    namespace = ""
    pod_name = ""
    skip_next = False
    for index, token in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if token in ("-n", "--namespace") and index + 1 < len(args):
            namespace = args[index + 1]
            skip_next = True
            continue
        if token.startswith("--namespace="):
            namespace = token.split("=", 1)[1]
            continue
        if token in ("-c", "--container"):
            skip_next = True
            continue
        if not token.startswith("-") and not pod_name:
            pod_name = token
    return pod_name, namespace


def _deleted_pod_identity(v_args: str) -> tuple[str, str]:
    try:
        args = shlex.split(v_args)
    except ValueError:
        args = v_args.split()
    namespace = ""
    for index, token in enumerate(args):
        if token in ("-n", "--namespace") and index + 1 < len(args):
            namespace = args[index + 1]
        elif token.startswith("--namespace="):
            namespace = token.split("=", 1)[1]
    positionals: list[str] = []
    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if token in ("-n", "--namespace", "-l", "--selector"):
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        positionals.append(token)
    if len(positionals) >= 2 and positionals[0] in ("pod", "pods", "po"):
        return positionals[1], namespace
    if positionals and positionals[0].startswith("pod/"):
        return positionals[0].split("/", 1)[1], namespace
    return "", namespace


def _artifact_key(artifact: dict) -> str:
    artifact_id = str(artifact.get("artifact_id") or "")
    if artifact_id:
        return artifact_id
    artifact_type = str(artifact.get("type") or "")
    name = str(artifact.get("name") or "")
    namespace = str(artifact.get("namespace") or "")
    return f"{artifact_type}:{namespace}/{name}" if artifact_type and name else ""


__all__ = [
    "cleanup_debug_pod_artifacts",
    "collect_execution_artifacts",
    "find_active_debug_pod",
    "is_vehicle_name",
    "parse_debug_pod_metadata",
]
