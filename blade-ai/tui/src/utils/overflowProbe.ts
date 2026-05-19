/**
 * Diagnostic probe for the dynamic-frame-vs-viewport overflow we
 * suspect is the root cause of the inject-mode flicker + scroll
 * hijack. Inert by default; activated by ``BLADE_AI_DEBUG_OVERFLOW=1``.
 *
 * What it measures, every layout commit:
 *   - pendingH      ``measureElement`` on MainContent's pending <Box>
 *   - controlsH     ``measureElement`` on Composer's outer <Box>
 *                   (LoadingIndicator + PhaseStepperCard + InputPrompt
 *                    + Footer + marginTop)
 *   - bootRow       1 if BootProgress visible, else 0
 *   - frameH        pendingH + controlsH + bootRow
 *                   (this is the "dynamic frame" that Ink rewrites
 *                    on every render; if it exceeds rows, the patch
 *                    in tui/patches/ink+7.0.3.patch lets the overflow
 *                    rows leak into scrollback every frame — that's
 *                    the suspected mechanism behind the user-reported
 *                    "循环输出 + 滚轮锁底")
 *   - overflow      max(0, frameH - rows) — the killer number
 *
 * State context written alongside the measurement so we can correlate
 * spikes with what the agent was doing at that moment:
 *   pendingLen, tailKind, tailAgentTextLen, hasStepper, thinkingLen,
 *   streamState.
 *
 * Output:
 *   ~/.blade-ai/logs/tui-overflow-debug.log  (one JSON record per
 *   line, ``jq``-friendly)
 *
 * Analysis recipes:
 *   tail -f ~/.blade-ai/logs/tui-overflow-debug.log | jq -c
 *   jq -c 'select(.overflow > 0)' ~/.blade-ai/logs/tui-overflow-debug.log
 *   jq -s 'group_by(.streamState) | map({state: .[0].streamState, max: ([.[].overflow] | max), n: length})' ~/.blade-ai/logs/tui-overflow-debug.log
 *
 * Refs are wired by ``setProbePendingRef`` / ``setProbeControlsRef``
 * (callback refs on the relevant <Box> elements). The hook
 * ``useOverflowProbe`` is called once at the top of <App>; it
 * subscribes to the store fields that drive chrome height, so a
 * fresh measurement is taken every render that could plausibly
 * have changed the frame.
 *
 * Cost when disabled: a single env-var check + the ref callback
 * assignments (Ink does these regardless). Zero file IO, zero
 * measurement work.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { useLayoutEffect } from "react";
import { measureElement, type DOMElement } from "ink";
import { useTerminalSize } from "../hooks/useTerminalSize.js";
import { useAppSelector } from "../state/store.js";
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
const probeRefs: { pending: DOMElement | null; controls: DOMElement | null } = {
  pending: null,
  controls: null,
};

export function setProbePendingRef(el: DOMElement | null): void {
  probeRefs.pending = el;
}

export function setProbeControlsRef(el: DOMElement | null): void {
  probeRefs.controls = el;
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
}

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
  const thinkingLen = useAppSelector((s) => s.thoughtBuffer.length);
  const streamState = useAppSelector((s) => s.streamState);
  const bootProgress = useAppSelector((s) => s.bootProgress);
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

    let pendingH = 0;
    let controlsH = 0;
    let pendingW = 0;
    let controlsW = 0;
    if (pendingEl) {
      try {
        const m = measureElement(pendingEl);
        pendingH = m.height;
        pendingW = m.width;
      } catch {
        pendingH = -1;
      }
    }
    if (controlsEl) {
      try {
        const m = measureElement(controlsEl);
        controlsH = m.height;
        controlsW = m.width;
      } catch {
        controlsH = -1;
      }
    }

    const bootRow = bootProgress ? 1 : 0;
    // Frame height: what Ink will write to the dynamic region. Static
    // items have already been burned into scrollback by the patched
    // path and don't contribute to per-render rewrites.
    const frameH = Math.max(0, pendingH) + Math.max(0, controlsH) + bootRow;
    const overflow = Math.max(0, frameH - rows);

    const record = {
      seq: ++frameSeq,
      ts: Date.now(),
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
    };

    writeRecord({ _kind: "frame", ...record });
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
