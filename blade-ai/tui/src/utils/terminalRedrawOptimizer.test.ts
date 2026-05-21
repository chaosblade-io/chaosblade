/**
 * Algorithm tests for ``optimizeMultilineEraseLines``.
 *
 * We don't test ``installTerminalRedrawOptimizer`` because it
 * monkey-patches process.stdout — the install/restore dance is
 * trivial (assignment + restore-when-still-ours) and exercising
 * it in a test would either touch real stdout or require a stub
 * that adds more code than it verifies.
 *
 * The interesting contract is:
 *   · Single eraseLine + single cursorUp: unchanged (no benefit
 *     to folding, would net-regress by adding bounded-jump overhead).
 *   · N >= 2 repeats: folded into one ``cursor up N`` jump + N
 *     eraseLines + final ``cursor up N`` + ``column 1``.
 *   · Non-matching content: passes through verbatim.
 *   · Multiple independent matches in one chunk: each folded
 *     independently.
 */

import { describe, expect, it } from "vitest";
import { optimizeMultilineEraseLines } from "./terminalRedrawOptimizer.js";

const ESC = String.fromCharCode(0x1b);
const ERASE_LINE = `${ESC}[2K`;
const UP_ONE = `${ESC}[1A`;
const DOWN_ONE = `${ESC}[1B`;
const LEFT = `${ESC}[G`;

describe("optimizeMultilineEraseLines", () => {
  it("passes through chunks with no erase-line pattern", () => {
    const input = "hello world\nplain text\nno escape sequences";
    expect(optimizeMultilineEraseLines(input)).toBe(input);
  });

  it("leaves a single eraseLine+cursorUp+terminator unchanged (no benefit)", () => {
    // 1 repeat → cursorUpCount = 0; the algorithm bails to avoid
    // adding overhead with no payoff.
    const input = `${ERASE_LINE}${UP_ONE}${ERASE_LINE}${LEFT}`;
    expect(optimizeMultilineEraseLines(input)).toBe(input);
  });

  it("folds 3-repeat pattern into a bounded-jump form", () => {
    // 3 eraseLine+cursorUp pairs (so cursorUpCount = 2 — folds).
    const input =
      `${ERASE_LINE}${UP_ONE}`.repeat(2) + `${ERASE_LINE}${LEFT}`;
    const out = optimizeMultilineEraseLines(input);
    // Bounded jump up 2 first, then 3 erase-lines walking down,
    // then jump back up 2, then column 1.
    const expected =
      `${ESC}[2A` +
      ERASE_LINE + DOWN_ONE +
      ERASE_LINE + DOWN_ONE +
      ERASE_LINE +
      `${ESC}[2A${LEFT}`;
    expect(out).toBe(expected);
  });

  it("folds 5-repeat pattern with cursorUpCount = 4", () => {
    const input =
      `${ERASE_LINE}${UP_ONE}`.repeat(4) + `${ERASE_LINE}${LEFT}`;
    const out = optimizeMultilineEraseLines(input);
    // Output starts with jump up 4, ends with jump up 4 + column 1.
    expect(out.startsWith(`${ESC}[4A`)).toBe(true);
    expect(out.endsWith(`${ESC}[4A${LEFT}`)).toBe(true);
    // 5 eraseLines + 4 cursorDown interleaved (one DOWN after each
    // eraseLine except the last).
    const middle = out.slice(`${ESC}[4A`.length, -`${ESC}[4A${LEFT}`.length);
    expect((middle.match(/\[2K/g) ?? []).length).toBe(5);
    expect((middle.match(/\[1B/g) ?? []).length).toBe(4);
  });

  it("folds multiple independent occurrences in one chunk", () => {
    const oneFolded =
      `${ERASE_LINE}${UP_ONE}`.repeat(2) + `${ERASE_LINE}${LEFT}`;
    const input = `prefix\n${oneFolded}middle\n${oneFolded}suffix`;
    const out = optimizeMultilineEraseLines(input);
    // Both occurrences fold → 2 bounded-jump openers.
    expect((out.match(/\[2A/g) ?? []).length).toBe(4); // open + close × 2
    expect(out.startsWith("prefix\n")).toBe(true);
    expect(out.endsWith("suffix")).toBe(true);
  });

  it("does not match adjacent-but-different sequences (e.g. cursor down)", () => {
    // Sequence with DOWN_ONE instead of UP_ONE — must NOT fold.
    const input =
      `${ERASE_LINE}${DOWN_ONE}${ERASE_LINE}${DOWN_ONE}${ERASE_LINE}${LEFT}`;
    expect(optimizeMultilineEraseLines(input)).toBe(input);
  });

  it("reduces single-step cursor moves to one bounded jump on a typical 8-line redraw", () => {
    // 8 lines → 7 cursorUp pairs + terminator. Real-world Ink redraws
    // commonly hit this scale during streaming. The byte count is
    // roughly neutral (each ``\x1b[1A`` swap becomes ``\x1b[1B`` plus
    // two ``\x1b[NA`` framing jumps), but the NUMBER of cursor-move
    // operations the terminal has to process drops from N-1 single
    // steps to 2 bounded jumps. That's what fixes scrollback bouncing.
    const input =
      `${ERASE_LINE}${UP_ONE}`.repeat(7) + `${ERASE_LINE}${LEFT}`;
    const out = optimizeMultilineEraseLines(input);
    // Before: 7 individual ``[1A`` cursor-up calls.
    expect((input.match(/\[1A/g) ?? []).length).toBe(7);
    // After: zero ``[1A`` calls — replaced by two ``[NA`` jumps.
    expect((out.match(/\[1A/g) ?? []).length).toBe(0);
    expect((out.match(/\[7A/g) ?? []).length).toBe(2);
  });
});
