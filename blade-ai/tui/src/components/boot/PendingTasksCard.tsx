/**
 * Boot-time card listing tasks whose liability verdict says the fault
 * may still be live (round-32), split into THREE display groups
 * (round-32b) by what the row itself claims vs what the ledger says:
 * in-flight (drill still running) / awaiting recovery (run stopped,
 * fault on the books) / completed-but-uncleared (a settled word over
 * a live ledger — the ghost family). When empty, shows a single "no
 * pending tasks" line; each row carries task_id + state + fault_type
 * so the user can `/replay <id>` or `blade-ai recover <id>` to resume.
 */

import { Box, Text } from "ink";
import { memo } from "react";
import { t } from "@blade-ai/core";
import type { PendingTasksCardItem, PendingTaskRow } from "@blade-ai/core";
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
  // Tier 2½ — fault effect UNCONFIRMED (round-17 C1): verification ran
  // but evidence is unavailable, so the fault is suspected LIVE
  // (fail-closed: “不知道 ≠ 不在”). These rows enter the card via the
  // liability verdict filter (round-32: ``liability_live`` keeps
  // unverified rows recoverable by evidence, not by word membership)
  // and MUST read as "you should look at this", NOT the gray fallback —
  // the header's "only rejected renders gray" invariant stays true.
  unverified: { color: Theme.status.warn, glyph: "◌" },
  // Tier 2½ — newborn anchor (round-17 D4): a row with zero lifecycle
  // evidence has not entered its pipeline yet — neutral-faint family:
  // not urgent (nothing has fired), but not gray-fallback either
  // (the row exists and deserves a distinguishable dot).
  pending: { color: Theme.gray[500], glyph: "◌" },
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

// Round-32b — the three-group split of liability-live rows, in display
// order. Loudness tracks the ledger-vs-word mismatch: in_flight is
// normal traffic (quiet secondary), needs_recovery asks for action
// (amber), uncleared is the ghost — the row CLAIMS settlement while
// the wings stay unbalanced (err red, loudest). Group membership is
// legislated server-side (``liability_group_for`` in state.py) and
// arrives on the row; this table holds presentation only, never the
// word→group derivation (the PENDING_STATES drift family stays
// retired).
const GROUP_ORDER = ["in_flight", "needs_recovery", "uncleared"] as const;
type GroupKey = (typeof GROUP_ORDER)[number];

const GROUP_META: Record<GroupKey, { i18nKey: string; color: string; glyph: string }> = {
  in_flight: {
    i18nKey: "boot.pending.group_in_flight",
    color: Theme.text.secondary,
    glyph: "⠿",
  },
  needs_recovery: {
    i18nKey: "boot.pending.group_needs_recovery",
    color: Theme.status.warn,
    glyph: "◉",
  },
  uncleared: {
    i18nKey: "boot.pending.group_uncleared",
    color: Theme.status.err,
    glyph: "⚠",
  },
};

function isGroupKey(value: string | undefined): value is GroupKey {
  return (
    value !== undefined && (GROUP_ORDER as readonly string[]).includes(value)
  );
}

function TaskRow({
  row,
  indent,
}: {
  row: PendingTaskRow;
  indent: number;
}): React.ReactElement {
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
    <Box marginLeft={indent}>
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
}

const PendingTasksCardInternal: React.FC<{ item: PendingTasksCardItem }> = ({
  item,
}) => {
  // Round-32b — bucket the rows by the server-legislated group. Rows
  // WITHOUT one (history payloads persisted before round-32b, or a
  // server predating the field) keep the flat legacy layout instead of
  // being force-bucketed — an unknown group is not "in flight".
  const buckets: Record<GroupKey, PendingTaskRow[]> = {
    in_flight: [],
    needs_recovery: [],
    uncleared: [],
  };
  const legacy: PendingTaskRow[] = [];
  for (const row of item.tasks) {
    // Fail-safe (round-32b F-1): the TS literal union is a compile-time
    // claim, but the field is RUNTIME wire data — a server vocabulary
    // drift (or a newer server paired with an older TUI binary in the
    // independent-distribution upgrade window) can deliver a group
    // word this build never legislated. ``buckets[unknownWord]`` is
    // undefined and ``undefined.push`` would crash the whole boot card;
    // the unknown word degrades to the flat legacy layout instead,
    // mirroring the fetcher's "a boot card is never worth failing a
    // boot over" contract on the render face.
    if (isGroupKey(row.group)) {
      buckets[row.group].push(row);
    } else {
      legacy.push(row);
    }
  }
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
        <>
          {GROUP_ORDER.filter((g) => buckets[g].length > 0).map((g) => (
            <Box key={g} flexDirection="column" marginBottom={1}>
              <Box marginLeft={1}>
                <Text color={GROUP_META[g].color} bold>
                  {GROUP_META[g].glyph} {t(GROUP_META[g].i18nKey)}
                </Text>
              </Box>
              {buckets[g].map((row) => (
                <TaskRow key={row.taskId} row={row} indent={2} />
              ))}
            </Box>
          ))}
          {legacy.length > 0
            ? legacy.map((row) => (
                <TaskRow key={row.taskId} row={row} indent={0} />
              ))
            : null}
        </>
      )}
    </BootCardFrame>
  );
};

// React.memo: pending-tasks payload is captured once during the boot
// sequence; item ref never changes after dispatch.
export const PendingTasksCard = memo(PendingTasksCardInternal);
