/**
 * ``/experiments`` card — bordered fault-catalog index. Same
 * info-card family as RuntimeDoctorCard / HelpCard / SessionCard
 * (forge.fire chrome, header with summary tail, bulleted rows).
 *
 * Row grammar:
 *
 *   •  Pod_OOM内存异常              Pod 内存使用率接近 Limit 上限
 *   •  Node_CPU使用率过高           节点 CPU 使用率持续超过 90%
 *
 * Flat layout (no category nesting) — typical skill packs have
 * one use case per category, and grouping would print a heading
 * over every single row. The categories still arrive in the
 * server's dictionary-iteration order so visually-adjacent rows
 * are still loosely clustered by topic.
 *
 * CJK alignment is via ``string-width``: Yoga (Ink's layout engine)
 * counts code points, not terminal cells, so a CJK string laid out
 * with ``minWidth={N}`` under-pads by exactly the CJK char count.
 * Pre-padding the name string in cell units (each CJK char = 2)
 * sidesteps Yoga entirely — the rendered Text is then a
 * "fixed-width" ASCII-equivalent string Yoga can size correctly.
 */

import { Box, Text } from "ink";
import { memo } from "react";
import stringWidth from "string-width";
import { t } from "../i18n/index.js";
import type {
  ExperimentsCardItem,
  ExperimentsCardRow,
} from "../state/types.js";
import { Theme } from "../theme/colors.js";
import { Icons, isAsciiMode } from "../theme/icons.js";
import { useBootCardWidth } from "./boot/BootCardFrame.js";

/** Bullet glyph (1 cell) + 2 cells gap, mirroring the other
 *  info-card families so a stack of doctor / help / session /
 *  experiments cards line up column-for-column. */
const GLYPH_COL_WIDTH = 3;
/** Cells reserved for ``use_case_name``. 32 fits the worst case in
 *  the bundled skill packs ("节点容器运行时磁盘使用率过高" = 28
 *  cells) with a 4-cell gutter before the symptom column. A name
 *  longer than 32 cells overflows into the symptom area; the symptom
 *  truncates via ``wrap="truncate-end"`` rather than wrap, keeping
 *  rows single-line. */
const NAME_COL_WIDTH = 32;

/** Title chip glyph. U+2726 four-pointed black star — "collection of
 *  things", single cell. ASCII fallback uses ``*`` so terminals
 *  without Unicode fonts still print a readable header. */
const TITLE_ICON = isAsciiMode ? "*" : "✦";

/** Right-pad a string with spaces until its rendered cell width
 *  reaches ``target``. Returns as-is when already wider — caller
 *  decides whether to truncate or let it overflow. */
function padToCellWidth(s: string, target: number): string {
  const deficit = target - stringWidth(s);
  return deficit > 0 ? s + " ".repeat(deficit) : s;
}

/** Format ISO timestamp as ``YYYY-MM-DD HH:MM:SS`` for the header
 *  tail — mirrors the other info-cards so the column lines up when
 *  stacked. */
function formatDateTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return `${date} ${time}`;
}

const Row: React.FC<{ row: ExperimentsCardRow }> = ({ row }) => {
  const namePadded = padToCellWidth(row.useCaseName, NAME_COL_WIDTH);
  const symptom = row.faultSymptom || t("experiments.card.symptom_empty");
  return (
    <Box>
      <Box minWidth={GLYPH_COL_WIDTH}>
        <Text color={Theme.text.secondary}>{Icons.bullet}</Text>
      </Box>
      {/* Inline the pre-padded name as plain text so Yoga doesn't try
       *  to size it (Yoga doesn't know CJK = 2 cells). Trailing
       *  spaces in ``namePadded`` are the column gutter. */}
      <Box>
        <Text>{namePadded}</Text>
      </Box>
      <Box flexGrow={1}>
        <Text color={Theme.text.secondary} wrap="truncate-end">
          {symptom}
        </Text>
      </Box>
    </Box>
  );
};

const ExperimentsCardInternal: React.FC<{ item: ExperimentsCardItem }> = ({
  item,
}) => {
  const width = useBootCardWidth();
  const time = formatDateTime(item.capturedAt);
  const countTail = t("experiments.card.count", { n: item.totalCount });

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
            {TITLE_ICON} {t("experiments.card.title")}
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

        {item.rows.map((row, i) => (
          <Row key={i} row={row} />
        ))}
      </Box>
    </Box>
  );
};

// React.memo: ExperimentsCard's rows array is fixed at dispatch
// time. Shallow compare on the ``item`` ref skips re-padding /
// re-rendering N rows during streaming.
export const ExperimentsCard = memo(ExperimentsCardInternal);
