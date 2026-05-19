/**
 * Welcome card — single boot-screen banner. Two-column layout with the
 * agent name + version embedded in the top border (Claude-Code style):
 *
 *   ╭─── ✻ blade-ai v0.1.0 ──────────...──╮
 *   │           Welcome back!     │  Tips for ... │
 *   │             █▄▄ █  …        │    • describe │
 *   │             model: ...      │  Runtime      │
 *   │             mode: ✗ confirm │    kubeconfig │
 *   ╰────────────────────────────────────────────╯
 *
 * Ink doesn't support a native ``title`` on bordered Boxes, so the top
 * row is rendered as a hand-drawn ``<Text>`` line (so we get coloured
 * border + coloured title text), and the body sits inside a Box with
 * ``borderTop={false}``. The vertical divider between the two columns
 * is the left-column Box's ``borderRight`` — Ink draws it for us using
 * the same character set as the outer border.
 */

import { Box, Text } from "ink";
import { useTerminalSize, NARROW_THRESHOLD } from "../../hooks/useTerminalSize.js";
import { t } from "../../i18n/index.js";
import type { WelcomeCardItem } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";
import { BootCardFrame, useBootCardWidth } from "./BootCardFrame.js";

// Half-block pixel-art logo for ``BLADE AI``. Each letter is a 3-cell
// × 2-line glyph that decodes to a 3-column × 4-row pixel grid via
// the upper/lower-half block characters (``█`` / ``▀`` / ``▄``).
//
// Letter pixel maps (for reference / future tweaks):
//   B   L   A   D   E   ' '   A   I
//   ██. █.. .██ ██. ███       .██ █..
//   █.█ █.. █.█ █.█ █..       █.█ █..
//   ██. █.. ███ █.█ ██.       ███ █..
//   █.█ ███ █.█ ██. ███       █.█ █..
//
// The earlier ``█▄▄ / █▄█`` for the first letter rendered as a
// lowercase ``b`` (single bowl on the bottom half — no top bar). The
// current ``█▀▄ / █▀▄`` produces a stylised capital ``B`` with two
// open bowls split by a centre bar, matching the casing of the
// rest of the brand line in the title border (``Blade-ai``).
const LOGO_LINES = [
  "█▀▄ █   ▄▀█ █▀▄ █▀▀  ▄▀█ █",
  "█▀▄ █▄▄ █▀█ █▄▀ ██▄  █▀█ █",
];

// Logo is 27 monospace cells wide. With border + paddingX={2} we need
// roughly 32 cols of *content* width to render the logo without
// truncation. Below this we drop the logo; above we keep it.
const LOGO_MIN_COLS = 40;

// Border + title colours. Accent (lavender) is the project's designated
// emphasis colour per theme/colors.ts — using it for the frame makes the
// boot banner pop the way Claude Code's orange frame does.
const BORDER_COLOR = Theme.text.accent;
const TITLE_NAME_COLOR = Theme.text.primary;
const TITLE_GLYPH_COLOR = Theme.text.accent;
const VERSION_COLOR = Theme.text.secondary;

function prettyPath(p: string, maxLen = 40): string {
  if (!p) return "(default)";
  const home = process.env["HOME"] ?? "";
  let s = home && p.startsWith(home) ? "~" + p.slice(home.length) : p;
  if (s.length <= maxLen) return s;
  const base = s.split("/").pop() ?? s;
  return ".../" + base;
}

/**
 * Build the components that go on the top-border line. We split it into
 * coloured chunks: ``╭─── `` (border) + glyph + name + version + `` ──...─╮`` (border).
 * Each chunk's visual width sums to exactly ``cardWidth`` so the body
 * Box (rendered immediately below) shares the same left/right edges.
 */
function topBorderSegments(
  cardWidth: number,
  glyph: string,
  name: string,
  versionText: string,
): {
  leadDashes: string;
  trailDashes: string;
} {
  // Fixed chars: ╭ (1) + leading dashes (3) + " " (1) + glyph (1) + " " (1) + name + " " + version + " " (1) + trailing dashes + ╮ (1)
  // Glyph ✻ is width-stable per icons.ts; name/version are ASCII. So
  // visual width === string length for these inputs.
  const FIXED_LEAD = 1 /* ╭ */ + 3 /* dashes */ + 1 /* space */;
  const FIXED_TRAIL = 1 /* space */ + 1 /* ╮ */;
  const titlePart = glyph.length + 1 /* space */ + name.length + 1 /* space */ + versionText.length;
  const trail = Math.max(3, cardWidth - FIXED_LEAD - titlePart - FIXED_TRAIL);
  return {
    leadDashes: "─".repeat(3),
    trailDashes: "─".repeat(trail),
  };
}

export const WelcomeCard: React.FC<{ item: WelcomeCardItem }> = ({ item }) => {
  const { columns } = useTerminalSize();
  const cardWidth = useBootCardWidth();
  // Two breakpoints rather than one: a moderately narrow tmux pane
  // (~50 cols) still benefits from the logo, just stacked vertically;
  // a very narrow split (≤40 cols) drops the logo to avoid a wrapped
  // mess. NARROW_THRESHOLD=60 from the shared hook so the cut-over
  // point matches Footer / SlashMenu behaviour.
  const stackVertically = columns <= NARROW_THRESHOLD;
  const showLogo = columns > LOGO_MIN_COLS;

  const modeGlyph = item.permissionMode === "auto" ? "⚡" : "✗";
  const modeLabel =
    item.permissionMode === "auto"
      ? t("welcome.mode.auto")
      : t("welcome.mode.confirm");
  const tips = [
    t("welcome.tip.describe"),
    t("welcome.tip.help"),
    t("welcome.tip.doctor"),
    t("welcome.tip.retry"),
    t("welcome.tip.mode"),
  ];

  // Mode glyph carries pass/warn semantics: ⚡ auto means "no
  // confirm-gate" — louder, gold; ✗ confirm means "human-in-the-loop"
  // — accent amber, signals the brand-default safe path.
  const modeGlyphColor =
    item.permissionMode === "auto" ? Theme.status.warn : Theme.text.accent;

  const leftBlock = (
    <Box flexDirection="column" alignItems="center">
      {/* "Welcome back!" promoted to bold + accent so it reads as the
       *  greeting headline rather than ambient chrome — it pairs
       *  visually with the logo immediately below. */}
      <Text color={Theme.text.accent} bold>
        {t("welcome.welcome_back")}
      </Text>
      {showLogo && (
        <Box marginTop={1} flexDirection="column">
          {LOGO_LINES.map((line) => (
            <Text key={line} color={Theme.text.accent} bold>
              {line}
            </Text>
          ))}
        </Box>
      )}
      {/* Model is the single most-asked "what am I running" fact —
       *  bold + accent so it pops; one extra blank line above to
       *  separate it from the logo block. */}
      <Box marginTop={1}>
        <Text color={Theme.text.accent} bold>
          {item.modelName || "(unknown model)"}
        </Text>
      </Box>
      <Box marginTop={1}>
        <Text color={Theme.text.secondary}>
          {t("welcome.mode_label")}:{" "}
        </Text>
        <Text color={modeGlyphColor} bold>
          {modeGlyph} {modeLabel}
        </Text>
      </Box>
    </Box>
  );

  const rightBlock = (
    <Box flexDirection="column">
      <Text color={Theme.text.accent} bold>
        {t("welcome.tips_header")}
      </Text>
      <Box marginTop={1} flexDirection="column">
        {tips.map((tip) => (
          <Box key={tip}>
            <Text color={Theme.text.secondary}>    • </Text>
            <Text color={Theme.text.primary} wrap="wrap">
              {tip}
            </Text>
          </Box>
        ))}
      </Box>
      <Box marginTop={1}>
        <Text color={Theme.text.accent} bold>
          {t("welcome.runtime_header")}
        </Text>
      </Box>
      <Box marginTop={1}>
        <Text color={Theme.text.secondary}>      kubeconfig: </Text>
        <Text color={Theme.text.primary}>{prettyPath(item.kubeconfig)}</Text>
      </Box>
      <Box>
        <Text color={Theme.text.secondary}>      namespace:  </Text>
        <Text color={Theme.text.primary}>{item.namespace}</Text>
      </Box>
      <Box marginTop={1}>
        <Text color={Theme.text.secondary} italic>
          {t("welcome.bottom_hint")}
        </Text>
      </Box>
    </Box>
  );

  const glyph = Icons.thinking;
  // Brand display name. Capital ``B`` matches the project's casing
  // convention (Blade-ai) — the previous lowercase ``blade-ai`` only
  // matched the CLI binary name. The pixel-art logo on the left
  // already renders BLADE AI uppercase, so the title border stays
  // mixed-case to balance visual weight without doubling the shout.
  const name = "Blade-ai";
  const versionText = `v${item.version}`;

  // Narrow terminals (≤60 cols) can't fit the title into the top border
  // without overflowing — fall back to the plain BootCardFrame and put
  // the title as the first row inside the box, same convention as the
  // other two boot cards.
  if (stackVertically) {
    return (
      <BootCardFrame paddingY={1}>
        <Box marginBottom={1}>
          <Text color={TITLE_GLYPH_COLOR} bold>
            {glyph}
          </Text>
          <Text color={TITLE_NAME_COLOR} bold>{` ${name}`}</Text>
          <Text color={VERSION_COLOR}>{` ${versionText}`}</Text>
        </Box>
        <Box flexDirection="column">
          {leftBlock}
          <Box marginTop={1}>{rightBlock}</Box>
        </Box>
      </BootCardFrame>
    );
  }

  const { leadDashes, trailDashes } = topBorderSegments(
    cardWidth,
    glyph,
    name,
    versionText,
  );

  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="column">
      {/*
        Top border: hand-drawn so the title can sit inside it. ``width``
        must be set explicitly — without it the parent flex shrinks the
        Box and Ink wraps each ``<Text>`` to fit, which scrambles the
        border characters. The coloured chunks add up to exactly
        ``cardWidth`` visual cells, matching the body Box below.
      */}
      <Box width={cardWidth} flexShrink={0}>
        <Text color={BORDER_COLOR}>{`╭${leadDashes} `}</Text>
        <Text color={TITLE_GLYPH_COLOR} bold>
          {glyph}
        </Text>
        <Text color={TITLE_NAME_COLOR} bold>{` ${name}`}</Text>
        <Text color={VERSION_COLOR}>{` ${versionText}`}</Text>
        <Text color={BORDER_COLOR}>{` ${trailDashes}╮`}</Text>
      </Box>

      {/*
        Body: borderTop suppressed so it stacks cleanly under the manual
        title line; left/right/bottom borders form the rest of the box.
      */}
      <Box
        width={cardWidth}
        flexDirection="column"
        borderStyle="round"
        borderTop={false}
        borderColor={BORDER_COLOR}
        paddingX={0}
        paddingY={0}
      >
        {/* Wide: 40/60 split with a coloured vertical divider in the
            middle — drawn by the left column's ``borderRight``.
            ``justifyContent="center"`` on the left column makes the
            brand block (Welcome / logo / model / mode) sit at the
            vertical middle of the card; without it the four short
            lines pile at the top while the right column's longer
            content stretches the card height, leaving an awkward
            blank lower half on the left. */}
        <Box flexDirection="row">
          <Box
            flexBasis="45%"
            flexShrink={0}
            flexDirection="column"
            justifyContent="center"
            paddingX={2}
            paddingY={1}
            borderStyle="round"
            borderTop={false}
            borderBottom={false}
            borderLeft={false}
            borderRight={true}
            borderColor={BORDER_COLOR}
          >
            {leftBlock}
          </Box>
          <Box
            flexBasis="55%"
            flexDirection="column"
            paddingX={2}
            paddingY={1}
          >
            {rightBlock}
          </Box>
        </Box>
      </Box>
    </Box>
  );
};
