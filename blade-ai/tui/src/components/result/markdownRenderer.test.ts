import { describe, expect, it } from "vitest";

import { parseInlines, parseMarkdown } from "./markdownRenderer.js";

describe("parseInlines", () => {
  it("returns single text span for plain text", () => {
    const spans = parseInlines("hello world");
    expect(spans).toEqual([{ kind: "text", value: "hello world" }]);
  });

  it("extracts a bold span", () => {
    const spans = parseInlines("foo **bar** baz");
    expect(spans).toEqual([
      { kind: "text", value: "foo " },
      { kind: "bold", value: "bar" },
      { kind: "text", value: " baz" },
    ]);
  });

  it("extracts an inline code span", () => {
    const spans = parseInlines("run `kubectl get` first");
    expect(spans).toEqual([
      { kind: "text", value: "run " },
      { kind: "code", value: "kubectl get" },
      { kind: "text", value: " first" },
    ]);
  });

  it("handles multiple bold + code spans on the same line", () => {
    const spans = parseInlines("**A** and `B` and **C**");
    expect(spans).toEqual([
      { kind: "bold", value: "A" },
      { kind: "text", value: " and " },
      { kind: "code", value: "B" },
      { kind: "text", value: " and " },
      { kind: "bold", value: "C" },
    ]);
  });

  it("tolerates unclosed markers as plain text", () => {
    // ``**broken`` (no closing) should not crash — falls through as text
    const spans = parseInlines("**broken");
    expect(spans).toEqual([{ kind: "text", value: "**broken" }]);
  });
});

describe("parseMarkdown", () => {
  it("recognises ## as level-2 heading", () => {
    const blocks = parseMarkdown("## Summary");
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({
      kind: "heading",
      level: 2,
    });
  });

  it("recognises ### as level-3 heading", () => {
    const blocks = parseMarkdown("### Sub");
    expect(blocks[0]).toMatchObject({ kind: "heading", level: 3 });
  });

  it("collapses #### and deeper to level-3", () => {
    const blocks = parseMarkdown("#### Deep\n##### Deeper");
    for (const b of blocks) {
      if (b.kind === "heading") expect(b.level).toBe(3);
    }
  });

  it("groups consecutive '- ' lines into a single list", () => {
    const md = "- one\n- two\n- three";
    const blocks = parseMarkdown(md);
    const list = blocks.find((b) => b.kind === "list");
    expect(list).toBeDefined();
    expect(list && list.kind === "list" && list.items).toHaveLength(3);
  });

  it("treats * as a bullet marker too", () => {
    const blocks = parseMarkdown("* one\n* two");
    const list = blocks.find((b) => b.kind === "list");
    expect(list && list.kind === "list" && list.items).toHaveLength(2);
  });

  it("collapses consecutive non-empty lines into one paragraph", () => {
    const blocks = parseMarkdown("line A\nline B\nline C");
    const paras = blocks.filter((b) => b.kind === "paragraph");
    expect(paras).toHaveLength(1);
  });

  it("blank lines produce a blank spacer (no stacking)", () => {
    const blocks = parseMarkdown("a\n\n\n\nb");
    const blanks = blocks.filter((b) => b.kind === "blank");
    // Multiple blank lines collapse to ONE blank token between
    // paragraphs (avoids vertical sprawl).
    expect(blanks).toHaveLength(1);
  });

  it("does not throw on disallowed constructs (table / link / blockquote)", () => {
    const md = [
      "## OK",
      "| a | b |",
      "[link](http://x)",
      "> quote",
      "```",
      "code fence body",
      "```",
    ].join("\n");
    expect(() => parseMarkdown(md)).not.toThrow();
  });

  it("preserves heading + list + paragraph order", () => {
    const md = ["## Top", "- item 1", "- item 2", "", "Some text."].join("\n");
    const blocks = parseMarkdown(md);
    expect(blocks[0]?.kind).toBe("heading");
    expect(blocks[1]?.kind).toBe("list");
    // blank then paragraph
    const lastNonBlank = blocks.filter((b) => b.kind !== "blank").pop();
    expect(lastNonBlank?.kind).toBe("paragraph");
  });
});
