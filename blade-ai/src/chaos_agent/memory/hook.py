"""Pre-reasoning hook: unified memory management entry point.

Called before each LLM reasoning step in agent_loop and execute_loop.
Handles: tool output truncation → context check → async persistence → sync compaction.
"""

import asyncio
import logging
import time

from langchain_core.messages import SystemMessage, RemoveMessage

from chaos_agent.agent.node_names import MEMORY_HOOK, TOOL_RESULT
from chaos_agent.memory.compactor import compact_memory
from chaos_agent.memory.context_manager import (
    MAX_CONSECUTIVE_COMPACT_FAILURES,
    CompactTrackingState,
    ContextManager,
    strip_large_outputs,
)
from chaos_agent.memory.session_store import SessionStore
from chaos_agent.memory.tokens import count_tokens, count_tokens_messages
from chaos_agent.memory.tool_compactor import ToolResultCompactor
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# E2 Phase 2A — structured metric extraction (runs BEFORE truncation)
# ---------------------------------------------------------------------------

_AUTO_EXTRACTED_MARKER = "[Auto-extracted:"


def _find_tool_command(target_msg, all_messages: list) -> str:
    """Find the command string for a ToolMessage by index-then-extract.

    Thin convenience wrapper around ``_build_tool_call_index`` +
    ``_command_from_parent`` for callers that have a single ToolMessage
    in hand and don't care about reusing the index (notably tests).
    The hot path in ``_extract_tool_metrics`` builds the index once
    per hook call and avoids the per-call rebuild here.
    """
    tool_call_id = getattr(target_msg, "tool_call_id", "") or ""
    if not tool_call_id:
        return ""
    parent = _build_tool_call_index(all_messages).get(tool_call_id)
    return _command_from_parent(parent, tool_call_id)


def _format_metric_summary(metrics: dict[str, str]) -> str:
    """Render the metric dict as the one-line head prefix.

    Pipe-separated with explicit markers so an operator scanning the
    raw output can locate the auto-summary at a glance. Order is dict
    insertion order, which matches the parser invocation order — stable
    for tests.
    """
    body = " | ".join(f"{k}={v}" for k, v in metrics.items())
    return f"{_AUTO_EXTRACTED_MARKER} {body}]\n"


def _is_json_shaped(content: str) -> bool:
    """Quick sniff: does ``content`` look like a JSON object/array root?

    Used to decide whether to prepend the ``[Auto-extracted: …]``
    summary into the ToolMessage content. JSON-shaped results MUST stay
    syntactically valid JSON because ``tool_compactor.compact``'s first
    strategy is ``smart_strip_k8s_json(content, max_bytes)``, which
    runs ``json.loads(content)``. Any non-JSON prefix breaks the parse,
    forces a fallback to dumb boundary truncation, and we lose the
    intelligent K8s field stripping (drops managedFields / annotations,
    keeps only essentials by ``kind``).

    For JSON-shaped content the metrics still land in
    ``additional_kwargs['extracted_metrics']`` for the state-side
    timeline + Phase 3 cross-check; the LLM can still read the
    canonical fields out of the smart-stripped JSON itself
    (RestartCount, etc. survive the stripping).
    """
    s = content.lstrip()
    return s.startswith("{") or s.startswith("[")


def _build_tool_call_index(messages: list) -> dict[str, object]:
    """Precompute ``{tool_call_id: AIMessage}`` in one pass over messages.

    Lets ``_extract_tool_metrics`` resolve each ToolMessage's parent
    in O(1) instead of doing a fresh reverse-scan per ToolMessage.
    Cost is one full forward scan; benefit grows quadratically with
    message-list length.
    """
    index: dict[str, object] = {}
    for m in messages:
        tool_calls = getattr(m, "tool_calls", None) or []
        for tc in tool_calls:
            if isinstance(tc, dict):
                tc_id = tc.get("id", "") or ""
            else:
                tc_id = getattr(tc, "id", "") or ""
            if tc_id and tc_id not in index:
                index[tc_id] = m
    return index


def _command_from_parent(parent_msg, tool_call_id: str) -> str:
    """Extract the command string for a given tool_call_id from its
    parent AIMessage's tool_calls list.

    Uses value-only join (not ``k=v``) so e.g.
    ``{"subcommand": "get", "v_args": "pod my-pod"}`` flattens to
    ``"get pod my-pod"`` and the metric extractor's substring
    dispatch still finds ``"get pod"`` as a contiguous match.
    """
    if parent_msg is None:
        return ""
    tool_calls = getattr(parent_msg, "tool_calls", None) or []
    for tc in tool_calls:
        if isinstance(tc, dict):
            tc_id = tc.get("id", "") or ""
            args = tc.get("args", {}) or {}
        else:
            tc_id = getattr(tc, "id", "") or ""
            args = getattr(tc, "args", {}) or {}
        if tc_id == tool_call_id:
            if isinstance(args, dict):
                return " ".join(str(v) for v in args.values() if v not in (None, ""))
            return str(args)
    return ""


def _extract_tool_metrics(messages: list) -> None:
    """For each ToolMessage that hasn't been processed yet, run the
    metric extractor and (a) store the dict in
    ``msg.additional_kwargs['extracted_metrics']``, (b) prepend a
    ``[Auto-extracted: …]`` line to the message content (skipped for
    JSON-shaped content — see ``_is_json_shaped``).

    Idempotent: messages whose ``additional_kwargs`` already carries
    ``extracted_metrics`` are skipped, so the hook can be called
    multiple times per turn without duplicating the summary line.

    Pure mutation (no return) — same convention as ``tool_compactor``
    elsewhere in this module.
    """
    # Lazy import to avoid a circular import (hook is imported by graph
    # construction; the extractor lives under agent.nodes which itself
    # transitively pulls in fault_spec and other graph-adjacent modules).
    from chaos_agent.agent.nodes._metric_extractor import extract_metrics

    parent_index: dict[str, object] | None = None  # built lazily on first need

    for msg in messages:
        if getattr(msg, "type", None) != "tool":
            continue
        # Skip if we've already processed this ToolMessage in a prior turn.
        akw = getattr(msg, "additional_kwargs", None)
        if not isinstance(akw, dict):
            continue
        if "extracted_metrics" in akw:
            continue
        content = getattr(msg, "content", "")
        # Only text content — multi-modal tool results are out of scope
        # for the per-format text parsers. Empty content also falls
        # here; mark with the flag so we don't re-walk it next turn.
        if not isinstance(content, str) or not content.strip():
            akw["extracted_metrics"] = {}
            continue
        # Belt-and-braces: a content that ALREADY starts with the
        # marker means a prior process inserted the summary but didn't
        # set the additional_kwargs flag (e.g. legacy session replay).
        # Don't double-prepend.
        if content.lstrip().startswith(_AUTO_EXTRACTED_MARKER):
            akw["extracted_metrics"] = {}  # mark as processed
            continue

        # Lazy-build the parent index on first ToolMessage that needs
        # it — saves the O(M) scan when all ToolMessages are already
        # flag-marked.
        if parent_index is None:
            parent_index = _build_tool_call_index(messages)
        tc_id = getattr(msg, "tool_call_id", "") or ""
        command = _command_from_parent(parent_index.get(tc_id), tc_id)
        tool_name = getattr(msg, "name", "") or ""
        try:
            metrics = extract_metrics(tool_name, command, content)
        except Exception as e:  # extractor must never raise, but guard anyway
            logger.debug("metric extractor failed: %s", e)
            metrics = {}

        # Always set the flag (even when {}) so we don't re-walk this
        # message on every subsequent hook invocation.
        akw["extracted_metrics"] = metrics
        # JSON-shaped content: skip the head prepend to preserve
        # ``smart_strip_k8s_json`` validity. See ``_is_json_shaped``
        # for the full rationale. Metrics still travel via
        # additional_kwargs to the Phase 2B state accumulator.
        if metrics and not _is_json_shaped(content):
            try:
                msg.content = _format_metric_summary(metrics) + content
            except Exception as e:
                logger.debug("metric summary prepend failed: %s", e)


class PreReasoningHook:
    """Unified memory management hook called before each LLM reasoning step."""

    def __init__(
        self,
        context_manager: ContextManager,
        tool_compactor: ToolResultCompactor,
        session_store: SessionStore,
        llm=None,
        tui_session_store=None,
    ):
        self.context_manager = context_manager
        self.tool_compactor = tool_compactor
        self.session_store = session_store
        self.llm = llm
        self.tui_session_store = tui_session_store
        # Per-task circuit breaker state. Keyed by task_id so two
        # concurrent tasks don't share each other's failure counters
        # (a single hook instance is shared across all tasks of a
        # running server). Without this, the
        # ``MAX_CONSECUTIVE_COMPACT_FAILURES`` guard in check_context
        # was dead code — every call constructed a fresh state with
        # zero failures, so the breaker could never trip.
        self._tracking: dict[str, CompactTrackingState] = {}

    def _get_tracking(self, task_id: str) -> CompactTrackingState:
        """Return (creating if needed) the per-task circuit breaker state."""
        state = self._tracking.get(task_id)
        if state is None:
            state = CompactTrackingState()
            self._tracking[task_id] = state
        return state

    def _build_observation_update(self, messages: list, state: dict) -> dict:
        """E2 Phase 2B — collect newly extracted metrics into the
        timeline on ``state.metric_observations``.

        ``_extract_tool_metrics`` already populated
        ``additional_kwargs['extracted_metrics']`` on each ToolMessage
        in this turn. Here we promote the non-empty entries we haven't
        seen before (matched by ``tool_call_id``) into a structured
        list on state. The list survives ``[Compressed History]``
        compaction because it lives on state, not on the message list
        — which is exactly the gap E2 was designed to close
        (1KB historical truncation + LLM-summary compaction both
        destroy in-message metric history).

        Returns ``{}`` when no new observations were captured (so the
        caller can spread it into the return dict without bumping
        state version when nothing changed).
        """
        current = list(state.get("metric_observations") or [])
        seen_ids = {obs.get("tool_call_id") for obs in current if obs.get("tool_call_id")}
        # iteration tag: SUM of the major loop counters present on
        # state. This makes the value monotonically non-decreasing
        # across phases (inject → verify) within a task, which is what
        # any timeline analysis needs. The previous ``or`` fallback
        # could regress from agent_loop_count=15 (inject phase) to
        # verifier_loop_count=1 (first verifier turn), producing a
        # nonsensical "iteration went backwards" series.
        iteration = (
            int(state.get("agent_loop_count", 0) or 0)
            + int(state.get("verifier_loop_count", 0) or 0)
        )
        ts = now_iso()

        new_obs: list[dict] = []
        for msg in messages:
            if getattr(msg, "type", None) != "tool":
                continue
            akw = getattr(msg, "additional_kwargs", {}) or {}
            metrics = akw.get("extracted_metrics") or {}
            if not metrics:
                continue
            tc_id = getattr(msg, "tool_call_id", "") or ""
            if not tc_id or tc_id in seen_ids:
                continue
            new_obs.append({
                "iteration": int(iteration),
                "timestamp": ts,
                "tool_call_id": tc_id,
                "tool_name": getattr(msg, "name", "") or "",
                "metrics": dict(metrics),
            })
        if not new_obs:
            return {}
        return {"metric_observations": current + new_obs}

    def _emit_context_size_snapshot(
        self,
        task_id: str,
        current_tokens: int,
        messages_count: int,
    ) -> None:
        """Convenience wrapper used by the 3 emit points in
        ``__call__``. Pulls ``max_tokens`` / ``trigger_tokens`` from
        the bound ``context_manager`` so call sites only need to
        supply what's genuinely per-emit (the current measurement
        and the messages count). If we ever switch the trigger
        source (e.g. use ``calculate_token_warning_state``'s computed
        effective threshold instead of the legacy
        ``compact_threshold`` attribute), this is the single seam to
        change."""
        self._emit_context_size_event(
            task_id,
            current_tokens=current_tokens,
            max_tokens=self.context_manager.max_tokens,
            trigger_tokens=self.context_manager.compact_threshold,
            messages_count=messages_count,
        )

    def _emit_context_size_event(
        self,
        task_id: str,
        *,
        current_tokens: int,
        max_tokens: int,
        trigger_tokens: int,
        messages_count: int,
    ) -> None:
        """Fan-out a snapshot of the post-hook state size so the TS
        TUI Footer can render a live ``current/window`` indicator
        aligned with the actual compaction trigger.

        Uses the same dual fan-out as ``_emit_compaction_event`` —
        per-task tracker for the legacy CLI status stream + per-tui-
        session tracker (``tui-{sid}``) for the TS TUI's main /turn
        SSE relay. Best-effort; never raises.

        Emitted AFTER any compaction decision so ``current_tokens``
        reflects what the NEXT LLM call will actually see — useful
        because compaction shrinks the value and the Footer should
        visibly drop in real time when that happens."""
        try:
            from chaos_agent.observability.status_tracker import (
                get_tracker,
                StatusEvent,
            )
            event = StatusEvent(
                task_id=task_id,
                # Phase is not a lifecycle marker here — it's a
                # plain "this is the current measurement" frame.
                # "running" is the closest StatusPhase value; the
                # TS TUI dispatcher routes purely on ``source``,
                # not on phase, so the exact value doesn't matter.
                phase="running",
                category="node",
                source="context_size",
                message=(
                    f"context: {current_tokens}/{max_tokens} tokens "
                    f"({messages_count} msgs)"
                ),
                detail={
                    "current_tokens": current_tokens,
                    "max_tokens": max_tokens,
                    "trigger_tokens": trigger_tokens,
                    "messages_count": messages_count,
                },
                duration_ms=0.0,
            )
            if task_id:
                tracker = get_tracker(task_id)
                if tracker:
                    tracker.emit(event)
            tui_sid = self._state_tui_session_id or ""
            if tui_sid:
                tui_tracker = get_tracker(f"tui-{tui_sid}")
                if tui_tracker:
                    tui_tracker.emit(event)
        except Exception:
            pass  # best-effort — never let the indicator break the hook

    async def __call__(self, state: dict, *, force: bool = False) -> dict:
        """Execute memory management before LLM reasoning.

        Returns LangGraph-compatible state updates:
        - Tool truncation: returns updated messages (replaced by ID via add_messages reducer)
        - Compression: returns RemoveMessage entries + summary message

        Args:
            state: Agent state dict with 'messages' key
            force: When True (user-initiated /compact), bypass the
                auto-trigger threshold check, skip the circuit breaker,
                and ALWAYS run the LLM compaction path (no strip-only
                short-circuit). Default False — the auto path called
                before every LLM reasoning step retains its threshold
                gating and cheap-path optimisations.

        Returns:
            Updated state dict with possibly compacted messages
        """
        messages = state.get("messages", [])
        task_id = state.get("task_id", "")

        # Store state fields for _async_session_append routing
        self._state_confirmed_intent = state.get("confirmed_intent")
        self._state_tui_session_id = state.get("tui_session_id", "")

        # 0. Structured metric extraction (E2) — MUST run BEFORE truncation.
        # The extractor reads each new ToolMessage's raw content, derives
        # a small dict of named metrics (RestartCount=8, Disk usage=26%, …),
        # and stores it in ``msg.additional_kwargs['extracted_metrics']``
        # plus prepends a one-line ``[Auto-extracted: …]`` summary to
        # the content HEAD. The summary survives the 1KB historical
        # ``truncate_text`` because that retains the head only, so the
        # metrics remain visible to the LLM even after compression. The
        # ``additional_kwargs`` copy survives both truncation and
        # ``[Compressed History]`` compaction (compaction's summariser
        # operates on content; additional_kwargs ride along on the
        # message object until the message itself is removed). The
        # state-side accumulator below reads from these
        # additional_kwargs to build the verification timeline.
        _extract_tool_metrics(messages)
        # Build the state-side append to metric_observations
        # ONCE here, then spread it into whichever return branch fires
        # below. We can't accumulate at each branch because they
        # bypass each other — the strip-only branch never reaches the
        # compaction branch's append.
        obs_update = self._build_observation_update(messages, state)

        # 1. Tool output truncation (modifies messages in-place)
        messages = self.tool_compactor.compact(messages, task_id=task_id)

        # 2. Context check — pass per-task tracking so the circuit
        # breaker inside check_context can observe past failures.
        # ``force`` is forwarded so a manual /compact invocation
        # bypasses both the threshold gate and the breaker.
        tracking = self._get_tracking(task_id)
        to_compact, to_keep, valid = self.context_manager.check_context(
            messages, tracking=tracking, force=force
        )

        # check_context returns valid=False when either:
        #   - the circuit breaker has tripped (too many consecutive
        #     failures), or
        #   - the context is at the BLOCKING level (already past the
        #     hard ceiling, no safe way to compact further).
        # Either way it has already written its own log line; we add
        # a hook-level warning so the operator sees the consequence
        # (we skipped compaction this turn) at the call site, not
        # buried in check_context's debug output. We still proceed
        # below — if to_compact is empty we'll bail out at the
        # ``if not to_compact:`` branch; if not, the caller asked us
        # to attempt aggressive stripping anyway.
        if not valid:
            logger.warning(
                f"check_context returned valid=False for task {task_id} "
                f"(circuit breaker tripped or at blocking level); "
                f"skipping LLM compaction this turn"
            )

        # 3. Append messages to SessionStore on EVERY call (not just compaction)
        if task_id and self.session_store:
            asyncio.create_task(
                self._async_session_append(task_id, messages)
            )

        if not to_compact:
            # Tool compaction modifies messages in-place (same IDs, changed content).
            # No state update needed — the message objects in state are already modified.
            # Write memory status to session file (always), but do NOT show in CLI.
            # safe_count = raw × per-quality margin (1.0/1.05/1.20).
            # Replaces the global ``× 1.2`` fudge; the multiplier now
            # tracks how accurate the underlying tokenizer actually is
            # for the configured model.
            total_tokens = count_tokens_messages(messages).safe_count
            self._persist_to_session(
                task_id,
                f"Memory OK: {len(messages)} messages, ~{total_tokens} tokens",
            )
            self._emit_context_size_snapshot(task_id, total_tokens, len(messages))
            return obs_update

        # --- Intermediate route: try aggressive tool output truncation first ---
        # If we can get below the compaction threshold by just truncating tool
        # outputs more aggressively, skip the expensive LLM-based compression.
        stripped = strip_large_outputs(to_compact, threshold=1000)
        # Force mode (manual /compact) ALWAYS runs the LLM path —
        # strip-only just truncates tool output content, it doesn't
        # produce a summary. A user pressing /compact wants the
        # structural collapse a summary gives, not a cosmetic trim.
        # We still run the strip above as preprocessing (smaller LLM
        # input = cheaper call), then fall through to compact_memory.
        if not force:
            combined = stripped + to_keep
            combined_tokens = count_tokens_messages(combined).safe_count
            if combined_tokens < self.context_manager.compact_threshold:
                logger.info(
                    f"Aggressive tool truncation sufficient: {combined_tokens} tokens "
                    f"(threshold {self.context_manager.compact_threshold}), "
                    f"skipping LLM compression"
                )
                # Hand the stripped messages back to LangGraph so the state
                # actually reflects the truncation. ``strip_large_outputs``
                # produces NEW message objects via ``model_copy`` (LangChain
                # BaseModels are immutable), preserving each message's id.
                # The ``add_messages`` reducer dedupes by id and REPLACES
                # the originals with the stripped copies — exactly what we
                # want here.
                #
                # PREVIOUS BUG: this returned ``{}``, so the stripped
                # objects were discarded and the next reasoning step still
                # saw the un-stripped tool output. The whole "intermediate
                # route" was therefore silently a no-op: token usage never
                # dropped, and we'd hit the same threshold again next turn,
                # paying the strip cost over and over with zero effect.
                self._emit_context_size_snapshot(task_id, combined_tokens, len(combined))
                return {**obs_update, "messages": stripped}

        # Calculate total tokens before compression for observability.
        # Single-message observability — use raw .count (not safe_count)
        # so the logged number matches what we'd actually send to the
        # LLM, not an inflated threshold-safe view.
        total_tokens_before = sum(
            count_tokens(getattr(msg, "content", "") or "").count
            for msg in to_compact
            if isinstance(getattr(msg, "content", ""), str)
        )

        logger.info(
            f"Compacting {len(to_compact)} messages for task {task_id}"
        )

        # Emit STARTED event (NODE category → debug-only in CLI, always persisted to session)
        self._emit_compaction_event(
            task_id,
            "started",
            f"Compressing {len(to_compact)} messages ({len(to_keep)} kept)",
            category="node",
            detail={
                "messages_to_compact": len(to_compact),
                "messages_to_keep": len(to_keep),
                "total_tokens_before": total_tokens_before,
            },
        )

        # 4. Sync compaction (generate structured summary).
        # Pass ``state`` so compact_memory's extract_critical_context
        # pulls structured fields (active blade_uid, skill, target,
        # plan_path) out of state and prepends them as a recovery
        # header on the summary. Without this, those fields rely
        # entirely on the LLM remembering to copy them — fine on a
        # good day, lossy on a bad one. The auto path used to skip
        # ``state``; passing it here is the same fix the unification
        # applied to the manual /compact path.
        start_time = time.monotonic()
        try:
            previous_summary = state.get("compressed_summary", "")
            summary = await compact_memory(
                stripped,
                previous_summary=previous_summary,
                llm=self.llm,
                state=state,
            )

            duration_ms = (time.monotonic() - start_time) * 1000
            # Post-compaction view used by circuit breaker logic — safe_count
            # so a still-too-large kept slice errs toward "didn't make
            # progress" rather than masking a failed compaction.
            tokens_after = count_tokens_messages(to_keep).safe_count
            # Circuit breaker bookkeeping: a successful compaction
            # resets the consecutive-failure counter (so a transient
            # LLM outage doesn't permanently trip the breaker once the
            # provider recovers) and marks this turn as compacted.
            tracking.consecutive_failures = 0
            tracking.compacted = True
            tracking.turn_count += 1
            self._emit_compaction_event(
                task_id,
                "completed",
                f"Compression done: {len(to_compact)} messages compressed ({duration_ms:.0f}ms)",
                category="node",
                duration_ms=duration_ms,
                detail={
                    "messages_compacted": len(to_compact),
                    "tokens_before": total_tokens_before,
                    "tokens_after": tokens_after,
                },
            )
        except Exception as e:
            duration_ms = (time.monotonic() - start_time) * 1000
            tracking.consecutive_failures += 1
            self._emit_compaction_event(
                task_id,
                "failed",
                f"LLM compaction failed ({tracking.consecutive_failures}/"
                f"{MAX_CONSECUTIVE_COMPACT_FAILURES}): {e}",
                category="node",
                duration_ms=duration_ms,
            )
            logger.warning(
                f"LLM compaction failed for task {task_id}, "
                f"skipping this turn (failures: {tracking.consecutive_failures}/"
                f"{MAX_CONSECUTIVE_COMPACT_FAILURES}): {e}"
            )
            return obs_update

        # 5. Build LangGraph-compatible state update:
        #    Remove compacted messages + add incremental summary message
        #    Incremental: each summary is a separate [Compressed History] entry,
        #    never re-compresses previous summaries (they are in to_keep).
        summary_message = SystemMessage(content=f"[Compressed History]\n{summary}")

        remove_messages = []
        for msg in to_compact:
            msg_id = getattr(msg, "id", None)
            if msg_id:
                remove_messages.append(RemoveMessage(id=msg_id))

        # Append new summary (don't replace previous summaries)
        # The previous [Compressed History] messages are in to_keep and untouched.
        # Emit the post-compaction state size so the Footer indicator
        # visibly drops in real time when the summary lands. safe_count
        # keeps Footer / circuit-breaker math consistent with the same
        # threshold semantics used at the entry check above.
        post_compaction_messages = list(to_keep) + [summary_message]
        post_compaction_tokens = count_tokens_messages(
            post_compaction_messages,
        ).safe_count
        self._emit_context_size_snapshot(
            task_id, post_compaction_tokens, len(post_compaction_messages),
        )
        return {
            **obs_update,
            "messages": remove_messages + [summary_message],
            "compressed_summary": summary,
        }

    async def _async_session_append(self, task_id: str, messages: list) -> None:
        """Append messages to appropriate store based on execution phase.

        Intent clarification phase (confirmed_intent=None/"unset"):
        → NO session file write. Intent dialogue persistence is handled
          exclusively by intent_clarification._persist_dialogue, which
          writes the user-visible filtered version (internal tools removed,
          fallback content injected). This eliminates the double-write bug
          where the hook wrote raw LLM output and _persist_dialogue wrote
          the filtered version, causing dedup failures when content differed
          (e.g. empty content vs fallback text).

        Execution phase (confirmed_intent=inject/recover/chat):
        → write to task file (execution messages).
        """
        try:
            # Determine routing based on confirmed_intent in state
            confirmed_intent = self._state_confirmed_intent

            if confirmed_intent in (None, "unset"):
                # Intent clarification: single-source persistence via
                # _persist_dialogue. Hook does NOT write session file.
                return

            # Inject/recover execution → task JSONL (existing behavior)
            # Stamp _node on ToolMessages (their only write path is through here)
            from langchain_core.messages import ToolMessage as _TM
            for msg in messages:
                if isinstance(msg, _TM):
                    _kwargs = getattr(msg, "additional_kwargs", None)
                    if isinstance(_kwargs, dict):
                        _kwargs.setdefault("_node", TOOL_RESULT)
            self.session_store.append_messages(task_id, messages)
        except Exception as e:
            logger.warning(f"Failed to append messages for task {task_id}: {e}")

    def _persist_to_session(self, task_id: str, message: str, detail: dict = None) -> None:
        """Persist a memory event to the task file (always, regardless of mode)."""
        if task_id and self.session_store:
            try:
                self.session_store.append_raw_message(task_id, {
                    "type": "system",
                    "content": f"[Memory Compression] {message}",
                    "detail": detail or {},
                    "node": MEMORY_HOOK,
                })
            except Exception:
                pass  # Session persistence is best-effort

    def _emit_compaction_event(
        self,
        task_id: str,
        phase: str,
        message: str,
        *,
        category: str = "node",
        source: str = "memory_compression",
        detail: dict = None,
        duration_ms: float = 0.0,
    ) -> None:
        """Emit a status event for memory compression activity.

        Uses tracker.emit() directly to avoid disrupting the calling node's
        tracker state (_current_source, _start_time).

        CLI visibility: NODE category → debug mode only.
        Session persistence: always written regardless of mode.
        """
        event_detail = detail or {}

        # 1. Emit to status tracker (real-time CLI observability)
        try:
            from chaos_agent.observability.status_tracker import (
                get_tracker,
                StatusEvent,
            )
            event = StatusEvent(
                task_id=task_id,
                phase=phase,
                category=category,
                source=source,
                message=message,
                detail=event_detail,
                duration_ms=duration_ms,
            )
            # Primary fan-out target: the per-task tracker the legacy
            # CLI ``/api/v1/status-stream/{task_id}`` endpoint
            # subscribes to. Existing CLI consumers continue to
            # receive every memory_compression event unchanged.
            tracker = get_tracker(task_id)
            if tracker:
                tracker.emit(event)
            # Phase 4 fan-out target: the TS TUI's main turn SSE
            # (``POST /sessions/{sid}/turn``) subscribes to a tracker
            # keyed by ``f"tui-{tui_session_id}"`` so it can merge
            # memory_compaction events into the same stream that
            # carries token/tool/phase events. Without this fan-out,
            # the TS TUI sees a multi-second silent stall while
            # ``compact_memory()`` runs and assumes the connection
            # has hung. ``tui_session_id`` is captured at the top
            # of ``__call__`` (line ~47) so it's available even when
            # state.task_id has been mutated to an op-task-id by
            # intent_clarification.
            tui_sid = self._state_tui_session_id or ""
            if tui_sid:
                tui_tracker = get_tracker(f"tui-{tui_sid}")
                if tui_tracker:
                    tui_tracker.emit(event)
        except Exception:
            pass  # Status tracking is best-effort

        # 2. Persist to session store (always, regardless of CLI visibility)
        if task_id:
            self._persist_to_session(
                task_id,
                message,
                detail={**event_detail, "duration_ms": duration_ms} if duration_ms else event_detail,
            )


def merge_hook_updates(result: dict, hook_updates: dict) -> dict:
    """Merge hook state updates into node result, concatenating messages lists.

    The add_messages reducer processes items sequentially: RemoveMessages
    delete old entries, then new messages are appended. Hook's messages
    (RemoveMessages + summary) must precede node's messages (LLM response)
    so deletions are processed before additions.

    Without this, ``result.update(hook_updates)`` overwrites
    ``result["messages"]`` when both contain entries, causing the LLM
    response (and any injection messages) to be silently lost.
    """
    if not hook_updates:
        return result
    for key, value in hook_updates.items():
        if key == "messages" and key in result:
            result["messages"] = value + result["messages"]
        else:
            result[key] = value
    return result
