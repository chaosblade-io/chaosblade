/**
 * Unit tests for ``tailWrappedLines`` — the soft-wrap helper that
 * powers the LoadingIndicator's rolling thinking viewport.
 */

import { describe, expect, it } from "vitest";
import { tailWrappedLines } from "./wrapText.js";

describe("tailWrappedLines", () => {
  it("returns an empty array for an empty buffer", () => {
    expect(tailWrappedLines("", 3, 40)).toEqual([]);
  });

  it("returns an empty array for non-positive maxLines / maxWidth", () => {
    expect(tailWrappedLines("hello", 0, 40)).toEqual([]);
    expect(tailWrappedLines("hello", 3, 0)).toEqual([]);
  });

  it("preserves explicit newlines as logical line breaks", () => {
    const out = tailWrappedLines("alpha\nbeta\ngamma", 5, 40);
    expect(out).toEqual(["alpha", "beta", "gamma"]);
  });

  it("soft-wraps a long single line into chunks of maxWidth", () => {
    const out = tailWrappedLines("abcdefghijklmnop", 10, 5);
    // 16 chars / 5 = 4 chunks: abcde, fghij, klmno, p
    expect(out).toEqual(["abcde", "fghij", "klmno", "p"]);
  });

  it("returns only the LAST maxLines visual rows", () => {
    const text = "L1\nL2\nL3\nL4\nL5";
    expect(tailWrappedLines(text, 3, 40)).toEqual(["L3", "L4", "L5"]);
  });

  it("treats blank lines as a single empty visual row", () => {
    const out = tailWrappedLines("alpha\n\nbeta", 5, 40);
    expect(out).toEqual(["alpha", "", "beta"]);
  });

  it("combines wrapping and tailing — long buffer of wrapped chunks", () => {
    // 30 chars wrapped at 10 = 3 chunks; ask for last 2.
    const out = tailWrappedLines("abcdefghijABCDEFGHIJ0123456789", 2, 10);
    expect(out).toEqual(["ABCDEFGHIJ", "0123456789"]);
  });
});
