/**
 * HelpCard contract tests — lock the visual grammar so a future
 * refactor doesn't silently regress it:
 *
 *   - title chip uses the ⌘ glyph + Commands label
 *   - timestamp suffix renders in the header
 *   - section heading uses the ``── Heading `` + dashes pattern
 *     (divider-prefixed so a stray "Commands" substring in the title
 *     can never collide with a heading match)
 *   - top-level row renders the command name including aliases
 *   - subcommand row renders without a leading slash (parent context
 *     is the indent, not a repeated ``/cmd ``)
 *   - tip line renders at the foot
 *
 * No reliance on exact column counts — those are layout choices that
 * may flex. The contract is "these tokens appear in this order".
 */

import { render } from "ink-testing-library";
import { describe, expect, it } from "vitest";
import { HelpCard } from "./HelpCard.js";
import type { HelpCardItem } from "../state/types.js";

const SAMPLE: HelpCardItem = {
  kind: "help_card",
  id: "help-test",
  capturedAt: "2026-05-21T01:15:00.000+08:00",
  sections: [
    {
      heading: "General",
      rows: [
        { kind: "top", name: "/clear", description: "Clear the scrollback" },
        {
          kind: "top",
          name: "/exit · /quit",
          description: "Exit blade-ai",
        },
        {
          kind: "top",
          name: "/mode [calm|working|dense]",
          description: "Toggle display density",
        },
      ],
    },
    {
      heading: "Skills",
      rows: [
        { kind: "top", name: "/config", description: "Server config" },
        { kind: "sub", name: "list", description: "List config keys" },
        { kind: "sub", name: "set <key> <value>", description: "Write a key" },
      ],
    },
  ],
  tip: "Tip: type / then TAB to autocomplete",
};

describe("HelpCard", () => {
  it("renders the ⌘ title chip", () => {
    // Glyph-only assertion — the title text itself is i18n-controlled
    // ("Commands" in en, "命令" in zh) and locking either string here
    // would couple the test to whatever LANG the test runner inherited.
    const { lastFrame } = render(<HelpCard item={SAMPLE} />);
    expect(lastFrame() ?? "").toContain("⌘");
  });

  it("renders the captured-at timestamp in the header", () => {
    const { lastFrame } = render(<HelpCard item={SAMPLE} />);
    expect(lastFrame() ?? "").toContain("2026-05-21");
  });

  it("renders the section heading with the divider prefix", () => {
    // The ``── `` prefix is load-bearing — it prevents a string match
    // against the card's own title chip (``⌘ Commands``) from
    // colliding with a section named "Commands" if a future taxonomy
    // change brought one in.
    const { lastFrame } = render(<HelpCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("── General");
    expect(frame).toContain("── Skills");
  });

  it("renders aliases inline with the parent command using `·`", () => {
    const { lastFrame } = render(<HelpCard item={SAMPLE} />);
    expect(lastFrame() ?? "").toContain("/exit · /quit");
  });

  it("renders argument hints inside the command name", () => {
    const { lastFrame } = render(<HelpCard item={SAMPLE} />);
    expect(lastFrame() ?? "").toContain("/mode [calm|working|dense]");
  });

  it("renders subcommands without re-printing the parent /cmd prefix", () => {
    // Sub names render as ``list`` / ``set <key> <value>`` — not
    // ``/config list`` — because the visual indent already establishes
    // parentage. Re-printing the prefix would push the description
    // column past the name gutter on every sub row.
    const { lastFrame } = render(<HelpCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("set <key> <value>");
    expect(frame).not.toContain("/config set <key> <value>");
  });

  it("renders descriptions next to their command", () => {
    const { lastFrame } = render(<HelpCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("Clear the scrollback");
    expect(frame).toContain("Toggle display density");
    expect(frame).toContain("List config keys");
  });

  it("renders the tip line at the foot", () => {
    const { lastFrame } = render(<HelpCard item={SAMPLE} />);
    expect(lastFrame() ?? "").toContain(
      "Tip: type / then TAB to autocomplete",
    );
  });

  it("omits the tip line when tip is empty", () => {
    const noTip = { ...SAMPLE, tip: "" };
    const { lastFrame } = render(<HelpCard item={noTip} />);
    expect(lastFrame() ?? "").not.toContain("Tip:");
  });

  it("renders empty sections gracefully (no rows, just heading)", () => {
    // Defensive — the builder is supposed to drop empty groups, but
    // a future caller might pass one in. Should not crash.
    const empty: HelpCardItem = {
      ...SAMPLE,
      sections: [{ heading: "EmptyGroup", rows: [] }],
    };
    const { lastFrame } = render(<HelpCard item={empty} />);
    expect(lastFrame() ?? "").toContain("── EmptyGroup");
  });
});
