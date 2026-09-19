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
import { configureI18n, getActiveLang } from "@blade-ai/core";
import { PendingTasksCard } from "./PendingTasksCard.js";
import type {
  PendingTaskRow,
  PendingTasksCardItem,
} from "@blade-ai/core";

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

// ────────────────────────────────────────────────────────────────
// Round-32b — the three-group split (in_flight / needs_recovery /
// uncleared): the card buckets its liability-live rows by the
// server-legislated ``group`` field and renders one header per
// non-empty bucket, in GROUP_ORDER. Group membership arrives on the
// row (state.py's ``liability_group_for``); rows WITHOUT a group
// (pre-round-32b history payloads) keep the flat legacy layout.
// Language is pinned to en so the header-text assertions are also
// i18n-key-existence assertions (a missing key renders the raw key
// text, failing the assertion).
// ────────────────────────────────────────────────────────────────
describe("PendingTasksCard / three-group split (round-32b)", () => {
  const grouped: PendingTasksCardItem = {
    kind: "pending_tasks_card",
    id: "boot-pending",
    tasks: [
      {
        taskId: "task-flight-1",
        faultType: "cpu_fullload",
        state: "injecting",
        createdAt: "2026-05-18T09:00:00Z",
        group: "in_flight",
      },
      {
        taskId: "task-recover-1",
        faultType: "network_delay",
        state: "failed",
        createdAt: "2026-05-18T09:05:00Z",
        group: "needs_recovery",
      },
      {
        taskId: "task-ghost-1",
        faultType: "disk_fill",
        state: "completed",
        createdAt: "2026-05-18T09:10:00Z",
        group: "uncleared",
      },
    ],
  };

  it("renders one header per non-empty bucket, in GROUP_ORDER", () => {
    const before = getActiveLang();
    configureI18n("en");
    try {
      const { lastFrame } = render(<PendingTasksCard item={grouped} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("In flight");
      expect(frame).toContain("Awaiting recovery");
      expect(frame).toContain("Completed but uncleared");
      // Display order legislated by GROUP_ORDER: quiet traffic first,
      // the ghost family last (loudest).
      const inflight = frame.indexOf("In flight");
      const recover = frame.indexOf("Awaiting recovery");
      const uncleared = frame.indexOf("Completed but uncleared");
      expect(inflight).toBeGreaterThanOrEqual(0);
      expect(recover).toBeGreaterThan(inflight);
      expect(uncleared).toBeGreaterThan(recover);
    } finally {
      configureI18n(before);
    }
  });

  it("buckets each row under its own group's header", () => {
    const before = getActiveLang();
    configureI18n("en");
    try {
      const { lastFrame } = render(<PendingTasksCard item={grouped} />);
      const lines = (lastFrame() ?? "").split("\n");
      const flightIdx = lines.findIndex((l) => l.includes("In flight"));
      const recoverIdx = lines.findIndex((l) => l.includes("Awaiting recovery"));
      const unclearedIdx = lines.findIndex((l) =>
        l.includes("Completed but uncleared"),
      );
      const rowIdx = (id: string) =>
        lines.findIndex((l) => l.includes(id));
      expect(rowIdx("task-flight-1")).toBeGreaterThan(flightIdx);
      expect(rowIdx("task-flight-1")).toBeLessThan(recoverIdx);
      expect(rowIdx("task-recover-1")).toBeGreaterThan(recoverIdx);
      expect(rowIdx("task-recover-1")).toBeLessThan(unclearedIdx);
      expect(rowIdx("task-ghost-1")).toBeGreaterThan(unclearedIdx);
    } finally {
      configureI18n(before);
    }
  });

  it("omits headers for empty buckets", () => {
    const before = getActiveLang();
    configureI18n("en");
    try {
      const item: PendingTasksCardItem = {
        ...grouped,
        tasks: grouped.tasks.slice(0, 1),
      };
      const { lastFrame } = render(<PendingTasksCard item={item} />);
      const frame = lastFrame() ?? "";
      expect(frame).toContain("In flight");
      expect(frame).not.toContain("Awaiting recovery");
      expect(frame).not.toContain("Completed but uncleared");
    } finally {
      configureI18n(before);
    }
  });

  it("renders group-less rows flat (legacy payloads), no headers", () => {
    // Pre-round-32b history items carry no group — the card must NOT
    // force-bucket them (an unknown group is not "in flight").
    const before = getActiveLang();
    configureI18n("en");
    try {
      const { lastFrame } = render(<PendingTasksCard item={POPULATED} />);
      const frame = lastFrame() ?? "";
      // POPULATED's two rows have no group: flat rows, zero headers.
      expect(frame).toContain("task-abc-12345");
      expect(frame).toContain("task-def-67890");
      expect(frame).not.toContain("In flight");
      expect(frame).not.toContain("Awaiting recovery");
      expect(frame).not.toContain("Completed but uncleared");
    } finally {
      configureI18n(before);
    }
  });

  it("degrades an unknown group word to the flat layout (fail-safe, no crash)", () => {
    // F-1 (round-32b self-review): the TS literal union is a
    // compile-time claim; the wire field is RUNTIME data. A server
    // that legislated a FOURTH group word (or a newer server paired
    // with an older TUI binary in the independent-distribution
    // upgrade window) delivers a word this build never knew — the
    // card must degrade that row to the flat legacy layout, never
    // throw (the boot-card "never worth failing a boot over" contract,
    // extended to the render face). Before the isGroupKey guard this
    // shape crashed the whole card on ``buckets[unknown].push``.
    const before = getActiveLang();
    configureI18n("en");
    try {
      const tasks: PendingTaskRow[] = [
        {
          taskId: "task-known",
          faultType: "cpu_fullload",
          state: "failed",
          createdAt: "2026-05-18T09:00:00Z",
          group: "needs_recovery",
        },
        {
          taskId: "task-unknown-group",
          faultType: "disk_fill",
          state: "completed",
          createdAt: "2026-05-18T09:05:00Z",
          // Deliberately outside the legislated union — the runtime
          // shape a vocabulary drift actually delivers.
          group: "fourth_epoch" as unknown as PendingTaskRow["group"],
        },
      ];
      const item: PendingTasksCardItem = {
        kind: "pending_tasks_card",
        id: "boot-pending",
        tasks,
      };
      const { lastFrame } = render(<PendingTasksCard item={item} />);
      const frame = lastFrame() ?? "";
      // The known bucket still renders with its header…
      expect(frame).toContain("Awaiting recovery");
      expect(frame).toContain("task-known");
      // …the unknown word degrades to a flat row (rendered, no throw)…
      expect(frame).toContain("task-unknown-group");
      // …and no phantom header for a group this build never legislated.
      expect(frame).not.toContain("fourth_epoch");
    } finally {
      configureI18n(before);
    }
  });
});
