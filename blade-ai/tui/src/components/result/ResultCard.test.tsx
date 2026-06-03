/**
 * ResultCard render tests — covers the v3 redesign (bracket chips +
 * section headings + adaptive partial/failed sections).
 *
 * We don't try to assert exact pixel/box-drawing layout because Ink's
 * frame width depends on stdout columns at test time. Instead we
 * normalise the frame (strip box-drawing chars + collapse whitespace)
 * and assert on substrings the user would actually read.
 */

import { render } from "ink-testing-library";
import { describe, expect, it } from "vitest";
import { ResultCard } from "./ResultCard.js";
import type { ResultItem } from "../../state/types.js";

const baseResult = (
  overrides: Partial<ResultItem> = {},
): ResultItem => ({
  kind: "result",
  id: "r-1",
  taskId: "task-6fa97268",
  status: "success",
  faultType: "node-cpu-fullload",
  bladeUid: "b02c7d1a745dcd54",
  duration: "13m14s",
  summary: "CPU sustained at 78-82% for the full 600s window",
  locator: "E1",
  ...overrides,
});

const normalise = (frame: string) =>
  frame.replace(/[│╭╮╰╯─━┃┏┓┗┛═╔╗╚╝]/g, " ").replace(/\s+/g, " ");

describe("ResultCard", () => {
  describe("success status", () => {
    const item = baseResult();

    it("renders the [✓ SUCCESS] bracket chip in the title row", () => {
      const { lastFrame } = render(<ResultCard item={item} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("[");
      expect(frame).toContain("SUCCESS");
      expect(frame).toContain("]");
    });

    it("renders the locator [E1] as a chip pair with the status chip", () => {
      const { lastFrame } = render(<ResultCard item={item} />);
      const frame = lastFrame() ?? "";
      // Locator appears as [E1]; both brackets and inner text are
      // rendered as separate Text nodes but the frame shows them
      // contiguous after the box-drawing strip.
      const normalised = normalise(frame);
      expect(normalised).toContain("[E1]");
    });

    it("renders the Outcome section heading", () => {
      const { lastFrame } = render(<ResultCard item={item} />);
      expect(lastFrame() ?? "").toContain("── 执行结果");
    });

    it("renders the Effect verified section when summary is set", () => {
      const { lastFrame } = render(<ResultCard item={item} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("── 效果验证");
      expect(frame).toContain("78-82%");
    });

    it("does NOT render the Recovery notes or Failure analysis sections", () => {
      const { lastFrame } = render(<ResultCard item={item} />);
      const frame = lastFrame() ?? "";
      expect(frame).not.toContain("── 恢复说明");
      expect(frame).not.toContain("── 失败分析");
    });

    it("renders the replay hint outside the box", () => {
      const { lastFrame } = render(<ResultCard item={item} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("task-6fa97268");
      // Hint string substring (Chinese dictionary): "/replay {id} instant"
      expect(frame).toMatch(/replay/);
    });
  });

  describe("partial status", () => {
    const item = baseResult({
      status: "partial",
      cause: "2 pods owned by a different ReplicaSet",
      hint: "re-run with --include-system-namespaces if intended",
    });

    it("renders the [⚠ PARTIAL] chip + Recovery notes section", () => {
      const { lastFrame } = render(<ResultCard item={item} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("PARTIAL");
      expect(frame).toContain("── 恢复说明");
    });

    it("uses the why_partial label inside Recovery notes (not 'cause')", () => {
      const { lastFrame } = render(<ResultCard item={item} />);
      const frame = lastFrame() ?? "";
      // zh dictionary: why_partial = '部分恢复原因' / cause = '失败原因'
      expect(frame).toContain("部分恢复原因");
      expect(frame).not.toContain("失败原因");
    });

    it("does NOT render the Failure analysis section", () => {
      const { lastFrame } = render(<ResultCard item={item} />);
      expect(lastFrame() ?? "").not.toContain("── 失败分析");
    });
  });

  describe("failed status", () => {
    const item = baseResult({
      status: "failed",
      bladeUid: "",
      duration: "8s",
      summary: "",
      cause: "blade create failed: target pod not found",
      hint: "verify pod name with kubectl get pods -n cms-demo",
    });

    it("renders the [✗ FAILED] chip + Failure analysis section", () => {
      const { lastFrame } = render(<ResultCard item={item} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("FAILED");
      expect(frame).toContain("── 失败分析");
    });

    it("renders the cause text inside Failure analysis", () => {
      const { lastFrame } = render(<ResultCard item={item} />);
      const frame = lastFrame() ?? "";
      const normalised = normalise(frame);
      expect(normalised).toContain("blade create failed");
    });

    it("does NOT render the Recovery notes section", () => {
      const { lastFrame } = render(<ResultCard item={item} />);
      expect(lastFrame() ?? "").not.toContain("── 恢复说明");
    });

    it("skips the Effect verified section when summary is empty", () => {
      const { lastFrame } = render(<ResultCard item={item} />);
      expect(lastFrame() ?? "").not.toContain("── 效果验证");
    });
  });

  describe("v3 audit fields (target / replanCount / sideEffects)", () => {
    it("renders the target (namespace · names) in the Outcome section", () => {
      const item = baseResult({
        target: { namespace: "cms-demo", names: ["cn-hongkong.10.0.1.60"] },
      });
      const { lastFrame } = render(<ResultCard item={item} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("cms-demo");
      expect(frame).toContain("cn-hongkong.10.0.1.60");
    });

    it("renders the Attempts row only when replanCount > 0", () => {
      const clean = baseResult({ replanCount: 0 });
      const cleanFrame = render(<ResultCard item={clean} />).lastFrame() ?? "";
      expect(cleanFrame).not.toContain("尝试次数");

      const replanned = baseResult({ replanCount: 2 });
      const replannedFrame =
        render(<ResultCard item={replanned} />).lastFrame() ?? "";
      expect(replannedFrame).toContain("尝试次数");
      expect(replannedFrame).toContain("2");
    });

    it("renders the Side effects section always for success/partial", () => {
      const noEffects = baseResult({ sideEffects: undefined });
      const noFrame = render(<ResultCard item={noEffects} />).lastFrame() ?? "";
      expect(noFrame).toContain("── 副作用");

      const withEffects = baseResult({
        sideEffects: [
          "pod restart · accounting-6fb-qn2vr (OOMKilled)",
          "hpa · scaled from 3 to 5",
        ],
      });
      const frame = render(<ResultCard item={withEffects} />).lastFrame() ?? "";
      expect(frame).toContain("── 副作用");
      expect(frame).toContain("OOMKilled");
      expect(frame).toContain("hpa");
    });
  });

  describe("guard rails", () => {
    it("skips the Outcome heading when all metadata fields are empty", () => {
      // Edge case: malformed payload (e.g. policy block before any
      // metadata was captured). The Outcome heading must not render
      // as a dangling label with no body underneath.
      const item = baseResult({
        faultType: "",
        bladeUid: "",
        duration: "",
        summary: "",
      });
      const { lastFrame } = render(<ResultCard item={item} />);
      expect(lastFrame() ?? "").not.toContain("── 执行结果");
    });

    it("still renders the title row when only status + taskId are set", () => {
      const item = baseResult({
        faultType: "",
        bladeUid: "",
        duration: "",
        summary: "",
      });
      const { lastFrame } = render(<ResultCard item={item} />);
      const frame = lastFrame() ?? "";
      // Title chip still appears even with no metadata content.
      expect(frame).toContain("SUCCESS");
      expect(frame).toContain("task-6fa97268");
    });
  });
});
