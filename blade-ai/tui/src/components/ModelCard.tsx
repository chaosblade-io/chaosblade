/**
 * ``/model list`` card — bordered model-catalog index. Same
 * info-card family as the doctor / help / session / experiments
 * cards (forge.fire chrome, header with summary tail, section
 * dividers for groups).
 *
 * Row grammar:
 *
 *   ●  qwen3.6-max-preview        ← bold + forge.fire (active)
 *   ○  qwen-max                   ← dim glyph + default text
 *
 * Three layers of visual contrast carry "this is the active row":
 *   · glyph fill (● vs ○)
 *   · weight (bold vs regular)
 *   · colour (forge.fire vs default)
 *
 * No need for a trailing "current default" tag — the row's own
 * appearance is self-evident.
 *
 * The ``apiBaseUrl`` is rendered as a subhead under the title (it's
 * session-level metadata, NOT per-model). Custom models (active id
 * not in the curated list) surface as their own ``custom`` section
 * at the bottom, with a dim note.
 */

import { Box, Text } from "ink";
import { memo } from "react";
import { t } from "../i18n/index.js";
import type {
  ModelCardItem,
  ModelCardRow,
} from "../state/types.js";
import { Theme } from "../theme/colors.js";
import { isAsciiMode } from "../theme/icons.js";
import { useBootCardWidth } from "./boot/BootCardFrame.js";

/** Bullet glyph (1 cell) + 2 cells gap, mirroring the other
 *  info-cards so a stack of them lines up column-for-column. */
const GLYPH_COL_WIDTH = 3;
/** Cells reserved for the model id column. 28 fits the longest
 *  curated id (``qwen3.6-max-preview`` = 19) with a 9-cell gutter
 *  before the optional dim note column. */
const NAME_COL_WIDTH = 28;
/** Fixed length of trailing dashes after ``── <provider> ``.
 *  Matches HelpCard. */
const SECTION_DASHES = 30;

/** Title-strip icon. U+25C6 black diamond — "selected/active",
 *  fits the "active model" semantic. ASCII fallback uses ``*``. */
const TITLE_ICON = isAsciiMode ? "*" : "◆";

/** Row glyphs — filled/empty circle pair reads as a radio-button
 *  selection. ASCII fallback uses ``*`` / ``-`` so terminals
 *  without Unicode fonts still get a discernible distinction. */
const ACTIVE_GLYPH = isAsciiMode ? "*" : "●";
const INACTIVE_GLYPH = isAsciiMode ? "-" : "○";

/** Format ISO timestamp as ``YYYY-MM-DD HH:MM:SS`` for the header
 *  tail — same formatter every info-card uses. */
function formatDateTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return `${date} ${time}`;
}

const Row: React.FC<{ row: ModelCardRow }> = ({ row }) => {
  const glyph = row.active ? ACTIVE_GLYPH : INACTIVE_GLYPH;
  const glyphColor = row.active ? Theme.text.accent : Theme.text.secondary;
  const nameColor = row.active ? Theme.text.accent : undefined;
  return (
    <Box>
      <Box minWidth={GLYPH_COL_WIDTH}>
        <Text color={glyphColor} bold={row.active}>
          {glyph}
        </Text>
      </Box>
      <Box minWidth={NAME_COL_WIDTH} width={NAME_COL_WIDTH}>
        <Text color={nameColor} bold={row.active} wrap="truncate-end">
          {row.id}
        </Text>
      </Box>
      {row.note && (
        <Box flexGrow={1}>
          <Text color={Theme.text.secondary} wrap="truncate-end">
            {row.note}
          </Text>
        </Box>
      )}
    </Box>
  );
};

const ModelCardInternal: React.FC<{ item: ModelCardItem }> = ({ item }) => {
  const width = useBootCardWidth();
  const time = formatDateTime(item.capturedAt);
  const dashTail = "─".repeat(SECTION_DASHES);
  const countTail = t("model.card.count", { n: item.totalCount });

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
        {/* Header: title chip + count + timestamp tail */}
        <Box marginBottom={1}>
          <Text color={Theme.border.diagnostic} bold>
            {TITLE_ICON} {t("model.card.title")}
          </Text>
          <Text color={Theme.text.secondary}>
            {"  · "}
            {countTail}
          </Text>
          {time && (
            <Text color={Theme.text.secondary}>
              {"  · "}
              {time}
            </Text>
          )}
        </Box>

        {/* api_base_url subhead — session-level metadata, not
         *  per-row. Hidden when empty. */}
        {item.apiBaseUrl && (
          <Box marginBottom={1}>
            <Box minWidth={NAME_COL_WIDTH - 6}>
              <Text color={Theme.text.secondary}>
                {t("model.base_url_label")}
              </Text>
            </Box>
            <Box flexGrow={1}>
              <Text wrap="truncate-end">{item.apiBaseUrl}</Text>
            </Box>
          </Box>
        )}

        {/* Provider sections. ``marginTop={1}`` on every section
         *  past the first gives breathing room between providers. */}
        {item.sections.map((section, sIdx) => (
          <Box key={sIdx} flexDirection="column" marginTop={sIdx === 0 ? 0 : 1}>
            <Box marginBottom={0}>
              <Text color={Theme.text.secondary}>
                ── {section.provider} {dashTail}
              </Text>
            </Box>
            {section.rows.map((row, rIdx) => (
              <Row key={rIdx} row={row} />
            ))}
          </Box>
        ))}

        {/* Tip — same dim/grey treatment as HelpCard's foot. */}
        <Box marginTop={1}>
          <Text color={Theme.text.secondary}>{t("model.card.tip")}</Text>
        </Box>
      </Box>
    </Box>
  );
};

// React.memo: walks sections × rows on every render. Item ref is
// stable post-/model dispatch.
export const ModelCard = memo(ModelCardInternal);
