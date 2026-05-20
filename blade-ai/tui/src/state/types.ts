/**
 * App-wide state types. All state mutations go through ``reducer.ts``.
 *
 * The shape mirrors Qwen Code's history / pending split:
 *   - ``history``: committed items, immutable. Renders inside Ink ``<Static>``
 *     so the terminal burn-in mechanism never re-paints them.
 *   - ``pending``: items belonging to the in-flight turn. Mutable
 *     (token streaming appends to the trailing agent item; tool status
 *     transitions in place). On turn end we move the whole batch into
 *     ``history`` and clear ``pending``.
 */

/** Stream lifecycle state machine — drives LoadingIndicator visibility. */
export type StreamState =
  | "idle"
  | "responding"
  | "waiting_confirmation";

export type ToolStatus = "running" | "success" | "error" | "canceled";

export interface UserItem {
  kind: "user";
  id: string;
  text: string;
}

export interface AgentItem {
  kind: "agent";
  id: string;
  text: string;
}

export interface ToolItem {
  kind: "tool";
  id: string;
  callId: string;
  name: string;
  node: string;
  status: ToolStatus;
  /** Truncated single-line preview for display. Full result lives in ``raw``. */
  resultPreview: string;
  raw: string;
  elapsedMs?: number;
  startedAt: number;
  /** Stable per-session locator (``T1``, ``T2`` …) assigned at the
   *  first ``TOOL_ENDED`` for this tool. Lets ``/show`` / ``/copy``
   *  / ``/expand`` reference past tool calls by short token without
   *  scrollback hunt. */
  locator?: string;
  /** i18n key rendered by ToolMessage's empty-body placeholder branch
   *  in place of the default ``tool.no_output``. Set by
   *  ``sanitizeStuckTools`` to ``tool.captured_in_confirm`` so the card
   *  reads "output is in the confirm below" instead of "(no output)".
   *  Stored as a key (not a translated string) so reducers stay pure
   *  and a future runtime locale switch resolves correctly. */
  placeholderKey?: string;
}

/** Group of consecutive tool calls — ToolGroupMessage renders one border around them. */
export interface ToolGroupItem {
  kind: "tool_group";
  id: string;
  tools: ToolItem[];
}

/** Result card for a completed inject/recover task. */
export interface ResultItem {
  kind: "result";
  id: string;
  taskId: string;
  status: "success" | "partial" | "failed" | "unknown";
  faultType: string;
  bladeUid: string;
  duration: string;
  /** Verification / side-effect summary line. */
  summary: string;
  /** Optional cause/hint pair when failure_reason is set. */
  cause?: string;
  hint?: string;
  /** Stable per-session locator (``E1``, ``E2`` …) assigned at
   *  ``RESULT_RECEIVED`` time. Lets ``/show`` / ``/copy`` / ``/rerun``
   *  reference past results by short token without scrollback hunt. */
  locator?: string;
  /** Natural-language input that started this turn — captured from
   *  ``state.lastTurnInput`` at result time. ``/rerun`` reads this so
   *  the user can paste-and-edit the original prompt without
   *  scrolling back. */
  userInput?: string;
}

/**
 * Confirm dialog — split into two items so the dynamic frame stays
 * bounded:
 *
 *   * ``ConfirmContextItem`` carries the heavy, read-only context
 *     (plan summary, safety warning, free-text content). Pushed
 *     directly to ``history`` (Static) at ``CONFIRM_RECEIVED`` so it
 *     burns into scrollback ONCE and never re-renders. Tall warnings
 *     no longer drive the dynamic frame past viewport rows.
 *
 *   * ``ConfirmPromptItem`` carries the live select widget — option
 *     list, current selection, optional inline-feedback text-edit
 *     mode. Stays in ``pending``; mutates as the user moves through
 *     options or types feedback, but tops out at ~6–8 rows so the
 *     dynamic frame stays small.
 *
 * Resolution flow:
 *   ``CONFIRM_USER_DECIDED`` updates ``selectedIndex`` / ``mode`` on
 *   the prompt only. ``CONFIRM_RESOLVED`` flips ``resolved`` true so
 *   ``flushLeadingStable`` migrates the prompt into history alongside
 *   a fresh ``"✓ confirmed" / "✗ cancelled"`` line.
 */
export interface ConfirmContextItem {
  kind: "confirm_context";
  id: string;
  taskId: string;
  /** Free-text body the agent sent — same content the legacy
   *  ConfirmItem held. */
  content: string;
  /** Graph node that paused (``intent_confirm`` or
   *  ``confirmation_gate``). Drives the renderer's title + safety
   *  badge selection. */
  node?: string;
  /** Structured payload from the server — see ConfirmEvent.payload
   *  in ``api/events.ts``. */
  payload?: Record<string, unknown>;
}

/** Modes the prompt cycles through:
 *   ``select``         — option list focused, arrow keys navigate.
 *   ``feedback_input`` — user picked the "tell agent something else"
 *                        option; show an inline text input until they
 *                        submit or escape back to ``select``. */
export type ConfirmPromptMode = "select" | "feedback_input";

export interface ConfirmPromptItem {
  kind: "confirm_prompt";
  id: string;
  taskId: string;
  /** Mirror of the context node so the prompt renderer can switch
   *  option labels (intent vs. plan confirm) without a context
   *  lookup. */
  node?: string;
  /** Highlighted option index (0-based). */
  selectedIndex: number;
  /** Active mode. */
  mode: ConfirmPromptMode;
  /** Free-form feedback text the user typed in ``feedback_input``
   *  mode. Sent on Enter as ``CONFIRM_FEEDBACK_SUBMITTED``. */
  feedback: string;
  /** True once ``CONFIRM_RESOLVED`` fired. ``flushLeadingStable``
   *  treats the prompt as stable when this is true and migrates it
   *  to history. */
  resolved: boolean;
  /** The chosen answer once resolved. ``undefined`` while pending. */
  answer?: "approved" | "rejected";
  /** Mirror of payload's ``options`` if the server supplied them
   *  custom; left undefined to use the renderer's default option
   *  list. */
  payload?: Record<string, unknown>;
}

/** Pre-formatted log message printed by slash commands. */
export interface LogItem {
  kind: "log";
  id: string;
  /** Severity for color picking. ``info`` is the default. */
  level: "info" | "warn" | "ok";
  /** Markdown-light text — rendered by SystemMessage. Multi-line allowed. */
  text: string;
}

export interface SystemItem {
  kind: "system";
  id: string;
  text: string;
}

export interface ErrorItem {
  kind: "error";
  id: string;
  text: string;
  taskId?: string;
}

/**
 * Collapsed thinking block — one per discrete "thinking session" in
 * a turn. Materialised when the agent transitions out of thinking
 * (token reply / tool call / turn end) so consecutive thinking →
 * tool → thinking sequences leave a clean ``▸ Thought for Ns ··· ▸
 * Thought for Ms`` trail in scrollback.
 *
 * Body text is intentionally NOT preserved: per the design review the
 * collapsed form shows only duration. If we later wire a Ctrl+R
 * expand affordance we'll add a ``fullText`` field then; today the
 * field's absence keeps history items small (long CoT can be tens of
 * KB per session) and avoids tempting future renderers into expensive
 * full-text re-flow on every paint.
 */
export interface ThinkingItem {
  kind: "thinking";
  id: string;
  /** Wall-clock duration of the thinking session (Date.now diff). */
  durationMs: number;
}

/**
 * Turn token usage summary, appended at TURN_DONE / TURN_ABORTED /
 * TURN_TRANSITION when the server reported any LLM token usage during
 * the turn. Renders as ``⚡ turn used N tokens (in I, out O)`` —
 * authoritative figures sourced from the backend's ``usage`` SSE
 * events (LangChain ``on_chat_model_end``), aggregated across every
 * LLM call in the turn (intent reasoning + tool-loop + final reply).
 *
 * Skipped entirely when both counts are 0 — older servers that never
 * emit ``usage`` simply produce turns without this summary line, which
 * is the expected back-compat behaviour.
 */
export interface TurnUsageItem {
  kind: "turn_usage";
  id: string;
  /** Sum of input_tokens across every ``usage`` event of this turn. */
  inputTokens: number;
  /** Sum of output_tokens across every ``usage`` event of this turn. */
  outputTokens: number;
}

/**
 * Phase 4 — finalised memory-compaction history row.
 *
 * Materialised at MEMORY_COMPACTION_COMPLETED / FAILED time and
 * appended to ``pending``; the existing ``commitPending`` path moves
 * it into ``history`` at TURN_DONE so it lives in scrollback as
 * "✓ 已压缩 12000 → 4500 tokens · 用时 6.3s" or
 * "✗ 记忆压缩失败：<原因>".
 *
 * Why pending → commitPending (instead of dispatching straight into
 * history): keeps the rendering policy uniform with ``ThinkingItem``
 * and ``TurnUsageItem`` so the user sees the entry burn into
 * ``<Static>`` exactly when the rest of the turn does, in a
 * consistent visual position.
 */
export interface MemoryCompactionItem {
  kind: "memory_compaction";
  id: string;
  /** ``true`` when the compactor returned cleanly, ``false`` on
   *  failure. Drives the ✓/✗ glyph and the colour. */
  succeeded: boolean;
  /** Approximate input token count before the compactor ran. */
  tokensBefore: number;
  /** Approximate token count of the compacted output. ``0`` on the
   *  failure path (the compactor never returned a list). */
  tokensAfter: number;
  /** Number of input messages folded into the summary. */
  messagesCompacted: number;
  /** Wall-clock duration of the compactor call (ms). */
  durationMs: number;
  /** Server-supplied layer label ("llm_summary" / "lightweight").
   *  Currently always "llm_summary" because the hook's lightweight
   *  path stays silent; the field is here so a future "show
   *  lightweight too" change doesn't need a schema migration. */
  layer: string;
  /** Failure message (only meaningful when ``succeeded === false``). */
  errorMessage?: string;
}

/**
 * Boot-time cards seeded into history once at startup. They live in
 * history (not pending) so ``<Static>`` burns them into scrollback —
 * each renders once and stays put. Three variants instead of one
 * polymorphic ``boot_card`` so the type system pins each card's data
 * shape independently and MainContent's switch stays exhaustive.
 */
export interface WelcomeCardItem {
  kind: "welcome_card";
  id: string;
  modelName: string;
  permissionMode: "auto" | "confirm";
  kubeconfig: string;
  namespace: string;
  version: string;
}

export interface BootDoctorCheck {
  name: string;
  severity: "blocking" | "warning";
  passed: boolean;
  message: string;
  fix: string;
}

export interface BootDoctorCardItem {
  kind: "boot_doctor_card";
  id: string;
  checks: BootDoctorCheck[];
  passedCount: number;
  totalCount: number;
  /** True when the preflight endpoint failed entirely (old server / fetch error). */
  unavailable?: boolean;
  /**
   * ISO-8601 timestamp captured at seed time. Rendered as "HH:MM:SS"
   * in the card header so the user knows the check is a boot snapshot
   * (not live) and can re-run ``/doctor`` for a fresh probe.
   */
  capturedAt: string;
}

export interface PendingTaskRow {
  taskId: string;
  faultType: string;
  state: string;
  createdAt: string;
}

export interface PendingTasksCardItem {
  kind: "pending_tasks_card";
  id: string;
  tasks: PendingTaskRow[];
}

/**
 * Runtime ``/doctor`` snapshot — distinct from {@link BootDoctorCardItem}
 * which carries preflight checks. This card is a key/value diagnostic
 * table the user produces on demand to grab the current backend handle
 * (URL, cluster, versions, protocol, mode). Rendered inside its own
 * bordered frame so multiple ``/doctor`` invocations stack cleanly in
 * scrollback rather than blending into adjacent log output.
 */
export interface RuntimeDoctorCardItem {
  kind: "runtime_doctor_card";
  id: string;
  /** ``true`` when ``client.health()`` returned cleanly. */
  reachable: boolean;
  serverUrl: string;
  /** Empty string when no cluster info — renderer shows a neutral hint. */
  cluster: string;
  tuiVersion: string;
  /** ``null`` when the server-version probe failed. */
  serverVersion: string | null;
  /** TUI protocol version known at build time. */
  tuiProtocol: string;
  /** Server-advertised protocol version, ``null`` when not yet handshaken. */
  serverProtocol: string | null;
  /** Active UI language code (``en``, ``zh`` …). */
  lang: string;
  /** Current permission mode. */
  mode: string;
  /** ISO-8601 timestamp of when the card was generated. */
  capturedAt: string;
  /**
   * Live preflight check results — same seven probes the boot screen
   * runs (LLM key / kubeconfig / kubectl / blade / skills /
   * k8s_connectivity / chaosblade_operator). Re-run on every
   * ``/doctor`` invocation so a session-mid change (key revoked, k8s
   * link drop, operator uninstall) shows up rather than reflecting
   * the stale boot snapshot.
   */
  checks: BootDoctorCheck[];
  /** Convenience counters mirroring the BootDoctorCard envelope so
   *  the renderer doesn't have to recompute per render. */
  passedCount: number;
  totalCount: number;
  /** ``true`` when the preflight endpoint failed or was unavailable.
   *  Distinct from "0 / 0 passed" — that would be a confidently-wrong
   *  report; this flag routes the renderer to a "(unavailable)" hint. */
  preflightUnavailable: boolean;
}

/**
 * Inject-flow stepper rendered above the input prompt during an inject
 * turn. Each step represents one bucket of the real graph node
 * sequence — the server emits coarse phase strings via
 * ``dispatch_phase_started`` and the reducer's ``mapNodeToStep``
 * helper translates ``(node, phase)`` pairs into the finer-grained
 * 5-row layout. ``intent`` is the entry step — present in the strip
 * but already-completed by the time the stepper materialises (chat-
 * only turns that never leave intent never show a stepper at all).
 *
 * Lifecycle:
 *   - Created the first time a NODE_STARTED event maps to a
 *     non-intent step (so chat-only turns and Layer-1 confirm waits
 *     never show a stepper — see ``mapNodeToStep`` for the mapping
 *     rules, in particular the ``intent_confirm`` demotion).
 *   - Updated on every NODE_STARTED that introduces a new step:
 *     monotonic forward only — already-completed steps never roll
 *     back, replayed earlier-step events become no-ops.
 *   - On TURN_DONE the active step flips to ``completed`` and the
 *     stepper sinks into history alongside other turn artefacts.
 *   - On TURN_ABORTED the active step flips to ``failed``; later
 *     pending steps stay ``pending`` (they never ran), so the
 *     scrollback strip honestly records "got this far, then broke".
 */
export type PhaseStatus = "pending" | "in_progress" | "completed" | "failed";

/** Single source of truth for inject-stepper phase order.
 *
 *  Defined as a ``readonly`` tuple so:
 *    - ``PhaseName`` below derives from it via ``[number]`` indexing —
 *      adding a phase here automatically widens the union, no parallel
 *      declaration to forget.
 *    - ``reducer.ts`` re-uses the array verbatim for ``indexOf`` /
 *      ``includes`` checks (no second copy with structural drift).
 *
 *  Phase order mirrors the actual graph execution sequence in
 *  ``src/chaos_agent/agent/graph.py``:
 *
 *      intent_clarification (intent)
 *        → agent_loop ⇄ phase1_tools (inject — gather context)
 *        → safety_check / confirmation_gate (safety)
 *        → baseline_capture → execute_loop ⇄ phase2_tools (inject — run)
 *        → verifier_loop ⇄ verifier_tools (verify)
 *
 *  The graph's ``inject`` phase fires twice — once at agent_loop
 *  (BEFORE safety) and once at execute_loop (AFTER safety). Using a
 *  single ``inject`` bucket would let the monotonic ratchet skip past
 *  ``safety`` the moment agent_loop fires, painting safety as
 *  completed before it actually runs. We split into ``agent_loop``
 *  (planning, pre-safety) and ``execute`` (post-safety, blade calls)
 *  so the strip honestly tracks the real graph progression.
 *
 *  ``recovery`` is intentionally absent: a fault injection never
 *  auto-recovers — the user must invoke ``/recover`` (a separate graph
 *  whose task_id may or may not match the original injection's).
 *  PendingTasksCard at boot covers unfinished tasks; ``blade
 *  --timeout`` provides time-bounded auto-cleanup. A future recover-
 *  flow stepper will live as ``mode: "recover"`` with its own phase
 *  list. */
export const INJECT_PHASE_ORDER = [
  "intent",
  "agent_loop",
  "safety",
  "execute",
  "verify",
] as const;

export type PhaseName = (typeof INJECT_PHASE_ORDER)[number];

export interface PhaseStep {
  phase: PhaseName;
  status: PhaseStatus;
}

export interface PhaseStepperItem {
  kind: "phase_stepper";
  id: string;
  /** ``"inject"`` is the only mode rendered today. The field exists so
   *  a future recover-flow stepper can share this discriminated union
   *  without breaking the existing reducer state machine. */
  mode: "inject";
  /** Always 4 entries (matching the inject-mode order). The reducer
   *  never re-orders or splices the array — it only mutates ``status``
   *  in place. */
  steps: PhaseStep[];
}

export type HistoryItem =
  | UserItem
  | AgentItem
  | ToolItem
  | ToolGroupItem
  | ResultItem
  | ConfirmContextItem
  | ConfirmPromptItem
  | LogItem
  | SystemItem
  | ErrorItem
  | ThinkingItem
  | TurnUsageItem
  | MemoryCompactionItem
  | WelcomeCardItem
  | BootDoctorCardItem
  | PendingTasksCardItem
  | RuntimeDoctorCardItem
  | PhaseStepperItem;

export interface SessionInfo {
  id: string;
  cluster?: string;
  namespace?: string;
  modelName?: string;
}

export interface AppState {
  history: HistoryItem[];
  pending: HistoryItem[];
  session: SessionInfo;
  streamState: StreamState;
  /** Current thinking subject — derived from accumulated thinking buffer. */
  thoughtSubject: string;
  /**
   * Buffered thinking content for the *current thinking session*. A
   * turn can contain multiple sessions (thinking → tool → thinking →
   * reply); each session's buffer is consumed and cleared by the
   * next non-thinking event (TOKEN_APPENDED / TOOL_STARTED) which
   * commits a ``ThinkingItem`` to pending.
   */
  thoughtBuffer: string;
  /**
   * Wall-clock (Date.now) when the current thinking session began.
   * Stamped on the first ``THINKING_APPENDED`` of a session, cleared
   * alongside ``thoughtBuffer`` when the session commits. ``0`` when
   * no thinking session is active.
   */
  thoughtStartedAt: number;
  /**
   * Authoritative LLM token consumption for the current turn, sourced
   * from server ``usage`` events. Resets to 0 on TURN_STARTED and
   * REPLAY_STARTED, accumulates on each USAGE_RECEIVED, surfaces in
   * the LoadingIndicator's live tail (sum) and in the TurnUsageItem
   * appended at commitPending (split). Replaces the prior
   * ``streamingChars / 4`` client-side approximation.
   */
  turnInputTokens: number;
  turnOutputTokens: number;
  /** Wall-clock elapsed since the current turn started (ms). UI computes derived seconds. */
  turnStartedAt: number;
  /** task_id of the in-flight turn, if any. */
  taskId?: string;
  /** Whether the agent is actively receiving content (token/thinking) vs waiting on API. */
  isReceiving: boolean;
  /**
   * Monotonic counter feeding ``nextItemId``. Lives on the state so
   * the reducer remains a pure function — no module-level mutable
   * counter — and ID assignment stays deterministic across React
   * StrictMode double-invokes.
   */
  nextItemId: number;
  /**
   * Bumped by ``HISTORY_CLEARED``. Used as the React key on
   * ``<Static>`` so /clear forces a remount that wipes the
   * previously burn-in'd items from the visible terminal frame
   * (the scrollback above it is cleared with an ANSI sequence by
   * the /clear handler before the bump).
   */
  historyRemountKey: number;
  /**
   * Whether pending items should respect the ``availableTerminalHeight``
   * budget by truncating overlong content (default ``true``). Toggled
   * by ``CONSTRAIN_HEIGHT_TOGGLED`` (Ctrl+S) so the user can briefly
   * see the full untruncated content of a pending item that overflowed
   * its slot, then re-engage the cap to keep the dynamic frame ≤
   * viewport rows. Mirrors qwen-code's ``constrainHeight`` UI state.
   */
  constrainHeight: boolean;
  /**
   * Last natural-language turn input. Captured on TURN_STARTED, never
   * cleared — /retry resubmits this verbatim after a stream_error.
   * Undefined when the user has not yet sent any non-slash input this
   * session. Slash commands (/help, /clear, …) do NOT update this
   * field; they're routed directly to history without going through
   * the reducer's TURN_STARTED path.
   */
  lastTurnInput?: string;
  /**
   * Most-recently-completed task id. Captured on RESULT_RECEIVED so
   * ``/recover latest`` and ``/review`` (no arg) can reach the last
   * thing the user injected without re-fetching ``listTasks`` first.
   * Undefined until a turn produces a Result with a non-empty
   * ``taskId``; reset on HISTORY_CLEARED.
   *
   * Mirrors Python TUI's ``conversation.last_task_id`` —
   * (``tui/controllers/conversation.py``). Same field, same
   * "latest in this session" semantics.
   */
  lastTaskId?: string;
  /**
   * Wall-clock when the session was constructed (Date.now()). Used by
   * the goodbye card to show "持续时间 Xh Ym Zs". Never updated after
   * init.
   */
  sessionStartTs: number;
  /** Total user submissions in this session. Incremented on TURN_STARTED. */
  messageCount: number;
  /** Turns that actually entered the inject pipeline (success + fail). */
  injectionCount: number;
  injectionSuccess: number;
  injectionFail: number;
  /** Number of /recover invocations in this session. */
  recoveryCount: number;
  /**
   * Per-turn transient flags — equivalent to the Python TUI's
   * ``conversation.last_turn_was_injection`` / ``last_turn_failed``
   * locals inside ``app.py``'s ``finally`` block. Both reset on
   * TURN_STARTED, set during the turn by observed events, and read on
   * TURN_DONE / TURN_ABORTED to decide whether to bump the injection
   * counters. Kept on AppState (rather than module-scope) so the
   * reducer remains pure and survives React StrictMode double-invokes.
   */
  currentTurnIsInjection: boolean;
  currentTurnFailed: boolean;
  /**
   * True iff the user clicked "reject" on the latest confirm dialog
   * of the current turn (Layer-1 ``intent_confirm`` or Layer-2
   * ``confirmation_gate``). Latest-decision-wins: an "approved"
   * reply on a later confirm flips it back to false.
   *
   * Why this exists: rejection at confirmation_gate routes the
   * server graph through the ``reject`` node to END, which the TS
   * side observes as a *clean* ``done`` event → ``TURN_DONE``.
   * Without this flag ``commitPending`` would default to
   * ``failed=false`` and ``finalisePhaseStepper`` would round every
   * step up to ``completed`` — producing a misleading "all green
   * ✓" strip even though execute / verify never ran. Tracking the
   * decision lets ``commitPending`` flip ``failed=true`` when the
   * turn actually ended in user rejection, so the strip honestly
   * shows ``[✓ ✓ ✗ ○ ○]`` on a Layer-2 reject.
   *
   * Reset on TURN_STARTED so a follow-up turn does not inherit a
   * prior rejection.
   */
  currentTurnRejected: boolean;
  /**
   * Tool ``call_id`` (LangChain ``run_id``) values already rendered
   * during the current turn. Used to drop **replay** events emitted by
   * LangGraph: when ``astream_events(Command(resume=...))`` resumes
   * after a multi-layer interrupt (intent_confirm → confirmation_gate),
   * LangGraph re-emits the events from already-completed nodes since
   * the last checkpoint as part of v2 stream replay. Those events
   * carry the same ``run_id`` as the original emission — the downstream
   * UI renders them as duplicate ToolGroup cards if not deduplicated.
   *
   * Reset on TURN_STARTED so a genuine second turn (user submits a
   * follow-up) does NOT inherit the previous turn's call_id set;
   * the LangChain run_id space is global UUIDs so collisions across
   * turns are impossible, but resetting keeps the set bounded.
   */
  seenToolCallIds: string[];
  /**
   * Per-session locator table. Mirrors Python TUI's
   * ``state.locators`` (``tui/state.py``). Each finalised tool call
   * gets ``T<N>`` and each ``ResultItem`` gets ``E<N>``; the values
   * land in ``byId`` so ``/show`` / ``/copy`` / ``/rerun`` /
   * ``/expand`` can resolve a typed locator in O(1).
   *
   * Reset on ``HISTORY_CLEARED`` (the user wiped scrollback so the
   * old E#/T# are no longer pointing at anything visible). Survives
   * ``TURN_STARTED`` — locator IDs are session-scoped, not turn-scoped.
   */
  locators: {
    /** ``"T1"`` / ``"E1"`` etc → snapshot of the underlying item.
     *  Snapshots are stored by reference; subsequent mutations of
     *  the original ToolItem (none today, but defensive) would be
     *  visible here. The two command shapes (tool / experiment) are
     *  disambiguated by the ``kind`` field on the stored value. */
    byId: Record<string, ToolItem | ResultItem>;
    /** Next ``T<N>`` index to allocate. Starts at 1, monotonic. */
    nextToolN: number;
    /** Next ``E<N>`` index to allocate. Starts at 1, monotonic. */
    nextExperimentN: number;
  };
  /**
   * Boot-time progress indicator. ``null`` when no boot work is in
   * flight; a non-empty string is the current step label rendered
   * with a spinner above the input prompt. Driven by
   * ``BootOrchestrator``: shown while ``/preflight`` and
   * ``/api/v1/metric`` are being fetched, hidden once both have
   * landed as Static history items.
   */
  bootProgress: string | null;
  /**
   * Pubsub slot for ConfirmMessage → Composer. ConfirmMessage owns
   * the Select widget and runs entirely inside the render tree, but
   * the network calls (``resolveInterrupt`` + optional follow-up
   * ``submitTurn`` for user feedback) live on Composer's
   * ``useStream`` instance — only one component should hold the
   * abort controllers + the SSE iterator. Rather than plumb
   * ``resolveConfirm`` through React Context for this single
   * hand-off, the user's selection lands here as state and
   * Composer's effect picks it up. Cleared via
   * ``CONFIRM_DECISION_CONSUMED`` after the call completes (or
   * fails) so the effect doesn't re-fire.
   *
   * ``null`` = nothing pending. ``answer`` is the LangGraph resume
   * value; ``feedback``, when non-empty, is sent as a fresh user
   * turn AFTER the resolveInterrupt call so the agent treats it
   * as the user's next message.
   */
  pendingDecision:
    | {
        taskId: string;
        answer: "approved" | "rejected";
        feedback?: string;
      }
    | null;
  /**
   * Live phase-stepper for the current turn. Lives outside ``pending``
   * specifically to keep the leading-stable flush in TOKEN_APPENDED
   * working: while a turn is in flight the stepper keeps mutating
   * (phases transition forward), and putting it at the head of
   * ``pending`` would block every subsequent ``thinking`` /
   * ``tool_group`` from being flushed to history. With pending blocked,
   * the dynamic-area output height grows past ``stdout.rows`` and Ink
   * falls into its fullscreen-redraw branch on every frame —
   * the visible flicker + scroll-position thrash users see during
   * inject.
   *
   * Lifecycle: created on the first non-intent step of the turn
   * (mapped from ``(node, phase)`` via ``mapNodeToStep``), updated
   * in place on subsequent step transitions, finalised + appended
   * to ``pending`` inside ``commitPending`` (so it lands in
   * scrollback as a phase-progress snapshot near the END of the
   * turn block — before the optional ``turn_usage`` summary, after
   * tool/agent items that already flushed mid-turn), then cleared
   * back to ``null``. Strict chronological "stepper right after
   * user echo" placement is unattainable with Ink's append-only
   * Static — the leading-stable flush in TOKEN_APPENDED has already
   * sent downstream items to history before commitPending runs.
   */
  currentPhaseStepper: PhaseStepperItem | null;
  /**
   * Phase 4 — live memory-compaction state.
   *
   * Set when the server emits ``memory_compaction`` event with phase
   * "started"; cleared when "completed" / "failed" arrives (or at
   * TURN_DONE / HISTORY_CLEARED for safety). Drives the
   * ``MemoryCompactingIndicator`` spinner above the input prompt and
   * suppresses the regular ``LoadingIndicator`` while non-null
   * (single-spinner UX — see useLoadingIndicator's mutex).
   *
   * ``layer`` is the server-supplied label, kept here so the spinner
   * can show "lightweight" vs "llm summary" if the protocol ever
   * surfaces both paths (today only the LLM-summary path emits).
   */
  currentCompaction:
    | {
        startedAt: number;       // Date.now() when STARTED arrived
        tokensBefore: number;    // input token count (server estimate)
        layer: string;           // "llm_summary" today
      }
    | null;
  /** Configuration knobs. */
  config: {
    permissionMode: "confirm" | "auto";
    displayMode: "calm" | "working" | "dense";
  };
}

export const initialAppState: AppState = {
  history: [],
  pending: [],
  session: { id: "" },
  streamState: "idle",
  thoughtSubject: "",
  thoughtBuffer: "",
  thoughtStartedAt: 0,
  turnInputTokens: 0,
  turnOutputTokens: 0,
  turnStartedAt: 0,
  isReceiving: false,
  nextItemId: 0,
  historyRemountKey: 0,
  constrainHeight: true,
  sessionStartTs: Date.now(),
  messageCount: 0,
  injectionCount: 0,
  injectionSuccess: 0,
  injectionFail: 0,
  recoveryCount: 0,
  currentTurnIsInjection: false,
  currentTurnFailed: false,
  currentTurnRejected: false,
  seenToolCallIds: [],
  locators: {
    byId: {},
    nextToolN: 1,
    nextExperimentN: 1,
  },
  bootProgress: null,
  pendingDecision: null,
  currentPhaseStepper: null,
  currentCompaction: null,
  config: {
    // Default to ``confirm`` to match the Python side
    // (chaos_agent/tui/state.py: PermissionMode.CONFIRM) — chaos
    // engineering defaults are safety-first; auto-mode (no
    // confirmation gate before injection) must be an explicit
    // choice via /mode toggle.
    permissionMode: "confirm",
    displayMode: "calm",
  },
};
