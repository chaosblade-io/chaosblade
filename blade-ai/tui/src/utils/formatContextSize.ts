/** Server-side default for ``settings.context_max_tokens`` — used
 *  as the baked-in window size before any ``context_size`` event has
 *  landed so the Footer always renders proper numbers at boot
 *  instead of a placeholder. The real value (whatever the operator
 *  set in ``BLADE_AI_CONTEXT_MAX_TOKENS``) overrides this as soon
 *  as the first hook fires. */
export const DEFAULT_CONTEXT_MAX_TOKENS = 128_000;

/**
 * Render the Footer "state size / window" indicator. ALWAYS returns
 * a string — never null — so the Footer never has to fall back to a
 * different display mode. When ``maxTokens`` is missing / invalid we
 * substitute ``DEFAULT_CONTEXT_MAX_TOKENS`` so the user still sees
 * meaningful numbers during the boot window (between TUI start and
 * the first hook fire).
 *
 * Format: ``{current}k / {max}k ({tail})`` with 1-decimal precision
 * on current and percentage. ``tail`` is either the percent string
 * (``"9.6%"``) or the literal ``"error"`` when the caller passes
 * ``options.error = true``. Examples:
 *
 *   0.0k / 128k (0.0%)         ← boot, no data yet
 *   12.3k / 128k (9.6%)        ← steady state
 *   135.7k / 128k (106.0%)     ← over-window, honest not clamped
 *   12.3k / 128k (error)       ← error mode — current/max preserved,
 *                                 percent replaced by literal "error"
 *                                 to signal "something's wrong" while
 *                                 still showing whatever we last knew
 */
export function formatContextSize(
  currentTokens: number,
  maxTokens: number,
  options?: { error?: boolean },
): string {
  const safeMax =
    !maxTokens || maxTokens <= 0 ? DEFAULT_CONTEXT_MAX_TOKENS : maxTokens;
  const curK = currentTokens / 1000;
  const maxK = safeMax / 1000;
  const tail = options?.error
    ? "error"
    : `${((currentTokens / safeMax) * 100).toFixed(1)}%`;
  return `${curK.toFixed(1)}k / ${Math.round(maxK)}k (${tail})`;
}

/**
 * Color bucket for the indicator. Two thresholds:
 *
 *   < 70%  → ``"normal"``  (Theme.text.secondary — gray)
 *   70-99% → ``"warn"``    (Theme.status.warn — yellow)
 *   ≥ 100% → ``"err"``     (Theme.status.err — red)
 *
 * 70% is the single "approaching trigger" line; 100% is a separate
 * red because that's "over the configured window" — transient (the
 * next hook call will compact) but worth visually flagging.
 *
 * Returns "normal" when maxTokens is 0 / negative so the caller
 * doesn't have to guard against the no-data case separately.
 */
export type ContextSizeSeverity = "normal" | "warn" | "err";

export function contextSizeSeverity(
  currentTokens: number,
  maxTokens: number,
  options?: { error?: boolean },
): ContextSizeSeverity {
  // Explicit error mode always renders red — overrides percent-based
  // thresholds. The error label needs the warn/err treatment to
  // visually stand out from a normal "(N.N%)" tail.
  if (options?.error) return "err";
  const safeMax =
    !maxTokens || maxTokens <= 0 ? DEFAULT_CONTEXT_MAX_TOKENS : maxTokens;
  const pct = (currentTokens / safeMax) * 100;
  if (pct >= 100) return "err";
  if (pct >= 70) return "warn";
  return "normal";
}
