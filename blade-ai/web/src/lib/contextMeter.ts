/**
 * Web-side context-window + cache-hit-rate formatters for the StatusBar.
 *
 * Mirrors the TUI's ``formatContextSize`` / ``formatCacheHitRate``
 * semantics (``tui/src/utils``). Both frontends read the SAME shared store
 * fields via ``@blade-ai/core`` (``contextCurrentTokens`` /
 * ``contextMaxTokens`` / ``turnCachedTokens`` / ``turnInputTokens``) but
 * render through different colour systems — Tailwind classes here, Ink
 * Theme there — so the severity→class mapping is web-local by design.
 *
 * Cache hit rate = ``cached / input`` where ``cached`` (prompt-cache hits)
 * is a SUBSET of ``input`` — never additive — so the rate is always 0–100%.
 */

/** Mirror of the Python/TUI global fallback window, used until the model's
 *  real ``context_max_tokens`` arrives from the server (boot window). */
export const DEFAULT_CONTEXT_MAX_TOKENS = 128_000;

/** ``{cur}k / {max}k ({pct}%)`` — 1-decimal precision, honest (not clamped)
 *  so an over-window state reads >100%. */
export function formatContextSize(current: number, max: number): string {
  const safeMax = !max || max <= 0 ? DEFAULT_CONTEXT_MAX_TOKENS : max;
  const curK = current / 1000;
  const maxK = safeMax / 1000;
  const pct = ((current / safeMax) * 100).toFixed(1);
  return `${curK.toFixed(1)}k / ${Math.round(maxK)}k (${pct}%)`;
}

/** Window-pressure palette (same thresholds as the TUI): <70% faint,
 *  70–99% warning, ≥100% danger. */
export function contextSizeClass(current: number, max: number): string {
  const safeMax = !max || max <= 0 ? DEFAULT_CONTEXT_MAX_TOKENS : max;
  const pct = (current / safeMax) * 100;
  if (pct >= 100) return "text-danger";
  if (pct >= 70) return "text-warning";
  return "text-forge-text-faint";
}

/** ``⚡ N%``; with no input tokens yet this turn (boot window, or a turn
 *  with no LLM call) renders a dimmed ``⚡ 0%`` rather than hiding. */
export function formatCacheHitRate(
  cached: number,
  input: number,
): string {
  if (!input || input <= 0) return "⚡ 0%";
  const rate = Math.max(0, Math.min(1, cached / input));
  return `⚡ ${Math.round(rate * 100)}%`;
}

/** REVERSED palette vs the window gauge: high hit-rate is GOOD (green),
 *  partial is neutral gray, a cold 0% is dimmed gray — never red, since a
 *  cold prefix is expected on the first call, not an error. */
export function cacheHitRateClass(cached: number, input: number): string {
  if (!input || input <= 0) return "text-forge-text-faint opacity-60";
  const rate = cached / input;
  if (rate >= 0.5) return "text-success";
  if (rate > 0) return "text-forge-text-faint";
  return "text-forge-text-faint opacity-60";
}
