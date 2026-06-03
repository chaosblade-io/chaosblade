/**
 * One-shot Ink render of the GoodbyeCard, called from cli.tsx after
 * the main interactive instance has unmounted.
 *
 * Why a second ``render()`` call instead of hand-painted ANSI:
 *   - Reuses ``BootCardFrame`` + ``Theme`` so the goodbye visually
 *     matches the rest of the boot panel (lavender border, same width
 *     policy, CJK-aware label padding via string-width).
 *   - No need to duplicate Yoga layout or maintain a parallel rendering
 *     path that would drift from BootCardFrame over time.
 *
 * Why it's safe to call ``render()`` after the main app exits:
 *   - Per Ink's docs (App Lifecycle section in the upstream readme),
 *     ``waitUntilExit()`` resolves once unmount finishes and Ink
 *     restores the native console. A fresh ``render()`` call after
 *     that point starts a new, independent Ink instance — no
 *     contention with the first one.
 *   - The component has no async work (no ``useInput`` / ``useEffect``
 *     timers), so Ink renders one frame and the only thing keeping the
 *     instance alive is the input listener it installs. We explicitly
 *     ``unmount()`` after a tick to release stdin and return control.
 */

import { render } from "ink";
import React from "react";
import { GoodbyeCard } from "../components/boot/GoodbyeCard.js";
import type { AppState } from "../state/types.js";

/**
 * Print the goodbye card to stdout. Safe to call from any exit path
 * (normal /exit, Esc on empty prompt, SIGINT/SIGTERM handler). Quietly
 * no-ops when:
 *   - ``state`` is ``null`` (first render never committed → nothing to
 *     summarize)
 *   - stdout isn't a TTY (piped output / CI / headless tests — printing
 *     a Box-drawn card into a logfile is just noise)
 */
export async function printGoodbye(state: AppState | null): Promise<void> {
  if (!state) return;
  if (!process.stdout.isTTY) return;

  let inst: ReturnType<typeof render> | null = null;
  try {
    inst = render(<GoodbyeCard state={state} />, {
      // ``patchConsole`` defaults to true; for this one-shot render we
      // don't want Ink monkey-patching console.* — main TUI already
      // unmounted, restoring native streams.
      patchConsole: false,
      // We're explicitly ``unmount()``-ing below; Ctrl+C handling for
      // this brief paint isn't needed and would leave a confusing
      // "press Ctrl+C to exit" state on the card.
      exitOnCtrlC: false,
    });
    // Yield one macrotask so Ink commits + flushes the frame to stdout.
    // ``setImmediate`` is sufficient: Ink's renderer uses microtask /
    // setImmediate scheduling internally, and the first frame is queued
    // synchronously on mount.
    await new Promise<void>((resolve) => setImmediate(resolve));
    inst.unmount();
    // Bound waitUntilExit at 500ms. Ink's stdin-listener cleanup can
    // stall when raw mode hasn't been fully released by the prior
    // (main) Ink instance — observed as exit hangs for several
    // seconds on /exit. The frame is already painted to stdout by the
    // time we reach this line, so we don't actually NEED full unmount
    // cleanup to complete; the OS will reclaim any leftover listeners
    // when the process exits a moment later. Localising the bound
    // here keeps the rationale where the quirk lives.
    await Promise.race([
      inst.waitUntilExit().catch(() => undefined),
      new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, 500);
        timer.unref?.();
      }),
    ]);
  } catch {
    // Last-resort guard. Goodbye rendering must never block process
    // exit — if Ink mis-behaves here (terminal restored to a weird
    // state, etc.), we silently bail and let cleanup proceed.
    if (inst) {
      try {
        inst.unmount();
      } catch {
        // ignore
      }
    }
  }
}
