/**
 * Shared bordered frame for boot-era cards (Welcome, BootDoctor,
 * PendingTasks, Goodbye). Single round border in ``forge.fire`` (the
 * brand orange) so all four cards read as one visual family.
 *
 * Internal layout is each consumer's own concern — this frame only
 * provides the bordered Box + outer indent + width policy. The
 * earlier title/metadata strip experiment was reverted; callers
 * render their own headers inside the box, matching the pre-redesign
 * structure.
 */

import { Box } from "ink";
import type { ReactNode } from "react";
import { useTerminalSize } from "../../hooks/useTerminalSize.js";
import { Theme } from "../../theme/colors.js";

const OUTER_HORIZONTAL_PAD = 4;
const CARD_MIN_WIDTH = 32;

export function useBootCardWidth(): number {
  const { columns } = useTerminalSize();
  return Math.max(CARD_MIN_WIDTH, columns - OUTER_HORIZONTAL_PAD);
}

export interface BootCardFrameProps {
  children: ReactNode;
  /** Inner vertical padding inside the border. WelcomeCard /
   *  GoodbyeCard use ``1`` so brand / stats blocks breathe; the
   *  lighter info cards default to ``0``. */
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
        borderColor={Theme.forge.fire}
        paddingX={2}
        paddingY={paddingY}
        width={width}
      >
        {children}
      </Box>
    </Box>
  );
};
