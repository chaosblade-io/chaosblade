/**
 * Lightweight perf-trace facility for diagnosing streaming-loop cost.
 *
 * Activation: ``BLADE_AI_PERF_TRACE=1`` at process start. When unset,
 * every public API is an inlinable no-op — zero overhead in production.
 *
 * Output: JSONL appended to ``~/.blade-ai/logs/tui-perf-trace.log``.
 * One line per mark / span end. Buffered + flushed every 500 ms (or
 * explicitly on demand via ``perfFlush``) so we don't fs.appendFile
 * inside the streaming hot loop.
 *
 * What to capture (kept tight on purpose — Phase 2 has 5 suspects;
 * one mark / span at each is enough to triangulate):
 *
 *   1. ``token.raw`` — every SSE raw token event in useStream's
 *      for-await. Lets us see the actual incoming rate (LLM emit
 *      rate) and per-event byte size, separate from the throttled
 *      dispatch rate downstream.
 *
 *   2. ``flushTokens`` — every TOKEN_APPENDED dispatch fire. The
 *      delta between ``token.raw`` count and ``flushTokens`` count
 *      tells us how effective the 60 ms throttle is; the timestamp
 *      delta tells us actual dispatch cadence (vs the 60 ms target).
 *
 *   3. ``reducer.TOKEN_APPENDED`` — measured around the reducer
 *      case body. Captures whether the new split path adds non-
 *      trivial reducer cost (history.push + array reconstruction).
 *      Payload distinguishes ``{ split: true }`` from in-place
 *      append so we can see split frequency.
 *
 *   4. ``commitPending`` — wraps the whole helper. The "stutter at
 *      stream end" complaint points here: TURN_DONE → commitPending
 *      → one-shot Static append of every leftover pending item.
 *      Duration reveals whether this is the spike or whether it's
 *      already cheap.
 *
 *   5. ``stdout.write`` (optional via ``BLADE_AI_PERF_TRACE_STDOUT=1``)
 *      — wraps the patched stdout.write to log byte count + bucket
 *      (frame redraw vs spinner tick vs small chrome). Off by
 *      default because it's noisy; turn on only when triangulating
 *      "is the bottleneck JS or IO?".
 *
 * Reading the log:
 *
 *   tail -f ~/.blade-ai/logs/tui-perf-trace.log
 *
 *   # Quick "raw events per second":
 *   awk -F'"label":' '/token.raw/{print}' ... | wc -l
 *
 *   # Average reducer cost per TOKEN_APPENDED:
 *   jq -r 'select(.label=="reducer.TOKEN_APPENDED") | .dur' ... \
 *     | awk '{s+=$1; n++} END{print s/n}'
 *
 *   # commitPending samples for the session:
 *   jq -r 'select(.label=="commitPending") | "\(.ts)\t\(.dur)"' ...
 *
 * The buffer is bounded at MAX_BUFFER to keep memory predictable on
 * a long-running TUI; oldest marks drop first if a turn produces
 * unusually heavy trace volume.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/** True iff ``BLADE_AI_PERF_TRACE=1`` at process start. Cached so
 *  hot-path callers don't repeat the env lookup. */
const ENABLED = process.env["BLADE_AI_PERF_TRACE"] === "1";

/** Separate gate for the stdout.write monkey-patch. Off by default
 *  because it (a) writes one mark per write call (noisy — hundreds
 *  per turn) and (b) double-hooks if ``BLADE_AI_DEBUG_OVERFLOW=1``
 *  is also set (overflowProbe.ts patches the same method). Turn on
 *  only when you need to confirm "is the stutter Ink/stdout layer?"
 *  and run WITHOUT the overflow probe to avoid double-wrap. */
const STDOUT_ENABLED =
  ENABLED && process.env["BLADE_AI_PERF_TRACE_STDOUT"] === "1";

const LOG_PATH = path.join(
  os.homedir(),
  ".blade-ai",
  "logs",
  "tui-perf-trace.log",
);

/** Cap the in-memory buffer so a stuck flush timer can't grow it
 *  unbounded. 5000 entries ≈ 1 MB JSONL; well past anything one
 *  turn realistically emits at 60 ms throttle × 30s turn = 500
 *  marks. */
const MAX_BUFFER = 5000;

/** Background flush cadence. 500 ms is a compromise: short enough
 *  that a SIGTERM mid-turn loses at most half a second of marks,
 *  long enough that we batch ~30 events per fs.appendFile call
 *  during streaming and don't add IO storm of our own. */
const FLUSH_INTERVAL_MS = 500;

interface Mark {
  /** Milliseconds since process start (``performance.now`` baseline). */
  ts: number;
  /** Short label namespacing the event (``token.raw``, ``commitPending``, …). */
  label: string;
  /** Duration in ms when this mark closes a span. Absent for point marks. */
  dur?: number;
  /** Free-form context (token count, byte size, split-vs-not, …). */
  payload?: Record<string, unknown>;
}

const buffer: Mark[] = [];
const PROCESS_START = performance.now();
let flushTimer: NodeJS.Timeout | null = null;
let logDirReady = false;
let appendErrorReported = false;

function ensureLogDir(): void {
  if (logDirReady) return;
  try {
    fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true });
  } catch {
    // best effort; appendFile will silently fail if the dir doesn't exist
  }
  logDirReady = true;
}

function scheduleFlush(): void {
  if (flushTimer) return;
  flushTimer = setTimeout(() => {
    flushTimer = null;
    flushNow();
  }, FLUSH_INTERVAL_MS);
  // ``unref`` so a stray flush timer doesn't keep the event loop
  // alive past natural exit. (The TUI exits via process.exit
  // anyway, but defensive against future restructuring.)
  flushTimer.unref?.();
}

function flushNow(): void {
  if (buffer.length === 0) return;
  ensureLogDir();
  const lines = buffer.map((m) => JSON.stringify(m)).join("\n") + "\n";
  buffer.length = 0;
  fs.appendFile(LOG_PATH, lines, (err) => {
    if (err && !appendErrorReported) {
      appendErrorReported = true;
      try {
        process.stderr.write(
          `[perf-trace] appendFile failed: ${err.message} (path=${LOG_PATH})\n`,
        );
      } catch {
        // give up
      }
    }
  });
}

function push(mark: Mark): void {
  if (buffer.length >= MAX_BUFFER) {
    // Drop oldest to keep memory bounded. The flush timer SHOULD
    // have drained us long before this, so hitting the cap means
    // either a flush failure or the producer is hammering us far
    // beyond the 60 ms throttle — either way, recent data wins.
    buffer.shift();
  }
  buffer.push(mark);
  scheduleFlush();
}

/**
 * Record a point-in-time event. Cheap: a single push + scheduleFlush
 * call. ``payload`` is JSON-serialized only at flush time, so
 * passing a small object is fine.
 */
export function perfMark(
  label: string,
  payload?: Record<string, unknown>,
): void {
  if (!ENABLED) return;
  push({
    ts: performance.now() - PROCESS_START,
    label,
    payload,
  });
}

/**
 * Wrap a synchronous body and record its duration. Re-throws any
 * exception after recording so the trace doesn't swallow errors.
 *
 * Use ``perfMark`` instead when the work isn't conveniently
 * wrappable (e.g. you only want to time a portion of a function).
 */
export function perfSpan<T>(
  label: string,
  fn: () => T,
  payload?: Record<string, unknown>,
): T {
  if (!ENABLED) return fn();
  const start = performance.now();
  try {
    return fn();
  } finally {
    push({
      ts: start - PROCESS_START,
      label,
      dur: performance.now() - start,
      payload,
    });
  }
}

/**
 * Force an immediate flush of the in-memory buffer. Useful at
 * turn boundaries (TURN_DONE / TURN_ABORTED) so a turn's trace
 * is on disk by the time the user reads the log. ``reason`` is
 * recorded as its own mark to make the flush boundaries easy
 * to find in the JSONL output.
 */
export function perfFlush(reason: string): void {
  if (!ENABLED) return;
  push({
    ts: performance.now() - PROCESS_START,
    label: `flush:${reason}`,
  });
  if (flushTimer) {
    clearTimeout(flushTimer);
    flushTimer = null;
  }
  flushNow();
}

/**
 * Wrap ``process.stdout.write`` so every write records:
 *   - duration of the synchronous write call (ms)
 *   - byte count of the chunk
 *   - bucket guess (frame redraw / spinner tick / small chrome) based
 *     on byte size; used to filter the log quickly with jq.
 *
 * The bucket heuristic mirrors qwen-code's "is this a frame? is this
 * a spinner tick?" classification — Ink writes are bimodal in size:
 *
 *   - < 200 bytes: spinner tick, single-char move, chrome refresh
 *   - 200-2000 bytes: small frame redraw (pending area only)
 *   - > 2000 bytes: full-frame redraw (Ink hit the eraseScreen branch)
 *
 * Reading the log:
 *
 *   # Total stdout time per second during a turn
 *   jq -r 'select(.label=="stdout.write") | "\(.ts|floor/1)\t\(.dur)"' ... \
 *     | awk '{sec=int($1/1000); s[sec]+=$2} END{for(k in s) print k, s[k]}'
 *
 *   # Largest writes (most likely "the stutter frame")
 *   jq -r 'select(.label=="stdout.write") | [.ts, .payload.bytes, .dur, .payload.bucket] | @tsv' ... \
 *     | sort -k2 -n | tail -20
 *
 *   # Worst-case write latency
 *   jq -r 'select(.label=="stdout.write") | .dur' ... \
 *     | sort -n | tail -5
 */
function hookStdoutPerf(): void {
  if (!STDOUT_ENABLED) return;
  let originalWrite: typeof process.stdout.write;
  try {
    originalWrite = process.stdout.write.bind(process.stdout);
  } catch {
    return;
  }
  const hooked = function (
    this: NodeJS.WriteStream,
    chunk: unknown,
    encodingOrCb?: unknown,
    cb?: unknown,
  ): boolean {
    // Measure ONLY the call into the real write. Side work (trace
    // bookkeeping) happens after; the recorded ``dur`` reflects how
    // long Ink waited for stdout to accept the bytes (which on a
    // TTY can be the actual blocking time — that's the data we
    // came for).
    const start = performance.now();
    const result = (
      originalWrite as unknown as (...args: unknown[]) => boolean
    )(chunk, encodingOrCb, cb);
    const dur = performance.now() - start;
    try {
      let bytes = 0;
      if (typeof chunk === "string") {
        bytes = Buffer.byteLength(chunk);
      } else if (Buffer.isBuffer(chunk)) {
        bytes = chunk.length;
      }
      // Bimodal bucket heuristic — see hook-docstring for thresholds.
      const bucket =
        bytes < 200 ? "tick" : bytes < 2000 ? "small" : "frame";
      push({
        ts: start - PROCESS_START,
        label: "stdout.write",
        dur,
        payload: { bytes, bucket },
      });
    } catch {
      // never let the probe break stdout
    }
    return result;
  };
  try {
    process.stdout.write = hooked as typeof process.stdout.write;
  } catch {
    // ignore — if we can't replace, we just get no stdout traces
  }
}

// Module-load breadcrumb — same purpose as overflowProbe's: makes
// the "I set BLADE_AI_PERF_TRACE but no file" failure mode obvious
// (no breadcrumb line means the bundle didn't load this file, or
// the env var was wrong).
if (ENABLED) {
  push({
    ts: 0,
    label: "perf-trace loaded",
    payload: {
      pid: process.pid,
      cwd: process.cwd(),
      logPath: LOG_PATH,
      nodeVersion: process.version,
      stdoutHookEnabled: STDOUT_ENABLED,
    },
  });
  try {
    process.stderr.write(
      `[perf-trace] enabled, writing to ${LOG_PATH} (pid=${process.pid}${
        STDOUT_ENABLED ? ", stdout-hook=on" : ""
      })\n`,
    );
  } catch {
    // ignore
  }
  hookStdoutPerf();
}
