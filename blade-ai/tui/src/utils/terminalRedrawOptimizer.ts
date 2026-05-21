/**
 * Terminal redraw optimizer — monkey-patches ``stdout.write`` to fold
 * Ink's verbose erase-line sequences into a compact equivalent.
 *
 * **Why this exists**: Ink clears dynamic output via
 * ``ansi-escapes.eraseLines(N)``, which emits a ``clear-line +
 * cursor-up`` pair for every previous line:
 *
 *     \x1b[2K\x1b[1A\x1b[2K\x1b[1A\x1b[2K\x1b[1A...
 *
 * For each ``\x1b[1A`` the terminal must update its cursor state. On
 * frequent re-renders (token streaming, spinner ticks) the cumulative
 * cursor jitter is what users perceive as "scrollback bouncing" when
 * they try to scroll back through history mid-stream — the scroll
 * position can't keep up with the cursor-up storm.
 *
 * **The fix**: detect the repeating pattern and rewrite as a single
 * bounded jump:
 *
 *     \x1b[NA + N×eraseLine (going down with \x1b[1B) + \x1b[NA\x1b[G
 *
 * Same observable result, one cursor-up command instead of N. The
 * terminal's scrollback isn't bounced N times per repaint.
 *
 * Lifted from qwen-code (Apache-2.0) at
 * ``packages/cli/src/ui/utils/terminalRedrawOptimizer.ts`` — same
 * algorithm, env-var renamed to BLADE_AI_LEGACY_ERASE_LINES, stats
 * trimmed (we don't surface them anywhere).
 */

// ESC + "[" = CSI (Control Sequence Introducer). Built via
// String.fromCharCode so the literal ESC byte (0x1B) doesn't sit
// invisibly in source — copy-paste corruption hazard + impossible
// to grep for as plain text.
const CSI = `${String.fromCharCode(0x1b)}[`;
const ERASE_LINE = `${CSI}2K`;
const CURSOR_UP_ONE = `${CSI}1A`;
const CURSOR_DOWN_ONE = `${CSI}1B`;
const CURSOR_LEFT = `${CSI}G`;

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

const MULTILINE_ERASE_LINES_PATTERN = new RegExp(
  `(?:${escapeRegExp(ERASE_LINE + CURSOR_UP_ONE)})+${escapeRegExp(
    ERASE_LINE + CURSOR_LEFT,
  )}`,
  "g",
);

function countOccurrences(value: string, search: string): number {
  let count = 0;
  let index = 0;
  while ((index = value.indexOf(search, index)) !== -1) {
    count++;
    index += search.length;
  }
  return count;
}

export function optimizeMultilineEraseLines(output: string): string {
  return output.replace(MULTILINE_ERASE_LINES_PATTERN, (sequence) => {
    const lineCount = countOccurrences(sequence, ERASE_LINE);
    const cursorUpCount = lineCount - 1;
    // Single-line cases don't benefit (we'd write the same bytes plus
    // a tiny constant overhead) — leave the original sequence intact
    // so we never net-regress on short redraws.
    if (cursorUpCount <= 1) {
      return sequence;
    }
    let boundedErase = `${CSI}${cursorUpCount}A`;
    for (let line = 0; line < lineCount; line++) {
      boundedErase += ERASE_LINE;
      if (line < lineCount - 1) {
        boundedErase += CURSOR_DOWN_ONE;
      }
    }
    return `${boundedErase}${CSI}${cursorUpCount}A${CURSOR_LEFT}`;
  });
}

/**
 * Wrap ``stdout.write`` so every chunk passes through
 * ``optimizeMultilineEraseLines`` before hitting the terminal.
 *
 * Returns the restore function; caller is expected to invoke it on
 * exit so a hot-reload or test teardown doesn't leak the patched
 * write. Idempotent: calling restore twice (or after a different
 * patch has installed itself on top) is safe — we only undo when
 * the current ``stdout.write`` is still our patched function.
 *
 * Set ``BLADE_AI_LEGACY_ERASE_LINES=1`` to opt out entirely; the
 * install becomes a no-op and the restore is a no-op too. Use this
 * if a terminal misbehaves with the optimized form (none observed
 * so far on iTerm2 / WezTerm / Ghostty / Terminal.app / kitty /
 * Windows Terminal, but the escape hatch is the honest answer).
 */
export function installTerminalRedrawOptimizer(
  stdout: NodeJS.WriteStream,
): () => void {
  if (process.env["BLADE_AI_LEGACY_ERASE_LINES"] === "1") {
    return () => {};
  }

  const originalWrite = stdout.write;

  const optimizedWrite = function (
    this: NodeJS.WriteStream,
    chunk: unknown,
    encodingOrCallback?: BufferEncoding | ((error?: Error | null) => void),
    callback?: (error?: Error | null) => void,
  ) {
    const optimizedChunk =
      typeof chunk === "string" ? optimizeMultilineEraseLines(chunk) : chunk;
    return originalWrite.call(
      this,
      optimizedChunk as string | Uint8Array,
      encodingOrCallback as BufferEncoding,
      callback,
    );
  } as typeof stdout.write;

  stdout.write = optimizedWrite;

  return () => {
    if (stdout.write === optimizedWrite) {
      stdout.write = originalWrite;
    }
  };
}
