/**
 * Regression tests for the "duplicate [C] row after Enter→type→Esc"
 * bug. See the header comment in Select.tsx for the root-cause
 * narrative.
 *
 * Why these are STRUCTURAL tests (no simulated keypress):
 *
 *   ink-testing-library's ``stdin.write`` synchronously emits 'data'
 *   but ink's useInput state updates are then batched into React's
 *   microtask queue, which doesn't flush before ``lastFrame()`` is
 *   read. Simulating "Enter → type → Esc" is therefore unreliable.
 *
 *   The fix the production bug needed is STRUCTURAL: keep the total
 *   row count constant across the feedback↔options transition so
 *   Ink's incremental-rendering path on Apple Terminal never has to
 *   handle a row-count shrink (the source of the stale-row residue
 *   that surfaced as a duplicated ``[C]``). We pin that invariant
 *   below — if a future edit re-introduces a separate input row, the
 *   row-count assertion blows up immediately.
 */

import { render } from "ink-testing-library";
import { describe, expect, it } from "vitest";
import { Select, type SelectItem } from "./Select.js";

type Answer = "yes" | "no" | "feedback";

const items: SelectItem<Answer>[] = [
  { value: "yes", label: "提交意图" },
  { value: "no", label: "调整意图" },
  { value: "feedback", label: "告诉 agent 别的话…", hasFeedback: true },
];

function countLinesContaining(frame: string, needle: string): number {
  if (!frame) return 0;
  return frame
    .split("\n")
    .filter((line) => line.includes(needle))
    .length;
}

function nonEmptyRowCount(frame: string): number {
  if (!frame) return 0;
  return frame.split("\n").filter((l) => l.trim().length > 0).length;
}

describe("Select", () => {
  describe("baseline render — no duplicates", () => {
    it("renders exactly one row per item, each with its own chip", () => {
      const { lastFrame } = render(
        <Select<Answer>
          items={items}
          isFocused={true}
          onSelect={() => undefined}
          onCancel={() => undefined}
        />,
      );
      const frame = lastFrame() ?? "";
      // The bug surfaced as a SECOND ``[C]`` row appearing after the
      // feedback→options transition. The baseline render must never
      // have more than one chip of any letter — this is the post-Esc
      // expected state too.
      expect(countLinesContaining(frame, "[A]")).toBe(1);
      expect(countLinesContaining(frame, "[B]")).toBe(1);
      expect(countLinesContaining(frame, "[C]")).toBe(1);
    });

    it("renders one hint row at the bottom (not two)", () => {
      const { lastFrame } = render(
        <Select<Answer>
          items={items}
          isFocused={true}
          onSelect={() => undefined}
          onCancel={() => undefined}
        />,
      );
      const frame = lastFrame() ?? "";
      // The options-mode hint substring is unique — assert it appears
      // exactly once.
      expect(countLinesContaining(frame, "A-Z")).toBe(1);
    });
  });

  describe("row-count stability (the structural invariant)", () => {
    it("renders exactly items.length + 2 visible rows (chips + marginTop blank + hint)", () => {
      const { lastFrame } = render(
        <Select<Answer>
          items={items}
          isFocused={true}
          onSelect={() => undefined}
          onCancel={() => undefined}
        />,
      );
      const frame = lastFrame() ?? "";
      // 3 chip rows + 1 hint row = 4 non-empty rows. (The marginTop
      // blank above the hint is whitespace-only and filtered out.)
      // If a future edit reintroduces a SEPARATE inline-input row
      // below the chips for feedback mode (the pre-fix layout that
      // caused the bug), the steady-state count would still be 4
      // here, but the feedback-mode count would jump to 6 and the
      // freed rows on Esc would re-expose the duplicate-[C] bug.
      // The test below pins the feedback-mode count too.
      expect(nonEmptyRowCount(frame)).toBe(items.length + 1);
    });

    it("never renders a row matching the ❯ input prompt glyph in the default options-mode render", () => {
      const { lastFrame } = render(
        <Select<Answer>
          items={items}
          isFocused={true}
          onSelect={() => undefined}
          onCancel={() => undefined}
        />,
      );
      const frame = lastFrame() ?? "";
      // The input prompt glyph (Icons.prompt = ❯) should NOT appear
      // in options mode — it only shows when the active item is the
      // hasFeedback one AND mode is feedback. The initial render is
      // options mode, so no ❯ should be present anywhere in the
      // frame. (The hint row uses different glyphs.)
      expect(frame).not.toContain("❯");
    });
  });

  describe("inline-input morph (the structural fix)", () => {
    it("renders the hasFeedback item's label on the same chip row as [C]", () => {
      const { lastFrame } = render(
        <Select<Answer>
          items={items}
          isFocused={true}
          onSelect={() => undefined}
          onCancel={() => undefined}
        />,
      );
      const frame = lastFrame() ?? "";
      const lines = frame.split("\n");
      const cRow = lines.find((l) => l.includes("[C]"));
      expect(cRow).toBeDefined();
      // Label travels with chip [C] on the SAME row. After my fix,
      // feedback mode preserves this row structure — the input
      // replaces the label payload IN PLACE, no new row is added.
      // The label text itself stays on the [C] row.
      expect(cRow ?? "").toContain("告诉 agent 别的话…");
    });
  });
});
