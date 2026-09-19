/**
 * Footer render assertions for the cache hit-rate segment (task 1.12).
 *
 * The formatter (``formatCacheHitRate``) has its own unit test; these are
 * the COMPONENT-level guards — they prove the Footer actually wires the
 * per-turn cache counters from the core store into a rendered ``⚡ N%``
 * segment beside the context gauge, and that the "always present from
 * boot" decision holds (a cold / no-LLM-call turn renders a dimmed
 * ``⚡ 0%`` rather than dropping the segment).
 *
 * Frame probing (not exact-match): Ink's flex layout + padding + ANSI make
 * a full-frame snapshot fragile across versions, so we assert on the
 * substrings that must appear — the same convention WelcomeCard /
 * PendingTasksCard tests use.
 */

import { render } from "ink-testing-library";
import { afterEach, describe, expect, it } from "vitest";
import { StoreProvider } from "@blade-ai/core";
import { Footer } from "./Footer.js";

const ORIGINAL_COLS = process.stdout.columns;

afterEach(() => {
  Object.defineProperty(process.stdout, "columns", {
    value: ORIGINAL_COLS,
    configurable: true,
    writable: true,
  });
});

function setCols(n: number): void {
  Object.defineProperty(process.stdout, "columns", {
    value: n,
    configurable: true,
    writable: true,
  });
}

function renderFooter(initial: {
  turnCachedTokens: number;
  turnInputTokens: number;
}): string {
  setCols(120);
  const { lastFrame } = render(
    <StoreProvider initial={initial}>
      <Footer />
    </StoreProvider>,
  );
  return lastFrame() ?? "";
}

describe("Footer / cache hit-rate segment", () => {
  it("renders the hit rate for a warm turn (cached ⊆ input)", () => {
    // 2176 / 2990 ≈ 72.8% → rounds to 73%. The real DashScope shape from
    // the task-1.1 probe (input constant 2990, cache_read 0→2176).
    const frame = renderFooter({ turnCachedTokens: 2176, turnInputTokens: 2990 });
    expect(frame).toContain("⚡ 73%");
  });

  it("renders a dimmed ⚡ 0% at boot (no input tokens yet), never hidden", () => {
    // The product decision: the slot is present from boot so the user never
    // wonders whether the metric exists. A cold 0% is dimmed (severity
    // "none"), not an error colour — but the TEXT must still render.
    const frame = renderFooter({ turnCachedTokens: 0, turnInputTokens: 0 });
    expect(frame).toContain("⚡ 0%");
  });

  it("renders ⚡ 0% for a warm turn that hit nothing", () => {
    // Distinguish "cold / no data" from "ran but zero hits": both show 0%,
    // the former dimmed (severity none) the latter not — here we only pin
    // the text, the colour split is the formatter's own unit concern.
    const frame = renderFooter({ turnCachedTokens: 0, turnInputTokens: 2990 });
    expect(frame).toContain("⚡ 0%");
  });

  it("renders a full hit (cached == input) as ⚡ 100%", () => {
    const frame = renderFooter({ turnCachedTokens: 2990, turnInputTokens: 2990 });
    expect(frame).toContain("⚡ 100%");
  });
});
