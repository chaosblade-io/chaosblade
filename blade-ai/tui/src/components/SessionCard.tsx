/**
 * ``/session`` card — bordered info table, sibling to
 * RuntimeDoctorCard + HelpCard in the "info-card" family (same
 * forge.fire chrome, header with ``· timestamp`` tail, bulleted
 * rows). Flat layout (no sections) — 6-7 fields don't warrant
 * help-style section dividers.
 *
 * Row grammar:
 *
 *   •  session id            sess_ef505e990bda
 *   •  cluster               (none)              ← dim placeholder
 *   •  permission mode       auto
 *
 * Pure presentational — the host (``/session`` slash handler) builds
 * the row list at dispatch time, snapshotting field labels and
 * (placeholder vs real) flags so the card stays static after burn-in.
 */

import { Box, Text } from "ink";
import { memo } from "react";
import { t } from "../i18n/index.js";
import type { SessionCardItem, SessionCardRow } from "../state/types.js";
import { Theme } from "../theme/colors.js";
import { Icons, isAsciiMode } from "../theme/icons.js";
import { useBootCardWidth } from "./boot/BootCardFrame.js";

/** Bullet glyph (1 cell) + 2 cells gap, mirroring RuntimeDoctorCard's
 *  GLYPH_COL_WIDTH so the two cards align column-for-column when
 *  stacked in scrollback. */
const GLYPH_COL_WIDTH = 3;
/** Cells reserved for the field-name column. 20 fits the longest
 *  label (``permission mode`` = 15 cells) with a 5-cell gutter
 *  before the value. */
const NAME_COL_WIDTH = 20;

/** Title chip glyph. U+25C9 fisheye reads as "currently active",
 *  fitting "this is your current session". ASCII fallback uses
 *  ``*`` so terminals without Unicode fonts still print something
 *  readable in the header. */
const TITLE_ICON = isAsciiMode ? "*" : "◉";

/** Format ISO timestamp as ``YYYY-MM-DD HH:MM:SS`` for the header
 *  tail — mirrors RuntimeDoctorCard's formatter so consecutive doctor
 *  / help / session cards share the same temporal column. */
function formatDateTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return `${date} ${time}`;
}

const Row: React.FC<{ row: SessionCardRow }> = ({ row }) => (
  <Box>
    <Box minWidth={GLYPH_COL_WIDTH}>
      <Text color={Theme.text.secondary}>{Icons.bullet}</Text>
    </Box>
    <Box minWidth={NAME_COL_WIDTH}>
      <Text>{row.label}</Text>
    </Box>
    <Box flexGrow={1}>
      <Text
        color={row.dim ? Theme.text.secondary : undefined}
        wrap="truncate-end"
      >
        {row.value}
      </Text>
    </Box>
  </Box>
);

const SessionCardInternal: React.FC<{ item: SessionCardItem }> = ({ item }) => {
  const width = useBootCardWidth();
  const time = formatDateTime(item.capturedAt);

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
            {TITLE_ICON} {t("session.card.title")}
          </Text>
          {time && (
            <Text color={Theme.text.secondary}>
              {"  · "}
              {time}
            </Text>
          )}
        </Box>

        {item.rows.map((row, i) => (
          <Row key={i} row={row} />
        ))}
      </Box>
    </Box>
  );
};

// React.memo: SessionCard's item ref is stable post-/session dispatch.
// Shallow compare prevents re-walking the rows array during downstream
// streaming activity.
export const SessionCard = memo(SessionCardInternal);
