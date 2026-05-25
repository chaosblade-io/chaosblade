/**
 * Boot-time card listing tasks in non-terminal states. When empty,
 * shows a single "no pending tasks" line; when non-empty, lists
 * task_id + state + fault_type per row so the user can `/replay <id>`
 * or `blade-ai recover <id>` to resume.
 */

import { Box, Text } from "ink";
import { memo } from "react";
import { t } from "../../i18n/index.js";
import type { PendingTasksCardItem } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";
import { BootCardFrame } from "./BootCardFrame.js";

/**
 * Per-state visual: colour + leading glyph + bold flag.
 *
 * Sorted by "how much should the user care?" — top of the file is the
 * loudest visual treatment, bottom is the quietest. The 13 covered
 * states are the union of:
 *
 *   - ``infer_task_state`` lifecycle outputs from the Python side
 *     (``src/chaos_agent/agent/state.py``): injecting · injected ·
 *     recovering · recovered · partial_recovered · failed · rejected ·
 *     completed.
 *   - ``TaskStore._compute_task_state`` overlay
 *     (``src/chaos_agent/persistence/task_store.py:427-430``):
 *     waiting_input.
 *   - Server lifecycle overlays from confirm/turn routes
 *     (``confirm.py:33`` / ``turn.py:825``): cancelled.
 *   - Legacy / defensive aliases retained for SQLite rows persisted
 *     by older backend versions: pending_confirmation · interrupted
 *     · running.
 *
 * Why same colour, different glyph in some buckets:
 *   - ``injecting / recovering`` (active IO) and ``pending_confirmation``
 *     (user input awaited) are equally "you should look at this", so
 *     both wear ``forge.fire + bold``. The glyph distinguishes the
 *     intent: ``⠿`` reads as "thing in motion", ``◐`` as "thing in
 *     wait state".
 *   - ``injected`` (fault active, run completed) and ``interrupted /
 *     partial_recovered`` (fault somewhere mid-cycle) share amber
 *     because both mean "not settled, not in motion either"; ``◉``
 *     vs ``◐`` flags whether it's at rest with a live fault or
 *     genuinely paused.
 *
 * ``rejected`` is the ONLY state that intentionally renders gray —
 * the safety check stopped this drill before it ran, so there's
 * nothing the user can / should do with the row except note it. Every
 * other "in motion" / "settled" state has an explicit non-gray colour
 * so the previous bug (``injecting`` falling through the default and
 * disappearing visually as gray) cannot recur.
 */
type Visual = { color: string; glyph: string; bold?: boolean };

const STATE_VISUALS: Record<string, Visual> = {
  // Tier 1 — active IO, brand orange + bold.
  injecting: { color: Theme.forge.fire, glyph: "⠿", bold: true },
  recovering: { color: Theme.forge.fire, glyph: "⠿", bold: true },
  // Tier 1 — user input awaited, same urgency family.
  // ``waiting_input`` is the persistence-layer overlay emitted by
  // ``TaskStore._compute_task_state`` when a task pauses at an
  // interrupt boundary (``task_state.py:427-430``); semantically
  // identical to ``pending_confirmation`` so it shares the visual.
  pending_confirmation: { color: Theme.forge.fire, glyph: "◐", bold: true },
  waiting_input: { color: Theme.forge.fire, glyph: "◐", bold: true },
  // Tier 2 — fault active, awaiting recovery.
  injected: { color: Theme.status.warn, glyph: "◉" },
  running: { color: Theme.status.warn, glyph: "◉" },
  // Tier 2 — paused / partial cleanup / stream-torn-down.
  // ``cancelled`` is emitted by ``server/routes/turn.py:825`` when
  // the SSE stream gets torn down before the graph reaches
  // save_memory; the fault state is genuinely indeterminate (may or
  // may not have fired) so amber + a "stopped before completion"
  // glyph reads more correctly than ``failed`` (which implies an
  // active error) or ``rejected`` (which implies safety pre-block).
  interrupted: { color: Theme.status.warn, glyph: "◐" },
  partial_recovered: { color: Theme.status.warn, glyph: "◐" },
  cancelled: { color: Theme.status.warn, glyph: "⊘" },
  // Tier 3 — settled / safe.
  recovered: { color: Theme.status.ok, glyph: "●" },
  completed: { color: Theme.status.ok, glyph: "●" },
  // Tier 4 — failure.
  failed: { color: Theme.status.err, glyph: "✗", bold: true },
  // Tier 5 — dismissed by safety check; only state that stays gray.
  rejected: { color: Theme.gray[500], glyph: "◯" },
};

const FALLBACK: Visual = { color: Theme.gray[500], glyph: "•" };

function stateVisual(state: string): Visual {
  return STATE_VISUALS[state] ?? FALLBACK;
}

const PendingTasksCardInternal: React.FC<{ item: PendingTasksCardItem }> = ({
  item,
}) => {
  return (
    <BootCardFrame>
      <Box marginBottom={1}>
        <Text color={Theme.text.accent} bold>
          {Icons.thinking} {t("boot.pending.title")}
        </Text>
      </Box>
      {item.tasks.length === 0 ? (
        <Box>
          <Text color={Theme.text.secondary}>{t("boot.pending.empty")}</Text>
        </Box>
      ) : (
        item.tasks.map((row) => {
          // Glyph fixed-width + state fixed-width + task_id flexible
          // + fault_type fills remaining space. task_id is the most
          // valuable column for /replay / blade-ai recover invocations,
          // so we give it the bigger share via flexGrow=2.
          //
          // Width note: the state column was 16 cols when the only
          // displayed states were ``injected``/``running``/``failed``;
          // the redesigned palette covers ``pending_confirmation`` and
          // ``partial_recovered`` (20 chars each) so widen to 22 to
          // keep all rows aligned without truncation. flexShrink=0
          // protects the column under narrow terminals.
          const v = stateVisual(row.state);
          return (
            <Box key={row.taskId}>
              <Box minWidth={3} flexShrink={0}>
                <Text color={v.color} bold={v.bold}>
                  {v.glyph}
                </Text>
              </Box>
              <Box minWidth={22} flexShrink={0}>
                <Text color={v.color} bold={v.bold}>
                  {row.state}
                </Text>
              </Box>
              <Box flexGrow={2} flexBasis={0} paddingRight={2}>
                <Text color={Theme.text.primary} wrap="truncate-end">
                  {row.taskId}
                </Text>
              </Box>
              {row.faultType ? (
                <Box flexGrow={1} flexBasis={0}>
                  <Text color={Theme.text.secondary} wrap="truncate-end">
                    {row.faultType}
                  </Text>
                </Box>
              ) : (
                <Box flexGrow={1} flexBasis={0} />
              )}
            </Box>
          );
        })
      )}
    </BootCardFrame>
  );
};

// React.memo: pending-tasks payload is captured once during the boot
// sequence; item ref never changes after dispatch.
export const PendingTasksCard = memo(PendingTasksCardInternal);
