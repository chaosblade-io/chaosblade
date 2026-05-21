/**
 * ExperimentsCard contract tests — lock the visual grammar so a
 * future refactor doesn't silently regress it. Render-only; pure
 * props in, frame text out.
 */

import { render } from "ink-testing-library";
import { describe, expect, it } from "vitest";
import { ExperimentsCard } from "./ExperimentsCard.js";
import type { ExperimentsCardItem } from "../state/types.js";

const SAMPLE: ExperimentsCardItem = {
  kind: "experiments_card",
  id: "experiments-test",
  capturedAt: "2026-05-21T03:00:00.000+08:00",
  totalCount: 4,
  rows: [
    {
      useCaseName: "Pod_OOM内存异常",
      faultSymptom: "Pod 内存使用率接近 Limit 上限",
    },
    {
      useCaseName: "Node_CPU使用率过高",
      faultSymptom: "节点 CPU 使用率持续超过 90%",
    },
    {
      useCaseName: "Service_负载均衡异常",
      faultSymptom: "Service Endpoints 列表为空",
    },
    {
      useCaseName: "节点容器运行时磁盘使用率过高",
      faultSymptom: "",
    },
  ],
};

describe("ExperimentsCard", () => {
  it("renders the ✦ title glyph", () => {
    // Glyph-only — title text is i18n-controlled, locale of the
    // test runner shouldn't pin the assertion.
    const { lastFrame } = render(<ExperimentsCard item={SAMPLE} />);
    expect(lastFrame() ?? "").toContain("✦");
  });

  it("formats the ISO timestamp as YYYY-MM-DD in the header", () => {
    const { lastFrame } = render(<ExperimentsCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("2026-05-21");
    expect(frame).not.toContain("2026-05-21T03:00:00.000");
  });

  it("renders every use-case name", () => {
    const { lastFrame } = render(<ExperimentsCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    for (const row of SAMPLE.rows) {
      expect(frame).toContain(row.useCaseName);
    }
  });

  it("renders symptoms next to their use case", () => {
    const { lastFrame } = render(<ExperimentsCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("Pod 内存使用率接近 Limit 上限");
    expect(frame).toContain("节点 CPU 使用率持续超过 90%");
  });

  it("substitutes a dim placeholder when symptom is empty", () => {
    // Row 4 (节点容器运行时磁盘使用率过高) carries faultSymptom=""
    // — must render the localised "(no symptom)" / "（未提供症状）"
    // placeholder rather than leave a bare line. Either locale's
    // dictionary's substring satisfies the assertion.
    const { lastFrame } = render(<ExperimentsCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    const hasZh = frame.includes("未提供症状");
    const hasEn = frame.includes("no symptom");
    expect(hasZh || hasEn).toBe(true);
  });

  it("renders bullets in front of every row", () => {
    const { lastFrame } = render(<ExperimentsCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    const bulletCount = (frame.match(/•/g) ?? []).length;
    expect(bulletCount).toBeGreaterThanOrEqual(SAMPLE.rows.length);
  });

  it("renders the count summary in the header tail", () => {
    // Whether "4 cases" or "共 4 项" — the digit 4 must appear.
    const { lastFrame } = render(<ExperimentsCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    expect(frame).toMatch(/[·•]\s+(\S*\s*)?4(\s+\S+)?/);
  });

  it("aligns the symptom column even when name lengths differ wildly", () => {
    // Sanity: the long-CJK name (28 cells) and the short one
    // ("Pod_OOM内存异常" ~15 cells) both have symptoms; the
    // pad-to-cell-width helper should make symptoms start at the
    // same column. We can't probe ANSI cell positions directly via
    // ink-testing-library, so settle for the negative: the
    // longest-name row must NOT have its symptom glued onto the
    // name without intervening spaces. Match opening paren as
    // either ASCII ``(`` (en) or full-width ``（`` (zh).
    const { lastFrame } = render(<ExperimentsCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    expect(frame).toMatch(/节点容器运行时磁盘使用率过高\s+[（(]/);
  });

  it("renders gracefully when rows is empty", () => {
    const empty: ExperimentsCardItem = { ...SAMPLE, totalCount: 0, rows: [] };
    const { lastFrame } = render(<ExperimentsCard item={empty} />);
    const frame = lastFrame() ?? "";
    // Title chip still renders.
    expect(frame).toContain("✦");
    // Count tail says 0.
    expect(frame).toMatch(/[·•]\s+\S*\s*0/);
  });
});
