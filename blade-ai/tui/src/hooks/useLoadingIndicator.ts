/**
 * Compose the LoadingIndicator's display props from the AppState.
 *
 * Returns a single object so the LoadingIndicator component is purely
 * presentational (no store coupling, easy to snapshot-test).
 *
 * The header label adapts to phase:
 *   - "thinking" while a thinking session is open (thoughtBuffer
 *     non-empty)
 *   - "responding" while the agent is streaming the visible reply
 *     (any other receiving state — token / tool / waiting on API)
 * That keeps the spinner row honest about what stage of the turn the
 * user is watching, without resurrecting the rotating-subject
 * behaviour we just retired (which leaked the live last-sentence into
 * the header and made the spinner feel like content rather than
 * chrome).
 *
 * Token counter is sourced from ``state.turnInputTokens +
 * turnOutputTokens`` — authoritative figures the server emits via the
 * ``usage`` SSE event after each LLM call. The prior chars/4
 * approximation has been retired so the live tail and the
 * end-of-turn TurnUsageItem agree on the same numbers.
 */

import { useEffect, useState } from "react";
import { t } from "../i18n/index.js";
import { useAppSelector } from "../state/store.js";
import { isNarrow, useTerminalSize } from "./useTerminalSize.js";
import { tailWrappedLines } from "../utils/wrapText.js";
import { usePhraseCycler } from "./usePhraseCycler.js";

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
  /** Authoritative cumulative token count for the current turn (input
   *  + output). Sourced from server ``usage`` events. ``0`` until the
   *  first LLM call ends, then jumps in steps. */
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

/** Last N wrapped lines of the live thinking buffer kept visible while
 *  a thinking session is in flight. Bumped to 16 so the user can read
 *  more context of the agent's chain-of-thought before the buffer
 *  flushes into the collapsed ``▸ Thought for Ns`` row. AgentMessage's
 *  ``PENDING_CHROME_RESERVE`` already factors in the up-to-16-row body
 *  block when computing the streaming-text cap. */
const BODY_MAX_LINES = 16;

export function useLoadingIndicator(): LoadingIndicatorProps {
  const streamState = useAppSelector((s) => s.streamState);
  const thoughtBuffer = useAppSelector((s) => s.thoughtBuffer);
  const thoughtSubject = useAppSelector((s) => s.thoughtSubject);
  const isReceiving = useAppSelector((s) => s.isReceiving);
  const turnStartedAt = useAppSelector((s) => s.turnStartedAt);
  const turnInputTokens = useAppSelector((s) => s.turnInputTokens);
  const turnOutputTokens = useAppSelector((s) => s.turnOutputTokens);
  const constrainHeight = useAppSelector((s) => s.constrainHeight);
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

  // Witty phrase used as a fallback header label when nothing more
  // specific is happening (e.g. waiting on the LLM's first token).
  // Gated on ``visible`` so the 15s cycler timer is also paused
  // during waiting_confirmation.
  const phrase = usePhraseCycler(visible);

  // Header label resolution order:
  //   1. live thinking session   → localized "thinking"
  //   2. thoughtSubject set      → use it directly (tool name set by
  //      TOOL_STARTED, "resuming…"/"stopping…" set by
  //      CONFIRM_RESOLVED, "replaying ..." set by REPLAY_STARTED).
  //      This keeps the header informative during tool execution
  //      and confirm-gate transitions — a regression we'd otherwise
  //      ship by collapsing every non-thinking phase to a generic
  //      "responding".
  //   3. receiving content       → localized "responding"
  //   4. waiting on first event  → rotating idle phrase
  let headerLabel: string;
  if (thoughtBuffer.length > 0) {
    headerLabel = t("loading.thinking_label");
  } else if (thoughtSubject) {
    headerLabel = thoughtSubject;
  } else if (isReceiving) {
    headerLabel = t("loading.responding_label");
  } else {
    headerLabel = phrase;
  }

  // Pre-wrap the live thinking buffer to the terminal width and keep
  // the trailing 3 lines. Empty when no session is active. Recomputed
  // on each render — the work is O(N) in buffer length but a single
  // session is bounded by the LLM's CoT budget (a few KB) so this is
  // cheap relative to the React reconcile.
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
  const bodyMax = constrainHeight
    ? Math.max(3, Math.min(BODY_MAX_LINES, rows - 18))
    : BODY_MAX_LINES;
  // Reserve a fixed-height block for the body whenever a thinking
  // session is open. Without this the rendered ``bodyLines.length``
  // grows by 1–2 every few tokens (one row per wrapped CoT line)
  // and Ink rewrites the whole dynamic frame on each growth — the
  // user-visible "持续小闪烁" verified by the overflow probe (delta
  // +1: 92 frames, +2: 37 frames in a single inject session). By
  // padding leading empty rows we hold ``bodyLines.length`` constant
  // at ``bodyMax`` for the entire thinking session, so the chrome
  // height stops jittering. Empty padding goes BEFORE the real
  // content so the latest thinking text always anchors at the
  // bottom (closest to the InputPrompt) — the same visual order the
  // user reads in scrollback. The streaming-only height jump
  // happens exactly twice now (open + close of the session) instead
  // of once per wrap-row addition; one start + one stop is
  // tolerable, 100+ inter-token jitters is what the user reported
  // as flicker.
  let bodyLines: string[];
  if (narrow || thoughtBuffer.length === 0) {
    bodyLines = [];
  } else {
    const realLines = tailWrappedLines(thoughtBuffer, bodyMax, bodyWidth);
    const padding = Math.max(0, bodyMax - realLines.length);
    bodyLines =
      padding > 0
        ? [...new Array<string>(padding).fill(""), ...realLines]
        : realLines;
  }

  return {
    visible,
    headerLabel,
    elapsedSec,
    isReceiving,
    isStreaming,
    turnTokens: turnInputTokens + turnOutputTokens,
    bodyLines,
    narrow,
    bodyWidth,
  };
}
