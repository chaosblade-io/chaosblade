/**
 * bootCards fetcher contract — the extraction shared by
 * BootOrchestrator (fresh boot) and BootRunner's resume branch.
 *
 * The failure semantics are the load-bearing part: a boot card is
 * decoration for the CURRENT environment, never worth failing a
 * boot over, so every fetch failure must degrade to a placeholder
 * (doctor: ``unavailable``; pending: empty task list) instead of
 * throwing. The preflight rejection path is the one the ORIGINAL
 * inline BootOrchestrator code let escape as an unhandled promise
 * rejection — pin it so the extraction's fix cannot regress.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BladeClient } from "@blade-ai/core";

import {
  fetchDoctorCard,
  fetchPendingCard,
  PREFLIGHT_BUDGET_MS,
} from "./bootCards.js";

// Fake timers for the whole file: the doctor fetcher arms a
// PREFLIGHT_BUDGET_MS race timer on every call, and the timeout
// test needs to advance it instantly instead of waiting 17s —
// while the other tests must not leak a real 17s timer into the
// worker teardown.
beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

const client = (overrides: Record<string, unknown>): BladeClient =>
  ({ ...overrides }) as unknown as BladeClient;

const PREFLIGHT = {
  passed_count: 3,
  total_count: 4,
  context_max_tokens: 131072,
  checks: [
    {
      name: "kubectl",
      severity: "blocking",
      passed: true,
      message: "ok",
      fix: "",
    },
    // Minimal shape: severity/message/fix must default, not explode.
    { name: "chaosblade", passed: false },
  ],
};

describe("fetchDoctorCard", () => {
  it("maps a successful preflight payload onto the card", async () => {
    const { item, contextMax } = await fetchDoctorCard(
      client({ getPreflight: async () => PREFLIGHT }),
      "t0",
    );
    expect(item).toMatchObject({
      kind: "boot_doctor_card",
      id: "boot-doctor",
      capturedAt: "t0",
      passedCount: 3,
      totalCount: 4,
    });
    expect(item.kind === "boot_doctor_card" && item.unavailable).toBe(
      undefined,
    );
    expect(
      item.kind === "boot_doctor_card" && item.checks,
    ).toMatchObject([
      { name: "kubectl", severity: "blocking", passed: true, message: "ok" },
      { name: "chaosblade", severity: "warning", passed: false, message: "" },
    ]);
    expect(contextMax).toBe(131072);
  });

  it("falls back to checks.length when total_count is missing", async () => {
    const { total_count, ...noTotal } = PREFLIGHT;
    expect(total_count).toBe(4); // linter: destructure is intentional
    const { item } = await fetchDoctorCard(
      client({ getPreflight: async () => noTotal }),
      "t0",
    );
    expect(item.kind === "boot_doctor_card" && item.totalCount).toBe(2);
  });

  it("returns a null contextMax for a non-positive budget", async () => {
    const { item, contextMax } = await fetchDoctorCard(
      client({
        getPreflight: async () => ({
          checks: [],
          context_max_tokens: 0,
        }),
      }),
      "t0",
    );
    expect(item.kind).toBe("boot_doctor_card");
    expect(contextMax).toBeNull();
  });

  it("degrades to the unavailable card when the endpoint rejects", async () => {
    const { item, contextMax } = await fetchDoctorCard(
      client({ getPreflight: () => Promise.reject(new Error("net down")) }),
      "t0",
    );
    expect(item).toMatchObject({
      kind: "boot_doctor_card",
      id: "boot-doctor",
      capturedAt: "t0",
      unavailable: true,
      passedCount: 0,
      totalCount: 0,
    });
    expect(item.kind === "boot_doctor_card" && item.checks).toEqual([]);
    expect(contextMax).toBeNull();
  });

  it("degrades to the unavailable card when the budget timer wins", async () => {
    const pending = fetchDoctorCard(
      client({ getPreflight: () => new Promise(() => {}) }),
      "t0",
    );
    await vi.advanceTimersByTimeAsync(PREFLIGHT_BUDGET_MS);
    const { item, contextMax } = await pending;
    expect(item.kind === "boot_doctor_card" && item.unavailable).toBe(true);
    expect(contextMax).toBeNull();
  });
});

describe("fetchPendingCard", () => {
  it("keeps only liability-carrying tasks, capped at 8, mapped to card rows", async () => {
    const tasks: Record<string, unknown>[] = [];
    for (let i = 0; i < 10; i += 1) {
      tasks.push({
        task_id: `task-${i}`,
        fault_type: "cpu_fullload",
        task_state: "injected",
        // Round-32: the pending list keys on the materialised liability
        // verdict, not the task_state word.
        liability_live: true,
        created_at: "2026-05-18T09:00:00Z",
      });
    }
    // Terminal-state / cleared tasks must not surface as "pending".
    tasks.push({
      task_id: "task-done",
      fault_type: "cpu_fullload",
      task_state: "success",
      liability_live: false,
      created_at: "2026-05-18T09:00:00Z",
    });
    // Mid-flight words (previously hidden by the word-set filter) surface
    // when the ledger verdict says the fault may still be live — the
    // round-32 K1/K2 blindness, from the display side. Placed FIRST so
    // the slice(0, 8) cap cannot eat it.
    tasks.unshift({
      task_id: "task-mid-recovery",
      fault_type: "cpu_fullload",
      task_state: "recovering",
      liability_live: true,
      created_at: "2026-05-18T09:00:00Z",
    });
    const item = await fetchPendingCard(
      client({ listTasks: async () => ({ tasks }) }),
    );
    expect(item.kind).toBe("pending_tasks_card");
    if (item.kind !== "pending_tasks_card") return;
    expect(item.tasks).toHaveLength(8);
    expect(item.tasks[0]).toMatchObject({
      taskId: "task-mid-recovery",
      faultType: "cpu_fullload",
      state: "recovering",
      createdAt: "2026-05-18T09:00:00Z",
    });
    expect(item.tasks[1]).toMatchObject({
      taskId: "task-0",
      faultType: "cpu_fullload",
      state: "injected",
      createdAt: "2026-05-18T09:00:00Z",
    });
    expect(item.tasks.some((task) => task.taskId === "task-done")).toBe(
      false,
    );
    expect(
      item.tasks.some((task) => task.taskId === "task-mid-recovery"),
    ).toBe(true);
  });

  it("degrades to the empty card when the endpoint rejects", async () => {
    const item = await fetchPendingCard(
      client({ listTasks: () => Promise.reject(new Error("net down")) }),
    );
    expect(item.kind).toBe("pending_tasks_card");
    expect(item.kind === "pending_tasks_card" && item.tasks).toEqual([]);
  });

  // Round-32b — the server-legislated group passthrough: the boot
  // card buckets its rows by ``liability_group`` (in_flight /
  // needs_recovery / uncleared) and this layer must DERIVE nothing
  // from the task_state word (the PENDING_STATES drift family stays
  // retired) — the field arrives on the row or it doesn't.
  it("passes the server-legislated liability_group through, verbatim", async () => {
    const tasks: Record<string, unknown>[] = [
      {
        task_id: "task-inflight",
        fault_type: "cpu_fullload",
        task_state: "injecting",
        liability_live: true,
        liability_group: "in_flight",
        created_at: "2026-05-18T09:00:00Z",
      },
      {
        task_id: "task-needs-recovery",
        fault_type: "cpu_fullload",
        task_state: "failed",
        liability_live: true,
        liability_group: "needs_recovery",
        created_at: "2026-05-18T09:00:00Z",
      },
      {
        task_id: "task-uncleared",
        fault_type: "cpu_fullload",
        task_state: "completed",
        liability_live: true,
        liability_group: "uncleared",
        created_at: "2026-05-18T09:00:00Z",
      },
      // Dead rows are filtered out before the group even matters.
      {
        task_id: "task-dead",
        fault_type: "cpu_fullload",
        task_state: "rejected",
        liability_live: false,
        liability_group: null,
        created_at: "2026-05-18T09:00:00Z",
      },
      // Pre-round-32b server payloads lack the field entirely — the
      // card renders such rows flat (legacy layout), never guessed.
      {
        task_id: "task-legacy",
        fault_type: "cpu_fullload",
        task_state: "injected",
        liability_live: true,
        created_at: "2026-05-18T09:00:00Z",
      },
    ];
    const item = await fetchPendingCard(
      client({ listTasks: async () => ({ tasks }) }),
    );
    expect(item.kind).toBe("pending_tasks_card");
    if (item.kind !== "pending_tasks_card") return;
    const byId = new Map(item.tasks.map((row) => [row.taskId, row]));
    expect(byId.get("task-inflight")?.group).toBe("in_flight");
    expect(byId.get("task-needs-recovery")?.group).toBe("needs_recovery");
    expect(byId.get("task-uncleared")?.group).toBe("uncleared");
    expect(byId.get("task-legacy")?.group).toBeUndefined();
    expect(byId.has("task-dead")).toBe(false);
  });
});
