/**
 * StatusBar render assertions for the cache hit-rate segment (task 1.12).
 *
 * The formatter (``formatCacheHitRate``) has its own unit test; these are
 * the COMPONENT-level guards — they prove the Web StatusBar actually wires
 * the per-turn cache counters from the shared core store into a rendered
 * ``⚡ N%`` segment beside the context gauge, and that the "always present
 * from boot" decision holds (a cold / no-LLM-call turn renders a dimmed
 * ``⚡ 0%`` rather than dropping the segment). Mirrors the TUI
 * Footer.test.tsx guards so both frontends are pinned to the same shape.
 *
 * Text probing (not snapshot): the Tailwind class palette + flex layout make
 * a DOM snapshot fragile, so we assert on the substrings that must appear —
 * the same convention the other web component tests use.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { StoreProvider, configureI18n } from "@blade-ai/core";
import { StatusBar } from "./StatusBar";

configureI18n("en");

afterEach(() => {
  cleanup();
});

function renderStatusBar(initial: {
  turnCachedTokens: number;
  turnInputTokens: number;
}): string {
  const { container } = render(
    <StoreProvider initial={initial}>
      <StatusBar />
    </StoreProvider>,
  );
  return container.textContent ?? "";
}

describe("StatusBar / cache hit-rate segment", () => {
  it("renders the hit rate for a warm turn (cached ⊆ input)", () => {
    // 2176 / 2990 ≈ 72.8% → rounds to 73%. The real DashScope shape from
    // the task-1.1 probe (input constant 2990, cache_read 0→2176).
    const text = renderStatusBar({ turnCachedTokens: 2176, turnInputTokens: 2990 });
    expect(text).toContain("⚡ 73%");
  });

  it("renders a dimmed ⚡ 0% at boot (no input tokens yet), never hidden", () => {
    // The product decision: the slot is present from boot so the user never
    // wonders whether the metric exists. A cold 0% is dimmed (class), not an
    // error colour — but the TEXT must still render.
    const text = renderStatusBar({ turnCachedTokens: 0, turnInputTokens: 0 });
    expect(text).toContain("⚡ 0%");
  });

  it("renders ⚡ 0% for a warm turn that hit nothing", () => {
    // Distinguish "cold / no data" from "ran but zero hits": both show 0%,
    // differing only in the class — here we pin the text.
    const text = renderStatusBar({ turnCachedTokens: 0, turnInputTokens: 2990 });
    expect(text).toContain("⚡ 0%");
  });

  it("renders a full hit (cached == input) as ⚡ 100%", () => {
    const text = renderStatusBar({ turnCachedTokens: 2990, turnInputTokens: 2990 });
    expect(text).toContain("⚡ 100%");
  });
});
