/**
 * Collapsed thinking item — one row per discrete thinking session
 * that the agent went through during a turn.
 *
 * Visual: ``▸ Thought for 26s`` in dim secondary color. Single-line by
 * design.
 *
 * Token usage is intentionally NOT displayed here — per-session
 * tokens would require retroactively updating Static-rendered items
 * once the server's ``usage`` event arrives (it lands AFTER the
 * thinking commits and gets flushed to history), which Ink's
 * append-only ``<Static>`` doesn't support. The authoritative
 * per-turn cumulative shows instead in the LoadingIndicator's live
 * tail and in the ``⚡ turn used N tokens`` summary appended at
 * commitPending.
 *
 * Lifecycle: ``ThinkingItem`` is appended to ``pending`` by the
 * reducer's ``commitThinking`` helper at every transition out of
 * thinking — token reply, tool call, or turn end. The pending → history
 * sink at TURN_DONE / TURN_ABORTED then burns it into scrollback like
 * any other history item.
 */

import { Box, Text } from "ink";
import { memo } from "react";
import { t } from "../../i18n/index.js";
import { Theme } from "../../theme/colors.js";
import type { ThinkingItem } from "../../state/types.js";

const ThinkingMessageInternal: React.FC<{ item: ThinkingItem }> = ({ item }) => {
  const duration = formatDuration(item.durationMs);
  return (
    <Box paddingLeft={2} marginTop={1}>
      <Text color={Theme.text.secondary}>
        {`▸ ${t("thinking.collapsed", { duration })}`}
      </Text>
    </Box>
  );
};

// React.memo: trivial render but in the streaming hot loop.
export const ThinkingMessage = memo(ThinkingMessageInternal);

function formatDuration(ms: number): string {
  if (ms < 1000) return "<1s";
  const sec = Math.round(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m${s.toString().padStart(2, "0")}s`;
}
