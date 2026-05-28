/**
 * PostmortemSection smoke tests — verify the component renders the
 * markdown body + footer path without crashing, and that ResultCard
 * conditionally mounts/un-mounts it based on item.postmortem presence.
 */
import { render } from "ink-testing-library";
import { describe, expect, it } from "vitest";

import type { ResultItem } from "../../state/types.js";
import { PostmortemSection } from "./PostmortemSection.js";
import { ResultCard } from "./ResultCard.js";

const BASE_ITEM: ResultItem = {
  kind: "result",
  id: "r1",
  taskId: "task-abc12345",
  status: "success",
  faultType: "k8s-chaos-skills",
  bladeUid: "blade-uid-1",
  duration: "47s",
  summary: "Verified",
};

describe("PostmortemSection", () => {
  it("renders the title chip", () => {
    const { lastFrame } = render(
      <PostmortemSection
        markdown={"## Summary\nAll good."}
        path={"/tmp/task-abc.md"}
      />,
    );
    const frame = lastFrame() ?? "";
    // Title text key resolves via i18n; the chip emoji + label should be
    // present in some shape (zh "事后分析" or en "Postmortem").
    expect(frame).toMatch(/事后分析|Postmortem/);
  });

  it("renders the file path footer", () => {
    const { lastFrame } = render(
      <PostmortemSection
        markdown={"## Summary\nx"}
        path={"/Users/x/.blade-ai/postmortems/task-abc.md"}
      />,
    );
    expect(lastFrame() ?? "").toContain("task-abc.md");
  });

  it("renders heading + list + paragraph content", () => {
    const md = [
      "## Summary",
      "HPA scaled within 8s.",
      "",
      "## Recommendations",
      "- raise pod requests",
      "- tune HPA min replicas",
    ].join("\n");
    const { lastFrame } = render(
      <PostmortemSection markdown={md} path={"/tmp/x.md"} />,
    );
    const frame = lastFrame() ?? "";
    expect(frame).toContain("Summary");
    expect(frame).toContain("Recommendations");
    expect(frame).toContain("raise pod requests");
  });

  it("does not throw on malformed markdown", () => {
    const md = "| not | a | table |\n[link](http://x)\n```code```";
    expect(() =>
      render(<PostmortemSection markdown={md} path={"/tmp/x.md"} />),
    ).not.toThrow();
  });
});

describe("ResultCard postmortem integration", () => {
  it("does not render PostmortemSection when item.postmortem is undefined", () => {
    const { lastFrame } = render(<ResultCard item={BASE_ITEM} />);
    const frame = lastFrame() ?? "";
    expect(frame).not.toMatch(/事后分析|Postmortem/);
  });

  it("renders PostmortemSection when item.postmortem is present", () => {
    const item: ResultItem = {
      ...BASE_ITEM,
      postmortem: {
        path: "/Users/x/.blade-ai/postmortems/task-abc12345.md",
        markdown: "## Summary\nAll good.",
        summary: "All good.",
      },
    };
    const { lastFrame } = render(<ResultCard item={item} />);
    const frame = lastFrame() ?? "";
    expect(frame).toMatch(/事后分析|Postmortem/);
    expect(frame).toContain("task-abc12345.md");
  });
});
