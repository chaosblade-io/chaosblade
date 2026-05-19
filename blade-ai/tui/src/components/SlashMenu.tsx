/**
 * Slash-command picker that floats below the InputPrompt while the
 * buffer starts with ``/``. Two modes, picked by InputPrompt's
 * ``computeSlashState`` helper:
 *
 *  - ``root`` mode (no whitespace yet, e.g. ``/he``): list all
 *    visible commands matching the prefix, grouped by category
 *    (general / business / skills / dynamic) with one-line section
 *    headers — the same layout Python's ``tui/commands.py`` produces.
 *
 *  - ``sub`` mode (e.g. ``/skills l``): the user finished typing a
 *    registered command followed by a space, the command has
 *    subcommands, and they're now picking one. We show the parent's
 *    sub list with a context line above so it's obvious which root
 *    we're under.
 *
 * Selection is driven from outside (InputPrompt owns the index) so
 * the input field can take ↑/↓ for browsing both menu rows AND
 * command history without crossed wires. We just render. Hidden
 * commands are filtered upstream by ``registry.list()``; this
 * component never renders a hidden entry.
 *
 * Visible row windowing: long candidate lists scroll within
 * ``VISIBLE_ROWS`` so the input field stays visible. The window
 * recenters around the selected index, with ``↑ N more`` / ``↓ N
 * more`` markers when items are clipped above / below.
 */

import { Box, Text } from "ink";
import { t } from "../i18n/index.js";
import {
  SLASH_GROUP_ORDER,
  type SlashCommand,
  type SlashGroup,
  type SlashSubcommand,
} from "../state/commands.js";
import { Theme } from "../theme/colors.js";
import { Icons } from "../theme/icons.js";

type Props =
  | {
      mode: "root";
      candidates: SlashCommand[];
      selectedIndex: number;
    }
  | {
      mode: "sub";
      parent: SlashCommand;
      subs: SlashSubcommand[];
      selectedIndex: number;
    };

// Width of the ``/<name> [usage]`` column. 16 fits short usages
// (``<NL>``, ``[N]``, ``<E#|T#>``); long ones like
// ``[calm|working|dense]`` or ``[active|failed|all] [N]`` overflow,
// but the description column still keeps a guaranteed 2-col gutter
// (see ``DESC_GUTTER`` below), so they read as
// ``/mode [calm|working|dense]  切换信息密度...`` instead of
// running glued together. Bumping NAME_COL further would waste a
// big chunk of horizontal space on every short-usage row.
const NAME_COL = 16;
// Minimum gap between the usage column and the description text —
// applied as ``marginLeft`` on the description Box so it survives the
// name+usage overflowing NAME_COL. Without it the description glues
// onto the usage when the name+usage exceeds the column budget,
// producing rows like ``/mode [calm|working|dense]切换信息密度`` (no
// space). 2 cols matches the menu's ``minWidth=2`` arrow gutter so
// the columns visually align.
const DESC_GUTTER = 2;
const VISIBLE_ROWS = 8;

export const SlashMenu: React.FC<Props> = (props) => {
  if (props.mode === "root") return <RootMenu {...props} />;
  return <SubMenu {...props} />;
};

// ── Root mode ────────────────────────────────────────────────────────

const GROUP_LABEL_KEY: Record<SlashGroup, string> = {
  general: "help.group.general",
  business: "help.group.business",
  skills: "help.group.skills",
  dynamic: "help.group.dynamic",
};

interface RowEntry {
  type: "header" | "cmd";
  group?: SlashGroup;
  cmd?: SlashCommand;
  /** Flat index across the visible cmd rows; -1 for headers (skipped
   *  by the windowing math but kept inline so headers travel with
   *  their group). */
  flatIdx: number;
}

/** Flatten the candidate list into a header-interspersed row sequence
 *  while preserving each command's flat index for selection mapping. */
function buildRootRows(candidates: SlashCommand[]): RowEntry[] {
  const byGroup = new Map<SlashGroup, SlashCommand[]>();
  for (const cmd of candidates) {
    const list = byGroup.get(cmd.group) ?? [];
    list.push(cmd);
    byGroup.set(cmd.group, list);
  }
  const rows: RowEntry[] = [];
  let flat = 0;
  for (const group of SLASH_GROUP_ORDER) {
    const cmds = byGroup.get(group);
    if (!cmds || cmds.length === 0) continue;
    rows.push({ type: "header", group, flatIdx: -1 });
    for (const cmd of cmds) {
      rows.push({ type: "cmd", cmd, flatIdx: flat });
      flat++;
    }
  }
  return rows;
}

const RootMenu: React.FC<{
  candidates: SlashCommand[];
  selectedIndex: number;
}> = ({ candidates, selectedIndex }) => {
  if (candidates.length === 0) {
    return (
      <Box paddingLeft={4}>
        <Text color={Theme.text.secondary}>{t("slash.menu.empty")}</Text>
      </Box>
    );
  }

  const rows = buildRootRows(candidates);
  // Window of cmd rows around the selected flat index, expanded to
  // include the header that owns the first visible cmd row so the
  // user always sees which group they're in.
  const cmdRows = rows.filter((r) => r.type === "cmd");
  const totalCmds = cmdRows.length;
  let startCmd = 0;
  let endCmd = totalCmds;
  if (totalCmds > VISIBLE_ROWS) {
    const half = Math.floor(VISIBLE_ROWS / 2);
    startCmd = Math.max(0, Math.min(selectedIndex - half, totalCmds - VISIBLE_ROWS));
    endCmd = startCmd + VISIBLE_ROWS;
  }
  // Map back to row indices so we keep group headers attached to
  // their first visible command.
  const visible: RowEntry[] = [];
  let lastHeaderGroup: SlashGroup | null = null;
  for (const row of rows) {
    if (row.type === "header") {
      lastHeaderGroup = row.group ?? null;
      continue;
    }
    if (row.flatIdx >= startCmd && row.flatIdx < endCmd) {
      // Insert the header for the active group exactly once, on the
      // first cmd row of that group within the window.
      if (lastHeaderGroup) {
        visible.push({
          type: "header",
          group: lastHeaderGroup,
          flatIdx: -1,
        });
        lastHeaderGroup = null;
      }
      visible.push(row);
    }
  }

  return (
    <Box flexDirection="column" paddingLeft={4}>
      {startCmd > 0 && (
        <Text color={Theme.text.secondary}>
          {t("slash.menu.more_above", { n: startCmd })}
        </Text>
      )}
      {visible.map((row, i) => {
        if (row.type === "header") {
          return (
            <Box key={`h-${row.group}-${i}`} marginTop={i === 0 ? 0 : 1}>
              <Text color={Theme.text.accent} bold>
                {row.group ? t(GROUP_LABEL_KEY[row.group]) : ""}
              </Text>
            </Box>
          );
        }
        const cmd = row.cmd!;
        const selected = row.flatIdx === selectedIndex;
        // ``usage`` (e.g. ``[calm|working|dense]``) is intentionally
        // NOT shown in the autocomplete dropdown — descriptions
        // already spell out the values when they matter, and the
        // bracketed form competed visually with the more useful
        // description column. The full usage IS still shown in the
        // ``/help`` output where readers expect a verbose listing.
        return (
          <Box key={cmd.name}>
            <Box minWidth={2}>
              <Text
                color={selected ? Theme.text.accent : Theme.text.secondary}
                bold={selected}
              >
                {selected ? Icons.arrow : " "}
              </Text>
            </Box>
            <Box minWidth={NAME_COL}>
              <Text
                color={selected ? Theme.text.accent : Theme.text.secondary}
                bold={selected}
              >
                /{cmd.name}
              </Text>
            </Box>
            <Box flexGrow={1} marginLeft={DESC_GUTTER}>
              <Text color={Theme.text.secondary} wrap="truncate-end">
                {cmd.description}
              </Text>
            </Box>
          </Box>
        );
      })}
      {endCmd < totalCmds && (
        <Text color={Theme.text.secondary}>
          {t("slash.menu.more_below", { n: totalCmds - endCmd })}
        </Text>
      )}
      <Box marginTop={1}>
        <Text color={Theme.text.secondary}>{t("slash.menu.hint")}</Text>
      </Box>
    </Box>
  );
};

// ── Sub mode ─────────────────────────────────────────────────────────

const SubMenu: React.FC<{
  parent: SlashCommand;
  subs: SlashSubcommand[];
  selectedIndex: number;
}> = ({ parent, subs, selectedIndex }) => {
  if (subs.length === 0) {
    return (
      <Box flexDirection="column" paddingLeft={4}>
        <Text color={Theme.text.secondary}>
          /{parent.name} {parent.usage ?? ""}
        </Text>
        <Text color={Theme.text.secondary}>{t("slash.menu.empty")}</Text>
      </Box>
    );
  }

  // Same windowing as root mode but flat (no inter-group headers in
  // sub mode — only one parent's subs are listed).
  const total = subs.length;
  let start = 0;
  let end = total;
  if (total > VISIBLE_ROWS) {
    const half = Math.floor(VISIBLE_ROWS / 2);
    start = Math.max(0, Math.min(selectedIndex - half, total - VISIBLE_ROWS));
    end = start + VISIBLE_ROWS;
  }

  return (
    <Box flexDirection="column" paddingLeft={4}>
      {/* Parent context line — tells the user which root they're
          currently sub-completing under. */}
      <Text color={Theme.text.accent} bold>
        /{parent.name}
        {parent.usage ? ` ${parent.usage}` : ""}
      </Text>
      {start > 0 && (
        <Text color={Theme.text.secondary}>
          {t("slash.menu.more_above", { n: start })}
        </Text>
      )}
      {subs.slice(start, end).map((sub, i) => {
        const idx = start + i;
        const selected = idx === selectedIndex;
        // Same ``usage``-less convention as the root menu — see the
        // comment in ``RootMenu``. Description carries the meaning.
        return (
          <Box key={sub.name}>
            <Box minWidth={2}>
              <Text
                color={selected ? Theme.text.accent : Theme.text.secondary}
                bold={selected}
              >
                {selected ? Icons.arrow : " "}
              </Text>
            </Box>
            <Box minWidth={NAME_COL}>
              <Text
                color={selected ? Theme.text.accent : Theme.text.secondary}
                bold={selected}
              >
                {sub.name}
              </Text>
            </Box>
            <Box flexGrow={1} marginLeft={DESC_GUTTER}>
              <Text color={Theme.text.secondary} wrap="truncate-end">
                {sub.description}
              </Text>
            </Box>
          </Box>
        );
      })}
      {end < total && (
        <Text color={Theme.text.secondary}>
          {t("slash.menu.more_below", { n: total - end })}
        </Text>
      )}
      <Box marginTop={1}>
        <Text color={Theme.text.secondary}>{t("slash.menu.hint")}</Text>
      </Box>
    </Box>
  );
};
