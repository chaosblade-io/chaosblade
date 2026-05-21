/**
 * SessionCard contract tests — lock the layout grammar so a refactor
 * can't silently regress it. Render-only; pure props in, frame text
 * out (locale-independent assertions on caller-controlled data).
 */

import { render } from "ink-testing-library";
import { describe, expect, it } from "vitest";
import { SessionCard } from "./SessionCard.js";
import type { SessionCardItem } from "../state/types.js";

const SAMPLE: SessionCardItem = {
  kind: "session_card",
  id: "session-test",
  capturedAt: "2026-05-21T02:22:11.086040+08:00",
  rows: [
    { label: "session id", value: "sess_ef505e990bda" },
    { label: "cluster", value: "(none)", dim: true },
    { label: "namespace", value: "default" },
    { label: "model", value: "qwen3.6-max-preview" },
    { label: "permission mode", value: "auto" },
    { label: "tasks", value: "0" },
  ],
};

describe("SessionCard", () => {
  it("renders the ◉ title glyph", () => {
    // Glyph-only — the localised title text ("Session" / "会话")
    // depends on LANG; pinning either string would couple the test
    // to whichever locale the test runner inherited.
    const { lastFrame } = render(<SessionCard item={SAMPLE} />);
    expect(lastFrame() ?? "").toContain("◉");
  });

  it("formats the ISO timestamp as YYYY-MM-DD HH:MM:SS in the header", () => {
    // Date-only assertion — local-time formatting may shift the hour
    // depending on the test runner's TZ. The DATE component is the
    // load-bearing signal (sortable, locale-neutral).
    const { lastFrame } = render(<SessionCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("2026-05-21");
    // The raw ISO must NOT appear — that's the bug-class this card
    // exists to fix.
    expect(frame).not.toContain("2026-05-21T02:22:11.086040");
  });

  it("renders every row's label + value", () => {
    const { lastFrame } = render(<SessionCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    for (const row of SAMPLE.rows) {
      expect(frame).toContain(row.label);
      expect(frame).toContain(row.value);
    }
  });

  it("renders bullets in front of every row", () => {
    const { lastFrame } = render(<SessionCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    // 6 rows → at least 6 bullets. Header line doesn't carry one.
    const bulletCount = (frame.match(/•/g) ?? []).length;
    expect(bulletCount).toBeGreaterThanOrEqual(SAMPLE.rows.length);
  });

  it("omits the header timestamp tail when capturedAt is empty", () => {
    // Defensive — server may omit created_at; renderer must not
    // print a dangling "  · " separator.
    const noTime: SessionCardItem = { ...SAMPLE, capturedAt: "" };
    const { lastFrame } = render(<SessionCard item={noTime} />);
    const frame = lastFrame() ?? "";
    expect(frame).not.toContain("  · ");
    // The card still renders its rows.
    expect(frame).toContain("sess_ef505e990bda");
  });

  it("falls through to ISO when the timestamp is unparseable", () => {
    // Defensive — formatDateTime returns the input verbatim on
    // Number.isNaN(d.getTime()). Asserts that path doesn't crash
    // the render.
    const bad: SessionCardItem = { ...SAMPLE, capturedAt: "not-a-date" };
    const { lastFrame } = render(<SessionCard item={bad} />);
    expect(lastFrame() ?? "").toContain("not-a-date");
  });
});
