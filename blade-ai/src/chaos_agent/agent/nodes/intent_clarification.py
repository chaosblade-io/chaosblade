"""intent_clarification node — TUI conversational gateway.

A unified dialogue node that handles chat, cluster Q&A, capability Q&A,
recover routing, and fault-intent convergence in a single LLM call. The
LLM naturally transitions between modes based on conversation context:

1. **Chat / Q&A** — greetings, chitchat, cluster queries (kubectl), capability
   questions (read_skill_resource) → answer directly in content (text only).
2. **Route** — explicit recover intent → classify_intent("recover");
   user says goodbye → classify_intent("chat").
3. **Converge** — fault injection intent (clear or vague) → collect details via
   kubectl + skills → submit_fault_intent.

Multi-invocation model: each user message = independent graph invocation.
Pure text response = conversation turn done (graph ends, TUI waits for next input).
Only classify_intent/submit_fault_intent trigger state transitions.

CLI mode skips this node entirely via route_after_load_memory routing.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Annotated, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool as lc_tool
from pydantic import BeforeValidator

from chaos_agent.config.settings import settings
from chaos_agent.agent.fault_spec import FaultSpec, read_fault_spec
from chaos_agent.agent.nodes.react_helpers import (
    detect_action_stagnation,
    detect_repeated_tool_calls,
)
from chaos_agent.agent.prompts.builders import build_system_prompt
from chaos_agent.agent.prompts.modes import PromptMode
from chaos_agent.agent.state import AgentState
from chaos_agent.memory.hook import merge_hook_updates
from chaos_agent.memory.session_store import NO_SESSION_MARKER
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Function calling schemas (parsed by the node, NOT ToolNode tools)
# ---------------------------------------------------------------------------

CLASSIFY_INTENT_TOOL = {
    "name": "classify_intent",
    "description": (
        "Route a non-inject intent. Call this ONLY when the user clearly "
        "wants to recover a previous experiment (and you have confirmed "
        "which one via query_active_experiments), or to end the conversation "
        "(chat/goodbye). Do NOT call this for cluster queries or capability "
        "questions — answer those directly using kubectl / read_skill_resource."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["recover", "chat"],
                "description": "The classified intent type.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "How confident you are. 1.0 = sure, 0.5 = uncertain.",
            },
            "recover_task_id": {
                "type": "string",
                "description": (
                    "The task_id of the experiment to recover. Required when "
                    "intent='recover'. Get this from query_active_experiments."
                ),
            },
        },
        "required": ["intent", "confidence"],
    },
}

MAX_CLARIFICATION_ROUNDS = settings.max_clarification_rounds
MAX_DIALOGUE_ROUNDS = settings.max_dialogue_rounds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def bootstrap_task_session(
    op_task_id: str,
    operation: str,
    tui_session_id: str,
    handoff_message: SystemMessage | None,
) -> None:
    """Register the freshly-allocated ``op_task_id`` with the global
    SessionStore so subsequent ``append_messages`` / ``finalize_session``
    calls can write to ``memory/tasks/<op_task_id>.json``.

    **Public** (no leading underscore) because ``intent_confirm.py`` also
    invokes it: the inject pipeline's session bootstrap happens AFTER
    user approval (Option A — see header comment in intent_confirm), so
    both nodes need access. The recover branch in this file still calls
    it from the clarification side (no second confirmation gate).

    Why this lives here: ``_allocate_operation_task_id`` is the moment
    the inject / recover pipeline takes over from intent clarification.
    Before this fix the TS TUI ``/turn`` flow never called
    ``SessionStore.create_session(...)`` for the freshly-minted
    ``task-<hex>`` (only the legacy ``/inject`` endpoint and the CLI
    runner did), so:

      - ``memory/tasks/`` stayed empty for every TUI-mode injection
      - ``append_messages`` in ``memory_nodes.py`` silently no-op'd
        because ``_active_sessions`` had no entry for the task
      - ``/replay <task_id>`` had no recording to play back
      - On TUI restart, the boot ``PendingTasksCard`` could see the
        SQLite metadata (separate persistence) but the recover flow
        had no message context for the task

    The bootstrap is best-effort: if the global store is not
    registered (e.g. a unit test running the node in isolation) the
    helper silently no-ops. Reasoning: graph correctness must not
    depend on the persistence layer being available — sync_to_store
    follows the same "log-and-continue on disk failure" contract.

    ``handoff_message`` is the IntentClarificationSummary SystemMessage
    that marks the boundary between intent dialogue (lives in the TUI
    session file) and execution content (lives in the task file). It
    becomes the FIRST entry in the task file so any future replay
    starts from the handoff boundary. ``None`` is acceptable for
    flows that don't carry a handoff (e.g. recover bridge) — the
    task file simply starts empty.
    """
    if not op_task_id or not isinstance(op_task_id, str):
        return
    try:
        from chaos_agent.memory.session_store import get_global_session_store
        store = get_global_session_store()
        if store is None:
            return
        # ``create_session`` is idempotent in the sense that calling
        # it twice with the same task_id resets the active session
        # entry — but the disk file would also be truncated. We guard
        # against double-invocation here so a re-entry into the
        # fast-path (e.g. LangGraph replay after an interrupt) doesn't
        # wipe the already-recording task file.
        if store.has_active(op_task_id):
            return
        initial = [handoff_message] if handoff_message is not None else None
        store.create_session(
            op_task_id,
            operation=operation,
            tui_session_id=tui_session_id or "",
            initial_messages=initial,
        )
        logger.info(
            "Bootstrapped task session task=%s operation=%s tui_session=%s",
            op_task_id, operation, tui_session_id or "(none)",
        )
    except Exception:
        logger.warning(
            "Failed to bootstrap task session for %s (operation=%s); "
            "task file will not be created. The graph will continue.",
            op_task_id, operation, exc_info=True,
        )


def _allocate_operation_task_id(current_task_id: str) -> str:
    """Allocate a real ``task-<hex>`` ID for an inject / recover op.

    Only the inject and recover pipelines own the concept of a "task";
    intent clarification, chat, and capability Q&A do not. This helper
    is called at the moment the dialogue transitions into one of those
    two pipelines (i.e. when ``intent_clarification`` returns
    ``confirmed_intent="inject"`` or ``"recover"``) so the task
    identity is born inside the pipeline that owns it — turn.py /
    routes do NOT mint ``task-`` IDs themselves.

    If the state already carries a ``task-<hex>`` (CLI runner mints
    one externally before entering the graph), reuse it so we don't
    clobber a CLI-provided id. Otherwise (TS TUI: per-turn id is
    ``turn-<hex>``) allocate a fresh ``task-<hex>``.
    """
    if isinstance(current_task_id, str) and current_task_id.startswith("task-"):
        return current_task_id
    return f"task-{uuid.uuid4().hex[:12]}"


def _extract_classify_intent(tool_calls: list) -> Optional[dict]:
    """Extract classify_intent result from LLM tool calls."""
    for tc in tool_calls:
        if tc.get("name") == "classify_intent":
            args = tc.get("args", {})
            intent = args.get("intent", "chat")
            confidence = args.get("confidence", 0.0)
            if isinstance(confidence, str):
                try:
                    confidence = float(confidence)
                except (ValueError, TypeError):
                    confidence = 0.0
            valid = ("recover", "chat")
            return {
                "intent": intent if intent in valid else "chat",
                "confidence": min(1.0, max(0.0, confidence)),
                "recover_task_id": args.get("recover_task_id", ""),
            }
    return None


# ---------------------------------------------------------------------------
# Argument coercion helpers
#
# Used by BOTH the Pydantic ``BeforeValidator`` on ``submit_fault_intent``
# (so JSON-stringified args from LLM tool_calls pass schema validation
# instead of dying with ``Input should be a valid list/dict``) AND by
# ``_extract_submit_args`` (which reads raw tool_call args directly from
# message history, bypassing ToolNode's schema validation entirely).
#
# Both paths must apply identical normalisation so the fault_intent the
# downstream pipeline sees is shape-stable regardless of which path
# produced it.
# ---------------------------------------------------------------------------


def _coerce_to_list(raw, field_name: str = "") -> list:
    """Coerce a tool_call arg value into a Python list.

    Why this exists: LangChain's ``@lc_tool`` declares ``names`` as
    ``list[str]``, but real-world LLM function-calling output is
    inconsistent — some models (notably qwen builds in certain
    function-calling modes) JSON-stringify list arguments before
    serialising the tool_call. The arg arrives as ``'["a","b"]'``
    instead of ``["a", "b"]``, and the previous extractor wrapped
    that whole JSON-string in a single-element list, silently
    corrupting the resulting fault_intent (a literal ``'["a","b"]'``
    became one phantom resource name).

    Resolution order:
      1. ``None`` / missing → ``[]``
      2. real list → defensive copy
      3. JSON-stringified array (``"[...]"``) → parse, return list
      4. plain non-empty string → wrap as single-element list
         (legitimate "single name typed without brackets" case)
      5. anything else iterable → list(); non-iterable → wrap

    Logs at debug level (not warning) when JSON parsing fails so we
    don't spam in the common single-name path.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return parsed
            except (ValueError, TypeError):
                logger.debug(
                    "submit_fault_intent.%s came in as JSON-shaped string "
                    "but failed to parse: %r — falling back to single-element list",
                    field_name,
                    s[:120],
                )
        return [raw]
    try:
        return list(raw)
    except TypeError:
        return [raw]


def _coerce_to_dict(raw, field_name: str = "") -> dict:
    """Coerce a tool_call arg value into a Python dict.

    Mirrors ``_coerce_to_list`` for ``params`` / ``labels``. Same root
    cause: qwen-style models emit ``params="{\\"percent\\":\\"80\\"}"``
    instead of structured ``params={"percent":"80"}``. The previous
    extractor did ``(args.get("params") or {}).items()``, which on a
    string value invoked ``str.items()`` → ``AttributeError`` and
    blew up the entire turn.

    Resolution order:
      1. ``None`` / missing → ``{}``
      2. real dict → defensive copy
      3. JSON-stringified object (``"{...}"``) → parse, return dict
      4. anything else → ``{}`` (with a debug log; we deliberately do
         NOT try to be clever about "key=value" pairs because the
         caller has no way to validate the shape)

    Returns ``{}`` rather than raising so a malformed arg degrades to
    "missing field" — the programmatic fallback (
    ``_merge_known_params_into_fault_intent``) can still recover the
    real values from the dialogue history.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {}
        if s.startswith("{") and s.endswith("}"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return parsed
            except (ValueError, TypeError):
                logger.debug(
                    "submit_fault_intent.%s came in as JSON-shaped string "
                    "but failed to parse: %r — falling back to empty dict",
                    field_name,
                    s[:120],
                )
        return {}
    return {}


# ---------------------------------------------------------------------------
# Pydantic BeforeValidator wrappers for submit_fault_intent
#
# These run BEFORE Pydantic checks the declared type on a tool_call arg.
# They convert a JSON-stringified list/dict into the real Python type so
# the ``list[str]`` / ``dict[str, str]`` annotations succeed.
#
# Without them, LLM-emitted ``names='["a"]'`` / ``params='{"k":"v"}'``
# (a known qwen-class quirk) would fail schema validation at the
# ``@lc_tool`` boundary with
#   ``Input should be a valid list``
#   ``Input should be a valid dictionary``
# and the LLM would never get to call the tool — observed in
# sess_27ec8f3ef6b2 where a single submit_fault_intent attempt was
# rejected and the dialogue ended without recovery.
#
# Empty container -> None preserves the "field omitted" semantics so
# downstream consumers that distinguish None from {} / [] (e.g.
# fault_spec validators) see no surprising change from the pre-coerce
# default.
# ---------------------------------------------------------------------------


def _validate_names(v):
    if v is None:
        return None
    coerced = _coerce_to_list(v, "names")
    return coerced if coerced else None


def _validate_labels(v):
    if v is None:
        return None
    coerced = _coerce_to_dict(v, "labels")
    return coerced if coerced else None


def _validate_params(v):
    if v is None:
        return None
    coerced = _coerce_to_dict(v, "params")
    return coerced if coerced else None


# ---------------------------------------------------------------------------
# Real tool: submit_fault_intent (executed by ToolNode, produces ToolMessage)
# ---------------------------------------------------------------------------

@lc_tool
def submit_fault_intent(
    fault_type: str,
    scope: str,
    target: str,
    action: str,
    namespace: str = "default",
    names: Annotated[Optional[list[str]], BeforeValidator(_validate_names)] = None,
    labels: Annotated[Optional[dict[str, str]], BeforeValidator(_validate_labels)] = None,
    params: Annotated[Optional[dict[str, str]], BeforeValidator(_validate_params)] = None,
    user_description: str = "",
) -> str:
    """Submit the collected fault injection intent for execution.

    Call this ONLY after:
    1. All required parameters are confirmed in dialogue with the user.
    2. You have shown a complete intent summary in your last reply.
    3. The user explicitly approved (said "执行" / "确认" / "开始" / "go" etc.).

    Pass every field you've derived from the dialogue — do NOT leave them
    blank expecting the system to re-extract them from chat history. The
    structured args you submit here are the source of truth that drives
    the downstream confirmation card and the inject pipeline.

    The (scope, target, action) triple mirrors the ChaosBlade command
    model ``blade create <scope> <target> <action> --<flag>=<value>``.
    Treat the values listed below as *common* examples, NOT a closed
    enum — ChaosBlade supports many more (e.g. action="drop" / "fill" /
    "occupy"; target="dns" / "file" / "jvm" / "kubelet").
    The authoritative set of legal triples and their required ``params``
    flags is published by ``read_skill_resource``; consult it BEFORE
    calling this tool whenever you're unsure about the fault you're
    about to submit.

    Args:
        fault_type: Composite identifier — by convention the dash-joined
                    triple ``"<scope>-<target>-<action>"``, e.g.
                    ``"node-cpu-fullload"`` / ``"pod-network-drop"``.
                    Acts as the human-readable label on the confirm
                    card. Required.
        scope: K8s resource family the fault attaches to. Common values:
               ``"pod"``, ``"node"``, ``"container"``. ChaosBlade also
               supports workload kinds (deployment / statefulset / ...)
               via skills. Required.
        target: Subsystem under attack. Common values: ``"cpu"`` /
                ``"mem"`` / ``"network"`` / ``"disk"`` / ``"process"``;
                skill catalogue may expose more (``"dns"`` /
                ``"file"`` / ``"jvm"`` / ...). Required.
        action: Concrete fault action. Common values vary per target:
                cpu→``"fullload"``; mem→``"load"``; network→``"drop"``
                / ``"dns"`` / ``"occupy"`` (v1.8.0 only supports these three);
                disk→``"burn"`` / ``"fill"``;
                process→``"kill"`` / ``"stop"``. Required.
        namespace: K8s namespace. Defaults to ``"default"`` — node-scope
                   faults conventionally use ``"default"`` and the user
                   rarely says so explicitly.
        names: Specific resource names (pods/nodes). Pass when the user
               named a target.
        labels: Label selector dict like ``{"app": "nginx"}``.
        params: Fault-type-specific flags. The keys depend on the
                ``(scope, target, action)`` triple — read the skill
                spec for the canonical set. Common shapes:
                  - cpu-fullload: ``{"percent": "80",
                    "timeout": "600"}``
                  - network-drop: ``{"interface": "eth0"}``
                    (drops all packets; no --percent support)
                  - disk-fill: ``{"path": "/data", "size": "10000"}``
                  - process-kill: ``{"process": "nginx",
                    "signal": "9"}``
                Values must be strings.
        user_description: User's original natural-language request.

    Returns:
        Acknowledgment string consumed by the dialogue gateway.
    """
    return "✓ 故障注入意图已提交，正在进入执行确认阶段。"


@lc_tool
async def query_active_experiments() -> str:
    """Query currently active fault experiments that can be recovered.

    Call this when the user wants to recover/undo a fault injection.
    Returns a list of recoverable experiments with their task_id, fault_type,
    and namespace. Use the task_id from the results when calling
    classify_intent(intent="recover", recover_task_id="task-xxx").
    """
    from chaos_agent.persistence.task_store import get_task_store
    store = await get_task_store()
    active = await store.query_active()
    if not active:
        return "当前没有活跃的故障注入实验，无需恢复。"
    lines = [f"当前有 {len(active)} 个可恢复的活跃实验:"]
    for i, t in enumerate(active[:10], 1):
        tid = t.get("task_id", "?")
        fault = t.get("skill") or t.get("skill_name") or "?"
        ns = (t.get("target") or {}).get("namespace", "?")
        lines.append(f"  {i}. task_id={tid}, fault_type={fault}, namespace={ns}")
    return "\n".join(lines)


def _extract_submit_args(messages: list) -> dict:
    """Pull the most recent submit_fault_intent tool_call args from history.

    LangGraph executes the tool, then routes back to this node with a
    ToolMessage trailing the source AIMessage. Walk backwards skipping
    ToolMessages until the owning AIMessage; if it carries a
    submit_fault_intent call, return the args dict normalised. Returns
    ``{}`` when no structured args are present (older qwen models that
    still call with no args, or schema-mismatch cases) — caller falls
    back to programmatic regex extraction.

    Normalisation (see ``_coerce_to_list`` / ``_coerce_to_dict``):
      * ``names``  — list or JSON-stringified list or single string
                     all collapse to ``list[str]``.
      * ``labels`` / ``params`` — dict or JSON-stringified dict
                     collapse to ``dict``; values stringified.
      * Scalar strings (``fault_type`` / ``scope`` / ``target`` /
        ``action`` / ``namespace`` / ``user_description``) — coerced
        through ``str(...) or ""`` so a stray int / None won't crash
        downstream string formatting.
      * Empty / missing fields are filled with empty string / list /
        dict, never ``None``, so callers can rely on uniform shape.
    """
    def _scalar_str(value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)

    for msg in reversed(messages):
        msg_type = getattr(msg, "type", "")
        if msg_type == "tool":
            continue  # ToolNode result, not the source AI message
        if msg_type == "ai":
            for tc in getattr(msg, "tool_calls", None) or []:
                if tc.get("name") != "submit_fault_intent":
                    continue
                args = tc.get("args") or {}
                names_list = _coerce_to_list(args.get("names"), "names")
                labels_dict = _coerce_to_dict(args.get("labels"), "labels")
                params_dict = _coerce_to_dict(args.get("params"), "params")
                return {
                    "fault_type": _scalar_str(args.get("fault_type")),
                    "scope": _scalar_str(args.get("scope")),
                    "target": _scalar_str(args.get("target")),
                    "action": _scalar_str(args.get("action")),
                    "namespace": _scalar_str(args.get("namespace")),
                    "names": [_scalar_str(n) for n in names_list if n not in (None, "")],
                    "labels": {
                        _scalar_str(k): _scalar_str(v)
                        for k, v in labels_dict.items()
                    },
                    "params": {
                        _scalar_str(k): _scalar_str(v)
                        for k, v in params_dict.items()
                    },
                    "user_description": _scalar_str(args.get("user_description")),
                }
            # AIMessage without a submit call — older turn we don't
            # care about; abandon the walk.
            return {}
        # SystemMessage / HumanMessage means we walked past the
        # current AI turn boundary without finding a submit call.
        return {}
    return {}


_INTENT_CONTENT_FALLBACKS = {
    "recover": "好的，正在为您恢复实验。",
    "chat": "好的,随时回来找我。",
    "inject": "好的，已记下你的需求，请确认下面的故障注入意图。",
    # "unset" is not a terminal intent — the user is still in dialogue.
    # Use a neutral acknowledgment that doesn't repeat the generic template.
    "unset": "好的，我继续帮你确认参数。",
}

# Heuristic patterns that indicate a reasoning/metacognition paragraph
# leaking into content. Qwen enable_thinking sometimes puts reasoning
# text directly in content instead of additional_kwargs.reasoning_content.
_REASONING_STRIP_PATTERNS = (
    # "所有必填项已收集完毕，现在应该调用 submit_fault_intent 提交实验请求。"
    # Self-referential LLM planning: mentions calling/submitting internal
    # tools like submit_fault_intent, classify_intent, kubectl. Real
    # LLMs never tell the user "I'm about to call submit_fault_intent".
    r"^.{0,6}(?:所有|全部|必填|必要|已收集|已确认|已获).*?(?:应该|需要|现在|接下来|下一步).{0,30}(?:调用|提交|使用|执行|触发).{0,50}(?:submit_fault_intent|classify_intent|kubectl|activate_skill|blade).{0,30}$",
    # "用户提供了节点名称 cms-node-1"
    # The LLM narrating the user's actions (meta-reasoning, NOT user-facing)
    r"^.{0,6}(?:用户|user).{0,30}(?:提供|给出|说|输入|mentioned|provided).{0,40}(?:节点|namespace|pod|参数|标签|label|target).{0,30}$",
)


def _strip_reasoning_from_content(content: str) -> str:
    """Remove reasoning/metacognition paragraphs from LLM content.

    When Qwen enable_thinking puts reasoning in content (not
    reasoning_content), the text typically starts with self-referential
    planning statements like "所有必填项已收集完毕，现在应该调用...".
    These paragraphs precede the actual user-facing reply.

    Strategy: split content into paragraphs, strip any that match
    reasoning patterns, return the remaining text. If ALL paragraphs
    are reasoning, return "" (triggers fallback in _ensure_visible_content).
    """
    if not content or not content.strip():
        return content

    import re
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    clean = []
    for para in paragraphs:
        # Check first line of each paragraph against reasoning patterns
        first_line = para.split("\n")[0].strip()
        is_reasoning = False
        for pattern in _REASONING_STRIP_PATTERNS:
            if re.match(pattern, first_line, re.IGNORECASE):
                is_reasoning = True
                break
        if not is_reasoning:
            clean.append(para)

    result = "\n\n".join(clean)
    return result


def _ensure_visible_content(response, intent: str = "") -> str:
    """Return a non-empty, *user-friendly* reply.

    Resolution order:

    1. ``response.content`` if non-empty — the LLM gave a normal answer.
       But strip reasoning/metacognition paragraphs first (Qwen
       enable_thinking sometimes leaks them into content).
    2. Templated fallback keyed by the classified intent.

    No reasoning_content fallback — reasoning is internal LLM thinking
    that should never be exposed to the user. The session file captures
    it separately via _filter_internal_tools_raw for auditability, but
    the TUI channel always uses content or a clean template.
    """
    content = getattr(response, "content", "") or ""
    if isinstance(content, str) and content.strip():
        stripped = _strip_reasoning_from_content(content)
        if stripped.strip():
            return stripped
        # All paragraphs were reasoning — fall through to template
    return _INTENT_CONTENT_FALLBACKS.get(
        intent, "好的，请继续告诉我你的需求。"
    )


# ---------------------------------------------------------------------------
# Execution keywords for forced-submit guard
# ---------------------------------------------------------------------------
_EXECUTION_KEYWORDS = frozenset({
    "开始", "执行", "确认", "好的", "可以", "go", "run",
    "就这样", "没问题", "可以了", "是的", "对", "没错",
    "确认执行", "开始吧", "执行吧",
})


def _filter_internal_tools_from_response(response, intent: str = "") -> AIMessage:
    """Remove classify_intent from AIMessage.

    classify_intent is parsed by the node; ToolNode processes all real tools
    (kubectl, activate_skill, submit_fault_intent, etc.).
    Also guarantees a non-empty content so the user always sees a reply.
    Used for the LangGraph state (ToolNode routing) and TUI display.
    """
    filtered_tc = [
        tc for tc in (getattr(response, "tool_calls", None) or [])
        if tc.get("name") not in ("classify_intent",)
    ]
    kwargs = {}
    original_id = getattr(response, "id", None)
    if original_id:
        kwargs["id"] = original_id
    return AIMessage(
        content=_ensure_visible_content(response, intent=intent),
        tool_calls=filtered_tc,
        response_metadata=getattr(response, "response_metadata", {}) or {},
        **kwargs,
    )


def _filter_internal_tools_raw(response) -> AIMessage:
    """Remove only classify_intent, preserving raw content.

    Unlike _filter_internal_tools_from_response, this keeps the original
    content and reasoning_content intact — no fallback substitution.
    Used for session file persistence where we want the complete LLM
    output (including kubectl/submit_fault_intent tool_calls and original
    text/reasoning).
    """
    filtered_tc = [
        tc for tc in (getattr(response, "tool_calls", None) or [])
        if tc.get("name") not in ("classify_intent",)
    ]
    kwargs = {}
    original_id = getattr(response, "id", None)
    if original_id:
        kwargs["id"] = original_id
    # Preserve original content — session file should capture what the LLM
    # actually said, not a sanitized fallback
    content = getattr(response, "content", "") or ""
    additional_kwargs = getattr(response, "additional_kwargs", None) or {}
    if additional_kwargs:
        kwargs["additional_kwargs"] = additional_kwargs
    return AIMessage(
        content=content,
        tool_calls=filtered_tc,
        response_metadata=getattr(response, "response_metadata", {}) or {},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Programmatic parameter extraction (supplementary to LLM tool calls)
# ---------------------------------------------------------------------------

# Keywords for lightweight extraction from HumanMessage text.
# This is NOT the primary extraction path — LLM via submit_fault_intent
# is the primary path. This function only ensures intermediate rounds
# don't lose parameters the user already mentioned.

_SCOPE_KEYWORDS = {
    "pod": ("pod", "pods", "容器"),
    "node": ("node", "nodes", "节点"),
    "container": ("container", "容器实例"),
}

_TARGET_KEYWORDS = {
    "cpu": ("cpu", "处理器", "CPU"),
    "mem": ("内存", "mem", "memory", "MEM"),
    "network": ("网络", "network", "net", "Network"),
    "disk": ("磁盘", "disk", "Disk"),
    "process": ("进程", "process", "Process"),
}

_ACTION_MAP = {
    "cpu": "fullload",
    "mem": "load",
    "network": "drop",
    "disk": "burn",
    "process": "kill",
}

_SCOPE_KEYWORD_EXCLUSIONS = frozenset(
    kw.lower() for keywords in _SCOPE_KEYWORDS.values() for kw in keywords
)

_OVERRIDE_SIGNALS = ("改成", "换成", "修改为", "改为", "调整")


def _extract_scope(text: str) -> str:
    """Extract scope keyword from user text."""
    lower = text.lower()
    for scope, keywords in _SCOPE_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in lower:
                return scope
    return ""


def _extract_target_keyword(text: str) -> str:
    """Extract target keyword from user text."""
    lower = text.lower()
    for target, keywords in _TARGET_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in lower:
                return target
    return ""


def _extract_namespace(text: str) -> str:
    """Extract namespace from text.

    Matches both explicit flag formats (-n, --namespace) and
    conversational formats (AI ack: "命名空间/namespace 已确认为 xxx",
    user reply: just a bare namespace name like "cms-demo").

    Priority order: explicit flag > conversational ack > bare name.
    """
    import re
    # 1. Explicit flags: -n <ns>, --namespace <ns>, namespace=<ns>, namespace:<ns>
    m = re.search(
        r'(?:-n|--namespace)[\s=]+([a-zA-Z0-9][-a-zA-Z0-9_.]*)'
        r'|namespace[:=]\s*([a-zA-Z0-9][-a-zA-Z0-9_.]*)',
        text, re.IGNORECASE
    )
    if m:
        return m.group(1) or m.group(2) or ""

    # 2. Conversational ack: "命名空间/namespace 已确认为 xxx",
    #    "namespace is xxx", "namespace: xxx".
    #
    # The label itself may be wrapped in 0-2 ``*`` chars from Markdown
    # bold (LLM bullets like ``* **命名空间**：default``), and the
    # separator can be a Chinese full-width colon (``：``) since the
    # LLM mixes scripts inside the same paragraph. The earlier version
    # of this regex required ``命名空间`` to be followed *immediately*
    # by ``\s*(?:已确认为|...|:)`` — markdown-bold ``**`` between the
    # label and the colon, plus ``：`` instead of ``:``, both made
    # the match silently fail. That single failure cascaded into the
    # whole "TS TUI never shows the intent-confirm card" bug:
    #   - namespace not extracted from the AI summary
    #   - fast-path `required` check fails (4-field gate)
    #   - intent_clarification falls through to a second LLM call
    #   - second call returns pure text ("意图已提交...") with no
    #     tool_calls → router takes the END branch → graph terminates
    #     before reaching ``intent_confirm`` → server emits no
    #     ``confirm`` event → user sees a turn that just stops.
    # Pattern below tolerates ``**`` wrappers, ``：``/``为`` separators
    # and arbitrary whitespace in between.
    m = re.search(
        r'\*{0,2}(?:命名空间|namespace)\*{0,2}'
        r'\s*(?:已确认为|确认为|is|confirmed\s+as|为|[:：])\s*'
        r'[*`]*([a-zA-Z0-9][-a-zA-Z0-9_.]*)[*`]*',
        text, re.IGNORECASE
    )
    if m:
        return m.group(1) or ""

    # 3. Bare name fallback: only when the entire text is a plausible
    #    *namespace* (DNS-1123 label, no dots).
    #
    # Why "no dots": real k8s namespace names match
    # ``[a-z0-9]([-a-z0-9]*[a-z0-9])?`` — they do NOT contain dots.
    # Node names commonly DO contain dots (e.g. ``cn-hongkong.10.0.1.101``).
    # Without the dot exclusion the previous version captured a node
    # name as namespace, which propagated through write-once-with-override
    # merging and made the Confirmed Parameters block lie to the LLM —
    # observed as the agent re-asking for scope after the user had
    # already answered "node".
    #
    # We also exclude scope keywords (``pod`` / ``node`` / ``容器``) for
    # the same reason ``_extract_names`` does — a single-word answer
    # to the scope question is NEVER a namespace.
    stripped = text.strip()
    if (
        re.fullmatch(r'[a-zA-Z0-9][-a-zA-Z0-9_]*', stripped)  # no dots
        and not any(kw in stripped.lower() for kw in _EXECUTION_KEYWORDS)
        and stripped.lower() not in _SCOPE_KEYWORD_EXCLUSIONS
    ):
        return stripped

    return ""


def _extract_names(text: str, *, source: str = "any") -> list[str]:
    """Extract K8s resource names from message text.

    The ``source`` parameter is the speaker of ``text``:

      ``"human"``  — User-typed input (HumanMessage). Users may legitimately
                     name multiple resources in one message ("X 和 Y" /
                     "node1 node2"), so a greedy multi-match is correct.

      ``"ai"``     — Agent reply (AIMessage). The agent typically lists
                     candidate resources for the user to pick from
                     ("以下是集群中的节点列表：- X - Y - Z"). Greedy
                     multi-match here is **destructive**: it sucks every
                     listed candidate into ``fault_intent.names``, which
                     downstream prompt rendering surfaces as
                     "names already confirmed (12 items)" — directly
                     contradicting the user's actual choice and forcing
                     the LLM to reconcile two truths in
                     ``reasoning_content``.

                     For ``ai`` we only honour the explicit ack pattern
                     ("目标节点已确认为 X" / "**target node**: X") which
                     is bounded to a single name. Bare-name regex
                     scanning is suppressed entirely.

      ``"any"``    — Back-compat default for callers that don't track
                     the source (kept identical to the pre-fix behaviour
                     so direct callers don't break).

    The change here is the production fix for a real session crash where
    the agent listed 12 nodes, ``_extract_names`` greedily harvested all
    12, and the resulting Confirmed Parameters block told the LLM "12
    nodes were already confirmed" while the user had picked exactly one.

    Matches node names (``cn-hongkong.10.0.1.101``), pod names, and
    Markdown-decorated AI ack patterns (``**目标节点**：X``).
    """
    import re
    names = []

    # 1. AI ack: "目标节点已确认为 xxx", "node cn-hongkong.xxx", "pod my-pod"
    #    Tolerates Markdown bold around the label (``**目标节点**：xxx``)
    #    and the Chinese full-width colon ``：``, mirroring the namespace
    #    extractor — same root cause, same fix. Honoured for every source
    #    because the pattern itself bounds to one name.
    m = re.search(
        r'\*{0,2}(?:目标节点|节点名|node\s+name|pod名|target\s+node)\*{0,2}'
        r'\s*(?:已确认为|确认为|is|confirmed\s+as|为|[:：])\s*'
        r'[*`]*([a-zA-Z0-9][-a-zA-Z0-9_.]*)[*`]*',
        text, re.IGNORECASE
    )
    if m:
        names.append(m.group(1))

    # AI messages stop here: an AIMessage without an explicit ack pattern
    # is most likely a list display of candidate resources (the dump
    # following ``kubectl get nodes``), and harvesting names out of
    # those would poison the prompt with the user's options instead of
    # their choice.
    if source == "ai":
        return names

    # 2. Bare K8s resource name: node-style (cn-hongkong.xxx),
    #    pod-style, or simple DNS label. Only run for "human" / "any"
    #    sources — see the source contract above.
    stripped = text.strip()
    # Match node-style names with dots (like cn-hongkong.10.0.1.101)
    m = re.findall(r'cn-[a-z]+(?:\.[\d]+){3,4}', stripped)
    if m:
        for n in m:
            if n not in names:
                names.append(n)
    # Match simple DNS label names (single word, K8s format)
    # Only if entire text is a bare name, not an execution keyword,
    # and not a scope keyword (prevents "node"/"pod" from being captured as names)
    if (
        not names
        and re.fullmatch(r'[a-zA-Z0-9][-a-zA-Z0-9_.]*', stripped)
        and not any(kw in stripped.lower() for kw in _EXECUTION_KEYWORDS)
        and stripped.lower() not in _SCOPE_KEYWORD_EXCLUSIONS
    ):
        names.append(stripped)

    return names


def _extract_params_from_text(text: str) -> dict:
    """Extract numeric parameters from user text (percent, timeout/duration)."""
    import re
    params = {}
    # percent: "80%", "percent 80", "强度 90"
    m = re.search(r'(\d+)%|percent\s+(\d+)|强度\s+(\d+)|占用\s+(\d+)', text, re.IGNORECASE)
    if m:
        params["percent"] = str(m.group(1) or m.group(2) or m.group(3) or m.group(4))

    # timeout/duration: "600s", "600秒", "timeout 600", "时间 600"
    m = re.search(r'(\d+)\s*(?:s|秒|seconds)|timeout\s+(\d+)|时间\s+(\d+)|duration\s+(\d+)', text, re.IGNORECASE)
    if m:
        params["timeout"] = str(m.group(1) or m.group(2) or m.group(3) or m.group(4))

    return params


def _merge_slot(merged: dict, key: str, value, is_override: bool = False) -> None:
    """Write-once-with-override: existing fields only updatable in override mode."""
    if value and (not merged.get(key) or is_override):
        merged[key] = value


def _merge_known_params_into_fault_intent(
    messages: list, fault_intent: dict
) -> dict:
    """Programmatically merge known parameters from dialogue history into fault_intent.

    Structural analogue of agent_loop's _extract_target_from_kubectl_get:
    LLM is the primary extraction path (via submit_fault_intent tool call).
    This function is a supplementary path — ensuring intermediate rounds
    don't lose parameters the user already mentioned.

    write-once-with-override:
    - Normal mode: existing confirmed fields are not overwritten (prevents
      re-asking for already-answered parameters).
    - Override mode: user says "改成 X" / "换成 Y" → allows updating
      previously confirmed fields.
    """
    merged = dict(fault_intent)

    # Scan both HumanMessage and AIMessage for confirmed parameters.
    # HumanMessage: user provides values directly (e.g. "cms-demo", "-n default").
    # AIMessage: AI acknowledges confirmed parameters (e.g. "命名空间已确认为 cms-demo")
    #   — these are critical because _extract_namespace can extract from
    #   conversational ack patterns, and AI acks are the canonical confirmation.
    for msg in reversed(messages):
        if not isinstance(msg, (HumanMessage, AIMessage)):
            continue
        text = msg.content.strip()
        if not text:
            continue
        is_override = any(s in text for s in _OVERRIDE_SIGNALS)

        _merge_slot(merged, "scope", _extract_scope(text), is_override)
        _merge_slot(merged, "target", _extract_target_keyword(text), is_override)

        # Derive default action from target (cpu→fullload, mem→load, etc.)
        if merged.get("target") and not merged.get("action"):
            merged["action"] = _ACTION_MAP.get(merged["target"], "")

        _merge_slot(merged, "namespace", _extract_namespace(text), is_override)

        # Merge names (target node/pod names). Pass the message
        # source so ``_extract_names`` can suppress greedy bare-name
        # findall on AIMessages — see that helper's docstring for the
        # data-poisoning case it defends against (agent lists 12
        # candidate nodes; user picks 1; greedy match would otherwise
        # commit all 12 into fault_intent.names).
        source = "human" if isinstance(msg, HumanMessage) else "ai"
        extracted_names = _extract_names(text, source=source)
        if extracted_names:
            existing_names = merged.get("names") or []
            confirmed_ns = merged.get("namespace", "")
            for n in extracted_names:
                if n == confirmed_ns:
                    continue  # Already captured as namespace, skip
                if n not in existing_names:
                    existing_names.append(n)
            merged["names"] = existing_names

        # Merge numeric params (percent, timeout) into params dict
        text_params = _extract_params_from_text(text)
        if text_params:
            existing_params = merged.get("params") or {}
            for pk, pv in text_params.items():
                if pk not in existing_params or is_override:
                    existing_params[pk] = pv
            merged["params"] = existing_params

    return merged


# ---------------------------------------------------------------------------
# Node factory
# ---------------------------------------------------------------------------

def _build_dialogue_persist_list(
    messages: list,
    response=None,
    system_msg: SystemMessage | None = None,
    human_msg=None,
    dialogue_round: int = 0,
) -> list:
    """Build the complete list of messages to persist to the session file.

    The session file should capture the full intent clarification dialogue:
    - System prompt (for auditability — what context the LLM saw)
    - HumanMessage (user input for this turn)
    - ToolMessage (kubectl/activate_skill results from previous ReAct steps)
    - AIMessage (raw — internal schema tools removed, but kubectl tool_calls
      and original content/reasoning_content preserved)

    This is different from _filter_internal_tools_from_response which also
    substitutes empty content with fallback text. The session file gets the
    raw version for completeness; the TUI/LangGraph state gets the filtered
    version for user readability.

    ToolMessages from previous ReAct iterations are extracted from state.messages.
    The session store's dedup (ID-based) handles already-written messages.
    """
    persist = []

    # 1. System prompt (with synthetic ID for dedup — one per dialogue_round)
    if system_msg is not None:
        ic_system = SystemMessage(
            content=f"[Intent Clarification Prompt]\n{system_msg.content}",
            id=f"ic-system-round-{dialogue_round}",
        )
        persist.append(ic_system)

    # 2. HumanMessage from current turn
    if human_msg is not None:
        persist.append(human_msg)

    # 3. ToolMessages from state (kubectl/activate_skill results from
    #    previous ReAct iterations within this node invocation)
    for msg in messages:
        if isinstance(msg, ToolMessage):
            _kw = getattr(msg, "additional_kwargs", None) or {}
            if not _kw.get(NO_SESSION_MARKER):
                persist.append(msg)

    # 4. Raw AIMessage (internal schema tools removed, kubectl kept,
    #    original content/reasoning preserved)
    if response is not None:
        persist.append(_filter_internal_tools_raw(response))

    return persist


def _persist_dialogue(tui_session_id: str, messages: list) -> None:
    """Persist filtered dialogue messages directly to the session file.

    This is the **sole write source** for the TUI session file during
    intent clarification. It captures the full dialogue exchange:
    system prompt + HumanMessage + ToolMessage + filtered AIMessage.

    The PreReasoningHook no longer writes to the session file during
    intent clarification (confirmed_intent=None/"unset"), eliminating
    the double-write bug. ID-based dedup in the session store handles
    messages that were already written in previous node invocations.
    """
    if not tui_session_id:
        return
    try:
        from chaos_agent.memory.tui_session_store import (
            get_global_tui_session_store,
        )
        store = get_global_tui_session_store()
        if store is not None:
            store.append_dialogue(tui_session_id, messages)
    except Exception as e:
        logger.debug(f"Dialogue persistence skipped: {e}")


def make_intent_clarification(llm=None, tools: list = None, hook=None):
    """Create the intent_clarification node function.

    Args:
        llm: LangChain LLM instance.
        tools: ToolNode tools (kubectl, activate_skill, read_skill_resource).
        hook: Optional PreReasoningHook for memory compaction.
    """
    async def intent_clarification(state: AgentState) -> dict:
        messages = state.get("messages", [])
        confirmed_intent = state.get("confirmed_intent")
        clarification_round = state.get("clarification_round", 0)
        dialogue_round = state.get("dialogue_round", 0)
        task_id = state.get("task_id", "")
        tui_session_id = state.get("tui_session_id", "")

        # Extract the current turn's HumanMessage for session persistence.
        # This is the user-visible input that pairs with the AI response
        # in _persist_dialogue calls. Search from the end to find the
        # most recent HumanMessage (converse_stream adds exactly one per turn).
        current_human_msg = None
        if messages:
            for msg in reversed(messages):
                if getattr(msg, "type", "") == "human":
                    _kw = getattr(msg, "additional_kwargs", None) or {}
                    if not _kw.get(NO_SESSION_MARKER):
                        current_human_msg = msg
                        break

        tracker = get_tracker(task_id) if task_id else None
        if tracker:
            tracker.start(StatusCategory.NODE, "intent_clarification",
                          "正在与用户对话...")

        # Already confirmed → pass through to the router, which will
        # direct to the appropriate downstream node (agent_loop for
        # inject, save_memory for chat, recover_handler for recover).
        if confirmed_intent in ("inject", "chat", "recover"):
            if tracker:
                tracker.complete(f"意图已确认: {confirmed_intent}")
            return {}

        # "unset" means intent was partially converged in a previous turn
        # (e.g. fault_intent has some parameters) but the user hasn't
        # fully confirmed yet. Continue the dialogue rather than
        # short-circuiting. LLM will see the already-collected
        # fault_intent fields in the system prompt dynamic section
        # and won't re-ask for them.
        if confirmed_intent == "unset":
            logger.info("Intent partially converged (unset), continuing dialogue")

        if llm is None:
            if tracker:
                tracker.complete("LLM 不可用，默认 chat")
            return {"confirmed_intent": "chat"}

        # Safety net: overall dialogue limit
        if dialogue_round >= MAX_DIALOGUE_ROUNDS:
            logger.warning("Dialogue round %d >= %d, forcing exit",
                           dialogue_round, MAX_DIALOGUE_ROUNDS)
            if tracker:
                tracker.complete("对话轮数超限")
            return {
                "confirmed_intent": "chat",
                "dialogue_round": dialogue_round + 1,
                "messages": [AIMessage(content="感谢使用 Blade AI！如有需要随时回来，再见！")],
            }

        # Memory compaction
        hook_updates = {}
        if hook:
            hook_updates = await hook(state) or {}

        # Build system prompt via section-based builder (U-shaped:
        # CRITICAL rules at beginning + end, dynamic completeness
        # signal + confirmed parameters below CACHE_BOUNDARY).
        #
        # The mid-conversation fault intent now lives on
        # ``state.fault_spec`` (entry-point placeholder or previous
        # turn's confirmed spec). Internal merge logic still works on
        # the legacy ``fault_intent`` dict shape, so we project the
        # spec through ``to_intent_dict()`` for that merge — return
        # paths below then convert the merged dict back to a spec.
        existing_spec = read_fault_spec(state)
        fault_intent_existing = (
            existing_spec.to_intent_dict() if existing_spec else {}
        )

        # --- Fast-path: detect submit_fault_intent completion from ToolNode ---
        # If any trailing ToolMessage (from the most recent ToolNode batch)
        # is from submit_fault_intent, the model successfully called it in
        # the previous ReAct iteration. Skip LLM call and transition directly
        # to confirmed_intent="inject".
        # Check all trailing ToolMessages (ToolNode may process multiple
        # tool_calls in one batch, e.g. kubectl + submit_fault_intent).
        has_submit_tool_msg = False
        if messages:
            for msg in reversed(messages):
                msg_type = getattr(msg, "type", "")
                if msg_type == "tool":
                    if getattr(msg, "name", "") == "submit_fault_intent":
                        has_submit_tool_msg = True
                        break
                else:
                    # Stop at first non-ToolMessage (e.g. AIMessage)
                    break
        if has_submit_tool_msg:
            # Layered merge of three sources, lowest priority first:
            #   1. fault_intent_existing — what previous turns confirmed.
            #   2. fallback              — programmatic regex extraction
            #                              from message history. Only used
            #                              to fill gaps; no longer the
            #                              primary path.
            #   3. llm_args              — what the LLM just submitted via
            #                              the structured submit_fault_intent
            #                              tool call. Wins on every non-
            #                              empty field so a confident model
            #                              can override a stale fallback
            #                              value (e.g. when the user changed
            #                              namespace mid-dialogue and the
            #                              regex extractor anchored on the
            #                              old value).
            #
            # Why this design: the previous version derived every field
            # by regex-walking the dialogue, which silently failed on any
            # markdown variation the LLM tried (``**命名空间**：default``
            # missed entirely until a regex patch). Letting the LLM fill
            # the structured args directly removes that dependency on
            # text shape. The regex path stays as a safety net so older
            # qwen builds that call submit_fault_intent with no/partial
            # args still work without regression.
            llm_args = _extract_submit_args(messages)
            fallback = _merge_known_params_into_fault_intent(
                messages, fault_intent_existing
            )
            updated_intent = {**fault_intent_existing, **fallback}
            for k, v in llm_args.items():
                if v in (None, "", [], {}):
                    continue
                updated_intent[k] = v

            # Convention: node-scope without explicit namespace → "default".
            # The tool docstring already documents the default, but qwen
            # historically omits the field entirely when it accepts a
            # default; belt-and-braces keeps the required check happy.
            if (
                not updated_intent.get("namespace")
                and updated_intent.get("scope") == "node"
            ):
                updated_intent["namespace"] = "default"

            # Validate minimum required fields
            required = ("scope", "target", "action", "namespace")
            if all(updated_intent.get(k) for k in required):
                if tracker:
                    tracker.complete(
                        f"故障意图收敛: {updated_intent.get('scope')}-"
                        f"{updated_intent.get('target')} "
                        f"{updated_intent.get('action')} @ "
                        f"{updated_intent.get('namespace')}"
                    )
                # Persist dialogue (audit log on disk; happens regardless
                # of whether the user later approves or rejects the intent
                # in ``intent_confirm``).
                persist_list = _build_dialogue_persist_list(
                    messages, system_msg=None,
                    human_msg=current_human_msg,
                    dialogue_round=dialogue_round,
                )
                _persist_dialogue(tui_session_id, persist_list)
                # Birth the operational task_id here — this is the
                # transition point where the inject pipeline takes
                # over from clarification. Allocation is idempotent
                # (a previously-allocated ``task-<hex>`` is reused),
                # has no disk side effect, and keeping it here
                # preserves tracker continuity for the downstream
                # ``intent_confirm`` node (whose tracker is keyed on
                # ``state.task_id``).
                op_task_id = _allocate_operation_task_id(state.get("task_id", ""))
                # NOTE — Option A: intentionally NOT trimming messages
                # here, NOT building the IntentClarificationSummary, and
                # NOT calling ``bootstrap_task_session``. Those side
                # effects are deferred to ``intent_confirm``'s approved /
                # dry_run branches so a user-initiated rejection at the
                # confirm gate leaves the full clarification dialogue
                # intact for the next conversational turn (avoids the
                # "agent forgets the last 5 rounds after I said no"
                # surprise). The submit_fault_intent AIMessage and its
                # paired ToolMessage stay in ``state.messages`` and get
                # cleaned up wholesale by ``intent_confirm.approved``'s
                # trim.
                # Persist the converged intent as a FaultSpec — single
                # source of truth from this point on. Downstream
                # consumers (intent_confirm, agent_loop, safety_check,
                # baseline_capture, ...) read via ``read_fault_spec``.
                new_spec = FaultSpec.from_intent_args(
                    updated_intent, existing=existing_spec,
                )
                return merge_hook_updates({
                    "confirmed_intent": "inject",
                    "fault_spec": new_spec.to_dict(),
                    "intent_confidence": 1.0,
                    "intent_reasoning": "submit_fault_intent tool executed",
                    "dialogue_round": dialogue_round + 1,
                    "task_id": op_task_id,
                }, hook_updates)

        system_msg = SystemMessage(
            content=build_system_prompt(
                PromptMode.INTENT,
                fault_intent=fault_intent_existing,
            )
        )

        # --- Anti-stagnation detection (same mechanism as agent_loop) ---
        loop_hint = detect_repeated_tool_calls(messages)
        _, stagnant_tool = detect_action_stagnation(messages)

        intent_stagnation_hint = None
        if stagnant_tool:
            intent_stagnation_hint = (
                f"**ACTION_STAGNATION**: You have called `{stagnant_tool}` too many "
                f"consecutive times with no progress. This tool has been temporarily "
                f"removed. You MUST now either:\n"
                f"- Call `submit_fault_intent` if you have collected enough fault parameters.\n"
                f"- Use a DIFFERENT tool (activate_skill, read_skill_resource) for more info.\n"
                f"- Output a plain text response to conclude this conversation turn.\n"
                f"Do NOT attempt to call `{stagnant_tool}` again."
            )

        tools_this_iter = list(tools or [])
        if stagnant_tool:
            tools_this_iter = [
                t for t in tools_this_iter
                if getattr(t, "name", "") != stagnant_tool
                or getattr(t, "name", "") == "submit_fault_intent"
            ]

        function_schemas = [CLASSIFY_INTENT_TOOL]
        llm_bound = llm.bind_tools(function_schemas + tools_this_iter)

        llm_messages = [system_msg] + messages[-20:]
        if loop_hint:
            llm_messages.append(HumanMessage(content=loop_hint))
        if intent_stagnation_hint:
            llm_messages.append(HumanMessage(content=intent_stagnation_hint))

        try:
            if tracker:
                tracker.update("调用 LLM...")
            response = await llm_bound.ainvoke(llm_messages)
        except Exception as e:
            logger.error("Intent clarification LLM failed: %s", e)
            if tracker:
                tracker.fail(f"LLM 调用失败: {e}")
            return merge_hook_updates({
                "confirmed_intent": "chat",
                "messages": [AIMessage(content="抱歉，我遇到了一些问题。请稍后再试。")],
            }, hook_updates)

        tool_calls = getattr(response, "tool_calls", None) or []

        # --- Priority 1: classify_intent ---
        classification = _extract_classify_intent(tool_calls)
        if classification:
            intent = classification["intent"]

            if intent == "chat":
                # Multi-invocation model: classify_intent(chat) = user says goodbye
                if tracker:
                    tracker.complete("用户结束对话")
                filtered = _filter_internal_tools_from_response(response, intent="chat")
                persist_list = _build_dialogue_persist_list(
                    messages, response=response,
                    system_msg=system_msg,
                    human_msg=current_human_msg,
                    dialogue_round=dialogue_round,
                )
                _persist_dialogue(tui_session_id, persist_list)
                return merge_hook_updates({
                    "confirmed_intent": "chat",
                    "intent_confidence": classification["confidence"],
                    "messages": [filtered],
                }, hook_updates)
            else:
                # recover (the only non-chat option after the enum was narrowed)
                if tracker:
                    tracker.complete(f"意图路由: {intent}")
                filtered = _filter_internal_tools_from_response(response, intent=intent)
                # Dispatch only when LLM produced no text (fallback was used).
                # When the LLM DID produce text, on_chat_model_stream already
                # streamed those tokens — dispatching again would duplicate.
                raw_content = getattr(response, "content", "") or ""
                if not raw_content.strip():
                    try:
                        from chaos_agent.agent.dispatch import dispatch_node_message
                        await dispatch_node_message("intent_clarification", filtered.content)
                    except Exception:
                        pass
                persist_list = _build_dialogue_persist_list(
                    messages, response=response,
                    system_msg=system_msg,
                    human_msg=current_human_msg,
                    dialogue_round=dialogue_round,
                )
                _persist_dialogue(tui_session_id, persist_list)
                # Birth the operational task_id here — recover takes
                # over from clarification at this point.
                op_task_id = _allocate_operation_task_id(state.get("task_id", ""))
                # Register with the global SessionStore for the recover
                # pipeline's task file. Unlike inject there is no
                # ``IntentClarificationSummary`` SystemMessage to use
                # as the handoff boundary (recover doesn't carry the
                # same fault-intent fields), so the task file starts
                # empty — recover_handler / recover_verifier_loop
                # populate it via the standard append_messages path.
                bootstrap_task_session(
                    op_task_id,
                    operation="recover",
                    tui_session_id=tui_session_id,
                    handoff_message=None,
                )
                return merge_hook_updates({
                    "confirmed_intent": intent,
                    "intent_confidence": classification["confidence"],
                    "recover_task_id": classification.get("recover_task_id", ""),
                    "messages": [filtered],
                    "task_id": op_task_id,
                }, hook_updates)

        # --- Priority 2: has tool calls (kubectl, submit_fault_intent, etc.) ---
        has_tool_calls = any(
            tc.get("name") not in ("classify_intent",)
            for tc in tool_calls
        )
        if has_tool_calls:
            if tracker:
                tracker.complete("等待工具执行")
            filtered = _filter_internal_tools_from_response(response)
            updated_intent = _merge_known_params_into_fault_intent(
                messages, fault_intent_existing
            )
            persist_list = _build_dialogue_persist_list(
                messages, response=response,
                system_msg=system_msg,
                human_msg=current_human_msg,
                dialogue_round=dialogue_round,
            )
            _persist_dialogue(tui_session_id, persist_list)
            mid_spec = FaultSpec.from_intent_args(
                updated_intent, existing=existing_spec,
            )
            return merge_hook_updates({
                "messages": [filtered],
                "fault_spec": mid_spec.to_dict(),
                "clarification_round": clarification_round + 1,
                "dialogue_round": dialogue_round + 1,
            }, hook_updates)

        # --- Priority 3: pure text response (no tool calls at all) ---
        # Multi-invocation model: pure text = conversation turn done.
        # Router will see no confirmed_intent + no tool_calls → END.
        updated_intent = _merge_known_params_into_fault_intent(
            messages, fault_intent_existing
        )
        if tracker:
            tracker.complete("对话回复完成")
        safe_response = _filter_internal_tools_from_response(response)
        persist_list = _build_dialogue_persist_list(
            messages, response=response,
            system_msg=system_msg,
            human_msg=current_human_msg,
            dialogue_round=dialogue_round,
        )
        _persist_dialogue(tui_session_id, persist_list)
        mid_spec = FaultSpec.from_intent_args(
            updated_intent, existing=existing_spec,
        )
        return merge_hook_updates({
            "messages": [safe_response],
            "fault_spec": mid_spec.to_dict(),
            "dialogue_round": dialogue_round + 1,
        }, hook_updates)

    return intent_clarification
