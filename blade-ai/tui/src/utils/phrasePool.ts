/**
 * Phrase pool helpers shared by the reducer-driven phrase cycler.
 *
 * The pool resolves through i18n first (so users see localised
 * phrases) and falls back to the hard-coded English seeds in
 * ``theme/colors.ts`` if the dictionary is empty for any reason.
 *
 * Lives in ``utils/`` (not in a hook) because the reducer-driven
 * cycler dispatches ``PHRASE_TICK`` from a plain ``setInterval`` —
 * not from a React render — and the reducer pre-bakes the chosen
 * phrase into the action payload so the reducer itself stays pure.
 */

import { tArr } from "../i18n/index.js";
import { ThinkingPhrases } from "../theme/colors.js";

export function getPool(): readonly string[] {
  const fromI18n = tArr("thinking.phrases");
  return fromI18n.length > 0 ? fromI18n : ThinkingPhrases;
}

export function pickRandom(pool: readonly string[]): string {
  if (pool.length === 0) return "thinking";
  const idx = Math.floor(Math.random() * pool.length);
  return pool[idx] ?? pool[0] ?? "thinking";
}

/** Pick a phrase that is not equal to ``avoid`` if the pool is large
 *  enough (≥2 entries). Used by the cycler so two consecutive ticks
 *  don't show the same word — the visual feedback signal is the
 *  *change*, not the words themselves. */
export function pickRandomDistinct(
  pool: readonly string[],
  avoid: string,
): string {
  if (pool.length === 0) return "thinking";
  if (pool.length === 1) return pool[0] ?? "thinking";
  for (let i = 0; i < 8; i++) {
    const candidate = pickRandom(pool);
    if (candidate !== avoid) return candidate;
  }
  // Pathological branch — pool is degenerate; just return whatever.
  return pickRandom(pool);
}
