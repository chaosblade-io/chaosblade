/**
 * Multi-line cursor arithmetic on a codepoint array.
 *
 * "Cursor" is a codepoint index into the buffer (NOT a UTF-16 unit
 * index — see InputPrompt for the emoji rationale). "Line" / "col"
 * are derived projections used to drive ↑/↓/Home/End semantics.
 *
 * Pure functions, framework-agnostic. Tested in smoke-slash.mjs.
 */

export interface LineCol {
  /** 0-indexed line number. ``\n`` increments. */
  line: number;
  /** 0-indexed codepoint offset within the line. */
  col: number;
}

/**
 * Project a cursor codepoint index → (line, col).
 *
 * The cursor is conceptually "between" codepoints, so a cursor sitting
 * on a ``\n`` is reported as the **end** of the line that contains the
 * preceding chars, not the start of the next one. (Standard editor
 * semantics — pressing ↓ from "end of line N" goes to line N+1.)
 */
export function cursorToLineCol(cps: string[], cursor: number): LineCol {
  let line = 0;
  let col = 0;
  const stop = Math.max(0, Math.min(cursor, cps.length));
  for (let i = 0; i < stop; i += 1) {
    if (cps[i] === "\n") {
      line += 1;
      col = 0;
    } else {
      col += 1;
    }
  }
  return { line, col };
}

/**
 * Inverse of ``cursorToLineCol``. Clamps:
 *   - ``line`` past EOF → cursor at end of last line
 *   - ``col`` past EOL  → cursor at end of that line (just before \n)
 */
export function lineColToCursor(
  cps: string[],
  line: number,
  col: number,
): number {
  if (line < 0) return 0;
  let curLine = 0;
  let lineStart = 0;
  // Walk to the start of ``line``.
  for (let i = 0; i < cps.length; i += 1) {
    if (curLine === line) break;
    if (cps[i] === "\n") {
      curLine += 1;
      lineStart = i + 1;
    }
  }
  if (curLine < line) {
    // Asked for a line past EOF — clamp to the last codepoint.
    return cps.length;
  }
  // Now find the end of this line (next \n or EOF) and clamp ``col``.
  let lineEnd = cps.length;
  for (let i = lineStart; i < cps.length; i += 1) {
    if (cps[i] === "\n") {
      lineEnd = i;
      break;
    }
  }
  return Math.min(lineStart + Math.max(0, col), lineEnd);
}

/** Codepoint index of the first character of the line containing ``cursor``. */
export function lineStartIdx(cps: string[], cursor: number): number {
  for (let i = Math.min(cursor, cps.length) - 1; i >= 0; i -= 1) {
    if (cps[i] === "\n") return i + 1;
  }
  return 0;
}

/**
 * Codepoint index of the position **just before** the next ``\n``
 * (or buffer end). Used by Ctrl+E / End.
 */
export function lineEndIdx(cps: string[], cursor: number): number {
  for (let i = Math.max(0, cursor); i < cps.length; i += 1) {
    if (cps[i] === "\n") return i;
  }
  return cps.length;
}
