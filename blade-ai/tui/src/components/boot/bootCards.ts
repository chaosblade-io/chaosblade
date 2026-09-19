/**
 * Boot-card fetchers shared by the two boot surfaces.
 *
 * ``BootOrchestrator`` (fresh boot) and ``BootRunner``'s resume branch
 * (``blade-ai resume -i <sid>``) both need the SAME two cards — the
 * environment self-check (doctor) and the pending-injection list —
 * built from the same endpoints with the same row shapes. Extracting
 * the fetch + mapping keeps the two surfaces from drifting apart; the
 * only difference is WHEN the cards land in history:
 *
 *   - fresh boot: after the welcome card, via BootOrchestrator's
 *     phased effect (spinner row in between);
 *   - resume boot: BEFORE the replay's dispatches, so the cards sit
 *     at the head of the rebuilt history (fresh-boot visual parity:
 *     welcome → doctor → pending → replayed turns) instead of being
 *     appended after N replayed events.
 *
 * Failure semantics: both fetchers degrade to a placeholder card
 * (doctor: ``unavailable``; pending: empty task list) rather than
 * throwing — a boot card is decoration for the CURRENT environment,
 * never worth failing a boot over.
 */

import type { BladeClient, HistoryItem, PendingTaskRow } from "@blade-ai/core";

// Soft cap on preflight wait. MUST exceed the server's outer
// ``_PREFLIGHT_BUDGET_S`` (currently 15s); 17s leaves a healthy 2s
// buffer for network + uvicorn dispatch.
export const PREFLIGHT_BUDGET_MS = 17_000;

export interface DoctorCardFetch {
  /** The ``boot_doctor_card`` history item (``unavailable`` shape when
   *  the preflight endpoint timed out or errored). */
  item: HistoryItem;
  /** Server-advertised context budget for the footer's context
   *  indicator, or ``null`` when unknown — the CALLER dispatches
   *  ``CONTEXT_SIZE_RECEIVED`` (fresh boot's BootOrchestrator and the
   *  resume branch both need it; keeping the dispatch at the call
   *  site preserves each surface's own ordering). */
  contextMax: number | null;
}

/** Round-32: the pending list filters on the materialised liability
 * verdict (``tasks.liability_live``, server-exposed as
 * ``liability_live`` on every list row) instead of re-guessing from
 * task_state words. The old PENDING_STATES word copy (round-16 S1:
 * pinned verbatim against TASK_STATE_ACTIVE_VALUES after it had already
 * drifted) was the display twin of the same word-guessing predicate the
 * SQL layer just retired — every new lifecycle word (recovering,
 * failed-with-experiment, partial_recovered) needed a fresh human
 * re-derivation of "is the fault still live under this word", and the
 * two that lost silently hid live faults. The verdict column absorbs
 * that judgment at write time, from evidence.
 *
 * Round-32b: the surviving rows split into THREE display groups
 * (in_flight / needs_recovery / uncleared), legislated server-side
 * (``liability_group_for`` in state.py, exposed as ``liability_group``)
 * so this layer derives nothing from the word — the passthrough below
 * carries no word copy. */

export async function fetchDoctorCard(
  client: BladeClient,
  capturedAt: string,
): Promise<DoctorCardFetch> {
  // The catch → null makes a network failure take the same
  // ``unavailable`` path as the budget timeout — the old inline
  // version let the rejection escape as an unhandled promise
  // rejection from the fire-and-forget effect.
  const preflight = await Promise.race([
    client.getPreflight().catch(() => null),
    new Promise<null>((resolve) =>
      setTimeout(() => resolve(null), PREFLIGHT_BUDGET_MS),
    ),
  ]);

  const item: HistoryItem = preflight
    ? {
        kind: "boot_doctor_card",
        id: "boot-doctor",
        capturedAt,
        passedCount: (preflight["passed_count"] as number) ?? 0,
        totalCount:
          (preflight["total_count"] as number) ??
          ((preflight["checks"] as Array<unknown>) ?? []).length,
        checks: (
          (preflight["checks"] as Array<Record<string, unknown>>) ?? []
        ).map((c) => ({
          name: (c["name"] as string) ?? "",
          severity: ((c["severity"] as string) ?? "warning") as
            | "blocking"
            | "warning",
          passed: Boolean(c["passed"]),
          message: (c["message"] as string) ?? "",
          fix: (c["fix"] as string) ?? "",
        })),
      }
    : {
        kind: "boot_doctor_card",
        id: "boot-doctor",
        capturedAt,
        passedCount: 0,
        totalCount: 0,
        checks: [],
        unavailable: true,
      };

  const ctxMax = preflight?.["context_max_tokens"];
  return {
    item,
    contextMax: typeof ctxMax === "number" && ctxMax > 0 ? ctxMax : null,
  };
}

export async function fetchPendingCard(
  client: BladeClient,
): Promise<HistoryItem> {
  const tasksRaw = await client.listTasks().catch(() => null);
  const allTasks =
    (tasksRaw?.["tasks"] as Array<Record<string, unknown>>) ?? [];
  const pendingTasks = allTasks
    .filter((tt) => tt["liability_live"] === true)
    .slice(0, 8)
    .map((tt) => ({
      taskId: (tt["task_id"] as string) ?? "?",
      faultType: (tt["fault_type"] as string) ?? "",
      state: (tt["task_state"] as string) ?? "?",
      createdAt: (tt["created_at"] as string) ?? "",
      // Round-32b — server-legislated group passthrough (in_flight /
      // needs_recovery / uncleared). ``undefined`` for dead rows and
      // pre-round-32b payloads; the card renders those flat.
      group: (tt["liability_group"] as PendingTaskRow["group"]) ?? undefined,
    }));

  return {
    kind: "pending_tasks_card",
    id: "boot-pending",
    tasks: pendingTasks,
  };
}
