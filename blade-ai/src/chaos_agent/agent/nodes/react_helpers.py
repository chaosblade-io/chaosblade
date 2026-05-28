"""Shared helper functions for ReAct loop nodes.

Extracted from agent_loop.py, execute_loop.py, verifier.py, and
recover_verifier.py to eliminate code duplication.  Every function
here is either:
  - a pure-function extraction (no external state dependencies beyond
    settings.is_debug), or
  - a parameterised version of near-identical logic where the only
    difference is a constant name.
"""

import logging
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from chaos_agent.config.settings import settings
from chaos_agent.errors import ErrorClass, classify_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tier 1: Pure-function extractions (identical code across files)
# ---------------------------------------------------------------------------

def record_system_prompt(hook, state: dict, prompt_text: str) -> None:
    """Record a system prompt to the session store (dedup handles repeated prompts).

    Parameters
    ----------
    hook : PreReasoningHook or None
        The hook object that may carry a ``session_store``.
    state : dict
        AgentState dict — reads ``task_id`` from it.
    prompt_text : str
        The system prompt content to record.
    """
    _task_id_local = state.get("task_id", "")
    if hook and getattr(hook, "session_store", None) and _task_id_local:
        hook.session_store.append_messages(_task_id_local, [SystemMessage(content=prompt_text)])


def record_ai_message(hook, state: dict, response) -> None:
    """Immediately save an AI message (including reasoning_content) to session.

    Parameters
    ----------
    hook : PreReasoningHook or None
    state : dict
        AgentState dict — reads ``task_id`` from it.
    response : AIMessage
        The LLM response to record.
    """
    _task_id_local = state.get("task_id", "")
    if hook and getattr(hook, "session_store", None) and _task_id_local:
        try:
            hook.session_store.append_messages(_task_id_local, [response])
        except Exception:
            pass


def log_reasoning_content(response, node_name: str, iteration: int) -> None:
    """Diagnostic log for reasoning_content presence (debug mode only).

    Parameters
    ----------
    response : AIMessage
    node_name : str
        Prefix for the log message, e.g. "Agent loop" or "Execute loop".
    iteration : int
        Current iteration count.
    """
    if settings.is_debug:
        additional_kwargs = getattr(response, "additional_kwargs", {}) or {}
        rc = additional_kwargs.get("reasoning_content", "")
        logger.debug(
            f"{node_name} {iteration}: "
            f"reasoning_content={'present(' + str(len(rc)) + ' chars)' if rc else 'ABSENT'}"
        )


def extract_tool_call_fields(tc) -> tuple[str, dict]:
    """Extract (name, args) from a tool call that may be a dict or object.

    Handles the dual-path pattern where LangChain tool calls can be either
    plain dicts (from older versions / deserialised state) or namedtuples /
    objects with attributes.

    Parameters
    ----------
    tc : dict | ToolCall
        A single tool call entry.

    Returns
    -------
    tuple[str, dict]
        (tool_name, tool_args) — name defaults to "" and args to {}.
    """
    if isinstance(tc, dict):
        return tc.get("name", ""), tc.get("args", {})
    return getattr(tc, "name", ""), getattr(tc, "args", {})


# ---------------------------------------------------------------------------
# Tier 2: Parameterised extractions (near-identical, differing only in a
#          constant name or boolean flag)
# ---------------------------------------------------------------------------

def emit_debug_tool_messages(
    tracker,
    state: dict,
    seed_existing: bool = False,
) -> None:
    """Emit ToolMessage results from previous iteration (debug mode only).

    In debug mode, this iterates over the messages in *state*, finds any
    ToolMessage whose ``id`` hasn't been emitted yet, and sends a
    tracker update so the TUI can display tool outputs in real time.

    An ``_emitted_tool_ids`` set is maintained on *tracker* to avoid
    re-emitting the same ToolMessage across iterations.

    Parameters
    ----------
    tracker : ProgressTracker
        The progress tracker attached to the current node.
    state : dict
        AgentState dict — reads ``messages`` from it.
    seed_existing : bool, default False
        If True, on the first call (when ``_emitted_tool_ids`` is empty),
        pre-populate the set with ALL existing ToolMessage IDs so that
        inject-phase / Layer1 results are suppressed from the node's
        observable output.  Used by verifier and recover_verifier.
        If False (agent_loop / execute_loop), all ToolMessages are
        emitted immediately.

        The seeding condition uses ``not emitted_ids`` rather than an
        iteration count because non-ChaosBlade Layer 2 iterations may
        start at count > 1 (after Layer 1 iterations have already
        incremented the counter).
    """
    if not settings.is_debug:
        return

    messages = state.get("messages", [])
    emitted_ids = getattr(tracker, "_emitted_tool_ids", set())

    # On first iteration, optionally seed emitted_ids with pre-existing
    # ToolMessage IDs to suppress results from earlier phases.
    if seed_existing and not emitted_ids:
        for msg in messages:
            if isinstance(msg, ToolMessage):
                msg_id = getattr(msg, "id", None)
                if msg_id:
                    emitted_ids.add(msg_id)

    for msg in messages:
        if isinstance(msg, ToolMessage):
            msg_id = getattr(msg, "id", None)
            if msg_id and msg_id not in emitted_ids:
                tool_name = getattr(msg, "name", "unknown")
                msg_content = msg.content if isinstance(msg.content, str) else str(msg.content)
                preview = msg_content[:100] + "..." if len(msg_content) > 100 else msg_content
                tracker.update(
                    f"📋 {tool_name}: {preview}",
                    {"debug": True, "tool_result": True, "tool_name": tool_name, "stdout_preview": msg_content[:200]},
                )
                emitted_ids.add(msg_id)
    tracker._emitted_tool_ids = emitted_ids


# ---------------------------------------------------------------------------
# Tier 2b: Parameterised extractions (near-identical, differing only in a
#          constant name)
# ---------------------------------------------------------------------------

def extract_synthetic_messages(
    messages: list,
    synthetic_ids: frozenset,
) -> list:
    """Extract synthetic AIMessage+ToolMessage pairs for state persistence.

    On the first iteration (count==1 / is_first_layer2), these messages
    were constructed and injected into the local ``messages`` list but
    are not yet in AgentState.  Extracting them and prepending to
    ``result_update["messages"]`` ensures they survive across iterations.

    Parameters
    ----------
    messages : list[BaseMessage]
        The local messages list for this iteration.
    synthetic_ids : frozenset
        Set of tool_call_ids that mark synthetic (injected) tool calls.
        Different for verifier vs recover_verifier.

    Returns
    -------
    list[BaseMessage]
        AIMessage and ToolMessage entries whose tool_call_ids match
        ``synthetic_ids``.
    """
    result = []
    for msg in messages:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            tc_ids = [
                tc.get("id", "") for tc in msg.tool_calls
                if isinstance(tc, dict)
            ]
            if any(tid in synthetic_ids for tid in tc_ids):
                result.append(msg)
        elif isinstance(msg, ToolMessage):
            if getattr(msg, "tool_call_id", "") in synthetic_ids:
                result.append(msg)
    return result


def extract_persistent_hm(
    messages: list,
    state: dict,
    kwargs_key: str,
) -> list:
    """Extract the main context HumanMessage for state persistence.

    On the first iteration, this HumanMessage was JUST built and appended
    to the local ``messages`` list.  We extract it and prepend to
    ``result_update["messages"]`` so it enters AgentState via the
    add_messages reducer.  On subsequent iterations, the HumanMessage is
    already in AgentState.messages (persisted from iteration 1), so we
    skip extraction to avoid wasteful re-injection.

    Parameters
    ----------
    messages : list[BaseMessage]
        The local messages list for this iteration.
    state : dict
        AgentState dict — reads ``messages`` to check if the HM already
        exists in persisted state.
    kwargs_key : str
        The ``additional_kwargs`` key used to tag this HumanMessage.
        Different for verifier (``_verifier_main_context``) vs
        recover_verifier (``_recover_main_context``).

    Returns
    -------
    list[HumanMessage]
        The tagged HumanMessage, or [] if it already exists in state.
    """
    already_in_state = any(
        getattr(m, "additional_kwargs", {}).get(kwargs_key)
        for m in state.get("messages", [])
        if isinstance(m, HumanMessage)
    )
    if already_in_state:
        return []
    result = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            if getattr(msg, "additional_kwargs", {}).get(kwargs_key):
                result.append(msg)
    return result


# ---------------------------------------------------------------------------
# Moved functions (originally in agent_loop.py)
# ---------------------------------------------------------------------------

def _fingerprint_tool_call(name: str, args: dict) -> str:
    """Generate a fingerprint for a tool call, excluding infrastructure params."""
    # Params to exclude from fingerprinting (connection/infrastructure params)
    _FINGERPRINT_EXCLUDE_PARAMS = {"kubeconfig", "context", "cluster"}
    core_args = {k: v for k, v in args.items()
                 if k not in _FINGERPRINT_EXCLUDE_PARAMS and v}
    sorted_args = sorted(core_args.items())
    args_str = ", ".join(f"{k}={v}" for k, v in sorted_args)
    return f"{name}({args_str})"


def _compare_tool_outputs(
    fingerprint: str,
    fingerprint_to_ids: dict[str, list[str]],
    tool_id_to_output: dict[str, str],
) -> tuple[bool, bool]:
    """Compare ToolMessage outputs for repeated tool calls.

    Returns (all_identical: bool, have_outputs: bool).
    - all_identical=True: all outputs are the same → genuine stuck loop
    - all_identical=False: outputs differ → fault is progressing, suppress loop
    - have_outputs=False: no ToolMessages found at all → can't determine
    """
    tc_ids = fingerprint_to_ids.get(fingerprint, [])
    outputs: list[str] = []
    for tc_id in tc_ids:
        if tc_id in tool_id_to_output:
            content = tool_id_to_output[tc_id]
            normalized = str(content).strip()[:500]
            outputs.append(normalized)

    if len(outputs) < 2:
        return True, bool(outputs)

    first = outputs[0]
    return all(o == first for o in outputs[1:]), True


def _is_observation_command(tool_name: str, args: dict) -> bool:
    """Check if a tool call is an observation/diagnostic command (not mutation)."""
    if tool_name not in ("kubectl", "kubectl_ro"):
        return False
    subcmd = args.get("subcommand", "")
    return subcmd in ("get", "describe", "top", "exec", "logs", "debug")


def suggest_verify_command(tool_name: str) -> str:
    """Suggest a tool-appropriate verification command."""
    if "blade" in tool_name:
        return (
            "Run `blade <subcommand> -h` to check supported flags "
            "(via kubectl exec if host blade is unavailable)"
        )
    if "kubectl" in tool_name:
        return "Run `kubectl <subcommand> --help` to check supported flags"
    return (
        f"Check the actual interface of `{tool_name}` — "
        "the error message itself is the best clue about what went wrong"
    )


def _build_strategy_hints(tool_name: str, subcommand: str | None = None, args: dict | None = None) -> str:
    """Build strategy hints based on the tool name for loop-breaking guidance."""
    if args and _is_observation_command(tool_name, args):
        return (
            "- Repeating the same observation command will NOT produce different results.\n"
            "- Check a DIFFERENT metric, partition, resource, or namespace.\n"
            "- Use a different kubectl subcommand (describe vs get vs exec vs top).\n"
            "- Check a different scope (pod vs node vs namespace vs cluster).\n"
            "- If the fault effect is not observable with current tools, conclude based on available evidence."
        )
    if tool_name in ("kubectl", "kubectl_ro") and subcommand == "get":
        return (
            "- 使用 --field-selector 缩小范围（如 --field-selector spec.nodeName=<node>）\n"
            "- 使用 -o name 仅获取资源名称\n"
            "- 使用 -o jsonpath 提取特定字段\n"
            "- 指定资源名称查询单个资源"
        )
    if tool_name == "execute_skill_script":
        return (
            "- 检查脚本名称是否正确（使用 list_scripts 查看可用脚本）\n"
            "- 尝试使用其他可用脚本\n"
            "- 直接使用 kubectl 工具代替"
        )
    if tool_name in ("blade_create", "blade_destroy"):
        return (
            "- " + suggest_verify_command(tool_name) + "\n"
            "- If a parameter was rejected, do NOT retry — remove it and verify\n"
            "- Runtime errors override documentation — trust the tool, not the docs\n"
            "- Use blade_query_k8s to check existing experiments\n"
            "- Simplify parameters — use only what the tool actually supports"
        )
    return (
        "- 尝试换用其他工具或不同的参数组合\n"
        "- 如果结果已足够，直接给出结论\n"
        "- 如果需要更多信息，尝试不同角度的查询"
    )


def detect_repeated_tool_calls(messages: list) -> str | None:
    """Scan recent messages for repeated identical tool calls.

    Returns a LOOP DETECTED hint if the same tool call fingerprint
    appears >= loop_detection_threshold times within the last
    loop_detection_window messages AND the tool outputs are identical
    (not progressing — e.g., CPU ramping up is legitimate monitoring).

    Two-pass design:
    1. Scan all messages building fingerprint counts + ToolMessage output map
    2. For each fingerprint exceeding threshold, compare outputs.
       If outputs differ → fault is progressing, suppress loop warning.
       If outputs identical → genuine stuck loop, emit hint.
    """
    window = settings.loop_detection_window
    threshold = settings.loop_detection_threshold

    recent = messages[-window:] if len(messages) > window else messages

    fingerprint_counts: dict[str, int] = {}
    fingerprint_to_ids: dict[str, list[str]] = {}
    tool_id_to_output: dict[str, str] = {}

    # Pass 1: single scan, build all three structures
    for msg in recent:
        if isinstance(msg, ToolMessage):
            tc_id = getattr(msg, "tool_call_id", None)
            if tc_id:
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                tool_id_to_output[tc_id] = content
        elif isinstance(msg, AIMessage):
            tool_calls = getattr(msg, "tool_calls", None) or []
            for tc in tool_calls:
                name, args = extract_tool_call_fields(tc)
                tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                if not name:
                    continue
                fp = _fingerprint_tool_call(name, args)
                fingerprint_counts[fp] = fingerprint_counts.get(fp, 0) + 1
                if tc_id:
                    fingerprint_to_ids.setdefault(fp, []).append(tc_id)

    # Pass 2: for each fingerprint exceeding threshold, check output progression
    for fp, count in fingerprint_counts.items():
        if count < threshold:
            continue

        all_identical, have_outputs = _compare_tool_outputs(
            fp, fingerprint_to_ids, tool_id_to_output,
        )

        if have_outputs and not all_identical:
            continue

        tool_name = fp.split("(")[0]
        matched_args = None
        subcmd = None
        if tool_name in ("kubectl", "kubectl_ro"):
            for msg2 in recent:
                if not isinstance(msg2, AIMessage):
                    continue
                for tc2 in (getattr(msg2, "tool_calls", None) or []):
                    tc2_name, tc2_args = extract_tool_call_fields(tc2)
                    if tc2_name == tool_name and _fingerprint_tool_call(tc2_name, tc2_args) == fp:
                        subcmd = tc2_args.get("subcommand")
                        matched_args = tc2_args
                        break
                if subcmd:
                    break

        strategy_hints = _build_strategy_hints(tool_name, subcommand=subcmd, args=matched_args)
        result_clause = "基本一致" if have_outputs else "无法获取"
        return (
            f"**LOOP DETECTED**: 你已用相同参数调用 {fp} 超过 {threshold} 次，"
            f"每次调用返回的结果{result_clause}。"
            f"当前方法无效，必须换策略：\n"
            f"{strategy_hints}"
        )

    return None


def detect_action_stagnation(messages: list, threshold: int | None = None) -> tuple[str | None, str | None]:
    """Detect consecutive calls to the same tool name (regardless of args).

    Unlike detect_repeated_tool_calls (which requires identical fingerprints),
    this catches "parameter thrashing" where the LLM calls the same tool
    with slightly different arguments each time.

    Scans at most ``loop_detection_window`` messages from the tail to
    stay consistent with ``detect_repeated_tool_calls`` and avoid
    unbounded reverse scans on long conversations.

    Returns:
        (hint_message, stagnant_tool_name) or (None, None) if no stagnation.
    """
    _threshold = threshold if threshold is not None else settings.stagnation_threshold
    if _threshold < 2:
        return None, None

    window = settings.loop_detection_window
    recent = messages[-window:] if len(messages) > window else messages

    streak = 0
    streak_tool: str | None = None

    for msg in reversed(recent):
        if not isinstance(msg, AIMessage):
            continue
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            break
        if len(tool_calls) != 1:
            break
        name, _ = extract_tool_call_fields(tool_calls[0])
        if not name:
            break
        if streak_tool is None:
            streak_tool = name
        if name != streak_tool:
            break
        streak += 1

    if streak >= _threshold and streak_tool:
        hint = (
            f"**ACTION_STAGNATION**: You have called `{streak_tool}` {streak} consecutive times "
            f"with no progress. This tool has been temporarily removed. "
            f"You MUST now either:\n"
            f"- Call `finish_planning` to finalize your plan and proceed to execution phase.\n"
            f"- Use a DIFFERENT tool if you need additional information.\n"
            f"Do NOT attempt to call `{streak_tool}` again."
        )
        return hint, streak_tool
    return None, None


def summarize_llm_response(response) -> tuple[str, list[str]]:
    """Extract a short human-readable summary from an LLM response.

    Returns (summary_text, tool_names) where:
    - summary_text: formatted multi-line string for display
    - tool_names: list of tool names called
    """
    tool_calls = getattr(response, "tool_calls", None) or []
    tool_names = []
    lines = []

    if tool_calls:
        for tc in tool_calls:
            # NOTE: This uses the reversed-priority pattern (getattr first, then dict)
            # deliberately — it's the original logic from agent_loop.py.
            name = getattr(tc, "name", "") or (tc.get("name", "") if isinstance(tc, dict) else "?")
            args = getattr(tc, "args", {}) or (tc.get("args", {}) if isinstance(tc, dict) else {})
            tool_names.append(name)
            arg_parts = []
            for k, v in args.items():
                sv = str(v)
                if sv and sv not in ("", "None"):
                    display = sv[:50] + "..." if len(sv) > 50 else sv
                    arg_parts.append(f"{k}={display}")
            args_str = ", ".join(arg_parts) if arg_parts else ""
            lines.append(f"  🔧 tool: {name}({args_str})")

    additional_kwargs = getattr(response, "additional_kwargs", {}) or {}
    reasoning_content = additional_kwargs.get("reasoning_content", "")
    if reasoning_content and isinstance(reasoning_content, str):
        text = reasoning_content[:300] + ("..." if len(reasoning_content) > 300 else "")
        lines.append(f"  💭 thinking: {text}")

    content = getattr(response, "content", "")
    if content and isinstance(content, str):
        text = content[:200] + ("..." if len(content) > 200 else "")
        lines.append(f"  💬 response: {text}")

    summary = "\n".join(lines) if lines else "(empty response)"
    return summary, tool_names


# ---------------------------------------------------------------------------
# Tier 4: Tool error introspection (runtime feedback > static docs)
# ---------------------------------------------------------------------------

_NON_INTERFACE_ERRORS = frozenset({
    ErrorClass.INFRA_TRANSIENT,
    ErrorClass.INFRA_PERSISTENT,
    ErrorClass.AUTH_DENIED,
    ErrorClass.TARGET_GONE,
    ErrorClass.QUOTA_EXCEEDED,
})

_REJECTED_PARAM_PATTERNS = [
    re.compile(r"unknown flag:\s*(\S+)"),
    re.compile(r"unknown shorthand flag:\s*'(\S+)'"),
    re.compile(r"flag provided but not defined:\s*(\S+)"),
    re.compile(r"(?:invalid|illegal) option[:\s]+[-]*([\w-]+)"),
    re.compile(r"unrecognized arguments?:\s*(\S+)"),
    re.compile(
        r"(?:unsupported|unknown|invalid)\s+"
        r"(?:flag|option|parameter|argument)[:\s]+(\S+)"
    ),
]

_HINT_MARKER = "TOOL ERROR — VERIFY BEFORE RETRY"


def _should_trigger_introspection(error_class: ErrorClass) -> bool:
    """Denylist: trigger for ALL errors except known non-interface ones."""
    return error_class not in _NON_INTERFACE_ERRORS


def extract_rejected_params(error_text: str) -> list[str]:
    """Best-effort extraction of rejected parameters from an error message."""
    if not error_text:
        return []
    found: list[str] = []
    for pat in _REJECTED_PARAM_PATTERNS:
        for m in pat.finditer(error_text):
            val = m.group(1).strip("'\"").rstrip(".,;:!?)")
            if val and val not in found:
                found.append(val)
    return found


def _build_introspection_hint(
    tool_name: str, error_content: str, rejected_params: list[str],
) -> str:
    parts = [
        f"**{_HINT_MARKER}**: `{tool_name}` returned an error.",
        error_content[:200],
        "",
        "Runtime feedback overrides documentation. Before retrying:",
    ]
    if rejected_params:
        flags_str = ", ".join(f"`{f}`" for f in rejected_params)
        parts.append(
            f"- Parameter(s) {flags_str} were REJECTED — do NOT retry with them"
        )
    parts.extend([
        "- " + suggest_verify_command(tool_name),
        "- Adapt your approach to match what the tool actually supports",
        "- If documentation says X is supported but the tool rejects it, the tool is right",
    ])
    return "\n".join(parts)


def detect_tool_error_hint(messages: list) -> str | None:
    """Scan recent ToolMessages for errors that warrant introspection.

    Returns a hint string if a qualifying error is found and no
    duplicate hint already exists in messages. Returns None otherwise.
    """
    window = min(len(messages), 10)
    recent = messages[-window:]

    for msg in reversed(recent):
        if not isinstance(msg, ToolMessage):
            continue
        content = msg.content if isinstance(msg.content, str) else ""
        if not content.startswith("Error"):
            continue

        result = classify_error(content)
        if not _should_trigger_introspection(result.error_class):
            continue

        tool_name = getattr(msg, "name", "") or ""
        if any(
            isinstance(m, HumanMessage)
            and isinstance(m.content, str)
            and _HINT_MARKER in m.content
            and f"`{tool_name}`" in m.content
            for m in recent
        ):
            continue

        rejected = extract_rejected_params(content)
        return _build_introspection_hint(tool_name, content, rejected)

    return None
