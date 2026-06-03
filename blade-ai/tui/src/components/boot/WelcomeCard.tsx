/**
 * Welcome card — single boot-screen banner. Two-column layout with
 * the agent name + version embedded in the top border:
 *
 *   ╭─── ✻ Blade-ai v0.1.0 ──────────...──╮
 *   │           Welcome back!     │  Tips for ... │
 *   │             █▄▄ █  …        │    • describe │
 *   │             model: ...      │  Runtime      │
 *   │             mode: ✗ confirm │    kubeconfig │
 *   ╰────────────────────────────────────────────╯
 *
 * Ink doesn't support a native ``title`` on bordered Boxes, so the
 * top row is rendered as a hand-drawn ``<Text>`` line (so we get a
 * coloured border + coloured title text), and the body sits inside
 * a Box with ``borderTop={false}``. The vertical divider between
 * the two columns is the left-column Box's ``borderRight``.
 *
 * Forge × Operator palette: border + title glyph use ``forge.fire``
 * (the brand orange). Layout / structure unchanged from the
 * pre-redesign double-column version.
 */

import { Box, Text } from "ink";
import { memo } from "react";
import { useTerminalSize, NARROW_THRESHOLD } from "../../hooks/useTerminalSize.js";
import { t } from "../../i18n/index.js";
import type { WelcomeCardItem } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";
import { BootCardFrame, useBootCardWidth } from "./BootCardFrame.js";

// Half-block pixel-art logo for ``BLADE AI``. Each letter is a
// 3-cell × 2-line glyph that decodes to a 3-column × 4-row pixel
// grid via upper/lower-half block characters (``█`` / ``▀`` / ``▄``).
const LOGO_LINES = [
  "█▀▄ █   ▄▀█ █▀▄ █▀▀  ▄▀█ █",
  "█▀▄ █▄▄ █▀█ █▄▀ ██▄  █▀█ █",
];

// Logo is 27 monospace cells wide. With border + paddingX={2} we
// need ~32 cols of content width to render without truncation.
const LOGO_MIN_COLS = 40;

// Border + title colours — forge.fire family across the boot deck.
const BORDER_COLOR = Theme.forge.fire;
const TITLE_NAME_COLOR = Theme.text.primary;
const TITLE_GLYPH_COLOR = Theme.forge.fire;
const VERSION_COLOR = Theme.text.secondary;

function prettyPath(p: string, maxLen = 40): string {
  if (!p) return "(default)";
  const home = process.env["HOME"] ?? "";
  let s = home && p.startsWith(home) ? "~" + p.slice(home.length) : p;
  if (s.length <= maxLen) return s;
  const base = s.split("/").pop() ?? s;
  return ".../" + base;
}

/** Build the top-border line segments. Splits the line into coloured
 *  chunks (border + glyph + name + version + border) whose visual
 *  widths sum to ``cardWidth`` so the body Box below shares the same
 *  left/right edges. */
function topBorderSegments(
  cardWidth: number,
  glyph: string,
  name: string,
  versionText: string,
): {
  leadDashes: string;
  trailDashes: string;
} {
  const FIXED_LEAD = 1 /* ╭ */ + 3 /* dashes */ + 1 /* space */;
  const FIXED_TRAIL = 1 /* space */ + 1 /* ╮ */;
  const titlePart =
    glyph.length + 1 + name.length + 1 + versionText.length;
  const trail = Math.max(3, cardWidth - FIXED_LEAD - titlePart - FIXED_TRAIL);
  return {
    leadDashes: "─".repeat(3),
    trailDashes: "─".repeat(trail),
  };
}

const WelcomeCardInternal: React.FC<{ item: WelcomeCardItem }> = ({ item }) => {
  const { columns } = useTerminalSize();
  const cardWidth = useBootCardWidth();
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

  const modeGlyphColor =
    item.permissionMode === "auto" ? Theme.status.warn : Theme.text.accent;

  const leftBlock = (
    <Box flexDirection="column" alignItems="center">
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
        <Text color={Theme.text.secondary}>{t("welcome.bottom_hint")}</Text>
      </Box>
    </Box>
  );

  const glyph = Icons.thinking;
  const name = "Blade-ai";
  const versionText = `v${item.version}`;

  // Narrow fallback: drop the title-in-border trick and use plain
  // BootCardFrame with the title as the first inner row.
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
      {/* Top border: hand-drawn so the title can sit inside it. */}
      <Box width={cardWidth} flexShrink={0}>
        <Text color={BORDER_COLOR}>{`╭${leadDashes} `}</Text>
        <Text color={TITLE_GLYPH_COLOR} bold>
          {glyph}
        </Text>
        <Text color={TITLE_NAME_COLOR} bold>{` ${name}`}</Text>
        <Text color={VERSION_COLOR}>{` ${versionText}`}</Text>
        <Text color={BORDER_COLOR}>{` ${trailDashes}╮`}</Text>
      </Box>

      {/* Body: borderTop suppressed so it stacks cleanly under the
       *  manual title line; left/right/bottom borders form the rest. */}
      <Box
        width={cardWidth}
        flexDirection="column"
        borderStyle="round"
        borderTop={false}
        borderColor={BORDER_COLOR}
        paddingX={0}
        paddingY={0}
      >
        {/* Wide: 45/55 split with a coloured vertical divider drawn
         *  by the left column's ``borderRight``. */}
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

// React.memo: WelcomeCard's item ref is set once at boot and never
// changes. The card is expensive (logo lines, two-column layout,
// hand-drawn border) — shallow compare skips re-rendering it on
// every downstream state churn.
export const WelcomeCard = memo(WelcomeCardInternal);
