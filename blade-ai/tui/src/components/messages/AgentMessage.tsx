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
import { memo, useMemo } from "react";
import { useTerminalSize } from "../../hooks/useTerminalSize.js";
import { t } from "../../i18n/index.js";
import type { AgentItem } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";
import { renderMarkdown } from "../../utils/markdown.js";

/** Floor for the visible budget. Applied AFTER the
 *  ``PENDING_AGENT_MAX_VISIBLE`` cap so a tiny terminal (where
 *  ``availableTerminalHeight`` could shrink below the cap) still
 *  shows ≥ 5 rows of streaming reply rather than just the truncation
 *  prefix with no body. Inequality always holds:
 *  ``PENDING_MIN_VISIBLE (5) ≤ PENDING_AGENT_MAX_VISIBLE (8)``. */
const PENDING_MIN_VISIBLE = 5;
/** Hard cap on the pending agent's visible row count, regardless of
 *  how much height the terminal could afford.
 *
 *  Why a tight cap (and why 8): every token append grows the rendered
 *  ``visibleText`` by 0-1 visual rows, which forces Ink to repaint
 *  the entire dyn-frame block (``eraseLines(prev) + new content``).
 *  At 12-20Hz token rate, the user sees the dyn frame redraw 12-20
 *  times per second. The size of each redraw scales with the visible
 *  row count: a 30-row agent body on a 50-row terminal means each
 *  redraw rewrites most of the visible viewport, which the eye
 *  perceives as "整块都在抖" continuous shimmer. Capping at 8 rows
 *  shrinks the redraw payload to roughly a quarter — visually the
 *  rewrite is now confined to a small block near the bottom and
 *  dominated by the static chrome above + tool cards, so the eye
 *  reads it as "the live tail is updating" instead of "the whole
 *  screen is shaking".
 *
 *  Trade-off: when the agent body exceeds 8 rows, the user sees the
 *  prefix "+N earlier lines · full text in scrollback after turn"
 *  followed by the most recent 7 rows. Once the agent message
 *  commits to history (``isPending=false``), the full text is
 *  re-rendered in Static so nothing is permanently hidden — they can
 *  always scroll up to read the full reply. The qwen-code TUI uses
 *  the same pattern (``MaxSizedBox`` with a tight pin-height); this
 *  is the industry-standard solution for streaming-agent flicker. */
const PENDING_AGENT_MAX_VISIBLE = 8;

const AgentMessageInternal: React.FC<{
  item: AgentItem;
  isPending?: boolean;
  /** Per-pending-item row budget from ``MainContent``. Already
   *  accounts for the chrome that lives below pending (Composer +
   *  InputPrompt + Footer + stepper). The pending agent's visible
   *  height further caps to ``min(this, PENDING_AGENT_MAX_VISIBLE)``
   *  so streaming flicker doesn't scale with terminal height.
   *  ``undefined`` (Ctrl+O / constrainHeight=false) opts out of any
   *  cap — full content is rendered, viewport overflow accepted. */
  availableTerminalHeight?: number;
}> = ({ item, isPending, availableTerminalHeight }) => {
  const { columns } = useTerminalSize();
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
    // Ctrl+O (``constrainHeight=false``) sends ``availableTerminalHeight=
    // undefined`` to indicate the user EXPLICITLY wants full content
    // visible, accepting that the dyn frame may overflow viewport. In
    // that mode we skip the 8-row cap entirely — the user has opted
    // out of flicker mitigation in favour of seeing everything.
    if (availableTerminalHeight === undefined) return rendered;
    // Budget resolution (smallest wins — we want as tight a cap as
    // safely possible during streaming):
    //   1. ``PENDING_AGENT_MAX_VISIBLE`` (8) — hard cap that prevents
    //      streaming flicker from scaling with terminal height.
    //   2. ``availableTerminalHeight`` from MainContent — accounts
    //      for chrome below pending (controls + footer + stepper).
    // ``Math.max(PENDING_MIN_VISIBLE, ...)`` floors the result so a
    // tiny terminal still shows something rather than just the
    // "+N earlier lines" hint with no body.
    const budget = Math.max(
      PENDING_MIN_VISIBLE,
      Math.min(PENDING_AGENT_MAX_VISIBLE, availableTerminalHeight),
    );
    const lines = rendered.split("\n");
    if (lines.length <= budget) return rendered;
    const dropped = lines.length - (budget - 1);
    return [
      t("agent.truncated_earlier", { n: dropped }),
      ...lines.slice(-(budget - 1)),
    ].join("\n");
  }, [rendered, isPending, availableTerminalHeight]);

  // Continuation fragments come from the Phase 2.3 mid-stream split:
  // a long agent reply was carved at a markdown-safe paragraph
  // boundary (always immediately after a ``\n\n``), the head fragment
  // committed to history, and this fragment is the tail still
  // streaming OR a subsequent head fragment from a later split.
  // Continuations render WITHOUT the leading ⏺ glyph so the user
  // reads the whole logical reply as one flowing block instead of N
  // stacked items each with its own glyph.
  //
  // ``marginTop`` is preserved (NOT dropped) on continuations: the
  // split always lands right after a ``\n\n`` (paragraph break), so
  // each continuation is by construction a new paragraph that
  // semantically deserves an empty row before it. The marginTop of
  // the next fragment reconstructs the paragraph spacing that
  // ``renderMarkdown`` would have produced for the un-split version
  // — without it, an inline split would look like one wrapped
  // paragraph rather than the two-paragraph layout the LLM emitted
  // (``renderMarkdown`` strips trailing ``\n+`` per its contract,
  // so the head fragment can't carry the spacer itself).
  const isContinuation = item.continuation === true;
  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="row">
      {/* No left rail — agent body is the primary conversation
       *  channel. Only the ``⏺`` glyph carries the brand orange,
       *  making it the sole high-saturation accent on screen.
       *
       *  Why we removed the rail (was forge.fire borderLeft):
       *  ToolMessage uses an identical vertical rail at the same
       *  position, so both blocks shared the exact same visual
       *  signature → no hierarchy. The fix follows three principles:
       *
       *    1. clig.dev: "use color with intention … sparingly" —
       *       warm bright colors burn the eye when used on every
       *       row; reserve them for true accents.
       *    2. NNG (Nielsen Norman): "Reserve warm bright colors,
       *       like red [orange], for warnings or errors. Don't rely
       *       only on color to communicate visual hierarchy."
       *    3. Refactoring UI: "Don't use bright color for tags;
       *       use depth and outline instead." Agent body is a tag-
       *       free natural-flow region; tools get the outline.
       *
       *  Result: agent body reads as conversation; only the
       *  leading ⏺ is forge.fire (1-2 glyphs per turn instead of
       *  one long colored wall). Multi-axis contrast with
       *  ToolMessage now: color (orange-on-glyph vs gray-on-rail),
       *  presence-of-rail (no vs yes), indent (2 vs 4). Three
       *  independent axes means even color-blind / NO_COLOR /
       *  screenshot-paste users can still parse hierarchy.
       *
       *  Continuation fragments skip the glyph entirely (rendered as
       *  a 2-cell spacer to keep the body left-edge aligned with the
       *  head fragment above). */}
      {isContinuation ? (
        <Text>{"  "}</Text>
      ) : (
        <Text color={Theme.forge.fire}>{Icons.agent} </Text>
      )}
      <Box flexGrow={1}>
        <Text wrap="wrap">{visibleText}</Text>
      </Box>
    </Box>
  );
};

// React.memo: AgentMessage is the heaviest renderer on the streaming
// path — every TOKEN_APPENDED triggers a MainContent re-render, which
// re-walks history.map(...). The pending agent item's ``text`` field
// changes per token (new reference), so memo lets it through; every
// OTHER agent message in history has a stable text ref and skips the
// expensive ``renderMarkdown`` + ``visibleText`` useMemo chain.
//
// Safe for memo: no useEffectEvent. Default shallow compare handles
// the three props (``item`` ref, ``isPending`` bool, ``availableTerminalHeight``
// number|undef). ``item`` ref is stable per reducer principle (only
// the pending item is mutated; committed items keep identity).
export const AgentMessage = memo(AgentMessageInternal);
