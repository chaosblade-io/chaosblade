/**
 * MaxSizedBox truncation tests.
 *
 * Each test renders a fixed number of single-line ``<Box><Text>``
 * children inside a MaxSizedBox with a known cap, then asserts the
 * truncation indicator + visible count via the rendered frame string.
 * The OverflowProvider wrap exercises the overflow signal path even
 * though we don't read the resulting context here — its purpose is
 * to prove the component works inside the wiring it'll see in
 * production.
 */

import { Box, Text } from "ink";
import { render } from "ink-testing-library";
import { describe, expect, it } from "vitest";
import { MaxSizedBox } from "./MaxSizedBox.js";
import { OverflowProvider } from "../../contexts/OverflowContext.js";

const wrap = (node: React.ReactNode) =>
  render(<OverflowProvider>{node}</OverflowProvider>);

const lineRow = (n: number) => (
  <Box key={n}>
    <Text>{`line-${n}`}</Text>
  </Box>
);

describe("MaxSizedBox", () => {
  it("renders all children when count <= maxHeight", () => {
    const { lastFrame } = wrap(
      <MaxSizedBox maxHeight={10}>
        {Array.from({ length: 5 }, (_, i) => lineRow(i))}
      </MaxSizedBox>,
    );
    const frame = lastFrame() ?? "";
    expect(frame).toContain("line-0");
    expect(frame).toContain("line-4");
    expect(frame).not.toMatch(/lines folded|行被折叠/);
  });

  it("truncates the head when count > maxHeight, keeping the tail", () => {
    const { lastFrame } = wrap(
      <MaxSizedBox maxHeight={3} overflowId="tool-T1">
        {Array.from({ length: 10 }, (_, i) => lineRow(i))}
      </MaxSizedBox>,
    );
    const frame = lastFrame() ?? "";
    // Only last (maxHeight - 1 = 2) lines + 1 indicator row should
    // be visible.
    expect(frame).not.toContain("line-0");
    expect(frame).not.toContain("line-7");
    expect(frame).toContain("line-8");
    expect(frame).toContain("line-9");
    // 8 lines folded = 10 - (3 - 1).
    expect(frame).toMatch(/8.*行被折叠|8.*lines folded/);
  });

  it("treats undefined maxHeight as no cap", () => {
    const { lastFrame } = wrap(
      <MaxSizedBox>
        {Array.from({ length: 100 }, (_, i) => lineRow(i))}
      </MaxSizedBox>,
    );
    const frame = lastFrame() ?? "";
    // Every line is rendered, no indicator.
    expect(frame).toContain("line-0");
    expect(frame).toContain("line-99");
    expect(frame).not.toMatch(/lines folded|行被折叠/);
  });

  it("clamps maxHeight to MIN_CAP=2 when caller passes a smaller value", () => {
    const { lastFrame } = wrap(
      <MaxSizedBox maxHeight={1}>
        {Array.from({ length: 5 }, (_, i) => lineRow(i))}
      </MaxSizedBox>,
    );
    const frame = lastFrame() ?? "";
    // MIN_CAP=2 → 1 indicator + 1 visible row.
    expect(frame).toContain("line-4");
    expect(frame).not.toContain("line-3");
    expect(frame).toMatch(/4.*行被折叠|4.*lines folded/);
  });
});
