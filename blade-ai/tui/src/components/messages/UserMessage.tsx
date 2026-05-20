/**
 * User echo — Forge × Operator palette. Rendered as a full-row amber
 * bubble so the user's own messages anchor the conversation flow
 * without needing a rail decoration on top of the background fill
 * (the bubble itself is already a strong visual marker).
 *
 * Why we hand-pad each visual line:
 *   - Ink only supports ``backgroundColor`` on ``<Text>``, not on
 *     ``<Box>``. The fill stops at the last printed cell, so without
 *     manual padding a short message produces a tiny coloured chip
 *     while the rest of the row stays terminal-default — visually
 *     weak and easy to miss.
 *   - Long messages that need wrapping have to be split into visual
 *     lines BEFORE padding, otherwise wrapped portions of a single
 *     ``<Text>`` end up the wrong width. ``wrap-ansi`` handles word
 *     boundaries correctly (CJK + ASCII safe).
 *
 * The ``▎`` rail prefix used by AgentMessage / ThinkingMessage is
 * intentionally omitted here — the bubble's coloured background
 * already serves the same "anchor this line as part of the
 * conversation channel" function, and adding a rail in front would
 * double-mark the line for no extra information.
 */

import { Box, Text } from "ink";
import stringWidth from "string-width";
import wrapAnsi from "wrap-ansi";
import type { UserItem } from "../../state/types.js";
import { useBootCardWidth } from "../boot/BootCardFrame.js";

// Deep forge wash — sits in the same family as the brand fire (it's
// roughly fire darkened ~3 stops). Distinguishable from the
// surrounding chrome on both light and dark terminals; carries
// enough warmth to read as "the user's own voice".
const USER_BUBBLE_BG = "#4A2A15";
// Off-white foreground — the bubble paints its own dark background
// so a definite light hue is required.
const USER_BUBBLE_FG = "#E6E1D6";

/** Right-pad ``s`` with spaces until it fills ``cols`` visual cells. */
function padToWidth(s: string, cols: number): string {
  const w = stringWidth(s);
  if (w >= cols) return s;
  return s + " ".repeat(cols - w);
}

export const UserMessage: React.FC<{ item: UserItem }> = ({ item }) => {
  const width = useBootCardWidth();
  // 2-cell horizontal gutter on each side of the text inside the
  // bubble. innerWidth governs how the message wraps.
  const innerWidth = Math.max(8, width - 2);
  const visualLines: string[] = [];
  for (const para of item.text.split("\n")) {
    if (para.length === 0) {
      visualLines.push("");
      continue;
    }
    const wrapped = wrapAnsi(para, innerWidth, { hard: true, trim: false });
    for (const line of wrapped.split("\n")) {
      visualLines.push(line);
    }
  }
  if (visualLines.length === 0) visualLines.push("");

  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="column">
      {visualLines.map((line, i) => (
        <Text
          key={i}
          color={USER_BUBBLE_FG}
          backgroundColor={USER_BUBBLE_BG}
        >
          {padToWidth(` ${line} `, width)}
        </Text>
      ))}
    </Box>
  );
};
