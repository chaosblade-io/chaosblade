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

import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Box, Static, measureElement } from "ink";
import { BootProgress } from "./boot/BootProgress.js";
import { Header } from "./Header.js";
import { HistoryItemDisplay } from "./HistoryItemDisplay.js";
import { useAppSelector } from "../state/store.js";
import { useTerminalSize } from "../hooks/useTerminalSize.js";
import { OverflowProvider } from "../contexts/OverflowContext.js";
import { ShowMoreLines } from "./shared/ShowMoreLines.js";
import { setProbePendingRef } from "../utils/overflowProbe.js";
import {
  getChromeMeasureRef,
  subscribeChromeMeasureRef,
} from "../state/chromeMeasureRef.js";

interface Props {
  version: string;
  serverUrl: string;
}

interface StaticEntry {
  key: string;
  node: React.ReactNode;
}

/**
 * **First-paint fallback** for the chrome row reservation. Phase 3.2
 * replaced the hard-coded reserve with ``useLayoutEffect +
 * measureElement`` reading the real height of Composer's outer Box,
 * so this constant only governs the very first render (before
 * Composer has mounted and registered its ref) and acts as the
 * safety value if ``measureElement`` ever throws or returns a bad
 * reading. Once Composer commits, ``chromeHeight`` state takes over.
 *
 * Kept at **26** to track the Forge × Operator redesign + thinking-
 * body padding accumulation, in case the dynamic measurement is
 * unavailable:
 *
 *   PhaseStepperCard (round border + title + 5 step rows) ........ 8
 *   LoadingIndicator (header 1 + separator 1 + body padded 8) .. 10
 *   InputPrompt (top fence + body + bottom fence) ................ 5
 *   Footer (help hint + status) .................................. 1
 *   Composer outer marginTop ..................................... 1
 *   Safety buffer (off-by-one Yoga measurement, breathing) ....... 1
 *                                                                 = 26
 *
 * The previous value (16) was set when the LoadingIndicator body
 * grew naturally (1-3 rows typical) and PhaseStepperCard was the
 * lighter horizontal HUD design. Both have since gained rows
 * (padded body + bordered list-style stepper); under-reserving by
 * 8-10 rows let pending items render past stdout.rows and triggered
 * the user-reported "重复输出 + 闪烁" via the scrollback-pollution
 * path. The probe showed pendingH growing to 39 / frameH to 56 on
 * a 46-row terminal — exactly chrome=25 + (46-16)=30 budget. With
 * the corrected reserve pendingH caps at ~20 and frameH stays
 * inside viewport.
 *
 * PhaseStepperCard is only active during inject turns; we always
 * reserve for it because the alternative is a budget that grows
 * mid-turn and re-truncates pending items dynamically, which
 * manifests visually as content "popping in" as the stepper
 * appears.
 */
const CHROME_ROWS_RESERVE = 26;

/** Lower bound on the budget passed to pending items. Below this any
 *  capped content would render as mostly the "+N folded" indicator. */
const MIN_PENDING_BUDGET = 6;

/**
 * Progressive Static replay (Phase 3.3) — split a large ``history``
 * array into mountable chunks instead of letting Ink's ``<Static>``
 * walk the full list on a single remount.
 *
 * The pain point this fixes: when ``/clear`` bumps ``historyRemountKey``
 * AFTER a long-running session has accumulated 100+ committed items,
 * the single re-mount tries to walk every HistoryItemDisplay subtree
 * in one synchronous pass. AgentMessage's ``renderMarkdown`` and
 * ToolMessage's ``truncateOutput`` each cost ~1-5 ms; 500 items × 3 ms
 * average = a 1.5 second UI freeze before the cleared viewport finishes
 * painting. With progressive replay, the first ``CHUNK_SIZE`` items
 * mount synchronously and the remainder is appended via ``setImmediate``
 * batches of ``CHUNK_SIZE`` so the event loop gets to breathe between
 * each batch (keystrokes, spinner ticks, resize events all stay
 * responsive). Static itself is append-only, so once an item is
 * mounted into a batch it never re-renders.
 *
 * Thresholds (mirrors qwen-code's MainContent.tsx):
 *
 *   - ``THRESHOLD`` (100): below this we mount everything in one shot
 *     — the freeze is imperceptible for short sessions, and skipping
 *     the chunking machinery avoids the small extra render cycle.
 *   - ``CHUNK_SIZE`` (50): per-tick batch size. ~50 items mounts in
 *     under one animation frame (16ms) on typical hardware; bigger
 *     batches lose the "breathe between" benefit.
 *
 * The tail-gap shortcut (see render logic) sends the full
 * ``mergedHistory`` to Static when the gap is ≤ CHUNK_SIZE so a
 * just-finalised item doesn't briefly disappear during normal
 * append-after-token activity.
 */
export const PROGRESSIVE_REPLAY_THRESHOLD = 100;
export const PROGRESSIVE_REPLAY_CHUNK_SIZE = 50;

/** Pure helper exported so the threshold + chunk semantics can be
 *  unit-tested without spinning up Ink + StoreProvider. The first
 *  ``replayCount`` to feed to ``<Static>`` after a (re)mount. */
export function initialReplayCount(length: number): number {
  return length <= PROGRESSIVE_REPLAY_THRESHOLD
    ? length
    : Math.min(PROGRESSIVE_REPLAY_CHUNK_SIZE, length);
}

/** Absolute upper bound on a "real" chrome measurement on blade-ai's
 *  current layout — LoadingIndicator (10) + PhaseStepper (8) +
 *  InputPrompt (5) + Footer (1) + margins (2) + comfortable head-room.
 *  Readings higher than this are almost certainly a Yoga bug or a
 *  terminal reporting the full viewport. */
const ABS_CHROME_CAP = 35;

/**
 * Compute the upper bound a single ``measureElement`` reading must
 * satisfy to be accepted. Two pressures interact:
 *
 *   - ``ABS_CHROME_CAP`` (35): an honest chrome on this app cannot
 *     exceed ~35 rows; values above it indicate a pathological reading.
 *   - ``rows - MIN_PENDING_BUDGET``: on a *very* narrow terminal
 *     (rows ≤ 12), this term collapses to ``MIN_PENDING_BUDGET`` and
 *     the cap floors at 6 — meaning the dynamic measurement path is
 *     effectively dead code there and the fallback ``CHROME_ROWS_RESERVE``
 *     takes over. That's intentional: tiny terminals can't fit our
 *     chrome anyway, so the fallback is just as good as any honest
 *     measurement.
 *
 * Exported so the boundary semantics can be unit-tested without
 * mounting Ink.
 */
export function chromeMeasurementCap(rows: number): number {
  return Math.min(
    ABS_CHROME_CAP,
    Math.max(MIN_PENDING_BUDGET, rows - MIN_PENDING_BUDGET),
  );
}

/**
 * Decide whether a fresh ``measureElement`` reading should be applied
 * to ``chromeHeight``. Returns the accepted value, or ``null`` to
 * keep the prior ``chromeHeight`` state. Three guards in order:
 *
 *   1. **Must be a finite real number.** JavaScript's ``<`` and ``>``
 *      operators return ``false`` for any comparison involving NaN, so
 *      the legacy `if (measured < 5) return;` / `if (measured > cap) return;`
 *      pair silently let NaN through — that NaN would then propagate
 *      through `availableTerminalHeight = max(6, rows - NaN - 2) = NaN`
 *      and pollute every ``MaxSizedBox`` budget in pending. Yoga has
 *      been observed returning NaN on rapid resize when a measurement
 *      lands between layout passes; ``Number.isFinite`` catches both
 *      NaN and the (similarly broken) Infinity case.
 *
 *   2. **Lower bound 5.** Real chrome is always at least 5 rows
 *      (InputPrompt fences + Footer + outer margin). Readings below
 *      that mean Composer hasn't fully laid out yet — keep the prior
 *      good value rather than jumping to a too-generous budget that
 *      would push pending into chrome's space.
 *
 *   3. **Upper bound from ``chromeMeasurementCap(rows)``** — see the
 *      cap docstring for the dual pressure (absolute ceiling vs
 *      narrow-terminal floor).
 *
 * Exported pure so the failure modes are unit-testable in isolation.
 */
export function acceptChromeMeasurement(
  measured: number,
  rows: number,
): number | null {
  if (!Number.isFinite(measured)) return null;
  if (measured < 5) return null;
  if (measured > chromeMeasurementCap(rows)) return null;
  return measured;
}

export const MainContent: React.FC<Props> = ({ version, serverUrl }) => {
  const history = useAppSelector((s) => s.history);
  const pending = useAppSelector((s) => s.pending);
  const session = useAppSelector((s) => s.session);
  const remountKey = useAppSelector((s) => s.historyRemountKey);
  const bootProgress = useAppSelector((s) => s.bootProgress);
  const constrainHeight = useAppSelector((s) => s.constrainHeight);
  // Phase 3.2 — these selectors are NOT consumed below; they're
  // subscribed as ``useLayoutEffect`` re-trigger keys. Every one of
  // them can change the chrome height that lives under the pending
  // area (spinner show/hide, stepper show/hide, compaction indicator
  // mutex), and we want a fresh ``measureElement`` reading right
  // after Ink commits each transition. Without these deps the
  // effect would only re-fire on resize.
  const streamStateForChrome = useAppSelector((s) => s.streamState);
  const hasStepperForChrome = useAppSelector(
    (s) => s.currentPhaseStepper !== null,
  );
  const compactionInFlightForChrome = useAppSelector(
    (s) => s.currentCompaction !== null,
  );
  // Phase 3.2 audit-fix — ``constrainHeight`` also affects chrome
  // height because ``useLoadingIndicator``'s ``bodyMax`` (the padded
  // CoT body inside LoadingIndicator) is gated on this flag. On
  // small terminals (rows < 30) the two branches can differ by up
  // to ~5 rows, so a Ctrl+O toggle mid-thinking changes chrome
  // height; without this dep, measureElement wouldn't re-fire and
  // ``chromeHeight`` would stay at the pre-toggle value until the
  // next unrelated transition.
  const { columns, rows } = useTerminalSize();

  // Phase 3.2 — measured chrome height (LoadingIndicator + optional
  // PhaseStepperCard + InputPrompt + Footer + outer margins). Initial
  // state is the historical ``CHROME_ROWS_RESERVE`` constant; it acts
  // as the first-paint fallback before Composer has mounted and as
  // the safety value if ``measureElement`` ever fails. After the
  // first commit cycle the useLayoutEffect below replaces it with
  // the real value and keeps it in sync as chrome rows appear /
  // disappear during the session.
  const [chromeHeight, setChromeHeight] = useState<number>(
    CHROME_ROWS_RESERVE,
  );

  // Phase 3.2 — ref-transition version bump. React commits siblings
  // in JSX order; MainContent is mounted *before* Composer, so on
  // the very first render our useLayoutEffect fires while
  // ``getChromeMeasureRef()`` is still ``null``. Without this
  // subscription the effect's deps wouldn't change until the user
  // triggered a turn, leaving ``chromeHeight`` stuck on the
  // fallback for the whole first session. Subscribing here means
  // ``setChromeMeasureRef`` flips ``chromeRefVersion`` one microtask
  // after Composer commits, the layout effect re-fires on the
  // next render, and the real measurement lands without any user
  // input.
  const [chromeRefVersion, setChromeRefVersion] = useState(0);
  useEffect(() => {
    const unsubscribe = subscribeChromeMeasureRef(() => {
      // Functional updater is mandatory — multiple ref transitions
      // could batch into the same microtask flush (rare but
      // theoretically possible during hot reload or rapid mount/
      // unmount cycles); using ``v => v + 1`` keeps the bump
      // monotonic regardless of how React schedules the setState.
      setChromeRefVersion((v) => v + 1);
    });
    return unsubscribe;
  }, []);

  useLayoutEffect(() => {
    const el = getChromeMeasureRef();
    if (!el) return;
    let measured: number;
    try {
      measured = measureElement(el).height;
    } catch {
      // measureElement throws when the element isn't mounted. Safe
      // to swallow — we keep the prior chromeHeight value, which is
      // either the conservative fallback (first render) or the
      // last good measurement.
      return;
    }
    // Sanity-check the measurement via the pure ``acceptChromeMeasurement``
    // helper. It encodes three guards (finite-number, lower-bound 5,
    // upper-bound from ``chromeMeasurementCap(rows)``) and returns
    // ``null`` on rejection so we keep the prior ``chromeHeight``
    // state — see the helper's docstring for the rationale on each
    // guard, in particular the ``Number.isFinite`` short-circuit
    // (NaN propagates through ``< / >`` comparisons silently, which
    // would otherwise let a Yoga-glitched NaN reading land in state
    // and poison every downstream ``availableTerminalHeight`` math).
    const accepted = acceptChromeMeasurement(measured, rows);
    if (accepted === null) return;
    // Same-reference guard: setState with the same value would still
    // trigger a render; gate it explicitly so a no-op measure
    // doesn't churn the render loop.
    setChromeHeight((prev) => (prev === accepted ? prev : accepted));
  }, [
    rows,
    columns,
    streamStateForChrome,
    hasStepperForChrome,
    compactionInFlightForChrome,
    // ``constrainHeight`` flips on Ctrl+O and changes ``bodyMax`` in
    // useLoadingIndicator (used by the LoadingIndicator's padded body
    // block). On small terminals the chrome height can swing by up to
    // ~5 rows from this toggle alone. Without this dep, a mid-thinking
    // Ctrl+O would leave ``chromeHeight`` stale until the next
    // unrelated transition.
    constrainHeight,
    // ``pending.length`` is intentionally included: the pending area
    // is rendered BELOW the chrome but its height growth can push the
    // chrome into a different viewport position, and Ink's Yoga can
    // reflow heights inside the chrome accordingly on some terminals.
    // Re-measuring on pending depth changes catches that case cheaply.
    pending.length,
    // First-paint catch-up — see the chromeRefVersion subscription
    // above. Without this dep the very first render's null-ref
    // miss would never recover until the next state-driven re-fire.
    chromeRefVersion,
  ]);

  // Per-pending-item height budget. ``constrainHeight: false`` (toggled
  // by Ctrl+O) sends ``undefined`` so MaxSizedBox renders content
  // without truncation — the user can scroll the long output normally
  // until they re-engage the cap. The same budget is passed to every
  // pending item; in practice ``flushLeadingStable``'s eager harvest
  // keeps pending to one or two items at a time, so per-item cap ≈
  // total cap.
  //
  // The ``- 2`` safety margin absorbs a one-row Yoga rounding error
  // (measureElement returns integer cells but Ink composes a margin
  // outside the measured Box) and gives the pending block a one-row
  // breathing space above InputPrompt so the next tick can grow the
  // pending body without immediately overflowing.
  const availableTerminalHeight = constrainHeight
    ? Math.max(MIN_PENDING_BUDGET, rows - chromeHeight - 2)
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
  // Phase 3.3 — progressive Static replay state machine. See the
  // ``PROGRESSIVE_REPLAY_*`` constant block for rationale. The state
  // lives here (not in the reducer) because it's a pure render-layer
  // concern: when remountKey changes (i.e. /clear fires), we want
  // Ink to mount items in chunks rather than all at once.
  const [replayCount, setReplayCount] = useState(() =>
    initialReplayCount(history.length),
  );
  // Latest length kept in a ref so the setImmediate callback (which
  // captures stale closures) always advances toward the up-to-date
  // length, not the length at the time the effect scheduled.
  const historyLengthRef = useRef(history.length);
  historyLengthRef.current = history.length;

  // Reset the replay window when /clear bumps remountKey. CRITICAL:
  // this MUST run during render — NOT in a useEffect. ``remountKey``
  // also drives the ``<Static>`` ``key`` below; Ink remounts Static
  // synchronously on the first render with the new key. If we
  // reset replayCount in a useEffect, that first render would have
  // already passed the full (just-cleared) history to the new
  // <Static>, defeating the chunking entirely. The "store previous
  // prop in state + setState during render" pattern queues a re-render
  // that discards this one before commit, so <Static> never sees the
  // stale full slice. (Refs alone don't work — they don't trigger a
  // re-render; React would commit the current render with the stale
  // state.) See:
  //   https://react.dev/reference/react/useState#storing-information-from-previous-renders
  const [lastRemountKey, setLastRemountKey] = useState(remountKey);
  if (lastRemountKey !== remountKey) {
    setLastRemountKey(remountKey);
    setReplayCount(initialReplayCount(historyLengthRef.current));
  }

  useEffect(() => {
    if (replayCount >= history.length) return;
    const remaining = history.length - replayCount;
    if (remaining <= PROGRESSIVE_REPLAY_CHUNK_SIZE) {
      // Within one chunk of done — jump straight to the end so we
      // don't waste another scheduling tick on a tiny remainder.
      setReplayCount(history.length);
      return;
    }
    // setImmediate yields back to the event loop between batches.
    // setTimeout(_, 0) would also work but setImmediate is slightly
    // higher priority and runs before timer callbacks, which lets the
    // user perceive each batch as "filling in fast" rather than
    // "waiting between frames".
    const handle = setImmediate(() => {
      setReplayCount((c) =>
        Math.min(c + PROGRESSIVE_REPLAY_CHUNK_SIZE, historyLengthRef.current),
      );
    });
    return () => clearImmediate(handle);
  }, [replayCount, history.length]);

  // Tail-gap shortcut. Normal append (a new history item lands during
  // streaming) creates a gap of exactly 1 — far below CHUNK_SIZE. In
  // that common case we hand Static the full slice so the new item
  // appears immediately instead of waiting a tick. Only when the gap
  // is bigger than CHUNK_SIZE (i.e. /clear + long-history remount) do
  // we slice and let the effect catch up chunk by chunk.
  const visibleHistory =
    history.length - replayCount <= PROGRESSIVE_REPLAY_CHUNK_SIZE
      ? history
      : history.slice(0, replayCount);

  const staticItems: StaticEntry[] = [];
  if (session.id) {
    staticItems.push({
      key: "header",
      node: <Header version={version} session={session} serverUrl={serverUrl} />,
    });
    for (const item of visibleHistory) {
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

        ``session.id`` gate is LOAD-BEARING. Do NOT mount ``<Static>``
        before the session has been created. Original symptom: the
        first-run wizard (which renders right after boot, never mounts
        ``<Static>`` itself) accumulated blank rows above the card —
        not per keystroke, but starting from the first wizard step
        TRANSITION (e.g. picking a preset model and advancing to the
        URL step). Captured via ``BLADE_AI_WIZARD_DEBUG=1``: at that
        transition Ink's ``renderInteractiveFrame`` started taking its
        ``hasStaticOutput`` branch, which writes ``staticOutput`` (here
        a ghostly ``\n\n\n\n`` for the leftover empty Static's yoga
        height=4) between ``log.clear()`` and the new frame. Each
        subsequent re-render did the same — 4 extra blank rows per
        render, pushing the frame down.

        Why the ghost: boot mounts App → MainContent → ``<Static
        items={[]}>``. Ink's reconciler writes that Static instance
        into ``rootNode.staticNode``. When BootRunner flips phase to
        ``"wizard"`` and ``<App>`` unmounts, Ink's ``removeChild``
        cleanup is supposed to reset ``rootNode.staticNode = undefined``
        — but the cleanup races React 19's commit phase, and the yoga
        node (held separately by ``cleanupYogaNode``) survives long
        enough for a later layout pass to recompute its height to 4.
        Once that's stored on the surviving staticNode, every frame
        thereafter is contaminated by the bad branch.

        ConfirmMessage doesn't show the symptom because it lives below
        a real, content-filled Static (chat history). Even though the
        same ``hasStaticOutput`` branch fires there, ``staticOutput``
        is the real history bytes which Ink already flushed to
        scrollback — the visual effect is invisible. The wizard had no
        history above, so the 4 empty newlines per render were the
        only thing on screen and the drift was directly visible.
      */}
      {session.id && (
        <Static key={remountKey} items={staticItems}>
          {(entry) => <Box key={entry.key}>{entry.node}</Box>}
        </Static>
      )}
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
