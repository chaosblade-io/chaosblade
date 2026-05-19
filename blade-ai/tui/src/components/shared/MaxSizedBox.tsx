/**
 * Constrain children's row count to ``maxHeight`` and report overflow.
 *
 * Contract:
 *   - Each direct child is treated as ONE row (typically ``<Box><Text>...</Text></Box>``
 *     or a single ``<Text>`` element with no embedded newlines).
 *   - When child count exceeds ``maxHeight``, the LAST ``maxHeight - 1``
 *     children are kept (tail-preferred — user sees the most recent
 *     output, which is what matters for streaming logs / tool stdout)
 *     and a single dim "(+N more lines)" row is prepended.
 *   - Overflow is signalled to the surrounding ``OverflowProvider`` so
 *     ``<ShowMoreLines>`` can render the "Press ctrl-s to show more"
 *     hint outside the affected card.
 *
 * Why not the more general qwen-code MaxSizedBox (628 lines):
 *   - We don't need wrap-aware truncation: every consumer already
 *     pre-wraps via ``wrapText`` / ``tailWrappedLines`` and feeds in
 *     one-line-per-child arrays. Counting children is an honest
 *     proxy for row count.
 *   - We don't need direction-toggling: tail clip is always right
 *     for our use cases (latest tool output, latest streaming token,
 *     newest thinking line).
 *   - Simpler implementation = fewer edge cases that could shift
 *     content height between renders and re-trigger Ink's
 *     overflow-redraw branch we're trying to escape.
 */

import { Children, useEffect } from "react";
import { Box, Text } from "ink";
import { useOverflowActions } from "../../contexts/OverflowContext.js";
import { Theme } from "../../theme/colors.js";
import { t } from "../../i18n/index.js";

export interface MaxSizedBoxProps {
  children?: React.ReactNode;
  /** Cap on visible rows. ``undefined`` disables the cap entirely
   *  (used when ``constrainHeight`` is toggled off, or when the
   *  caller is in a Static context where overflow doesn't matter). */
  maxHeight?: number;
  /** Stable identifier for ``OverflowContext`` tracking. Required when
   *  the caller wants ``<ShowMoreLines>`` to react to overflow on this
   *  particular block. Omit for dumb truncation without reporting. */
  overflowId?: string;
}

/**
 * Minimum useful cap. Below this, the truncation indicator + one row
 * of content barely communicates anything; cap the cap upward so we
 * always render at least 2 rows when content exists.
 */
const MIN_CAP = 2;

export const MaxSizedBox: React.FC<MaxSizedBoxProps> = ({
  children,
  maxHeight,
  overflowId,
}) => {
  const overflowActions = useOverflowActions();
  const childArray = Children.toArray(children);
  const cap = maxHeight === undefined ? Infinity : Math.max(MIN_CAP, maxHeight);
  const overflowing = childArray.length > cap;
  const hiddenCount = overflowing ? childArray.length - (cap - 1) : 0;
  const visibleChildren = overflowing
    ? childArray.slice(childArray.length - (cap - 1))
    : childArray;

  useEffect(() => {
    if (!overflowActions || !overflowId) return;
    if (overflowing) {
      overflowActions.addOverflowing(overflowId);
      return () => overflowActions.removeOverflowing(overflowId);
    }
    overflowActions.removeOverflowing(overflowId);
    return undefined;
  }, [overflowing, overflowActions, overflowId]);

  return (
    <Box flexDirection="column">
      {overflowing && (
        <Box>
          <Text color={Theme.text.secondary} italic>
            {t("overflow.more_lines", { count: hiddenCount })}
          </Text>
        </Box>
      )}
      {visibleChildren}
    </Box>
  );
};
