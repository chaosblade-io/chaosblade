/**
 * Tests for the tool-output helpers — primarily ``fitLineWidth`` /
 * ``fitTextWidth`` which guarantee that no body row inside a
 * bordered ToolMessage exceeds the inner content area (preventing the
 * right border misalignment described at the top of toolOutput.ts).
 */

import { describe, expect, it } from "vitest";
import stringWidth from "string-width";
import {
  fitLineWidth,
  fitTextWidth,
  formatElapsed,
  truncateOutput,
} from "./toolOutput.js";

describe("fitLineWidth", () => {
  it("returns lines that already fit unchanged", () => {
    expect(fitLineWidth("hello", 10)).toBe("hello");
    expect(fitLineWidth("", 10)).toBe("");
  });

  it("truncates with a single ellipsis cell when the line overflows", () => {
    const out = fitLineWidth("abcdefghij", 5);
    expect(out).toBe("abcd…");
    expect(stringWidth(out)).toBe(5);
  });

  it("respects CJK/fullwidth chars (2 cells each)", () => {
    // "你好世界abc" → 2+2+2+2+1+1+1 = 11 cells
    const out = fitLineWidth("你好世界abc", 6);
    expect(stringWidth(out)).toBeLessThanOrEqual(6);
    // Should keep "你好" (4 cells) + "…" (1 cell) = 5 cells, within budget.
    expect(out).toBe("你好…");
  });

  it("returns an empty string for non-positive maxWidth", () => {
    expect(fitLineWidth("anything", 0)).toBe("");
    expect(fitLineWidth("anything", -3)).toBe("");
  });

  it("handles a single fullwidth char that won't fit", () => {
    // maxWidth=2, "你" is 2 cells. Fits exactly, no ellipsis needed.
    expect(fitLineWidth("你", 2)).toBe("你");
    // maxWidth=1, "你" is 2 cells. Doesn't fit → cap is 0 → empty + "…"
    // string-width("…") is 1, exactly the budget.
    const tight = fitLineWidth("你", 1);
    expect(stringWidth(tight)).toBeLessThanOrEqual(1);
  });

  it("preserves emoji as a single grapheme (no surrogate split)", () => {
    // "ab🎉cd" → 1+1+2+1+1 = 6 cells
    const out = fitLineWidth("ab🎉cd", 5);
    expect(stringWidth(out)).toBeLessThanOrEqual(5);
    // Must NOT contain a lone surrogate half — string-width on a
    // half surrogate returns NaN/0; rendering would be broken.
    // We don't assert exact content (algorithm could legitimately
    // stop before or after the emoji depending on width), just that
    // stringWidth still works on the result.
    expect(stringWidth(out)).toBeGreaterThan(0);
  });
});

describe("fitTextWidth", () => {
  it("applies fit per logical line (preserving newlines)", () => {
    const input = "short\nthisisaverylongline\nfine";
    const out = fitTextWidth(input, 7);
    const lines = out.split("\n");
    expect(lines).toHaveLength(3);
    expect(lines[0]).toBe("short");
    // Line 2 truncated — last cell should be the ellipsis.
    expect(lines[1]).toMatch(/…$/);
    expect(stringWidth(lines[1]!)).toBeLessThanOrEqual(7);
    expect(lines[2]).toBe("fine");
  });

  it("returns empty string unchanged", () => {
    expect(fitTextWidth("", 10)).toBe("");
  });

  it("preserves blank lines (empty visual row)", () => {
    const out = fitTextWidth("a\n\nb", 5);
    expect(out.split("\n")).toEqual(["a", "", "b"]);
  });
});

describe("truncateOutput", () => {
  // Pre-existing helper; sanity-check that the new tests don't
  // accidentally break it via the shared module.
  it("clamps to maxLines and reports hidden tail", () => {
    const r = truncateOutput("a\nb\nc\nd\ne\nf", 3);
    expect(r.body).toBe("a\nb\nc");
    expect(r.hiddenLines).toBe(3);
    expect(r.totalLines).toBe(6);
  });
});

describe("formatElapsed", () => {
  it("formats sub-second / sub-minute / multi-minute durations", () => {
    expect(formatElapsed(85)).toBe("85ms");
    expect(formatElapsed(1240)).toBe("1.2s");
    expect(formatElapsed(95_000)).toBe("1m35s");
    expect(formatElapsed(undefined)).toBe("");
    expect(formatElapsed(0)).toBe("");
  });
});
