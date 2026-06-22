"""Streaming event models and parsers for LangGraph astream_events."""

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


@dataclass
class StreamEvent:
    """A single streaming event emitted during graph execution.

    Types:
        token      - LLM token (partial text output)
        thinking   - LLM thinking/reasoning token (from enable_thinking mode)
        tool_start - Tool invocation started
        tool_end   - Tool invocation completed (content = result string)
        node_start - Graph node started
        node_end   - Graph node completed
        confirm    - Graph paused at confirmation_gate or intent_confirm
        result     - Final result envelope
        error      - Error during execution
        usage      - LLM call ended; carries authoritative token counts
        context_size - PreReasoningHook snapshot of post-hook state size
        memory_compaction - PreReasoningHook compaction lifecycle phase
        conversation_turn - Conversation turn completed (multi-invocation model)
    """

    type: str  # token | thinking | tool_start | tool_end | node_start | node_end | confirm | result | error | usage | memory_compaction | context_size
    content: str = ""
    node: str = ""
    tool_name: str = ""
    task_id: str = ""
    # Memory-compaction discriminator. Populated only on
    # ``type=memory_compaction`` events: ``"started"`` when the
    # PreReasoningHook decides to invoke the LLM summariser (Layer 2
    # path — Layer 1 lightweight truncation is too fast to bother with
    # a UI roundtrip and stays silent), ``"completed"`` when the
    # summary lands, ``"failed"`` if compact_memory raises. The TS
    # TUI uses this to drive the spinner ("started") → final history
    # row ("completed"/"failed") transition.
    compaction_phase: str = ""
    # Approximate token counts straddling the compaction. ``before`` is
    # ``count_tokens_messages(...).safe_count`` over the messages
    # slated for compaction; ``after`` is the same metric over the
    # post-compaction tail. Both are best-effort estimates that carry
    # an internal quality tag (EXACT / APPROXIMATE / HEURISTIC, see
    # ``chaos_agent.memory.tokens``) — the LLM's actual token cost
    # lands on ``usage`` events. The user just needs a relative
    # magnitude to judge whether the compaction freed real context.
    tokens_before: int = 0
    tokens_after: int = 0
    # How many input messages got rolled into the summary. Useful in
    # the post-compaction history line ("compacted 23 messages → 1
    # summary") so the user grasps the savings beyond raw tokens.
    messages_compacted: int = 0
    # Wall-clock duration of the compaction call in milliseconds.
    # ``count_tokens_messages`` is cheap (tiktoken Rust path is ~3-6×
    # the legacy heuristic); the LLM call is what makes this
    # multi-second-grade. Lets the TUI show "压缩用时 6.3s".
    duration_ms: float = 0.0
    # Layer label for diagnostics: ``"llm_summary"`` (the only path
    # that surfaces UI today) or ``"lightweight"`` (reserved for a
    # future change that decides to surface fast-path compaction too).
    # Empty string for non-compaction events; falsy-stripped from the
    # wire frame.
    layer: str = ""
    # Per-invocation id for tool calls. Sourced from LangChain's
    # ``run_id`` (a UUID stamped on every chain/tool run) so the TS
    # TUI can correlate ``tool_start`` and ``tool_end`` even when the
    # agent invokes the same tool multiple times in parallel within
    # one turn. Empty for non-tool events.
    call_id: str = ""
    # Stepper phase the event belongs to ("intent" | "safety" | "inject"
    # | "verify" | "recovery"), populated for ``node_start`` / ``node_end``
    # events emitted via ``dispatch_phase_started`` / ``dispatch_phase_completed``.
    # The TS TUI's PhaseStepperCard subscribes to phase transitions and
    # uses this to drive the 5-stage progress checklist sitting above the
    # input prompt. Empty string for every other event type — falsy so
    # ``to_dict`` drops it from the wire frame and older clients see the
    # familiar shape.
    phase: str = ""
    # Structured payload, currently used by ``confirm`` events to ship
    # the raw ``interrupt(value)`` dict from intent_confirm /
    # confirmation_gate to the TS TUI for fielded rendering. ``None``
    # for every other event type, in which case ``to_dict`` drops it
    # so older v1 clients see a frame indistinguishable from before.
    payload: Optional[dict] = None
    # LLM token usage, populated only on ``type=usage`` events. Both
    # default to 0 so the wire-format stripper in ``to_dict`` drops
    # them from every other event type — older TUI clients that
    # don't know the ``usage`` discriminator see no extra fields and
    # keep working unchanged.
    input_tokens: int = 0
    output_tokens: int = 0
    # Context-size snapshot, populated only on
    # ``type=context_size`` events. Emitted by PreReasoningHook after
    # every reasoning step so the TS TUI Footer can render a live
    # "state size / window" indicator. ``current_tokens`` is the
    # post-hook ``count_tokens_messages(...).safe_count`` (the exact
    # same number the trigger compares against — per-quality margin,
    # 1.0/1.05/1.20, see ``chaos_agent.memory.tokens``), so the
    # displayed percent corresponds 1:1 to compaction firing. All
    # default 0 so the wire-format stripper drops them on every other
    # event type.
    context_current_tokens: int = 0
    context_trigger_tokens: int = 0
    context_max_tokens: int = 0
    context_messages_count: int = 0
    timestamp: str = field(
        default_factory=lambda: now_iso()
    )

    def to_dict(self) -> dict:
        """Serialize to dict, omitting empty optional fields.

        ``None`` is falsy under the stripping rule below, so a
        ``payload=None`` (the default for every non-confirm event)
        is dropped from the wire frame entirely. Confirm events with
        a real dict payload survive — empty dicts {} are also dropped,
        matching the behaviour for empty strings on string fields.

        ``usage`` events are special-cased to ALWAYS carry both
        ``input_tokens`` and ``output_tokens`` on the wire, even when
        one of them is 0 (e.g. DashScope's prompt_cache_hit case can
        legitimately report ``input_tokens=0`` with a non-zero
        completion). Without this exception the falsy-strip drops
        the 0 from the JSON frame, the TS reducer's
        ``Math.max(0, undefined)`` returns ``NaN``, and the per-turn
        running total stays ``NaN`` for the rest of the turn — the
        live LoadingIndicator tail and the end-of-turn TurnUsageItem
        both go silent. Other event types keep the falsy-strip so
        older TUIs that don't know the ``input_tokens`` discriminator
        keep seeing the historical minimal frame shape.
        """
        d = asdict(self)
        out = {k: v for k, v in d.items() if v}
        if self.type == "usage":
            out["input_tokens"] = self.input_tokens
            out["output_tokens"] = self.output_tokens
        if self.type == "context_size":
            # Same rationale as ``usage`` above — defensively force
            # all four context_size fields onto the wire so the TS
            # reducer's ``Number(x) || 0`` defenses can recognise a
            # genuine ``0`` (empty thread on first hook call) vs an
            # absent field (older server). Without this, a fresh
            # state with ``current_tokens=0`` would get stripped and
            # the Footer would treat it as "no data yet".
            out["context_current_tokens"] = self.context_current_tokens
            out["context_trigger_tokens"] = self.context_trigger_tokens
            out["context_max_tokens"] = self.context_max_tokens
            out["context_messages_count"] = self.context_messages_count
        return out

    def to_sse(self) -> str:
        """Format as SSE data line."""
        return f"data: {json.dumps(self.to_dict(), ensure_ascii=False)}\n\n"


# Nodes whose LLM token / thinking chunks should NOT stream to the TUI.
# Their final output is delivered via the result envelope (e.g. ``save_memory``
# attaches the postmortem markdown to the result payload, which the TUI
# renders as PostmortemSection). Streaming the tokens would render the
# same content twice — once as agent text, once as the card.
# ``on_chat_model_end`` (→ usage event) is NOT gated, so token accounting
# stays accurate for the per-turn footer.
_SILENT_TOKEN_NODES: frozenset[str] = frozenset({"save_memory"})


def parse_stream_event(raw_event: dict) -> Optional[StreamEvent | list[StreamEvent]]:
    """Parse a LangGraph astream_events (v2) raw event into a StreamEvent.

    Handles:
    - on_chat_model_stream  → type=token (response content) or type=thinking (reasoning_content)
    - on_tool_start         → type=tool_start
    - on_tool_end           → type=tool_end
    - on_chat_model_end     → type=usage (token counts); or [type=token, type=usage]
                              when content is empty but reasoning_content has the
                              reply (Qwen enable_thinking short-response fallback)

    When enable_thinking mode is active (e.g., Qwen), reasoning_content
    from the model is emitted as type=thinking events, separate from
    the regular token stream.

    For short responses (e.g. "你好"), Qwen sometimes puts the entire
    reply inside ``<think>...</think>``, leaving ``content`` empty.
    In that case ``on_chat_model_end`` returns BOTH a synthetic token
    event (carrying reasoning_content) AND the usage event, so the user
    sees the reply and token counts are preserved.

    Returns None for events we don't care about.
    """
    event_name = raw_event.get("event", "")

    if event_name == "on_chat_model_stream":
        # LLM token stream
        chunk = raw_event.get("data", {}).get("chunk")
        if chunk is None:
            return None

        node = _extract_node_name(raw_event)
        # Drop token/thinking from nodes whose output is delivered via
        # the result envelope (postmortem). Usage events still pass
        # through on_chat_model_end so token counting stays correct.
        if node in _SILENT_TOKEN_NODES:
            return None

        # Check for thinking/reasoning content (from Qwen enable_thinking mode)
        additional_kwargs = getattr(chunk, "additional_kwargs", {}) or {}
        reasoning_content = additional_kwargs.get("reasoning_content", "")
        if reasoning_content:
            return StreamEvent(type="thinking", content=reasoning_content, node=node)

        content = getattr(chunk, "content", "") or ""
        # Defensive: LangChain content can be list (mixed content blocks)
        if isinstance(content, list):
            text_parts = [
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            content = "".join(text_parts)
        if not content:
            return None
        return StreamEvent(type="token", content=content, node=node)

    elif event_name == "on_tool_start":
        tool_name = raw_event.get("name", "")
        node = _extract_node_name(raw_event)
        run_id = str(raw_event.get("run_id") or "")
        return StreamEvent(
            type="tool_start",
            content="",
            node=node,
            tool_name=tool_name,
            call_id=run_id,
        )

    elif event_name == "on_tool_end":
        tool_name = raw_event.get("name", "")
        output = raw_event.get("data", {}).get("output", "")
        content = _extract_tool_output_content(output)
        node = _extract_node_name(raw_event)
        run_id = str(raw_event.get("run_id") or "")
        return StreamEvent(
            type="tool_end",
            content=content,
            node=node,
            tool_name=tool_name,
            call_id=run_id,
        )

    elif event_name in ("on_chat_model_end", "on_llm_end"):
        # Authoritative LLM token usage. LangChain emits this once per
        # LLM call (after all chunks for that call have streamed). We
        # extract (prompt, completion) and forward as a ``usage`` event
        # so the TUI can display per-turn cumulative counts grounded in
        # the model's reported numbers — unifying the prior frontend
        # chars/4 estimate with the backend's authoritative source
        # already used by the persistence layer (TaskTrace).
        from chaos_agent.observability.tracer import _extract_token_usage
        output = raw_event.get("data", {}).get("output")
        if output is None:
            return None

        # Qwen enable_thinking short-response fallback: when the model
        # puts the entire reply inside <think>…</think>, ``content`` is
        # empty and no token events were streamed during on_chat_model_stream.
        # Return BOTH a synthetic token (carrying reasoning_content) AND
        # the usage event so the user sees the reply AND token counts.
        node = _extract_node_name(raw_event)
        synthetic_token: StreamEvent | None = None
        if node not in _SILENT_TOKEN_NODES:
            _content = getattr(output, "content", "") or ""
            if isinstance(_content, list):
                _content = "".join(
                    p.get("text", "") for p in _content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            if not _content.strip():
                _ak = getattr(output, "additional_kwargs", {}) or {}
                _rc = _ak.get("reasoning_content", "") or ""
                if _rc.strip():
                    synthetic_token = StreamEvent(
                        type="token", content=_rc, node=node,
                    )

        prompt, completion = _extract_token_usage(output)
        if synthetic_token:
            events = [synthetic_token]
            if prompt or completion:
                events.append(StreamEvent(
                    type="usage",
                    input_tokens=int(prompt),
                    output_tokens=int(completion),
                ))
            return events
        if not (prompt or completion):
            return None
        return StreamEvent(
            type="usage",
            input_tokens=int(prompt),
            output_tokens=int(completion),
        )

    # Custom domain events emitted by nodes via adispatch_custom_event.
    # These replace the fragile on_chain_start/on_chain_end parsing.
    elif event_name == "on_custom_event":
        custom_name = raw_event.get("name", "")
        data = raw_event.get("data", {})
        if custom_name == "phase_started":
            return StreamEvent(
                type="node_start",
                content="",
                node=data.get("node", ""),
                phase=data.get("phase", "") or "",
            )
        elif custom_name == "phase_completed":
            return StreamEvent(
                type="node_end",
                content="",
                node=data.get("node", ""),
                phase=data.get("phase", "") or "",
            )
        elif custom_name == "node_message":
            content = data.get("content", "")
            if content:
                return StreamEvent(
                    type="token",
                    content=content,
                    node=data.get("node", ""),
                )
        elif custom_name == "batch_fault_result":
            result_entry = data.get("result", {})
            task_state = result_entry.get("task_state", "unknown")
            import json as _json
            envelope_data = {
                "task_id": result_entry.get("task_id", ""),
                "task_state": task_state,
                "fault_type": result_entry.get("fault_type", ""),
                "blade_uid": result_entry.get("blade_uid", ""),
                "duration_ms": result_entry.get("duration_ms", 0),
            }
            for k in ("target", "verification", "side_effects", "postmortem",
                       "failure_reason", "failure_detail", "side_effects_summary"):
                v = result_entry.get(k)
                if v is not None:
                    envelope_data[k] = v
            return StreamEvent(
                type="result",
                content=_json.dumps({
                    "status": "success" if task_state in ("injected", "recovered", "partial_recovered") else "fail",
                    "data": envelope_data,
                }, ensure_ascii=False),
            )
        return None

    # Silently ignore other events (on_chat_model_start, on_chain_start, etc.)
    return None


def parse_stream_events(raw_event: dict) -> list[StreamEvent]:
    """Like :func:`parse_stream_event` but always returns a list (may be empty).

    Convenience wrapper so callers don't need to handle the ``Optional``
    / ``list`` union return type of :func:`parse_stream_event`::

        for stream_evt in parse_stream_events(event):
            stream_evt.task_id = task_id
            yield stream_evt
    """
    parsed = parse_stream_event(raw_event)
    if parsed is None:
        return []
    if isinstance(parsed, list):
        return parsed
    return [parsed]


def _extract_tool_output_content(output) -> str:
    """Extract a clean string from a LangGraph tool output payload.

    LangChain emits tool results as ``ToolMessage`` objects (or plain
    strings, or sometimes a list of content blocks). The previous
    implementation called ``str(output)`` directly on whatever came
    back, which on ``ToolMessage`` produces the dataclass-style repr
    ``"content='...' name='kubectl' tool_call_id='...'"``. That repr
    leaked into the TUI as the ``⎿ content='...'`` line every user
    saw on every multi-line tool result — a high-frequency UI artefact.

    Resolution order:

    1. ``output.content``  — the canonical LangChain ToolMessage path.
    2. ``output["content"]`` — defensive for callers that pass a dict
       (e.g. some test fixtures or future structured payloads).
    3. ``str(output)`` — last resort fallback for plain strings or
       anything else that's already string-shaped.

    A list of content blocks (LangChain mixed content) is flattened
    by joining ``text`` parts in order, mirroring the ``token`` path
    in :func:`parse_stream_event`.
    """
    if not output:
        return ""

    # Case 1: ToolMessage (or any object with .content)
    inner = getattr(output, "content", None)
    if inner is None and isinstance(output, dict):
        inner = output.get("content")

    if inner is not None:
        # Sometimes content itself is a list of blocks.
        if isinstance(inner, list):
            parts: list[str] = []
            for part in inner:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
                else:
                    parts.append(str(part))
            return "".join(parts)
        return str(inner)

    return str(output)


def _extract_node_name(raw_event: dict) -> str:
    """Extract the graph node name from astream_events metadata."""
    # LangGraph v2 astream_events: tags often contain ["langsmith:nodes:<node_name>"]
    tags = raw_event.get("tags", [])
    for tag in tags:
        if tag.startswith("langsmith:nodes:"):
            return tag.split(":")[-1]
    # Fallback: try metadata
    metadata = raw_event.get("metadata", {})
    if "langgraph_node" in metadata:
        return metadata["langgraph_node"]
    return ""


# ---------------------------------------------------------------------------
# Server-side SSE token batching
# ---------------------------------------------------------------------------

_BATCHABLE_TYPES = frozenset(("token", "thinking"))


class SSEBatcher:
    """Accumulates token/thinking events and flushes on deadline or size.

    Usage inside an event loop::

        batcher = SSEBatcher()
        for event in stream:
            for sse_str in batcher.feed(event):
                yield sse_str
        for sse_str in batcher.flush():
            yield sse_str

    Non-batchable events pass through immediately (after flushing any
    pending batch so event ordering is preserved).

    When ``flush_interval_ms <= 0``, batching is disabled and every
    event passes through immediately (zero-overhead).
    """

    __slots__ = (
        "_interval_s", "_flush_chars", "_disabled",
        "_token_buf", "_token_node",
        "_thinking_buf", "_thinking_node",
        "_batch_start", "_task_id",
    )

    def __init__(self, flush_interval_ms: int = 30, flush_chars: int = 30):
        self._disabled = flush_interval_ms <= 0
        self._interval_s = flush_interval_ms / 1000.0
        self._flush_chars = flush_chars
        self._token_buf = ""
        self._token_node = ""
        self._thinking_buf = ""
        self._thinking_node = ""
        self._batch_start: float = 0.0
        self._task_id = ""

    def _flush_pending(self) -> list[str]:
        out: list[str] = []
        if self._token_buf:
            out.append(StreamEvent(
                type="token", content=self._token_buf,
                node=self._token_node, task_id=self._task_id,
            ).to_sse())
            self._token_buf = ""
            self._token_node = ""
        if self._thinking_buf:
            out.append(StreamEvent(
                type="thinking", content=self._thinking_buf,
                node=self._thinking_node, task_id=self._task_id,
            ).to_sse())
            self._thinking_buf = ""
            self._thinking_node = ""
        self._batch_start = 0.0
        return out

    def _has_pending(self) -> bool:
        return bool(self._token_buf or self._thinking_buf)

    def _deadline_exceeded(self) -> bool:
        if self._batch_start <= 0.0:
            return False
        return (time.monotonic() - self._batch_start) >= self._interval_s

    def feed(self, evt: StreamEvent) -> list[str]:
        """Accept one event; return zero or more SSE strings to yield."""
        if self._disabled:
            return [evt.to_sse()]

        if evt.task_id:
            self._task_id = evt.task_id

        # Check time-based flush before processing the new event.
        result: list[str] = []
        if self._has_pending() and self._deadline_exceeded():
            result.extend(self._flush_pending())

        if evt.type not in _BATCHABLE_TYPES:
            # Structural event: flush pending first, then pass through.
            result.extend(self._flush_pending())
            result.append(evt.to_sse())
            return result

        # Accumulate batchable event.
        if not self._has_pending():
            self._batch_start = time.monotonic()

        if evt.type == "token":
            self._token_buf += evt.content
            if evt.node:
                self._token_node = evt.node
        else:
            self._thinking_buf += evt.content
            if evt.node:
                self._thinking_node = evt.node

        # Size threshold flush.
        if (len(self._token_buf) + len(self._thinking_buf)) >= self._flush_chars:
            result.extend(self._flush_pending())

        return result

    def flush(self) -> list[str]:
        """Force-flush any remaining buffered content."""
        if self._disabled:
            return []
        return self._flush_pending()
