/**
 * Spinner type selection.
 *
 * Three options:
 *
 *   - ``dots`` — 10-frame Braille ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ at 80ms. Used by the main
 *     LoadingIndicator; this is the "I am thinking" rotation users
 *     associate with chat agents.
 *   - ``toggle`` — 2-frame ⊶⊷ at 250ms. Used by ToolStatusIndicator —
 *     it's intentionally low-amplitude so the eye doesn't get pulled to
 *     the running tool when several are firing simultaneously.
 *   - ``tmux`` — 3-frame ``.``/``..``/``...`` at 750ms. Triggered when
 *     ``$TMUX`` is set: tmux's redraw cost is high enough that 12.5Hz
 *     Braille spinners cause visible lag.
 *
 * Consumer side: ``ink-spinner`` accepts a ``type`` prop matching
 * ``cli-spinners``' name. We export the chosen type from ``selectSpinnerType``
 * so components can pass it through unchanged.
 */

import type { SpinnerName } from "cli-spinners";

export interface SpinnerProfile {
  /** Name accepted by ink-spinner / cli-spinners. */
  type: SpinnerName;
  /** Hint to the renderer for refresh interval (frames per second). */
  fps: number;
}

const isTmux = Boolean(process.env["TMUX"]);

/**
 * Spinner used in the main LoadingIndicator (thinking row).
 */
export const ThinkingSpinner: SpinnerProfile = isTmux
  ? { type: "simpleDotsScrolling", fps: 1.3 }
  : { type: "dots", fps: 12.5 };

/**
 * Spinner used in ToolStatusIndicator. Lower amplitude so multiple
 * concurrent tools don't compete for attention.
 */
export const ToolSpinner: SpinnerProfile = isTmux
  ? { type: "simpleDotsScrolling", fps: 1.3 }
  : { type: "toggle", fps: 4 };

/** Whether the current terminal looks like tmux. */
export const isTmuxTerminal = isTmux;
