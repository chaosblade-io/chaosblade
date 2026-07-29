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
 *
 * 2026-05-26 perf cleanup — removed the live CoT body render path
 * (``bodyLines`` + ``displayedBuffer`` 250ms throttle + ``bodyMax`` /
 * ``bodyWidth`` calculations + the ``tailWrappedLines`` wrap pipeline).
 * The LoadingIndicator only renders a single-line spinner+header
 * (per the qwen-code-style redesign documented in
 * ``components/LoadingIndicator.tsx``); the body machinery was kept as
 * a dead-code "future restoration switch" but every LLM streaming
 * dispatch still ran its useEffect / useMemo / useState chain. Removing
 * the deprecated path drops a useEffect + useState + useRef + useMemo
 * tuple that was firing 10-20Hz under thinking streams, alongside the
 * O(N) wrap+pad work even when nothing rendered the result.
 */

import { useEffect, useState } from "react";
import { t } from "../i18n/index.js";
import { useAppSelector } from "../state/store.js";
import { streamingResponseCharsRef } from "../state/streamingRefs.js";
import { isNarrow, useTerminalSize } from "./useTerminalSize.js";
import { useAnimationFrame } from "./useAnimationFrame.js";
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
  /** True on terminals narrower than ``NARROW_THRESHOLD`` — host uses
   *  this to fold the meta tail onto its own row. Re-exposed here so
   *  the LoadingIndicator doesn't need a second ``useTerminalSize``
   *  subscription. */
  narrow: boolean;
}

export function useLoadingIndicator(): LoadingIndicatorProps {
  const streamState = useAppSelector((s) => s.streamState);
  const turnStartedAt = useAppSelector((s) => s.turnStartedAt);
  const { columns } = useTerminalSize();

  // Combined headerLabel selector — returns the SAME string value
  // when the label branch hasn't changed, even if underlying slices
  // (e.g. ``idlePhrase``) were updated by PHRASE_TICK. This is
  // critical because ``useAppSelector`` uses ``Object.is`` to decide
  // whether to re-render: when ``hasActiveThinking`` is true the
  // selector always returns ``t("loading.thinking_label")``
  // ("思考中"), so a PHRASE_TICK that changes ``idlePhrase`` does
  // NOT trigger a re-render here. Previously three separate
  // ``useAppSelector`` calls (``hasActiveThinking``,
  // ``thoughtSubject``, ``idlePhrase``) each triggered re-renders
  // independently — ``idlePhrase`` changing every 8s caused a full
  // LoadingIndicator re-render (→ Ink dynamic-frame repaint →
  // pending area erased + rewritten) even while the label was
  // firmly in the "思考中" branch and the visible text didn't
  // change at all.

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
  // with zero React work) at 200ms while visible and tweens the
  // displayed value upward so the counter feels alive even when
  // raw token arrivals are bursty. Polling pauses (``null`` interval)
  // when the indicator is hidden so we don't burn intervals during
  // ``waiting_confirmation`` / idle. The /4 divisor is the standard
  // chars→tokens rough conversion; for the after-the-fact exact
  // figure the committed TurnUsageItem uses server ``usage`` events.
  //
  // 2026-07 perf — was 100ms (10Hz). Each ``setDisplay`` call here
  // causes LoadingIndicator to re-render, which in Ink causes the
  // ENTIRE dynamic frame to be erased + rewritten (including the
  // pending area). At 10Hz this added 10 extra repaints/s on top of
  // the 16.7Hz TOKEN_APPENDED dispatch rate and the 12.5Hz InkSpinner
  // animation — totalling ~39Hz of dynamic-frame repaints, which the
  // user perceived as "shimmer when the label changes". Dropping to
  // 200ms (5Hz) halves the extra repaints while still feeling smooth
  // for a numeric counter.
  const animatedChars = useAnimationFrame(
    streamingResponseCharsRef,
    visible ? 200 : null,
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

  // Header label resolution order (same priority as before, now
  // computed inside a single ``useAppSelector`` so that ``Object.is``
  // comparison prevents re-renders when the label value hasn't
  // changed even though underlying state slices did):
  //   1. live thinking session   → localized "思考中"
  //   2. thoughtSubject set      → use directly (tool name, etc.)
  //   3. else                    → ``idlePhrase`` (PHRASE_TICK cycler)
  //
  // Pool fallback: if the cycler hasn't ticked yet (``idlePhrase ===
  // ""``), use the first pool entry so the header is never blank.
  const headerLabel = useAppSelector((s) => {
    if (s.hasActiveThinking) return t("loading.thinking_label");
    if (s.thoughtSubject) return s.thoughtSubject;
    return s.idlePhrase || getPool()[0] || "thinking";
  });

  const narrow = isNarrow(columns);

  return {
    visible,
    headerLabel,
    elapsedSec,
    // ``isReceiving`` is no longer subscribed — it was returned but
    // never consumed by LoadingIndicator. Keeping it in the interface
    // for API stability; always ``false`` is safe because no caller
    // reads this field.
    isReceiving: false,
    isStreaming,
    turnTokens,
    narrow,
  };
}
