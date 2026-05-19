/**
 * User echo — rendered as a full-row amber band so the user's own
 * messages stand out clearly from agent replies and tool output.
 *
 * Why we hand-pad each visual line:
 *   - Ink 5 only supports ``backgroundColor`` on ``<Text>``, not on
 *     ``<Box>``. The fill stops at the last printed cell, so without
 *     manual padding a short message produces a tiny coloured chip
 *     while the rest of the row stays terminal-default — visually
 *     weak and easy to miss.
 *   - Long messages that need wrapping have to be split into visual
 *     lines BEFORE padding, otherwise wrapped portions of a single
 *     ``<Text>`` end up the wrong width. ``wrap-ansi`` handles word
 *     boundaries correctly (CJK + ASCII safe).
 *
 * Width target: ``useBootCardWidth`` — same value the boot cards and
 * input prompt dividers use, so the user's bubble lines up flush with
 * the rest of the chrome.
 */

import { Box, Text } from "ink";
import stringWidth from "string-width";
import wrapAnsi from "wrap-ansi";
import type { UserItem } from "../../state/types.js";
import { useBootCardWidth } from "../boot/BootCardFrame.js";

// Dark amber wash. About three stops below ``Theme.text.accent``
// (#F2A65A) so the colour family matches but the contrast is gentle —
// readable on both default-dark and high-contrast terminal themes.
const USER_BUBBLE_BG = "#3a2a14";
// The bubble paints its own dark bg, so the foreground must be a
// definite light hue rather than ``Theme.text.primary`` (which is
// intentionally ``undefined`` so terminal-default text adapts to
// dark/light themes). On a white terminal the terminal-default fg is
// black, and black-on-#3a2a14 falls well below the 4.5:1 contrast
// threshold — hard-pinning an off-white keeps the bubble readable on
// every terminal.
const USER_BUBBLE_FG = "#e6e1d6";

/** Right-pad ``s`` with spaces until it fills ``cols`` visual cells. */
function padToWidth(s: string, cols: number): string {
  const w = stringWidth(s);
  if (w >= cols) return s;
  return s + " ".repeat(cols - w);
}

export const UserMessage: React.FC<{ item: UserItem }> = ({ item }) => {
  const width = useBootCardWidth();

  // Strategy: split user-typed newlines first (preserve intentional
  // paragraph breaks), then word-wrap each paragraph to fit within
  // the bubble's content area. The 2-cell deduction below is for the
  // 1-cell horizontal gutter we add on each side of the text.
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
  // Single empty input shouldn't happen (Composer trims) but guard so
  // we still draw at least one band.
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
