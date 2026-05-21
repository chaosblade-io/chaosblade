/**
 * Tests for ``findLastSafeSplitPoint`` — the markdown-aware split used
 * by ``TOKEN_APPENDED`` to carve streaming agent replies into a
 * head fragment (commits to history) and tail fragment (stays in
 * pending).
 *
 * Three families of behaviour the reducer relies on:
 *   1. Paragraph-boundary splits — the common path during prose.
 *   2. Code-block preservation — never split between ``` fences.
 *   3. "No safe split" — return content.length so reducer keeps text
 *      in pending until a paragraph break arrives.
 */

import { describe, expect, it } from "vitest";
import { findLastSafeSplitPoint } from "./markdownSplit.js";

describe("findLastSafeSplitPoint / paragraph boundaries", () => {
  it("splits right after the last \\n\\n in plain prose", () => {
    const text = "First paragraph.\n\nSecond paragraph still streaming";
    const point = findLastSafeSplitPoint(text);
    // Point is just past the \n\n so head ends with the blank line.
    expect(text.slice(0, point)).toBe("First paragraph.\n\n");
    expect(text.slice(point)).toBe("Second paragraph still streaming");
  });

  it("picks the LAST paragraph boundary, not the first", () => {
    const text = "Para one.\n\nPara two.\n\nPara three in progress";
    const point = findLastSafeSplitPoint(text);
    expect(text.slice(0, point)).toBe("Para one.\n\nPara two.\n\n");
    expect(text.slice(point)).toBe("Para three in progress");
  });

  it("returns content.length when no \\n\\n is present yet", () => {
    const text = "All one paragraph, no breaks yet";
    expect(findLastSafeSplitPoint(text)).toBe(text.length);
  });
});

describe("findLastSafeSplitPoint / code blocks", () => {
  it("returns the code-block start when the tail is mid-block", () => {
    // Open fence + some content, no closing fence yet — streaming
    // mid-block scenario. The whole block must stay together until
    // closed, so split moves to BEFORE the open fence.
    const text = "Before block\n\n```python\ndef foo():\n    return 1";
    const point = findLastSafeSplitPoint(text);
    expect(text.slice(point)).toBe("```python\ndef foo():\n    return 1");
    expect(text.slice(0, point)).toBe("Before block\n\n");
  });

  it("allows splits at \\n\\n AFTER a closed code block", () => {
    const text = "Intro\n\n```js\nconsole.log(1)\n```\n\nAfter block";
    const point = findLastSafeSplitPoint(text);
    // Last \n\n is between ``` and "After block"; that boundary is
    // outside any code block so it's a safe split.
    expect(text.slice(point)).toBe("After block");
  });

  it("skips a \\n\\n that lies inside a code block", () => {
    // Code block contains its own blank line (valid markdown — code
    // blocks pass content through verbatim). The split must not land
    // there. Outside the block we DO have a safe \n\n, so we use it.
    const text =
      "Para.\n\n```\nline one\n\nline three\n```\n\nTail still streaming";
    const point = findLastSafeSplitPoint(text);
    expect(text.slice(point)).toBe("Tail still streaming");
  });

  it("returns 0 when the entire content is a single open code block", () => {
    // Edge case: code block opened at offset 0, never closed. Split
    // = 0 → head fragment is empty. Reducer's canSplit guard catches
    // splitPoint <= 0 and falls through to the no-split path.
    const text = "```python\nprint('streaming')";
    expect(findLastSafeSplitPoint(text)).toBe(0);
  });
});

describe("findLastSafeSplitPoint / edge inputs", () => {
  it("returns 0 for the empty string", () => {
    expect(findLastSafeSplitPoint("")).toBe(0);
  });

  it("returns content.length for a single trailing newline", () => {
    expect(findLastSafeSplitPoint("hello\n")).toBe("hello\n".length);
  });
});
