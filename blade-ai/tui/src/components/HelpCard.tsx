/**
 * ``/help`` card — bordered command index, sibling to RuntimeDoctorCard
 * in the "info-card" family (same forge.fire chrome, same header
 * grammar). Renders the sections + rows assembled at dispatch time;
 * no registry coupling — the renderer is pure.
 *
 * Row grammar:
 *
 *   /clear                           Clear the terminal scrollback
 *                                                                       ← blank line
 *   /mode [calm|working|dense]       Toggle display density …
 *      list                          List candidate models            ← sub, indented
 *      set <model-id>                Switch the active model
 *
 * Layout knobs are kept aligned with the mock at /tmp/help_card_mock.py
 * so the renderer matches the design preview:
 *   NAME_COL_WIDTH = 33  cells reserved for "name + args"
 *   SUB_INDENT     = 3   cells of indent for subcommand rows
 *
 * Blank lines are inserted BEFORE every top-level command except the
 * first in its section, so adjacent root commands get vertical
 * breathing room while subcommands stay tight under their parent.
 */

import { Box, Text } from "ink";
import { memo } from "react";
import { t } from "../i18n/index.js";
import type { HelpCardItem, HelpCardRow } from "../state/types.js";
import { Theme } from "../theme/colors.js";
import { isAsciiMode } from "../theme/icons.js";
import { useBootCardWidth } from "./boot/BootCardFrame.js";

/** Cells reserved for the name+args column. 33 fits the longest
 *  root command (``/recover <task_id|latest|list>`` = 30 cells) with
 *  a small gutter before the description. */
const NAME_COL_WIDTH = 33;
/** Extra indent for sub-command rows. Picked so the sub-name column
 *  starts where ``/cmd`` would have been + 3 cells — visually obvious
 *  parentage without burning much horizontal real estate. */
const SUB_INDENT = 3;
/** Fixed length of the trailing dashes after ``── Heading ``. Keeps
 *  every section heading the same visual length irrespective of the
 *  heading text length. */
const SECTION_DASHES = 30;

/** Title-strip icon. U+2318 is single-cell and recognised universally
 *  as the command-key glyph. ASCII fallback uses ``#`` so a terminal
 *  without Unicode fonts gets a still-readable header rather than a
 *  tofu box. */
const TITLE_ICON = isAsciiMode ? "#" : "⌘";

/** Render ISO timestamp as ``YYYY-MM-DD HH:MM:SS`` for the header
 *  tail — mirrors the RuntimeDoctorCard formatter so consecutive
 *  ``/doctor`` + ``/help`` cards share the same temporal column. */
function formatDateTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return `${date} ${time}`;
}

const Row: React.FC<{ row: HelpCardRow }> = ({ row }) => {
  const indent = row.kind === "sub" ? SUB_INDENT : 0;
  const reserved = NAME_COL_WIDTH - indent;
  return (
    <Box>
      {indent > 0 && <Box width={indent} />}
      <Box minWidth={reserved} width={reserved}>
        <Text
          color={Theme.text.accent}
          bold={row.kind === "top"}
          wrap="truncate-end"
        >
          {row.name}
        </Text>
      </Box>
      <Box flexGrow={1}>
        <Text wrap="truncate-end">{row.description}</Text>
      </Box>
    </Box>
  );
};

const HelpCardInternal: React.FC<{ item: HelpCardItem }> = ({ item }) => {
  const width = useBootCardWidth();
  const time = formatDateTime(item.capturedAt);
  // Pre-build the dash string once — section heading lengths differ
  // only in the heading text, the tail is fixed.
  const dashTail = "─".repeat(SECTION_DASHES);

  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="column">
      <Box
        flexDirection="column"
        borderStyle="round"
        borderColor={Theme.border.diagnostic}
        paddingX={2}
        paddingY={1}
        width={width}
      >
        <Box marginBottom={1}>
          <Text color={Theme.border.diagnostic} bold>
            {TITLE_ICON} {t("help.card.title")}
          </Text>
          {time && (
            <Text color={Theme.text.secondary}>
              {"  · "}
              {time}
            </Text>
          )}
        </Box>

        {item.sections.map((section, sIdx) => (
          <Box key={sIdx} flexDirection="column" marginTop={sIdx === 0 ? 0 : 1}>
            <Box marginBottom={0}>
              <Text color={Theme.text.secondary}>
                ── {section.heading} {dashTail}
              </Text>
            </Box>
            {section.rows.map((row, rIdx) => {
              // Blank breathing line BEFORE every top-row except the
              // first row of the section. Subs stay tight under their
              // parent because they don't get the blank-line treatment.
              const needsSpacer = row.kind === "top" && rIdx > 0;
              return (
                <Box key={rIdx} flexDirection="column">
                  {needsSpacer && <Box height={1} />}
                  <Row row={row} />
                </Box>
              );
            })}
          </Box>
        ))}

        {item.tip && (
          <Box marginTop={1}>
            <Text color={Theme.text.secondary}>{item.tip}</Text>
          </Box>
        )}
      </Box>
    </Box>
  );
};

// React.memo: HelpCard walks N sections × M rows on every render —
// committed help items never change, so skipping when ``item`` ref is
// equal is a free win during downstream activity.
export const HelpCard = memo(HelpCardInternal);
