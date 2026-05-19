/**
 * Soft-wrap a string to a fixed width and return the *last* N visual
 * lines. Used by ``LoadingIndicator`` to render a rolling viewport of
 * the live thinking buffer (last 3 lines visible while streaming).
 *
 * Why we don't lean on Ink's built-in ``wrap``: Ink wraps text at
 * render time but doesn't expose how many visual rows the wrap
 * produced, so we'd have no way to bound the body to "last 3" without
 * also lying about the height. Pre-wrapping here lets us show a
 * fixed-height block whose top scrolls off as new content arrives.
 *
 * Approximation: width is measured in code units, not visual cells.
 * CJK / fullwidth characters take two cells but count as one here, so
 * a buffer of pure CJK can soft-wrap to 2× the requested width on
 * screen. Acceptable for a transient streaming view (the user only
 * sees these lines for a few seconds before they collapse) and
 * cheaper than pulling in ``string-width``.
 */

export function tailWrappedLines(
  text: string,
  maxLines: number,
  maxWidth: number,
): string[] {
  if (maxLines <= 0 || maxWidth < 1 || text.length === 0) return [];
  const logical = text.split("\n");
  const wrapped: string[] = [];
  for (const line of logical) {
    if (line.length === 0) {
      wrapped.push("");
      continue;
    }
    let i = 0;
    while (i < line.length) {
      wrapped.push(line.slice(i, i + maxWidth));
      i += maxWidth;
    }
  }
  return wrapped.slice(-maxLines);
}
