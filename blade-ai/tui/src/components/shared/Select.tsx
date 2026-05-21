/**
 * Inline radio-select with optional free-form feedback mode.
 *
 * Pattern lifted from Claude Code's PermissionPrompt — each item is
 * either a "commit" item (Enter → ``onSelect(value)``) or a
 * "feedback" item (Enter → switch to inline text input, then Enter
 * again → ``onSelect(value, feedbackText)``). This is what lets the
 * user pick between a fixed answer ("approve" / "reject") and a
 * free-form reply that becomes the agent's next user message.
 *
 * Visual:
 *
 *   [A] Yes, proceed                        ← focused: brand-orange + bold
 *   [B] No, cancel
 *   [C] Tell agent something else…          ← hasFeedback option
 *
 *   A-Z jump · ↑↓ select · Enter confirm · Esc cancel
 *
 * All `[X]` chips share column 0 — focus is signaled by colour + bold
 * on the focused row, never by indent. Letter chips double as direct
 * keyboard shortcuts (press `A` to jump to the first option). Number
 * keys 1-9 stay live for users with prior muscle memory.
 *
 * In feedback mode (after Enter on a hasFeedback item):
 *
 *   [A] Yes, proceed
 *   [B] No, cancel
 *   [C] Tell agent something else…
 *   ❯ <typed text>|                         ← cursor block
 *
 *   Enter send · Esc back to options
 *
 * Keyboard handling lives entirely inside this component; callers
 * just pass ``isFocused`` to gate participation. The hosting
 * component (ConfirmMessage) is responsible for hiding Select once
 * the answer has been resolved.
 */

import { Box, Text, useInput } from "ink";
import { useState } from "react";
import { t } from "../../i18n/index.js";
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";

export interface SelectItem<T> {
  /** The value passed back to ``onSelect``. */
  value: T;
  /** Display label rendered next to the row's `[A]` / `[B]` chip. */
  label: string;
  /**
   * When true, pressing Enter on this item switches the Select into
   * feedback mode — a single-line text input appears below, the user
   * types freely, and Enter then commits with ``onSelect(value, text)``.
   * Other items commit immediately.
   */
  hasFeedback?: boolean;
}

export interface SelectProps<T> {
  items: SelectItem<T>[];
  /** When false, useInput is detached so the host can yield the
   *  keyboard to a different component (e.g. once the answer has
   *  been submitted). */
  isFocused: boolean;
  initialIndex?: number;
  /**
   * Fired when the user commits a choice. ``feedback`` is set only
   * when the chosen item had ``hasFeedback`` AND the user typed in
   * feedback mode (empty string is still passed through so the
   * caller can decide whether to fire a follow-up turn).
   */
  onSelect: (value: T, feedback?: string) => void;
  /**
   * Fired on Esc / Ctrl+C while in options mode. Esc inside
   * feedback mode just bounces back to options without firing
   * onCancel (the caller's "cancel" semantic is reserved for full
   * dialog dismissal — feedback Esc is a local correction).
   */
  onCancel: () => void;
}

const MAX_FEEDBACK_LEN = 2000;

export function Select<T>({
  items,
  isFocused,
  initialIndex = 0,
  onSelect,
  onCancel,
}: SelectProps<T>): React.JSX.Element {
  const safeInitial = Math.min(
    Math.max(0, initialIndex),
    Math.max(0, items.length - 1),
  );
  const [activeIndex, setActiveIndex] = useState<number>(safeInitial);
  const [mode, setMode] = useState<"options" | "feedback">("options");
  const [feedbackText, setFeedbackText] = useState<string>("");

  // Defensive clamp — ``items.length`` is a prop and a parent may
  // hand us a shorter list without remounting. Without this, after a
  // shrink (e.g. 5 → 3 items, activeIndex still 4), the first 2 ↑
  // presses would walk 4→3→2 with zero visible movement before the
  // highlight finally appears to move. Same stale-index bug class as
  // InputPrompt's slash menu — use the clamped value as the base for
  // both rendering and nav arithmetic. ``setActiveIndex`` still
  // writes through to the underlying state.
  const lastIdx = Math.max(0, items.length - 1);
  const safeActiveIdx = Math.min(activeIndex, lastIdx);

  useInput(
    (input, key) => {
      // ──────────────────────────────────────────────────────────
      // Feedback (free-form text) mode
      // ──────────────────────────────────────────────────────────
      if (mode === "feedback") {
        // Esc inside feedback mode is "cancel correction" — go back
        // to options without firing onCancel. Lets the user change
        // their mind about the option after starting to type.
        if (key.escape) {
          setMode("options");
          return;
        }
        if (key.return) {
          // Empty submission is a no-op. The user explicitly switched
          // into feedback mode (i.e. picked the "tell agent something
          // else" option), so committing nothing back to the host
          // would be indistinguishable from a plain reject — confusing
          // UX. Make Enter silently wait until they type at least one
          // non-whitespace character. Esc still bounces back to options
          // if they change their mind.
          if (feedbackText.trim().length === 0) return;
          const item = items[safeActiveIdx];
          if (item) {
            onSelect(item.value, feedbackText);
          }
          return;
        }
        if (key.backspace || key.delete) {
          setFeedbackText((s) => s.slice(0, -1));
          return;
        }
        // Ignore navigation keys + meta/ctrl combos so the user
        // can't, e.g., Up-arrow themselves out of feedback mode
        // by typing it as text.
        if (key.upArrow || key.downArrow || key.leftArrow || key.rightArrow) {
          return;
        }
        if (key.ctrl || key.meta || key.tab) {
          return;
        }
        // Append printable input. Multi-char ``input`` happens for
        // pasted text — we keep it as-is (truncated at MAX_FEEDBACK_LEN
        // so a stray gigabyte paste doesn't wedge the render).
        if (input && input.length > 0) {
          setFeedbackText((s) =>
            (s + input).slice(0, MAX_FEEDBACK_LEN),
          );
        }
        return;
      }

      // ──────────────────────────────────────────────────────────
      // Options mode
      // ──────────────────────────────────────────────────────────
      if (key.escape || (key.ctrl && input === "c")) {
        onCancel();
        return;
      }
      if (key.upArrow) {
        setActiveIndex(safeActiveIdx > 0 ? safeActiveIdx - 1 : safeActiveIdx);
        return;
      }
      if (key.downArrow) {
        setActiveIndex(
          safeActiveIdx < lastIdx ? safeActiveIdx + 1 : safeActiveIdx,
        );
        return;
      }
      // Letter-key direct jump (A-Z, case-insensitive). Letters are
      // the primary affordance now that each row is prefixed with a
      // visible `[A]` / `[B]` chip — pressing A jumps to the first
      // option, B to the second, etc. Out-of-range letters are
      // ignored; no auto-commit (user still presses Enter so a stray
      // keystroke can't fire an action).
      if (input && /^[a-zA-Z]$/.test(input)) {
        const idx = input.toUpperCase().charCodeAt(0) - 65;
        if (idx >= 0 && idx < items.length) {
          setActiveIndex(idx);
        }
        return;
      }
      // Number-key direct jump (1-9) kept for muscle memory from the
      // previous Select revision and from peers like Claude Code /
      // Qwen Code. Same out-of-range and no-auto-commit behaviour.
      if (input && /^[1-9]$/.test(input)) {
        const idx = parseInt(input, 10) - 1;
        if (idx >= 0 && idx < items.length) {
          setActiveIndex(idx);
        }
        return;
      }
      if (key.return) {
        const item = items[safeActiveIdx];
        if (!item) return;
        if (item.hasFeedback) {
          setMode("feedback");
        } else {
          onSelect(item.value);
        }
        return;
      }
    },
    { isActive: isFocused },
  );

  return (
    <Box flexDirection="column">
      {items.map((item, i) => {
        const active = i === safeActiveIdx;
        const inOptions = mode === "options";
        // Three visual states per row:
        //   - highlighted  (active + options mode)   → chip+label brand-orange + bold
        //   - pending      (active + feedback mode)  → chip dim, label primary (reminds
        //                                              the user which option they're
        //                                              about to commit while they type)
        //   - idle         (everything else)         → chip+label secondary dim
        const highlighted = active && inOptions;
        const pending = active && !inOptions;
        // Letter chip for direct keyboard jump. Cap at 26; beyond
        // that we render a blank 3-wide slot so column alignment is
        // preserved even though the row has no shortcut. (Select is
        // typically used with ≤5 items; >26 would be a misuse.)
        const chip =
          i < 26 ? `[${String.fromCharCode(65 + i)}]` : "   ";
        const chipColor = highlighted
          ? Theme.forge.fire
          : Theme.text.secondary;
        const labelColor = highlighted
          ? Theme.forge.fire
          : pending
            ? Theme.text.primary
            : Theme.text.secondary;
        return (
          <Box key={`opt-${i}`}>
            <Box minWidth={4}>
              <Text color={chipColor} bold={highlighted}>
                {`${chip} `}
              </Text>
            </Box>
            <Box flexGrow={1}>
              <Text color={labelColor} bold={highlighted}>
                {item.label}
              </Text>
            </Box>
          </Box>
        );
      })}
      {mode === "feedback" ? (
        <>
          <Box marginTop={1}>
            <Box minWidth={4}>
              <Text color={Theme.text.accent} bold>
                {Icons.prompt}{" "}
              </Text>
            </Box>
            <Box flexGrow={1}>
              {feedbackText.length === 0 ? (
                // Empty buffer: cursor block FIRST, placeholder as a
                // dim hint to its right. The pre-fix layout rendered
                // ``placeholder ▌`` which read as "the user typed the
                // placeholder text and the cursor sits at end-of-line"
                // — exactly the confusion reported. Mirrors the
                // InputPrompt empty-buffer rendering for consistency.
                <>
                  <Text color={Theme.text.accent}>{"▌"}</Text>
                  <Text color={Theme.text.secondary}>
                    {` ${t("select.feedback.placeholder")}`}
                  </Text>
                </>
              ) : (
                // Non-empty: typed text in primary, cursor block at
                // the end (we don't track an in-string caret yet —
                // the feedback box is single-line append-only).
                <>
                  <Text color={Theme.text.primary}>{feedbackText}</Text>
                  <Text color={Theme.text.accent}>{"▌"}</Text>
                </>
              )}
            </Box>
          </Box>
          <Box marginTop={1}>
            <Box minWidth={4} />
            <Box flexGrow={1}>
              <Text color={Theme.text.secondary}>
                {t("select.feedback.hint")}
              </Text>
            </Box>
          </Box>
        </>
      ) : (
        <Box marginTop={1}>
          <Box minWidth={4} />
          <Box flexGrow={1}>
            <Text color={Theme.text.secondary}>
              {t("select.options.hint")}
            </Text>
          </Box>
        </Box>
      )}
    </Box>
  );
}
