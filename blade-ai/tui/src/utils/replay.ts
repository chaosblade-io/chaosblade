/**
 * Replay a Python TUIEvent jsonl recording by mapping each event to
 * its reducer ``Action`` equivalent.
 *
 * Recording line shape (from src/chaos_agent/tui/recording.py):
 *   {"ts": "2026-...", "type": "ThinkingReceived", "data": {...}}
 *
 * The ``type`` field is a Python TUIEvent dataclass class name, NOT
 * a SSE event-type string. We translate them here so the existing
 * reducer can consume replays without protocol awareness.
 *
 * M6 ships only "instant" replay: dispatch all events synchronously
 * in arrival order. Wall-clock pacing (replay-by-original-timing) is
 * a future M7 concern.
 */

import type { Action } from "../state/reducer.js";
import type { ToolStatus } from "../state/types.js";

interface RecordedEvent {
  ts?: string;
  type?: string;
  data?: Record<string, unknown>;
}

const asStr = (v: unknown): string => (typeof v === "string" ? v : "");

/**
 * Translate one recorded event to a reducer Action. Returns ``null``
 * for events we don't surface in the UI (e.g. PhaseCompleted, ProgressUpdate
 * — we only need NODE_STARTED for the LoadingIndicator's subject).
 */
export function recordedEventToAction(record: RecordedEvent): Action | null {
  const type = asStr(record.type);
  const data = (record.data ?? {}) as Record<string, unknown>;

  switch (type) {
    case "TokenReceived":
      return {
        type: "TOKEN_APPENDED",
        content: asStr(data["content"]),
        node: asStr(data["node"]),
      };

    case "ThinkingReceived":
      return {
        type: "THINKING_APPENDED",
        content: asStr(data["content"]),
        node: asStr(data["node"]),
      };

    case "ToolStarted":
      return {
        type: "TOOL_STARTED",
        // Recordings predate M5's ``call_id`` so synthesize a stable
        // key. ``${task_id}/${tool_name}`` is the same fallback the
        // live useStream path uses; for replay it's fine because the
        // recording is single-pass.
        callId: `replay/${asStr(data["task_id"]) || "task"}/${asStr(data["tool_name"])}`,
        name: asStr(data["tool_name"]),
        node: asStr(data["node"]),
      };

    case "ToolCompleted": {
      const status = (asStr(data["status"]) || "success") as ToolStatus;
      return {
        type: "TOOL_ENDED",
        callId: `replay/${asStr(data["task_id"]) || "task"}/${asStr(data["tool_name"])}`,
        name: asStr(data["tool_name"]),
        status,
        content: asStr(data["content"]),
      };
    }

    case "PhaseChanged": {
      // Distinguish start vs end by the message string the recorder
      // saved (``Starting <node>`` / ``Completed <node>``).
      const msg = asStr(data["message"]);
      const node = asStr(data["source"]) || asStr(data["phase"]);
      if (msg.startsWith("Starting")) {
        return { type: "NODE_STARTED", node };
      }
      if (msg.startsWith("Completed")) {
        return { type: "NODE_ENDED", node };
      }
      return null;
    }

    case "InterruptRequired": {
      const info = data["interrupt_info"];
      let content = "";
      if (typeof info === "object" && info !== null) {
        // Often ``info.plan_summary`` carries the body the user saw.
        const ps = (info as Record<string, unknown>)["plan_summary"];
        content = asStr(ps);
      }
      return {
        type: "CONFIRM_RECEIVED",
        content,
        taskId: asStr(data["task_id"]),
      };
    }

    case "TaskResult":
      return {
        type: "RESULT_RECEIVED",
        content: JSON.stringify({
          status: "success",
          data: (data["data"] as Record<string, unknown>) ?? {},
        }),
        taskId: asStr(data["task_id"]),
      };

    case "TaskError":
      return {
        type: "ERROR_RECEIVED",
        message: asStr(data["message"]),
        taskId: asStr(data["task_id"]),
      };

    // Recorded events we don't model in the live reducer — silently skip.
    case "TaskResumed":
    case "ProgressUpdate":
    case "PhaseCompleted":
    case "PhaseFailed":
    case "RecoveryTriggered":
    case "PreflightAction":
    case "PermissionModeChanged":
      return null;

    default:
      return null;
  }
}

export interface ReplayOptions {
  /**
   * Speedup factor relative to the original recording timing.
   *   - ``Infinity`` (or 0 / negative): instant — dispatch all events
   *     synchronously, ignoring timestamps.
   *   - ``1``: real-time. A 9-minute recording takes 9 minutes.
   *   - ``4`` (default): 4× faster — fast enough to be watchable but
   *     slow enough to feel like a paced rewatch.
   *   - ``10`` etc: arbitrary speed.
   */
  speed?: number;
  /**
   * Optional AbortSignal to cancel an in-flight replay. M7 wires this
   * through but doesn't yet expose a UI affordance for it; M8 will
   * hand the signal to the Composer's Esc handler.
   */
  signal?: AbortSignal;
  /**
   * Called once before each event dispatch with progress info. Lets a
   * caller surface "replaying 42 / 257" kind of feedback. Synchronous
   * — should not block.
   */
  onProgress?: (info: { index: number; total: number }) => void;
}

export interface ReplayStats {
  converted: number;
  skipped: number;
  /** True when the replay was aborted before reaching the final event. */
  aborted: boolean;
  /** Wall-clock duration in ms. */
  elapsedMs: number;
}

/**
 * Drive a full replay through ``dispatch``.
 *
 * Important: we deliberately do NOT dispatch ``TURN_STARTED``. The
 * Composer that invokes /replay has already echoed the user's literal
 * slash command (``/replay task-xxx``) into history; a second echo
 * (``(replay) task-xxx``) was the M6.1 self-check finding — it
 * appeared as a duplicate user line directly under the original.
 *
 * Just as deliberately, this function NO LONGER dispatches a final
 * ``TURN_DONE`` — that was the M8 self-check finding. When the
 * caller wraps the replay in ``REPLAY_STARTED / REPLAY_ENDED``,
 * an internal ``TURN_DONE`` would briefly flip ``streamState`` to
 * ``idle`` between the two, causing the LoadingIndicator to
 * disappear-then-reappear. The matching ``REPLAY_ENDED`` action
 * is the single source of truth for committing pending → history.
 * Callers that don't wrap (legacy / tests) must dispatch
 * ``REPLAY_ENDED`` themselves to surface the events.
 *
 * M7 timing: when ``speed`` is finite, events fire on a setTimeout
 * chain anchored to the first event's timestamp. The N-th event
 * fires at ``(events[N].ts - events[0].ts) / speed`` ms after the
 * first dispatch. Skipped events (those mapping to null Action) do
 * NOT consume time — we still respect the original ts gap of
 * subsequent events.
 */
export async function replayRecording(
  events: RecordedEvent[],
  dispatch: (a: Action) => void,
  // Kept on the signature for telemetry / log lines that might want
  // to know which task this replay represents.
  _taskId: string,
  opts: ReplayOptions = {},
): Promise<ReplayStats> {
  const speed = opts.speed ?? 4;
  const isInstant = !Number.isFinite(speed) || speed <= 0;
  const startedAt = Date.now();

  let converted = 0;
  let skipped = 0;

  // Anchor for relative-time pacing.
  const firstWithTs = events.find((e) => typeof e.ts === "string" && e.ts);
  const anchorMs = firstWithTs?.ts ? Date.parse(firstWithTs.ts) : NaN;
  const useTiming = !isInstant && Number.isFinite(anchorMs);

  for (let i = 0; i < events.length; i += 1) {
    if (opts.signal?.aborted) {
      return { converted, skipped, aborted: true, elapsedMs: Date.now() - startedAt };
    }

    const ev = events[i];
    if (!ev) continue; // belt-and-braces (TS noUncheckedIndexedAccess)

    if (useTiming && ev.ts) {
      const evtMs = Date.parse(ev.ts);
      if (Number.isFinite(evtMs)) {
        const targetOffset = (evtMs - (anchorMs as number)) / (speed as number);
        const elapsed = Date.now() - startedAt;
        const wait = Math.max(0, targetOffset - elapsed);
        if (wait > 0) {
          await sleep(wait, opts.signal);
          if (opts.signal?.aborted) {
            return {
              converted,
              skipped,
              aborted: true,
              elapsedMs: Date.now() - startedAt,
            };
          }
        }
      }
    }

    opts.onProgress?.({ index: i, total: events.length });

    const action = recordedEventToAction(ev);
    if (action === null) {
      skipped += 1;
      continue;
    }
    dispatch(action);
    converted += 1;
  }

  return {
    converted,
    skipped,
    aborted: false,
    elapsedMs: Date.now() - startedAt,
  };
}

/**
 * Sleep that wakes early on AbortSignal. Cleans up the abort
 * listener so we don't leak handlers when the timer fires first.
 */
function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) {
      resolve();
      return;
    }
    let aborted = false;
    const onAbort = (): void => {
      if (aborted) return;
      aborted = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      resolve();
    };
    const timer = setTimeout(() => {
      if (aborted) return;
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}
