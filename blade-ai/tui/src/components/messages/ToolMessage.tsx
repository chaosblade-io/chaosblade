/**
 * Tool call — TWO visual forms (Phase 4.A).
 *
 * **Compact form** (``isPending=true``): single-line entry used while
 * the tool is still in the dynamic frame. Renders as
 *
 *   [T1] ⊶ kubectl   2.3s · phase1
 *
 * One row regardless of body content. The chip's status colour
 * (orange running / green ✓ / red ✗) carries the state, the
 * elapsed/node tail provides context. Body output is suppressed in
 * this form because the dynamic frame must stay small — every byte
 * gets erase+rewritten on every Ink commit, and on terminals without
 * DEC 2026 (Apple Terminal in particular) those erases produce
 * visible flicker proportional to the dynamic frame's row count.
 *
 * **Full bordered card form** (``isPending=false``): used in
 * committed history. Static is append-only, so this card paints once
 * and never repaints — the 5-12 row footprint costs nothing visually.
 * Visual grammar (bordered card with title chip / section rule /
 * body / hint block):
 *
 * Generic renderer for any tool the agent can invoke — no shape /
 * format assumptions about the result payload. Agnostic over:
 *
 *   - Tool name        (any LangGraph ToolNode invocation — built-in
 *                       agent tools, skill helpers, MCP tools, future
 *                       custom registrations; the renderer never
 *                       branches on tool identity)
 *   - Output format    (tabular / JSON / plain text / diff / binary
 *                       blob serialised to string — all rendered as
 *                       a head-truncated text body, no per-tool
 *                       parser)
 *   - Phase / node     (free-form string, displayed as a hint chip;
 *                       absent for tools that don't carry one)
 *
 * Each invocation renders its own card so a multi-call turn reads
 * as a stack of independent boxes instead of one monolithic block.
 *
 * Visual grammar:
 *
 *   ╭─────────────────────────────────────────────╮
 *   │ ▓ ✓ kubectl ▓                 491ms · phase1 │
 *   │ ──────                                       │
 *   │ ⎿ <first line of output>                     │
 *   │   <second line>                               │
 *   │ ──────                                       │
 *   │ … +K more lines                              │
 *   ╰─────────────────────────────────────────────╯
 *
 *   - Title row: status-coloured **inverse chip** carrying the status
 *     glyph + tool name; right cluster shows elapsed · node. The chip
 *     is the load-bearing visual signal — easier to scan than border
 *     colour alone, which gets washed out in dark terminals.
 *   - Single-line ``─`` rule (a Box with only ``borderTop`` enabled —
 *     same primitive ConfirmMessage and ResultCard use, so the three
 *     card families share a divider grammar) separates title from
 *     body, and body from the ``+N more lines`` footer or error
 *     hint block.
 *   - Round outer border tracks status (turquoise / amber / red).
 *
 * Border colour tracks status:
 *   success  → ``Theme.border.tool``      (turquoise — "agent action")
 *   running  → ``Theme.status.warnDim``   (dim gold — "in flight")
 *   error    → ``Theme.status.errDim``    (dim red — "broke here")
 *   canceled → ``Theme.status.warnDim``   (same dim gold; the ✗ in
 *                                          the chip carries the
 *                                          rejection signal)
 *
 * Output rules (see ``utils/toolOutput.ts``) — deliberately format-blind:
 *   - First N lines (default 5) of the cleaned raw body.
 *   - Trailing whitespace + blank tail rows stripped so verbose
 *     output (JSON dumps, log noise) doesn't grow the card with
 *     empty rows.
 *   - Footer ``… +K more lines`` when the cap clipped the tail.
 *   - No per-format parsing: tables stay as monospace text, JSON
 *     stays as JSON.
 *
 * Error path adds a ``next:`` hint block under the body (separated
 * by a section rule) using the same ``suggestionsForError`` matcher
 * used by chat-level errors — also tool-agnostic.
 */

import { Box, Text } from "ink";
import InkSpinner from "ink-spinner";
import { memo } from "react";
import { useTerminalSize } from "../../hooks/useTerminalSize.js";
import { t } from "../../i18n/index.js";
import type { ToolItem, ToolStatus } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";
import { ToolSpinner } from "../../theme/spinners.js";
import { suggestionsForError } from "../../utils/errorHints.js";
import {
  fitLineWidth,
  fitTextWidth,
  formatElapsed,
  truncateOutput,
} from "../../utils/toolOutput.js";

/** How many output lines to keep on the card before the
 *  ``… +N more lines`` footer kicks in. ``kubectl get pods`` /
 *  ``kubectl describe`` / log dumps routinely have 8–15 useful rows
 *  worth seeing at a glance — 12 fits a typical phase-1 inspection
 *  without dwarfing surrounding agent text. The body lives in
 *  ``<Static>`` history once committed, so this cap only affects
 *  visual density, not render performance. */
const MAX_OUTPUT_LINES = 12;
/** Tighter cap used while the tool is still rendering in the dynamic
 *  area (``isPending=true``). Multiple in-flight tool cards at the
 *  full 12-line cap could push the dynamic frame past ``stdout.rows``;
 *  with ``maxFps: 4`` capping write rate the visual cost of an
 *  occasional overflow is now a barely-noticeable blink rather than
 *  continuous flicker, so we can comfortably show 5 lines per
 *  in-flight tool (matching the pre-isPending default). Once the
 *  group is committed the tighter cap is dropped and the full 12
 *  lines render in history. */
const MAX_OUTPUT_LINES_PENDING = 5;

/** Left vertical rail colour — Forge × Operator redesign collapses
 *  the previous round-bordered card into a single left rail that
 *  signals "this is a tool block" without spending visual real
 *  estate on a full box. Rail tracks status so the eye can scan a
 *  vertical list of tools and pick out failures / runners. */
function railColorFor(status: ToolStatus): string {
  switch (status) {
    case "error":
      return Theme.status.err;
    case "canceled":
      return Theme.status.warnDim;
    default:
      // Success / running: neutral mid-gray rail. Previously this
      // returned ``forge.fire``, but that placed the brand color on
      // every tool card on screen (often 3-10 cards per agent turn)
      // → high-saturation overload. The mid-gray rail keeps the
      // Gestalt grouping (vertical line frames the tool block) while
      // letting the orange be a true accent on agent ``⏺`` only.
      //
      // Error / canceled keep their semantic status colors because
      // those ARE the warnings clig.dev / NNG say warm colors are
      // reserved for — they appear rarely and should grab attention.
      return Theme.text.secondary;
  }
}

/** Status-coded colour painted as the chip background (via Ink's
 *  ``inverse`` modifier — terminal-portable across ANSI palettes). */
function chipColorFor(status: ToolStatus): string {
  switch (status) {
    case "success":
      return Theme.status.ok;
    case "error":
      return Theme.status.err;
    case "canceled":
    case "running":
      return Theme.status.warn;
    default:
      return Theme.text.secondary;
  }
}

/** Status glyph carried inside the chip. ``running`` is special-cased
 *  by the chip renderer so the spinner can animate. */
function chipGlyphFor(status: ToolStatus): string {
  switch (status) {
    case "success":
      return Icons.success;
    case "error":
    case "canceled":
      return Icons.fail;
    default:
      return Icons.pending;
  }
}

/** Bracket-bounded chip: ``[ ✓ kubectl ]``.
 *
 *  Replaces the previous inverse-bg chip, which painted a saturated
 *  status-colored background on every tool card. With agents
 *  routinely calling 3-10 tools per turn, that meant the screen was
 *  perpetually full of colored chip blocks → eye fatigue. The new
 *  shape uses outline (closure) instead of fill:
 *
 *    - ``[`` ``]`` brackets in ``text.secondary`` give the chip its
 *      silhouette without a background block (Gestalt closure via
 *      outline, not fill).
 *    - The status-color glyph (``✓`` ok / ``⊶`` running / ``✗`` err)
 *      is the single high-information visual element — color now
 *      *means* something (status) instead of being decoration.
 *    - Tool name in ``bold`` default fg = the most scannable token
 *      on the line, in the terminal's native fg so it adapts to
 *      both light- and dark-bg themes.
 *
 *  Brand color (``forge.fire``) is intentionally absent here so it
 *  remains a true accent reserved for ``AgentMessage``'s ``⏺``
 *  glyph (per clig.dev "use color sparingly" + NNG "warm bright
 *  colors for warnings, sparingly").
 */
const ToolNameChip: React.FC<{ status: ToolStatus; name: string }> = ({
  status,
  name,
}) => {
  const glyphColor = chipColorFor(status);
  const Bracket: React.FC<{ children: string }> = ({ children }) => (
    <Text color={Theme.text.secondary}>{children}</Text>
  );
  return (
    <Text>
      <Bracket>[ </Bracket>
      {status === "running" ? (
        <Text color={glyphColor}>
          <InkSpinner type={ToolSpinner.type} />
        </Text>
      ) : (
        <Text color={glyphColor} bold>
          {chipGlyphFor(status)}
        </Text>
      )}
      <Text bold>{` ${name} `}</Text>
      <Bracket>]</Bracket>
    </Text>
  );
};

/** Single-line horizontal rule used as a section separator inside the
 *  tool block. Drawn dimly so the rail (the louder vertical line on
 *  the left) remains the dominant chrome element. */
const SectionRule: React.FC = () => (
  <Box
    borderStyle="single"
    borderTop={true}
    borderBottom={false}
    borderLeft={false}
    borderRight={false}
    borderColor={Theme.gray[700]}
  />
);

/**
 * Single-row compact form rendered while the tool is in the dynamic
 * frame (``isPending=true``). Not memo'd here — the parent
 * ``ToolMessage`` already memoises and decides which form to mount.
 *
 * Sizing: GUARANTEED 1 row regardless of body content, terminal
 * width, locator chip presence, or metadata length. Layout strategy
 * mirrors qwen-code's compact tool row (``ToolMessage.tsx:407``):
 * a SINGLE outer ``<Text wrap="truncate-end">`` contains every
 * span as nested ``<Text>`` children. Ink renders nested Text as
 * inline spans within the outer Text's word-wrap context, and
 * ``truncate-end`` clips overflow at the parent box's column edge
 * with ``…`` instead of wrapping to a new line. The outer ``<Box>``
 * exists only for ``paddingLeft`` (Ink Box can carry padding,
 * Text cannot).
 *
 * Why this matters: a previous ``<Box flexDirection="row">`` shape
 * with sibling Text spans inherited Ink's default ``wrap="wrap"``,
 * so a narrow-terminal render of a long tool name + locator + meta
 * could push the row to 2-3 visual lines, defeating the whole
 * "single-row compact form" promise. The Apple-Terminal flicker
 * mitigation depends on the dynamic frame staying small; even one
 * extra wrapped row across N concurrent tools multiplies into
 * meaningful frame growth.
 */
const ToolMessageCompact: React.FC<{ item: ToolItem }> = ({ item }) => {
  const elapsedStr = formatElapsed(item.elapsedMs);
  const chipColor = chipColorFor(item.status);
  const isRunning = item.status === "running";
  // Pre-build the metadata tail so the JSX stays flat. Both
  // ``elapsedStr`` and ``item.node`` are ``string`` (formatElapsed
  // returns ``""`` on missing ms; node is required on ToolItem) so
  // the ``||`` falsy check correctly skips the tail when both are
  // empty.
  const hasMeta = Boolean(elapsedStr) || Boolean(item.node);
  const metaSeparator = elapsedStr && item.node ? " · " : "";
  return (
    <Box paddingLeft={4}>
      <Text wrap="truncate-end">
        {item.locator ? (
          <>
            <Text color={Theme.text.secondary}>[</Text>
            <Text bold>{item.locator}</Text>
            <Text color={Theme.text.secondary}>] </Text>
          </>
        ) : null}
        {/* Status glyph carries the load-bearing signal — chip colour
         *  = state. Running tools spin the small inline spinner so
         *  the user can tell "still working" from "stuck". Other
         *  states use a static glyph. ``<InkSpinner>`` itself
         *  renders ``<Text>`` internally; nesting Text inside Text
         *  is well-supported by Ink (qwen-code's ``GeminiSpinner``
         *  uses the same pattern in their Footer / inline-spinner
         *  sites). */}
        {isRunning ? (
          <Text color={chipColor}>
            <InkSpinner type={ToolSpinner.type} />
          </Text>
        ) : (
          <Text color={chipColor} bold>
            {chipGlyphFor(item.status)}
          </Text>
        )}
        <Text bold>{` ${item.name}`}</Text>
        {hasMeta ? (
          <Text color={Theme.text.secondary}>
            {`  ·  ${elapsedStr}${metaSeparator}${item.node}`}
          </Text>
        ) : null}
      </Text>
    </Box>
  );
};

const ToolMessageInternal: React.FC<{
  item: ToolItem;
  isPending?: boolean;
  /** Per-card row budget set by ``MainContent``'s
   *  ``availableTerminalHeight``. When provided AND ``isPending``, the
   *  body line cap is the tighter of ``MAX_OUTPUT_LINES_PENDING`` and
   *  ``budget - card chrome``. Static history (committed) ignores
   *  this and uses the full ``MAX_OUTPUT_LINES`` cap. */
  availableTerminalHeight?: number;
}> = ({ item, isPending, availableTerminalHeight }) => {
  // ALWAYS call hooks unconditionally so the hook order stays stable
  // across the compact / full-card branches (a future change that
  // toggles ``isPending`` mid-life would otherwise violate Rules of
  // Hooks). ``columns`` is only consumed by the full-card branch but
  // calling ``useTerminalSize`` is free.
  const { columns } = useTerminalSize();
  // Phase 4.A — divert in-flight tools to the single-row compact form.
  // See the file-level docstring for the rationale (Apple Terminal
  // flicker, dynamic-frame size). Body / hint / locator absorption
  // happens in the full-card path below; the compact form is
  // intentionally read-only display.
  if (isPending) {
    return <ToolMessageCompact item={item} />;
  }
  const elapsedStr = formatElapsed(item.elapsedMs);
  const isErr = item.status === "error" || item.status === "canceled";
  const isRunning = item.status === "running";

  // Truncated body. Falls back to ``resultPreview`` (single-line
  // summary) when ``raw`` is empty — some legacy code paths populate
  // only the preview.
  //
  // Body line-cap pick:
  //   * Static history (``isPending`` falsy):
  //       use ``MAX_OUTPUT_LINES`` (12) — full preview burns once.
  //   * Pending without budget (e.g., constrainHeight=false, Ctrl+O):
  //       use ``MAX_OUTPUT_LINES`` (12) so the user sees the long
  //       output they asked for.
  //   * Pending with budget: pick the tighter of
  //       ``MAX_OUTPUT_LINES_PENDING`` (5) and ``budget - chrome``
  //       (~4 chrome rows: title + rule + optional placeholder + a
  //       breathing margin). Floor at 1 row.
  const baseCap = isPending ? MAX_OUTPUT_LINES_PENDING : MAX_OUTPUT_LINES;
  const lineCap = (() => {
    if (!isPending) return MAX_OUTPUT_LINES;
    if (availableTerminalHeight === undefined) return MAX_OUTPUT_LINES;
    const budget = Math.max(1, availableTerminalHeight - 4);
    return Math.max(1, Math.min(baseCap, budget));
  })();
  const truncated = truncateOutput(item.raw || item.resultPreview, lineCap);
  const hasBody = truncated.body.length > 0;
  const showBodyPlaceholder = !hasBody && !isRunning && item.status !== "canceled";
  const showRunningRow = isRunning && !hasBody;
  const renderBodySection = hasBody || showBodyPlaceholder || showRunningRow;
  const showMoreLines = hasBody && truncated.hiddenLines > 0;

  // Width budget for body text inside the bordered card. Subtractions:
  //   - 2  outer paddingLeft (tool card sits 2 cols off the terminal edge)
  //   - 2  border glyphs (left + right ``│``)
  //   - 2  inner paddingX (one space inside each border)
  //   - 2  ``⎿ `` tree-icon prefix and its trailing space
  // A 2-cell safety margin handles the case where Ink's Yoga and
  // ``wrap-ansi`` disagree by one cell (the tail end of the
  // misalignment story explained at the top of this file). Floor at
  // 20 so absurdly narrow terminals still produce *something*
  // readable rather than an all-ellipsis card.
  const bodyTextWidth = Math.max(20, columns - 10);
  // Hint suggestions sit under the body, indented by ``paddingLeft={2}``
  // and prefixed by a bullet glyph. Slightly narrower budget so the
  // bullet + space fit before the truncation cap kicks in.
  const hintTextWidth = Math.max(20, columns - 12);

  const fittedBody = hasBody ? fitTextWidth(truncated.body, bodyTextWidth) : "";

  const railColor = railColorFor(item.status);
  const hint = item.status === "error" ? suggestionsForError(item.raw) : null;

  return (
    <Box paddingLeft={4} marginTop={1} flexDirection="column">
      {/* ``paddingLeft={4}`` (was 2) — tool card is indented 2 cols
       *  more than AgentMessage's ``paddingLeft={2}``, so tools sit
       *  visually subordinate to the agent text above. Combined with
       *  the gray rail (vs agent's no-rail) and the bracketed chip
       *  (vs agent's ⏺ glyph), this gives 3 independent contrast
       *  axes between the two block types (NNG: "Don't rely only on
       *  color to communicate visual hierarchy").
       *
       *  Left rail still uses Ink's ``borderLeft`` for a single
       *  ``│`` running the full height of the Box. Now in
       *  ``text.secondary`` mid-gray (see railColorFor) so it
       *  groups the tool block by structure rather than by
       *  competing for attention. */}
      <Box
        flexDirection="column"
        borderStyle="single"
        borderLeft
        borderTop={false}
        borderBottom={false}
        borderRight={false}
        borderColor={railColor}
        paddingLeft={1}
      >
        {/* Title row: [T#] · chip [glyph + name] · elapsed · node
         *
         * The ``[T#]`` prefix surfaces the per-session locator the
         * reducer assigned at TOOL_ENDED time so users can run
         * ``/show T3`` / ``/expand T3`` without scrollback hunting.
         * Hidden when ``locator`` is unset (running tool that
         * hasn't been finalised yet — its locator is allocated
         * exactly at the first TOOL_ENDED). The slot is rendered
         * with secondary color so it doesn't compete with the
         * chip's status-coded background. */}
        <Box>
          {item.locator && (
            <Box marginRight={1}>
              {/* Locator chip mirrors ToolNameChip exactly: secondary-
               *  grey brackets + bold default-fg inner text. Why
               *  default fg instead of an explicit colour: the tool
               *  name beside this chip is also default-fg-bold, so
               *  ``[T1]`` and ``[ ✓ activate_skill ]`` read as a
               *  visual pair regardless of light- or dark-terminal
               *  background. The previous ``gray.300`` looked dim
               *  on light terminals (low contrast against off-white)
               *  while the tool name stayed crisp — visually inconsistent. */}
              <Text>
                <Text color={Theme.text.secondary}>[</Text>
                <Text bold>{item.locator}</Text>
                <Text color={Theme.text.secondary}>]</Text>
              </Text>
            </Box>
          )}
          <ToolNameChip status={item.status} name={item.name} />
          {/* Elapsed + node metadata pinned with a fixed 2-col gap
           *  AFTER the tool-name chip — NOT pushed to the right edge
           *  with ``flexGrow``. Reason: with the previous spacer, the
           *  visual gap between ``]`` and ``5ms`` jumped from 0 to
           *  20+ cols depending on the tool name's length (long names
           *  collapsed the spacer, short names blew it open). The
           *  metadata is informational, not a right-rail status —
           *  reading it next to the chip flows better than chasing
           *  the cursor across the row. */}
          {(elapsedStr || item.node) && (
            <Box marginLeft={2}>
              {elapsedStr && (
                <Text color={Theme.text.secondary}>{elapsedStr}</Text>
              )}
              {item.node && elapsedStr && (
                <Text color={Theme.text.secondary}> · </Text>
              )}
              {item.node && (
                <Text color={Theme.text.secondary}>{item.node}</Text>
              )}
            </Box>
          )}
        </Box>

        {/* Section rule between title and body. Skipped when there is
         *  no body section so the card never ends on a dangling rule. */}
        {renderBodySection && <SectionRule />}

        {/* Output body. Each line is pre-fitted to ``bodyTextWidth`` so
         *  no row exceeds the inner content area — Ink never has to
         *  wrap, and the right border stays at a fixed column. The
         *  ``wrap="truncate-end"`` is belt-and-braces: if our fit
         *  miscounts by one cell (CJK boundary edge case), Ink
         *  truncates rather than wrapping into the border. */}
        {hasBody && (
          <Box>
            <Text color={Theme.text.secondary}>{Icons.tree} </Text>
            <Box flexGrow={1}>
              <Text
                color={isErr ? Theme.status.err : Theme.text.secondary}
                wrap="truncate-end"
              >
                {fittedBody}
              </Text>
            </Box>
          </Box>
        )}

        {/* Empty-output placeholder for non-running, non-canceled calls.
         *  ``running`` is communicated by the spinner chip; ``canceled``
         *  is communicated by the strikethrough X icon. The ``(no output)``
         *  line only fires for success / error calls whose tool genuinely
         *  emitted nothing — we still want the reader to see a row so
         *  the card doesn't look truncated. */}
        {showBodyPlaceholder && (
          <Box>
            <Text color={Theme.text.secondary}>
              {Icons.tree} {t(item.placeholderKey ?? "tool.no_output")}
            </Text>
          </Box>
        )}

        {/* Running spinner placeholder — keeps height stable while the
         *  call is in flight so the card doesn't reflow when results
         *  arrive. */}
        {showRunningRow && (
          <Box>
            <Text color={Theme.text.secondary}>
              {Icons.tree} {t("tool.running")}
            </Text>
          </Box>
        )}

        {/* Section rule + hidden-line counter. Only shows when output
         *  was actually clipped (``hasBody && hiddenLines > 0``); when
         *  the tool fits in MAX_OUTPUT_LINES the body is the whole
         *  story and a footer rule would be visual noise. */}
        {showMoreLines && <SectionRule />}
        {showMoreLines && (
          <Box>
            <Text color={Theme.gray[500]}>
              {t("tool.more_lines", { n: String(truncated.hiddenLines) })}
            </Text>
          </Box>
        )}

        {/* Error hint block — actionable next-step under the body,
         *  separated by its own rule so it reads as a distinct
         *  semantic section rather than free-floating output. */}
        {hint && <SectionRule />}
        {hint && (
          <Box flexDirection="column">
            <Text color={Theme.text.secondary}>{t("error.next_label")}</Text>
            {hint.suggestions.map((s, i) => (
              <Box key={i} paddingLeft={2}>
                <Text color={Theme.text.secondary} wrap="truncate-end">
                  {Icons.bullet} {fitLineWidth(s, hintTextWidth)}
                </Text>
              </Box>
            ))}
          </Box>
        )}
      </Box>
    </Box>
  );
};

// React.memo: ToolMessage renders inside the streaming hot loop —
// every committed-tool card in history would re-render on every
// TOKEN_APPENDED without memo, even though only the in-flight tool's
// item ref changes. Default shallow compare on (item, isPending,
// availableTerminalHeight) catches the no-op case. Safe: no
// useEffectEvent in this tree.
export const ToolMessage = memo(ToolMessageInternal);
