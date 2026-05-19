/**
 * Stack of consecutive tool calls.
 *
 * Each tool — running or done, single or multi-tool group — renders as
 * a full bordered ``ToolMessage`` card with its body preview. The
 * earlier compact chip mode (``isPending && tools.length >= 2`` →
 * single-row glyph + name) was a flicker workaround that targeted
 * Ink's fullscreen-redraw branch: once the dynamic frame exceeded
 * ``stdout.rows``, ``shouldClearTerminalForFrame``'s ``wasOverflowing``
 * flag stayed true and every subsequent frame re-wrote
 * ``fullStaticOutput`` (Welcome card / BootDoctor / all history) into
 * the viewport, causing the "boot cards keep reappearing at the
 * bottom" behaviour and scroll-position drift. The structural fix
 * landed in ``patches/ink+7.0.3.patch`` (the fullscreen branch no
 * longer writes ``fullStaticOutput``), so overflow is now harmless —
 * dynamic frame can grow past viewport, content scrolls into
 * scrollback naturally, Static items stay where they were originally
 * painted. Hiding the per-tool body during streaming traded
 * information density for visual stability we no longer need.
 *
 * Why keep ``ToolGroupMessage`` (instead of mapping ``ToolMessage``
 * directly in ``HistoryItemDisplay``): the reducer's ``TOOL_STARTED``
 * groups consecutive tool calls into a single ``ToolGroupItem`` so
 * the discriminated-union router stays uniform — the group is the
 * unit ``HistoryItemDisplay`` dispatches on, regardless of how many
 * tools are inside.
 */

import { Box } from "ink";
import type { ToolGroupItem } from "../../state/types.js";
import { ToolMessage } from "./ToolMessage.js";

export const ToolGroupMessage: React.FC<{
  item: ToolGroupItem;
  isPending?: boolean;
  availableTerminalHeight?: number;
}> = ({ item, isPending, availableTerminalHeight }) => {
  // Distribute the budget evenly across in-flight tools so a
  // multi-tool concurrent group can't claim viewport rows N times
  // over. Single-tool groups get the whole budget.
  const perTool =
    availableTerminalHeight === undefined
      ? undefined
      : Math.max(4, Math.floor(availableTerminalHeight / Math.max(1, item.tools.length)));
  return (
    <Box flexDirection="column">
      {item.tools.map((tool) => (
        <ToolMessage
          key={tool.id}
          item={tool}
          isPending={isPending}
          availableTerminalHeight={perTool}
        />
      ))}
    </Box>
  );
};
