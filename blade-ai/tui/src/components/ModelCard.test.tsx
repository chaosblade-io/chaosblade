/**
 * ModelCard contract tests — lock the visual grammar so a future
 * refactor doesn't silently regress it.
 */

import { render } from "ink-testing-library";
import { describe, expect, it } from "vitest";
import { ModelCard } from "./ModelCard.js";
import type { ModelCardItem } from "../state/types.js";

const SAMPLE: ModelCardItem = {
  kind: "model_card",
  id: "model-test",
  capturedAt: "2026-05-21T10:15:32.000+08:00",
  activeModel: "qwen3.6-max-preview",
  apiBaseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  totalCount: 7,
  sections: [
    {
      provider: "qwen",
      rows: [
        { id: "qwen3.6-max-preview", active: true },
        { id: "qwen-max", active: false },
        { id: "qwen-plus", active: false },
      ],
    },
    {
      provider: "openai",
      rows: [
        { id: "gpt-4o", active: false },
        { id: "gpt-4o-mini", active: false },
      ],
    },
    {
      provider: "anthropic",
      rows: [
        { id: "claude-opus-4-7", active: false },
        { id: "claude-sonnet-4-6", active: false },
      ],
    },
  ],
};

describe("ModelCard", () => {
  it("renders the ◆ title glyph", () => {
    const { lastFrame } = render(<ModelCard item={SAMPLE} />);
    expect(lastFrame() ?? "").toContain("◆");
  });

  it("formats the captured-at timestamp", () => {
    const { lastFrame } = render(<ModelCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("2026-05-21");
    expect(frame).not.toContain("2026-05-21T10:15:32.000");
  });

  it("renders the count in the header tail", () => {
    const { lastFrame } = render(<ModelCard item={SAMPLE} />);
    expect(lastFrame() ?? "").toMatch(/[·•]\s+(\S*\s*)?7(\s+\S+)?/);
  });

  it("renders the api_base_url subhead", () => {
    const { lastFrame } = render(<ModelCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("api_base_url");
    expect(frame).toContain("dashscope.aliyuncs.com");
  });

  it("hides the api_base_url subhead when empty", () => {
    const noUrl: ModelCardItem = { ...SAMPLE, apiBaseUrl: "" };
    const { lastFrame } = render(<ModelCard item={noUrl} />);
    expect(lastFrame() ?? "").not.toContain("api_base_url");
  });

  it("renders the active row with ● glyph", () => {
    const { lastFrame } = render(<ModelCard item={SAMPLE} />);
    expect(lastFrame() ?? "").toContain("●");
  });

  it("renders inactive rows with ○ glyph", () => {
    const { lastFrame } = render(<ModelCard item={SAMPLE} />);
    expect(lastFrame() ?? "").toContain("○");
  });

  it("renders the active model name", () => {
    const { lastFrame } = render(<ModelCard item={SAMPLE} />);
    expect(lastFrame() ?? "").toContain("qwen3.6-max-preview");
  });

  it("renders every section heading with the divider prefix", () => {
    const { lastFrame } = render(<ModelCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("── qwen");
    expect(frame).toContain("── openai");
    expect(frame).toContain("── anthropic");
  });

  it("renders every model id across sections", () => {
    const { lastFrame } = render(<ModelCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("qwen-max");
    expect(frame).toContain("qwen-plus");
    expect(frame).toContain("gpt-4o");
    expect(frame).toContain("gpt-4o-mini");
    expect(frame).toContain("claude-opus-4-7");
    expect(frame).toContain("claude-sonnet-4-6");
  });

  it("renders the optional note column when set", () => {
    // Custom models commonly carry a note explaining why they're
    // outside the curated list. Note must appear in the rendered
    // frame next to its row.
    const withNote: ModelCardItem = {
      ...SAMPLE,
      activeModel: "private-llm-v9",
      sections: [
        ...SAMPLE.sections,
        {
          provider: "custom",
          rows: [
            {
              id: "private-llm-v9",
              active: true,
              note: "— not in the curated list",
            },
          ],
        },
      ],
    };
    const { lastFrame } = render(<ModelCard item={withNote} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("── custom");
    expect(frame).toContain("private-llm-v9");
    expect(frame).toContain("not in the curated list");
  });

  it("renders a tip line at the foot", () => {
    // Tip text is i18n-controlled; the slash that anchors the tip
    // is locale-stable.
    const { lastFrame } = render(<ModelCard item={SAMPLE} />);
    expect(lastFrame() ?? "").toContain("/model set");
  });

  it("renders gracefully when sections is empty", () => {
    const empty: ModelCardItem = {
      ...SAMPLE,
      totalCount: 0,
      sections: [],
    };
    const { lastFrame } = render(<ModelCard item={empty} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("◆");
    expect(frame).toMatch(/[·•]\s+\S*\s*0/);
  });
});
