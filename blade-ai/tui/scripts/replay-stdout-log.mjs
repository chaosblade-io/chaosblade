#!/usr/bin/env node
/**
 * Replay ``_kind="stdout"`` records from tui-overflow-debug.log into a
 * synthetic terminal model (rows × cols viewport, append-only
 * scrollback) and emit a per-write cursor-trajectory report.
 *
 * Pinpoints the bytes that pushed content into scrollback or left
 * blank rows in the viewport — the smoking gun for the "blank gap"
 * symptom we can't see from the frame records alone.
 *
 * Usage:
 *   node scripts/replay-stdout-log.mjs                           (latest log)
 *   node scripts/replay-stdout-log.mjs <path>
 *   node scripts/replay-stdout-log.mjs --window <seq-from>:<seq-to>
 *   node scripts/replay-stdout-log.mjs --commit <commit-seq>     (focus on one commit)
 *   node scripts/replay-stdout-log.mjs --blanks                  (show blank-row count over time)
 *
 * Output is human-readable text on stdout; pipe through ``less``.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";

const DEFAULT_LOG = path.join(
  os.homedir(),
  ".blade-ai",
  "logs",
  "tui-overflow-debug.log",
);

function parseArgs(argv) {
  const args = { logPath: DEFAULT_LOG, window: null, commit: null, blanks: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--window") {
      const [from, to] = (argv[++i] ?? "").split(":").map(Number);
      args.window = { from: from ?? 0, to: to ?? Infinity };
    } else if (a === "--commit") {
      args.commit = Number(argv[++i]);
    } else if (a === "--blanks") {
      args.blanks = true;
    } else if (!a.startsWith("--")) {
      args.logPath = a;
    }
  }
  return args;
}

/** Synthetic terminal: 1-indexed rows, 1-indexed cols.
 *
 *  ``viewport[r][c]`` is the character at row r, col c (0-indexed
 *  internally). Cells that have been WRITTEN are tracked separately
 *  from cells that are blank-from-erase, so we can count "real
 *  content" vs "leftover erased rows".
 *
 *  Rows that scroll off the top are appended to ``scrollback``. */
class TerminalModel {
  constructor(rows, cols) {
    this.rows = rows;
    this.cols = cols;
    // viewport[r][c] = { ch: string|null, written: bool }
    this.viewport = this._blankViewport();
    this.row = 1;
    this.col = 1;
    this.scrollback = []; // each entry = { ch: string|null, written: bool }[] for one row
    this.scrolledBlankRows = 0; // count of all-blank rows that entered scrollback
    this.scrolledContentRows = 0;
  }
  _blankViewport() {
    const v = new Array(this.rows);
    for (let r = 0; r < this.rows; r++) {
      v[r] = this._blankRow();
    }
    return v;
  }
  _blankRow() {
    return new Array(this.cols).fill(null).map(() => ({ ch: null, written: false }));
  }
  scrollUp(n) {
    for (let i = 0; i < n; i++) {
      const top = this.viewport.shift();
      const allBlank = top.every((cell) => !cell.written);
      // Tag each scrollback entry with the write seq that caused
      // it to scroll out, so callers can ask "when did this blank
      // appear in scrollback?".
      const tagged = top.slice();
      tagged.causedBySeq = this.currentWriteSeq;
      tagged.allBlank = allBlank;
      if (allBlank) this.scrolledBlankRows++;
      else this.scrolledContentRows++;
      this.scrollback.push(tagged);
      this.viewport.push(this._blankRow());
    }
  }
  putChar(ch) {
    if (this.row < 1 || this.row > this.rows) return; // off-viewport (shouldn't happen)
    const cellsRow = this.viewport[this.row - 1];
    if (this.col >= 1 && this.col <= this.cols) {
      cellsRow[this.col - 1] = { ch, written: true };
    }
    this.col++;
    if (this.col > this.cols) {
      // Wrap is terminal-dependent; most terminals stay at last col
      // until next \n. Our synthetic model: stay at last col
      // (Ink rarely overflows column anyway since it pre-fits text).
      this.col = this.cols;
    }
  }
  newline() {
    this.row++;
    this.col = 1;
    if (this.row > this.rows) {
      this.scrollUp(1);
      this.row = this.rows;
    }
  }
  carriageReturn() {
    this.col = 1;
  }
  cursorUp(n = 1) {
    this.row = Math.max(1, this.row - n);
  }
  cursorDown(n = 1) {
    this.row = Math.min(this.rows, this.row + n);
  }
  cursorRight(n = 1) {
    this.col = Math.min(this.cols, this.col + n);
  }
  cursorLeft(n = 1) {
    this.col = Math.max(1, this.col - n);
  }
  cursorNextLine(n = 1) {
    this.row = Math.min(this.rows, this.row + n);
    this.col = 1;
  }
  cursorTo(r, c = 1) {
    this.row = Math.max(1, Math.min(this.rows, r));
    this.col = Math.max(1, Math.min(this.cols, c));
  }
  eraseLine() {
    if (this.row >= 1 && this.row <= this.rows) {
      this.viewport[this.row - 1] = this._blankRow();
    }
  }
  eraseScreen() {
    this.viewport = this._blankViewport();
  }
  eraseScrollback() {
    this.scrollback = [];
    this.scrolledBlankRows = 0;
    this.scrolledContentRows = 0;
  }
  countBlankRowsAtBottom() {
    let n = 0;
    for (let r = this.rows - 1; r >= 0; r--) {
      const allBlank = this.viewport[r].every((cell) => !cell.written);
      if (allBlank) n++;
      else break;
    }
    return n;
  }
  countTotalBlankRows() {
    let n = 0;
    for (let r = 0; r < this.rows; r++) {
      const allBlank = this.viewport[r].every((cell) => !cell.written);
      if (allBlank) n++;
    }
    return n;
  }
  /** Apply an escape-laden chunk and return what changed.
   *  Caller may set ``writeSeq`` so tagged scrollback entries can
   *  reference the originating stdout write. */
  applyChunk(str, writeSeq) {
    this.currentWriteSeq = writeSeq;
    const before = {
      row: this.row,
      col: this.col,
      scrollback: this.scrollback.length,
      blankAtBottom: this.countBlankRowsAtBottom(),
    };
    let i = 0;
    while (i < str.length) {
      const ch = str.charCodeAt(i);
      if (ch === 0x0a) {
        this.newline();
        i++;
        continue;
      }
      if (ch === 0x0d) {
        this.carriageReturn();
        i++;
        continue;
      }
      if (ch === 0x1b && str[i + 1] === "[") {
        let j = i + 2;
        while (j < str.length) {
          const c = str.charCodeAt(j);
          if ((c >= 0x30 && c <= 0x39) || c === 0x3b || c === 0x3f) {
            j++;
            continue;
          }
          break;
        }
        const final = str[j];
        const body = str.slice(i + 2, j);
        const params = body
          .split(";")
          .map((p) => (p === "" ? undefined : Number(p)));
        switch (final) {
          case "K":
            if (body === "2" || body === "") this.eraseLine();
            break;
          case "A":
            this.cursorUp(params[0] ?? 1);
            break;
          case "B":
            this.cursorDown(params[0] ?? 1);
            break;
          case "C":
            this.cursorRight(params[0] ?? 1);
            break;
          case "D":
            this.cursorLeft(params[0] ?? 1);
            break;
          case "E":
            this.cursorNextLine(params[0] ?? 1);
            break;
          case "G":
            this.cursorTo(this.row, params[0] ?? 1);
            break;
          case "H":
            this.cursorTo(params[0] ?? 1, params[1] ?? 1);
            break;
          case "J":
            if (body === "2") this.eraseScreen();
            else if (body === "3") this.eraseScrollback();
            break;
        }
        i = j + 1;
        continue;
      }
      if (ch === 0x1b) {
        // ignore non-CSI escapes for our purposes
        i++;
        continue;
      }
      // Plain printable
      this.putChar(str[i]);
      i++;
    }
    const after = {
      row: this.row,
      col: this.col,
      scrollback: this.scrollback.length,
      blankAtBottom: this.countBlankRowsAtBottom(),
    };
    return {
      before,
      after,
      scrolled: after.scrollback - before.scrollback,
      scrolledBlankRows: this.scrolledBlankRows,
      scrolledContentRows: this.scrolledContentRows,
    };
  }
}

async function* readRecords(logPath) {
  const stream = fs.createReadStream(logPath, { encoding: "utf8" });
  const rl = readline.createInterface({ input: stream, crlfDelay: Infinity });
  for await (const line of rl) {
    if (!line.trim()) continue;
    try {
      yield JSON.parse(line);
    } catch {
      // skip malformed lines
    }
  }
}

/** Time window (ms) around a named commit's ``ts`` for ``--commit``. */
const COMMIT_WINDOW_MS = 2000;

async function main() {
  const args = parseArgs(process.argv.slice(2));
  // First pass: get terminal dimensions from "loaded" record + commits.
  let rows = 46;
  let cols = 177;
  const commits = [];
  for await (const r of readRecords(args.logPath)) {
    if (r._kind === "loaded") {
      rows = r.initialRows || rows;
      cols = r.initialColumns || cols;
    } else if (r._kind === "resize") {
      rows = r.newRows || rows;
      cols = r.newColumns || cols;
    } else if (r._kind === "commit") {
      commits.push(r);
    }
  }
  process.stderr.write(
    `terminal model: ${rows} rows × ${cols} cols, ${commits.length} commits\n`,
  );

  // ``--commit <seq>`` narrows replay to stdout writes within
  // ±COMMIT_WINDOW_MS of the named commit's wall-clock — we want to
  // see the byte stream that produced one specific Static append.
  // The commit is keyed by its frame seq (the ``seq`` field on the
  // ``commit`` record).
  let tsWindow = null;
  if (args.commit !== null) {
    const target = commits.find((c) => c.seq === args.commit);
    if (!target) {
      process.stderr.write(
        `error: no commit with seq=${args.commit} (available: ${commits
          .map((c) => c.seq)
          .join(", ")})\n`,
      );
      process.exit(2);
    }
    tsWindow = {
      from: target.ts - COMMIT_WINDOW_MS,
      to: target.ts + COMMIT_WINDOW_MS,
    };
    process.stderr.write(
      `--commit window: ts=${target.ts} ± ${COMMIT_WINDOW_MS}ms (seq filter relaxed)\n`,
    );
  }

  // Second pass: replay stdout records.
  const term = new TerminalModel(rows, cols);
  let processedWrites = 0;
  let totalScrolled = 0;
  const blankSpikes = [];

  for await (const r of readRecords(args.logPath)) {
    if (r._kind === "resize") {
      // Resize during session: re-init model. Lossy but unavoidable
      // — terminal state across resizes is not recoverable from the
      // byte stream alone.
      term.rows = r.newRows;
      term.cols = r.newColumns;
      term.viewport = term._blankViewport();
      continue;
    }
    if (r._kind !== "stdout") continue;
    if (args.window) {
      if (r.seq < args.window.from || r.seq > args.window.to) continue;
    }
    if (tsWindow) {
      if (r.ts < tsWindow.from || r.ts > tsWindow.to) continue;
    }
    if (r.truncated) {
      process.stderr.write(
        `warn: stdout seq=${r.seq} truncated at ${r.bytes.length} bytes — replay may diverge\n`,
      );
    }
    const result = term.applyChunk(r.bytes, r.seq);
    processedWrites++;
    totalScrolled += result.scrolled;
    if (result.scrolled > 0 && result.before.blankAtBottom > 0) {
      blankSpikes.push({
        writeSeq: r.seq,
        ts: r.ts,
        scrolled: result.scrolled,
        prevBlankAtBottom: result.before.blankAtBottom,
        scrolledBlankRowsTotal: term.scrolledBlankRows,
      });
    }
  }
  // After replay: separate "orphan blanks" (real bug) from "marginTop
  // blanks" (expected). Orphan = blank cell in the AGGREGATE scrollback
  // that sits in the middle of a sequence of consecutive blank rows
  // longer than would be produced by a single marginTop=1 between
  // siblings.
  let runLen = 0;
  let maxRun = 0;
  let runs = [];
  for (let i = 0; i < term.scrollback.length; i++) {
    const isBlank = term.scrollback[i].every((c) => !c.written);
    if (isBlank) {
      runLen++;
      if (runLen > maxRun) maxRun = runLen;
    } else {
      if (runLen >= 2) runs.push({ len: runLen, endIdx: i });
      runLen = 0;
    }
  }
  if (runLen >= 2) runs.push({ len: runLen, endIdx: term.scrollback.length });

  // Report.
  console.log("=".repeat(70));
  console.log(`Replay summary`);
  console.log("=".repeat(70));
  console.log(`Stdout writes processed:    ${processedWrites}`);
  console.log(`Total rows scrolled into scrollback: ${totalScrolled}`);
  console.log(`  ↳ blank rows (raw):       ${term.scrolledBlankRows}`);
  console.log(`  ↳ content rows:           ${term.scrolledContentRows}`);
  console.log(`Final cursor:               row=${term.row} col=${term.col}`);
  console.log(`Final blank rows in viewport: ${term.countTotalBlankRows()} (${term.countBlankRowsAtBottom()} at bottom)`);
  console.log("");
  console.log(`Consecutive blank-row runs in scrollback (≥2 = suspicious):`);
  console.log(`  longest run:  ${maxRun} rows`);
  console.log(`  total runs:   ${runs.length}`);
  console.log(`  runs by length:`);
  const runHist = runs.reduce((acc, r) => {
    acc[r.len] = (acc[r.len] ?? 0) + 1;
    return acc;
  }, {});
  Object.entries(runHist)
    .sort((a, b) => Number(b[0]) - Number(a[0]))
    .slice(0, 10)
    .forEach(([len, n]) => console.log(`    ${len} consecutive blanks × ${n} run(s)`));
  console.log("");
  console.log(`Top 10 longest runs (with originating stdout writes):`);
  runs
    .sort((a, b) => b.len - a.len)
    .slice(0, 10)
    .forEach((r) => {
      const startIdx = r.endIdx - r.len;
      const causes = new Set();
      for (let i = startIdx; i < r.endIdx; i++) {
        const seq = term.scrollback[i].causedBySeq;
        if (seq !== undefined) causes.add(seq);
      }
      const seqs = Array.from(causes).sort((a, b) => a - b);
      console.log(
        `    ${r.len} rows blank @ scrollback[${startIdx}..${r.endIdx - 1}], from stdout seq=${seqs.join(",")}`,
      );
    });
  console.log("");
  if (blankSpikes.length > 0) {
    console.log(`Blank-rows-into-scrollback events (top 10):`);
    blankSpikes
      .sort((a, b) => b.scrolled - a.scrolled)
      .slice(0, 10)
      .forEach((e) => {
        console.log(
          `  seq=${e.writeSeq} ts=${e.ts} scrolled=${e.scrolled} (had ${e.prevBlankAtBottom} blank at bottom before this write)`,
        );
      });
  } else {
    console.log(`No blank-rows-into-scrollback events detected.`);
  }
  console.log("");
  console.log(`Commits seen: ${commits.length}`);
  if (args.blanks) {
    console.log("");
    console.log(`Final scrollback (${term.scrollback.length} rows total):`);
    term.scrollback.slice(-30).forEach((row, i) => {
      const idx = term.scrollback.length - 30 + i;
      const text = row.map((c) => c.ch ?? " ").join("").trimEnd();
      const flag = row.every((c) => !c.written) ? "[BLANK]" : "       ";
      console.log(`  ${idx}: ${flag} ${text.slice(0, 100)}`);
    });
  }
}

main().catch((err) => {
  process.stderr.write(`replay failed: ${err.stack || err}\n`);
  process.exit(1);
});
