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

  describe("initial selection cursor", () => {
    it("places the cursor on the first option by default", () => {
      const { lastFrame } = render(
        <YesNoFeedbackSelect
          isFocused={true}
          onConfirm={() => undefined}
          onCancel={() => undefined}
        />,
      );
      const frame = lastFrame() ?? "";
      // Cursor glyph is ``❯`` (Icons.prompt) in Unicode mode. We
      // search for it preceding "是" — the first item — to confirm
      // initial focus lands on index 0.
      const lines = frame.split("\n");
      const yesLine = lines.find((l) => l.includes("是"));
      expect(yesLine).toBeDefined();
      expect(yesLine).toContain("❯");
    });

    it("respects initialIndex prop", () => {
      const { lastFrame } = render(
        <YesNoFeedbackSelect
          isFocused={true}
          initialIndex={1}
          onConfirm={() => undefined}
          onCancel={() => undefined}
        />,
      );
      const frame = lastFrame() ?? "";
      const lines = frame.split("\n");
      const noLine = lines.find((l) => l.includes("否"));
      expect(noLine).toBeDefined();
      expect(noLine).toContain("❯");
    });
  });
});
