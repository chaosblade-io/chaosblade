/**
 * Module-level refs for high-frequency streaming counters, kept OUTSIDE
 * React state so per-token updates don't trigger reconcile work.
 *
 * Why a separate file (vs piggy-back on AppState):
 *   - Per-token AppState updates would force ``useAppSelector`` callers
 *     to re-evaluate even when their slice didn't change — the whole
 *     point of Phase 1.1's selector store would be defeated by writing
 *     into state at 16 Hz. Module-level refs sidestep React entirely.
 *   - ``useAnimationFrame`` (Phase 2.2) polls these refs at a fixed
 *     interval and yields a smoothly-interpolated display value via
 *     ``useState``. The ref itself never causes a render; the rendered
 *     value comes from the polled state inside the hook.
 *   - Mirrors the established ``sessionStatsRef`` pattern used by the
 *     goodbye card (state/sessionStats.ts) — module-level mutable
 *     refs are the canonical "data Ink needs but React shouldn't
 *     re-render for" container in this codebase.
 *
 * Lifecycle:
 *   - ``streamingResponseCharsRef.current`` is incremented by
 *     ``useStream``'s token handler on every token event (~16 Hz under
 *     LLM streaming). Reset to 0 by ``useStream`` at TURN_STARTED time
 *     so each turn's counter starts fresh.
 *   - The displayed "token count" in LoadingIndicator is derived from
 *     this char count via ``chars / 4`` (the standard rough estimate;
 *     ~4 chars/token for English-heavy CoT content), animated through
 *     ``useAnimationFrame`` so the user sees a smooth 10 Hz climb
 *     rather than a 5 Hz step ladder driven by per-event setStates.
 *   - The authoritative server-sourced ``turnInputTokens +
 *     turnOutputTokens`` (from ``usage`` SSE events) still lives in
 *     AppState — it lands in the TurnUsageItem at TURN_DONE for the
 *     committed history row, so the after-the-fact summary is exact
 *     even though the live tail uses the estimate.
 */

/** Running cumulative character count of agent token deltas for the
 *  current turn. Reset on TURN_STARTED, written by useStream. Read by
 *  LoadingIndicator via useAnimationFrame for the live tokens display. */
export const streamingResponseCharsRef: { current: number } = { current: 0 };

/** Reset every per-turn streaming counter that lives outside React
 *  state. Call this from every dispatch site that begins a fresh
 *  visible-spinner turn (TURN_STARTED via useStream.submitTurn,
 *  REPLAY_STARTED via the /replay handler). Centralising the reset
 *  here means new turn-start paths only need one line to stay in
 *  sync — forgetting it produces a "tokens estimate from the prior
 *  turn lingers under the new spinner" visual bug.
 *
 *  Safe to call multiple times in succession (idempotent assignment).
 *  Does NOT touch any reducer state; ``useAnimationFrame`` snaps the
 *  displayed value down to 0 on its next interval tick (or in-render
 *  if the calling component happens to re-render in the same React
 *  cycle as the reset). */
export function resetStreamingCounters(): void {
  streamingResponseCharsRef.current = 0;
}
