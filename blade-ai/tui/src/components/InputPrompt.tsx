/**
 * Single-line editor with persistent history + slash menu.
 *
 * M4 scope:
 *   - Append printable chars
 *   - Backspace / Delete (both delete the char left of cursor; we
 *     don't yet track a separate cursor for inline editing)
 *   - Enter → submit
 *   - Tab → apply selected slash candidate
 *   - ↑/↓ → walk command history when no slash menu is showing,
 *     otherwise move the slash-menu cursor
 *   - Esc → dismiss slash menu / clear text / exit (cascading)
 *
 * State props (owned by Composer):
 *
 *   - ``disabled``    — keyboard fully yielded. ``useInput`` unsubscribes
 *                       and the row renders dim with no cursor block.
 *                       Used during ``waiting_confirmation`` so the
 *                       ConfirmMessage's Select widget can read Y/N
 *                       without racing this component.
 *
 *   - ``enterLocked`` — ``useInput`` stays subscribed but Enter /
 *                       Esc / Ctrl+C are no-ops here. Used during
 *                       ``responding``: the agent is still streaming so
 *                       we mustn't queue another submit, but the user
 *                       can still type so their next message is drafted
 *                       while the agent finishes. Esc / Ctrl+C are
 *                       deferred to Composer's cancel-turn handler.
 *
 *                       Visual cues distinguish this state from idle so
 *                       users don't press Enter expecting a submit:
 *                         · prompt glyph + cursor block dimmed to
 *                           ``Theme.text.secondary``
 *                         · placeholder (when buffer empty) replaced
 *                           with the streaming hint string
 *                       SlashMenu is also suppressed in this state —
 *                       its 5+ visible rows would otherwise push the
 *                       dynamic frame past ``stdout.rows`` on a
 *                       streaming turn and trip Ink's fullscreen-redraw
 *                       branch on every keystroke. The ``/`` text
 *                       remains in the buffer; the menu reappears on
 *                       the next idle render and Tab/Enter resume
 *                       acting on it normally.
 *
 * The two are mutually exclusive — Composer feeds one or the other
 * based on the streaming sub-state.
 */

import { Box, Text, useInput } from "ink";
import { useCallback, useEffectEvent, useMemo, useState } from "react";
import { useInputHistory } from "../hooks/useInputHistory.js";
import { t } from "../i18n/index.js";
import {
  SLASH_GROUP_ORDER,
  type SlashCommand,
  type SlashCommandRegistry,
  type SlashSubcommand,
} from "../state/commands.js";
import { Theme } from "../theme/colors.js";
import { Icons, isAsciiMode } from "../theme/icons.js";
import {
  cursorToLineCol,
  lineColToCursor,
  lineEndIdx,
  lineStartIdx,
} from "../utils/cursorMath.js";
import { useBootCardWidth } from "./boot/BootCardFrame.js";
import { SlashMenu } from "./SlashMenu.js";

interface Props {
  /** Fully passive: yield keyboard, render dim, no cursor block. */
  disabled: boolean;
  /** Active visual + typing accepted, but Enter / Esc / Ctrl+C
   *  are no-ops. Composer owns those keys during ``responding``. */
  enterLocked?: boolean;
  registry: SlashCommandRegistry;
  onSubmit: (text: string) => void;
  onExit: () => void;
  placeholder?: string;
}

/** Two-mode slash autocomplete state. ``root`` mode is the classic
 *  "user is still typing the command name" picker. ``sub`` mode kicks
 *  in once the user has fully typed a registered command followed by
 *  whitespace AND that command has subcommands — at that point the
 *  picker switches to the parent's sub list, mirroring Python's
 *  two-level menu. Anything past the first sub-arg disables the
 *  picker so the user can type free arguments without distraction. */
type SlashState =
  | { active: false }
  | {
      active: true;
      mode: "root";
      candidates: SlashCommand[];
      selected: number;
    }
  | {
      active: true;
      mode: "sub";
      parent: SlashCommand;
      subs: SlashSubcommand[];
      selected: number;
    };

/**
 * Sort candidates so the array matches what ``SlashMenu`` actually
 * renders: SLASH_GROUP_ORDER (general → business → skills → dynamic),
 * each group internally alphabetical.
 *
 * Why this matters: ``registry.filter("")`` returns a strictly
 * alphabetical list, but ``SlashMenu``'s ``buildRootRows`` re-orders
 * by group when assigning the visible flat indices. With the two
 * orderings mismatched, ``slash.selected = 2`` paints the arrow on
 * the third row of ``general`` while the Enter handler dispatches
 * the third item of the alphabetical list — so the user could see
 * ``→ /exit`` highlighted yet have ``/config`` execute on Enter
 * (because alphabetical[2] = doctor / [1] = config, but
 * group-flat[2] = exit / [1] = doctor). This helper aligns the
 * arrays so visible position N === dispatched candidates[N] for any
 * value of N.
 */
export function orderCandidatesByGroup(
  candidates: SlashCommand[],
): SlashCommand[] {
  const byGroup = new Map<string, SlashCommand[]>();
  for (const cmd of candidates) {
    const list = byGroup.get(cmd.group) ?? [];
    list.push(cmd);
    byGroup.set(cmd.group, list);
  }
  const out: SlashCommand[] = [];
  for (const group of SLASH_GROUP_ORDER) {
    const cmds = byGroup.get(group);
    if (!cmds || cmds.length === 0) continue;
    out.push(...cmds);
  }
  // Defensive — any unknown group (shouldn't happen given
  // SlashGroup is a closed union) lands at the end so we don't
  // silently drop the command.
  for (const cmd of candidates) {
    if (!SLASH_GROUP_ORDER.includes(cmd.group)) out.push(cmd);
  }
  return out;
}

function computeSlashState(
  registry: SlashCommandRegistry,
  buffer: string,
  prevSelected: number,
): SlashState {
  if (!buffer.startsWith("/")) return { active: false };
  const text = buffer.slice(1);
  if (text.length === 0) {
    // Bare ``/`` — show the full visible command list.
    const candidates = orderCandidatesByGroup(registry.filter(""));
    return {
      active: true,
      mode: "root",
      candidates,
      selected: clamp(prevSelected, candidates.length),
    };
  }

  const firstSpaceIdx = text.search(/\s/);
  if (firstSpaceIdx === -1) {
    // ``/he`` — still typing the root.
    const candidates = orderCandidatesByGroup(registry.filter(text));
    return {
      active: true,
      mode: "root",
      candidates,
      selected: clamp(prevSelected, candidates.length),
    };
  }

  // ``/<root>(\s)+<rest...>``. Drill into sub mode iff (a) the root
  // resolves AND (b) the root has subcommands AND (c) the rest is
  // a single token (no further whitespace). That third condition
  // disables the picker once the user starts typing sub args, which
  // mirrors how the Python TUI hides its candidate list during
  // free-form arg entry.
  const rootRaw = text.slice(0, firstSpaceIdx);
  const rest = text.slice(firstSpaceIdx).replace(/^\s+/, "");
  const cmd = registry.get(rootRaw);
  if (!cmd || !cmd.subcommands) return { active: false };
  if (/\s/.test(rest)) return { active: false };
  const subPrefix = rest.toLowerCase();
  const subs = Object.values(cmd.subcommands)
    .filter((s) => s.name.toLowerCase().startsWith(subPrefix))
    .sort((a, b) => a.name.localeCompare(b.name));
  return {
    active: true,
    mode: "sub",
    parent: cmd,
    subs,
    selected: clamp(prevSelected, subs.length),
  };
}

function clamp(idx: number, length: number): number {
  if (length === 0) return 0;
  return Math.max(0, Math.min(idx, length - 1));
}

export const InputPrompt: React.FC<Props> = ({
  disabled,
  enterLocked = false,
  registry,
  onSubmit,
  onExit,
  placeholder,
}) => {
  // The streaming placeholder is preferred over the caller's
  // ``placeholder`` prop only when (a) we're in enterLocked AND (b) the
  // buffer is empty — i.e. the slot would otherwise show the generic
  // "Type your message …" hint. With the buffer non-empty the user is
  // looking at their own draft; the placeholder isn't shown anyway.
  const idlePlaceholder = placeholder ?? t("input.placeholder");
  const lockedPlaceholder = t("input.placeholder_streaming");
  const resolvedPlaceholder = enterLocked ? lockedPlaceholder : idlePlaceholder;
  const [value, setValue] = useState("");
  // Cursor is a *codepoint* index into ``value`` — not a UTF-16 unit
  // index. Without this, an emoji (surrogate pair, length 2 in JS)
  // would split when the cursor lands between its halves and the
  // terminal would render replacement boxes.
  const [cursor, setCursor] = useState(0);
  const [selected, setSelected] = useState(0);
  const history = useInputHistory();

  // Codepoint view of the current buffer. Used everywhere we need
  // cursor-aware slicing. Recomputed on every value change but
  // cheap (O(value.length)) and the value is bounded.
  const codepoints = useMemo(() => Array.from(value), [value]);

  // Recompute slash state on every render; cheap and keeps ``selected``
  // in range whenever the buffer changes.
  //
  // Forced inactive while ``enterLocked``: showing the SlashMenu while
  // the agent is streaming would add 5+ visible rows to the dynamic
  // frame, pushing it past ``stdout.rows`` on most terminals and
  // tripping Ink's fullscreen-redraw branch on every keystroke. The
  // user can still type ``/help`` into the buffer — the menu (and the
  // associated arrow/Tab/Enter handlers) re-engages automatically the
  // moment ``enterLocked`` clears.
  const slash: SlashState = useMemo(
    () =>
      enterLocked
        ? { active: false }
        : computeSlashState(registry, value, selected),
    [enterLocked, registry, value, selected],
  );

  // Replace the buffer + reset cursor to the codepoint-end. Used by
  // history nav, slash Tab-complete, and Esc-clear so they all
  // converge on a consistent post-update cursor position.
  //
  // ``useCallback`` with empty deps because ``setValue`` / ``setCursor``
  // are React's own stable setters — this lets ``replaceValue`` be a
  // referentially-stable dependency anywhere callers want to memoize.
  const replaceValue = useCallback((next: string): void => {
    setValue(next);
    setCursor(Array.from(next).length);
  }, []);

  // The keyboard handler closes over a lot of state (value, cursor,
  // codepoints, slash, history, ...). Wrapping it in
  // ``useEffectEvent`` (React 19.2+ stable API) makes the function
  // returned to ``useInput`` referentially stable across renders —
  // Ink doesn't resubscribe its stdin listener on every keystroke
  // even though the closure picks up the latest values. Pure perf
  // optimization; logic is identical to the inline version. Was
  // previously a hand-rolled ``useEvent`` hook (ref + useLayoutEffect
  // + useCallback) — replaced with the native one when we upgraded
  // to React 19.2 / Ink v7.
  const handleKey = useEffectEvent(
    (input: string, key: Parameters<Parameters<typeof useInput>[0]>[1]) => {
      // ── Esc / Ctrl+C cascade ────────────────────────────────
      // When ``enterLocked``, Composer's parallel useInput owns
      // these keys (cancel the running turn). Returning early here
      // prevents the local cascade from also clearing the buffer
      // or — far worse — calling ``onExit`` on an empty buffer
      // (which would close the app mid-turn).
      if (key.escape || (key.ctrl && input === "c")) {
        if (enterLocked) return;
        if (slash.active) {
          replaceValue("");
          history.reset();
        } else if (value.length > 0) {
          replaceValue("");
          history.reset();
        } else {
          onExit();
        }
        return;
      }

      // ── slash menu navigation ───────────────────────────────
      // Both root and sub modes navigate via the same ↑/↓/Tab/Enter
      // shortcuts; the only difference is what the selected entry
      // resolves to. Keep the discriminated-union check tight so the
      // compiler narrows ``slash.candidates`` / ``slash.subs`` for
      // each branch.
      const slashCount =
        slash.active
          ? slash.mode === "root"
            ? slash.candidates.length
            : slash.subs.length
          : 0;
      if (slash.active && slashCount > 0) {
        if (key.upArrow) {
          setSelected((i) => Math.max(0, i - 1));
          return;
        }
        if (key.downArrow) {
          setSelected((i) => Math.min(slashCount - 1, i + 1));
          return;
        }
        if (key.tab) {
          if (slash.mode === "root") {
            const cmd = slash.candidates[slash.selected];
            if (cmd) {
              // Append a trailing space when the picked command has
              // subcommands so the buffer flows directly into sub
              // mode. Without subs we still add a space so the user
              // can immediately type free args; if they don't want
              // it Backspace removes it.
              replaceValue(`/${cmd.name} `);
            }
          } else {
            const sub = slash.subs[slash.selected];
            if (sub) replaceValue(`/${slash.parent.name} ${sub.name} `);
          }
          return;
        }
        if (key.return) {
          if (enterLocked) return;
          let line: string | null = null;
          if (slash.mode === "root") {
            const cmd = slash.candidates[slash.selected];
            if (cmd) line = `/${cmd.name}`;
          } else {
            const sub = slash.subs[slash.selected];
            if (sub) line = `/${slash.parent.name} ${sub.name}`;
          }
          if (line !== null) {
            replaceValue("");
            setSelected(0);
            history.push(line);
            onSubmit(line);
          }
          return;
        }
      }

      // ── multi-line cursor: ↑/↓ within a multi-line buffer ──
      // When the buffer contains a newline, ↑/↓ navigate lines while
      // preserving the original column. This takes priority over
      // history nav so editing a multi-line draft doesn't accidentally
      // wipe it on ↑.
      //
      // Boundary fall-through: if the cursor is already on the first
      // line and ↑ is pressed (or last line + ↓), let history nav
      // handle it instead of trapping the user. Without this a user
      // who recalled a multi-line history entry has no way to walk
      // back to the previous one short of Esc-clear.
      const cpLen = codepoints.length;
      const isMultiLine = value.includes("\n");
      const slashTakesArrows = slash.active && slashCount > 0;
      if (isMultiLine && !slashTakesArrows) {
        if (key.upArrow) {
          const { line, col } = cursorToLineCol(codepoints, cursor);
          if (line > 0) {
            setCursor(lineColToCursor(codepoints, line - 1, col));
            return;
          }
          // First line + ↑ → fall through to history.prev below.
        } else if (key.downArrow) {
          const { line, col } = cursorToLineCol(codepoints, cursor);
          // Total lines = newline count + 1. Last line index = totalLines - 1.
          let newlineCount = 0;
          for (const cp of codepoints) if (cp === "\n") newlineCount += 1;
          if (line < newlineCount) {
            setCursor(lineColToCursor(codepoints, line + 1, col));
            return;
          }
          // Last line + ↓ → fall through to history.next.
        }
      }

      // ── command history ─────────────────────────────────────
      // Active when no slash menu is taking arrows (single-line
      // buffer never has line-nav, multi-line buffer falls through
      // here only at top/bottom boundaries — see above).
      if (!slashTakesArrows) {
        if (key.upArrow) {
          const prev = history.prev(value);
          if (prev !== null) replaceValue(prev);
          return;
        }
        if (key.downArrow) {
          const nxt = history.next(value);
          if (nxt !== null) replaceValue(nxt);
          return;
        }
      }

      // ── inline cursor movement ──────────────────────────────
      // Ctrl+A = Home, Ctrl+E = End. In a multi-line buffer they
      // operate within the current line; in a single-line buffer they
      // jump to buffer ends (lineStart/EndIdx degenerate to 0/length).
      // ←/→ step one codepoint and naturally cross newlines.
      if (key.ctrl && input === "a") {
        setCursor(lineStartIdx(codepoints, cursor));
        return;
      }
      if (key.ctrl && input === "e") {
        setCursor(lineEndIdx(codepoints, cursor));
        return;
      }
      if (key.leftArrow) {
        setCursor((c) => Math.max(0, c - 1));
        return;
      }
      if (key.rightArrow) {
        setCursor((c) => Math.min(cpLen, c + 1));
        return;
      }

      // ── submit ──────────────────────────────────────────────
      // Drop Enter silently while ``enterLocked`` so the user can keep
      // drafting a follow-up message during the live turn without it
      // queueing a second submit. The press is acknowledged to Ink
      // (we ``return``), so it doesn't fall through to the
      // printable-text branch below and slip a literal newline into
      // the buffer.
      if (key.return) {
        if (enterLocked) return;
        const text = value;
        if (text.length > 0) history.push(text);
        replaceValue("");
        setSelected(0);
        onSubmit(text);
        return;
      }

      // ── editing at cursor ───────────────────────────────────
      // Backspace deletes the codepoint left of the cursor; Delete
      // deletes the codepoint to the right. Both clamp at boundaries.
      // Joining ``codepoints.slice(...)`` is what makes emoji-safe.
      //
      // Ink v7 fixed the long-standing Mac key reporting bug
      // (#634) so Backspace now correctly sets ``key.backspace`` on
      // every platform — the prior darwin ``key.delete``-as-
      // backspace alias has been removed because it would now
      // *swallow* the legitimate Fn+Delete keystroke on Mac.
      // Forward-delete on PC keyboards continues to set
      // ``key.delete`` as before.
      if (key.backspace) {
        if (cursor === 0) return;
        const next =
          codepoints.slice(0, cursor - 1).join("") +
          codepoints.slice(cursor).join("");
        setValue(next);
        setCursor(cursor - 1);
        return;
      }
      if (key.delete) {
        if (cursor >= cpLen) return;
        const next =
          codepoints.slice(0, cursor).join("") +
          codepoints.slice(cursor + 1).join("");
        setValue(next);
        return;
      }
      // Plain text entry (printable). Insert at cursor; advance by
      // the codepoint length of the inserted text.
      //
      // Multi-line paste: when the user pastes content containing
      // ``\n``, Ink delivers the whole chunk in a single useInput call
      // with ``input.includes("\n")`` and ``key.return=false`` (the
      // bare Enter key is the *only* path that sets key.return). We
      // accept newlines verbatim into the buffer; Enter still submits.
      if (input && !key.meta && !key.ctrl) {
        // Defensive filter for raw C0 control bytes that Ink failed
        // to flag as ctrl-combos. Specifically Ctrl+O on macOS
        // Terminal can arrive as ``""`` (SO, code 15) without
        // ``key.ctrl=true``. Without this strip, those bytes would
        // land in the buffer as garbled characters. ``\t`` (HT) is
        // intentionally allowed — pasted tab characters are real
        // text. ``\n`` is also allowed (multi-line paste path).
        const filtered = input.replace(/[ --]/g, "");
        if (filtered.length === 0) return;
        const insertCps = Array.from(filtered);
        const next =
          codepoints.slice(0, cursor).join("") +
          filtered +
          codepoints.slice(cursor).join("");
        setValue(next);
        setCursor(cursor + insertCps.length);
      }
    },
  );

  useInput(handleKey, { isActive: !disabled });

  // Horizontal divider visually fencing the input area. Width matches
  // ``useBootCardWidth`` so the dividers + boot cards above share the
  // same right edge. ASCII fallback uses ``-`` for terminals that
  // can't render Box-drawing chars.
  //
  // MUST be called before the disabled-render branch below — React
  // requires hooks to run in the same order every render, and an
  // early return between two hooks would break that invariant
  // ("Rendered fewer hooks than expected").
  const fenceWidth = useBootCardWidth();
  const fenceChar = isAsciiMode ? "-" : "─";
  const fenceLine = fenceChar.repeat(fenceWidth);

  // Disabled-state render. Same outer shape as the active prompt
  // (fence ─ prompt ─ fence) so the bottom region of the TUI doesn't
  // jump on every busy/idle transition; everything dimmed, no cursor
  // block, no SlashMenu. The user can still SEE the input box —
  // they just can't type into it until the agent yields.
  if (disabled) {
    return (
      <Box flexDirection="column">
        <Box paddingLeft={2}>
          <Text color={Theme.text.secondary}>{fenceLine}</Text>
        </Box>
        <Box paddingLeft={2}>
          <Text color={Theme.text.secondary}>{`${Icons.prompt} `}</Text>
          <Text color={Theme.text.secondary}>{resolvedPlaceholder}</Text>
        </Box>
        <Box paddingLeft={2}>
          <Text color={Theme.text.secondary}>{fenceLine}</Text>
        </Box>
      </Box>
    );
  }

  // Render: split the buffer into pre-cursor / under-cursor / post-cursor
  // using codepoint slicing so emoji-aware Backspace/Delete renders
  // a single character at the cursor instead of half a surrogate pair.
  //
  // Multi-line buffers (paste containing \n) are rendered with each
  // ``\n`` replaced by a faint ↵ glyph followed by a real newline, so
  // the user can visually distinguish line breaks from a wrapped long
  // line. The under-cursor cell collapses to ▌ when it lands ON a
  // newline (the position is "the gap between lines").
  const safeCursor = Math.max(0, Math.min(cursor, codepoints.length));
  const beforeRaw = codepoints.slice(0, safeCursor).join("");
  const underCp = codepoints[safeCursor] ?? "";
  const afterRaw = codepoints.slice(safeCursor + 1).join("");

  const renderWithNewlineMarkers = (text: string): React.ReactNode => {
    if (!text.includes("\n")) return text;
    const parts = text.split("\n");
    return parts.flatMap((seg, i) =>
      i < parts.length - 1
        ? [seg, <Text key={`n${i}`} color={Theme.text.secondary} dimColor>{"↵\n"}</Text>]
        : [seg],
    );
  };

  // Visual-cue colour split for the active vs locked states. The
  // prompt glyph and the cursor block are the two "active" markers
  // that read as accent in idle mode; dropping them to secondary
  // during ``enterLocked`` is the visible signal that Enter won't fire
  // (paired with the streaming-aware placeholder above when the
  // buffer is empty). Body text colour stays primary either way —
  // we don't want the user's own typed draft to read as dim/grey.
  const promptColor = enterLocked ? Theme.text.secondary : Theme.text.accent;
  // Inverse-block under-cursor: keep the highlight visible (so users
  // can still tell where the cursor sits while drafting), but in the
  // dim band — same signal family as the prompt glyph.
  const underInverseColor = enterLocked
    ? Theme.text.secondary
    : Theme.text.primary;

  return (
    <Box flexDirection="column">
      <Box paddingLeft={2}>
        <Text color={Theme.text.secondary}>
          {fenceLine}
        </Text>
      </Box>
      <Box paddingLeft={2}>
        <Text color={promptColor}>{`${Icons.prompt} `}</Text>
        {value.length === 0 ? (
          <>
            <Text color={promptColor}>▌</Text>
            <Text color={Theme.text.secondary}> {resolvedPlaceholder}</Text>
          </>
        ) : (
          <>
            <Text color={Theme.text.primary}>
              {renderWithNewlineMarkers(beforeRaw)}
            </Text>
            {underCp === "\n" ? (
              <Text color={promptColor}>{"↵\n"}</Text>
            ) : underCp ? (
              <Text color={underInverseColor} inverse>
                {underCp}
              </Text>
            ) : (
              <Text color={promptColor}>▌</Text>
            )}
            <Text color={Theme.text.primary}>
              {renderWithNewlineMarkers(afterRaw)}
            </Text>
          </>
        )}
      </Box>
      <Box paddingLeft={2}>
        <Text color={Theme.text.secondary}>
          {fenceLine}
        </Text>
      </Box>
      {slash.active && slash.mode === "root" && (
        <SlashMenu
          mode="root"
          candidates={slash.candidates}
          selectedIndex={slash.selected}
        />
      )}
      {slash.active && slash.mode === "sub" && (
        <SlashMenu
          mode="sub"
          parent={slash.parent}
          subs={slash.subs}
          selectedIndex={slash.selected}
        />
      )}
    </Box>
  );
};
