/**
 * Conversation main area — Static + pending double layer.
 *
 * Why two layers:
 *   - Ink's ``<Static>`` component writes children to the terminal once
 *     and never re-renders them. That's the burn-in mechanism we want
 *     for committed history — no flicker, no per-token re-paint.
 *   - The trailing ``pending`` slice (current turn) sits below Static
 *     in a normal Box. It re-renders on each token / tool transition.
 *     When TURN_DONE fires, the reducer slices ``pending`` into
 *     ``history``, Static absorbs the new items, and pending clears.
 *
 * Static accepts an array of children plus a render prop. We build the
 * children array from session header + history items so the very first
 * thing in scrollback is the greeting (it'll never re-render either).
 */

import React from "react";
import { Box, Static } from "ink";
import { BootProgress } from "./boot/BootProgress.js";
import { Header } from "./Header.js";
import { HistoryItemDisplay } from "./HistoryItemDisplay.js";
import { useAppSelector } from "../state/store.js";
import { useTerminalSize } from "../hooks/useTerminalSize.js";
import { OverflowProvider } from "../contexts/OverflowContext.js";
import { ShowMoreLines } from "./shared/ShowMoreLines.js";
import { setProbePendingRef } from "../utils/overflowProbe.js";

interface Props {
  version: string;
  serverUrl: string;
}

interface StaticEntry {
  key: string;
  node: React.ReactNode;
}

/**
 * Static estimate of how many rows the chrome below the pending area
 * occupies (Composer = PhaseStepperCard ≤ 9 + InputPrompt fence/content
 * 3 + Footer 1 + breathing 2). The number is intentionally conservative
 * — over-reserving costs us a row or two of pending body when content
 * is unusually short, but under-reserving lets the dynamic frame
 * exceed viewport rows and trips the overflow-into-scrollback path
 * we're trying to escape. PhaseStepperCard is only active during inject
 * turns; we always reserve for it because the alternative is a budget
 * that grows mid-turn and re-truncates pending items dynamically,
 * which manifests visually as content "popping in" as the stepper
 * appears.
 */
const CHROME_ROWS_RESERVE = 16;

/** Lower bound on the budget passed to pending items. Below this any
 *  capped content would render as mostly the "+N folded" indicator. */
const MIN_PENDING_BUDGET = 6;

export const MainContent: React.FC<Props> = ({ version, serverUrl }) => {
  const history = useAppSelector((s) => s.history);
  const pending = useAppSelector((s) => s.pending);
  const session = useAppSelector((s) => s.session);
  const remountKey = useAppSelector((s) => s.historyRemountKey);
  const bootProgress = useAppSelector((s) => s.bootProgress);
  const constrainHeight = useAppSelector((s) => s.constrainHeight);
  const { rows } = useTerminalSize();

  // Per-pending-item height budget. ``constrainHeight: false`` (toggled
  // by Ctrl+O) sends ``undefined`` so MaxSizedBox renders content
  // without truncation — the user can scroll the long output normally
  // until they re-engage the cap. The same budget is passed to every
  // pending item; in practice ``flushLeadingStable``'s eager harvest
  // keeps pending to one or two items at a time, so per-item cap ≈
  // total cap.
  const availableTerminalHeight = constrainHeight
    ? Math.max(MIN_PENDING_BUDGET, rows - CHROME_ROWS_RESERVE)
    : undefined;

  // Static items: header + every committed history item.
  //
  // We do NOT push anything to <Static> until ``session.id`` is set
  // — i.e. until BootRunner's handshake completes and dispatches
  // SESSION_INITIALIZED. Two reasons:
  //
  //   1. ``<Header>`` would otherwise burn placeholder values
  //      (empty cluster / namespace / model) into scrollback on the
  //      very first paint and Static never re-renders, so a later
  //      session update couldn't fix it.
  //
  //   2. ``<Static>`` is index-based: it tracks how many items it
  //      has written and on each render slices ``items[prevIndex:]``
  //      to render only the *new* tail. If we pushed a history item
  //      (e.g. the welcome card from a HISTORY_APPENDED dispatch)
  //      *before* the header was eligible, Static would write
  //      ``[welcome]`` first, then on the SESSION_INITIALIZED
  //      re-render see ``[header, welcome]`` and slice from index 1
  //      → re-render welcome only, never write header. This is the
  //      classic Static reorder hazard. Gating the entire static
  //      array on ``session.id`` makes the transition atomic: items
  //      length goes 0 → N in a single step, so Static appends N
  //      items in order with no chance of header being lost.
  //
  // Until session.id is set, MainContent renders only the boot
  // spinner from ``state.bootProgress`` — pending area is also
  // empty during boot. The terminal shows just the spinner row.
  const staticItems: StaticEntry[] = [];
  if (session.id) {
    staticItems.push({
      key: "header",
      node: <Header version={version} session={session} serverUrl={serverUrl} />,
    });
    for (const item of history) {
      staticItems.push({
        key: item.id,
        node: <HistoryItemDisplay item={item} />,
      });
    }
  }

  return (
    <>
      {/*
        ``key={remountKey}`` forces the Static block to unmount + remount
        whenever /clear bumps it. Without that, Ink's append-only Static
        keeps every previously-rendered item in scrollback regardless of
        the items array — /clear would have no visible effect.
      */}
      <Static key={remountKey} items={staticItems}>
        {(entry) => <Box key={entry.key}>{entry.node}</Box>}
      </Static>
      {/* Boot-time spinner row, only visible during the brief window
          between welcome-card paint and doctor/pending-tasks cards
          landing in history. Sits ABOVE pending so a mid-boot turn
          (unlikely, but defensive) doesn't visually swap above it. */}
      {bootProgress && <BootProgress text={bootProgress} />}
      {/* OverflowProvider wraps the pending area: every MaxSizedBox
       *  inside reports overflow via context, ShowMoreLines reads the
       *  aggregated set to render the "Press Ctrl+O to expand" hint
       *  outside the affected card. Provider is scoped tightly to
       *  pending so static history items (where overflow is
       *  meaningless) don't pollute the set. */}
      <OverflowProvider>
        <Box flexDirection="column" ref={setProbePendingRef}>
          {/* The live phase-stepper lives in ``state.currentPhaseStepper``
              (a dedicated slot, NOT in pending) so its perpetual mutation
              during the turn doesn't block the leading-stable flush in
              TOKEN_APPENDED. Composer renders it as a sticky strip above
              InputPrompt; ``commitPending`` finalises and prepends it to
              pending right before the history flush, so it lands in
              scrollback at the top of the turn block in the right
              chronological position. No filter is needed here — pending
              never contains a phase_stepper mid-turn.

              ``isPending={true}`` + ``availableTerminalHeight`` together
              tell each pending component how many rows it can paint
              without pushing the total dynamic frame past viewport.
              Components route the budget through ``MaxSizedBox`` to
              cap their body content; overflow lands in
              ``OverflowContext`` so the user gets a Ctrl+O hint. */}
          {(() => {
            // Pre-compute the id of the FIRST unresolved confirm_prompt
            // so only it receives keyboard focus. The server contract
            // resolves Layer 1 before emitting Layer 2, so this is
            // typically a single-element set; defending against the
            // multi-prompt edge avoids two focused Selects fighting
            // over Enter.
            const firstUnresolvedPromptId = pending.find(
              (it) => it.kind === "confirm_prompt" && !it.resolved,
            )?.id;
            return pending.map((item) => (
              <HistoryItemDisplay
                key={item.id}
                item={item}
                isPending={true}
                availableTerminalHeight={availableTerminalHeight}
                isPromptFocused={
                  item.kind === "confirm_prompt"
                    ? item.id === firstUnresolvedPromptId
                    : undefined
                }
              />
            ));
          })()}
        </Box>
        <ShowMoreLines />
      </OverflowProvider>
    </>
  );
};
