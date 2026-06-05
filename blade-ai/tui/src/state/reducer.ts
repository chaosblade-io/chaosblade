/**
 * State reducer. All updates funnel through here.
 *
 * Structure mirrors Qwen Code's split:
 *   - ``history`` is immutable once an item is in (ready for ``<Static>``).
 *   - ``pending`` is the mutable workspace for the in-flight turn.
 * On TURN_DONE we slice ``pending`` into ``history`` in one shot, so
 * Static only ever sees a stable suffix grow.
 */

import { findLastSafeSplitPoint } from "../utils/markdownSplit.js";
import { perfMark } from "../utils/perfTrace.js";
import { parseResultEnvelope } from "../utils/result.js";
import {
  INJECT_PHASE_ORDER,
  type AgentItem,
  type AppState,
  type ConfirmContextItem,
  type ConfirmPromptItem,
  type HistoryItem,
  type PhaseName,
  type PhaseStatus,
  type PhaseStep,
  type PhaseStepperItem,
  type ResultItem,
  type ThinkingItem,
  type ToolItem,
  type ToolStatus,
  type TurnUsageItem,
} from "./types.js";

// ---------------------------------------------------------------------------
// Phase stepper helpers
// ---------------------------------------------------------------------------

/** Map a server-emitted ``(node, phase)`` pair to the todo-list step
 *  that should be marked active. Returns ``null`` when the event is
 *  outside the inject pipeline (older servers, future custom events,
 *  recovery-graph events the strip doesn't model).
 *
 *  Why node-aware: the graph emits ``phase=inject`` from BOTH
 *  agent_loop (planning, pre-safety) and execute_loop / baseline_capture
 *  / direct_execute (post-safety, blade calls). Mapping naively on
 *  ``phase`` alone would let the monotonic ratchet jump past
 *  ``safety`` the instant agent_loop fires, painting safety as
 *  completed before safety_check has run. We split inject into two
 *  todo-list steps and disambiguate by node name here.
 *
 *  ``node=intent_confirm`` is graph-tagged ``phase=safety`` so the
 *  Layer-1 confirm wait paints a stepper indicator while the user
 *  reads the confirm card. Conceptually it's still part of "intent"
 *  (the user is confirming the intent they just expressed in NL),
 *  not a safety check on a derived plan. We re-tag it to the
 *  ``intent`` step here so the strip doesn't light "safety" before
 *  agent_loop runs. */
function mapNodeToStep(
  node: string,
  phase: string,
): PhaseName | null {
  if (node === "batch_setup") return null;
  if (phase === "intent") return "intent";
  if (phase === "verify") return "verify";
  if (phase === "safety") {
    if (node === "intent_confirm") return "intent";
    return "safety";
  }
  if (phase === "inject") {
    if (node === "agent_loop") return "agent_loop";
    return "execute";
  }
  return null;
}

/** Build a fresh PhaseStepperItem snapshot.
 *
 *  Earlier phases (lower index than ``activeIndex``) are marked
 *  ``completed``; the active phase is ``in_progress``; later phases
 *  ``pending``. Used by NODE_STARTED to materialise the stepper the
 *  first time a non-intent phase fires for a turn. */
function buildPhaseStepper(
  id: string,
  activeIndex: number,
): PhaseStepperItem {
  const steps: PhaseStep[] = INJECT_PHASE_ORDER.map((phase, idx) => ({
    phase,
    status:
      idx < activeIndex
        ? "completed"
        : idx === activeIndex
          ? "in_progress"
          : "pending",
  }));
  return { kind: "phase_stepper", id, mode: "inject", steps };
}

/** Advance a PhaseStepperItem so ``activeIndex`` is the in-progress
 *  step — but **monotonically forward only**. Steps already at a more
 *  advanced status never roll back to a less-advanced one.
 *
 *  Why monotonic matters: LangGraph's astream_events replays earlier
 *  phase events when resuming after an interrupt
 *  (``Command(resume=...)``). Without this guard a replayed
 *  ``phase_started safety`` event arriving after we've already
 *  progressed to ``inject`` would naively rewrite ``inject`` back to
 *  ``pending``, producing a visible regress / re-progress flicker
 *  in the stepper card. The same root cause we fixed in the
 *  TOOL_STARTED replay-guard, applied to phase transitions.
 *
 *  Rules:
 *    - ``idx < activeIndex``  → ratchet up to ``completed`` (never down)
 *    - ``idx === activeIndex`` → if pending, become in_progress; if
 *                                already completed, stay completed
 *                                (don't regress)
 *    - ``idx > activeIndex``  → leave alone (replay event for an
 *                                earlier phase must NOT touch later
 *                                steps)
 */
function applyActivePhase(
  stepper: PhaseStepperItem,
  activeIndex: number,
): PhaseStepperItem {
  let mutated = false;
  const steps = stepper.steps.map((step, idx) => {
    let next = step.status;
    if (idx < activeIndex) {
      if (step.status !== "completed") next = "completed";
    } else if (idx === activeIndex) {
      if (step.status === "pending") next = "in_progress";
    }
    // idx > activeIndex: untouched. A replay of an earlier phase
    // must never rewind ``inject`` / ``verify`` to pending.
    if (next !== step.status) {
      mutated = true;
      return { ...step, status: next };
    }
    return step;
  });
  return mutated ? { ...stepper, steps } : stepper;
}

/** Freeze the stepper into history at the end of a turn.
 *
 *  Two modes, driven by whether the turn ended cleanly or aborted:
 *
 *    failed=false (TURN_DONE) — the pipeline reached a terminal node
 *      under its own steam. Anything not yet completed (the trailing
 *      in_progress step plus any pending later steps that the graph
 *      may have skipped) is rounded up to ``completed``: by definition
 *      the run finished, so the strip honestly says "all done".
 *
 *    failed=true  (TURN_ABORTED) — the run was interrupted: Esc /
 *      network drop / unhandled exception. The active in_progress
 *      step flips to ``failed`` (red ✗); already-completed prefix
 *      stays completed (those steps did run); later pending steps
 *      stay pending (they never ran). This produces an honest
 *      record like ``[✓ ✓ ✗ ○]`` instead of the misleading "all ✓"
 *      that the previous TURN_DONE-shape finalisation produced.
 */
function finalisePhaseStepper(
  stepper: PhaseStepperItem,
  options: { failed?: boolean } = {},
): PhaseStepperItem {
  const failed = options.failed ?? false;
  let mutated = false;
  const steps = stepper.steps.map((step) => {
    let next: PhaseStatus = step.status;
    if (failed) {
      // Failed turn: only the active step transitions; everything
      // else holds (completed prefix, untouched pending tail).
      if (step.status === "in_progress") next = "failed";
    } else {
      // Successful turn: round up everything to completed.
      if (step.status !== "completed") next = "completed";
    }
    if (next !== step.status) {
      mutated = true;
      return { ...step, status: next };
    }
    return step;
  });
  return mutated ? { ...stepper, steps } : stepper;
}

/**
 * Pull the next monotonic item id from the state and return both the
 * id and the bumped counter. Callers thread the new counter through
 * their returned state so the reducer remains pure.
 */
function nextId(state: AppState, prefix: string): { id: string; counter: number } {
  const counter = state.nextItemId + 1;
  return { id: `${prefix}-${counter}`, counter };
}

/**
 * Close the current thinking session, if any, by appending a
 * ``ThinkingItem`` to ``pending`` and clearing the buffer + start
 * timestamp. Idempotent: returns the same state object when no session
 * is active so callers can invoke it unconditionally without forcing a
 * re-render.
 *
 * Called at every transition out of thinking — TOKEN_APPENDED (first
 * agent text after thinking), TOOL_STARTED (tool call interrupts
 * thinking), and inside ``commitPending`` (turn ended mid-thought).
 * The result is a clean ``▸ Thought for Ns`` row in scrollback for
 * each discrete thinking session, even when a single turn contains
 * multiple alternating thinking/tool phases.
 */
/**
 * Promote any tool that's still in ``running`` status to ``success``.
 *
 * Why this exists — the ``submit_fault_intent`` leak documented at
 * length in ``TOOL_ENDED``:
 *
 *   When a node returns and the graph immediately transitions into an
 *   ``interrupt()`` (intent_clarification → intent_confirm being the
 *   canonical case), LangGraph's ``astream_events`` re-stamps the
 *   ``on_tool_end`` event with a different ``run_id`` than its
 *   matching ``on_tool_start``. ``TOOL_ENDED`` falls back to a
 *   name-based match for that exact case, but in practice (verified
 *   by the overflow probe — pending[0] = ``tg(0/1)`` for 543 / 543
 *   pending frames in a real inject session) the fallback ALSO
 *   doesn't always fire — likely because the on_tool_end frame is
 *   dropped from the stream entirely rather than just re-keyed.
 *
 * The semantic invariant that lets us safely flip the status: the
 * graph cannot reach ``interrupt()`` without the preceding tool
 * having returned. So at every CONFIRM_RECEIVED dispatch (and
 * potentially at every NODE_STARTED that crosses a phase boundary),
 * any ``running`` tool in pending is by definition done — we just
 * never received the wire event that says so. Marking it ``success``
 * here is a faithful reflection of the underlying graph state, not
 * a fake completion.
 *
 * Without this fix: pending[0] stays at a stuck tool_group forever,
 * ``flushLeadingStable`` can't advance past it, every subsequent
 * stable item (resolved confirm cards, completed tool groups,
 * thinking sessions) piles up behind it, and the dynamic frame
 * grows linearly until TURN_DONE finally commits everything in one
 * burst. With pending growing to 13–21 items × ~8 rows each, the
 * frame routinely exceeds viewport by 80–135 rows (rows=47, frame
 * up to 181) — every render then leaks the overflow rows into
 * scrollback because of the ink+7.0.3 patch behaviour, producing
 * the user-visible "循环输出 + 滚轮锁底".
 *
 * Idempotency: returns the original ``pending`` reference if no tool
 * was running, so callers can call this unconditionally without
 * forcing extra reducer work / re-renders.
 *
 * No locator allocation: a sanitized tool's real output left via the
 * confirm payload (run_id was rewritten at the interrupt() boundary,
 * so ``on_tool_end`` never reached us), so ``/show T#`` would have
 * nothing meaningful to surface. Locator stays unallocated.
 *
 * Card body: ``placeholderKey`` is set so ToolMessage's empty-body
 * branch renders ``tool.captured_in_confirm`` instead of the default
 * ``tool.no_output`` — the output exists in the confirm card directly
 * below this one, not nowhere. Storing the i18n key (not the resolved
 * string) keeps the reducer pure.
 */
function sanitizeStuckTools(pending: HistoryItem[]): HistoryItem[] {
  let mutated = false;
  const next = pending.map((item) => {
    if (item.kind !== "tool_group") return item;
    let groupChanged = false;
    const tools = item.tools.map((tool) => {
      if (tool.status !== "running") return tool;
      groupChanged = true;
      mutated = true;
      const elapsed = Date.now() - tool.startedAt;
      return {
        ...tool,
        status: "success" as const,
        raw: "",
        resultPreview: "",
        placeholderKey: "tool.captured_in_confirm",
        elapsedMs: elapsed,
      };
    });
    return groupChanged ? { ...item, tools } : item;
  });
  return mutated ? next : pending;
}

/**
 * Peel leading "stable" pending items off the front and into history.
 * Stable means: never mutates again, so safe to commit to ``<Static>``
 * (which is append-only and doesn't repaint).
 *
 * Stability rules (kind → predicate):
 *   - ``thinking``     — always stable (produced by commitThinking,
 *                        immutable after creation)
 *   - ``tool_group``   — stable iff every tool is in a terminal status
 *                        (success / error / canceled). A running tool
 *                        still has a TOOL_ENDED in its future.
 *   - ``agent``        — stable iff it is NOT the current trailing
 *                        item. Token streaming only ever appends to
 *                        the tail AgentItem (see TOKEN_APPENDED tail
 *                        check); once a non-agent item lands behind
 *                        an AgentItem, that AgentItem is finalised.
 *                        Without this rule, multi-step turns
 *                        (verifier_loop iterating agent ⇄ tools 5–10×)
 *                        accumulate stale ``[Agent, Tool, Agent,
 *                        Tool, …]`` pairs in pending — every leading
 *                        AgentItem is "stable in fact but not in our
 *                        old list", so flush stops at index 0 and
 *                        pending grows past stdout.rows. That's the
 *                        post-confirmation-gate flicker / scroll-
 *                        hijack the user reported in session
 *                        sess_271c179fc814: 5+ accumulated
 *                        Agent/Tool pairs × ≈10 rows each = >50
 *                        rows of pending alone, well past most
 *                        terminals' viewports, tripping Ink's
 *                        fullscreen-redraw branch which BYPASSES
 *                        the maxFps throttle.
 *   - ``confirm``      — stable iff resolved (user already answered;
 *                        the resolved badge text is final).
 *
 * Any other in-flight item BREAKS the chain — flushing past it would
 * commit items out of chronological order, since Static is
 * append-only.
 *
 * Idempotent: returns the same state object reference if nothing
 * changed. Callers wire this into every reducer branch where pending
 * could be growing — TOKEN_APPENDED, TOOL_STARTED, TOOL_ENDED — so
 * the dynamic frame stays small relative to ``stdout.rows`` and Ink's
 * fullscreen-redraw branch never trips.
 */
function flushLeadingStable(state: AppState): AppState {
  let flushCount = 0;
  const len = state.pending.length;
  // Partial-flush slot for the boundary tool_group: when the first
  // non-fully-stable item in pending is a tool_group with a leading
  // run of already-completed tools, we split it — the completed
  // prefix flushes as a fresh ``tool_group`` to history; the
  // remainder (running + later tools) replaces the original at the
  // pending head. This keeps the dynamic frame from accumulating
  // every Phase 1 / verifier_loop tool just because a single later
  // tool is still in flight.
  let partialFlushTail: HistoryItem | null = null;
  let partialKeepHead: HistoryItem | null = null;
  // Track id-allocator drift across the partial-split path so the
  // flushed prefix gets a *fresh* id while the kept remainder
  // retains the original id (subsequent ``TOOL_ENDED`` dispatches
  // look the still-running tool up by callId inside that group, so
  // changing its id mid-flight would lose the match). Without this
  // the prefix and remainder would share the same id and produce
  // duplicate React keys inside ``<Static>``.
  let nextItemIdAfter = state.nextItemId;
  while (flushCount < len) {
    const item = state.pending[flushCount];
    if (!item) break;
    const isTail = flushCount === len - 1;
    let stable = false;
    if (item.kind === "thinking") {
      // Thinking is always immutable, BUT: don't flush it when the
      // very next item is a still-streaming tail agent. Ink's renderer
      // appends "\n" after every Static flush (renderer.js L45); if
      // thinking lands in Static while agent stays in pending, that
      // trailing "\n" + agent's marginTop={1} produces a double blank
      // line between "▸ 思考用时" and "⏺ reply". Keeping them
      // together in pending avoids the Static/pending boundary gap.
      const next = state.pending[flushCount + 1];
      stable = !(next?.kind === "agent" && flushCount + 1 === len - 1);
    } else if (item.kind === "tool_group") {
      stable = item.tools.every((tool) => tool.status !== "running");
    } else if (item.kind === "agent") {
      // Tail agent may still be receiving tokens; non-tail means a
      // newer item has landed behind it and the agent text is frozen.
      stable = !isTail;
    } else if (item.kind === "confirm_prompt") {
      stable = item.resolved === true;
    }
    if (stable) {
      flushCount++;
      continue;
    }
    // First non-fully-stable item. If it's a partially-completed
    // tool_group, harvest the completed prefix into history.
    if (item.kind === "tool_group") {
      const splitAt = item.tools.findIndex((tool) => tool.status === "running");
      if (splitAt > 0) {
        const flushedAlloc = nextId(
          { ...state, nextItemId: nextItemIdAfter },
          "g",
        );
        nextItemIdAfter = flushedAlloc.counter;
        partialFlushTail = {
          ...item,
          id: flushedAlloc.id,
          tools: item.tools.slice(0, splitAt),
        };
        partialKeepHead = {
          ...item,
          tools: item.tools.slice(splitAt),
        };
      }
    }
    break;
  }
  if (flushCount === 0 && partialFlushTail === null) return state;
  const flushed = state.pending.slice(0, flushCount);
  const newPending: HistoryItem[] = [];
  if (partialKeepHead) {
    newPending.push(partialKeepHead);
    // The original boundary item is at index ``flushCount``; we've
    // replaced it with ``partialKeepHead``, so skip it and keep the
    // tail items beyond it.
    newPending.push(...state.pending.slice(flushCount + 1));
  } else {
    newPending.push(...state.pending.slice(flushCount));
  }
  const newHistory = partialFlushTail
    ? [...state.history, ...flushed, partialFlushTail]
    : [...state.history, ...flushed];
  return {
    ...state,
    history: newHistory,
    pending: newPending,
    nextItemId: nextItemIdAfter,
  };
}

function commitThinking(state: AppState): AppState {
  if (state.thoughtBuffer.length === 0 && state.thoughtStartedAt === 0) {
    return state;
  }
  // Suppress mid-content thinking: once the first ThinkingItem for a
  // response segment has been committed and content tokens have started
  // flowing, subsequent reasoning chunks within the same segment are
  // silently discarded. This prevents trailing "▸ 思考用时 <1s" blocks
  // after the response text — caused by Qwen's enable_thinking mode
  // emitting reasoning_content interleaved with content.
  if (state.suppressMidContentThinking) {
    return {
      ...state,
      thoughtBuffer: "",
      thoughtSubject: "",
      thoughtStartedAt: 0,
      hasActiveThinking: false,
    };
  }
  const durationMs =
    state.thoughtStartedAt > 0 ? Date.now() - state.thoughtStartedAt : 0;
  const { id, counter } = nextId(state, "th");
  const item: ThinkingItem = {
    kind: "thinking",
    id,
    durationMs,
  };
  return {
    ...state,
    pending: [...state.pending, item],
    thoughtBuffer: "",
    thoughtSubject: "",
    thoughtStartedAt: 0,
    hasActiveThinking: false,  // N → 0 edge: session committed to pending
    nextItemId: counter,
  };
}

export type Action =
  | { type: "TURN_STARTED"; input: string }
  | { type: "TOKEN_APPENDED"; content: string; node: string }
  | { type: "THINKING_APPENDED"; content: string; node: string }
  | { type: "USAGE_RECEIVED"; inputTokens: number; outputTokens: number }
  | { type: "TOOL_STARTED"; callId: string; name: string; node: string }
  | {
      type: "TOOL_ENDED";
      callId: string;
      name: string;
      status: ToolStatus;
      content: string;
    }
  | { type: "NODE_STARTED"; node: string; phase?: string }
  | { type: "NODE_ENDED"; node: string }
  | {
      type: "CONFIRM_RECEIVED";
      content: string;
      taskId?: string;
      node?: string;
      payload?: Record<string, unknown>;
    }
  | { type: "CONFIRM_RESOLVED"; taskId: string; answer: string }
  | { type: "RESULT_RECEIVED"; content: string; taskId?: string }
  | { type: "ERROR_RECEIVED"; message: string; taskId?: string }
  /**
   * Phase 4 — memory-compaction lifecycle.
   *
   * STARTED:   server hit the LLM-summary path; ``state.currentCompaction``
   *            is populated and the LoadingIndicator yields the spinner
   *            slot to ``MemoryCompactingIndicator``.
   * COMPLETED: compactor returned cleanly; we materialise a
   *            ``MemoryCompactionItem`` into ``pending`` (so it lands
   *            in scrollback at TURN_DONE alongside the rest of the
   *            turn block) and clear ``currentCompaction``.
   * FAILED:    compactor raised; same item shape but ``succeeded=false``
   *            with the error message preserved, then clear.
   *
   * All three actions are idempotent vs. ``currentCompaction`` —
   * COMPLETED with no prior STARTED still appends a history row (we'd
   * rather over-report than swallow a real compaction the wire saw).
   */
  | {
      type: "MEMORY_COMPACTION_STARTED";
      tokensBefore: number;
      layer: string;
    }
  | {
      type: "MEMORY_COMPACTION_COMPLETED";
      tokensBefore: number;
      tokensAfter: number;
      messagesCompacted: number;
      durationMs: number;
      layer: string;
    }
  | {
      type: "MEMORY_COMPACTION_FAILED";
      tokensBefore: number;
      durationMs: number;
      layer: string;
      errorMessage: string;
    }
  /**
   * PreReasoningHook snapshot of post-hook state size. Fires after
   * EVERY reasoning step regardless of whether compaction ran, so
   * the Footer indicator stays live during the whole turn. Persists
   * across turns — NOT reset on TURN_STARTED.
   */
  | {
      type: "CONTEXT_SIZE_RECEIVED";
      currentTokens: number;
      triggerTokens: number;
      maxTokens: number;
      messagesCount: number;
    }
  /**
   * Manual ``/compact`` lifecycle. Fired by the slash handler
   * around its ``streamCompactSession`` call. Drives the
   * ManualCompactIndicator spinner (uniform UX whether the actual
   * compaction is noop / strip-only / LLM-driven). ``STARTED``
   * records the wall-clock for the elapsed-seconds tail; ``DONE``
   * clears the slot so the spinner unmounts.
   */
  | { type: "COMPACT_MANUAL_STARTED" }
  | { type: "COMPACT_MANUAL_DONE" }
  | { type: "TURN_DONE" }
  | { type: "TURN_ABORTED"; reason: string }
  | { type: "MODE_TOGGLED"; mode: AppState["config"]["permissionMode"] }
  /**
   * Display-density toggle (mirrors Python TUI's ``/mode
   * calm|working|dense``). Independent from ``MODE_TOGGLED`` (which
   * toggles the ``permissionMode`` orthogonally). Both names — display
   * mode and permission mode — historically lived under ``/mode`` on
   * the TS side and ``/mode`` on the Python side meaning DIFFERENT
   * things. Phase 1 split the TS ``/mode`` so display density wins
   * the ``/mode`` slot (matching Python) and the permission toggle
   * moves to ``/permission``.
   */
  | {
      type: "DISPLAY_MODE_CHANGED";
      mode: AppState["config"]["displayMode"];
    }
  | { type: "LOG_APPENDED"; level: "info" | "warn" | "ok"; text: string }
  | { type: "HISTORY_CLEARED" }
  // M8: replay lifecycle. REPLAY_STARTED flips streamState into a
  // pseudo-busy mode so InputPrompt unsubscribes (no accidental new
  // turn races with the timed setTimeout chain). REPLAY_ENDED commits
  // pending → history and reverts to idle. Both are pure state changes;
  // the AbortController + setTimeout chain live in commands.ts.
  | { type: "REPLAY_STARTED"; taskId: string }
  | { type: "REPLAY_ENDED"; aborted: boolean }
  /**
   * Bumped before any recovery side effects fire — same counter
   * Python ``app.py:313`` maintains for the goodbye card. Reserved
   * for when the TS TUI ships its own ``/recover`` slash command;
   * until then nothing dispatches this and ``recoveryCount`` stays
   * 0, matching the Python behaviour for users who never ran the
   * command.
   */
  | { type: "RECOVERY_TRIGGERED" }
  /**
   * Boot-progress indicator. Set the label while a boot phase is
   * running (preflight / pending-tasks), clear when it completes.
   * Drives the spinner row rendered between the static welcome card
   * and the input prompt.
   */
  | { type: "BOOT_PROGRESS_SHOW"; text: string }
  | { type: "BOOT_PROGRESS_HIDE" }
  /**
   * Toggle ``constrainHeight``. Bound to Ctrl+O in Composer's
   * ``useInput`` so the user can flip pending-item height-cap on/off
   * when a card was truncated. While off, ``MaxSizedBox`` skips
   * truncation and the dynamic frame is allowed to overflow viewport
   * — overflow contents land in scrollback until the user toggles
   * back. Mirrors qwen-code's Ctrl+S binding (we picked O so it
   * doesn't collide with shell ``stop output``).
   */
  | { type: "CONSTRAIN_HEIGHT_TOGGLED" }
  /**
   * Append a fully-formed item to ``history``. Used by
   * ``BootOrchestrator`` to push the doctor + pending-tasks cards
   * once their async fetches finish — these cards can't be in the
   * initial seed because we want the welcome card to paint before
   * the slow preflight call.
   */
  | { type: "HISTORY_APPENDED"; item: HistoryItem }
  /**
   * Backend handshake completed: server spawned, /health passed,
   * session created and state fetched. Sets ``session`` so Header
   * (which lives inside ``<Static>``) renders for the first time
   * with real values. Issued exactly once per process by
   * ``BootRunner`` — before this, ``session.id`` is ``""`` and
   * MainContent skips the header so the dynamic-area boot spinner
   * is the only thing the user sees.
   */
  | {
      type: "SESSION_INITIALIZED";
      session: { id: string; cluster: string; namespace: string; modelName: string };
    }
  /**
   * User picked a confirm-dialog option (or typed feedback). Posted
   * by ConfirmMessage's Select widget; consumed by Composer's
   * effect which actually runs the network calls on its
   * ``useStream`` instance. See ``AppState.pendingDecision`` for
   * the rationale.
   */
  | {
      type: "CONFIRM_USER_DECIDED";
      taskId: string;
      /** "approved"/"rejected" for confirm gates; raw key for plan_builder. */
      answer: string;
      feedback?: string;
    }
  | { type: "CONFIRM_DECISION_CONSUMED" }
  /**
   * Graceful turn-to-turn handoff. Commits the current ``pending``
   * to ``history`` exactly like TURN_DONE / TURN_ABORTED would, but
   * carries no error item and no goodbye-stat ratchet — it's the
   * "cleanly walking off the current turn before starting a new
   * one" signal. Dispatched by ``submitTurn({ supersedePrevious })``
   * before it aborts the in-flight stream, so resolved confirm
   * cards / completed tool groups land in scrollback rather than
   * getting wiped by the next TURN_STARTED's ``pending: []`` clear.
   */
  | { type: "TURN_TRANSITION" }
  /**
   * Phrase cycler heartbeat. Composer dispatches this every ~8s
   * while ``streamState === "responding"`` (and no memory compaction
   * is in flight) to rotate the LoadingIndicator's fallback header
   * label. The dispatcher pre-bakes the new phrase into the action
   * payload so the reducer stays pure (no ``Math.random`` inside the
   * reducer). Idempotent: if ``phrase === state.idlePhrase`` the
   * reducer returns the same state, avoiding a no-op re-render.
   */
  | { type: "PHRASE_TICK"; phrase: string };

const PREVIEW_MAX = 80;
const SUBJECT_MAX = 80;


/**
 * Graph nodes that are reached only when the LLM has decided to go
 * down the injection pipeline (chat / capability Q&A short-circuit
 * earlier at intent_clarification → END). Observing any ``NODE_STARTED``
 * for one of these flips ``currentTurnIsInjection`` — the same signal
 * the Python TUI derives from its ``conversation.last_turn_was_injection``
 * controller flag. ``intent_confirm`` is included because the user
 * actively confirming intent already commits this turn to "injection";
 * even an Esc-cancel after that point should count as a failed
 * injection attempt, mirroring Python ``app.py:347-360`` exactly.
 */
const _INJECT_NODES = new Set([
  "intent_confirm",
  "safety_check",
  "confirmation_gate",
  "baseline_capture",
  "execute_loop",
  "direct_execute",
  "verifier_loop",
]);

/**
 * Tool names whose execution unambiguously means we're inside the
 * inject pipeline (``kubectl`` is shared with chat/Q&A so it's NOT
 * here). Belt-and-braces alongside ``_INJECT_NODES`` — if the phase
 * event stream is dropped for any reason, the tool name still flips
 * the flag.
 */
const _INJECT_TOOL_NAMES = new Set([
  "blade_create",
  "blade_destroy",
  "blade_status",
]);

export function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    // ---------------------------------------------------------------
    case "TURN_STARTED": {
      const { id, counter } = nextId(state, "u");
      const userItem: HistoryItem = {
        kind: "user",
        id,
        text: action.input,
      };
      // Push the user echo straight to history — it's already final
      // and shouldn't bounce around in pending.
      //
      // Also stash the input as ``lastTurnInput`` so /retry can pull
      // it back after a stream_error — but ONLY for natural-language
      // turns. Composer routes slash commands through this same path
      // so they appear in scrollback as a user echo (the user typed
      // ``/retry`` and they want to see it), but ``/retry`` itself
      // isn't a re-submittable turn. Without this guard, typing
      // /retry would briefly set lastTurnInput="/retry" and a second
      // /retry race would resubmit the literal slash. Identify by
      // the leading slash on the trimmed text — same rule the
      // Composer uses to dispatch into the slash-command registry.
      const isSlashEcho = action.input.trimStart().startsWith("/");
      return {
        ...state,
        history: [...state.history, userItem],
        pending: [],
        streamState: "responding",
        thoughtSubject: "",
        thoughtBuffer: "",
        thoughtStartedAt: 0,
        hasActiveThinking: false,
        suppressMidContentThinking: false,
        turnInputTokens: 0,
        turnOutputTokens: 0,
        currentPhaseStepper: null,
        // Phase 4 — defensive reset. A compaction whose COMPLETED
        // event somehow never arrived (server crash mid-turn, network
        // drop) would otherwise leave the spinner stuck across into
        // the next turn. TURN_STARTED is the canonical
        // "everything begins fresh" boundary.
        currentCompaction: null,
        turnStartedAt: Date.now(),
        taskId: undefined,
        isReceiving: false,
        nextItemId: counter,
        lastTurnInput: isSlashEcho ? state.lastTurnInput : action.input,
        // Goodbye-card stats. Python ``app.py:306`` increments
        // ``message_count`` for every user submission BEFORE dispatching
        // to slash-vs-agent — count both here too. The per-turn flags
        // reset so the next turn starts clean.
        messageCount: state.messageCount + 1,
        currentTurnIsInjection: false,
        currentTurnFailed: false,
        // Reset the per-turn rejection flag so a follow-up turn
        // doesn't inherit a prior reject and erroneously paint
        // its stepper as failed at TURN_DONE.
        currentTurnRejected: false,
        seenToolCallIds: [],
      };
    }

    // ---------------------------------------------------------------
    case "TOKEN_APPENDED": {
      // Perf trace #3: measure the whole reducer case body. The
      // span captures commitThinking + flushLeadingStable + the
      // split decision + the resulting state rebuild. Reading the
      // log: ``dur`` per dispatch is the JS-side reducer cost; the
      // gap between consecutive ``ts`` values is the inter-dispatch
      // wall clock (i.e. how long Ink + React + stdout take between
      // batched TOKEN_APPENDED events).
      const __perfStart = performance.now();
      const __perfMark = (extra: Record<string, unknown>): void => {
        perfMark("reducer.TOKEN_APPENDED", {
          dur: performance.now() - __perfStart,
          ...extra,
        });
      };
      // Commit any buffered thinking into a ThinkingItem before
      // appending content tokens. With suppressMidContentThinking
      // active, this is a no-op (buffer cleared without creating an
      // item) — mid-content reasoning from Qwen's enable_thinking
      // mode is silently discarded. The first call (suppress=false)
      // creates the single ThinkingItem that shows "▸ 思考用时 Ns"
      // before the response text.
      state = commitThinking(state);
      state = flushLeadingStable(state);
      // Arm suppression: content tokens are flowing, so any
      // subsequent reasoning chunks within this response segment
      // are noise. Reset at boundary events (TOOL_STARTED / CONFIRM
      // / NODE change / TURN_STARTED).
      if (!state.suppressMidContentThinking) {
        state = { ...state, suppressMidContentThinking: true };
      }

      // Decide the trailing agent item's incoming text. If pending
      // already ends with an agent item we append into it; otherwise
      // we start a fresh agent item with ``action.content``. The
      // continuation flag is inherited from the existing tail so a
      // mid-stream split that produced a body-only tail keeps its
      // glyph-less rendering on subsequent token appends.
      const tail = state.pending[state.pending.length - 1];
      const existingIsAgent = tail && tail.kind === "agent";
      const newText = existingIsAgent
        ? (tail as AgentItem).text + action.content
        : action.content;
      const inheritedContinuation =
        existingIsAgent ? (tail as AgentItem).continuation === true : false;

      // Phase 2.3 — try to carve off a head fragment at a markdown-
      // safe boundary (paragraph break, never inside a fenced code
      // block) and commit it straight to history. Once committed it
      // lives in ``<Static>`` and never re-renders again, regardless
      // of how long the streaming reply ultimately grows. Without
      // this, the dynamic frame would have to re-paint the entire
      // accumulated reply on every TOKEN_APPENDED — even with the
      // AgentMessage 8-row visual cap, the React reconcile cost on
      // the full string scales with reply size.
      //
      // The split fires only when EVERY condition below holds:
      //   1. We are EXTENDING an existing agent tail (``existingIsAgent
      //      === true``). Without an existing agent in pending there is
      //      nothing to split out yet — the very first chunk of a fresh
      //      agent reply must land in pending whole.
      //   2. ``state.pending.length === 1`` — the agent tail is the
      //      ONLY item in pending. This is the chronology guard.
      //      flushLeadingStable only flushes items that are *stable*;
      //      a still-running tool_group OR an unresolved confirm
      //      prompt sitting BEFORE the agent tail stays in pending,
      //      and committing the head fragment to history NOW would
      //      reorder it ahead of those items (Static is append-only,
      //      so head would land in history while toolGroup is still
      //      in pending — when TOOL_ENDED eventually flushes the
      //      group, the timeline ends up as ``[head, toolGroup,
      //      tailItem]`` which is BACKWARDS from the chronological
      //      ``[toolGroup, head, tailItem]`` the user expects).
      //      Wait until the blocking item resolves and flushes,
      //      leaving the agent alone in pending; the split fires on
      //      the next token after that.
      //   3. ``splitPoint`` is strictly inside the text — splitPoint
      //      === content.length means "no safe boundary found yet".
      //   4. ``splitPoint > 0`` — splitPoint === 0 means the whole
      //      remaining content is inside a code block that started
      //      at offset 0; committing an empty head fragment is
      //      pointless.
      //   5. The remaining tail has at least one character — a split
      //      that consumes 100% of the text would commit the lot to
      //      history and leave an empty pending agent stub, which is
      //      both wasteful and would confuse the next TOKEN_APPENDED
      //      into thinking it should be a continuation.
      //
      // When a split fires, the head fragment reuses the existing
      // tail's id (preserves React keys → no remount of the head row
      // when it moves from pending to history) with the SAME
      // ``continuation`` value as the existing tail (so the very
      // first head keeps its ⏺ glyph and subsequent splits don't add
      // new ones), and a new pending agent item carries the body-only
      // tail with ``continuation: true`` so AgentMessage renders it
      // without the leading ⏺ glyph (paragraph spacing is preserved
      // by AgentMessage's marginTop=1 on every fragment).
      const pendingIsAgentOnly =
        existingIsAgent && state.pending.length === 1;
      const splitPoint = pendingIsAgentOnly
        ? findLastSafeSplitPoint(newText)
        : newText.length;
      const canSplit =
        pendingIsAgentOnly &&
        splitPoint > 0 &&
        splitPoint < newText.length &&
        newText.length - splitPoint > 0;

      let counter = state.nextItemId;

      if (canSplit) {
        const headText = newText.slice(0, splitPoint);
        const tailText = newText.slice(splitPoint);
        const headItem: AgentItem = {
          kind: "agent",
          id: (tail as AgentItem).id,
          text: headText,
          continuation: inheritedContinuation,
        };
        // Tail fragment: ALWAYS gets a fresh id (it's a new pending
        // item) and is marked as a continuation so AgentMessage drops
        // the leading ⏺ glyph. ``marginTop`` is preserved on the
        // continuation so the paragraph spacing matches the un-split
        // layout (each continuation starts a new paragraph by
        // construction — the split always lands right after ``\n\n``).
        const allocatedTail = nextId({ ...state, nextItemId: counter }, "a");
        counter = allocatedTail.counter;
        const tailItem: AgentItem = {
          kind: "agent",
          id: allocatedTail.id,
          text: tailText,
          continuation: true,
        };
        const nextPending = [...state.pending.slice(0, -1), tailItem];
        const result = {
          ...state,
          history: [...state.history, headItem],
          pending: nextPending,
          isReceiving: true,
          nextItemId: counter,
        };
        __perfMark({
          split: true,
          newTextLen: newText.length,
          headLen: headText.length,
          tailLen: tailText.length,
          historyLen: result.history.length,
        });
        return result;
      }

      // No split this round — append in place exactly like the
      // pre-2.3 path.
      const next = [...state.pending];
      if (existingIsAgent) {
        const updated: AgentItem = { ...(tail as AgentItem), text: newText };
        next[next.length - 1] = updated;
      } else {
        const allocated = nextId(state, "a");
        counter = allocated.counter;
        next.push({ kind: "agent", id: allocated.id, text: newText });
      }
      const result = {
        ...state,
        pending: next,
        isReceiving: true,
        nextItemId: counter,
      };
      __perfMark({
        split: false,
        newTextLen: newText.length,
        pendingLen: next.length,
      });
      return result;
    }

    // ---------------------------------------------------------------
    case "THINKING_APPENDED": {
      // Accumulate into the session buffer. The first chunk of a new
      // session also stamps ``thoughtStartedAt`` so commitThinking
      // can compute a duration when the session ends. Subsequent
      // chunks leave the timestamp alone (preserving the original
      // start). We deliberately do NOT touch ``thoughtSubject`` here:
      // LoadingIndicator picks up the live thinking buffer directly
      // for its body block, and ``thoughtSubject`` is the channel
      // for *non-thinking* phase labels (tool name, "resuming…",
      // replay banner). Re-deriving a "last sentence" on every chunk
      // was O(N) per dispatch with no consumer — pure waste.
      //
      // First chunk of a session is also a flush trigger: by the time
      // the next LLM call has started thinking, anything sitting in
      // pending from before (a resolved confirm card, a stale agent
      // text, a done tool group) is by definition done mutating.
      // Flushing here unblocks the execute_loop / verifier_loop
      // transition where the agent thinks for several seconds before
      // emitting any token / tool — without this hook those leftovers
      // sit in the dynamic frame for the whole thinking window,
      // tripping the fullscreen-redraw branch on every NODE_STARTED
      // re-render.
      const isFirstChunk =
        state.thoughtBuffer.length === 0 && state.thoughtStartedAt === 0;
      const flushed = isFirstChunk ? flushLeadingStable(state) : state;
      const buffer = flushed.thoughtBuffer + action.content;
      const startedAt =
        flushed.thoughtStartedAt > 0 ? flushed.thoughtStartedAt : Date.now();
      // Edge-trigger ``hasActiveThinking``: only flip on the 0→N
      // transition so high-frequency subscribers (LoadingIndicator)
      // don't re-render for every appended token. The ``thoughtBuffer``
      // itself still updates per chunk for the eventual collapse.
      const wasActive = flushed.thoughtBuffer.length > 0;
      const isActive = buffer.length > 0;
      return {
        ...flushed,
        thoughtBuffer: buffer,
        thoughtStartedAt: startedAt,
        hasActiveThinking:
          wasActive !== isActive ? isActive : flushed.hasActiveThinking,
        isReceiving: true,
      };
    }

    // ---------------------------------------------------------------
    case "USAGE_RECEIVED": {
      // Server emitted authoritative LLM token usage at the end of an
      // LLM call (LangChain ``on_chat_model_end``). Sum into the
      // per-turn counters so the LoadingIndicator's live tail and the
      // turn-end summary line both ground out on these figures.
      //
      // Defensive coercion via ``Number(x) || 0`` handles three
      // wire-level edge cases at once:
      //
      //   1. Field missing from frame entirely — older servers, or
      //      the falsy-strip in ``to_dict`` for non-usage event types.
      //      ``action.inputTokens`` arrives as ``undefined``,
      //      ``Number(undefined)`` is ``NaN``, ``NaN || 0`` is ``0``.
      //   2. Field arrives as a string (legacy serializers) — coerce
      //      via ``Number`` first.
      //   3. Field is a real ``0`` — preserved by ``|| 0`` (no flip).
      //
      // The previous version did a bare ``Math.max(0, action.inputTokens)``
      // which on ``undefined`` returned ``NaN``. That ``NaN`` propagated
      // through ``state.turnInputTokens + NaN`` and silently corrupted
      // the running total for the rest of the turn — the LoadingIndicator
      // tail and the end-of-turn summary both went blank. The server-side
      // fix in ``streaming.py:to_dict`` ensures ``usage`` events always
      // carry both fields, but this belt-and-braces stays so older
      // server builds + future serialisation drift can't recreate the bug.
      const inAdd = Math.max(0, Number(action.inputTokens) || 0);
      const outAdd = Math.max(0, Number(action.outputTokens) || 0);
      if (inAdd === 0 && outAdd === 0) return state;
      return {
        ...state,
        turnInputTokens: state.turnInputTokens + inAdd,
        turnOutputTokens: state.turnOutputTokens + outAdd,
      };
    }

    // ---------------------------------------------------------------
    case "CONTEXT_SIZE_RECEIVED": {
      // Snapshot replace, not accumulate — each event carries the
      // latest measurement and supersedes the previous one. Persists
      // across turns (NOT reset on TURN_STARTED) so the Footer
      // always reflects the most recent measurement, even between
      // turns when no LLM is actively running.
      //
      // Defensive zero short-circuit: if all four fields are 0 we
      // skip the update so a protocol glitch can't wipe the Footer's
      // existing numbers. A real hook call always produces at least
      // ``max_tokens > 0``, so all-zero indicates a wire issue.
      //
      // Also auto-clears ``contextError`` — a successful snapshot
      // is the signal that the pipeline recovered, so the Footer
      // can switch its tail back from "(error)" to the live "(N.N%)".
      const cur = Math.max(0, Number(action.currentTokens) || 0);
      const trig = Math.max(0, Number(action.triggerTokens) || 0);
      const max = Math.max(0, Number(action.maxTokens) || 0);
      const cnt = Math.max(0, Number(action.messagesCount) || 0);
      if (cur === 0 && trig === 0 && max === 0 && cnt === 0) return state;
      return {
        ...state,
        contextCurrentTokens: cur,
        contextTriggerTokens: trig,
        contextMaxTokens: max,
        contextMessagesCount: cnt,
        contextError: false,
      };
    }

    // ---------------------------------------------------------------
    case "COMPACT_MANUAL_STARTED": {
      // Open the slot the ManualCompactIndicator subscribes to.
      // Idempotent: if a previous /compact never closed (e.g. the
      // handler's finally was skipped by an unhandled exception),
      // we overwrite the slot with a fresh startedAt so the elapsed
      // tail doesn't show stale seconds.
      return {
        ...state,
        currentManualCompact: { startedAt: Date.now() },
      };
    }

    // ---------------------------------------------------------------
    case "COMPACT_MANUAL_DONE": {
      // Close the slot — indicator unmounts. Always safe to dispatch
      // unconditionally (no-op if slot was already null).
      if (state.currentManualCompact === null) return state;
      return {
        ...state,
        currentManualCompact: null,
      };
    }

    // ---------------------------------------------------------------
    case "TOOL_STARTED": {
      // Replay guard. LangGraph's astream_events v2 re-emits events
      // from already-completed nodes when ``Command(resume=...)`` is
      // invoked after an interrupt, which makes phase1_tools cards
      // visually duplicate after the second confirm gate. Each tool
      // call carries a stable LangChain ``run_id`` (a UUID) — when we
      // see the same id twice in one turn it's the replay frame; drop
      // it. Empty / synthetic call_ids (older servers without run_id)
      // bypass the guard so legacy paths keep working as before.
      if (
        action.callId &&
        action.callId.length > 0 &&
        state.seenToolCallIds.includes(action.callId)
      ) {
        return state;
      }
      // Tool boundary — un-suppress so the thinking that preceded
      // this tool call gets its own ThinkingItem (genuine multi-phase
      // thinking: think → tool → think → reply).
      state = { ...state, suppressMidContentThinking: false };
      // Tool start interrupts any thinking session — collapse the
      // buffer first so the ThinkingItem lands BEFORE the new tool
      // group in pending order. Idempotent when no session is
      // active.
      state = commitThinking(state);
      // Also flush any leading-stable items (e.g., a previously
      // completed ToolGroup) so this new tool starts a fresh group at
      // the head of pending instead of piling up behind dead history
      // items.
      state = flushLeadingStable(state);
      const toolAlloc = nextId(state, "t");
      const tool: ToolItem = {
        kind: "tool",
        id: toolAlloc.id,
        callId: action.callId,
        name: action.name,
        node: action.node,
        status: "running",
        resultPreview: "",
        raw: "",
        startedAt: Date.now(),
      };
      // Group consecutive tool calls inside one bordered ToolGroup. If
      // the trailing pending item is already a tool_group, append to
      // it; otherwise spin up a new group. This collapses a series of
      // ``kubectl get / kubectl describe / blade create`` calls into
      // one visual block instead of three independent stripes.
      const tail = state.pending[state.pending.length - 1];
      let pending: HistoryItem[];
      let counter = toolAlloc.counter;
      if (tail && tail.kind === "tool_group") {
        const updatedGroup: HistoryItem = {
          ...tail,
          tools: [...tail.tools, tool],
        };
        pending = [...state.pending.slice(0, -1), updatedGroup];
      } else {
        const groupAlloc = nextId({ ...state, nextItemId: counter }, "g");
        counter = groupAlloc.counter;
        pending = [
          ...state.pending,
          { kind: "tool_group", id: groupAlloc.id, tools: [tool] },
        ];
      }
      // Mark this turn as "real injection" if the tool is one of the
      // inject-side binaries (blade_create / blade_destroy / …). kubectl
      // is shared with chat/Q&A turns so it can't flip this — node-based
      // detection in NODE_STARTED handles the rest.
      const isInjectByTool =
        state.currentTurnIsInjection || _INJECT_TOOL_NAMES.has(action.name);
      // Record the run_id so the replay guard above can drop a future
      // duplicate for this same call. Synthetic / empty call_ids skip
      // recording (matches the guard's bypass) so legacy servers don't
      // accumulate noise.
      const seenToolCallIds =
        action.callId && action.callId.length > 0
          ? [...state.seenToolCallIds, action.callId]
          : state.seenToolCallIds;
      return {
        ...state,
        pending,
        // Tool start is also a strong "what's the agent doing" signal —
        // surface the tool name in the subject line.
        thoughtSubject: action.name,
        isReceiving: true,
        nextItemId: counter,
        currentTurnIsInjection: isInjectByTool,
        seenToolCallIds,
      };
    }

    // ---------------------------------------------------------------
    case "TOOL_ENDED": {
      // Two-pass match strategy:
      //   1. Strict pass — match the running tool whose ``callId``
      //      equals ``action.callId``. Prevents wrong matches when
      //      two concurrent invocations share a tool name.
      //   2. Name fallback — when (1) finds nothing AND the action
      //      carries a name, match the FIRST running tool with that
      //      name. Catches the case observed with
      //      ``submit_fault_intent``: LangGraph's astream_events emits
      //      ``on_tool_end`` with a different ``run_id`` than the
      //      preceding ``on_tool_start`` when the tool's owning node
      //      transitions immediately into an ``interrupt()`` (e.g.
      //      intent_clarification → intent_confirm). The strict pass
      //      misses; the tool stays "running" forever and the user
      //      sees a confirmation answered + a still-spinning tool
      //      card. Falling back to name match keeps the state machine
      //      correct without inventing fake completions.
      //
      // Invariant preserved: at most one running tool flips per
      // TOOL_ENDED action — the second pass only runs when the first
      // missed entirely.
      let matched = false;
      // Captures the FINALISED ToolItem (with locator already
      // attached) so the post-apply pass can register it in
      // ``state.locators.byId``. ``matched`` already guarantees only
      // one finalisation per action so we never double-allocate.
      let allocatedTool: ToolItem | null = null;
      const newLocator = `T${state.locators.nextToolN}`;
      const matchByCallId = (t: ToolItem): boolean => {
        if (matched || t.status !== "running") return false;
        return Boolean(action.callId) && t.callId === action.callId;
      };
      const matchByName = (t: ToolItem): boolean => {
        if (matched || t.status !== "running") return false;
        return Boolean(action.name) && t.name === action.name;
      };

      // Finalise + assign a locator if the tool didn't already have
      // one. Under normal flow ``locator`` is set exactly once per
      // tool, on the first TOOL_ENDED that finds the running tool.
      // Replay of the same tool_end (LangGraph re-emits on resume)
      // is blocked upstream by the seenToolCallIds guard in
      // TOOL_STARTED; if the guard ever leaks and the same tool
      // finishes twice, the ``!t.locator`` check stops a second
      // allocation.
      const finalizeWithLocator = (t: ToolItem): ToolItem => {
        const finished = finishTool(t, action.status, action.content);
        if (t.locator) return finished;
        const tagged: ToolItem = { ...finished, locator: newLocator };
        allocatedTool = tagged;
        return tagged;
      };

      const apply = (
        items: HistoryItem[],
        predicate: (t: ToolItem) => boolean,
      ): HistoryItem[] =>
        items.map((item) => {
          if (item.kind === "tool") {
            if (predicate(item)) {
              matched = true;
              return finalizeWithLocator(item);
            }
            return item;
          }
          if (item.kind === "tool_group") {
            let groupChanged = false;
            const updatedTools = item.tools.map((tool) => {
              if (predicate(tool)) {
                matched = true;
                groupChanged = true;
                return finalizeWithLocator(tool);
              }
              return tool;
            });
            return groupChanged ? { ...item, tools: updatedTools } : item;
          }
          return item;
        });

      // Pass 1: strict callId
      let next = apply(state.pending, matchByCallId);
      // Pass 2: name fallback (only when nothing matched on pass 1)
      if (!matched) {
        next = apply(next, matchByName);
      }
      // If we allocated a locator above, register it in the byId
      // table and bump the counter. Done after the apply so the
      // table only ever sees the FINAL ToolItem shape (no half-
      // initialised entries with status="running").
      let stateAfter: AppState = { ...state, pending: next };
      if (allocatedTool !== null) {
        const tool = allocatedTool as ToolItem;
        stateAfter = {
          ...stateAfter,
          locators: {
            ...stateAfter.locators,
            byId: {
              ...stateAfter.locators.byId,
              [newLocator]: tool,
            },
            nextToolN: stateAfter.locators.nextToolN + 1,
          },
        };
      }
      // Tool just completed — if this was the LAST running tool in the
      // leading group, the group is now stable and can be flushed
      // straight to history. Without this, multiple sequentially-
      // completed tools accumulate in pending while the agent thinks
      // about its next move (the LLM-thinking gap between TOOL_ENDED
      // and the next TOKEN/TOOL_STARTED can be 5–30s); during that
      // window the dynamic frame includes every just-completed
      // ToolGroup card, which trips Ink's fullscreen-redraw branch on
      // every Spinner tick. The flush is idempotent so it's a no-op
      // when leading items are still in flight.
      return flushLeadingStable(stateAfter);
    }

    // ---------------------------------------------------------------
    case "NODE_STARTED": {
      // Flip the per-turn injection flag when we enter a node that's
      // exclusive to the inject pipeline. See _INJECT_NODES above for
      // the rationale. We update isInjection unconditionally (boolean
      // ratchet — once true, stays true for the turn) so the order of
      // node events relative to the subject-update branch doesn't
      // matter.
      const nextIsInjection =
        state.currentTurnIsInjection || _INJECT_NODES.has(action.node);
      // M2: only update subject if we don't have a richer LLM-emitted
      // thought yet. Avoids stomping over a more specific phrase.
      const nextSubject = state.thoughtSubject || action.node;

      // PhaseStepperCard state machine.
      //
      // The server tags every node with a coarse phase
      // ("intent" / "safety" / "inject" / "verify" / "recovery").
      // ``mapNodeToStep`` translates that ``(node, phase)`` pair into
      // the finer-grained todo-list step — splitting the overloaded
      // ``inject`` phase into ``agent_loop`` and ``execute``, and
      // demoting the ``intent_confirm`` Layer-1 wait back into the
      // ``intent`` bucket. See the helper for the full mapping
      // rationale.
      //
      // The stepper lives in ``state.currentPhaseStepper`` — NOT in
      // pending — so its perpetual mutation during the turn doesn't
      // block the leading-stable flush in TOKEN_APPENDED. With the
      // stepper at pending[0] (its old home) every thinking /
      // tool_group sat behind it stayed pending all the way to
      // TURN_DONE, growing the dynamic area past ``stdout.rows`` and
      // tripping Ink's fullscreen-redraw branch on every frame —
      // the visible flicker + scroll-position thrash users see during
      // inject.
      //
      // Rules:
      //   - Skip events that don't map to a known step (older servers,
      //     unrelated nodes, recovery-graph events).
      //   - The ``intent`` step NEVER materialises the stepper —
      //     chat-only turns end at intent_clarification, a "1 of 5
      //     done" panel for a one-line greeting would be misleading.
      //   - First non-intent step creates the stepper.
      //   - Subsequent step transitions update it in place
      //     (monotonically forward — see ``applyActivePhase``).
      //   - The stepper is finalised + appended to pending inside
      //     ``commitPending`` so it lands in scrollback at the end of
      //     the turn block as a phase-progress snapshot.
      // Batch loop-back: when batch_setup fires, the pipeline is
      // starting a new fault iteration. Clear the stepper so the
      // subsequent safety_check NODE_STARTED creates a fresh one
      // starting from the "safety" phase — otherwise the monotonic
      // ratchet would keep it stuck at "verify" from the previous fault.
      let nextStepper = state.currentPhaseStepper;
      let stepperCounter = state.nextItemId;
      if (action.node === "batch_setup" && nextStepper !== null) {
        nextStepper = null;
      }

      const stepName = mapNodeToStep(action.node, action.phase ?? "");
      if (stepName !== null) {
        const activeIndex = INJECT_PHASE_ORDER.indexOf(stepName);
        if (nextStepper) {
          const repainted = applyActivePhase(nextStepper, activeIndex);
          if (repainted !== nextStepper) {
            nextStepper = repainted;
          }
        } else if (stepName !== "intent") {
          const alloc = nextId(state, "ps");
          nextStepper = buildPhaseStepper(alloc.id, activeIndex);
          stepperCounter = alloc.counter;
        }
      }

      return {
        ...state,
        thoughtSubject: nextSubject,
        currentTurnIsInjection: nextIsInjection,
        currentPhaseStepper: nextStepper,
        nextItemId: stepperCounter,
        suppressMidContentThinking: false,
      };
    }

    case "NODE_ENDED":
      return state;

    // ---------------------------------------------------------------
    case "CONFIRM_RECEIVED": {
      // Two-item split (see ``ConfirmContextItem`` /
      // ``ConfirmPromptItem`` in types.ts):
      //
      //   1. Drain currently-stable pending items into history first
      //      (``flushLeadingStable``) so the upcoming context card
      //      lands AFTER the in-flight Phase-1 leftovers in scrollback,
      //      preserving chronological order.
      //   2. FORCE-flush whatever ``flushLeadingStable`` left behind.
      //      The rule "agent stable iff !isTail" treats a trailing
      //      agent as "might still receive tokens", but the server
      //      contract guarantees no further tokens arrive for this
      //      LLM call once ``confirm`` is emitted — so the tail
      //      agent is in fact terminal. Two reasons to force it:
      //        (a) chronological order — agent spoke BEFORE confirm,
      //            so it must appear ABOVE context in scrollback.
      //            Without the force, context is pushed past a still-
      //            pending agent, then at TURN_DONE agent commits
      //            AFTER context — reversing the natural order users
      //            expect to read.
      //        (b) dyn-frame shrink — leaving agent in pending bloats
      //            the dyn frame during the waiting_confirmation
      //            window (every re-render writes pending+controls).
      //            Pushing agent out leaves dyn = [prompt] + controls,
      //            ~10 rows lighter, which reduces the chance of
      //            re-renders pushing past viewport bottom and
      //            triggering Ink cursor desync.
      //   3. Append a ``confirm_context`` item directly to history.
      //      Burns into Static scrollback ONCE — the heavy plan
      //      summary / safety warning never re-paints, so the dynamic
      //      frame doesn't grow with confirm content.
      //   4. Push a ``confirm_prompt`` item to the *now-empty*
      //      pending. This is the live select widget (~6–8 rows max).
      //
      // Future ``CONFIRM_USER_DECIDED`` / ``CONFIRM_RESOLVED``
      // dispatches mutate only the prompt — the context card is
      // immutable in scrollback.
      //
      // Step 0 — sanitize stuck-running tools BEFORE flushing.
      // ``submit_fault_intent`` (and any other tool that runs in a
      // node that immediately transitions into ``interrupt()``) can
      // leak as ``status="running"`` forever because LangGraph's
      // astream_events drops or re-keys its ``on_tool_end`` event
      // (see ``sanitizeStuckTools`` for the long-form rationale).
      // Promoting them to ``success`` here unblocks
      // ``flushLeadingStable`` so leading tool_groups actually drain
      // into history instead of piling up at pending[0] for the
      // entire turn — which was the verified root cause of the
      // dynamic-frame overflow + scrollback pollution behaviour.
      const sanitized = sanitizeStuckTools(state.pending);
      let s = sanitized === state.pending ? state : { ...state, pending: sanitized };
      // Defensive: close any open thinking session BEFORE the flush
      // so the ``▸ Thought for Ns`` row lands in chronological
      // position (above the agent / above the context). In practice
      // TOKEN_APPENDED already commits thinking when agent tokens
      // start streaming, so by the time confirm arrives the buffer
      // is usually empty — this guard catches the rare path where
      // the agent transitions thinking → confirm with no token
      // emission in between.
      s = { ...s, suppressMidContentThinking: false };
      s = commitThinking(s);
      s = flushLeadingStable(s);
      // Force-flush whatever ``flushLeadingStable`` couldn't move
      // (the tail-agent case described above). Exception: an
      // UNRESOLVED ``confirm_prompt`` from a still-open prior layer
      // (e.g. Layer-1 intent_confirm hasn't been resolved when
      // Layer-2 confirmation_gate fires — a rare protocol race) must
      // stay in pending because it's still the live UI the user
      // interacts with. Flushing it to Static would commit a
      // mid-decision widget into scrollback as "unresolved forever".
      // Everything ELSE in pending (agent / tool_group / thinking /
      // resolved confirm_prompt) is terminal by the time confirm
      // arrives, so it flushes cleanly.
      if (s.pending.length > 0) {
        const toFlush: HistoryItem[] = [];
        const toKeep: HistoryItem[] = [];
        for (const item of s.pending) {
          if (item.kind === "confirm_prompt" && !item.resolved) {
            toKeep.push(item);
          } else {
            toFlush.push(item);
          }
        }
        if (toFlush.length > 0) {
          s = {
            ...s,
            history: [...s.history, ...toFlush],
            pending: toKeep,
          };
        }
      }
      const { id: ctxId, counter: counterAfterCtx } = nextId(s, "c-ctx");
      const taskId = action.taskId ?? s.taskId ?? "";
      const context: ConfirmContextItem = {
        kind: "confirm_context",
        id: ctxId,
        taskId,
        content: action.content,
        node: action.node,
        payload: action.payload,
      };
      const promptId = `c-prompt-${counterAfterCtx}`;
      const prompt: ConfirmPromptItem = {
        kind: "confirm_prompt",
        id: promptId,
        taskId,
        node: action.node,
        selectedIndex: 0,
        mode: "select",
        feedback: "",
        resolved: false,
        payload: action.payload,
      };
      return {
        ...s,
        history: [...s.history, context],
        // ``s.pending`` is normally empty after the force-flush; the
        // only carry-over is an UNRESOLVED confirm_prompt from a
        // prior layer (preserved by the toKeep branch above). Append
        // the new prompt rather than overwriting so the prior
        // prompt — if any — stays live.
        pending: [...s.pending, prompt],
        streamState: "waiting_confirmation",
        taskId: action.taskId ?? s.taskId,
        nextItemId: counterAfterCtx + 1,
      };
    }

    // ---------------------------------------------------------------
    case "CONFIRM_RESOLVED": {
      // Mark the matching ConfirmItem resolved and flip the stream
      // state back to ``responding`` so the LoadingIndicator returns
      // and the InputPrompt stays hidden until the server emits the
      // remaining events + ``done``.
      //
      // ALSO flush the leading-stable items NOW. Without this, all
      // the Phase 1 leftovers (thinking rows, completed tool_groups,
      // stale agent items, plus the resolved confirm card itself)
      // sit in pending until the FIRST execute_loop event fires —
      // and execute_loop typically pauses for several seconds while
      // ``baseline_capture`` runs server-side and the LLM "warms up"
      // before its first token. During that window every NODE_STARTED
      // dispatch re-renders a tall dynamic frame (Phase 1 leftovers
      // can be 30+ rows on a real turn), tripping Ink's fullscreen-
      // redraw branch on every state change → the user-reported
      // "execute_loop starts → screen flickers" symptom. Flushing
      // here drains pending before the bridge phase begins.
      const resolved = state.pending.map((item) => {
        if (item.kind !== "confirm_prompt") return item;
        if (item.taskId !== action.taskId) return item;
        return { ...item, resolved: true, answer: action.answer };
      });
      const flushed = flushLeadingStable({ ...state, pending: resolved });
      return {
        ...flushed,
        streamState: "responding",
        thoughtSubject: action.answer === "approved" ? "resuming…" : "stopping…",
      };
    }

    // ---------------------------------------------------------------
    case "RESULT_RECEIVED": {
      // Force-commit all pending → history BEFORE the result item
      // lands, then push the result directly to history (NOT pending).
      //
      // Why: result is the turn's terminal outcome. By the time it
      // arrives, any agent message in pending has finished streaming
      // and any tool group has terminated. Leaving result + agent +
      // (turn_usage + stepper) to all commit at once on TURN_DONE
      // produces a single +11..15 row dyn-frame jump that the user
      // sees as a flicker burst right before the cursor returns to
      // idle. Splitting the commit into "result lands now, stepper +
      // usage land at TURN_DONE" makes the visual transition feel
      // smoother — each Static append is small and incremental.
      //
      // We DON'T call ``commitPending`` here because that helper
      // also resets ``streamState`` to "idle" + clears thought state
      // + finalises the phase stepper. The turn isn't over yet
      // (TURN_DONE may still come behind a final agent reply), so
      // those resets would prematurely tear down the live spinner
      // and stepper. Instead we do a minimal manual flush: commit
      // any in-flight thinking session, dump pending to history,
      // append result, leave streamState / stepper untouched.
      let s = commitThinking(state);
      const { id, counter } = nextId(s, "r");
      const parsed = parseResultEnvelope(
        action.content,
        action.taskId ?? s.taskId ?? "",
      );
      // Locator: every ResultItem gets a per-session ``E<N>`` token
      // so ``/show E1`` / ``/copy E1`` / ``/rerun E1`` can resolve
      // it. ``userInput`` is captured from ``state.lastTurnInput`` —
      // the NL prompt that started this turn — so ``/rerun`` can
      // surface the original description for paste-and-edit.
      const locator = `E${s.locators.nextExperimentN}`;
      const result: ResultItem = {
        kind: "result",
        id,
        ...parsed,
        locator,
        userInput: s.lastTurnInput || undefined,
      };
      // Goodbye stats: ``parseResultEnvelope`` maps ``data.task_state``
      // onto a normalized status. ``failed`` flips the per-turn flag;
      // the ``currentTurnIsInjection`` gate decides whether counters
      // actually move on TURN_DONE.
      const failedState = parsed.status === "failed";
      // Capture the task id of the experiment that just completed so
      // ``/recover latest`` and ``/review`` (no arg) can reach it
      // without re-querying listTasks. Only update when non-empty.
      const resolvedTaskId = action.taskId || parsed.taskId || "";
      return {
        ...s,
        // pending → history first, then result. Order matters: the
        // result must appear AFTER the agent message / tool groups
        // that produced it.
        history: [...s.history, ...s.pending, result],
        pending: [],
        taskId: action.taskId ?? s.taskId,
        lastTaskId: resolvedTaskId || s.lastTaskId,
        nextItemId: counter,
        currentTurnFailed: s.currentTurnFailed || failedState,
        locators: {
          ...s.locators,
          byId: { ...s.locators.byId, [locator]: result },
          nextExperimentN: s.locators.nextExperimentN + 1,
        },
      };
    }

    // ---------------------------------------------------------------
    case "ERROR_RECEIVED": {
      const { id, counter } = nextId(state, "e");
      const err: HistoryItem = {
        kind: "error",
        id,
        text: action.message,
        taskId: action.taskId,
      };
      // Goodbye stats: the runner emitted a stream-level error.
      // Equivalent to Python's ``conversation.last_turn_failed`` being
      // set by the runner — see ``app.py:356``. Flags this turn as
      // failed; whether it counts in the inject bucket depends on
      // ``currentTurnIsInjection`` at TURN_DONE.
      //
      // Also flips ``contextError`` so the Footer signals "the
      // displayed numbers may be stale" — the percent tail switches
      // to the literal "(error)" until a fresh ``CONTEXT_SIZE_RECEIVED``
      // clears it (auto-recovery on next successful turn).
      return {
        ...state,
        pending: [...state.pending, err],
        nextItemId: counter,
        currentTurnFailed: true,
        contextError: true,
      };
    }

    // ---------------------------------------------------------------
    // Phase 4 — memory compaction lifecycle.
    //
    // STARTED simply parks the live state slot. COMPLETED/FAILED
    // materialise a MemoryCompactionItem into ``pending`` so the
    // existing commitPending path moves it into ``history`` when the
    // turn ends, alongside thinking + tool_group + (optional)
    // turn_usage rows. Both finalisers clear ``currentCompaction``
    // so the LoadingIndicator regains the spinner slot.
    //
    // STARTED idempotency: if a previous compaction never closed
    // (e.g. a duplicate STARTED arrives), we overwrite the slot with
    // the new tokensBefore. Old in-flight info gets lost in that
    // case, but the alternative — refusing to update — would leave
    // the spinner showing stale numbers indefinitely.
    case "MEMORY_COMPACTION_STARTED": {
      return {
        ...state,
        currentCompaction: {
          startedAt: Date.now(),
          tokensBefore: Math.max(0, action.tokensBefore || 0),
          layer: action.layer || "llm_summary",
        },
      };
    }

    // ---------------------------------------------------------------
    case "MEMORY_COMPACTION_COMPLETED": {
      const { id, counter } = nextId(state, "mc");
      const item: HistoryItem = {
        kind: "memory_compaction",
        id,
        succeeded: true,
        tokensBefore: Math.max(0, action.tokensBefore || 0),
        tokensAfter: Math.max(0, action.tokensAfter || 0),
        messagesCompacted: Math.max(0, action.messagesCompacted || 0),
        durationMs: Math.max(0, action.durationMs || 0),
        layer: action.layer || "llm_summary",
      };
      return {
        ...state,
        pending: [...state.pending, item],
        nextItemId: counter,
        currentCompaction: null,
      };
    }

    // ---------------------------------------------------------------
    case "MEMORY_COMPACTION_FAILED": {
      const { id, counter } = nextId(state, "mc");
      const item: HistoryItem = {
        kind: "memory_compaction",
        id,
        succeeded: false,
        tokensBefore: Math.max(0, action.tokensBefore || 0),
        // Failed runs return no compacted output — explicit 0 so the
        // renderer doesn't accidentally display "saved 0 tokens" as
        // a success-flavour message.
        tokensAfter: 0,
        messagesCompacted: 0,
        durationMs: Math.max(0, action.durationMs || 0),
        layer: action.layer || "llm_summary",
        errorMessage: action.errorMessage || "",
      };
      return {
        ...state,
        pending: [...state.pending, item],
        nextItemId: counter,
        currentCompaction: null,
      };
    }

    // ---------------------------------------------------------------
    case "TURN_DONE":
      return commitPending(applyTurnStats(state));

    // ---------------------------------------------------------------
    case "TURN_ABORTED": {
      const { id, counter } = nextId(state, "e");
      const err: HistoryItem = {
        kind: "error",
        id,
        text: action.reason,
      };
      // Python ``app.py:336-346`` sets ``injection_failed = True`` on
      // KeyboardInterrupt / Exception inside the inject pipeline. The
      // closest TS equivalent is TURN_ABORTED (user Esc, network drop,
      // protocol error). Flip the failed flag, then run the same
      // counted-bucket logic on commitPending below.
      const withError: AppState = {
        ...state,
        pending: [...state.pending, err],
        nextItemId: counter,
        currentTurnFailed: true,
      };
      // failed=true so the phase stepper's active step is marked
      // ``failed`` (red ✗) instead of optimistically rounded up to
      // ``completed``. Later pending steps stay pending — they
      // never ran, lying about it would mislead the user about
      // where the pipeline actually broke.
      return commitPending(applyTurnStats(withError), { failed: true });
    }

    // ---------------------------------------------------------------
    case "RECOVERY_TRIGGERED":
      // Mirrors ``app.py:313`` — bump the recover counter the moment
      // the user types ``/recover``, regardless of whether the recover
      // graph subsequently succeeds. Same coarse semantic Python uses.
      return { ...state, recoveryCount: state.recoveryCount + 1 };

    // ---------------------------------------------------------------
    case "BOOT_PROGRESS_SHOW":
      return { ...state, bootProgress: action.text };

    case "BOOT_PROGRESS_HIDE":
      return { ...state, bootProgress: null };

    case "CONSTRAIN_HEIGHT_TOGGLED":
      return { ...state, constrainHeight: !state.constrainHeight };

    case "HISTORY_APPENDED":
      // BootOrchestrator drops the doctor / pending-tasks cards here
      // once their fetches return. Items use hand-rolled string IDs
      // (``boot-doctor``, ``boot-pending``) like the welcome card, so
      // no nextItemId bump is needed.
      return { ...state, history: [...state.history, action.item] };

    case "SESSION_INITIALIZED":
      // First and only time session details are written. Triggered by
      // ``BootRunner`` once the backend handshake completes; before
      // this the session is the ``initialAppState`` placeholder
      // (``id: ""``).
      return { ...state, session: action.session };

    case "CONFIRM_USER_DECIDED":
      // Stash the user's confirm-dialog choice for Composer's effect
      // to pick up. We don't run the network call here — the reducer
      // is pure. If a previous decision is still pending (effect
      // hasn't fired yet — shouldn't happen because Select unmounts
      // immediately after firing), the new one wins.
      //
      // Track the latest decision in ``currentTurnRejected`` so
      // ``commitPending`` can promote a clean TURN_DONE into a
      // failed-finalize when the user rejected at any confirm card
      // (server's reject path goes graph→END→done event, which on
      // the TS side looks identical to a successful turn — without
      // this flag the stepper would round up every step to
      // ``completed`` and falsely show "all green ✓"). Latest-
      // decision-wins: an ``approved`` reply on a subsequent
      // confirm flips the flag back to false so a Layer-1-approved
      // → Layer-2-approved sequence ends clean.
      return {
        ...state,
        pendingDecision: {
          taskId: action.taskId,
          answer: action.answer,
          feedback: action.feedback,
        },
        currentTurnRejected: action.answer === "rejected",
      };

    case "CONFIRM_DECISION_CONSUMED":
      // Composer's effect finished its network calls. Clear the slot
      // so the same decision doesn't get re-fired on the next
      // unrelated state change.
      return { ...state, pendingDecision: null };

    case "TURN_TRANSITION":
      // Graceful supersede — commit pending → history, leaving no
      // error item or stat ratchet behind. Dispatched by
      // ``submitTurn({ supersedePrevious: true })`` before it aborts
      // the in-flight SSE so the resolved confirm card (and anything
      // else in pending) lands in scrollback before TURN_STARTED's
      // ``pending: []`` clear wipes it.
      return commitPending(state);

    // ---------------------------------------------------------------
    case "MODE_TOGGLED":
      return { ...state, config: { ...state.config, permissionMode: action.mode } };

    // ---------------------------------------------------------------
    case "DISPLAY_MODE_CHANGED":
      return {
        ...state,
        config: { ...state.config, displayMode: action.mode },
      };

    // ---------------------------------------------------------------
    case "LOG_APPENDED": {
      // Slash-command output. Lands directly in ``history`` (skipping
      // ``pending``) because it isn't part of any agent turn — the
      // user typed `/help` and we want the response to appear in
      // scrollback immediately, not at TURN_DONE time.
      //
      // ID is allocated by the reducer (not the caller) so this stays
      // pure — same purity rule as every other item kind.
      const { id, counter } = nextId(state, "log");
      return {
        ...state,
        history: [
          ...state.history,
          { kind: "log", id, level: action.level, text: action.text },
        ],
        nextItemId: counter,
      };
    }

    // ---------------------------------------------------------------
    case "REPLAY_STARTED": {
      // Push the buffer's existing ``pending`` slice (shouldn't have
      // any since slash commands run from idle, but defensive) to
      // history before re-purposing pending for replay events.
      const baseState = state.pending.length > 0
        ? commitPending(state)
        : state;
      return {
        ...baseState,
        streamState: "responding",
        thoughtSubject: `replaying ${action.taskId}`,
        thoughtBuffer: "",
        thoughtStartedAt: 0,
        hasActiveThinking: false,
        turnInputTokens: 0,
        turnOutputTokens: 0,
        currentPhaseStepper: null,
        turnStartedAt: Date.now(),
        taskId: action.taskId,
        isReceiving: true,
      };
    }

    case "REPLAY_ENDED": {
      // Commit the replayed pending items to history and reset
      // stream state. ``aborted`` is informational — the matching
      // ``LogItem`` ("replay done · aborted") is dispatched
      // separately by the /replay handler, so we don't add another
      // visual artifact here.
      void action.aborted;
      return commitPending(state);
    }

    // ---------------------------------------------------------------
    case "HISTORY_CLEARED": {
      // /clear: drop all committed history AND bump the remount key
      // so MainContent's ``<Static>`` unmounts + re-mounts with the
      // empty items list. The /clear handler also writes an ANSI
      // clear-screen sequence to stdout *before* dispatching this
      // action so the previously burn-in'd lines get erased.
      //
      // Pending items (mid-turn streaming) survive — we don't want
      // /clear during a turn to disappear the running thinking row.
      //
      // Locators reset too: every previously-allocated ``T#`` /
      // ``E#`` was a pointer into history, and history is gone now.
      // Counters restart at 1 so the next allocated locator reads
      // ``T1`` / ``E1`` again — the user has no way to see "T7"
      // anywhere on screen so reusing the namespace causes no
      // confusion.
      return {
        ...state,
        history: [],
        historyRemountKey: state.historyRemountKey + 1,
        locators: { byId: {}, nextToolN: 1, nextExperimentN: 1 },
        // ``lastTaskId`` is keyed off history items the user could
        // still resolve via ``/recover latest`` / ``/review``; once
        // the locator table is gone the id refers to nothing visible
        // so wipe it too.
        lastTaskId: undefined,
      };
    }

    // ---------------------------------------------------------------
    case "PHRASE_TICK": {
      // Idempotent guard — same phrase as last tick is a no-op so
      // we don't burn a re-render rotating the LoadingIndicator
      // through identical bytes.
      if (action.phrase === state.idlePhrase) return state;
      return { ...state, idlePhrase: action.phrase };
    }

    default: {
      // Exhaustive check.
      const _never: never = action;
      void _never;
      return state;
    }
  }
}

// -- helpers ---------------------------------------------------------

function commitPending(
  state: AppState,
  options: { failed?: boolean } = {},
): AppState {
  // Perf trace #4: wrap the whole commit. The "stutter at stream
  // end" complaint points here — TURN_DONE → commitPending → one-
  // shot Static append of every leftover pending item. Reading the
  // log: ``dur`` is the JS work; the visible stutter is usually
  // Ink/stdout post-processing of the result state (which we can't
  // trace from inside the reducer). Payload reports input/output
  // sizes so we can correlate cost with pending depth.
  const __perfStart = performance.now();
  const __pendingBefore = state.pending.length;
  // Close any in-flight thinking session before deciding the early
  // bail. ``commitThinking`` is idempotent so this is free when no
  // session is active. We must run it even on the trivial-pending
  // path so a turn that ends mid-thought (TURN_ABORTED, TURN_DONE
  // racing the final thinking flush) still produces a
  // ``▸ Thought for Ns`` row in scrollback rather than silently
  // dropping the buffer.
  state = commitThinking(state);
  // Materialise the live phase-stepper, finalised, at the very END
  // of pending — strictly AFTER the optional ``⚡ turn-usage line``
  // so the strip lands directly above the InputPrompt in scrollback.
  // Users read the bottom of the turn block as "what just finished",
  // and they expect the todo strip there to mirror what they were
  // watching live; tucking the token tally between the strip and the
  // input was confusing ("did the strip belong to the previous turn?")
  // and pushed the strip away from the input. Appending the strip
  // last keeps it nearest to the cursor.
  //
  // We can't restore strict chronological order — the leading-stable
  // flush in TOKEN_APPENDED already moved tool/thinking items to
  // history *before* the stepper finalises here, and Static is
  // append-only so we cannot insert above committed history.
  // Appending makes the placement explicit: "here's where the turn
  // ended."
  //
  // Failed-finalize gate is the OR of three signals:
  //
  //   1. ``options.failed`` — caller-supplied. ``TURN_ABORTED``
  //      passes ``true`` for Esc / network drop / unhandled
  //      exception. ``TURN_DONE`` and ``TURN_TRANSITION`` default
  //      to ``false``.
  //
  //   2. ``state.currentTurnRejected`` — set by CONFIRM_USER_DECIDED
  //      when the user clicked "reject" on any confirm card this
  //      turn. The server's reject path routes graph → ``reject``
  //      node → END, which on the TS side looks identical to a
  //      successful turn (clean ``done`` event → TURN_DONE with
  //      ``failed=false``). Without this OR the stepper would
  //      round up every step to ``completed`` even though
  //      execute / verify never ran — the visible bug users
  //      reported as "I rejected but inject todos still all green".
  //
  //   3. ``state.currentTurnFailed`` — set by RESULT_RECEIVED when
  //      the server-emitted result envelope carries a failure status
  //      (replan exhausted, execute_loop max iterations, verifier
  //      reported "unverified", etc.). Same root pattern as #2:
  //      graph reaches END cleanly so ``options.failed`` is
  //      ``false``, but the operation itself failed and the
  //      stepper must reflect that.
  //
  // With all three OR'd, ``finalisePhaseStepper(failed=true)``
  // honestly marks the active in_progress step as ✗ and leaves
  // later pending steps untouched, producing the truthful
  // ``[✓ ✓ ✗ ○ ○]`` strip the user expects to see.
  const failed =
    (options.failed ?? false) ||
    (state.currentTurnRejected ?? false) ||
    (state.currentTurnFailed ?? false);

  // Early-bail when there's truly nothing to commit. Pulled BEFORE
  // both append blocks so the stepper-only / usage-only paths still
  // run their respective appends. The previous version had the bail
  // sandwiched between the two appends, which broke turns whose only
  // pending content was the stepper (the bail would short-circuit
  // before the usage append, harmless) — fine then, but moving the
  // stepper-append to AFTER the usage-append meant a stepper-only
  // turn would bail before the stepper got a chance to flush.
  // Hoisting the bail up keeps both orderings safe.
  const hasStepper = !!state.currentPhaseStepper;
  const hasUsage = state.turnInputTokens > 0 || state.turnOutputTokens > 0;
  if (
    state.pending.length === 0 &&
    state.streamState === "idle" &&
    !hasStepper &&
    !hasUsage
  ) {
    perfMark("commitPending", {
      dur: performance.now() - __perfStart,
      pendingBefore: __pendingBefore,
      path: "early-bail",
    });
    return state;
  }

  // Append the per-turn token-usage summary FIRST so it lands
  // ABOVE the finalised stepper in scrollback. Skipped when both
  // counts are 0 so older servers without ``usage`` events produce
  // turns identical to the prior shape.
  if (hasUsage) {
    const alloc = nextId(state, "tu");
    const usageItem: TurnUsageItem = {
      kind: "turn_usage",
      id: alloc.id,
      inputTokens: state.turnInputTokens,
      outputTokens: state.turnOutputTokens,
    };
    state = {
      ...state,
      pending: [...state.pending, usageItem],
      nextItemId: alloc.counter,
    };
  }

  // Append the finalised stepper LAST so the todo strip is the row
  // immediately above the InputPrompt. See the long comment above
  // for why this anchors the user's mental model better than the
  // previous stepper-then-usage order.
  if (state.currentPhaseStepper) {
    const finalisedStepper = finalisePhaseStepper(
      state.currentPhaseStepper,
      { failed },
    );
    state = {
      ...state,
      pending: [...state.pending, finalisedStepper],
      currentPhaseStepper: null,
    };
  }
  const result = {
    ...state,
    history: [...state.history, ...state.pending],
    pending: [],
    streamState: "idle" as const,
    thoughtSubject: "",
    thoughtBuffer: "",
    thoughtStartedAt: 0,
    hasActiveThinking: false,
    turnStartedAt: 0,
    isReceiving: false,
    // Phase 4 — defensive end-of-turn cleanup. Any in-flight compaction
    // that didn't close cleanly (the COMPLETED/FAILED event was
    // dropped) shouldn't bleed visible chrome into the next turn.
    // The compaction history row, if any, was already appended via
    // the dedicated reducer cases — clearing the slot here only
    // affects the live spinner, not scrollback.
    currentCompaction: null,
  };
  perfMark("commitPending", {
    dur: performance.now() - __perfStart,
    pendingBefore: __pendingBefore,
    pendingAtCommit: state.pending.length,
    historyAfter: result.history.length,
    hasStepper,
    hasUsage,
    failed,
  });
  return result;
}

/**
 * Goodbye-card stat bucketing — direct port of Python ``app.py:347-360``.
 *
 *     counted = injection_failed or conversation.last_turn_was_injection
 *     if counted:
 *         state.injection_count += 1
 *         if injection_failed or conversation.last_turn_failed:
 *             state.injection_fail += 1
 *         else:
 *             state.injection_success += 1
 *
 * In our terminology: ``currentTurnFailed`` covers both
 * ``injection_failed`` (Esc / exception) and ``last_turn_failed``
 * (runner reported error). ``currentTurnIsInjection`` covers
 * ``last_turn_was_injection``. The truth table is therefore identical.
 *
 * Called from TURN_DONE and TURN_ABORTED, immediately before
 * ``commitPending``. Returning the same object when nothing changes
 * preserves React's referential-equality optimizations downstream.
 */
function applyTurnStats(state: AppState): AppState {
  const counted = state.currentTurnIsInjection || state.currentTurnFailed;
  if (!counted) return state;
  return {
    ...state,
    injectionCount: state.injectionCount + 1,
    injectionSuccess: state.injectionSuccess + (state.currentTurnFailed ? 0 : 1),
    injectionFail: state.injectionFail + (state.currentTurnFailed ? 1 : 0),
  };
}

function finishTool(tool: ToolItem, status: ToolStatus, raw: string): ToolItem {
  const elapsed = Date.now() - tool.startedAt;
  return {
    ...tool,
    status,
    raw,
    resultPreview: previewLine(raw),
    elapsedMs: elapsed,
  };
}

function previewLine(text: string): string {
  if (!text) return "(no output)";
  // Try parsing a JSONEnvelope-shaped result for a friendlier preview.
  if (text.trimStart().startsWith("{") || text.trimStart().startsWith("[")) {
    try {
      const obj = JSON.parse(text) as Record<string, unknown>;
      if (obj && typeof obj === "object") {
        const status = typeof obj["status"] === "string" ? obj["status"] : "";
        const message = typeof obj["message"] === "string" ? obj["message"] : "";
        if (status && message) return truncate(`${status} · ${message}`, PREVIEW_MAX);
        if (status) return truncate(status, PREVIEW_MAX);
        if (message) return truncate(message, PREVIEW_MAX);
      }
    } catch {
      // Fall through to the plain-text path.
    }
  }
  const firstLine = text.split("\n").find((l) => l.trim()) ?? text;
  return truncate(firstLine.trim(), PREVIEW_MAX);
}

function truncate(s: string, max: number): string {
  return s.length <= max ? s : `${s.slice(0, max - 1)}…`;
}

/**
 * Extract a stable subject line from the accumulated thinking buffer.
 *
 * Strategy (mirrors Qwen Code's _extract_last_sentence):
 *   1. Strip leading whitespace.
 *   2. If the buffer contains at least one terminator (.!?。！？), take the
 *      last *complete* sentence — that represents the current line of
 *      reasoning. Earlier sentences are stale.
 *   3. Otherwise, just take the last 80 chars of the buffer (rolling
 *      view) so the user sees something stable instead of a flicker.
 *
 * The subject is for *display*, never logged or persisted; truncation
 * is fine.
 */
export function extractThoughtSubject(buffer: string): string {
  if (!buffer) return "";
  const trimmed = buffer.trim();
  if (!trimmed) return "";

  // Find sentence boundaries (CJK + ASCII).
  const sentenceRegex = /[.!?。！？]+/g;
  const matches = [...trimmed.matchAll(sentenceRegex)];
  if (matches.length > 0) {
    const last = matches[matches.length - 1];
    if (last && typeof last.index === "number") {
      const end = last.index + last[0].length;
      // Walk back to find the start of the last sentence.
      const before = trimmed.slice(0, last.index);
      const prevMatches = [...before.matchAll(sentenceRegex)];
      let start = 0;
      if (prevMatches.length > 0) {
        const prev = prevMatches[prevMatches.length - 1];
        if (prev && typeof prev.index === "number") {
          start = prev.index + prev[0].length;
        }
      }
      const sentence = trimmed.slice(start, end).trim();
      if (sentence) return truncate(sentence, SUBJECT_MAX);
    }
  }

  // No terminator yet — show a rolling tail of the buffer.
  if (trimmed.length <= SUBJECT_MAX) return trimmed;
  return `…${trimmed.slice(trimmed.length - (SUBJECT_MAX - 1))}`;
}
