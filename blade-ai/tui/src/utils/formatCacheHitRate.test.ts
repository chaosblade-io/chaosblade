import { describe, expect, it } from "vitest";
import {
  cacheHitRateSeverity,
  formatCacheHitRate,
} from "./formatCacheHitRate.js";

describe("utils / formatCacheHitRate", () => {
  it("renders a dimmed ⚡ 0% when there are no input tokens (boot / no LLM call)", () => {
    // The slot is present from boot (never hidden); a cold / no-data turn
    // shows 0% with severity "none" (dimmed gray), not an error colour.
    expect(formatCacheHitRate(0, 0)).toBe("⚡ 0%");
    expect(formatCacheHitRate(100, 0)).toBe("⚡ 0%");
    expect(formatCacheHitRate(100, -1)).toBe("⚡ 0%");
  });

  it("renders integer percent of cached / input (real-run figures)", () => {
    // DashScope qwen3.8-max: cache_read=2176, input=2990 → ≈72.8% → 73%.
    expect(formatCacheHitRate(2176, 2990)).toBe("⚡ 73%");
    expect(formatCacheHitRate(0, 2990)).toBe("⚡ 0%");
    expect(formatCacheHitRate(2990, 2990)).toBe("⚡ 100%");
  });

  it("clamps to 100% if a provider ever reports cached > input", () => {
    expect(formatCacheHitRate(4000, 2990)).toBe("⚡ 100%");
  });
});

describe("utils / cacheHitRateSeverity (REVERSED palette)", () => {
  it("good at ≥50% — high hit-rate is the desired state", () => {
    expect(cacheHitRateSeverity(2176, 2990)).toBe("good");
    expect(cacheHitRateSeverity(1500, 3000)).toBe("good");
    expect(cacheHitRateSeverity(3000, 3000)).toBe("good");
  });

  it("low for a partial hit (1-49%)", () => {
    expect(cacheHitRateSeverity(300, 3000)).toBe("low");
    expect(cacheHitRateSeverity(1, 3000)).toBe("low");
  });

  it("none for a cold 0% — NOT an error, just no reuse yet", () => {
    // Reversed vs the window gauge: 0% here must never read red.
    expect(cacheHitRateSeverity(0, 2990)).toBe("none");
  });

  it("none when there are no input tokens", () => {
    expect(cacheHitRateSeverity(0, 0)).toBe("none");
    expect(cacheHitRateSeverity(100, 0)).toBe("none");
  });
});
