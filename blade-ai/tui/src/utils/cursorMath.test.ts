/**
 * cursorMath invariants. These functions look small but every M5/M7
 * self-check found a fresh edge case in them — emoji surrogate pairs,
 * multi-line ↑/↓ at boundaries, EOF clamping. Pinning them in unit
 * tests so a future "small" refactor can't silently regress.
 */

import { describe, expect, it } from "vitest";
import {
  cursorToLineCol,
  lineColToCursor,
  lineStartIdx,
  lineEndIdx,
} from "./cursorMath.js";

const cps = (s: string): string[] => Array.from(s);

describe("cursorToLineCol", () => {
  it("reports col 0 at buffer start", () => {
    expect(cursorToLineCol(cps("hello"), 0)).toEqual({ line: 0, col: 0 });
  });

  it("counts codepoints, not UTF-16 units (emoji)", () => {
    // "😀" is one codepoint but two UTF-16 units. After the emoji the
    // cursor is at col 1 (codepoint), not col 2 (UTF-16).
    const buf = cps("a😀b");
    expect(cursorToLineCol(buf, 2)).toEqual({ line: 0, col: 2 });
  });

  it("resets col on \\n and increments line", () => {
    expect(cursorToLineCol(cps("ab\ncd"), 4)).toEqual({ line: 1, col: 1 });
  });

  it("clamps cursor past EOF to buffer length", () => {
    const buf = cps("hi");
    expect(cursorToLineCol(buf, 999)).toEqual({ line: 0, col: 2 });
  });

  it("treats sitting on \\n as end-of-line for the preceding line", () => {
    // Cursor at index 2 (the \n). Per editor semantics this is "end of
    // line 0", not "start of line 1". Pressing ↓ from here goes to
    // line 1 col 0; that's the only correct behaviour.
    const buf = cps("ab\ncd");
    expect(cursorToLineCol(buf, 2)).toEqual({ line: 0, col: 2 });
  });
});

describe("lineColToCursor", () => {
  it("inverts cursorToLineCol round-trip on multi-line buffers", () => {
    const buf = cps("alpha\nbeta\ngamma");
    for (let i = 0; i <= buf.length; i += 1) {
      const lc = cursorToLineCol(buf, i);
      const back = lineColToCursor(buf, lc.line, lc.col);
      expect(back).toBe(i);
    }
  });

  it("clamps line past EOF to buffer end", () => {
    const buf = cps("a\nb");
    expect(lineColToCursor(buf, 99, 0)).toBe(buf.length);
  });

  it("clamps col past EOL to just before \\n", () => {
    const buf = cps("ab\ncd");
    // Line 0 has 2 codepoints; col 99 should land at index 2 (right
    // before the \n), not eat into the next line.
    expect(lineColToCursor(buf, 0, 99)).toBe(2);
  });

  it("handles negative line by clamping to 0", () => {
    expect(lineColToCursor(cps("x"), -1, 0)).toBe(0);
  });

  it("preserves col when ↓ from a long line into a short line", () => {
    // From "long line" col 8 pressing ↓ to "ab" should land at end of
    // "ab" (col 2), not crash — that's exactly what the clamp gives us.
    const buf = cps("long line\nab");
    expect(lineColToCursor(buf, 1, 8)).toBe(buf.length);
  });
});

describe("lineStartIdx / lineEndIdx", () => {
  it("lineStartIdx returns 0 on the first line", () => {
    expect(lineStartIdx(cps("hello"), 3)).toBe(0);
  });

  it("lineStartIdx returns the index just after the previous \\n", () => {
    // "ab\ncd" — cursor at index 4 (in "cd") → line starts at 3.
    expect(lineStartIdx(cps("ab\ncd"), 4)).toBe(3);
  });

  it("lineEndIdx returns position just before next \\n", () => {
    expect(lineEndIdx(cps("ab\ncd"), 0)).toBe(2);
  });

  it("lineEndIdx returns buffer length on the last line", () => {
    const buf = cps("ab\ncd");
    expect(lineEndIdx(buf, 4)).toBe(buf.length);
  });

  it("Home/End round-trip preserves line", () => {
    const buf = cps("hello\nworld");
    const start = lineStartIdx(buf, 8);
    const end = lineEndIdx(buf, 8);
    // Both should report the same line via cursorToLineCol.
    expect(cursorToLineCol(buf, start).line).toBe(1);
    expect(cursorToLineCol(buf, end).line).toBe(1);
  });
});
