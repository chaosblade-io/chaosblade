/**
 * Rotate a fallback "thinking phrase" while the agent has not yet
 * emitted a thoughtSubject of its own. 15-second cadence matches
 * Qwen Code (slow enough not to feel like the agent restarted, fast
 * enough not to feel stuck).
 */

import { useEffect, useState } from "react";
import { tArr } from "../i18n/index.js";
import { ThinkingPhrases } from "../theme/colors.js";

const CYCLE_MS = 15_000;

/**
 * Resolve the active phrase pool through the i18n dictionary first
 * (so users see localized phrases) and fall back to the hard-coded
 * English seeds in ``theme/colors.ts`` if the dictionary is empty
 * for some reason. Wrapped in a function so module load order
 * doesn't matter.
 */
function getPool(): readonly string[] {
  const fromI18n = tArr("thinking.phrases");
  return fromI18n.length > 0 ? fromI18n : ThinkingPhrases;
}

export function usePhraseCycler(active: boolean): string {
  const pool = getPool();
  const initial = pool[0] ?? "thinking";
  const [phrase, setPhrase] = useState<string>(initial);

  useEffect(() => {
    if (!active) {
      setPhrase(initial);
      return;
    }
    // Pick a random initial phrase so two adjacent turns don't show
    // the same word.
    setPhrase(pickRandom(pool));
    const id = setInterval(() => setPhrase(pickRandom(pool)), CYCLE_MS);
    return () => clearInterval(id);
  }, [active, initial, pool]);

  return phrase;
}

function pickRandom(pool: readonly string[]): string {
  if (pool.length === 0) return "thinking";
  const idx = Math.floor(Math.random() * pool.length);
  return pool[idx] ?? pool[0] ?? "thinking";
}
