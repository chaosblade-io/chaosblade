/**
 * Slash-command picker that floats below the InputPrompt while the
 * buffer starts with ``/``. Two modes, picked by InputPrompt's
 * ``computeSlashState`` helper:
 *
 *  - ``root`` mode (no whitespace yet, e.g. ``/he``): list all
 *    visible commands matching the prefix as a FLAT list. Ordering
 *    follows SLASH_GROUP_ORDER (general → business → skills →
 *    dynamic) via ``orderCandidatesByGroup`` in InputPrompt; the
 *    menu itself doesn't render any ``General`` / ``Business`` /
 *    ``Skills`` header rows. We used to — but they shared the same
 *    ``forge.fire`` + bold styling as the focused row, which made
 *    the user's selection visually indistinguishable from a header.
 *    The group ordering alone is enough hierarchy; the section
 *    labels were chrome.
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
import { useTerminalSize } from "../hooks/useTerminalSize.js";
import { t } from "../i18n/index.js";
import {
  type SlashCommand,
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
/** Default item cap on terminals tall enough to afford it. Beyond
 *  ~8 visible items the menu starts to dominate the viewport
 *  vertically; users can ``↓`` for more if the list runs longer. */
const VISIBLE_ROWS_MAX = 8;
/** Floor — always show at least 1 item so even a tight terminal can
 *  still type-and-pick by repeatedly ↓-ing through the list. Was 3
 *  but that left no room on terminals ≤ 12 rows. */
const VISIBLE_ROWS_MIN = 1;
/** Rows the slash menu burns OUTSIDE its item list, used to back out
 *  the per-terminal item cap so ``items + chrome ≤ viewport``.
 *  Breakdown:
 *    · InputPrompt fence-top + body + fence-bottom .. 3
 *    · Footer ............................................. 1
 *    · Composer marginTop ................................. 1
 *    · slash menu ↑ / ↓ "N more" markers (worst case 2) ... 2
 *    · slash menu hint row (inline, no margin) ............ 1
 *                                                          —
 *                                                          8
 *  Setting at 7 leaves the menu 1 row of headroom — dyn frame stays
 *  < viewport, so the trailing ``\n`` Ink emits per render doesn't
 *  push cursor past viewport bottom → no scrolling per re-render →
 *  no ghost copies of the menu in scrollback. (Was 9 when group
 *  headers were rendered — dropping them reclaimed 2 rows of
 *  budget.) */
const SLASH_CHROME_RESERVE = 7;
/** Hard cutoff: when ``terminalRows < this``, suppress the menu
 *  entirely (return null) — even MIN items + chrome can't fit, and
 *  showing a glitching menu is worse than no menu at all. User can
 *  still type the full command name and press Enter. */
const SLASH_MENU_MIN_TERMINAL_ROWS = SLASH_CHROME_RESERVE + 1; // 10

/** Compute the per-render item cap. Pure function so callers don't
 *  need to remember the min/max/clamp dance. Returns ``0`` when
 *  the terminal is too small to show even one item plus chrome —
 *  callers should suppress the menu entirely in that case. */
function visibleItemCap(terminalRows: number): number {
  if (terminalRows < SLASH_MENU_MIN_TERMINAL_ROWS) return 0;
  const budget = terminalRows - SLASH_CHROME_RESERVE;
  return Math.max(VISIBLE_ROWS_MIN, Math.min(VISIBLE_ROWS_MAX, budget));
}

export const SlashMenu: React.FC<Props> = (props) => {
  if (props.mode === "root") return <RootMenu {...props} />;
  return <SubMenu {...props} />;
};

// ── Root mode ────────────────────────────────────────────────────────

const RootMenu: React.FC<{
  candidates: SlashCommand[];
  selectedIndex: number;
}> = ({ candidates, selectedIndex }) => {
  // Per-render item cap, sized to keep the whole dyn frame (menu +
  // input + footer) inside viewport rows. Critical on small
  // terminals — without this clamp, each ↑/↓ keypress would
  // re-render an oversized menu, scrolling top rows into scrollback
  // and producing the "ghost copies of the menu" symptom users saw
  // in pre-fix screenshots. ``useTerminalSize`` subscribes to
  // SIGWINCH so resizing live re-tightens or relaxes the cap.
  const { rows: terminalRows } = useTerminalSize();
  const visibleRows = visibleItemCap(terminalRows);

  // Bail entirely on terminals so small that even MIN items + chrome
  // would overflow — showing a glitching menu is worse than no menu.
  // The user can still type the full command name and press Enter.
  if (visibleRows === 0) return null;

  if (candidates.length === 0) {
    return (
      <Box>
        <Text color={Theme.text.secondary}>{t("slash.menu.empty")}</Text>
      </Box>
    );
  }

  // Flat windowing — same shape as SubMenu. Ordering across groups
  // is already applied by ``orderCandidatesByGroup`` in InputPrompt,
  // so we just slice in display order.
  const total = candidates.length;
  let start = 0;
  let end = total;
  if (total > visibleRows) {
    const half = Math.floor(visibleRows / 2);
    start = Math.max(0, Math.min(selectedIndex - half, total - visibleRows));
    end = start + visibleRows;
  }

  return (
    <Box flexDirection="column">
      {start > 0 && (
        <Text color={Theme.text.secondary}>
          {t("slash.menu.more_above", { n: start })}
        </Text>
      )}
      {candidates.slice(start, end).map((cmd, i) => {
        const idx = start + i;
        const selected = idx === selectedIndex;
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
      {end < total && (
        <Text color={Theme.text.secondary}>
          {t("slash.menu.more_below", { n: total - end })}
        </Text>
      )}
      <Box>
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
  // Same per-render cap as RootMenu — see the comment there for the
  // small-terminal rationale.
  const { rows: terminalRows } = useTerminalSize();
  const visibleRows = visibleItemCap(terminalRows);

  // Same suppression as RootMenu — bail on terminals too small to
  // host the menu without ghosts.
  if (visibleRows === 0) return null;

  if (subs.length === 0) {
    return (
      <Box flexDirection="column">
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
  if (total > visibleRows) {
    const half = Math.floor(visibleRows / 2);
    start = Math.max(0, Math.min(selectedIndex - half, total - visibleRows));
    end = start + visibleRows;
  }

  return (
    <Box flexDirection="column">
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
      {/* Chrome-cut: dropped ``marginTop=1`` on hint, same as
       *  RootMenu — see comment there for rationale. */}
      <Box>
        <Text color={Theme.text.secondary}>{t("slash.menu.hint")}</Text>
      </Box>
    </Box>
  );
};
