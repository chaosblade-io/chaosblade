/**
 * Thinking / streaming indicator — the single most-watched UI element.
 *
 * Single-line layout (qwen-code style):
 *
 *   ⠋ thinking  (12s · esc to cancel)
 *
 * The previous design rendered up to 8-12 wrapped rows of the live
 * thinking buffer beneath a ┄ separator. That brought multiple
 * compounding pain points:
 *
 *   1. **Chrome height churn.** Every CoT line growth (12-17 Hz under
 *      thinking-mode streaming) extended ``bodyLines.length`` by 1,
 *      forcing Ink to redraw the whole dynamic frame at the new
 *      height. The eye perceived "持续小闪烁".
 *   2. **Padded variant traded amplitude for area.** Padding the body
 *      to a fixed ``bodyMax`` rows kept the chrome height stable, but
 *      meant every spinner tick (12.5 Hz) re-issued an erase+rewrite
 *      of the full ``bodyMax``-row block. The eye now perceived
 *      "整块在闪".
 *   3. **Resize-time leak.** Any chrome height change (thinking start
 *      / end / wrap re-flow) intersected with the patched ink Static
 *      semantics: rows that had been written above the current
 *      viewport were no longer reachable by ``eraseLines(...)`` and
 *      stuck in scrollback as永久留痕. Long turns produced screens-
 *      worth of duplicate "⏺ VERIFICATION_CHECKLIST:" stripes.
 *
 * qwen-code dodges all three by simply not rendering the body. The
 * indicator is one row: spinner + ``primaryText`` + meta. Their
 * ``primaryText`` is ``thought.subject`` (parsed from ``**Subject**``
 * markdown in the LLM's thinking stream) or a witty phrase fallback.
 * We mirror the structure: ``headerLabel`` is set by the reducer
 * from NODE_STARTED / TOOL_STARTED / phrase cycler — the live CoT
 * buffer is *kept* in state (``state.thoughtBuffer``) for the
 * eventual collapsed ``▸ Thought for Ns`` row in scrollback, but is
 * **not displayed live** as multiple rows. This:
 *
 *   - keeps chrome height at exactly 1 row regardless of state →
 *     no per-token redraws of the dynamic frame's body region,
 *   - eliminates the start/end height jumps that caused scrollback
 *     leaks on resize,
 *   - is still informative (header text reflects the active node /
 *     tool / phrase, refreshed by reducer on transitions).
 *
 * The hook ``useLoadingIndicator`` keeps returning ``bodyLines``
 * (deprecated path) so future restoration / opt-in is a one-line
 * change in this file. The existing field is no longer read here.
 */

import { memo } from "react";
import { Box, Text } from "ink";
import { useLoadingIndicator } from "../hooks/useLoadingIndicator.js";
import { t } from "../i18n/index.js";
import { Theme } from "../theme/colors.js";
import { ThinkingSpinner } from "../theme/spinners.js";
import { Spinner } from "./shared/Spinner.js";

const LoadingIndicatorInternal: React.FC = () => {
  const { visible, headerLabel, elapsedSec, turnTokens, narrow } =
    useLoadingIndicator();

  if (!visible) return null;

  // Live tokens estimate shown only once the counter has actually
  // climbed off zero — a "~0 tokens" prefix during the first 200ms
  // of a turn before any chars have arrived just adds chrome noise.
  // ``~`` prefix flags the figure as an estimate; the committed
  // ``⚡ turn used …`` row carries the authoritative server count.
  const tokensSegment =
    turnTokens > 0 ? ` · ${t("loading.tokens_estimate", { n: turnTokens })}` : "";
  const meta = `(${formatElapsed(elapsedSec)}${tokensSegment} · ${t("loading.esc_to_cancel")})`;

  return (
    <Box paddingLeft={2} flexDirection="column">
      <Box
        flexDirection={narrow ? "column" : "row"}
        alignItems={narrow ? "flex-start" : "center"}
      >
        <Box>
          <Box marginRight={1}>
            <Spinner type={ThinkingSpinner.type} color={Theme.text.primary} />
          </Box>
          <Text color={Theme.text.accent} wrap="truncate-end">
            {headerLabel}
          </Text>
          {!narrow && <Text color={Theme.text.secondary}> {meta}</Text>}
        </Box>
      </Box>
      {narrow && (
        <Box>
          <Text color={Theme.text.secondary}>{meta}</Text>
        </Box>
      )}
    </Box>
  );
};

// React.memo: LoadingIndicator has zero props. The default shallow
// prop comparison always reports "equal", so the component only
// re-renders when its OWN ``useLoadingIndicator`` hook (or
// inner-Spinner state) produces a new value. Composer's per-render
// JSX walk no longer pulls this component along for the ride.
export const LoadingIndicator = memo(LoadingIndicatorInternal);

function formatElapsed(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m${s.toString().padStart(2, "0")}s`;
}
