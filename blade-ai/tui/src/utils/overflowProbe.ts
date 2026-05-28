/**
 * Diagnostic probe for the dynamic-frame-vs-viewport overflow we
 * suspect is the root cause of the inject-mode flicker + scroll
 * hijack + "blank gap in scrollback" symptoms. Inert by default;
 * activated by ``BLADE_AI_DEBUG_OVERFLOW=1``.
 *
 * Record kinds emitted to ``~/.blade-ai/logs/tui-overflow-debug.log``
 * (one JSON object per line, ``jq``-friendly):
 *
 *   _kind="loaded"   Module-load breadcrumb (rows/cols/argv/pid).
 *
 *   _kind="resize"   Terminal SIGWINCH (oldRows → newRows).
 *
 *   _kind="frame"    One per render's useLayoutEffect commit. Carries:
 *     - pendingH / controlsH / bootRow / frameH / overflow
 *     - loadingH / stepperH / inputH / footerH  (sub-control breakdown
 *       so we can tell which strip's mount/unmount caused a controlsH
 *       swing — typically PhaseStepperCard's 8-row appearance/disappearance)
 *     - pending/controls/loading/stepper/input/footer ``MeasureUs``
 *       (per-strip Yoga measurement time in microseconds — proves out
 *       whether layout itself is the bottleneck)
 *     - tsDelta (inter-render gap, ms) + actionsBatched (dispatches
 *       coalesced into this single render) — together expose React 18
 *       automatic batching effectiveness during streaming
 *     - pendingLen / tailKind / tailAgentTextLen / hasStepper /
 *       thinkingLen / streamState / pendingKinds / historyLen
 *     - lastWriteSeq  (correlates back to the most recent stdout record)
 *     - lastActionSeq (correlates back to the most recent action record)
 *
 *   _kind="commit"   Fires when ``state.history.length`` grows.
 *     Marks the moment Ink's normal render path wrote ``staticOutput``
 *     to scrollback. Carries delta + the kind of the just-committed
 *     item + frame state at commit moment.
 *
 *   _kind="action"   ONE PER REDUCER DISPATCH. Logs the action ``type``
 *     plus a compact summary of payload-level fields (lengths over
 *     bodies, callId-prefix over full UUIDs, etc.) so the log can
 *     carry 30k+ actions without becoming unreadable.
 *     Use ``jq 'select(._kind == "action")'`` to pull the dispatch
 *     stream; correlate with ``frame.actionsBatched`` to see which
 *     dispatches got coalesced into a single render.
 *
 *   _kind="stdout"   ONE PER PROCESS-STDOUT-WRITE CALL. The killer
 *     probe for the "blank gap" mystery: every byte Ink (or anyone
 *     else) writes to stdout is captured here with:
 *       - bytesLen / printableLen / newlines / eraseLine / cursorUp /
 *         cursorDown / cursorNextLine / cursorTo / cursorHome /
 *         cursorLeft / eraseScreen / eraseScrollback / hideCursor /
 *         showCursor   (parsed CSI escape counters)
 *       - truncated   (true when bytes > STDOUT_CAPTURE_LIMIT = 16KB)
 *       - bytes       (raw chunk, escape-encoded as JSON string)
 *     The raw bytes can be replayed offline into a synthetic cursor
 *     model (terminal width × height) to detect exactly when content
 *     scrolled past viewport bottom.
 *
 *   _kind="refs_null" First few renders before MainContent / Composer
 *     have mounted (refs not yet attached).
 *
 * Analysis recipes:
 *   tail -f ~/.blade-ai/logs/tui-overflow-debug.log | jq -c
 *   jq -c 'select(._kind == "commit")' tui-overflow-debug.log  # all Static appends
 *   jq -c 'select(._kind == "stdout" and .eraseScreen)' …      # full-screen wipes
 *   jq -c 'select(._kind == "stdout" and .newlines > 30)'      # big writes
 *   jq -c 'select(._kind == "frame" and .overflow > 0)'        # actual overflow events
 *   jq -c 'select(._kind == "frame" and .actionsBatched > 5)'  # well-batched bursts
 *   jq -c 'select(._kind == "frame" and .tsDelta < 20)'        # back-to-back renders
 *   jq -c 'select(._kind == "action")' | head                  # action stream
 *   jq -s 'map(select(._kind=="frame")) | map(.actionsBatched) | add / length' …  # avg batching
 *   jq -s 'group_by(.streamState) | map({state: .[0].streamState, max: ([.[].overflow] | max), n: length})' …
 *
 * Refs are wired by ``setProbePendingRef`` / ``setProbeControlsRef`` /
 * ``setProbeLoadingRef`` / ``setProbeStepperRef`` /
 * ``setProbeInputRef`` / ``setProbeFooterRef`` (callback refs on the
 * relevant <Box> elements). The hook ``useOverflowProbe`` is called
 * once at the top of <App>; it subscribes to the store fields that
 * drive chrome height + history length, so a fresh measurement +
 * commit-detection check happens on every render.
 *
 * Cost when disabled: a single env-var check + the ref callback
 * assignments (Ink does these regardless). Zero file IO, zero
 * measurement work, no stdout hook installed.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { performance } from "node:perf_hooks";
import { useLayoutEffect } from "react";
import { measureElement, type DOMElement } from "ink";
import { useTerminalSize } from "../hooks/useTerminalSize.js";
import { useAppSelector, useAppStateGetter } from "../state/store.js";
import type { HistoryItem } from "../state/types.js";

/**
 * Compact, stable string description of a pending item — designed so
 * the same item across consecutive renders produces the same string
 * (so ``useAppSelector``'s strict equality check doesn't churn) AND
 * carries enough detail to diagnose why ``flushLeadingStable`` is or
 * is not advancing past it.
 *
 * Format:
 *   thinking          → "thk"
 *   tool_group(2/5)   → "tg(2/5)" — 2 of 5 tools terminal
 *   tool_group(5/5)   → "tg(5/5)" — fully done, should be stable
 *   agent(140)        → "agt(140)" — 140 chars of text
 *   confirm_prompt(R) → "cp(R)" / "cp(U)" — resolved / unresolved
 *   confirm_context   → "cc"
 *   result            → "res"
 *   error             → "err"
 *   memory_compaction → "mc"
 *   turn_usage        → "tu"
 *   phase_stepper     → "ps"
 *   <other>           → "?<kind>"
 *
 * The ``flushLeadingStable`` stability rules right now:
 *   thinking            stable
 *   tool_group          stable iff (every tool not running)
 *   agent               stable iff !isTail
 *   confirm_prompt      stable iff resolved
 *   <anything else>     NOT in the rule set — defaults to stable=false,
 *                       which BREAKS the flush chain. So if any of
 *                       result / error / mc / tu / ps / cc shows up in
 *                       pending head, every subsequent flush attempt
 *                       no-ops until the user-visible TURN_DONE finally
 *                       commits the whole thing. That's the suspected
 *                       reason pending grew to 21 items in the
 *                       observed inject turn.
 */
function summarizePending(pending: HistoryItem[]): string {
  return pending
    .map((it) => {
      switch (it.kind) {
        case "thinking":
          return "thk";
        case "tool_group": {
          const total = it.tools.length;
          const done = it.tools.filter((t) => t.status !== "running").length;
          return `tg(${done}/${total})`;
        }
        case "agent":
          return `agt(${it.text.length})`;
        case "confirm_prompt":
          return it.resolved ? "cp(R)" : "cp(U)";
        case "confirm_context":
          return "cc";
        case "result":
          return "res";
        case "error":
          return "err";
        case "memory_compaction":
          return "mc";
        case "turn_usage":
          return "tu";
        case "phase_stepper":
          return "ps";
        default:
          return `?${it.kind}`;
      }
    })
    .join(",");
}

const LOG_PATH = path.join(
  os.homedir(),
  ".blade-ai",
  "logs",
  "tui-overflow-debug.log",
);

let logDirReady = false;
function ensureLogDir(): void {
  if (logDirReady) return;
  try {
    fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true });
  } catch {
    // best effort; if we can't create it, appendFile will silently fail
  }
  logDirReady = true;
}

// Module-scope refs. Hack but intentional: lets MainContent and
// Composer wire themselves up via callback refs without us having to
// thread props through BootRunner / App / MainContent / Composer.
// In production builds the env-var gate keeps these refs untouched
// from a behavioral standpoint — the values are written but never
// read. The probe hook is the only consumer.
//
// Sub-control refs (loading / stepper / input / footer) let us
// decompose ``controlsH`` so we can tell which strip moved when the
// total swings — typically PhaseStepperCard mount/unmount accounts
// for the +9 spikes, but without a per-strip breakdown the log just
// records "controls grew by 9" with no attribution.
const probeRefs: {
  pending: DOMElement | null;
  controls: DOMElement | null;
  loading: DOMElement | null;
  stepper: DOMElement | null;
  input: DOMElement | null;
  footer: DOMElement | null;
} = {
  pending: null,
  controls: null,
  loading: null,
  stepper: null,
  input: null,
  footer: null,
};

export function setProbePendingRef(el: DOMElement | null): void {
  probeRefs.pending = el;
}

export function setProbeControlsRef(el: DOMElement | null): void {
  probeRefs.controls = el;
}

export function setProbeLoadingRef(el: DOMElement | null): void {
  probeRefs.loading = el;
}

export function setProbeStepperRef(el: DOMElement | null): void {
  probeRefs.stepper = el;
}

export function setProbeInputRef(el: DOMElement | null): void {
  probeRefs.input = el;
}

export function setProbeFooterRef(el: DOMElement | null): void {
  probeRefs.footer = el;
}

/** True when ``BLADE_AI_DEBUG_OVERFLOW=1`` was set at process start.
 *  Cached so the per-render hook doesn't repeatedly hit ``process.env``.
 */
const ENABLED = process.env["BLADE_AI_DEBUG_OVERFLOW"] === "1";

/** Per-process counter included in each record so we can spot
 *  re-renders that produced identical measurements (drop-in
 *  ``uniq``-ability) and correlate with token throughput. */
let frameSeq = 0;
let firstMeasurementWritten = false;
let appendErrorReported = false;

/** Counter for the stdout-write hook — separate from frame seq so a
 *  given frame can correlate with the stdout writes that happened
 *  before / during it via timestamps. */
let writeSeq = 0;

/** Counter for reducer actions — same separation rationale as
 *  ``writeSeq``. Lets a frame record carry "since last frame, N
 *  actions were dispatched" so we can directly observe React 18's
 *  automatic batching at work. If batching is broken (one action =
 *  one render), this stays at 1 across the board; if batching is
 *  effective during streaming, we'll see clusters of 5-10 actions
 *  per frame. */
let actionSeq = 0;
let actionCountSinceLastFrame = 0;

/** Cap on raw bytes captured per stdout write. Spinner ticks are
 *  ~30-200 bytes; full dyn frame redraws on a 46×177 terminal hit
 *  ~5-8 KB. 16 KB cap captures the full frame even on wider terminals
 *  while preventing runaway log growth on accidentally-huge writes
 *  (e.g. paste through stdin via terminal echo). */
const STDOUT_CAPTURE_LIMIT = 16 * 1024;

/**
 * Summarise an ANSI-laden stdout chunk into a few scalar counters.
 * The full bytes go in a sibling field of the record so offline
 * analysis can replay the byte stream into a synthetic cursor model;
 * the summary is what shows up in compact ``jq`` queries.
 *
 * Detected escapes (the ones Ink + log-update emit):
 *   \x1b[2K          erase entire line
 *   \x1b[<N>A        cursor up N (default 1)
 *   \x1b[<N>B        cursor down N (default 1)
 *   \x1b[<N>C        cursor right N (default 1)
 *   \x1b[<N>D        cursor left N (default 1)
 *   \x1b[<N>E        cursor next line N (default 1)
 *   \x1b[<N>;<M>H    cursor to (N,M)
 *   \x1b[H           cursor home (1,1)
 *   \x1b[G           cursor to col 0
 *   \x1b[2J          erase entire viewport
 *   \x1b[3J          erase scrollback (the dangerous one)
 *   \x1b[?25l/h      hide / show cursor
 *
 * ``eraseLines(N)`` (from ansi-escapes) is N times ``\x1b[2K`` plus
 * N-1 cursor-up plus a final cursor-left; the count of ``\x1b[2K``
 * tells us N directly so we don't have to re-derive it.
 */
function summariseChunk(str: string): {
  bytesLen: number;
  newlines: number;
  eraseLine: number;
  cursorUp: number;
  cursorDown: number;
  cursorNextLine: number;
  cursorTo: number;
  cursorHome: number;
  cursorLeft: number;
  eraseScreen: boolean;
  eraseScrollback: boolean;
  hideCursor: number;
  showCursor: number;
  printableLen: number;
} {
  const bytesLen = str.length;
  let newlines = 0;
  let eraseLine = 0;
  let cursorUp = 0;
  let cursorDown = 0;
  let cursorNextLine = 0;
  let cursorTo = 0;
  let cursorHome = 0;
  let cursorLeft = 0;
  let eraseScreen = false;
  let eraseScrollback = false;
  let hideCursor = 0;
  let showCursor = 0;
  let printableLen = 0;

  let i = 0;
  while (i < str.length) {
    const ch = str.charCodeAt(i);
    if (ch === 0x0a) {
      newlines++;
      i++;
      continue;
    }
    if (ch === 0x1b && str[i + 1] === "[") {
      // CSI sequence: \x1b[ <params> <final>
      let j = i + 2;
      while (j < str.length) {
        const c = str.charCodeAt(j);
        // params: digits / ; / ?
        if (
          (c >= 0x30 && c <= 0x39) ||
          c === 0x3b ||
          c === 0x3f
        ) {
          j++;
          continue;
        }
        break;
      }
      const final = str[j];
      const body = str.slice(i + 2, j);
      switch (final) {
        case "K":
          if (body === "2" || body === "") eraseLine++;
          break;
        case "A":
          cursorUp++;
          break;
        case "B":
          cursorDown++;
          break;
        case "E":
          cursorNextLine++;
          break;
        case "H":
          if (body === "" || body === "1;1") cursorHome++;
          else cursorTo++;
          break;
        case "G":
          cursorLeft++;
          break;
        case "J":
          if (body === "2") eraseScreen = true;
          else if (body === "3") eraseScrollback = true;
          break;
        case "l":
          if (body === "?25") hideCursor++;
          break;
        case "h":
          if (body === "?25") showCursor++;
          break;
        default:
          // Other CSI (SGR colours, etc.) — ignored for our purpose.
          break;
      }
      i = j + 1;
      continue;
    }
    // OSC etc. (rare from Ink) — skip the lead byte and let the
    // counter advance. We don't fully parse them; printableLen stays
    // accurate only for ESC-free segments, which is fine for
    // diagnostic summaries.
    if (ch === 0x1b) {
      i++;
      continue;
    }
    printableLen++;
    i++;
  }
  return {
    bytesLen,
    newlines,
    eraseLine,
    cursorUp,
    cursorDown,
    cursorNextLine,
    cursorTo,
    cursorHome,
    cursorLeft,
    eraseScreen,
    eraseScrollback,
    hideCursor,
    showCursor,
    printableLen,
  };
}

/**
 * Compact summary of a reducer ``Action`` for the action log.
 *
 * Goals:
 *   - One line per dispatch — never blow up the log with raw payloads.
 *   - Carry just enough payload to correlate "this dispatch happened"
 *     with downstream stdout writes / frame measurements.
 *   - Keep ``type`` always present so ``jq 'select(.type=="...")'``
 *     filters cleanly.
 *
 * For high-frequency / large-payload actions (TOKEN_APPENDED,
 * THINKING_APPENDED, TOOL_ENDED), we log only the LENGTH of the body
 * — never the body itself. Reasons: (a) bytes already captured in
 * stdout records, (b) keeping action records tiny lets us look at
 * 30k-action streams without a 1 GB log.
 *
 * The ``Action`` discriminated union lives in ``state/reducer.ts`` —
 * importing it here would create a circular dep
 * (overflowProbe → reducer → store → overflowProbe). We accept
 * ``unknown`` and discriminate by the runtime ``type`` string. The
 * cost is one ``as Record<string, unknown>`` cast at the entry; the
 * runtime shape is fully checked by string equality on ``type``.
 */
function summariseAction(action: unknown): Record<string, unknown> {
  if (typeof action !== "object" || action === null) return { type: "unknown" };
  const a = action as Record<string, unknown>;
  const type = typeof a["type"] === "string" ? (a["type"] as string) : "unknown";
  const out: Record<string, unknown> = { type };
  switch (type) {
    case "TURN_STARTED":
      out["inputLen"] =
        typeof a["input"] === "string" ? (a["input"] as string).length : 0;
      break;
    case "TOKEN_APPENDED":
    case "THINKING_APPENDED":
      out["contentLen"] =
        typeof a["content"] === "string"
          ? (a["content"] as string).length
          : 0;
      out["node"] = a["node"];
      break;
    case "TOOL_STARTED":
      out["callId"] =
        typeof a["callId"] === "string"
          ? (a["callId"] as string).slice(0, 8)
          : null;
      out["name"] = a["name"];
      out["node"] = a["node"];
      break;
    case "TOOL_ENDED":
      out["callId"] =
        typeof a["callId"] === "string"
          ? (a["callId"] as string).slice(0, 8)
          : null;
      out["name"] = a["name"];
      out["status"] = a["status"];
      out["contentLen"] =
        typeof a["content"] === "string"
          ? (a["content"] as string).length
          : 0;
      break;
    case "NODE_STARTED":
      out["node"] = a["node"];
      out["phase"] = a["phase"];
      break;
    case "NODE_ENDED":
      out["node"] = a["node"];
      break;
    case "USAGE_RECEIVED":
      out["inputTokens"] = a["inputTokens"];
      out["outputTokens"] = a["outputTokens"];
      break;
    case "PHRASE_TICK":
      out["phrase"] = a["phrase"];
      break;
    case "RESULT_RECEIVED":
    case "ERROR_RECEIVED":
      out["taskId"] = a["taskId"];
      out["contentLen"] =
        typeof a["content"] === "string"
          ? (a["content"] as string).length
          : typeof a["message"] === "string"
            ? (a["message"] as string).length
            : 0;
      break;
    case "CONFIRM_RECEIVED":
      out["taskId"] = a["taskId"];
      out["node"] = a["node"];
      break;
    case "CONFIRM_RESOLVED":
    case "CONFIRM_USER_DECIDED":
      out["taskId"] = a["taskId"];
      out["answer"] = a["answer"];
      break;
    case "MEMORY_COMPACTION_STARTED":
    case "MEMORY_COMPACTION_COMPLETED":
    case "MEMORY_COMPACTION_FAILED":
      out["layer"] = a["layer"];
      out["tokensBefore"] = a["tokensBefore"];
      out["tokensAfter"] = a["tokensAfter"];
      break;
    case "REPLAY_STARTED":
    case "REPLAY_ENDED":
      out["taskId"] = a["taskId"];
      out["aborted"] = a["aborted"];
      break;
    case "MODE_TOGGLED":
    case "DISPLAY_MODE_CHANGED":
      out["mode"] = a["mode"];
      break;
    case "TURN_ABORTED":
      out["reason"] = a["reason"];
      break;
    case "BOOT_PROGRESS_SHOW":
      out["text"] = a["text"];
      break;
    case "HISTORY_APPENDED":
      // Don't unpack the full item; just its kind.
      out["itemKind"] =
        typeof a["item"] === "object" && a["item"] !== null
          ? (a["item"] as Record<string, unknown>)["kind"]
          : "unknown";
      break;
    // Idempotent / argless / cheap actions get only the type:
    //   TURN_DONE, TURN_TRANSITION, HISTORY_CLEARED,
    //   CONSTRAIN_HEIGHT_TOGGLED, CONFIRM_DECISION_CONSUMED,
    //   BOOT_PROGRESS_HIDE, RECOVERY_TRIGGERED, etc.
    default:
      break;
  }
  return out;
}

/**
 * Public hook the StoreProvider's wrapped dispatch calls before
 * forwarding to the real reducer. Records a ``_kind="action"`` log
 * line and bumps the per-frame action counter so the next frame
 * record can report ``actionsBatched``.
 *
 * No-op when ``BLADE_AI_DEBUG_OVERFLOW`` is unset.
 */
export function recordAction(action: unknown): void {
  if (!ENABLED) return;
  actionCountSinceLastFrame++;
  writeRecord({
    _kind: "action",
    seq: ++actionSeq,
    ts: Date.now(),
    ...summariseAction(action),
  });
}

/**
 * Append a record (any shape; gets a ``_kind`` discriminator) to
 * the log. First write also lazy-creates the directory. Errors are
 * surfaced to stderr ONCE per process so we don't drown the user's
 * terminal but they still see something if the path is unwritable.
 */
function writeRecord(record: Record<string, unknown>): void {
  ensureLogDir();
  const line = JSON.stringify(record) + "\n";
  fs.appendFile(LOG_PATH, line, (err) => {
    if (err && !appendErrorReported) {
      appendErrorReported = true;
      try {
        process.stderr.write(
          `[overflow-probe] appendFile failed: ${err.message} (path=${LOG_PATH})\n`,
        );
      } catch {
        // give up
      }
    }
  });
}

// Module-load breadcrumb. The very fact that this file shows up in
// ~/.blade-ai/logs/ proves: (a) BLADE_AI_DEBUG_OVERFLOW was honored,
// (b) the running JS bundle includes our new code, (c) the path is
// writable. Use this to rule out the "I set the var but no log
// appeared" failure mode before we look at any frame data.
if (ENABLED) {
  writeRecord({
    _kind: "loaded",
    ts: Date.now(),
    pid: process.pid,
    path: LOG_PATH,
    cwd: process.cwd(),
    argv: process.argv,
    nodeVersion: process.version,
    initialRows: process.stdout.rows ?? 0,
    initialColumns: process.stdout.columns ?? 0,
  });
  // Also print to stderr so a curious user running with `2>/tmp/x`
  // can confirm activation without hunting for the file.
  try {
    process.stderr.write(
      `[overflow-probe] enabled, writing to ${LOG_PATH} (pid=${process.pid})\n`,
    );
  } catch {
    // Ink may have raw mode on; ignore.
  }

  // SIGWINCH watcher — write one record per resize so the log
  // captures exactly when the user changed terminal size. The frame
  // records on either side of a resize record show the chrome /
  // pending re-measurement, making it easy to correlate "I resized"
  // with "did chrome height jump and trigger an overflow".
  //
  // ``process.stdout`` is a Node TTY stream and emits ``resize``
  // synchronously on SIGWINCH. We dedupe against the last seen
  // rows/columns because some terminals re-fire ``resize`` on
  // focus / hover events with identical dimensions.
  let lastRows = process.stdout.rows ?? 0;
  let lastCols = process.stdout.columns ?? 0;
  const onResize = (): void => {
    const newRows = process.stdout.rows ?? 0;
    const newCols = process.stdout.columns ?? 0;
    if (newRows === lastRows && newCols === lastCols) return;
    writeRecord({
      _kind: "resize",
      ts: Date.now(),
      oldRows: lastRows,
      oldColumns: lastCols,
      newRows,
      newColumns: newCols,
      rowsDelta: newRows - lastRows,
      colsDelta: newCols - lastCols,
    });
    lastRows = newRows;
    lastCols = newCols;
  };
  // Set a generous listener limit because Ink + our probe + other
  // tooling can compete for the same emitter. The probe adds 1; we
  // don't want a MaxListenersExceededWarning to disrupt the host
  // terminal during a debugging session.
  try {
    process.stdout.setMaxListeners(
      Math.max(15, process.stdout.getMaxListeners()),
    );
  } catch {
    // best effort
  }
  process.stdout.on("resize", onResize);

  // ──────────────────────────────────────────────────────────────
  // Raw stdout write capture.
  //
  // Why this is the killer probe for the "blank gap" mystery: the
  // ``frame`` records show what the SHAPE of each render is, but
  // not what bytes Ink actually emitted — and the gap is, by
  // definition, a discrepancy between the rendered shape and what
  // ended up on screen. Hooking ``process.stdout.write`` captures
  // the literal byte stream so we can replay it into a synthetic
  // cursor model offline and see exactly when blank rows entered
  // scrollback.
  //
  // We monkey-patch the ``write`` method (not the underlying
  // ``_write`` of the stream) because Ink calls ``write`` directly.
  // The patch:
  //   1. Logs a record with the chunk's bytes (truncated to
  //      ``STDOUT_CAPTURE_LIMIT``) and a summary of recognised
  //      escape sequences.
  //   2. Calls the original ``write`` with the original args so
  //      Ink's behaviour is unchanged.
  //
  // Errors inside the probe path are caught and silently swallowed —
  // breaking stdout would leave the user with an unresponsive
  // terminal; a missing log line is a much smaller price.
  //
  // Note: ``fs.appendFile`` (the underlying writeRecord) writes to
  // a separate fd, so it does NOT cause infinite recursion through
  // the stdout hook. ``process.stderr`` writes (used for the
  // "[overflow-probe] enabled..." breadcrumb) are NOT hooked
  // because stderr is a different stream.
  try {
    const originalWrite = process.stdout.write.bind(process.stdout);
    type WriteFn = typeof process.stdout.write;
    const hooked: WriteFn = function (
      this: NodeJS.WriteStream,
      chunk: unknown,
      encodingOrCb?: unknown,
      cb?: unknown,
    ): boolean {
      try {
        let str: string | null = null;
        if (typeof chunk === "string") {
          str = chunk;
        } else if (Buffer.isBuffer(chunk)) {
          str = chunk.toString("utf8");
        }
        if (str !== null && str.length > 0) {
          const summary = summariseChunk(str);
          const truncated = str.length > STDOUT_CAPTURE_LIMIT;
          const bytes = truncated
            ? str.slice(0, STDOUT_CAPTURE_LIMIT)
            : str;
          writeRecord({
            _kind: "stdout",
            seq: ++writeSeq,
            ts: Date.now(),
            ...summary,
            truncated,
            bytes,
          });
        }
      } catch {
        // best effort — never let probe break stdout
      }
      // Forward to the real write. The TS overload soup of
      // WritableStream.write means we cast through unknown.
      return (originalWrite as unknown as (
        ...args: unknown[]
      ) => boolean)(chunk, encodingOrCb, cb);
    };
    process.stdout.write = hooked;
  } catch (err) {
    try {
      process.stderr.write(
        `[overflow-probe] failed to hook stdout.write: ${
          err instanceof Error ? err.message : String(err)
        }\n`,
      );
    } catch {
      // give up
    }
  }
}

/**
 * Track ``state.history.length`` across renders so the hook can emit
 * a ``commit`` record the moment new Static items are appended.
 * Pairs cleanly with the stdout-write log: every commit record has
 * a matching cluster of ``stdout`` records around its timestamp,
 * letting us tie a specific Static append to the byte stream Ink
 * actually wrote.
 */
let lastHistoryLen = 0;

/** Last frame's wall-clock — used to compute the inter-render gap.
 *  Combined with ``actionsBatched`` (count of dispatches between
 *  this frame and the previous), tells us how effective React 18's
 *  automatic batching is during streaming. */
let lastFrameTs = 0;

export function useOverflowProbe(): void {
  // Subscribe to the state fields that move the chrome / pending
  // heights so the hook re-runs whenever any of them changes. We
  // don't gate the hook calls on ``ENABLED`` — React requires a
  // stable hook order — but the effect body is a no-op when disabled.
  const { rows, columns } = useTerminalSize();
  const pendingLen = useAppSelector((s) => s.pending.length);
  const tailKind = useAppSelector(
    (s) => s.pending[s.pending.length - 1]?.kind ?? "none",
  );
  const tailAgentTextLen = useAppSelector((s) => {
    const last = s.pending[s.pending.length - 1];
    return last?.kind === "agent" ? last.text.length : 0;
  });
  const hasStepper = useAppSelector((s) => s.currentPhaseStepper !== null);
  // 2026-05-26 perf — was ``s.thoughtBuffer.length`` (changes per token,
  // forcing the App re-render cascade on every thinking chunk even when
  // ``ENABLED === false``). Switched to the edge-triggered boolean so we
  // only re-render on the 0→N / N→0 transition. Precise length is read
  // non-subscribing via ``getState`` inside the ENABLED branch below so
  // the ``thinkingLen`` log field is unchanged for downstream jq queries.
  const hasActiveThinking = useAppSelector((s) => s.hasActiveThinking);
  const getState = useAppStateGetter();
  const streamState = useAppSelector((s) => s.streamState);
  const bootProgress = useAppSelector((s) => s.bootProgress);
  const historyLen = useAppSelector((s) => s.history.length);
  const lastHistoryItemKind = useAppSelector((s) => {
    const tail = s.history[s.history.length - 1];
    return tail?.kind ?? "none";
  });
  // Pending kind sequence — the diagnostic that tells us which item
  // is stuck at pending[0] and why flushLeadingStable can't advance.
  // Returns a string so the selector's reference equality test stays
  // stable across renders that didn't actually change pending.
  const pendingKinds = useAppSelector((s) => summarizePending(s.pending));

  useLayoutEffect(() => {
    if (!ENABLED) return;

    const pendingEl = probeRefs.pending;
    const controlsEl = probeRefs.controls;
    if (!pendingEl && !controlsEl) {
      // Refs not wired yet (first few renders before MainContent /
      // Composer mount). Record the bail so we can tell "probe never
      // measured" from "refs never attached" in the log.
      if (frameSeq < 3) {
        writeRecord({
          _kind: "refs_null",
          seq: ++frameSeq,
          ts: Date.now(),
          pendingAttached: pendingEl !== null,
          controlsAttached: controlsEl !== null,
          streamState,
        });
      }
      return;
    }

    // ``measureElement`` calls Yoga's layout calculation under the
    // hood. Timing each call lets us spot a slow strip that's
    // dragging out the useLayoutEffect — typically a heavily-nested
    // Box with Flex children causes Yoga to re-compute multiple
    // times. Captured as separate fields so a single slow strip
    // doesn't get hidden in an aggregate.
    let pendingH = 0;
    let controlsH = 0;
    let pendingW = 0;
    let controlsW = 0;
    let pendingMeasureUs = 0;
    let controlsMeasureUs = 0;
    if (pendingEl) {
      const t0 = performance.now();
      try {
        const m = measureElement(pendingEl);
        pendingH = m.height;
        pendingW = m.width;
      } catch {
        pendingH = -1;
      }
      pendingMeasureUs = Math.round((performance.now() - t0) * 1000);
    }
    if (controlsEl) {
      const t0 = performance.now();
      try {
        const m = measureElement(controlsEl);
        controlsH = m.height;
        controlsW = m.width;
      } catch {
        controlsH = -1;
      }
      controlsMeasureUs = Math.round((performance.now() - t0) * 1000);
    }

    // Sub-control breakdown — measured ONLY when refs attached. A
    // missing strip (e.g. stepper on chat-only turns) reports as 0.
    // Returns [height, microseconds].
    const measureTimed = (el: DOMElement | null): [number, number] => {
      if (!el) return [0, 0];
      const t0 = performance.now();
      try {
        const h = measureElement(el).height;
        return [h, Math.round((performance.now() - t0) * 1000)];
      } catch {
        return [-1, Math.round((performance.now() - t0) * 1000)];
      }
    };
    const [loadingH, loadingMeasureUs] = measureTimed(probeRefs.loading);
    const [stepperH, stepperMeasureUs] = measureTimed(probeRefs.stepper);
    const [inputH, inputMeasureUs] = measureTimed(probeRefs.input);
    const [footerH, footerMeasureUs] = measureTimed(probeRefs.footer);

    const bootRow = bootProgress ? 1 : 0;
    // Frame height: what Ink will write to the dynamic region. Static
    // items have already been burned into scrollback by the patched
    // path and don't contribute to per-render rewrites.
    const frameH = Math.max(0, pendingH) + Math.max(0, controlsH) + bootRow;
    const overflow = Math.max(0, frameH - rows);

    const now = Date.now();
    const tsDelta = lastFrameTs > 0 ? now - lastFrameTs : 0;
    const actionsBatched = actionCountSinceLastFrame;
    actionCountSinceLastFrame = 0;
    lastFrameTs = now;

    // Read precise thoughtBuffer length non-subscribing — the hook
    // itself only subscribes to ``hasActiveThinking`` (edge-triggered)
    // to avoid per-token re-renders when the probe is disabled.
    const thinkingLen = hasActiveThinking
      ? getState().thoughtBuffer.length
      : 0;

    const record = {
      seq: ++frameSeq,
      ts: now,
      // Inter-render gap (ms). Spinner ticks are 12.5 Hz = 80 ms;
      // streaming TOKEN_APPENDED is 60-80 ms typically. ``tsDelta``
      // approaching 200+ ms suggests render coalescing / lag; very
      // small (<20 ms) suggests redundant re-renders.
      tsDelta,
      // Number of reducer dispatches that landed between the previous
      // frame and this one. ``1`` means batching is off (every action
      // → its own render); ``5-10`` means React 18's auto-batching is
      // collapsing token bursts into single renders.
      actionsBatched,
      rows,
      columns,
      pendingH,
      pendingW,
      controlsH,
      controlsW,
      bootRow,
      frameH,
      overflow,
      pendingLen,
      tailKind,
      tailAgentTextLen,
      hasStepper,
      thinkingLen,
      streamState,
      pendingKinds,
      historyLen,
      // Sub-control heights — sum of these (plus marginTop=1 on
      // Composer's outer Box) should equal controlsH; if it doesn't,
      // Yoga is rounding somewhere we should investigate.
      loadingH,
      stepperH,
      inputH,
      footerH,
      // measureElement timing in microseconds. Total layout-time
      // budget per render = pendingMeasureUs + controlsMeasureUs +
      // loadingMeasureUs + stepperMeasureUs + inputMeasureUs +
      // footerMeasureUs. If this approaches ms-scale we have a Yoga
      // perf problem to chase down.
      pendingMeasureUs,
      controlsMeasureUs,
      loadingMeasureUs,
      stepperMeasureUs,
      inputMeasureUs,
      footerMeasureUs,
      // Last associated stdout write seq so we can quickly grep
      // "what bytes were written just before this frame measurement?"
      lastWriteSeq: writeSeq,
      // Last associated action seq so we can grep
      // "what action triggered this re-render?"
      lastActionSeq: actionSeq,
    };

    writeRecord({ _kind: "frame", ...record });

    // Static commit detection — fires whenever ``history.length``
    // grew between renders. Ink's normal render path wrote
    // ``staticOutput`` to stdout right around this time; the matching
    // ``stdout`` record(s) (queryable via ``ts`` window or
    // ``lastWriteSeq``) carry the literal bytes.
    if (historyLen !== lastHistoryLen) {
      writeRecord({
        _kind: "commit",
        seq: frameSeq,
        ts: Date.now(),
        prevHistoryLen: lastHistoryLen,
        newHistoryLen: historyLen,
        delta: historyLen - lastHistoryLen,
        lastHistoryItemKind,
        // Snapshot of frame state at commit moment so we don't have
        // to cross-reference into the frame record.
        frameH,
        pendingH,
        controlsH,
        pendingKinds,
        streamState,
      });
      lastHistoryLen = historyLen;
    }

    if (!firstMeasurementWritten) {
      firstMeasurementWritten = true;
      try {
        process.stderr.write(
          `[overflow-probe] first measurement: rows=${rows} pendingH=${pendingH} controlsH=${controlsH} frameH=${frameH} overflow=${overflow}\n`,
        );
      } catch {
        // ignore
      }
    }
  });
}

/** Exposed for tests / one-off scripts that want the resolved log
 *  path without re-deriving it. */
export function getOverflowProbeLogPath(): string {
  return LOG_PATH;
}

/** True if the probe is active for this process. Useful for showing
 *  a one-line hint at startup so the user knows where to look for
 *  the data. */
export function isOverflowProbeEnabled(): boolean {
  return ENABLED;
}
