/**
 * Shared frame for the three boot-screen cards (Welcome, BootDoctor,
 * PendingTasks). They previously each rendered their own bordered Box
 * without a width, so Ink shrunk every card to its content and the
 * staircase of right edges looked broken. Centralising the frame here
 * gives them all the same terminal-derived width and keeps the
 * left-padding / border / paddingX boilerplate in one place.
 */

import { Box } from "ink";
import type { ReactNode } from "react";
import { useTerminalSize } from "../../hooks/useTerminalSize.js";
import { Theme } from "../../theme/colors.js";

// Outer wrapper pads 2 cols on the left; we leave a matching 2-col
// gutter on the right so the card doesn't hug the terminal edge.
const OUTER_HORIZONTAL_PAD = 4;
const CARD_MIN_WIDTH = 32;

/**
 * Width allocated to a boot card's outer Box (border included).
 * Exported as a hook so WelcomeCard — which renders a custom title-in-
 * border layout rather than using the frame — stays in lockstep with the
 * other two cards.
 */
export function useBootCardWidth(): number {
  const { columns } = useTerminalSize();
  return Math.max(CARD_MIN_WIDTH, columns - OUTER_HORIZONTAL_PAD);
}

export interface BootCardFrameProps {
  children: ReactNode;
  /** Inner vertical padding inside the border. WelcomeCard uses 1, the others 0. */
  paddingY?: number;
}

export const BootCardFrame: React.FC<BootCardFrameProps> = ({
  children,
  paddingY = 0,
}) => {
  const width = useBootCardWidth();
  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="column">
      <Box
        flexDirection="column"
        borderStyle="round"
        borderColor={Theme.text.accent}
        paddingX={2}
        paddingY={paddingY}
        width={width}
      >
        {children}
      </Box>
    </Box>
  );
};
