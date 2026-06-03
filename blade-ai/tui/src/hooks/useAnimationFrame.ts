/**
 * Smoothly interpolate a numeric value displayed in the UI.
 *
 * Why this exists. Streaming token counters jump in big steps because
 * LLMs emit content in irregular bursts (sometimes 2 chars, sometimes
 * 200). A naive ``Text>{realCount}</Text>`` would tear visually. We
 * tween the displayed value toward the true value at fixed intervals
 * so the eye sees a smooth crawl.
 *
 * Rules (1:1 ported from Qwen Code's useAnimationFrame):
 *   gap < 70:    +3 / frame
 *   gap 70–200:  +20% gap / frame
 *   gap > 200:   +50 / frame
 *
 * Snap-down on reset (real value drops below displayed) — when a new
 * turn starts and the counter resets to 0 we want immediate response,
 * not a backwards crawl.
 *
 * Polling source is a ref so the producer (TOKEN_APPENDED dispatch
 * path) doesn't have to trigger React re-renders per token. Only this
 * hook reconciles, at the chosen interval.
 */

import { useEffect, useRef, useState } from "react";

export function useAnimationFrame(
  watchRef: React.RefObject<number>,
  intervalMs: number | null = 100,
): number {
  const initial = watchRef.current ?? 0;
  const [display, setDisplay] = useState<number>(initial);
  const cur = useRef<number>(initial);

  // Synchronous snap-down: if the source value already dropped below
  // our last displayed value (e.g. the consumer cleared it before the
  // next interval tick fires), reflect that in this render directly.
  const observed = watchRef.current ?? 0;
  if (observed < cur.current) {
    cur.current = observed;
  }

  useEffect(() => {
    if (intervalMs === null) return;

    const id = setInterval(() => {
      const real = watchRef.current ?? 0;
      if (real < cur.current) {
        cur.current = real;
        setDisplay(real);
        return;
      }
      const gap = real - cur.current;
      if (gap <= 0) return;

      const inc =
        gap < 70
          ? 3
          : gap <= 200
            ? Math.max(3, Math.round(gap * 0.2))
            : 50;
      cur.current = Math.min(cur.current + inc, real);
      setDisplay(cur.current);
    }, intervalMs);

    return () => clearInterval(id);
  }, [watchRef, intervalMs]);

  // Return the lower of state vs current observation so that a freshly
  // reset ref reflects in this render even before the next setInterval
  // tick lands a state update.
  return Math.min(display, observed);
}
