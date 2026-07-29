"""Stateless injection-detection primitives shared by FaultProviders.

These are the low-level message-history scans that a provider's ``detect`` /
``recover`` needs to recognise its own carrier in the conversation. They live
in the providers package (not in ``agent/nodes/execute``) so a provider never
has to reverse-import an orchestration module — the *data* (which tool names /
kubectl subcommands count as a given carrier's injection) is declared on the
provider class, and these functions take that set as a parameter.

The execute / verify / recover nodes re-export thin wrappers around these
(``_was_kubectl_injection_attempted`` /
``_was_kubectl_blade_injection_successful``) so existing call sites and tests
keep their import paths, while this module remains the single source of the
scan logic. This module intentionally imports nothing from ``agent.nodes`` —
only langchain messages + stdlib — so it sits below both providers and nodes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from langchain_core.messages import AIMessage, ToolMessage

logger = logging.getLogger(__name__)


def build_tool_call_args_lookup(messages: list) -> dict:
    """Map ``tool_call_id`` → tool call args by scanning AIMessages.

    Lets a ToolMessage be cross-referenced back to the originating tool call
    arguments (e.g. ``subcommand`` / ``v_args``). Entries with missing/empty
    id are skipped.
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


def _host_native_call_is_readonly(args: object) -> bool:
    """True if a host-native carrier tool_call ran a READ-ONLY diagnostic.

    ``host_inject`` is the superset of ``host_read`` (it admits read-only
    diagnostics with ``skip_guard``), so a successful ``host_inject`` ToolMessage
    is NOT necessarily an injection. Mirror the content-aware attribution the
    kubectl-native scan uses: a read-only command is not an injection.
    """
    if not isinstance(args, dict):
        return False
    import shlex

    from chaos_agent.tools.readonly import is_readonly_argv

    command = args.get("command")
    if isinstance(command, str) and command.strip():
        try:
            argv = shlex.split(command)
        except ValueError:
            return False
        return bool(argv) and is_readonly_argv(argv)
    # exec_host_command shape: binary + args list.
    binary = args.get("binary")
    if isinstance(binary, str) and binary:
        extra = args.get("args") or []
        argv = [binary] + [str(a) for a in extra]
        return is_readonly_argv(argv)
    return False


def scan_host_native_injection(messages: list, tool_names: frozenset[str]) -> bool:
    """True if a host-native command tool in ``tool_names`` ran successfully.

    Reverse-scans for the most recent ToolMessage whose name is one of the
    carrier's injection tools and whose content is not an ``Error:``. The
    caller (HostShellProvider) supplies its own ``inject_tool_names`` so this
    stays carrier-agnostic. A read-only diagnostic run through ``host_inject``
    (its host_read superset role) is content-aware EXCLUDED — it is not an
    injection.
    """
    lookup = build_tool_call_args_lookup(messages)
    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") not in tool_names:
            continue
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if content.startswith("Error:"):
            continue
        if _host_native_call_is_readonly(lookup.get(getattr(msg, "tool_call_id", ""), {})):
            continue
        return True
    return False


def scan_kubectl_injection_after_blade(
    messages: list, subcommands: set[str] | frozenset[str]
) -> bool:
    """True if a successful kubectl write op in ``subcommands`` followed a
    ``blade_create`` attempt.

    Detects the kubectl-native alternative injection: a mutating kubectl call
    (scale/patch/cordon/...) that succeeded AFTER the last blade_create, so
    kubectl calls before blade_create (normal verification) and failed calls
    don't count.
    """
    lookup = build_tool_call_args_lookup(messages)

    last_blade_create_idx = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, ToolMessage) and getattr(msg, "name", "") == "blade_create":
            last_blade_create_idx = i

    for i, msg in enumerate(messages):
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") != "kubectl":
            continue
        if i <= last_blade_create_idx:
            continue
        tc_id = getattr(msg, "tool_call_id", "")
        if tc_id and tc_id in lookup:
            args = lookup[tc_id]
            subcommand = args.get("subcommand", "")
            if subcommand in subcommands:
                content = msg.content or ""
                if not content.startswith("Error:"):
                    return True
    return False


def scan_kubectl_mutation_attempted(
    messages: list,
    write_subcommands: set[str] | frozenset[str],
    *,
    command_subcommands: set[str] | frozenset[str] = frozenset(),
    is_mutating_command: Callable[[str], bool] | None = None,
) -> bool:
    """True if a kubectl mutating call was ATTEMPTED, keyed on the injection
    ATTEMPT (AIMessage tool_calls) — NOT on the tool RESULT.

    Attribution follows what was *launched*, not whether it returned success: a
    network-drop injection severs the ``kubectl exec`` connection, so its
    ToolMessage comes back as ``Error:`` (timeout). Keying on a successful
    result would misread a *successful* injection as none — the forensic
    paradox (the more effective the network fault, the more its own carrier
    command looks like a failure). We therefore scan AIMessage tool_calls for a
    ``kubectl`` call and deliberately do NOT inspect the ToolMessage outcome.
    Whether the fault actually took effect is Layer 2's job; this only
    attributes the carrier.

    Two attempt shapes are recognised, so a read-only ``kubectl exec`` is NOT
    mistaken for an injection:

    - **Object-write** — a ``subcommand`` in ``write_subcommands``
      (scale/patch/cordon/...). The verb itself IS the mutation, so no command
      inspection is needed.
    - **Command-mode** — a ``subcommand`` in ``command_subcommands``
      (``exec``/``debug``) is only an injection when its inner command actually
      mutates. That judgement is delegated to the caller-injected
      ``is_mutating_command`` (fed the raw ``v_args``) so this scan stays
      carrier-agnostic and reuses the tool layer's structured read/mutate
      classification instead of parsing command text here. Without a callback,
      command-mode calls never count (a bare ``exec`` is not assumed mutating).

    All subcommand sets are caller-injected — the provider owns its own
    vocabulary — keeping this scan carrier-agnostic.
    """
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            if isinstance(tc, dict):
                name = tc.get("name", "")
                args = tc.get("args", {})
            else:
                name = getattr(tc, "name", "")
                args = getattr(tc, "args", {})
            if name != "kubectl" or not isinstance(args, dict):
                continue
            subcommand = args.get("subcommand", "")
            if subcommand in write_subcommands:
                return True
            if (
                subcommand in command_subcommands
                and is_mutating_command is not None
            ):
                v_args = args.get("v_args", "")
                if isinstance(v_args, str) and is_mutating_command(v_args):
                    return True
    return False


def scan_destroyed_uids(messages: list) -> set[str]:
    """UIDs the LLM has issued ``blade_destroy`` for (from AIMessage tool_calls).

    A UID sent to ``blade_destroy`` is no longer an active injection: whether
    the destroy succeeded or failed, it is residual and MUST NOT be picked up
    as the current fault's carrier. Mirrors the execute-node's
    ``_collect_destroyed_uids`` so provider detection applies the same rigor as
    ``_extract_blade_uid_from_messages`` (previously only the latter excluded
    destroyed UIDs, so ``ChaosbladeProvider.detect`` re-claimed a failed,
    already-cleaned experiment — task-76c59364 regression).
    """
    destroyed: set[str] = set()
    for msg in messages:
        for tc in getattr(msg, "tool_calls", None) or []:
            name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            if name != "blade_destroy":
                continue
            args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            uid = args.get("uid", "") if isinstance(args, dict) else ""
            if uid:
                destroyed.add(uid)
    return destroyed


def scan_blade_evidence_index(
    messages: list, *, destroyed: set[str] | frozenset[str] = frozenset(),
) -> tuple[int, str | None]:
    """Most-recent NON-destroyed ChaosBlade injection evidence.

    Reverse-scans for the latest ``blade_create`` / ``kubectl`` ToolMessage
    carrying a parseable blade UID that has NOT been ``blade_destroy``'d.
    Returns ``(message_index, method)`` where method is ``host_blade`` (via the
    blade tool) or ``kubectl_exec`` (blade run through kubectl exec), or
    ``(-1, None)`` when no live blade experiment is attested.

    The ``message_index`` is the recency key the registry uses to arbitrate
    against a later kubectl-/host-native injection (attribute to the LAST
    successful injection, not the earliest blade UID in history).
    """
    from chaos_agent.utils.blade_uid import extract_blade_uid

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, ToolMessage):
            continue
        name = getattr(msg, "name", "") or ""
        if name not in ("blade_create", "kubectl"):
            continue
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        uid = extract_blade_uid(content)
        if uid and uid not in destroyed:
            return i, ("host_blade" if name == "blade_create" else "kubectl_exec")
    return -1, None


def scan_kubectl_mutation_index(
    messages: list,
    write_subcommands: set[str] | frozenset[str],
    *,
    command_subcommands: set[str] | frozenset[str] = frozenset(),
    is_mutating_command: Callable[[str], bool] | None = None,
) -> int:
    """Index of the most-recent AIMessage carrying a mutating kubectl attempt.

    Recency-returning companion to :func:`scan_kubectl_mutation_attempted`
    (same attribution rules — keyed on the ATTEMPT, not the tool result).
    Returns the message index, or ``-1`` when no mutating kubectl call was
    attempted.
    """
    last = -1
    for i, msg in enumerate(messages):
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            if isinstance(tc, dict):
                name = tc.get("name", "")
                args = tc.get("args", {})
            else:
                name = getattr(tc, "name", "")
                args = getattr(tc, "args", {})
            if name != "kubectl" or not isinstance(args, dict):
                continue
            subcommand = args.get("subcommand", "")
            if subcommand in write_subcommands:
                last = i
                break
            if subcommand in command_subcommands and is_mutating_command is not None:
                v_args = args.get("v_args", "")
                if isinstance(v_args, str) and is_mutating_command(v_args):
                    last = i
                    break
    return last


def scan_host_native_index(messages: list, tool_names: frozenset[str]) -> int:
    """Index of the most-recent successful host-native carrier ToolMessage.

    Recency-returning companion to :func:`scan_host_native_injection`
    (same content-aware rule — a read-only ``host_inject`` diagnostic is not an
    injection). Returns ``-1`` when no successful host-native command is
    attested.
    """
    lookup = build_tool_call_args_lookup(messages)
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, ToolMessage):
            continue
        if getattr(msg, "name", "") not in tool_names:
            continue
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        if content.startswith("Error:"):
            continue
        if _host_native_call_is_readonly(lookup.get(getattr(msg, "tool_call_id", ""), {})):
            continue
        return i
    return -1


def scan_kubectl_blade_success(messages: list) -> bool:
    """True if ``kubectl exec`` was used to successfully inject a ChaosBlade
    experiment (bypassing the ``blade_create`` tool).

    Finds a kubectl ToolMessage carrying ChaosBlade success JSON
    (``{"code":200,"success":true,"result":"<uid>"}``) and cross-references
    the AIMessage tool_call to verify it was ``subcommand='exec'`` with
    ``blade`` + ``create`` in ``v_args``. Falls back to content-only detection
    when the tool_call_id is missing (older sessions / synthetic ids).
    """
    lookup = build_tool_call_args_lookup(messages)

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

        tc_id = getattr(msg, "tool_call_id", "")
        if tc_id and tc_id in lookup:
            args = lookup[tc_id]
            subcommand = args.get("subcommand", "")
            v_args = args.get("v_args", "")
            if subcommand == "exec" and "blade" in v_args and "create" in v_args:
                return True
            continue

        logger.debug(
            "kubectl ToolMessage with ChaosBlade success JSON: "
            "tool_call_id=%s not in AIMessage lookup, using content-only detection",
            tc_id or "(none)",
        )
        return True

    return False


__all__ = [
    "build_tool_call_args_lookup",
    "scan_host_native_injection",
    "scan_kubectl_injection_after_blade",
    "scan_kubectl_mutation_attempted",
    "scan_kubectl_blade_success",
    "scan_destroyed_uids",
    "scan_blade_evidence_index",
    "scan_kubectl_mutation_index",
    "scan_host_native_index",
]
