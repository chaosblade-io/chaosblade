"""intent_clarification node — TUI conversational gateway.

A unified dialogue node that handles chat, cluster Q&A, capability Q&A,
recover routing, and fault-intent convergence in a single LLM call. The
LLM naturally transitions between modes based on conversation context:

1. **Chat / Q&A** — greetings, chitchat, environment questions through
   read-only discovery, and capability questions (read_skill_resource).
2. **Route** — explicit recover intent → recover_task(task_id=...).
3. **Converge** — fault injection intent (clear or vague) → identify the
   semantic fault family from the full skill catalog, probe current targets,
   then submit_fault_intent.

Multi-invocation model: each user message = independent graph invocation.
Pure text response = conversation turn done (graph ends, TUI waits for next input).
Only submit_fault_intent/submit_batch_intent/recover_task trigger state transitions.

CLI mode skips this node entirely: the Intent Graph is TUI-only, and the
Pipeline Graph enters at agent_loop via route_pipeline_start.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Literal, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool as lc_tool
from pydantic import BeforeValidator

from chaos_agent.agent.spec.fault_spec import (
    INTENT_ACTIONS,
    INTENT_SCOPES,
    INTENT_TARGETS,
    FaultSpec,
    parse_fault_proposal,
    read_fault_spec,
)
from chaos_agent.agent.nodes.execute.llm_step_helpers import (
    build_stagnation_hint,
    persist_corrective_hint,
    filter_stagnant_tool,
)
from chaos_agent.agent.nodes.execute.react_helpers import (
    detect_action_stagnation,
    detect_repeated_tool_calls,
)
from chaos_agent.agent.capabilities import (
    build_capability_context,
    build_intent_discovery_context,
    filter_tools_for_context,
)
from chaos_agent.agent.dispatch import dispatch_node_message
from chaos_agent.agent.spec.fault_registry import family_for_scope
from chaos_agent.agent.prompts.builders import build_system_prompt
from chaos_agent.agent.prompts.modes import PromptMode
from chaos_agent.persistence.task_identity import is_real_task_id, new_task_id
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.memory.hook import merge_hook_updates
from chaos_agent.memory.session_store import NO_SESSION_MARKER
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory
from chaos_agent.transports.registry import (
    profile_of,
    resolve_channel_name,
)

logger = logging.getLogger(__name__)

MAX_CLARIFICATION_ROUNDS = settings.max_clarification_rounds
MAX_DIALOGUE_ROUNDS = settings.max_dialogue_rounds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_successful_trailing_tool_result(messages: list, tool_name: str) -> bool:
    """Return whether the most recent ToolNode batch completed this tool.

    A ToolNode emits a ``ToolMessage`` even when argument validation or tool
    execution fails. Only a successful result may advance an intent into the
    executable pipeline; an error stays in the normal ReAct conversation so
    the model can repair and resubmit it.
    """

    for msg in reversed(messages):
        if getattr(msg, "type", "") != "tool":
            break
        if getattr(msg, "name", "") != tool_name:
            continue
        content = getattr(msg, "content", "")
        return not (
            getattr(msg, "status", None) == "error"
            or (isinstance(content, str) and content.startswith("Error:"))
        )
    return False


def _advance_fault_spec(existing: FaultSpec | None, raw: dict) -> FaultSpec:
    """Build the only persistent fault contract from an LLM proposal."""
    candidate = FaultSpec.from_intent_args(raw, existing=existing)
    if existing is None:
        return candidate.replace(revision=1)
    revision = existing.revision
    if candidate.contract_dict() != existing.contract_dict():
        revision += 1
    return candidate.replace(revision=revision)


def _submission_matches_spec(args: dict, spec: FaultSpec) -> bool:
    """Require execution arguments to match the reviewed FaultSpec exactly."""
    submitted = FaultSpec.from_intent_args(args, existing=spec)
    return (
        int(args.get("fault_revision", -1)) == spec.revision
        and submitted.scope == spec.scope
        and submitted.blade_target == spec.blade_target
        and submitted.blade_action == spec.blade_action
        and submitted.namespace == spec.namespace
        and submitted.names == spec.names
        and submitted.labels == spec.labels
        and submitted.params == spec.params
    )


def _bootstrap_submitted_spec(args: dict) -> FaultSpec | None:
    """Create the first durable contract from an approved structured submit.

    A proposal trailer is an optimisation for carrying the reviewed contract
    across dialogue turns, not a second safety boundary.  Some models can
    render a complete user-facing summary yet omit that private trailer.  In
    that case the final ``submit_fault_intent`` call is still structured,
    happens only after the model observed explicit approval, and is the most
    faithful available contract.  Bootstrap from those exact arguments rather
    than attempting to recover fields from natural-language history.
    """
    candidate = FaultSpec.from_intent_args(args)
    if not candidate.is_complete:
        return None
    return candidate.replace(revision=1)


def _stored_batch_specs(state: AgentState) -> list[FaultSpec]:
    """Read the reviewed batch as FaultSpecs, never as a second intent model."""
    payload = state.get("batch_submit_args") or {}
    faults = payload.get("faults") if isinstance(payload, dict) else None
    if not isinstance(faults, list):
        return []
    return [spec for item in faults if isinstance(item, dict)
            if (spec := FaultSpec.from_dict(item)) is not None]


def _submission_matches_batch(args: dict, specs: list[FaultSpec]) -> bool:
    """Require a batch submit call to replay the reviewed contracts exactly."""
    submitted_faults = args.get("faults")
    if not specs or not isinstance(submitted_faults, list):
        return False
    if len(submitted_faults) != len(specs):
        return False
    if int(args.get("fault_revision", -1)) != specs[0].revision:
        return False
    for submitted, spec in zip(submitted_faults, specs):
        if not isinstance(submitted, dict):
            return False
        candidate_args = {**submitted, "fault_revision": spec.revision}
        if not _submission_matches_spec(candidate_args, spec):
            return False
    return True


def _advance_proposed_specs(
    existing: FaultSpec | None,
    existing_batch: list[FaultSpec],
    raw_faults: list[dict],
) -> list[FaultSpec]:
    """Normalise the private proposal immediately into the canonical contract."""
    candidates: list[FaultSpec] = []
    for index, raw in enumerate(raw_faults):
        prior = existing if len(raw_faults) == 1 else (
            existing_batch[index] if index < len(existing_batch) else None
        )
        candidates.append(_advance_fault_spec(prior, raw))
    return candidates


def _proposal_state_update(specs: list[FaultSpec]) -> dict:
    """Persist one canonical FaultSpec or a serial list of canonical FaultSpecs."""
    if not specs:
        return {}
    if len(specs) == 1:
        return {"fault_spec": specs[0].to_dict(), "batch_submit_args": None}
    return {
        "fault_spec": specs[0].to_dict(),
        "batch_submit_args": {
            "faults": [spec.to_dict() for spec in specs],
            "fault_revision": specs[0].revision,
            "execution_order": "serial",
            "interval_seconds": 0,
        },
    }

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

    If the state already carries a real task id (CLI runner mints one
    externally before entering the graph), reuse it so we don't clobber
    a CLI-provided id. Otherwise (TS TUI per-turn ``turn-<hex>``,
    platform conversation thread ``chaos-<session>``, or no id at all)
    allocate a fresh one.

    The "is this a real task id" judgement lives in
    ``persistence.task_identity`` — the single source of truth shared
    with the persistence guards, so the contract cannot drift between
    the minting site and the write sites.
    """
    if is_real_task_id(current_task_id):
        return current_task_id
    return new_task_id()


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
    empty field instead of crashing the intent node.
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
    fault_revision: int,
    namespace: str = "",
    names: Annotated[Optional[list[str]], BeforeValidator(_validate_names)] = None,
    labels: Annotated[Optional[dict[str, str]], BeforeValidator(_validate_labels)] = None,
    params: Annotated[Optional[dict[str, str]], BeforeValidator(_validate_params)] = None,
    user_description: str = "",
) -> str:
    """Submit the collected fault injection intent (planning ONLY —
    structured handoff to execution confirmation).

    The triple is SEMANTIC — WHAT to inject, not HOW; NOT tied to any
    tool/command syntax (scenarios/params: ``read_skill_resource``).

    When to use: ONLY after every required parameter confirmed, an intent
    summary shown, and explicit user approval ("执行"/"确认"/"开始"/"go").
    Pass every reviewed-FaultSpec field EXACTLY; never complete a field
    from dialogue history. Multiple objectives → ``submit_batch_intent``.

    Inputs: fault_type "<scope>-<target>-<action>"; target = subsystem,
    NOT a resource instance name; fault_revision replayed exactly
    (0 = none yet); namespace empty for host/cluster-scoped;
    names/labels/params per the reviewed spec.

    Output: acknowledgment. Side effects: none (NO injection here).
    """
    return "✓ Fault-injection intent submitted; moving on to the execution-confirmation stage."


# ---------------------------------------------------------------------------
# Patch tool description with canonical values from fault_spec.INTENT_*
# so TUI/CLI/SDK all reference the SAME schema definitions.
# ---------------------------------------------------------------------------
submit_fault_intent.description = (
    submit_fault_intent.description
    + "\n\n    Valid scope values: "
    + "|".join(INTENT_SCOPES)
    + ".\n    Valid target values: "
    + "|".join(INTENT_TARGETS)
    + ".\n    Valid action values: "
    + "|".join(INTENT_ACTIONS)
    + "."
)


@lc_tool
def submit_batch_intent(
    faults: list[dict],
    fault_revision: int,
    execution_order: Literal["serial"] = "serial",
    interval_seconds: int = 0,
) -> str:
    """Submit MULTIPLE independent fault intents for batch execution (planning
    ONLY). The batch counterpart of ``submit_fault_intent``.

    When to use:
      - Only when the reviewed contract contains multiple INDEPENDENT fault
        objectives.
      - A single objective with multiple targets / traffic directions / steps /
        retries remains ONE semantic intent — use ``submit_fault_intent``.
      - Do NOT invent faults for coverage or diversity; preserve the user's
        reviewed objectives exactly.

    Inputs:
      - faults: list of fault dicts; each REQUIRES scope / target / action /
        namespace, and may independently add names (list[str]) / labels (dict) /
        params (dict) / fault_type (str).
      - fault_revision: server-owned revision of the reviewed FaultSpec — replay
        it exactly as shown.
      - execution_order: only "serial" is currently implemented.
      - interval_seconds: seconds between serial faults (default 0).

    Output: acknowledgment string; the server advances to execution confirmation.

    Side effects: none directly (records the batch intent and transitions to
        execution confirmation; no injection here).
    """
    return "✓ Batch fault-injection intent submitted; moving on to the execution-confirmation stage."


def _extract_submit_batch_intent(messages: list) -> dict | None:
    """Extract submit_batch_intent args from the most recent tool_call."""
    for msg in reversed(messages):
        if getattr(msg, "type", "") == "tool":
            continue
        if getattr(msg, "type", "") == "ai":
            for tc in getattr(msg, "tool_calls", None) or []:
                if tc.get("name") != "submit_batch_intent":
                    continue
                args = tc.get("args") or {}
                faults = args.get("faults", [])
                if not isinstance(faults, list):
                    continue
                valid = []
                for f in faults:
                    if not isinstance(f, dict):
                        continue
                    normalized = _normalise_fault_args(f)
                    if normalized["scope"] and normalized["target"] and normalized["action"]:
                        valid.append(normalized)
                if not valid:
                    return None
                return {
                    "faults": valid,
                    "fault_revision": _coerce_fault_revision(
                        args.get("fault_revision")
                    ),
                    "execution_order": "serial",
                    "interval_seconds": int(args.get("interval_seconds", 0)),
                }
            break
    return None


def _extract_recover_task_id(messages: list) -> str:
    """Extract task_id from the most recent recover_task tool_call.

    Walks backwards through messages to find the AIMessage that owns
    the recover_task ToolMessage, then returns the ``task_id`` arg.
    Returns empty string if not found (recover_handler will fall back
    to querying active experiments).
    """
    for msg in reversed(messages):
        if getattr(msg, "type", "") == "tool":
            continue
        if getattr(msg, "type", "") == "ai":
            for tc in getattr(msg, "tool_calls", None) or []:
                if tc.get("name") == "recover_task":
                    args = tc.get("args") or {}
                    return str(args.get("task_id", ""))
            break
    return ""


@lc_tool
async def query_active_experiments() -> str:
    """Read-only. List the active fault experiments that can still be recovered.

    When to use:
      - The user wants to recover / undo / rollback a fault but did NOT give a
        task_id — call this FIRST to discover candidates, then
        ``recover_task(task_id="task-xxx")``.
      - Do NOT use to inspect cluster/experiment health (use blade_status /
        kubectl); this only lists THIS tenant's recoverable experiments.

    Inputs: none.

    Output: a numbered list (newest first, up to 10) of recoverable experiments,
      each with task_id / fault_type / target / namespace / inject time; or a
      "no active experiments" message when none remain.

    Side effects: None (read-only query of the task store).
    """
    from chaos_agent.config.settings import settings
    from chaos_agent.persistence.task_store import get_task_store
    store = await get_task_store()
    # 多租户隔离：仅查询当前租户的活跃实验
    tenant_id = getattr(settings, "tenant_id", "") or ""
    active = await store.query_active(tenant_id=tenant_id)
    if not active:
        return "There are no active fault-injection experiments, so there is nothing to recover."
    from chaos_agent.agent.experiment_display import format_experiment_line
    # Newest first so "刚才 / 昨天" maps to the top rows.
    active = sorted(active, key=lambda t: t.get("gmt_create", ""), reverse=True)
    lines = [f"There are {len(active)} recoverable active experiment(s) (most recently injected first):"]
    for i, t in enumerate(active[:10], 1):
        lines.append(format_experiment_line(i, t))
    lines.append(
        "\nUse the fault type / target resource / injection time to decide which one to recover, "
        'then call recover_task(task_id="...").'
    )
    return "\n".join(lines)


@lc_tool
def recover_task(task_id: str) -> str:
    """Recover (undo) a previously injected fault experiment by task_id.

    When to use:
      - The user wants to undo / rollback / recover a specific prior injection.
      - If the user did NOT give a task_id, call ``query_active_experiments``
        FIRST to find it — never guess a task_id.

    Inputs:
      - task_id: the experiment's task_id (e.g. "task-xxx"), from the user or
        from ``query_active_experiments``.

    Output: an acknowledgment string; the recover graph then runs the reverse
      operation and its Layer-2 verification.

    Side effects: triggers fault RECOVERY — a mutating reverse operation that
      changes cluster/host state back toward normal (not a new injection).
    """
    return f"Recover request received for task: {task_id}"


def _extract_submit_args(messages: list) -> dict:
    """Pull the most recent submit_fault_intent tool_call args from history.

    LangGraph executes the tool, then routes back to this node with a
    ToolMessage trailing the source AIMessage. Walk backwards skipping
    ToolMessages until the owning AIMessage; if it carries a
    submit_fault_intent call, return the args dict normalised. Returns
    ``{}`` when no structured args are present.

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
    for msg in reversed(messages):
        msg_type = getattr(msg, "type", "")
        if msg_type == "tool":
            continue  # ToolNode result, not the source AI message
        if msg_type == "ai":
            for tc in getattr(msg, "tool_calls", None) or []:
                if tc.get("name") != "submit_fault_intent":
                    continue
                args = tc.get("args") or {}
                return {
                    **_normalise_fault_args(args),
                    "fault_type": _scalar_str(args.get("fault_type")),
                    "fault_revision": _coerce_fault_revision(
                        args.get("fault_revision")
                    ),
                    "user_description": _scalar_str(args.get("user_description")),
                }
            # AIMessage without a submit call — older turn we don't
            # care about; abandon the walk.
            return {}
        # SystemMessage / HumanMessage means we walked past the
        # current AI turn boundary without finding a submit call.
        return {}
    return {}


def _coerce_fault_revision(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _scalar_str(value: object) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _normalise_fault_args(args: dict) -> dict:
    """Normalize the shared fault vocabulary without interpreting semantics."""

    names_list = _coerce_to_list(args.get("names"), "names")
    labels_dict = _coerce_to_dict(args.get("labels"), "labels")
    params_dict = _coerce_to_dict(args.get("params"), "params")
    return {
        "scope": _scalar_str(args.get("scope")),
        "target": _scalar_str(args.get("target")),
        "action": _scalar_str(args.get("action")),
        "namespace": _scalar_str(args.get("namespace")),
        "names": [_scalar_str(name) for name in names_list if name not in (None, "")],
        "labels": {
            _scalar_str(key): _scalar_str(value)
            for key, value in labels_dict.items()
        },
        "params": {
            _scalar_str(key): _scalar_str(value)
            for key, value in params_dict.items()
        },
    }


def _public_intent_response(response, reply: str) -> AIMessage:
    """Keep tool metadata while replacing the private envelope with reply."""
    kwargs = {}
    original_id = getattr(response, "id", None)
    if original_id:
        kwargs["id"] = original_id
    additional_kwargs = dict(getattr(response, "additional_kwargs", None) or {})
    additional_kwargs["intent_response_protocol"] = "v1"
    kwargs["additional_kwargs"] = additional_kwargs
    return AIMessage(
        content=reply,
        tool_calls=getattr(response, "tool_calls", None) or [],
        response_metadata=getattr(response, "response_metadata", {}) or {},
        **kwargs,
    )


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
    - AIMessage (public reply plus original tool metadata)

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

    # 4. Public AIMessage. The private structured envelope is state, not
    #    conversational content, and is persisted separately on AgentState.
    if response is not None:
        persist.append(response)

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


def _capability_reject_message(state: dict, scope: str, tools) -> Optional[str]:
    """Return a rejection message when *scope* cannot run on this transport.

    ``None`` means "go ahead". Single home for the verdict so the single-fault
    and batch submit paths cannot drift apart.

    The verdict is ``build_capability_context(state, "plan", tools)`` — literally
    the call ``agent_loop`` makes before planning. Sharing the call rather than
    re-deriving the rule is deliberate: a hand-rolled predicate would be a second
    source of truth, and a submit that passes here only to be refused at planning
    is the exact round-trip this gate exists to remove.

    ``state`` is probed with the submitted scope so a batch can be checked one
    fault at a time — each entry may target a different domain.

    Errors are swallowed by the caller's guard: never block on a failed verdict.
    """
    probe = dict(state)
    # ``state["fault_spec"]`` is declared ``Optional[dict]`` and every writer in
    # the tree passes a dict (``to_dict()`` and friends). Guarded anyway: a
    # checkpoint restored from an older shape would otherwise raise inside the
    # caller's try/except and silently skip the gate — a gate that fails open
    # without a trace is worse than one that never ran.
    _existing = state.get("fault_spec")
    probe["fault_spec"] = {
        **(_existing if isinstance(_existing, dict) else {}),
        "scope": scope,
    }
    if build_capability_context(probe, "plan", tools or ()).supported:
        return None

    family = family_for_scope(scope or "")
    scope_profile = family.profile if family else ""
    scope_note = f" (capability profile {scope_profile})" if scope_profile else ""

    # With no connection field anywhere, the resolved channel is a process-wide
    # ``settings`` default the user never chose — naming it would only confuse.
    # Measured on the real platform DB: 39 of 45 environment records carry no
    # connection field at all.
    has_conn_field = bool(
        state.get("kube_connection_mode")
        or state.get("host_name")
        or state.get("ssh_host")
        or state.get("kubeconfig")
        or state.get("kubewiz_cluster_uuid")
    )
    if not has_conn_field:
        return (
            "The current drill environment has no usable connection configured, "
            "so this fault injection cannot be submitted.\n\n"
            f"- Fault domain: `{scope}`{scope_note}\n"
            "- Environment connection: not configured\n\n"
            "Fill in a connection method in the environment configuration first "
            "(K8s: a kubeconfig or a KubeWiz cluster; "
            "host: a KubeWiz host name or an SSH address), "
            "or rebind to a drill environment that is already configured."
        )

    channel = resolve_channel_name(state)
    channel_profile = profile_of(channel)
    channel_note = f" (capability profile {channel_profile})" if channel_profile else ""
    return (
        "The intent does not match the current drill environment, so it cannot be submitted.\n\n"
        f"- Fault domain: `{scope}`{scope_note}\n"
        f"- Environment channel: `{channel}`{channel_note}\n\n"
        "Switch to a drill environment that matches this fault domain, "
        "or choose a fault type the current environment supports."
    )


async def _reject_turn(
    content: str,
    *,
    messages: list,
    human_msg,
    dialogue_round: int,
    tui_session_id: str,
    hook_updates,
) -> dict:
    """Return a node-authored rejection so all three consumers see it.

    A rejection produced inside the node — not by an LLM call — has three
    audiences, and returning only the ``AIMessage`` reaches just one of them:

    - ``dispatch_node_message`` → the TUI. Its stream only renders
      ``on_chat_model_stream`` / ``on_tool_*`` / ``on_custom_event``; a message
      merely appended to state produces no event at all, so the turn ended with
      the tool marked ✓ and nothing shown (observed: submit ran, no intent card,
      no reason, session silently over). ``node_message`` is the channel built
      for exactly this ("programmatic text not produced by an LLM call") and is
      already understood by both the TUI and the platform's event normaliser.
    - ``_persist_dialogue`` → the session file. Every terminal path persists;
      the early returns did not, which is why the rejection could not be found
      in the transcript afterwards either. The rejection must be passed as
      ``response``: without it ``_build_dialogue_persist_list`` writes only the
      human turn and the trailing ToolMessages, so a drill's transcript showed
      ``submit_fault_intent`` succeeding and then jumped straight to the next
      round's prompt — the reason for stopping was nowhere on disk (observed on
      sess_5f082a560921, whose ``ic-system-round-5`` is missing entirely).
    - the returned ``AIMessage`` → the platform (read out as ``summary``) and
      the next turn's model context.

    The same message object goes to state and to disk. Building two would give
    them different ids, and the session store dedups by id — a re-persist of the
    same turn would then append a second copy.

    Dispatch is cosmetic and must never break the rejection itself: failures are
    logged and swallowed. ``dispatch_node_message`` only catches ``RuntimeError``
    (missing run context, e.g. unit tests), so the broader guard stays here.
    """
    rejection = AIMessage(content=content)
    try:
        await dispatch_node_message("intent_clarification", content)
    except Exception:  # noqa: BLE001
        logger.warning(
            "dispatch_node_message failed for a rejection (message still "
            "returned in state)", exc_info=True,
        )
    try:
        _persist_dialogue(
            tui_session_id,
            _build_dialogue_persist_list(
                messages, response=rejection,
                system_msg=None,
                human_msg=human_msg,
                dialogue_round=dialogue_round,
            ),
        )
    except Exception:  # noqa: BLE001
        logger.warning("persisting a rejection turn failed", exc_info=True)

    return merge_hook_updates({
        "messages": [rejection],
        "dialogue_round": dialogue_round + 1,
    }, hook_updates)


def make_intent_clarification(llm=None, tools: list = None, hook=None, registry=None):
    """Create the intent_clarification node function.

    Args:
        llm: LangChain LLM instance.
        tools: ToolNode tools available to semantic clarification. The full
               skill catalog remains visible; only provider read-only probes
               are selected by the current environment for safe discovery.
        hook: Optional PreReasoningHook for memory compaction.
        registry: SkillRegistry for dynamic skill catalog in system prompts.
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
                          "Talking with the user...")

        # Already confirmed → pass through to the router, which will
        # direct to the appropriate downstream node (agent_loop for
        # inject, save_memory for chat, recover_handler for recover).
        if confirmed_intent in ("inject", "chat", "recover"):
            if tracker:
                tracker.complete(f"Intent confirmed: {confirmed_intent}")
            return {}

        # "unset" means the caller has entered a fresh dialogue turn. The
        # durable FaultSpec below, not ad-hoc prose extraction, keeps the
        # reviewed contract available to the model.
        if confirmed_intent == "unset":
            logger.info("Intent partially converged (unset), continuing dialogue")

        if llm is None:
            if tracker:
                tracker.complete("LLM unavailable, defaulting to chat")
            return {"confirmed_intent": "chat"}

        # Safety net: overall dialogue limit
        if dialogue_round >= MAX_DIALOGUE_ROUNDS:
            logger.warning("Dialogue round %d >= %d, forcing exit",
                           dialogue_round, MAX_DIALOGUE_ROUNDS)
            if tracker:
                tracker.complete("Conversation turn limit exceeded")
            goodbye = AIMessage(content="Thanks for using Blade AI! Come back any time — goodbye!")
            # Both this exit and the LLM-failure one below set
            # ``confirmed_intent="chat"``, which routes past the mid-conversation
            # append and into the full finalize path. That path reaches the
            # session file only for the CLI runner: the server route gates its
            # flush on an operational ``task_id``, and ``chat`` turns never
            # allocate one (``_allocate_operation_task_id`` runs for inject,
            # batch and recover only — the real session sess_5f082a560921 ended
            # with ``task_ids: []``). Write it here so the last thing the user
            # was told survives on both transports.
            _persist_dialogue(tui_session_id, [goodbye])
            return {
                "confirmed_intent": "chat",
                "dialogue_round": dialogue_round + 1,
                "messages": [goodbye],
            }

        # Memory compaction
        hook_updates = {}
        if hook:
            hook_updates = await hook(state) or {}
            # Re-read after compaction, as agent_loop/execute_loop do. The hook
            # may replace the message list (RemoveMessage + summary), and the
            # copy captured at the top of this node predates that. Reading it
            # here is what lets the LLM call below pass the FULL history instead
            # of a fixed-size tail: compaction is the mechanism that bounds
            # context, so the node does not need to bound it again.
            messages = state.get("messages", [])

        existing_spec = read_fault_spec(state)
        existing_batch = _stored_batch_specs(state)

        # --- Fast-path: detect submit_fault_intent completion from ToolNode ---
        # If any trailing ToolMessage (from the most recent ToolNode batch)
        # is from submit_fault_intent, the model successfully called it in
        # the previous ReAct iteration. Skip LLM call and transition directly
        # to confirmed_intent="inject".
        # Check all trailing ToolMessages (ToolNode may process multiple
        # tool_calls in one batch, e.g. kubectl + submit_fault_intent).
        has_submit_tool_msg = _has_successful_trailing_tool_result(
            messages, "submit_fault_intent"
        )
        if has_submit_tool_msg:
            llm_args = _extract_submit_args(messages)
            # The normal path replays an already reviewed FaultSpec exactly.
            # If a model omitted its private proposal trailer, no FaultSpec
            # exists yet even though it may have shown a complete summary and
            # received explicit approval.  The structured submit is then the
            # only trustworthy contract source; bootstrap from it without
            # parsing prose or relaxing validation for an existing contract.
            if existing_spec is None:
                existing_spec = _bootstrap_submitted_spec(llm_args)
            if (
                existing_spec is None
                or not existing_spec.is_complete
                or (
                    # A bootstrapped spec has no prior server revision for
                    # the model to replay.  Once a spec exists, the revision
                    # and every executable field remain strict.
                    state.get("fault_spec") is not None
                    and not _submission_matches_spec(llm_args, existing_spec)
                )
            ):
                return await _reject_turn(
                    "The submitted content does not match the fault plan currently under review, "
                    "or the plan is not yet complete. "
                    "Confirm the current plan first, then resubmit using its exact fields.",
                    messages=messages,
                    human_msg=current_human_msg,
                    dialogue_round=dialogue_round,
                    tui_session_id=tui_session_id,
                    hook_updates=hook_updates,
                )

            # --- Channel compatibility gate -----------------------------------
            # Same verdict as ``agent_loop``'s capability gate, a planning round
            # earlier. See ``_capability_reject_message`` for why the call is
            # shared rather than re-derived.
            #
            # Guarded: if the verdict cannot be computed, let the submit through
            # and leave it to agent_loop — never block on an error.
            _reject = None
            try:
                _reject = _capability_reject_message(
                    state, existing_spec.scope or "", tools,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "capability gate check failed scope=%s; "
                    "letting the submit through",
                    getattr(existing_spec, "scope", "?"),
                    exc_info=True,
                )

            if _reject:
                return await _reject_turn(
                    _reject,
                    messages=messages,
                    human_msg=current_human_msg,
                    dialogue_round=dialogue_round,
                    tui_session_id=tui_session_id,
                    hook_updates=hook_updates,
                )

            if existing_spec.is_complete:
                if tracker:
                    tracker.complete(
                        f"Fault intent converged: {existing_spec.scope}-{existing_spec.blade_target} "
                        f"{existing_spec.blade_action} @ {existing_spec.namespace}"
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
                return merge_hook_updates({
                    "confirmed_intent": "inject",
                    "fault_spec": existing_spec.to_dict(),
                    "intent_confidence": 1.0,
                    "intent_reasoning": "submit_fault_intent tool executed",
                    "dialogue_round": dialogue_round + 1,
                    "task_id": op_task_id,
                }, hook_updates)
            return await _reject_turn(
                "The submitted execution parameters are incomplete or unsupported; "
                "re-confirm based on the current intent summary.",
                messages=messages,
                human_msg=current_human_msg,
                dialogue_round=dialogue_round,
                tui_session_id=tui_session_id,
                hook_updates=hook_updates,
            )

        # ── submit_batch_intent (batch injection) ──
        # Outside has_submit_tool_msg block: submit_batch_intent ToolMessage
        # has a different tool name so has_submit_tool_msg is False.
        has_batch_tool_msg = _has_successful_trailing_tool_result(
            messages, "submit_batch_intent"
        )
        if has_batch_tool_msg:
            batch_args = _extract_submit_batch_intent(messages)
            if batch_args:
                if not _submission_matches_batch(batch_args, existing_batch):
                    return await _reject_turn(
                        "The batch submission does not match the fault plan currently under review. "
                        "Confirm the current plan first, then resubmit using its exact fields.",
                        messages=messages,
                        human_msg=current_human_msg,
                        dialogue_round=dialogue_round,
                        tui_session_id=tui_session_id,
                        hook_updates=hook_updates,
                    )
                # --- Channel compatibility gate (per fault) -------------------
                # Same verdict as the single-fault path, applied to every entry:
                # a batch may mix domains, and ``agent_loop`` plans them one at a
                # time — so one incompatible entry would fail mid-batch, after
                # earlier faults were already injected. Refusing the whole batch
                # up front is both cheaper and safer than a partial run.
                #
                # Guarded like the single path: never block on a failed verdict.
                _batch_reject = None
                try:
                    for _idx, _spec in enumerate(existing_batch, 1):
                        _msg = _capability_reject_message(
                            state, _spec.scope or "", tools,
                        )
                        if _msg:
                            _batch_reject = (
                                f"Fault #{_idx} in this batch cannot run in the current environment, "
                                f"so the whole batch submission was cancelled.\n\n{_msg}"
                            )
                            break
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "capability gate check failed for batch; "
                        "letting the submit through", exc_info=True,
                    )

                if _batch_reject:
                    return await _reject_turn(
                        _batch_reject,
                        messages=messages,
                        human_msg=current_human_msg,
                        dialogue_round=dialogue_round,
                        tui_session_id=tui_session_id,
                        hook_updates=hook_updates,
                    )

                if tracker:
                    tracker.complete(f"Batch intent converged: {len(existing_batch)} faults")
                persist_list = _build_dialogue_persist_list(
                    messages, system_msg=None,
                    human_msg=current_human_msg,
                    dialogue_round=dialogue_round,
                )
                _persist_dialogue(tui_session_id, persist_list)
                op_task_id = _allocate_operation_task_id(state.get("task_id", ""))
                return merge_hook_updates({
                    "confirmed_intent": "batch_inject",
                    "fault_spec": existing_batch[0].to_dict(),
                    "batch_submit_args": {
                        "faults": [spec.to_dict() for spec in existing_batch],
                        "fault_revision": existing_batch[0].revision,
                        "execution_order": "serial",
                        "interval_seconds": batch_args.get("interval_seconds", 0),
                    },
                    "intent_confidence": 1.0,
                    "intent_reasoning": "submit_batch_intent tool executed",
                    "dialogue_round": dialogue_round + 1,
                    "task_id": op_task_id,
                }, hook_updates)

        # ── recover_task (recover flow) ──
        # Same pattern as submit_fault_intent: LLM calls recover_task,
        # ToolNode processes it, we detect the ToolMessage here and
        # route to recover_handler.
        has_recover_tool_msg = False
        if messages:
            for msg in reversed(messages):
                msg_type = getattr(msg, "type", "")
                if msg_type == "tool":
                    if getattr(msg, "name", "") == "recover_task":
                        has_recover_tool_msg = True
                        break
                else:
                    break
        if has_recover_tool_msg:
            recover_task_id = _extract_recover_task_id(messages)
            if tracker:
                tracker.complete(f"Recovery intent confirmed: {recover_task_id}")
            persist_list = _build_dialogue_persist_list(
                messages, system_msg=None,
                human_msg=current_human_msg,
                dialogue_round=dialogue_round,
            )
            _persist_dialogue(tui_session_id, persist_list)
            op_task_id = _allocate_operation_task_id(state.get("task_id", ""))
            bootstrap_task_session(
                op_task_id,
                operation="recover",
                tui_session_id=tui_session_id,
                handoff_message=None,
            )
            return merge_hook_updates({
                "confirmed_intent": "recover",
                "recover_task_id": recover_task_id,
                "task_id": op_task_id,
                "dialogue_round": dialogue_round + 1,
            }, hook_updates)

        # Resolve the configured channel into a capability profile so the model
        # knows *as a fact* whether it is drilling a host or a Kubernetes
        # environment. Without it the only signal was the user's wording, and a
        # host-channel environment would load ``k8s-chaos-skills``.
        #
        # ``profile_of`` is the single source of truth for channel → profile;
        # do not re-derive the mapping here. Unresolvable channels come back as
        # ``unknown`` and are passed through as such — ``build_system_prompt``
        # deliberately omits the section in that case rather than emitting the
        # "environment is unsupported, do not attempt injection" wording.
        #
        # The skill catalog stays unfiltered either way: this informs, it does
        # not restrict.
        _profile = profile_of(state.get("kube_connection_mode") or "")

        system_msg = SystemMessage(
            content=build_system_prompt(
                PromptMode.INTENT,
                fault_spec=existing_spec.to_dict() if existing_spec else None,
                batch_faults=[spec.to_dict() for spec in existing_batch],
                skill_catalog=registry.build_catalog_prompt() if registry else "",
                semantic_only=True,
                profile=_profile,
            )
        )

        # --- Anti-stagnation detection (same mechanism as agent_loop) ---
        loop_hint = detect_repeated_tool_calls(messages, phase="intent")
        _, stagnant_tool = detect_action_stagnation(messages, phase="intent")

        intent_stagnation_hint = None
        if stagnant_tool:
            intent_stagnation_hint = build_stagnation_hint(
                stagnant_tool,
                colon_suffix="",
                else_actions=[
                    "Return a normal reply; append a FaultSpec proposal only if the contract changed.",
                    "Use a DIFFERENT tool (activate_skill, read_skill_resource) for more info.",
                    "Conclude the conversation turn with the normal reply.",
                ],
            )
            if ":" in stagnant_tool:
                # The subcommand-level branch lists no alternatives, so name the
                # one that ends this phase: replying to the user IS a valid
                # outcome here, and a model that does not know that keeps
                # probing. Anchored on the shared closing sentence rather than a
                # prohibition, which that branch no longer issues.
                _anchor = "If repeating"
                _submit_line = (
                    "Replying to the user is a valid outcome here: return a normal "
                    "reply, and append a FaultSpec proposal only if the contract "
                    "changed.\n"
                )
                intent_stagnation_hint = intent_stagnation_hint.replace(
                    _anchor, _submit_line + _anchor, 1,
                )

        # Fault recognition uses the full catalog regardless of transport.
        # Only target *discovery* follows the configured environment, so a
        # host intent can be recognized on a K8s session (and vice versa)
        # before Agent Loop performs the final compatibility decision.
        discovery_context = build_intent_discovery_context(state, tools)
        tools_this_iter = filter_stagnant_tool(
            filter_tools_for_context(tools, discovery_context),
            stagnant_tool, preserve={"submit_fault_intent", "submit_batch_intent"},
        )

        llm_bound = llm.bind_tools(tools_this_iter)

        # Full history, same as agent_loop / execute_loop / verifier /
        # recover_verifier. This was ``messages[-20:]``, which silently deleted
        # the user's own request: a real 84-message drill held exactly ONE
        # HumanMessage — at index 0 — so from message 22 onward the window
        # contained no user turn at all, and the model was left with tool output
        # and no task. It also dropped the operation summaries that
        # ``write_operation_summary`` appends after each inject/recover, i.e. the
        # record of what previous drills did.
        #
        # Bounding context is the compaction hook's job (invoked above, with the
        # same threshold/reserve budget the main chain relies on). A fixed
        # message count cannot do that job: it is blind to message size, and it
        # treats the task definition as equal in value to a routine tool result.
        llm_messages = [system_msg] + messages
        # Corrective notices are persisted into the returned messages as well as
        # injected here. Injection alone lasts one turn, so every iteration
        # re-derives the same warning and the model reads it as the first time —
        # the failure measured in task-e9ee12d6.
        _hints_for_state: list = []
        # Counts live on state so compaction cannot reset them.
        _hint_counts = dict(state.get("hint_repeat_counts") or {})
        if loop_hint:
            llm_messages.append(persist_corrective_hint(
                _hints_for_state, messages, "loop", "intent", loop_hint,
                escalate_after=settings.hint_escalate_after,
                counts=_hint_counts, counts_out=_hint_counts,
            ))
        if intent_stagnation_hint:
            llm_messages.append(persist_corrective_hint(
                _hints_for_state, messages,
                "stagnation", stagnant_tool or "intent", intent_stagnation_hint,
                escalate_after=settings.hint_escalate_after,
                counts=_hint_counts, counts_out=_hint_counts,
            ))

        try:
            if tracker:
                tracker.update("Calling the LLM...")
            response = await llm_bound.ainvoke(llm_messages)
        except Exception as e:
            logger.error("Intent clarification LLM failed: %s", e)
            if tracker:
                tracker.fail(f"LLM call failed: {e}")
            apology = AIMessage(content="Sorry, I ran into a problem. Please try again shortly.")
            # See the dialogue-limit exit above for why a ``chat`` turn has to
            # persist its own closing message.
            _persist_dialogue(tui_session_id, [apology])
            return merge_hook_updates({
                "confirmed_intent": "chat",
                "messages": [apology],
            }, hook_updates)

        tool_calls = getattr(response, "tool_calls", None) or []
        parsed_response = parse_fault_proposal(getattr(response, "content", ""))

        # --- Priority 1: has tool calls (kubectl, submit_fault_intent, etc.) ---
        if tool_calls:
            if tracker:
                tracker.complete("Waiting for tool execution")
            proposal_update = {}
            if parsed_response is not None:
                reply, raw_faults = parsed_response
                response = _public_intent_response(response, reply)
                proposal_update = _proposal_state_update(
                    _advance_proposed_specs(existing_spec, existing_batch, raw_faults)
                )
            persist_list = _build_dialogue_persist_list(
                messages, response=response,
                system_msg=system_msg,
                human_msg=current_human_msg,
                dialogue_round=dialogue_round,
            )
            _persist_dialogue(tui_session_id, persist_list)
            result = {
                "messages": _hints_for_state + [response],
                "hint_repeat_counts": _hint_counts,
                "clarification_round": clarification_round + 1,
                "dialogue_round": dialogue_round + 1,
            }
            result.update(proposal_update)
            return merge_hook_updates(result, hook_updates)

        # --- Priority 2: pure text response (no tool calls at all) ---
        # Plain text is a valid chat/capability reply. It is deliberately not
        # interpreted as semantic state: this preserves ordinary conversation
        # (for example, "你是谁") without restoring the old unsafe practice of
        # deriving executable fields from prose.
        if parsed_response is None:
            if tracker:
                tracker.complete("Chat reply finished (intent unchanged)")
            persist_list = _build_dialogue_persist_list(
                messages, response=response,
                system_msg=system_msg,
                human_msg=current_human_msg,
                dialogue_round=dialogue_round,
            )
            _persist_dialogue(tui_session_id, persist_list)
            return merge_hook_updates({
                "messages": _hints_for_state + [response],
                "hint_repeat_counts": _hint_counts,
                "dialogue_round": dialogue_round + 1,
            }, hook_updates)

        reply, raw_faults = parsed_response
        public_response = _public_intent_response(response, reply)
        proposal_update = _proposal_state_update(
            _advance_proposed_specs(existing_spec, existing_batch, raw_faults)
        )
        if tracker:
            tracker.complete("Chat reply finished")
        persist_list = _build_dialogue_persist_list(
            messages, response=public_response,
            system_msg=system_msg,
            human_msg=current_human_msg,
            dialogue_round=dialogue_round,
        )
        _persist_dialogue(tui_session_id, persist_list)
        result = {
            "messages": [public_response],
            "dialogue_round": dialogue_round + 1,
        }
        result.update(proposal_update)
        return merge_hook_updates(result, hook_updates)

    return intent_clarification
