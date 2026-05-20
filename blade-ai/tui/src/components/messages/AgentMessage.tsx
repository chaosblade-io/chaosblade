/**
 * Agent reply with real markdown rendering.
 *
 * The single ⏺ leader (text.accent purple) is followed by the
 * marked-rendered body. The body string already carries its own ANSI
 * color sequences (set by marked-terminal), so the wrapping <Text>
 * MUST NOT set a ``color`` prop or Ink will double-apply and produce
 * artifacts.
 *
 * useMemo on item.text avoids re-parsing on every render — important
 * because token streaming triggers a re-render per token chunk and
 * marked.parse on a multi-KB body is non-trivial work.
 *
 * ``isPending=true`` (the streaming path) caps the visible body to the
 * tail of the rendered output that fits in the current viewport
 * minus a chrome reserve (LoadingIndicator + InputPrompt + Footer +
 * optional PhaseStepper strip). Without this cap a multi-screen
 * agent reply pushes the dynamic frame past ``stdout.rows`` on every
 * token — Ink's render-loop falls into its fullscreen-redraw branch
 * (``eraseScreen + cursorTo(0,0) + fullStaticOutput + output``) and
 * the user sees continuous flicker + the terminal auto-scrolling
 * the viewport back to live position on every frame (the reported
 * "scroll wheel hijack"). Mirrors the height-cap strategy qwen-code
 * applies via its ``MaxSizedBox``. ``isPending`` is unset once the
 * item is committed to ``<Static>`` history → full text shown there.
 */

import { Box, Text } from "ink";
import { useMemo } from "react";
import { useTerminalSize } from "../../hooks/useTerminalSize.js";
import { t } from "../../i18n/index.js";
import type { AgentItem } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";
import { renderMarkdown } from "../../utils/markdown.js";

/** Rows we reserve for everything in the dynamic frame OTHER than this
 *  AgentMessage's own visible body. Covers, worst-case, the full
 *  Composer chrome (PhaseStepper ≈ 6 + LoadingIndicator with body block
 *  ≈ 14 (header 1 + separator 1 + BODY_MAX_LINES up to 12) + InputPrompt
 *  3 + Footer 2 + Composer marginTop 1 = 26), PLUS a 5-row buffer for:
 *    · any leading-stable item (a Thinking row or completed ToolGroup)
 *      that's queued for flush but not yet committed to ``<Static>``
 *      because the flush only fires inside TOKEN_APPENDED / TOOL_STARTED
 *    · this component's own ``marginTop=1``
 *    · 1 row of safety margin
 *  = 31 rounded to 32.
 *
 *  Why over-budgeting matters: the moment ``outputHeight >= stdout.rows``
 *  fires for a single tick, Ink's render path falls into its
 *  fullscreen-redraw branch (``eraseScreen + cursorTo(0,0) +
 *  fullStaticOutput + output``). On a long agent reply that fires every
 *  Spinner tick (~80ms) — the user perceives continuous flicker AND
 *  the terminal auto-scrolls the viewport back to live position to
 *  follow the cursor (the reported "scroll wheel hijack"). Erring on
 *  the small-frame side trims at most a few rows of agent body which
 *  the user can scroll up to read in scrollback after the turn ends. */
const PENDING_CHROME_RESERVE = 32;
/** Floor for the visible budget so a tiny terminal still shows
 *  *something* of the streaming reply rather than the bare truncation
 *  hint. */
const PENDING_MIN_VISIBLE = 5;

export const AgentMessage: React.FC<{
  item: AgentItem;
  isPending?: boolean;
  /** Per-pending-item row budget from ``MainContent``. When provided
   *  takes precedence over the legacy ``rows - PENDING_CHROME_RESERVE``
   *  estimate; the budget already accounts for the chrome that lives
   *  below pending (Composer + InputPrompt + Footer + stepper). */
  availableTerminalHeight?: number;
}> = ({ item, isPending, availableTerminalHeight }) => {
  const { columns, rows } = useTerminalSize();
  // Reserve 4 columns for paddingLeft={2} + the leading "⏺ " glyph.
  // The marked-terminal cache buckets widths to 4-col steps, so a
  // 1-col resize doesn't churn the cache.
  const width = Math.max(20, columns - 4);
  const rendered = useMemo(
    () => renderMarkdown(item.text, width),
    [item.text, width],
  );

  // Tail-clip when streaming. Splitting on ``\n`` and slicing keeps any
  // ANSI styling within each line intact (marked-terminal resets at
  // line ends). Replaced prefix gets a localized "+N earlier lines"
  // hint so the user knows why their reply appears mid-paragraph.
  const visibleText = useMemo(() => {
    if (!isPending) return rendered;
    // Prefer the explicit budget from MainContent; fall back to the
    // self-computed one for callers that haven't been migrated yet
    // (shouldn't happen in production — every pending dispatch
    // routes through HistoryItemDisplay which forwards the budget).
    const budget =
      availableTerminalHeight !== undefined
        ? Math.max(PENDING_MIN_VISIBLE, availableTerminalHeight)
        : Math.max(PENDING_MIN_VISIBLE, rows - PENDING_CHROME_RESERVE);
    const lines = rendered.split("\n");
    if (lines.length <= budget) return rendered;
    const dropped = lines.length - (budget - 1);
    return [
      t("agent.truncated_earlier", { n: dropped }),
      ...lines.slice(-(budget - 1)),
    ].join("\n");
  }, [rendered, isPending, rows, availableTerminalHeight]);

  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="row">
      {/* Left forge.fire rail — Forge × Operator redesign. The rail
       *  runs the full height of the wrapped markdown body so
       *  multi-line agent replies stay visually anchored in the
       *  conversation channel that UserMessage / ThinkingMessage
       *  also live in. ``borderLeft`` only is Ink's most reliable
       *  primitive for a per-row vertical accent — Yoga sizes the
       *  Box to fit the wrapped Text's measured height. */}
      <Box
        borderStyle="single"
        borderLeft
        borderTop={false}
        borderBottom={false}
        borderRight={false}
        borderColor={Theme.forge.fire}
        paddingLeft={1}
        flexGrow={1}
      >
        <Text color={Theme.forge.fire}>{Icons.agent} </Text>
        <Box flexGrow={1}>
          <Text wrap="wrap">{visibleText}</Text>
        </Box>
      </Box>
    </Box>
  );
};
