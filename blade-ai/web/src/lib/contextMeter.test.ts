import { describe, expect, it } from "vitest";
import {
  cacheHitRateClass,
  contextSizeClass,
  DEFAULT_CONTEXT_MAX_TOKENS,
  formatCacheHitRate,
  formatContextSize,
} from "./contextMeter";

describe("lib / contextMeter · formatContextSize", () => {
  it("mirrors the TUI window gauge (default fallback + honest >100%)", () => {
    expect(DEFAULT_CONTEXT_MAX_TOKENS).toBe(128_000);
    expect(formatContextSize(0, 0)).toBe("0.0k / 128k (0.0%)");
    expect(formatContextSize(12_300, 128_000)).toBe("12.3k / 128k (9.6%)");
    expect(formatContextSize(135_700, 128_000)).toBe("135.7k / 128k (106.0%)");
  });

  it("window-pressure palette: faint < 70%, warning 70-99%, danger ≥100%", () => {
    expect(contextSizeClass(12_300, 128_000)).toBe("text-forge-text-faint");
    expect(contextSizeClass(95_000, 128_000)).toBe("text-warning");
    expect(contextSizeClass(135_700, 128_000)).toBe("text-danger");
  });
});

describe("lib / contextMeter · cache hit rate (REVERSED palette)", () => {
  it("dimmed ⚡ 0% when no input tokens; integer percent otherwise", () => {
    expect(formatCacheHitRate(0, 0)).toBe("⚡ 0%");
    expect(formatCacheHitRate(2176, 2990)).toBe("⚡ 73%");
    expect(formatCacheHitRate(0, 2990)).toBe("⚡ 0%");
    expect(formatCacheHitRate(4000, 2990)).toBe("⚡ 100%");
  });

  it("high hit-rate is GOOD (green) — opposite of the window gauge", () => {
    expect(cacheHitRateClass(2176, 2990)).toBe("text-success");
  });

  it("partial hit is neutral gray; cold 0% is dimmed gray, never red", () => {
    expect(cacheHitRateClass(300, 3000)).toBe("text-forge-text-faint");
    expect(cacheHitRateClass(0, 2990)).toBe(
      "text-forge-text-faint opacity-60",
    );
    expect(cacheHitRateClass(0, 0)).toBe("text-forge-text-faint opacity-60");
  });
});
