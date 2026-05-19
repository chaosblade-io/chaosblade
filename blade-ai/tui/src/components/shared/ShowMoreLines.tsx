/**
 * Footer hint rendered when one or more pending items reported overflow
 * via ``MaxSizedBox`` / ``OverflowContext``. Tells the user there's
 * truncated content and how to expand it (Ctrl+S toggles
 * ``constrainHeight`` in the reducer).
 *
 * Visibility rule: only show during ``idle`` or ``waiting_confirmation``.
 * Mid-stream the truncated tail is constantly being replaced as new
 * tokens land — the message would flicker as items overflow / un-
 * overflow on each frame, which is more noisy than helpful.
 */

import { Box, Text } from "ink";
import { useOverflowState } from "../../contexts/OverflowContext.js";
import { useAppSelector } from "../../state/store.js";
import { t } from "../../i18n/index.js";
import { Theme } from "../../theme/colors.js";

export const ShowMoreLines: React.FC = () => {
  const overflow = useOverflowState();
  const streamState = useAppSelector((s) => s.streamState);
  const constrainHeight = useAppSelector((s) => s.constrainHeight);

  if (!overflow || overflow.overflowingIds.size === 0) return null;
  if (!constrainHeight) return null;
  // Suppress while tokens are streaming — the truncate edge moves on
  // every render, the hint reads as flickering chrome rather than info.
  if (streamState !== "idle" && streamState !== "waiting_confirmation")
    return null;

  return (
    <Box paddingLeft={2} marginTop={1}>
      <Text color={Theme.text.secondary} italic>
        {t("overflow.show_more_hint")}
      </Text>
    </Box>
  );
};
