/**
 * @blade-ai/core — the shared, environment-neutral core for Blade-AI
 * frontends (tui/ and web/).
 *
 * Contents:
 *   api/      BladeClient (HTTP + SSE), the StreamEvent wire protocol,
 *             and the pure auth-precedence rule (pickServerToken).
 *   state/    AppState / HistoryItem types, the reducer, the React
 *             store, and the slash-command registry.
 *   hooks/    useStream — the turn/replay/compact streaming engine.
 *   i18n/     t / tArr + explicit host language configuration.
 *   utils/    pure helpers + the host-injection sinks (perf / debug).
 *
 * Hard rule: nothing here may import ``node:*`` or touch
 * ``process``/``Buffer`` outside of a ``typeof process !== "undefined"``
 * guard (i18n's Node auto-detect is the one sanctioned case). Host
 * capabilities (auth token, perf backend, file export, screen clear)
 * are injected — see ``ClientOptions.getAuthToken``,
 * ``setPerfSink``/``setActionRecorder``, and the optional fields on
 * ``SlashCommandContext``.
 */

// ── api ─────────────────────────────────────────────────────────────
export {
  BladeClient,
  TUI_PROTOCOL_VERSION,
  type ClientOptions,
  type CreateSessionOpts,
  type InterruptResolve,
  type ResumableSessionItem,
  type SessionListItem,
  type TurnRequest,
} from "./api/client.js";
export { pickServerToken } from "./api/auth.js";
export {
  isStreamEvent,
  type AutoApprovedEvent,
  type ConfirmEvent,
  type ContextSizeEvent,
  type DoneEvent,
  type ErrorEvent,
  type LlmStartEvent,
  type MemoryCompactionEvent,
  type NodeEndEvent,
  type NodeMessageEvent,
  type NodeStartEvent,
  type ResultEvent,
  type StreamEvent,
  type StreamEventBase,
  type StreamEventType,
  type ThinkingEvent,
  type TokenEvent,
  type ToolEndEvent,
  type ToolStartEvent,
  type UsageEvent,
} from "./api/events.js";

// ── state ───────────────────────────────────────────────────────────
export {
  initialAppState,
  type AgentItem,
  type AppState,
  type BootDoctorCardItem,
  type BootDoctorCheck,
  type ConfirmContextItem,
  type ConfirmPromptItem,
  type ConfirmPromptMode,
  type ErrorItem,
  type ExperimentsCardItem,
  type ExperimentsCardRow,
  type HelpCardItem,
  type HelpCardRow,
  type HelpCardSection,
  type HistoryItem,
  type LogItem,
  type MemoryCardItem,
  type MemoryCompactionItem,
  type ModelCardItem,
  type ModelCardRow,
  type ModelCardSection,
  type PendingTaskRow,
  type PendingTasksCardItem,
  type PhaseId,
  type PhaseStatus,
  type PhaseStep,
  type PhaseStepperItem,
  type PhaseStepperState,
  type ResultItem,
  type RuntimeDoctorCardItem,
  type SessionCardItem,
  type SessionCardRow,
  type SessionInfo,
  type StreamState,
  type SystemItem,
  type ThinkingItem,
  type ToolGroupItem,
  type ToolItem,
  type ToolStatus,
  type TurnUsageItem,
  type UserItem,
  type WelcomeCardItem,
} from "./state/types.js";
export { reducer, extractThoughtSubject, type Action } from "./state/reducer.js";
export {
  StoreProvider,
  useAppDispatch,
  useAppSelector,
  useAppState,
  useAppStateGetter,
  type StoreProviderProps,
} from "./state/store.js";
export {
  resetStreamingCounters,
  streamingResponseCharsRef,
} from "./state/streamingRefs.js";
export { sessionStatsRef } from "./state/sessionStats.js";
export {
  buildRegistry,
  formatFaultType,
  formatReviewCard,
  parseSlashCommand,
  parseSlashLine,
  parseTasksArgs,
  passesTasksFilter,
  runSessionResume,
  SlashCommandRegistry,
  SLASH_GROUP_ORDER,
  type ParsedCommand,
  type SessionResumeContext,
  type SessionResumeOutcome,
  type SlashCommand,
  type SlashCommandContext,
  type SlashGroup,
  type SlashSubcommand,
  type TasksFilter,
} from "./state/commands.js";

// ── hooks ───────────────────────────────────────────────────────────
export {
  useStream,
  type SubmitTurnOpts,
  type UseStreamApi,
} from "./hooks/useStream.js";

// ── i18n ────────────────────────────────────────────────────────────
export {
  configureI18n,
  detectLangFromEnv,
  getActiveLang,
  t,
  tArr,
  type Dict,
  type LangCode,
  type LangEnv,
} from "./i18n/index.js";

// ── utils ───────────────────────────────────────────────────────────
export { findLastSafeSplitPoint } from "./utils/markdownSplit.js";
export { parseResultEnvelope } from "./utils/result.js";
export {
  recordedEventToAction,
  replayRecording,
  type ReplayOptions,
  type ReplayStats,
} from "./utils/replay.js";
export {
  perfFlush,
  perfMark,
  perfSpan,
  setPerfSink,
  type PerfSink,
} from "./utils/perf.js";
export {
  recordAction,
  setActionRecorder,
  type ActionRecorder,
} from "./utils/debug.js";
