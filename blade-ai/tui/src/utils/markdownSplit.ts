/**
 * Find a "safe" split index in a streaming markdown buffer.
 *
 * Used by the reducer's ``TOKEN_APPENDED`` handler to carve the live
 * agent reply into a head fragment (commit to history → moves into
 * Ink's ``<Static>`` and stops re-rendering) and a tail fragment
 * (stays in pending, continues streaming). Mid-stream Static commits
 * are the lever that bounds the dynamic-frame redraw payload: once
 * the prefix lands in Static it never re-renders again, no matter
 * how long the overall reply ultimately becomes.
 *
 * Safety rules (1:1 port of qwen-code's findLastSafeSplitPoint, which
 * itself is the long-running solution to streaming-markdown flicker):
 *
 *   1. **Never split inside a fenced code block.** A split that
 *      lands between the two triple-backticks would produce one
 *      fragment that opens a code block and another fragment that
 *      can't close it — marked-terminal renders the rest of the
 *      content in monospace forever afterward. If the tail of the
 *      current text IS inside a code block, return the start index
 *      of that code block so the whole block stays in pending until
 *      it closes (or, worst case, returns the original content
 *      length so we just don't split this round).
 *
 *   2. **Prefer paragraph boundaries (``\n\n``).** A double-newline
 *      ends a markdown block element (paragraph, list item terminator,
 *      heading separator). Splitting there means each fragment is a
 *      self-contained markdown document — marked-terminal can render
 *      the head fragment to ANSI completely, and the tail fragment
 *      starts fresh with whatever the LLM emits next.
 *
 *   3. **Don't split inside a code block reachable from a paragraph
 *      boundary candidate.** If the candidate ``\n\n`` happens to be
 *      inside a code block (which can contain blank lines), keep
 *      searching backward.
 *
 *   4. **Fall back to ``content.length``** (no split this round) when
 *      no safe paragraph break is found. The next TOKEN_APPENDED will
 *      re-evaluate with more content; eventually the LLM will emit a
 *      paragraph break and the split fires then.
 *
 * Returns the EXCLUSIVE end index of the head fragment, so the caller
 * does ``content.slice(0, splitPoint)`` for head and
 * ``content.slice(splitPoint)`` for tail.
 */

/**
 * Does the character at ``indexToTest`` sit inside a fenced (```)
 * code block? Walks fence delimiters from the start of the content
 * to indexToTest and counts: odd → inside, even → outside.
 */
function isIndexInsideCodeBlock(content: string, indexToTest: number): boolean {
  let fenceCount = 0;
  let searchPos = 0;
  while (searchPos < content.length) {
    const nextFence = content.indexOf("```", searchPos);
    if (nextFence === -1 || nextFence >= indexToTest) {
      break;
    }
    fenceCount += 1;
    searchPos = nextFence + 3;
  }
  return fenceCount % 2 === 1;
}

/**
 * Find the start index of the code block enclosing ``index``, or -1
 * if ``index`` isn't inside one. Used to compute "split right before
 * the open fence" when the streaming tail is mid-code-block.
 */
function findEnclosingCodeBlockStart(content: string, index: number): number {
  if (!isIndexInsideCodeBlock(content, index)) return -1;
  let cursor = 0;
  while (cursor < index) {
    const blockStart = content.indexOf("```", cursor);
    if (blockStart === -1 || blockStart >= index) break;
    const blockEnd = content.indexOf("```", blockStart + 3);
    if (blockStart < index) {
      if (blockEnd === -1 || index < blockEnd + 3) {
        return blockStart;
      }
    }
    if (blockEnd === -1) break;
    cursor = blockEnd + 3;
  }
  return -1;
}

export function findLastSafeSplitPoint(content: string): number {
  // Rule 1: tail mid-code-block → split right before the open fence
  // (or 0 if the block starts at position 0, which yields an empty
  // head fragment — caller's responsibility to skip the split when
  // splitPoint === 0).
  const enclosingBlockStart = findEnclosingCodeBlockStart(
    content,
    content.length,
  );
  if (enclosingBlockStart !== -1) {
    return enclosingBlockStart;
  }

  // Rule 2-3: walk back over ``\n\n`` candidates and pick the last
  // one not inside a code block.
  let searchFrom = content.length;
  while (searchFrom >= 0) {
    const dnlIndex = content.lastIndexOf("\n\n", searchFrom);
    if (dnlIndex === -1) break;
    const candidate = dnlIndex + 2;
    if (!isIndexInsideCodeBlock(content, candidate)) {
      return candidate;
    }
    // Candidate was inside a code block — keep searching backward
    // from BEFORE the matched ``\n\n`` so we make progress.
    searchFrom = dnlIndex - 1;
  }

  // Rule 4: no safe split this round.
  return content.length;
}
