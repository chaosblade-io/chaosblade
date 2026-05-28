/**
 * Lightweight markdown → block-token renderer.
 *
 * Postmortem prompt constrains the LLM to FOUR constructs:
 *   - `## heading` / `### subheading`
 *   - `- list item`
 *   - `**bold**`     (inline)
 *   - `` `code` ``   (inline)
 *
 * Anything else (tables, links, blockquotes, code fences) is rendered
 * as plain paragraph text — graceful degradation rather than throwing.
 *
 * Output: an array of block tokens. Each block contains inline spans.
 * PostmortemSection turns this into Ink elements with appropriate
 * styling per block / span kind.
 */

export type InlineSpan =
  | { kind: "text"; value: string }
  | { kind: "bold"; value: string }
  | { kind: "code"; value: string };

export type Block =
  | { kind: "heading"; level: 2 | 3; spans: InlineSpan[] }
  | { kind: "list"; items: InlineSpan[][] }
  | { kind: "paragraph"; spans: InlineSpan[] }
  | { kind: "blank" };

/**
 * Top-level entry. Splits the markdown into block tokens.
 *
 * Strategy: line-by-line scan. Consecutive `- ` lines collapse into a
 * single list block; consecutive non-empty paragraph lines collapse
 * into a single paragraph block; blank lines preserve as spacing.
 */
export function parseMarkdown(markdown: string): Block[] {
  const lines = markdown.split("\n");
  const blocks: Block[] = [];
  let listBuffer: InlineSpan[][] | null = null;
  let paraBuffer: string[] | null = null;

  const flushList = () => {
    if (listBuffer && listBuffer.length > 0) {
      blocks.push({ kind: "list", items: listBuffer });
    }
    listBuffer = null;
  };
  const flushPara = () => {
    if (paraBuffer && paraBuffer.length > 0) {
      blocks.push({
        kind: "paragraph",
        spans: parseInlines(paraBuffer.join(" ")),
      });
    }
    paraBuffer = null;
  };
  const flushAll = () => {
    flushList();
    flushPara();
  };

  for (const rawLine of lines) {
    const line = rawLine.replace(/\t/g, "  ");
    const trimmed = line.trim();

    // Blank line → close any open buffer and emit a blank spacer.
    if (!trimmed) {
      flushAll();
      // Avoid stacking multiple blanks in a row.
      const prev = blocks[blocks.length - 1];
      if (prev && prev.kind !== "blank") blocks.push({ kind: "blank" });
      continue;
    }

    // Heading: `## ...` or `### ...`. Anything `#### ...` or deeper
    // collapses to a level-3 heading (we don't render finer levels —
    // postmortem outline is shallow by design).
    //
    // Destructuring with defaults silences ``noUncheckedIndexedAccess``
    // — TS types ``RegExpExecArray`` indices as ``string | undefined``
    // even though a successful match with capture groups always
    // populates them. Defaults are the safe zero-cost narrowing.
    const headingMatch = /^(#{2,6})\s+(.*)$/.exec(trimmed);
    if (headingMatch) {
      flushAll();
      const [, hashes = "", headingText = ""] = headingMatch;
      const level = hashes.length === 2 ? 2 : 3;
      blocks.push({
        kind: "heading",
        level: level as 2 | 3,
        spans: parseInlines(headingText),
      });
      continue;
    }

    // List item: `- ...` or `* ...`. We treat both as bullet lists.
    const listMatch = /^[-*]\s+(.+)$/.exec(trimmed);
    if (listMatch) {
      flushPara();
      if (listBuffer === null) listBuffer = [];
      const [, itemText = ""] = listMatch;
      listBuffer.push(parseInlines(itemText));
      continue;
    }

    // Anything else → paragraph text. Multiple non-empty lines in a
    // row collapse into one paragraph (markdown convention).
    flushList();
    if (paraBuffer === null) paraBuffer = [];
    paraBuffer.push(trimmed);
  }
  flushAll();

  return blocks;
}

/**
 * Inline-span tokeniser. Splits a line into {text|bold|code} spans.
 *
 * `**bold**` and `` `code` `` are matched non-greedily; nesting is not
 * supported (postmortem prompt forbids it). Anything that fails to
 * match cleanly is treated as plain text — never throws.
 */
export function parseInlines(text: string): InlineSpan[] {
  const spans: InlineSpan[] = [];
  // Pattern alternation: `**.*?**` (bold) or `` `.*?` `` (code). Anything
  // outside these matches is plain text. We iterate via a single regex
  // with sticky / global flag.
  const pattern = /\*\*([^*]+?)\*\*|`([^`]+?)`/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      spans.push({ kind: "text", value: text.slice(lastIndex, match.index) });
    }
    if (match[1] !== undefined) {
      spans.push({ kind: "bold", value: match[1] });
    } else if (match[2] !== undefined) {
      spans.push({ kind: "code", value: match[2] });
    }
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    spans.push({ kind: "text", value: text.slice(lastIndex) });
  }
  if (spans.length === 0) spans.push({ kind: "text", value: text });
  return spans;
}
