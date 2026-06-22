/**
 * TS mirrors of Python's StreamEvent (src/chaos_agent/agent/streaming.py)
 * and TUIEvent (src/chaos_agent/tui/events.py).
 *
 * The wire format is a single ``data: {...json...}\n\n`` SSE frame whose
 * JSON carries a discriminator field ``type``. We do NOT rely on SSE
 * ``event:`` rows because the Python side doesn't emit them.
 */

export type StreamEventType =
  | "token"
  | "thinking"
  | "llm_start"
  | "tool_start"
  | "tool_end"
  | "node_start"
  | "node_end"
  | "confirm"
  | "result"
  | "error"
  | "usage"
  | "memory_compaction"
  | "context_size"
  | "done";

export interface StreamEventBase {
  type: StreamEventType;
  task_id?: string;
  timestamp?: string;
}

export interface TokenEvent extends StreamEventBase {
  type: "token";
  content: string;
  node?: string;
}

export interface ThinkingEvent extends StreamEventBase {
  type: "thinking";
  content: string;
  node?: string;
}

/** LLM call started (on_chat_model_start). Carries no content —
 *  the TUI uses the arrival timestamp to stamp ``thoughtStartedAt``
 *  BEFORE the prefill phase, so "思考用时" includes prompt-processing
 *  latency rather than only the thinking-token streaming duration. */
export interface LlmStartEvent extends StreamEventBase {
  type: "llm_start";
  node?: string;
}

export interface ToolStartEvent extends StreamEventBase {
  type: "tool_start";
  tool_name: string;
  node?: string;
  /** LangChain run_id (UUID per tool invocation). Empty when the
   * server is on a pre-callId build — useStream falls back to a
   * synthetic ``${task_id}/${tool_name}`` key in that case. */
  call_id?: string;
}

export interface ToolEndEvent extends StreamEventBase {
  type: "tool_end";
  tool_name: string;
  content: string;
  node?: string;
  call_id?: string;
}

export interface NodeStartEvent extends StreamEventBase {
  type: "node_start";
  node: string;
  /** Stepper phase the node belongs to. Server populates this for
   *  events emitted via ``dispatch_phase_started`` so the TS TUI can
   *  drive PhaseStepperCard. ``"intent" | "safety" | "inject" |
   *  "verify" | "recovery"``. Older servers omit this field — the
   *  reducer treats absence as "no phase update". */
  phase?: string;
}

export interface NodeEndEvent extends StreamEventBase {
  type: "node_end";
  node: string;
  phase?: string;
}

export interface ConfirmEvent extends StreamEventBase {
  type: "confirm";
  content: string;
  /** Graph node that paused — ``intent_confirm`` (Layer 1, intent
   *  parse review) or ``confirmation_gate`` (Layer 2, plan + safety
   *  review). Drives the structured renderer in ConfirmMessage.tsx. */
  node?: string;
  /** Raw ``interrupt(value)`` dict from the server. Shape depends on
   *  ``node``:
   *    intent_confirm:    { type, fault_intent, summary, intent_confidence }
   *    confirmation_gate: { skill_name, target, plan_summary, safety_status, safety_reason }
   *  Older servers (pre-fix) omit this; renderers must fall back to
   *  ``content`` when absent. */
  payload?: Record<string, unknown>;
}

export interface ResultEvent extends StreamEventBase {
  type: "result";
  /** Stringified JSON envelope. Parse on demand.
   *  Legacy shape — older surfaces (some inject/recover endpoints)
   *  stuff their final envelope into ``content`` as a JSON string. */
  content?: string;
  /** Structured envelope. Used by newer surfaces that prefer typed
   *  fields over re-parsing JSON — currently the /compact SSE route
   *  populates this with ``{thread_id, tokens_before, tokens_after,
   *  tokens_saved, compacted, layer}``. Exactly one of ``content``
   *  or ``payload`` will be non-empty for any given result event. */
  payload?: Record<string, unknown>;
  /** Wall-clock duration of the operation in ms. Currently set by
   *  the /compact route so the TUI can render "压缩用时 1.2s". */
  duration_ms?: number;
}

export interface ErrorEvent extends StreamEventBase {
  type: "error";
  content: string;
}

export interface DoneEvent extends StreamEventBase {
  type: "done";
}

/**
 * Authoritative LLM token usage. Emitted by the server once per LLM call
 * (LangChain ``on_chat_model_end`` / ``on_llm_end``). The TUI accumulates
 * ``input_tokens`` + ``output_tokens`` into per-turn counters that drive
 * the LoadingIndicator's ``↓ N tokens`` tail and the
 * ``⚡ turn used N tokens`` summary appended at TURN_DONE — replacing
 * the prior client-side ``streamingChars / 4`` approximation.
 */
export interface UsageEvent extends StreamEventBase {
  type: "usage";
  input_tokens: number;
  output_tokens: number;
}

/**
 * Phase 4 — memory compaction lifecycle.
 *
 * Emitted by the server when ``PreReasoningHook`` decides to invoke the
 * LLM-based compactor. Three discrete phases:
 *
 *   - ``started``   — compaction is about to run; ``tokens_before`` is
 *                     the approximate input token count, the rest are
 *                     unknown yet (omitted from the wire frame because
 *                     of the falsy-strip).
 *   - ``completed`` — compactor returned cleanly; all numeric fields
 *                     populated.
 *   - ``failed``    — compactor raised; ``content`` carries the error
 *                     message, ``tokens_before`` and ``duration_ms``
 *                     populated, the rest may be 0.
 *
 * The lightweight tool-truncation path the hook also has does NOT emit
 * any of these — too fast to be worth a UI roundtrip. So every event
 * the TUI sees comes from the multi-second LLM summary path.
 *
 * All numeric fields are best-effort estimates from
 * ``count_tokens_approx``; the ``usage`` events on subsequent LLM calls
 * carry the authoritative figures used by ``LoadingIndicator``'s tail
 * counter.
 */
export interface MemoryCompactionEvent extends StreamEventBase {
  type: "memory_compaction";
  /** "started" | "completed" | "failed". Field absent on the wire when
   *  the server forgot to populate it; readers should default to
   *  "started" for safety so the UI doesn't tear down a spinner that
   *  never appeared. */
  compaction_phase?: "started" | "completed" | "failed";
  /** Pre-compaction approximate token count. Always present on
   *  ``started``; preserved on ``completed`` / ``failed`` for the
   *  delta calculation. */
  tokens_before?: number;
  /** Post-compaction approximate token count. Only populated on
   *  ``completed``. */
  tokens_after?: number;
  /** How many input messages got rolled into the summary. */
  messages_compacted?: number;
  /** Wall-clock duration of the compactor call (ms). */
  duration_ms?: number;
  /** Layer label — currently always ``"llm_summary"`` because the
   *  hook's ``"lightweight"`` path stays silent. */
  layer?: string;
  /** Human-readable status / error message (free-form). */
  content?: string;
}

/**
 * PreReasoningHook snapshot of state size, emitted after EVERY
 * reasoning step (whether compaction fired or not). Drives the
 * Footer's live ``current / window`` indicator. ``context_current_tokens``
 * is the same number the hook compares against the compaction trigger,
 * so the displayed percent corresponds 1:1 with compaction firing —
 * if the Footer reads "85%", the next ``count_tokens_approx`` check
 * is at the trigger line.
 *
 * All four fields ALWAYS arrive on the wire (forced by the server's
 * to_dict exception, so genuine ``0`` is distinguishable from absent).
 * Reducer can therefore use ``Number(x) || 0`` without ambiguity.
 */
export interface ContextSizeEvent extends StreamEventBase {
  type: "context_size";
  /** Post-hook estimated tokens in state.messages
   *  (``count_tokens_approx × 1.2`` safety margin). */
  context_current_tokens: number;
  /** The trigger threshold the hook compares against
   *  (``min(max - 13K, max × ratio)`` with 50% floor). */
  context_trigger_tokens: number;
  /** The configured ``context_max_tokens`` (LLM window size). */
  context_max_tokens: number;
  /** Number of messages currently in state.messages. */
  context_messages_count: number;
}

export type StreamEvent =
  | TokenEvent
  | ThinkingEvent
  | LlmStartEvent
  | ToolStartEvent
  | ToolEndEvent
  | NodeStartEvent
  | NodeEndEvent
  | ConfirmEvent
  | ResultEvent
  | ErrorEvent
  | UsageEvent
  | MemoryCompactionEvent
  | ContextSizeEvent
  | DoneEvent;

/** Type guard helper. */
export function isStreamEvent(value: unknown): value is StreamEvent {
  if (!value || typeof value !== "object") return false;
  const t = (value as { type?: unknown }).type;
  return (
    t === "token" ||
    t === "thinking" ||
    t === "llm_start" ||
    t === "tool_start" ||
    t === "tool_end" ||
    t === "node_start" ||
    t === "node_end" ||
    t === "confirm" ||
    t === "result" ||
    t === "error" ||
    t === "usage" ||
    t === "memory_compaction" ||
    t === "context_size" ||
    t === "done"
  );
}
