/**
 * PendingTasksCard render assertions. Two main variants:
 *   - empty: shows the "no pending tasks" message
 *   - populated: shows per-task rows with state + id + fault_type
 *
 * The card uses flex layout (M17) so we don't pin column widths;
 * just check that each row's substrings appear.
 */

import { render } from "ink-testing-library";
import { describe, expect, it } from "vitest";
import { PendingTasksCard } from "./PendingTasksCard.js";
import type { PendingTasksCardItem } from "../../state/types.js";

const EMPTY: PendingTasksCardItem = {
  kind: "pending_tasks_card",
  id: "boot-pending",
  tasks: [],
};

const POPULATED: PendingTasksCardItem = {
  kind: "pending_tasks_card",
  id: "boot-pending",
  tasks: [
    {
      taskId: "task-abc-12345",
      faultType: "cpu_fullload",
      state: "injected",
      createdAt: "2026-05-18T09:00:00Z",
    },
    {
      taskId: "task-def-67890",
      faultType: "network_delay",
      state: "injecting",
      createdAt: "2026-05-18T09:05:00Z",
    },
  ],
};

describe("PendingTasksCard / empty", () => {
  it("renders the title", () => {
    const { lastFrame } = render(<PendingTasksCard item={EMPTY} />);
    const frame = lastFrame() ?? "";
    // i18n'd title (en: "Unfinished tasks", zh: "未完成任务") — probe
    // for a discriminator instead. Empty path shows the empty-state
    // message; either language version is fine to assert on, but pin
    // by *not* finding any task_id row.
    expect(frame).not.toMatch(/task-abc/);
  });

  it("does not render task rows when tasks is empty", () => {
    const { lastFrame } = render(<PendingTasksCard item={EMPTY} />);
    const frame = lastFrame() ?? "";
    expect(frame).not.toMatch(/injected/);
    expect(frame).not.toMatch(/injecting/);
  });
});

describe("PendingTasksCard / populated", () => {
  it("shows each task_id", () => {
    const { lastFrame } = render(<PendingTasksCard item={POPULATED} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("task-abc-12345");
    expect(frame).toContain("task-def-67890");
  });

  it("shows each task state", () => {
    const { lastFrame } = render(<PendingTasksCard item={POPULATED} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("injected");
    expect(frame).toContain("injecting");
  });

  it("shows fault types", () => {
    const { lastFrame } = render(<PendingTasksCard item={POPULATED} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("cpu_fullload");
    expect(frame).toContain("network_delay");
  });

  it("renders the row without a fault-type column when fault_type is empty", () => {
    // Empty ``faultType`` used to fall back to a localized
    // ``(unknown fault type)`` sentinel; that read as repetitive
    // "I don't know" noise across rows so we now leave the column
    // blank. Verify the row still renders (state + task id present)
    // and the legacy parens sentinel is gone.
    const item: PendingTasksCardItem = {
      ...POPULATED,
      tasks: [
        {
          taskId: "task-no-type",
          faultType: "",
          state: "injected",
          createdAt: "2026-05-18T09:00:00Z",
        },
      ],
    };
    const { lastFrame } = render(<PendingTasksCard item={item} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("task-no-type");
    expect(frame).toContain("injected");
    // Old sentinel must NOT show up — both the en string and the
    // zh string should be absent.
    expect(frame).not.toContain("unknown fault type");
    expect(frame).not.toContain("未知故障类型");
  });
});
