import { describe, expect, it } from "vitest";
import {
  contextSizeSeverity,
  DEFAULT_CONTEXT_MAX_TOKENS,
  formatContextSize,
} from "./formatContextSize.js";

describe("utils / formatContextSize", () => {
  it("substitutes the default window when maxTokens is missing", () => {
    // ALWAYS returns a string now — caller never has to fall back to
    // some other display mode. The substituted default is 128k.
    expect(formatContextSize(0, 0)).toBe("0.0k / 128k (0.0%)");
    expect(formatContextSize(1000, 0)).toBe("1.0k / 128k (0.8%)");
    expect(formatContextSize(1000, -1)).toBe("1.0k / 128k (0.8%)");
    // Sanity: the constant itself is 128_000.
    expect(DEFAULT_CONTEXT_MAX_TOKENS).toBe(128_000);
  });

  it("1-decimal current + integer max + 1-decimal percent", () => {
    expect(formatContextSize(500, 128_000)).toBe("0.5k / 128k (0.4%)");
    expect(formatContextSize(12_300, 128_000)).toBe("12.3k / 128k (9.6%)");
    expect(formatContextSize(95_000, 128_000)).toBe("95.0k / 128k (74.2%)");
  });

  it("over 100% renders honestly without clamp", () => {
    expect(formatContextSize(135_700, 128_000)).toBe(
      "135.7k / 128k (106.0%)",
    );
  });

  it("handles non-default window sizes", () => {
    expect(formatContextSize(80_000, 200_000)).toBe("80.0k / 200k (40.0%)");
    expect(formatContextSize(800_000, 1_000_000)).toBe(
      "800.0k / 1000k (80.0%)",
    );
  });

  it("error mode replaces percent tail with literal (error)", () => {
    // Numbers preserved so the user still sees what we last knew;
    // only the percent is swapped for the error signal.
    expect(formatContextSize(12_300, 128_000, { error: true })).toBe(
      "12.3k / 128k (error)",
    );
    // Even at boot (no data) error mode renders cleanly.
    expect(formatContextSize(0, 0, { error: true })).toBe(
      "0.0k / 128k (error)",
    );
  });
});

describe("utils / contextSizeSeverity", () => {
  it("normal below 70%", () => {
    expect(contextSizeSeverity(0, 128_000)).toBe("normal");
    expect(contextSizeSeverity(50_000, 128_000)).toBe("normal");
    expect(contextSizeSeverity(89_500, 128_000)).toBe("normal");
  });

  it("warn at 70% inclusive", () => {
    expect(contextSizeSeverity(89_600, 128_000)).toBe("warn");
    expect(contextSizeSeverity(100_000, 128_000)).toBe("warn");
    expect(contextSizeSeverity(127_999, 128_000)).toBe("warn");
  });

  it("err at 100% inclusive", () => {
    expect(contextSizeSeverity(128_000, 128_000)).toBe("err");
    expect(contextSizeSeverity(135_700, 128_000)).toBe("err");
    expect(contextSizeSeverity(300_000, 128_000)).toBe("err");
  });

  it("uses default window when max is missing (matches formatContextSize)", () => {
    // Pre-change this returned "normal" — now it substitutes the
    // default window so severity is calculated honestly even at
    // boot. With current=0 and substituted max=128k, severity is
    // still normal here, but the contract is "use the same denominator
    // formatter uses" so the colour never disagrees with the percent.
    expect(contextSizeSeverity(0, 0)).toBe("normal");
    expect(contextSizeSeverity(100_000, 0)).toBe("warn");
  });

  it("error mode always renders red regardless of percent", () => {
    // Even a sub-70% reading goes red when error is signaled — the
    // colour reinforces the "(error)" tail so the user can't miss
    // the stale-data signal.
    expect(contextSizeSeverity(0, 128_000, { error: true })).toBe("err");
    expect(contextSizeSeverity(50_000, 128_000, { error: true })).toBe("err");
  });
});
