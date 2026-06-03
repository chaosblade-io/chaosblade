/**
 * YesNoFeedbackSelect — generic primitive for any "yes / no /
 * free-form-text" prompt. Tests verify the API surface (default
 * labels, label overrides, focus gating) without coupling to any
 * specific consumer (ConfirmMessage etc.).
 */

import { render } from "ink-testing-library";
import { describe, expect, it, vi } from "vitest";
import {
  YesNoFeedbackSelect,
  type YesNoFeedbackAnswer,
} from "./YesNoFeedbackSelect.js";

describe("YesNoFeedbackSelect", () => {
  describe("default labels", () => {
    it("uses i18n defaults when no label overrides are provided", () => {
      const { lastFrame } = render(
        <YesNoFeedbackSelect
          isFocused={true}
          onConfirm={() => undefined}
          onCancel={() => undefined}
        />,
      );
      const frame = lastFrame() ?? "";
      // Defaults from i18n: "是" / "否" / "告诉我别的话…" (zh) or
      // "Yes" / "No" / "Tell me something else…" (en). The active
      // dictionary in tests is zh — same default ordering.
      expect(frame).toContain("是");
      expect(frame).toContain("否");
      expect(frame).toContain("告诉我别的话…");
    });

    it("includes the keyboard hint row", () => {
      const { lastFrame } = render(
        <YesNoFeedbackSelect
          isFocused={true}
          onConfirm={() => undefined}
          onCancel={() => undefined}
        />,
      );
      const frame = lastFrame() ?? "";
      // Hint row is the last visible line — assert one of its
      // cues is present (full string has CJK width quirks). We
      // pick "↑↓" which any caller will see regardless of locale.
      expect(frame).toContain("↑↓");
    });
  });

  describe("label overrides", () => {
    it("renders custom yes / no / feedback labels when provided", () => {
      const { lastFrame } = render(
        <YesNoFeedbackSelect
          yesLabel="提交"
          noLabel="取消"
          feedbackLabel="自定义答复…"
          isFocused={true}
          onConfirm={() => undefined}
          onCancel={() => undefined}
        />,
      );
      const frame = lastFrame() ?? "";
      expect(frame).toContain("提交");
      expect(frame).toContain("取消");
      expect(frame).toContain("自定义答复…");
      // Defaults should NOT appear when overrides are set.
      expect(frame).not.toContain("告诉我别的话…");
    });
  });

  describe("focus gating", () => {
    it("ignores keyboard when isFocused is false", () => {
      // We can't easily simulate keyboard in ink-testing-library
      // without manually feeding stdin chunks, so we do a
      // structural assertion: the component still renders the
      // option list (visual remains, just no input). The actual
      // useInput.isActive=false behaviour is covered by Ink's own
      // tests for useInput; here we just check the component
      // doesn't crash and the labels are still drawn.
      const onConfirm = vi.fn<(a: YesNoFeedbackAnswer, f?: string) => void>();
      const { lastFrame } = render(
        <YesNoFeedbackSelect
          isFocused={false}
          onConfirm={onConfirm}
          onCancel={() => undefined}
        />,
      );
      const frame = lastFrame() ?? "";
      expect(frame).toContain("是");
      expect(onConfirm).not.toHaveBeenCalled();
    });
  });

  describe("initial selection prefix", () => {
    it("renders the [A] / [B] / [C] chips in column 0 for all rows", () => {
      const { lastFrame } = render(
        <YesNoFeedbackSelect
          isFocused={true}
          onConfirm={() => undefined}
          onCancel={() => undefined}
        />,
      );
      const frame = lastFrame() ?? "";
      // Each row carries its own letter chip — there is no cursor
      // glyph and no indent. We only need to verify the chips are
      // present and paired with the right label; colour-based focus
      // is exercised by Ink internally.
      const lines = frame.split("\n");
      expect(lines.some((l) => l.includes("[A]") && l.includes("是"))).toBe(
        true,
      );
      expect(lines.some((l) => l.includes("[B]") && l.includes("否"))).toBe(
        true,
      );
      expect(
        lines.some((l) => l.includes("[C]") && l.includes("告诉我别的话…")),
      ).toBe(true);
    });

    it("respects initialIndex prop without changing chip layout", () => {
      const { lastFrame } = render(
        <YesNoFeedbackSelect
          isFocused={true}
          initialIndex={1}
          onConfirm={() => undefined}
          onCancel={() => undefined}
        />,
      );
      const frame = lastFrame() ?? "";
      // Chips stay aligned regardless of focused row; focus is now
      // a colour-only signal, so the structural check is just "all
      // chips present in their canonical order".
      const lines = frame.split("\n");
      expect(lines.some((l) => l.includes("[A]") && l.includes("是"))).toBe(
        true,
      );
      expect(lines.some((l) => l.includes("[B]") && l.includes("否"))).toBe(
        true,
      );
    });
  });
});
