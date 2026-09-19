/**
 * Footer cache hit-rate indicator, rendered beside the context-window
 * gauge (see ``Footer.tsx`` / ``formatContextSize.ts``).
 *
 * Cache hit rate = ``cached / input``, where ``cached`` (prompt-cache
 * hits, ``usage_metadata.input_token_details.cache_read``) is a SUBSET
 * of ``input`` — never additive — so the rate is always 0–100%. Both
 * figures come from the per-turn counters the reducer accumulates from
 * server ``usage`` events (``turnCachedTokens`` / ``turnInputTokens``).
 */

/**
 * Render the indicator. Always returns a string: with no input tokens yet
 * this turn (boot window, or a turn that made no LLM call) it renders a
 * dimmed ``⚡ 0%`` (severity ``none``) rather than hiding the segment, so
 * the slot is present from boot and the user never wonders whether the
 * metric exists. A cold 0% is dimmed gray, not red — expected, not error.
 *
 * Format: ``⚡ 73%`` (integer percent, clamped to 0–100).
 */
export function formatCacheHitRate(
  cachedTokens: number,
  inputTokens: number,
): string {
  if (!inputTokens || inputTokens <= 0) return "⚡ 0%";
  const rate = Math.max(0, Math.min(1, cachedTokens / inputTokens));
  return `⚡ ${Math.round(rate * 100)}%`;
}

export type CacheHitRateSeverity = "good" | "low" | "none";

/**
 * REVERSED colour semantics vs the context-window gauge. There, high is
 * BAD (approaching auto-compaction → red). Here, high is GOOD (the stable
 * prefix is being reused → green). A cold first call (0%) is NOT an error
 * — it renders neutral gray, since the prefix simply hasn't been seen
 * before and the next call should hit.
 *
 *   ≥ 50% → ``"good"`` (Theme.status.ok — green)      cache paying off
 *   1-49% → ``"low"``  (Theme.text.secondary — gray)   partial hit
 *   0%    → ``"none"`` (secondary + dimColor — dark gray) cold start
 */
export function cacheHitRateSeverity(
  cachedTokens: number,
  inputTokens: number,
): CacheHitRateSeverity {
  if (!inputTokens || inputTokens <= 0) return "none";
  const rate = cachedTokens / inputTokens;
  if (rate >= 0.5) return "good";
  if (rate > 0) return "low";
  return "none";
}
