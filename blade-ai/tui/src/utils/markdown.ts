/**
 * Render markdown text into ANSI-styled text Ink can display.
 *
 * Pipeline: marked → marked-terminal renderer → ANSI string. Ink's
 * `<Text>` component preserves ANSI escape sequences when present in
 * children, so we pass the rendered string straight through.
 *
 * Caveat: do NOT set ``color`` on the wrapping Text — the inline ANSI
 * escapes carry their own color and Ink would then double-apply on
 * top, producing visible artifacts on some terminals. Let the rendered
 * string own its own colors.
 *
 * Mid-stream behavior: when the agent is still emitting tokens, the
 * input text often ends with a half-formed paragraph or unclosed
 * fence. ``marked`` is permissive and renders partial input as if it
 * were complete; the output flickers slightly as new tokens land but
 * never goes blank.
 *
 * Width handling: ``marked-terminal`` is registered as a global
 * extension on ``marked``, but its width is fixed at registration.
 * We can't re-register on every render (state would clobber on the
 * shared instance), so we keep a tiny LRU of per-width ``Marked``
 * instances. AgentMessage passes the live terminal width; reflowText
 * inside marked-terminal handles paragraph re-flow accordingly.
 */

import { Marked } from "marked";
import { markedTerminal } from "marked-terminal";

const MAX_CACHE = 4;
const _cache = new Map<number, Marked>();

function getMarked(width: number): Marked {
  // Bucket widths so noisy resize events (column-by-column) don't
  // explode the cache. 4-col buckets is fine — marked-terminal's
  // reflow doesn't care about exact width past line-break decisions.
  const bucket = Math.max(20, Math.round(width / 4) * 4);
  const cached = _cache.get(bucket);
  if (cached) {
    // Real LRU: promote to most-recently-used by re-inserting at
    // the end of the Map's insertion order. Without this, a width
    // that's been used continuously can still get evicted because
    // ``_cache.keys().next().value`` returns the oldest *insertion*
    // (FIFO), not the oldest *access*.
    _cache.delete(bucket);
    _cache.set(bucket, cached);
    return cached;
  }

  const m = new Marked();
  m.use(
    markedTerminal({
      reflowText: true,
      width: bucket,
      showSectionPrefix: false,
      tab: 2,
    }) as Parameters<Marked["use"]>[0],
  );

  // Evict the least-recently-used (= oldest insertion among entries
  // that have NOT been promoted) before inserting.
  if (_cache.size >= MAX_CACHE) {
    const oldestKey = _cache.keys().next().value as number | undefined;
    if (oldestKey !== undefined) _cache.delete(oldestKey);
  }
  _cache.set(bucket, m);
  return m;
}

export function renderMarkdown(text: string, width = 80): string {
  if (!text) return "";
  try {
    const result = getMarked(width).parse(text, { async: false });
    if (typeof result !== "string") return text;
    // marked-terminal often appends a trailing newline; trim so our
    // Ink <Box marginTop> handles spacing instead.
    return result.replace(/\n+$/, "");
  } catch {
    return text;
  }
}
