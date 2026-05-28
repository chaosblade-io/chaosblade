/**
 * Drives the post-welcome boot phases as a side effect.
 *
 * Sequence:
 *   1. mount → spinner "Running environment self-check…"
 *   2. ``GET /api/v1/preflight`` returns → push boot doctor card,
 *      switch spinner to "Checking pending tasks…"
 *   3. ``GET /api/v1/metric`` returns → push pending tasks card,
 *      hide spinner.
 *
 * Why a dedicated component rather than doing it inline in cli.tsx:
 *   - cli.tsx runs OUTSIDE React; it has to use the module-level
 *     ``dispatchRef`` or similar plumbing to talk to the store after
 *     ``ink.render()`` returns. A useEffect inside the tree is the
 *     idiomatic place for side effects bound to render lifecycle.
 *   - Cancellation cleanup is trivial via the effect's return value —
 *     if Ink unmounts mid-boot (e.g., user Ctrl+C during the spinner),
 *     pending dispatches no-op rather than firing into a torn-down
 *     store.
 *   - Each phase's transition is one ``dispatch`` call — easy to read.
 *
 * The component renders nothing visible. The spinner row is owned by
 * ``MainContent`` reading ``state.bootProgress``.
 */

import { useEffect } from "react";
import type { BladeClient } from "../../api/client.js";
import { t } from "../../i18n/index.js";
import { useAppDispatch } from "../../state/store.js";
import type { HistoryItem } from "../../state/types.js";

export interface BootOrchestratorProps {
  client: BladeClient;
  /** ISO timestamp captured BEFORE the preflight fetch, so the doctor
   *  card's ``capturedAt`` matches when the check started. */
  capturedAt: string;
}

// Soft cap on preflight wait. MUST exceed the server's outer
// ``_PREFLIGHT_BUDGET_S`` (currently 8s); 10s leaves a healthy 2s
// buffer for network + uvicorn dispatch.
const PREFLIGHT_BUDGET_MS = 10_000;

export const BootOrchestrator: React.FC<BootOrchestratorProps> = ({
  client,
  capturedAt,
}) => {
  const dispatch = useAppDispatch();

  useEffect(() => {
    let cancelled = false;

    const run = async () => {
      // ── Phase 1: preflight ─────────────────────────────────────
      dispatch({
        type: "BOOT_PROGRESS_SHOW",
        text: t("boot.progress.preflight"),
      });

      const deadline = new Promise<null>((resolve) =>
        setTimeout(() => resolve(null), PREFLIGHT_BUDGET_MS),
      );
      const preflight = await Promise.race([client.getPreflight(), deadline]);
      if (cancelled) return;

      const doctorItem: HistoryItem = preflight
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
      dispatch({ type: "HISTORY_APPENDED", item: doctorItem });

      // Seed the footer's context indicator with the real model budget
      // so it never flickers from the 128k placeholder to the actual value.
      const ctxMax = preflight?.["context_max_tokens"];
      if (typeof ctxMax === "number" && ctxMax > 0) {
        dispatch({
          type: "CONTEXT_SIZE_RECEIVED",
          currentTokens: 0,
          triggerTokens: 0,
          maxTokens: ctxMax,
          messagesCount: 0,
        });
      }

      // ── Phase 2: pending tasks ─────────────────────────────────
      dispatch({
        type: "BOOT_PROGRESS_SHOW",
        text: t("boot.progress.tasks"),
      });

      const tasksRaw = await client.listTasks().catch(() => null);
      if (cancelled) return;

      // Same canonical "in-flight" set the Python TUI uses
      // (task_store_backend.py:123). Anything outside this set is
      // either still being driven by an active turn (showing it would
      // confuse) or already terminal.
      const PENDING_STATES = new Set(["injecting", "injected"]);
      const allTasks =
        (tasksRaw?.["tasks"] as Array<Record<string, unknown>>) ?? [];
      const pendingTasks = allTasks
        .filter((tt) =>
          PENDING_STATES.has((tt["task_state"] as string) ?? ""),
        )
        .slice(0, 8)
        .map((tt) => ({
          taskId: (tt["task_id"] as string) ?? "?",
          faultType: (tt["fault_type"] as string) ?? "",
          state: (tt["task_state"] as string) ?? "?",
          createdAt: (tt["created_at"] as string) ?? "",
        }));

      const pendingItem: HistoryItem = {
        kind: "pending_tasks_card",
        id: "boot-pending",
        tasks: pendingTasks,
      };
      dispatch({ type: "HISTORY_APPENDED", item: pendingItem });

      dispatch({ type: "BOOT_PROGRESS_HIDE" });
    };

    void run();
    return () => {
      cancelled = true;
    };
  }, [client, capturedAt, dispatch]);

  return null;
};
