/**
 * Thinking / streaming indicator — the single most-watched UI element.
 *
 * Two-region layout:
 *
 *   ⠋ thinking  (12s · ↓ 142 tokens · esc to cancel)        ← header
 *   ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄  ← separator
 *   <last 3 wrapped lines of live thinking buffer>           ← body
 *
 * The body region only renders while a thinking session is open
 * (``bodyLines.length > 0``). Once thinking ends — token reply / tool
 * call / turn end — the reducer commits a ``ThinkingItem`` (collapsed
 * "▸ Thought for Ns") to pending and the buffer empties, so this
 * component snaps back to single-line header-only.
 *
 * The token counter is the authoritative per-turn cumulative emitted
 * by the server's ``usage`` SSE event (LangChain ``on_chat_model_end``).
 * It jumps in steps as each LLM call lands rather than ticking
 * smoothly per token, which is honest about what's actually being
 * counted. The prior chars/4 client-side approximation has been
 * retired so this number matches the ``⚡ turn used N tokens`` summary
 * that lands at the bottom of the turn block.
 */

import { Box, Text } from "ink";
import { useLoadingIndicator } from "../hooks/useLoadingIndicator.js";
import { t } from "../i18n/index.js";
import { Theme } from "../theme/colors.js";
import { ThinkingSpinner } from "../theme/spinners.js";
import { Spinner } from "./shared/Spinner.js";

export const LoadingIndicator: React.FC = () => {
  const {
    visible,
    headerLabel,
    elapsedSec,
    bodyLines,
    narrow,
    bodyWidth,
  } = useLoadingIndicator();

  if (!visible) return null;

  // Token tally is intentionally NOT shown here anymore. The previous
  // "↓ N tokens" tail produced two annoyances:
  //   1. before the first ``usage`` event lands (5–30s of qwen
  //      reasoning) we showed ``↓ -- tokens`` which read as "the
  //      feature is broken" rather than "data not yet available".
  //   2. it created a per-tick churn target (the live counter is
  //      refreshed on every Spinner tick, even when unchanged) and
  //      filled horizontal real estate without telling the user
  //      anything they couldn't read off the ``⚡ turn used N tokens``
  //      summary that lands at the bottom of the turn block.
  // Authoritative per-turn total still streams in via state (the
  // ``USAGE_RECEIVED`` action) and surfaces in TurnUsageMessage at
  // commitPending — that's the one place users care to see it.
  const meta = `(${formatElapsed(elapsedSec)} · ${t("loading.esc_to_cancel")})`;

  return (
    <Box paddingLeft={2} flexDirection="column">
      <Box flexDirection={narrow ? "column" : "row"} alignItems={narrow ? "flex-start" : "center"}>
        <Box>
          <Box marginRight={1}>
            <Spinner type={ThinkingSpinner.type} color={Theme.text.primary} />
          </Box>
          <Text color={Theme.text.accent} wrap="truncate-end">
            {headerLabel}
          </Text>
          {!narrow && (
            <Text color={Theme.text.secondary}> {meta}</Text>
          )}
        </Box>
      </Box>
      {narrow && (
        <Box>
          <Text color={Theme.text.secondary}>{meta}</Text>
        </Box>
      )}
      {bodyLines.length > 0 && (
        <Box flexDirection="column">
          <Text color={Theme.text.secondary}>{"┄".repeat(bodyWidth)}</Text>
          {bodyLines.map((line, i) => (
            <Text key={`tl-${i}`} color={Theme.text.secondary}>
              {line.length > 0 ? line : " "}
            </Text>
          ))}
        </Box>
      )}
    </Box>
  );
};

function formatElapsed(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m${s.toString().padStart(2, "0")}s`;
}
