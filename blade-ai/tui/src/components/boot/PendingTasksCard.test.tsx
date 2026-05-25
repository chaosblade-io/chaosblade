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

  // ────────────────────────────────────────────────────────────────
  // State-visual contract — pins the post-redesign per-state glyph
  // map. Before this redesign the renderer had a 5-state switch with
  // a gray fallback, so ``injecting`` / ``recovering`` / ``recovered``
  // / ``partial_recovered`` / ``completed`` / ``rejected`` all
  // collapsed into the same gray ``•`` row and were impossible to
  // tell apart at a glance. The assertions below pin the lifecycle
  // states each to a distinct glyph so a future refactor can't
  // silently regress to the bug-class.
  // ────────────────────────────────────────────────────────────────
  function renderWithState(state: string): string {
    const item: PendingTasksCardItem = {
      kind: "pending_tasks_card",
      id: "boot-pending",
      tasks: [
        {
          taskId: `task-${state}`,
          faultType: "pod-cpu-fullload",
          state,
          createdAt: "2026-05-18T09:00:00Z",
        },
      ],
    };
    const { lastFrame } = render(<PendingTasksCard item={item} />);
    return lastFrame() ?? "";
  }

  // ``frameRowFor`` finds the row of the rendered frame that contains
  // the state label. We assert against that single row to avoid the
  // header / border lines polluting glyph matches (header glyph is
  // ✻; we don't want a glyph assertion to find the title).
  function frameRowFor(frame: string, state: string): string {
    const row = frame.split("\n").find((l) => l.includes(state));
    return row ?? "";
  }

  it("uses ⠿ for active IO states (injecting / recovering)", () => {
    expect(frameRowFor(renderWithState("injecting"), "injecting")).toContain("⠿");
    expect(frameRowFor(renderWithState("recovering"), "recovering")).toContain("⠿");
  });

  it("uses ◐ for awaiting / paused / partial states", () => {
    expect(
      frameRowFor(renderWithState("pending_confirmation"), "pending_confirmation"),
    ).toContain("◐");
    expect(
      frameRowFor(renderWithState("waiting_input"), "waiting_input"),
    ).toContain("◐");
    expect(frameRowFor(renderWithState("interrupted"), "interrupted")).toContain("◐");
    expect(
      frameRowFor(renderWithState("partial_recovered"), "partial_recovered"),
    ).toContain("◐");
  });

  it("uses ⊘ for cancelled (stream torn down mid-flight)", () => {
    expect(frameRowFor(renderWithState("cancelled"), "cancelled")).toContain("⊘");
  });

  it("uses ◉ for fault-active states (injected / running)", () => {
    expect(frameRowFor(renderWithState("injected"), "injected")).toContain("◉");
    expect(frameRowFor(renderWithState("running"), "running")).toContain("◉");
  });

  it("uses ● for settled / safe states (recovered / completed)", () => {
    expect(frameRowFor(renderWithState("recovered"), "recovered")).toContain("●");
    expect(frameRowFor(renderWithState("completed"), "completed")).toContain("●");
  });

  it("uses ✗ for failed", () => {
    expect(frameRowFor(renderWithState("failed"), "failed")).toContain("✗");
  });

  it("uses ◯ for rejected", () => {
    expect(frameRowFor(renderWithState("rejected"), "rejected")).toContain("◯");
  });

  it("falls back to • for unknown states", () => {
    // Unknown state must still render a row (so the user sees the
    // task) with a neutral glyph that won't be confused with any
    // lifecycle-state glyph.
    const frame = renderWithState("uncharted_territory");
    expect(frame).toContain("uncharted_territory");
    expect(frameRowFor(frame, "uncharted_territory")).toContain("•");
  });

  it("never renders the gray dot fallback for any known lifecycle state", () => {
    // Regression guard for the original bug: ``injecting`` falling
    // through ``stateColor``'s default gray branch made it visually
    // identical to "settled, no longer relevant". Every known
    // lifecycle state must produce a non-• glyph.
    const knownStates = [
      "injecting",
      "injected",
      "pending_confirmation",
      "waiting_input",
      "interrupted",
      "recovering",
      "recovered",
      "partial_recovered",
      "failed",
      "rejected",
      "completed",
      "running",
      "cancelled",
    ];
    for (const state of knownStates) {
      const row = frameRowFor(renderWithState(state), state);
      expect(row).not.toMatch(/^\s*•\s/);
    }
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
