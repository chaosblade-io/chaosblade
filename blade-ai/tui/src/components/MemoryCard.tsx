/**
 * ``/memory show`` snapshot card — doctor-style single-list view.
 *
 * Mirrors ``RuntimeDoctorCard``'s visual language:
 *   - bordered box (``Theme.border.diagnostic``)
 *   - one icon + label + timestamp header
 *   - one continuous row list with status glyph + name col + body col
 *   - sections separated by a single blank divider row (no
 *     section sub-headings — each row's name says what it is)
 *
 * Row grammar:
 *
 *     •  Session             sess_c74ee022867d
 *     ✓  Status              active
 *     •  Cluster             (未设置)
 *     ...
 *
 * ``•`` (info) is the default — most memory rows are factual not
 * pass/fail. ``✓`` is reserved for fields the operator might
 * actually want to confirm are set (status=active, namespace
 * non-default, etc.). ``⚠`` flags concerning state (no recent
 * tasks but stats show injections — could indicate session_store
 * loss).
 */

import { Box, Text } from "ink";
import type { MemoryCardItem } from "../state/types.js";
import { Theme } from "../theme/colors.js";
import { Icons } from "../theme/icons.js";

/** Width of the leading glyph cell (1 cell glyph + 2 cells gap). */
const GLYPH_COL_WIDTH = 3;
/** Width of the name column. 24 cells fits ``Recoveries`` /
 *  ``message_count`` / ``Injection success`` etc. with breathing
 *  room. Doctor uses 30; memory rows are shorter so we trim. */
const NAME_COL_WIDTH = 22;

export type MemoryRowStatus = "ok" | "warn" | "info";

export interface MemoryRow {
  /** Status glyph kind. Most rows are ``"info"`` (factual). */
  status: MemoryRowStatus;
  /** Short label — the "what" (e.g. ``Session``, ``Namespace``). */
  name: string;
  /** Body content (already-formatted string or a node). */
  body: React.ReactNode;
}

/** Re-export for callers that prefer importing the data type from
 *  the component module (e.g. preview scripts). The canonical
 *  declaration lives in ``state/types.ts`` so the reducer / commands
 *  can dispatch it without circular imports. */
export type { MemoryCardItem } from "../state/types.js";

function glyphForStatus(s: MemoryRowStatus): string {
  switch (s) {
    case "ok":
      return Icons.success;
    case "warn":
      return Icons.warning;
    case "info":
      return Icons.bullet;
  }
}

function colorForStatus(s: MemoryRowStatus): string | undefined {
  switch (s) {
    case "ok":
      return Theme.status.ok;
    case "warn":
      return Theme.status.warn;
    case "info":
      return Theme.text.secondary;
  }
}

/** Render ISO timestamp as ``YYYY-MM-DD HH:MM:SS`` (local zone).
 *  Mirror of RuntimeDoctorCard's formatter — kept inline so the
 *  two cards can drift independently if one ever needs a richer
 *  format (relative time, "5 min ago", etc.). */
function formatDateTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return `${date} ${time}`;
}

/** Translate snapshot data into the row list. Pure — every visual
 *  decision (status, glyph, body content) is decided here so the
 *  renderer stays presentational. */
function buildRows(data: MemoryCardItem): MemoryRow[] {
  const rows: MemoryRow[] = [];

  // — Session identity & lifecycle —
  rows.push({
    status: "info",
    name: "Session",
    body: <Text>{data.sessionId || "(未知)"}</Text>,
  });
  rows.push({
    status: data.status === "active" ? "ok" : "info",
    name: "Status",
    body: <Text>{data.status || "active"}</Text>,
  });
  rows.push({
    status: "info",
    name: "Started",
    body: (
      <Text>{data.startedAt ? formatDateTime(data.startedAt) : "—"}</Text>
    ),
  });

  // — Context (cluster + namespace) —
  rows.push({
    status: data.cluster ? "ok" : "info",
    name: "Cluster",
    body: data.cluster ? (
      <Text>{data.cluster}</Text>
    ) : (
      <Text color={Theme.text.secondary}>(未设置)</Text>
    ),
  });
  rows.push({
    status: data.namespace && data.namespace !== "default" ? "ok" : "info",
    name: "Namespace",
    body: (
      <Text>
        {data.namespace || (
          <Text color={Theme.text.secondary}>(未设置)</Text>
        )}
      </Text>
    ),
  });

  // — Recent tasks tail —
  // Header row carries the "shown/total" count; body lists the ids
  // (or "(无)" placeholder when empty). One row per task to keep the
  // doctor-style grid alignment, but truncate-end on long ids so a
  // narrow terminal doesn't push the layout sideways.
  const shown = data.recentTasks.length;
  rows.push({
    status: shown === 0 ? "info" : "ok",
    name: `Tasks (${shown}/${data.totalTasks})`,
    body:
      shown === 0 ? (
        <Text color={Theme.text.secondary}>(无最近任务)</Text>
      ) : (
        <Text>{data.recentTasks[shown - 1]}</Text>
      ),
  });
  // Additional task ids on their own rows (skip first which is in
  // the header row above), all dim because they're historical.
  for (let i = shown - 2; i >= 0; i--) {
    rows.push({
      status: "info",
      name: "",
      body: (
        <Text color={Theme.text.secondary} wrap="truncate-end">
          {data.recentTasks[i]}
        </Text>
      ),
    });
  }

  // — Stats —
  // Inject success / fail roll up into a single row for compactness.
  const msgCount = Number(data.stats["message_count"] ?? 0);
  const injCount = Number(data.stats["injection_count"] ?? 0);
  const injOk = Number(data.stats["injection_success"] ?? 0);
  const injFail = Number(data.stats["injection_fail"] ?? 0);
  const recCount = Number(data.stats["recovery_count"] ?? 0);

  rows.push({
    status: msgCount > 0 ? "ok" : "info",
    name: "Messages",
    body: <Text>{msgCount}</Text>,
  });
  rows.push({
    status: injFail > 0 ? "warn" : injCount > 0 ? "ok" : "info",
    name: "Injections",
    body: (
      <>
        <Text>{injCount}</Text>
        {injCount > 0 && (
          <Text color={Theme.text.secondary}>
            {"  ("}
            <Text color={Theme.status.ok}>{`✓ ${injOk}`}</Text>
            {" / "}
            <Text color={Theme.status.err}>{`✗ ${injFail}`}</Text>
            {")"}
          </Text>
        )}
      </>
    ),
  });
  rows.push({
    status: recCount > 0 ? "ok" : "info",
    name: "Recoveries",
    body: <Text>{recCount}</Text>,
  });

  // — Path —
  rows.push({
    status: "info",
    name: "Memory dir",
    body: (
      <Text color={Theme.text.secondary} wrap="truncate-end">
        {data.memoryDir || "—"}
      </Text>
    ),
  });

  return rows;
}

const Row: React.FC<{ row: MemoryRow }> = ({ row }) => (
  <Box>
    <Box minWidth={GLYPH_COL_WIDTH}>
      <Text color={colorForStatus(row.status)}>
        {row.name ? glyphForStatus(row.status) : " "}
      </Text>
    </Box>
    <Box minWidth={NAME_COL_WIDTH}>
      <Text>{row.name}</Text>
    </Box>
    <Box flexGrow={1}>{row.body}</Box>
  </Box>
);

export const MemoryCard: React.FC<{ item: MemoryCardItem }> = ({ item }) => {
  const rows = buildRows(item);
  const time = formatDateTime(item.capturedAt);
  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="column">
      <Box
        flexDirection="column"
        borderStyle="round"
        borderColor={Theme.border.diagnostic}
        paddingX={2}
        paddingY={1}
      >
        <Box marginBottom={1}>
          <Text color={Theme.border.diagnostic} bold>
            ◈ 会话记忆
          </Text>
          {time && (
            <Text color={Theme.text.secondary}>
              {"  · "}
              {time}
            </Text>
          )}
        </Box>
        {rows.map((row, i) => (
          <Row key={i} row={row} />
        ))}
      </Box>
    </Box>
  );
};
