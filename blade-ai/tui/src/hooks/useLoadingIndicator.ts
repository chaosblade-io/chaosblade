/**
 * Compose the LoadingIndicator's display props from the AppState.
 *
 * Returns a single object so the LoadingIndicator component is purely
 * presentational (no store coupling, easy to snapshot-test).
 *
 * Header label resolution (priority top-down):
 *   1. ``thoughtBuffer`` non-empty   → localized "thinking"
 *   2. ``thoughtSubject`` set        → use directly (tool name set by
 *      TOOL_STARTED, "resuming…"/"stopping…" set by CONFIRM_RESOLVED,
 *      "replaying ..." set by REPLAY_STARTED)
 *   3. else                          → ``state.idlePhrase`` (the
 *      reducer-driven cycler ticked by Composer every 8s)
 *
 * The previous design had a 4th branch that hardcoded "responding"
 * whenever ``isReceiving`` was true. That made a long agent reply
 * sit on the static word "responding" for tens of seconds — the
 * user couldn't tell whether the agent had stalled or was still
 * working. The reducer-driven cycler now covers that case AND the
 * dead-air idle case with the same rotation, so the static branch
 * is gone.
 *
 * Token counter — Phase 2.2 two-tier model:
 *
 *   1. **Live tail** (this hook) returns a smoothly-animated estimate
 *      derived from ``streamingResponseCharsRef.current / 4``. The
 *      ref is incremented per raw token event in ``useStream`` (zero
 *      React work), polled here via ``useAnimationFrame`` at 100 ms.
 *      The hook tweens between samples so the displayed figure
 *      climbs smoothly (~10 Hz visual rate) rather than jumping in
 *      big chunks driven by server ``usage`` events (which only land
 *      at LLM call boundaries — often 4-8 seconds apart).
 *   2. **Committed history row** (``TurnUsageItem`` rendered by
 *      ``TurnUsageMessage``) uses the authoritative server figures
 *      from ``turnInputTokens + turnOutputTokens`` for the
 *      after-the-fact summary. The live estimate is replaced by the
 *      exact count once the turn commits.
 *
 * The chars/4 approximation is the standard rough conversion (English-
 * heavy CoT runs about 3.8-4.2 chars/token). It's not exact — the
 * committed summary row carries the truth — but it's stable and
 * smooth, which the live tail needs to feel "alive". The exact
 * figures are still available for users who care; this just makes
 * the spinner's tail counter pleasant to watch instead of stuttery.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { t } from "../i18n/index.js";
import { useAppSelector } from "../state/store.js";
import { streamingResponseCharsRef } from "../state/streamingRefs.js";
import { isNarrow, useTerminalSize } from "./useTerminalSize.js";
import { useAnimationFrame } from "./useAnimationFrame.js";
import { tailWrappedLines } from "../utils/wrapText.js";
import { getPool } from "../utils/phrasePool.js";

export interface LoadingIndicatorProps {
  visible: boolean;
  /** Header label — "thinking" / "responding" / rotating idle phrase. */
  headerLabel: string;
  /** Seconds since the current turn started. */
  elapsedSec: number;
  /** Whether the most recent activity is content (token/thinking) vs API wait. */
  isReceiving: boolean;
  /** True while a turn is in progress (covers responding + waiting_confirmation). */
  isStreaming: boolean;
  /** Smooth-animated estimate of the current turn's token count
   *  (``streamingResponseCharsRef.current / 4``, polled by
   *  ``useAnimationFrame``). Updates at ~10 Hz while the indicator
   *  is visible — bursts of token chars resolve as a gentle climb,
   *  not a step ladder. ``0`` until the first token arrives.
   *
   *  This is intentionally an **estimate**: the authoritative figure
   *  lives on the committed ``TurnUsageItem`` (rendered by
   *  ``TurnUsageMessage`` once the turn ends), sourced from server
   *  ``usage`` events. The estimate prioritises responsiveness in
   *  the live tail; the committed summary prioritises accuracy. */
  turnTokens: number;
  /**
   * Last ~3 visual lines of the live thinking buffer. Empty array when
   * no thinking session is active. Rendered as the dim-color body
   * block under the header.
   */
  bodyLines: string[];
  /** True on terminals narrower than ``NARROW_THRESHOLD`` — host uses
   *  this to fold the meta tail onto its own row. Re-exposed here so
   *  the LoadingIndicator doesn't need a second ``useTerminalSize``
   *  subscription. */
  narrow: boolean;
  /** Width (in code units) used to wrap ``bodyLines`` — same value
   *  the LoadingIndicator uses for its dashed separator so the rule
   *  matches the visible body block. */
  bodyWidth: number;
}

/** Width budget reserved for the left padding (2) + a 2-col safety
 *  margin so soft-wrapped content doesn't kiss the right edge. Mirrors
 *  the LoadingIndicator's own ``paddingLeft={2}``. */
const BODY_RESERVED_COLS = 4;

/** Visible CoT body row cap. The body is ambient chrome — once it
 *  exceeds ~8 rows it starts to feel like content competing with
 *  the actual agent reply, so we lock the upper bound here
 *  regardless of how tall the terminal gets. On smaller terminals
 *  the per-render math (``rows - 22``) brings the effective cap
 *  down so the dynamic frame still fits. */
const BODY_MAX_LINES = 8;

export function useLoadingIndicator(): LoadingIndicatorProps {
  const streamState = useAppSelector((s) => s.streamState);
  const thoughtBuffer = useAppSelector((s) => s.thoughtBuffer);
  const thoughtSubject = useAppSelector((s) => s.thoughtSubject);
  const isReceiving = useAppSelector((s) => s.isReceiving);
  const turnStartedAt = useAppSelector((s) => s.turnStartedAt);
  const constrainHeight = useAppSelector((s) => s.constrainHeight);
  const idlePhrase = useAppSelector((s) => s.idlePhrase);
  const { columns, rows } = useTerminalSize();

  const isStreaming =
    streamState === "responding" || streamState === "waiting_confirmation";
  // Phase 4 — single-spinner mutex. While a memory-compaction call
  // is in flight, ``MemoryCompactingIndicator`` owns the spinner
  // slot above the InputPrompt. The regular LoadingIndicator yields
  // for the duration so the user sees one clear "what's happening"
  // signal instead of two competing ones.
  //
  // Why mutex (not augment): server SSE goes silent during the
  // 5-15s LLM summary call. Without the mutex the LoadingIndicator
  // sits showing "thinking…" the whole time — which is the bug
  // we fixed: the user can't tell whether the agent is reasoning
  // or whether memory's being compacted. Showing the dedicated
  // compaction spinner instead is the explicit answer.
  const compactionInFlight = useAppSelector(
    (s) => s.currentCompaction !== null,
  );

  // Visibility is *narrower* than ``isStreaming``: the indicator only
  // renders while the agent is actively producing output. During
  // ``waiting_confirmation`` the user owns the keyboard (Select inside
  // ConfirmMessage) and the agent is paused — leaving the spinner
  // ticking burns ~12.5 fps of stdout writes for no gain, which on
  // a tall confirm card pushes the dynamic frame past
  // ``stdout.rows`` and trips Ink's fullscreen-redraw branch every
  // tick. Result observed by users: continuous flicker + scroll
  // hijack while reading the confirm dialog. ``isStreaming`` keeps
  // its broader meaning for callers that gate on "turn in flight"
  // (Composer's Esc handling, InputPrompt disabled).
  const visible = streamState === "responding" && !compactionInFlight;

  // Phase 2.2 — smooth animated token counter. The hook polls the
  // module-level char counter (written per-token in ``useStream``
  // with zero React work) at 100ms while visible and tweens the
  // displayed value upward so the counter feels alive even when
  // raw token arrivals are bursty. Polling pauses (``null`` interval)
  // when the indicator is hidden so we don't burn intervals during
  // ``waiting_confirmation`` / idle. The /4 divisor is the standard
  // chars→tokens rough conversion; for the after-the-fact exact
  // figure the committed TurnUsageItem uses server ``usage`` events.
  const animatedChars = useAnimationFrame(
    streamingResponseCharsRef,
    visible ? 100 : null,
  );
  const turnTokens = Math.round(animatedChars / 4);

  // Local 1Hz timer for elapsed seconds. Gated on ``visible`` (not
  // ``isStreaming``) so the timer also stops during
  // waiting_confirmation — every 1Hz setState here is one more
  // re-render that would otherwise force a fullscreen redraw on a
  // confirm card taller than the viewport.
  const [elapsedSec, setElapsedSec] = useState(0);
  useEffect(() => {
    if (!visible || turnStartedAt === 0) {
      setElapsedSec(0);
      return;
    }
    const tick = () => {
      const ms = Date.now() - turnStartedAt;
      setElapsedSec(Math.max(0, Math.floor(ms / 1000)));
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [visible, turnStartedAt]);

  // Header label resolution order:
  //   1. live thinking session   → localized "thinking"
  //   2. thoughtSubject set      → use it directly (tool name set by
  //      TOOL_STARTED, "resuming…"/"stopping…" set by
  //      CONFIRM_RESOLVED, "replaying ..." set by REPLAY_STARTED).
  //      This keeps the header informative during tool execution
  //      and confirm-gate transitions — a regression we'd otherwise
  //      ship by collapsing every non-thinking phase to a generic
  //      cycling phrase.
  //   3. else                    → ``idlePhrase`` from state, ticked
  //      by Composer's PHRASE_TICK driver every 8s so a long agent
  //      reply / long-running node still has rotating header text
  //      (the static "responding" branch the previous design carried
  //      gave the user no liveness signal during such windows).
  //
  // Pool fallback: if the cycler hasn't ticked yet (``idlePhrase ===
  // ""`` — e.g. fresh ``waiting_confirmation`` -> ``responding``
  // transition before the first interval boundary), use the first
  // pool entry so the header is never blank.
  const fallbackPhrase = idlePhrase || getPool()[0] || "thinking";
  let headerLabel: string;
  if (thoughtBuffer.length > 0) {
    headerLabel = t("loading.thinking_label");
  } else if (thoughtSubject) {
    headerLabel = thoughtSubject;
  } else {
    headerLabel = fallbackPhrase;
  }

  // Throttle the thinking buffer that drives ``bodyLines``. The
  // raw ``thoughtBuffer`` ticks every token (≥ 12 Hz from a
  // streaming LLM) — and Ink redraws the whole 20+ row dynamic
  // frame on every state change. Most of those redraws are pure
  // chrome flicker: spinner advances 1 glyph, last body line gains
  // one character, and the user perceives 8-17 fps full-frame
  // shimmer. Letting the body lag the buffer by ~250 ms collapses
  // those into ~4 visible body refreshes per second — slow enough
  // to read, fast enough to feel "live", and the spinner-only
  // redraws between updates touch a single glyph instead of the
  // whole padded body.
  //
  // Spinner-driven redraws (12.5 Hz from ink-spinner's own
  // setInterval) still happen, but those repaint the same body
  // bytes — Ink writes the same content, so terminals with
  // intelligent diffing produce near-zero visible flicker.
  const BUFFER_THROTTLE_MS = 250;
  const [displayedBuffer, setDisplayedBuffer] = useState(thoughtBuffer);
  const lastFlushRef = useRef(Date.now());
  useEffect(() => {
    if (thoughtBuffer === displayedBuffer) return;
    const now = Date.now();
    const sinceLast = now - lastFlushRef.current;
    if (sinceLast >= BUFFER_THROTTLE_MS) {
      lastFlushRef.current = now;
      setDisplayedBuffer(thoughtBuffer);
      return;
    }
    const id = setTimeout(() => {
      lastFlushRef.current = Date.now();
      setDisplayedBuffer(thoughtBuffer);
    }, BUFFER_THROTTLE_MS - sinceLast);
    return () => clearTimeout(id);
  }, [thoughtBuffer, displayedBuffer]);

  // Pre-wrap the throttled buffer to the terminal width. Empty when
  // no session is active. Recomputed on each render — the work is
  // O(N) in buffer length but a single session is bounded by the
  // LLM's CoT budget (a few KB) so this is cheap relative to the
  // React reconcile.
  const narrow = isNarrow(columns);
  const bodyWidth = Math.max(20, columns - BODY_RESERVED_COLS);
  // When ``constrainHeight`` is on (default), shrink the live CoT body
  // budget so the LoadingIndicator + downstream chrome (PhaseStepperCard
  // + InputPrompt + Footer ≈ 13 rows) + any pending item still fit in
  // the viewport. Without this clamp, a long thinking session pushes
  // the dynamic frame past terminal rows and the bottom of the live
  // CoT scrolls into scrollback every render — exactly the symptom we
  // patched out of Ink's fullscreen-redraw branch. ``Ctrl+O`` toggles
  // ``constrainHeight`` off so the user can see the full thinking body
  // when needed.
  // bodyMax — computed against the live terminal height (re-read on
  // every SIGWINCH via ``useTerminalSize``). Two rules:
  //
  //   1. Capped at ``BODY_MAX_LINES`` (8) regardless of how tall
  //      the terminal is — once the body grows beyond ~8 rows it
  //      stops feeling like a peripheral CoT preview and starts
  //      competing with the actual agent output for attention.
  //   2. On small terminals where reserving 22 rows for the rest
  //      of the chrome (PhaseStepperCard 8 + InputPrompt 5 + Footer
  //      1 + Composer marginTop 1 + LoadingIndicator header/separator
  //      2 + safety 5) would push the frame past stdout.rows, fall
  //      back to ``rows - 22`` so the dynamic frame still fits.
  //
  // Sample bodyMax:
  //   rows=24 → max(3, min(8, 2))  = 3   (tiny terminal, body collapses)
  //   rows=28 → max(3, min(8, 6))  = 6
  //   rows=30 → max(3, min(8, 8))  = 8   (8-cap binds from here)
  //   rows=47 → max(3, min(8, 25)) = 8
  //   rows=80 → max(3, min(8, 58)) = 8
  //
  // Ctrl+O (``constrainHeight=false``) lifts the "fits in viewport"
  // rule but keeps the 8-row cap — useful when the user explicitly
  // wants to see live CoT in scrollback while accepting that the
  // frame may overflow.
  const bodyMax = constrainHeight
    ? Math.max(3, Math.min(BODY_MAX_LINES, rows - 22))
    : BODY_MAX_LINES;
  // Body is ALWAYS padded to bodyMax rows during thinking so chrome
  // height stays stable across token streaming. This trade-off was
  // tried before with bodyMax=12 and reverted (a 12-row block
  // pulsing on every token reads as "整块在闪"); at the much
  // smaller bodyMax=6 the per-token rewrite spans only 6 rows of
  // body content, which is light enough that "chrome stays still"
  // dominates "block flashes". Padding goes AFTER the real lines so
  // the body grows tail-f-style — fresh CoT appears under the
  // separator and travels downward, with empty slots between the
  // latest line and the InputPrompt fence. Once the buffer crosses
  // ``bodyMax`` wrapped lines the padding is gone and the block
  // starts the normal bottom-anchored scroll.
  // Phase 3.5 — useMemo the wrap+pad pipeline. ``tailWrappedLines``
  // is O(N) in buffer length and runs every render; the indicator
  // re-renders at ~10 Hz from useAnimationFrame + every thinking
  // append + every 1 Hz elapsed tick. Memoising on the four inputs
  // (the only things that actually affect ``bodyLines``) collapses
  // most of those re-renders into a cached array reference, which
  // also helps downstream React reconciliation skip the body Box.
  const bodyLines = useMemo<string[]>(() => {
    if (narrow || displayedBuffer.length === 0) return [];
    const realLines = tailWrappedLines(displayedBuffer, bodyMax, bodyWidth);
    const padding = Math.max(0, bodyMax - realLines.length);
    return padding > 0
      ? [...realLines, ...new Array<string>(padding).fill("")]
      : realLines;
  }, [narrow, displayedBuffer, bodyMax, bodyWidth]);

  return {
    visible,
    headerLabel,
    elapsedSec,
    isReceiving,
    isStreaming,
    turnTokens,
    bodyLines,
    narrow,
    bodyWidth,
  };
}
