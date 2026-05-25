/**
 * ToolMessage render tests — focused on Phase 4.A's two-form split.
 *
 * Pending (``isPending=true``) → ``ToolMessageCompact`` (1 row).
 * History (``isPending=false`` or absent) → full bordered card.
 *
 * Why pin these explicitly: the compact form is the dominant lever
 * for streaming-flicker mitigation on terminals without DEC 2026
 * (Apple Terminal). A future refactor that removes
 * ``wrap="truncate-end"``, drops the early return, or re-introduces a
 * body section in pending would silently regress the dynamic-frame
 * size budget that this app has been tuned against.
 */

import { render as inkRender } from "ink-testing-library";
import { describe, expect, it } from "vitest";
import { ToolMessage } from "./ToolMessage.js";
import type { ToolItem } from "../../state/types.js";

function tool(overrides: Partial<ToolItem> = {}): ToolItem {
  return {
    kind: "tool",
    id: "tool-test-1",
    callId: "call-1",
    name: "kubectl",
    node: "phase1",
    status: "running",
    resultPreview: "",
    raw: "first line\nsecond line\nthird line",
    elapsedMs: 2300,
    startedAt: 0,
    ...overrides,
  };
}

/** Visual rows of the rendered frame (after stripping the trailing
 *  newline ink-testing-library tacks on). */
function frameRows(frame: string | undefined): string[] {
  if (!frame) return [];
  // Trim trailing empty lines but keep internal blanks.
  const lines = frame.split("\n");
  while (lines.length > 0 && lines[lines.length - 1]?.trim() === "") {
    lines.pop();
  }
  return lines;
}

describe("ToolMessage / pending compact form (Phase 4.A)", () => {
  it("renders exactly one visible row regardless of body content", () => {
    // ``raw`` has 3 lines of would-be body content; compact form
    // must NOT show any of them. The whole tool entry collapses
    // into a single line.
    const item = tool({
      raw: "alpha\nbeta\ngamma\ndelta\nepsilon",
    });
    const { lastFrame } = inkRender(<ToolMessage item={item} isPending />);
    expect(frameRows(lastFrame())).toHaveLength(1);
  });

  it("still includes the tool name in the single row", () => {
    const item = tool({ name: "blade-status" });
    const { lastFrame } = inkRender(<ToolMessage item={item} isPending />);
    expect(lastFrame() ?? "").toContain("blade-status");
  });

  it("excludes the body output text in pending", () => {
    // The body text is the bug-class — it's what pre-Phase-4.A used
    // to cost 5+ rows per running tool. Verify no body line bleeds
    // through to the compact frame.
    const item = tool({ raw: "BODY_MARKER_should_not_appear" });
    const { lastFrame } = inkRender(<ToolMessage item={item} isPending />);
    expect(lastFrame() ?? "").not.toContain("BODY_MARKER_should_not_appear");
  });

  it("shows the locator chip when present (post-TOOL_ENDED)", () => {
    const item = tool({ status: "success", locator: "T7", elapsedMs: 491 });
    const { lastFrame } = inkRender(<ToolMessage item={item} isPending />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("T7");
    // Brackets sit immediately around the locator text.
    expect(frame).toMatch(/\[\s*T7\s*\]/);
  });

  it("omits the locator chip while a tool is still running (no locator yet)", () => {
    const item = tool({ status: "running", locator: undefined });
    const { lastFrame } = inkRender(<ToolMessage item={item} isPending />);
    expect(lastFrame() ?? "").not.toMatch(/\[T\d+\]/);
  });

  it("renders the elapsed + node tail", () => {
    const item = tool({ status: "success", elapsedMs: 491, node: "phase2" });
    const { lastFrame } = inkRender(<ToolMessage item={item} isPending />);
    const frame = lastFrame() ?? "";
    // ``491ms`` is the formatted elapsed; ``phase2`` is the node;
    // the `·` separator joins them.
    expect(frame).toContain("491ms");
    expect(frame).toContain("phase2");
  });

  it("survives an empty raw + missing elapsed (just-started tool)", () => {
    const item = tool({
      status: "running",
      elapsedMs: undefined,
      raw: "",
      resultPreview: "",
      node: "",
    });
    const { lastFrame } = inkRender(<ToolMessage item={item} isPending />);
    // Should still render exactly one row with at least the tool name.
    expect(frameRows(lastFrame())).toHaveLength(1);
    expect(lastFrame() ?? "").toContain(item.name);
  });
});

describe("ToolMessage / history full-card form", () => {
  it("renders multi-row bordered card when not pending", () => {
    // History form should expand to multiple rows (border top, body,
    // border bottom, etc.) — at least 3 rows.
    const item = tool({
      status: "success",
      raw: "first\nsecond\nthird",
      locator: "T1",
    });
    const { lastFrame } = inkRender(<ToolMessage item={item} />);
    const rows = frameRows(lastFrame());
    expect(rows.length).toBeGreaterThanOrEqual(3);
  });

  it("renders the body text in history form", () => {
    const item = tool({
      status: "success",
      raw: "BODY_MARKER_visible_in_history",
      locator: "T2",
    });
    const { lastFrame } = inkRender(<ToolMessage item={item} />);
    expect(lastFrame() ?? "").toContain("BODY_MARKER_visible_in_history");
  });
});
