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
import type { BladeClient } from "@blade-ai/core";
import { t } from "@blade-ai/core";
import { useAppDispatch } from "@blade-ai/core";

import { fetchDoctorCard, fetchPendingCard } from "./bootCards.js";

export interface BootOrchestratorProps {
  client: BladeClient;
  /** ISO timestamp captured BEFORE the preflight fetch, so the doctor
   *  card's ``capturedAt`` matches when the check started. */
  capturedAt: string;
}

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

      const { item: doctorItem, contextMax } = await fetchDoctorCard(
        client,
        capturedAt,
      );
      if (cancelled) return;
      dispatch({ type: "HISTORY_APPENDED", item: doctorItem });

      // Seed the footer's context indicator with the real model budget
      // so it never flickers from the 128k placeholder to the actual value.
      if (contextMax !== null) {
        dispatch({
          type: "CONTEXT_SIZE_RECEIVED",
          currentTokens: 0,
          triggerTokens: 0,
          maxTokens: contextMax,
          messagesCount: 0,
        });
      }

      // ── Phase 2: pending tasks ─────────────────────────────────
      dispatch({
        type: "BOOT_PROGRESS_SHOW",
        text: t("boot.progress.tasks"),
      });

      const pendingItem = await fetchPendingCard(client);
      if (cancelled) return;
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
