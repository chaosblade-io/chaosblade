"""Streaming event models and parsers for LangGraph astream_events."""

import json
import logging
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
        conversation_turn - Conversation turn completed (multi-invocation model)
    """

    type: str  # token | thinking | tool_start | tool_end | node_start | node_end | confirm | result | error | usage | memory_compaction
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
    # ``count_tokens_approx`` over the messages slated for compaction;
    # ``after`` is the same metric over the post-compaction tail. Both
    # are best-effort estimates (the LLM's actual token cost lands on
    # ``usage`` events) — the user just needs a relative magnitude to
    # judge whether the compaction freed real context.
    tokens_before: int = 0
    tokens_after: int = 0
    # How many input messages got rolled into the summary. Useful in
    # the post-compaction history line ("compacted 23 messages → 1
    # summary") so the user grasps the savings beyond raw tokens.
    messages_compacted: int = 0
    # Wall-clock duration of the compaction call in milliseconds.
    # ``count_tokens_approx`` is cheap; the LLM call is what makes
    # this multi-second-grade. Lets the TUI show "压缩用时 6.3s".
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
        return out

    def to_sse(self) -> str:
        """Format as SSE data line."""
        return f"data: {json.dumps(self.to_dict(), ensure_ascii=False)}\n\n"


def parse_stream_event(raw_event: dict) -> Optional[StreamEvent]:
    """Parse a LangGraph astream_events (v2) raw event into a StreamEvent.

    Handles:
    - on_chat_model_stream  → type=token (response content) or type=thinking (reasoning_content)
    - on_tool_start         → type=tool_start
    - on_tool_end           → type=tool_end

    When enable_thinking mode is active (e.g., Qwen), reasoning_content
    from the model is emitted as type=thinking events, separate from
    the regular token stream.

    Returns None for events we don't care about.
    """
    event_name = raw_event.get("event", "")

    if event_name == "on_chat_model_stream":
        # LLM token stream
        chunk = raw_event.get("data", {}).get("chunk")
        if chunk is None:
            return None

        # Check for thinking/reasoning content (from Qwen enable_thinking mode)
        additional_kwargs = getattr(chunk, "additional_kwargs", {}) or {}
        reasoning_content = additional_kwargs.get("reasoning_content", "")
        if reasoning_content:
            node = _extract_node_name(raw_event)
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
        # Determine source node from tags or name
        node = _extract_node_name(raw_event)
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
        prompt, completion = _extract_token_usage(output)
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
        return None

    # Silently ignore other events (on_chat_model_start, on_chain_start, etc.)
    return None


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
