/**
 * Helpers for rendering tool-call result bodies inside ToolMessage.
 *
 * Tool-agnostic. A "tool" here is anything the agent invokes through
 * LangGraph's ToolNode: built-in tools, skill helpers, MCP tools,
 * future custom tools. The raw output is treated as opaque text —
 * no per-tool parser, no format detection. It can range from an
 * empty string all the way to multi-megabyte JSON / log / table
 * dumps, and the TUI never wants to paint all of that: a wrapped
 * thousand-line block in the dynamic render area is slow to redraw
 * and pushes everything else off-screen.
 *
 * Goal: surface enough lines to confirm the tool actually did
 * something (head sample), tell the user how much more is hidden,
 * and pin the slow path behind a deterministic line cap.
 *
 * The width-cap helper (``fitLineWidth``) exists because Ink's
 * native wrap (which goes through ``wrap-ansi`` and inserts hard
 * ``\n`` characters into the rendered grid — see ink#883) misaligns
 * borders on bordered Boxes when content rows are longer than the
 * inner content area. Pre-fitting each line to one visual row
 * sidesteps the issue entirely: every row has both ``│`` borders at
 * fixed columns regardless of terminal width or content shape.
 */

import stringWidth from "string-width";

export interface TruncatedOutput {
  /** Lines kept for display, ``\n``-joined. Already stripped of
   *  trailing whitespace per line and trailing blank lines so the
   *  card doesn't grow with empty rows. */
  body: string;
  /** Number of lines hidden from the tail. ``0`` when the output
   *  fit under the cap. The renderer surfaces this as a dim
   *  ``… +N more lines`` footer. */
  hiddenLines: number;
  /** Line count of the input *after* normalisation (ANSI strip +
   *  per-line rstrip + trailing-blank drop) but *before* the
   *  ``maxLines`` cap. Equals ``hiddenLines + visible-row count``;
   *  callers can use it for tooltip text or invariant checks. */
  totalLines: number;
}

/**
 * Trim, normalise, and truncate raw tool output for in-card display.
 *
 * Rules:
 *   - Empty / whitespace-only input → ``{ body: "", hiddenLines: 0,
 *     totalLines: 0 }`` so the renderer can hide the body block
 *     entirely (no ghost trailing branch).
 *   - Strip ANSI control sequences (some CLIs prefix output with
 *     cursor-clear codes; spinners inject erase-line codes).
 *   - Strip trailing whitespace per line so verbose output doesn't
 *     add visible garbage.
 *   - Drop trailing blank lines from the end of the buffer.
 *   - Keep the first ``maxLines`` rows; report the rest as hidden.
 *
 * Why head-only (not head + tail): tail snippets sound clever but
 * confuse readers — a tabular output truncated as ``[first 3] …
 * +N … [last 2]`` flips the visual semantics of "row N of the body
 * = row N of the output" mid-block. Head-only keeps the contract
 * that line 1 of the body is line 1 of the output, regardless of
 * what the tool emitted.
 */
export function truncateOutput(
  raw: string | undefined | null,
  maxLines = 5,
): TruncatedOutput {
  if (!raw) {
    return { body: "", hiddenLines: 0, totalLines: 0 };
  }
  // Drop ANSI control sequences a tool might inject (Cursor up /
  // erase line — common in spinners). Conservative: only the
  // ``ESC[…m``  / ``ESC[…K`` / ``ESC[…A`` shapes; anything else
  // is left for downstream Text wrap to handle.
  // eslint-disable-next-line no-control-regex
  const cleaned = raw.replace(/\x1b\[[0-9;]*[a-zA-Z]/g, "");

  // Per-line normalisation: rstrip + drop fully-empty trailing rows.
  const lines = cleaned.split("\n").map((l) => l.replace(/\s+$/, ""));
  while (lines.length > 0 && lines[lines.length - 1] === "") {
    lines.pop();
  }
  if (lines.length === 0) {
    return { body: "", hiddenLines: 0, totalLines: 0 };
  }

  const cap = Math.max(1, Math.floor(maxLines));
  if (lines.length <= cap) {
    return { body: lines.join("\n"), hiddenLines: 0, totalLines: lines.length };
  }
  return {
    body: lines.slice(0, cap).join("\n"),
    hiddenLines: lines.length - cap,
    totalLines: lines.length,
  };
}

/**
 * Truncate a single line to fit within ``maxWidth`` visual cells.
 *
 * CJK / fullwidth aware via ``string-width``. Replaces overflow with a
 * single ``…`` ellipsis (one cell wide) so the visible row is exactly
 * ``maxWidth`` cells or less — never more. Lines that already fit are
 * returned unchanged.
 *
 * Why we need this even though Ink supports ``wrap="truncate-end"``:
 * the per-Text truncate prop only acts when Ink's layout engine
 * decides the content overflows its allotted column count. Inside a
 * bordered Box with ``paddingX`` and a flex-grown text container, the
 * column accounting can drift (Yoga + wrap-ansi disagree on the
 * effective width by a few cells when there's a sibling icon in the
 * row), letting a "just-overflowing" line slip through into the
 * wrap branch and then break the right border. Pre-fitting in JS
 * makes the cap deterministic.
 *
 *   maxWidth ≤ 0 → empty string
 *   "abc"        @ 5 → "abc"
 *   "abcdef"     @ 5 → "abcd…"
 *   "你好世界abc" @ 6 → "你好世…"  (string-width counts CJK as 2)
 */
export function fitLineWidth(line: string, maxWidth: number): string {
  if (maxWidth <= 0) return "";
  if (stringWidth(line) <= maxWidth) return line;
  // Reserve one cell for the trailing ellipsis. Walk one code point
  // at a time using the iterator (so surrogate pairs / emoji stay
  // intact) and accumulate by visual width.
  const cap = maxWidth - 1;
  let acc = "";
  let accWidth = 0;
  for (const ch of line) {
    const w = stringWidth(ch);
    if (accWidth + w > cap) break;
    acc += ch;
    accWidth += w;
  }
  return acc + "…";
}

/**
 * Apply ``fitLineWidth`` per line, preserving newlines. Used by
 * ToolMessage to pre-fit a multi-line body before handing it to Ink
 * — every output row is guaranteed to occupy exactly one visual row
 * inside the bordered card, so the borders stay aligned even when
 * the underlying tool emits 200-column-wide table headers.
 */
export function fitTextWidth(text: string, maxWidth: number): string {
  return text
    .split("\n")
    .map((line) => fitLineWidth(line, maxWidth))
    .join("\n");
}

/**
 * Format an elapsed-time hint shown on the tool card title row.
 *
 *   85         → ``"85ms"``
 *   1_240      → ``"1.2s"``
 *   95_000     → ``"1m35s"``
 *   undefined  → ``""``
 */
export function formatElapsed(ms: number | undefined): string {
  if (!ms || ms <= 0) return "";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return seconds > 0 ? `${minutes}m${seconds}s` : `${minutes}m`;
}
