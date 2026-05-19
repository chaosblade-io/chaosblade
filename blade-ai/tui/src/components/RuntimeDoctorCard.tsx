/**
 * Runtime ``/doctor`` diagnostic card — unified single-list view.
 *
 * Every diagnostic — runtime metadata (server URL / cluster / versions
 * / protocol / lang / mode) AND live preflight checks (LLM key /
 * kubeconfig / kubectl / blade / skills / k8s_connectivity /
 * chaosblade_operator) — renders as a row in one continuous list. No
 * internal section heading, no "M/N passed" tally; the row glyph and
 * its colour carry the status.
 *
 * Row grammar (uniform across the whole card):
 *
 *     ✓  <name>                 <message>            ← passing / known
 *     ✗  <name>                 <message>            ← blocking failure
 *     ⚠  <name>                 <message>            ← non-blocking warning
 *     •  <name>                 <message>            ← pure info, no pass/fail
 *
 * Border: ``Theme.border.diagnostic`` (medium violet) — distinct from
 * every other card border so a stack of scrollback artefacts still
 * tells the user "here is the diagnostic snapshot" at a glance.
 *
 * Suggested-fixes block: lists every row whose status is not ``ok`` and
 * carries an actionable hint — server-unreachable, protocol mismatch,
 * preflight unavailable, plus the per-check ``fix`` field returned by
 * ``run_tui_checks()`` server-side. Each fix is keyed by its row name
 * so the user can see which problem the hint addresses.
 */

import { Box, Text } from "ink";
import { t } from "../i18n/index.js";
import type { RuntimeDoctorCardItem } from "../state/types.js";
import { Theme } from "../theme/colors.js";
import { Icons } from "../theme/icons.js";
import { useBootCardWidth } from "./boot/BootCardFrame.js";

/** Width of the leading glyph cell (1 cell glyph + 2 cells gap). */
const GLYPH_COL_WIDTH = 3;
/** Width of the name column. 30 cells fits ``chaosblade_operator``
 *  (the longest preflight name, 19 chars) with breathing room AND
 *  matches the boot doctor card's ``CheckList`` so the two cards line
 *  up identically when stacked in scrollback. */
const NAME_COL_WIDTH = 30;

type RowStatus = "ok" | "warn" | "err" | "info";

interface DoctorRow {
  status: RowStatus;
  /** Short label — the "what" (e.g. ``server``, ``protocol``,
   *  ``llm_api_key``, ``chaosblade_operator``). */
  name: string;
  /** Render-prop body for the value/message column. Lets a row mix
   *  neutral text with a coloured tail (e.g. URL + ``(unreachable)``). */
  body: React.ReactNode;
  /** Remediation hint surfaced in the fixes block. Set on rows whose
   *  status is not ``ok`` and where there's something the user can do. */
  fix?: string;
}

function glyphForStatus(s: RowStatus): string {
  switch (s) {
    case "ok":
      return Icons.success;
    case "warn":
      return Icons.warning;
    case "err":
      return Icons.fail;
    case "info":
      return Icons.bullet;
  }
}

function colorForStatus(s: RowStatus): string | undefined {
  switch (s) {
    case "ok":
      return Theme.status.ok;
    case "warn":
      return Theme.status.warn;
    case "err":
      return Theme.status.err;
    case "info":
      return Theme.text.secondary;
  }
}

/** Render ISO timestamp as ``YYYY-MM-DD HH:MM:SS`` for the header tail.
 *  Local zone (matches the user's wall clock); date prefix is locale-
 *  neutral and sortable so multiple ``/doctor`` cards across days read
 *  in obvious chronological order. */
function formatDateTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return `${date} ${time}`;
}

/** Translate item state into the unified row list. Pure — every visual
 *  decision (status, glyph, body content, attached fix) is decided
 *  here so the renderer below stays presentational. */
function buildRows(item: RuntimeDoctorCardItem): DoctorRow[] {
  const rows: DoctorRow[] = [];

  // Server reachability — the load-bearing diagnostic. Failure here
  // typically explains every preflight failure that follows, so it
  // gets its own ``fix`` even though the user can usually figure it
  // out from the URL alone.
  if (item.reachable) {
    rows.push({
      status: "ok",
      name: t("doctor.server"),
      body: <Text>{item.serverUrl}</Text>,
    });
  } else {
    rows.push({
      status: "err",
      name: t("doctor.server"),
      body: (
        <>
          <Text>{item.serverUrl}</Text>
          <Text color={Theme.status.err}>
            {"  "}
            {t("doctor.server_unreachable")}
          </Text>
        </>
      ),
      fix: t("doctor.fix.server_unreachable"),
    });
  }

  // Cluster: empty is informational (no cluster set yet, expected in
  // some flows) — no fix.
  rows.push(
    item.cluster
      ? {
          status: "ok",
          name: t("doctor.cluster"),
          body: <Text>{item.cluster}</Text>,
        }
      : {
          status: "info",
          name: t("doctor.cluster"),
          body: (
            <Text color={Theme.text.secondary}>{t("doctor.cluster_none")}</Text>
          ),
        },
  );

  // TUI version is always known (read at build time) — pure info.
  rows.push({
    status: "info",
    name: t("doctor.tui_version"),
    body: <Text>{item.tuiVersion}</Text>,
  });

  // Server version: missing means the server endpoint didn't return one.
  // Could be an older build; surface as info (not error) since the
  // unreachable case has its own dedicated row above.
  rows.push(
    item.serverVersion
      ? {
          status: "ok",
          name: t("doctor.server_version"),
          body: <Text>{item.serverVersion}</Text>,
        }
      : {
          status: "info",
          name: t("doctor.server_version"),
          body: <Text color={Theme.text.secondary}>?</Text>,
        },
  );

  // Protocol mismatch is a warning — events may parse incorrectly;
  // attach an explicit fix so the user knows the remediation isn't just
  // "wait it out".
  const protoMismatch =
    item.serverProtocol !== null && item.serverProtocol !== item.tuiProtocol;
  if (protoMismatch) {
    rows.push({
      status: "warn",
      name: t("doctor.protocol"),
      body: (
        <>
          <Text>{item.tuiProtocol}</Text>
          <Text color={Theme.status.warn}>
            {" → "}
            {item.serverProtocol}
          </Text>
        </>
      ),
      fix: t("doctor.fix.protocol_mismatch"),
    });
  } else {
    rows.push({
      status: "ok",
      name: t("doctor.protocol"),
      body: <Text>{item.tuiProtocol}</Text>,
    });
  }

  // Language and mode are always known and never problematic — pure info.
  rows.push({
    status: "info",
    name: t("doctor.lang"),
    body: <Text>{item.lang}</Text>,
  });
  rows.push({
    status: "info",
    name: t("doctor.mode"),
    body: <Text>{item.mode}</Text>,
  });

  // Live preflight rows. When the endpoint itself is unavailable AND
  // the server is otherwise reachable, surface a single warn row with
  // a fix — the per-check rows below would be empty and silent
  // omission would read as "nothing to check" which is a confidently-
  // wrong report. When the server is unreachable, the dedicated
  // server row above already explains the gap; skip the duplicate.
  if (item.preflightUnavailable) {
    if (item.reachable) {
      rows.push({
        status: "warn",
        name: t("doctor.preflight"),
        body: (
          <Text color={Theme.status.warn}>{t("boot.doctor.unavailable")}</Text>
        ),
        fix: t("doctor.fix.preflight_unavailable"),
      });
    }
  } else {
    for (const c of item.checks) {
      const status: RowStatus = c.passed
        ? "ok"
        : c.severity === "warning"
          ? "warn"
          : "err";
      rows.push({
        status,
        name: c.name,
        body: (
          <Text color={colorForStatus(status)} wrap="truncate-end">
            {c.passed
              ? c.message?.trim() || t("boot.doctor.passed_short")
              : c.message}
          </Text>
        ),
        // Only attach a fix to actually-failing rows. Defensive: a
        // passing check with a stray ``fix`` field (server-side
        // contract drift) shouldn't leak into the suggestions block.
        fix: !c.passed && c.fix?.trim() ? c.fix.trim() : undefined,
      });
    }
  }

  return rows;
}

const Row: React.FC<{ row: DoctorRow }> = ({ row }) => (
  <Box>
    <Box minWidth={GLYPH_COL_WIDTH}>
      <Text color={colorForStatus(row.status)}>{glyphForStatus(row.status)}</Text>
    </Box>
    <Box minWidth={NAME_COL_WIDTH}>
      <Text>{row.name}</Text>
    </Box>
    <Box flexGrow={1}>{row.body}</Box>
  </Box>
);

export const RuntimeDoctorCard: React.FC<{ item: RuntimeDoctorCardItem }> = ({
  item,
}) => {
  const width = useBootCardWidth();
  const rows = buildRows(item);
  const fixes = rows.filter((r) => r.fix);
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
            {Icons.system} {t("doctor.head")}
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

        {fixes.length > 0 && (
          <Box marginTop={1} flexDirection="column">
            <Box>
              <Text color={Theme.text.accent} bold>
                {t("boot.doctor.fixes_header")}
              </Text>
            </Box>
            {fixes.map((row, i) => (
              <Box key={i} paddingLeft={2}>
                <Box minWidth={NAME_COL_WIDTH}>
                  <Text color={Theme.text.secondary}>{row.name}</Text>
                </Box>
                <Box flexGrow={1}>
                  <Text color={Theme.text.secondary} wrap="wrap">
                    {row.fix}
                  </Text>
                </Box>
              </Box>
            ))}
          </Box>
        )}
      </Box>
    </Box>
  );
};
