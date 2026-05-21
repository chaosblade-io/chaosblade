/**
 * User echo — Forge × Operator palette. Rendered as a full-row
 * light-grey bubble (Slack-message-chip style) so the user's own
 * messages anchor the conversation flow without needing a rail
 * decoration on top of the background fill (the bubble itself is
 * already a strong visual marker). Light-grey (vs. the previous
 * #303030 charcoal) was picked because the operator profile is
 * light-terminal-dominant in practice — a dark bubble on a white
 * canvas read as a heavy block.
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
import { memo } from "react";
import stringWidth from "string-width";
import wrapAnsi from "wrap-ansi";
import type { UserItem } from "../../state/types.js";
import { useTerminalBg } from "../../theme/TerminalBgContext.js";
import { useBootCardWidth } from "../boot/BootCardFrame.js";

/**
 * Bubble palette — per terminal-bg kind. The dark-canvas variant is
 * the historical look (a slight grey panel that reads as "raised"
 * against a near-black bg). The light-canvas variant is the
 * Slack-style chip that fits a white bg. Both pairs are above WCAG
 * AAA (≥7:1) contrast, so readability survives any terminal font.
 *
 * Picked by ``useTerminalBg()`` in the component body — the
 * Context's initial value comes from a boot-time OSC 11 probe (see
 * ``utils/terminalBg.ts``) and updates if a future slash command
 * calls ``setKind`` for a manual flip.
 */
const USER_BUBBLE_PALETTE: Record<"light" | "dark", { bg: string; fg: string }> = {
  light: { bg: "#EEEEEE", fg: "#333333" }, // Slack-chip on white canvas
  dark: { bg: "#303030", fg: "#D4D4D4" }, // Slight-raised panel on black canvas
};

/** Right-pad ``s`` with spaces until it fills ``cols`` visual cells. */
function padToWidth(s: string, cols: number): string {
  const w = stringWidth(s);
  if (w >= cols) return s;
  return s + " ".repeat(cols - w);
}

const UserMessageInternal: React.FC<{ item: UserItem }> = ({ item }) => {
  const width = useBootCardWidth();
  // Pick the bubble palette based on the detected terminal bg. The
  // Context value comes from a boot-time OSC 11 probe (see
  // utils/terminalBg.ts); ``useTerminalBg()`` returns 'dark' as the
  // fallback when detection failed (older / non-standard terminals),
  // which matches the historical look the rest of the chrome was
  // tuned against.
  const { bg, fg } = USER_BUBBLE_PALETTE[useTerminalBg()];
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
        <Text key={i} color={fg} backgroundColor={bg}>
          {padToWidth(` ${line} `, width)}
        </Text>
      ))}
    </Box>
  );
};

// React.memo: UserMessage carries the per-line wrap+pad pass, which
// is cheap but still wasteful when ``item.text`` hasn't changed.
// Default shallow compare on ``item`` ref is the right gate — user
// items are immutable post-USER_SUBMITTED.
export const UserMessage = memo(UserMessageInternal);
