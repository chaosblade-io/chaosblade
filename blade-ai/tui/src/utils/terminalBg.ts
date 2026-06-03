/**
 * Terminal background-colour detection — picks light/dark so
 * components that need adaptive contrast (currently just
 * UserMessage's bubble) can render correctly on either canvas.
 *
 * Detection runs ONCE at boot in cli.tsx, before Ink takes over
 * stdin. Result lives in ``TerminalBgContext`` (theme/) for the rest
 * of the session. Re-running mid-session (e.g. for a future
 * ``/theme refresh`` command) is supported but not currently wired.
 *
 * Resolution order (first hit wins):
 *
 *   1. ``BLADE_AI_TERMINAL_BG=light|dark`` — explicit user override.
 *      For tmux / screen / mosh users whose terminals filter OSC
 *      passthrough, and for CI / scripted runs that want a fixed
 *      answer. Case-insensitive (``LIGHT`` / ``Light`` / ``light``
 *      all work); anything else (typo / unsupported value) is
 *      ignored and we fall through to the next source.
 *
 *      Recommended for: tmux without ``set -g allow-passthrough on``;
 *      GNU screen; mosh; any container / CI where OSC 11 has nowhere
 *      to round-trip. Without the override these environments hit the
 *      100ms timeout on every launch and silently fall through to
 *      'dark'.
 *
 *   2. ``COLORFGBG`` env var — some terminals (rxvt-family) export
 *      ``"fg;bg"`` or ``"fg;default;bg"`` at start. ANSI palette
 *      indices: 0-6 = dark / 7 = white / 8 = bright black / 9-15 =
 *      bright. We treat bg ≥ 9 as 'light', everything else as 'dark'.
 *      Includes 7 (white) as 'light' even though it's technically not
 *      bright — most rxvt-likes use 7 for white backgrounds.
 *
 *   3. OSC 11 query — ``ESC ] 1 1 ; ? ESC \``, terminal responds with
 *      ``ESC ] 1 1 ; rgb:RRRR/GGGG/BBBB BEL|ST``. Most modern
 *      terminals (iTerm2, Terminal.app, WezTerm, Alacritty, Kitty,
 *      VS Code, Windows Terminal) support this. Channels can be
 *      4-digit (16-bit, the spec) or 2-digit (8-bit, some terminals
 *      shorten); we normalise to 8-bit.
 *
 *      Luminance via Rec.601 weights — Y > 128 → 'light'. 128 is the
 *      neutral-grey midpoint; terminals usually pick bgs well above
 *      or below it so the boundary case is rare.
 *
 *   4. Fallback — 'dark'. Historical default for the entire CLI
 *      ecosystem; the existing palette is tuned for it, so users on
 *      undetectable terminals see the same look they used to.
 *
 * The OSC 11 path puts stdin into raw mode briefly. Anything that
 * arrives on stdin during the probe window that ISN'T an OSC 11
 * response is buffered and ``unshift()``-ed back so the next stdin
 * consumer (Ink) sees it. This handles the unlikely race where the
 * user types in the first 100ms after launch.
 */

import process from "node:process";

export type TerminalBgKind = "light" | "dark";

export interface TerminalBgInfo {
  /** Resolved kind — always one of the two valid values, never
   *  "unknown". An undetected terminal falls back to 'dark'. */
  kind: TerminalBgKind;
  /** How we got here. Surfaced in /doctor so users can debug
   *  detection issues (e.g. "tmux ate my OSC 11" → source='fallback'). */
  source: "env_override" | "colorfgbg" | "osc11" | "fallback";
  /** Parsed RGB from OSC 11 response. Undefined for non-osc11 sources. */
  rgb?: { r: number; g: number; b: number };
  /** How long detection took in ms — interesting for telemetry +
   *  /doctor display. ``timeoutMs`` is the upper bound. */
  detectMs: number;
}

/** OSC 11 query — ESC ] 1 1 ; ? ESC \  (ST terminator).
 *  Some terminals respond with BEL terminator (\\x07) instead; the
 *  response parser accepts both. */
const OSC11_QUERY = "\x1b]11;?\x1b\\";

/** Response regex. Two grouped channels each, terminator either
 *  BEL or ST. Case-insensitive because some terminals lowercase. */
const OSC11_RESPONSE_RE =
  /\x1b\]11;rgb:([0-9a-f]+)\/([0-9a-f]+)\/([0-9a-f]+)(\x07|\x1b\\)/i;

/** Default probe budget. 100ms covers the round-trip on every
 *  terminal I tested (locally < 5ms, over SSH < 50ms). Bumping
 *  further trades startup latency for fewer ``fallback`` outcomes
 *  on slow / non-responsive terminals — diminishing returns. */
const DEFAULT_TIMEOUT_MS = 100;

/**
 * Run the full detection pipeline. Always resolves (never rejects),
 * always returns a definite 'light' or 'dark' kind.
 */
export async function detectTerminalBg(
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<TerminalBgInfo> {
  const t0 = Date.now();

  // 1. Explicit env override — short-circuit everything.
  // Lower-cased so ``LIGHT`` / ``Light`` / ``LiGhT`` all work — users
  // typically don't think about case-sensitivity for these env vars
  // (the value is a label, not an identifier).
  const overrideRaw = process.env["BLADE_AI_TERMINAL_BG"];
  const override = overrideRaw?.toLowerCase().trim();
  if (override === "light" || override === "dark") {
    return {
      kind: override,
      source: "env_override",
      detectMs: Date.now() - t0,
    };
  }

  // 2. COLORFGBG — some terminals (urxvt, mintty, Konsole) export it.
  const cfbgKind = parseColorFgBg(process.env["COLORFGBG"]);
  if (cfbgKind) {
    return {
      kind: cfbgKind,
      source: "colorfgbg",
      detectMs: Date.now() - t0,
    };
  }

  // 3. OSC 11 query — only possible on a TTY where we can do raw IO.
  if (process.stdin.isTTY && process.stdout.isTTY) {
    const oscResult = await queryOsc11(timeoutMs);
    if (oscResult) {
      return {
        ...oscResult,
        detectMs: Date.now() - t0,
      };
    }
  }

  // 4. Fallback.
  return {
    kind: "dark",
    source: "fallback",
    detectMs: Date.now() - t0,
  };
}

/**
 * Parse the ``COLORFGBG`` env var. Format examples:
 *
 *   ``"15;0"``        — white fg, black bg → dark
 *   ``"0;15"``        — black fg, white bg → light
 *   ``"default;default;0"`` — Konsole 3-part with explicit default
 *
 * Returns the inferred terminal-bg kind, or ``null`` if the value
 * is missing / malformed / has no bg field.
 *
 * ANSI palette index → kind:
 *   0-6  → dark   (black + dark colours + dark grey via 8)
 *   7    → light  (white, included even though not "bright" — most
 *                  rxvt-likes use 7 for the actual white bg)
 *   8    → dark   (bright black = dark grey, treat as dark)
 *   9-15 → light  (bright colours, typically light backgrounds)
 */
export function parseColorFgBg(value: string | undefined): TerminalBgKind | null {
  if (!value) return null;
  const parts = value.split(";");
  if (parts.length < 2) return null;
  // bg is always the last field — copes with both 2- and 3-part forms.
  const last = parts[parts.length - 1];
  if (!last || last === "default") return null;
  const bgIdx = Number.parseInt(last, 10);
  if (!Number.isFinite(bgIdx) || bgIdx < 0 || bgIdx > 15) return null;
  if (bgIdx === 7 || bgIdx >= 9) return "light";
  return "dark";
}

/**
 * Send OSC 11 query and wait for the response within ``timeoutMs``.
 * Returns ``null`` if no parseable response arrived (terminal doesn't
 * support OSC 11, or filters it — common under tmux/screen/mosh).
 *
 * Stdin handling: we briefly switch to raw mode so the OSC response
 * bytes don't get cooked. Any non-OSC bytes received in the window
 * (rare: user typed faster than the response arrived) are ``unshift``-
 * ed back so the next stdin consumer (Ink) doesn't lose keystrokes.
 */
async function queryOsc11(
  timeoutMs: number,
): Promise<{ kind: TerminalBgKind; source: "osc11"; rgb: { r: number; g: number; b: number } } | null> {
  return new Promise((resolve) => {
    const stdin = process.stdin;
    const wasRaw = stdin.isRaw === true;
    const wasPaused = stdin.isPaused();

    let buffer = Buffer.alloc(0);
    let settled = false;

    const cleanup = (): void => {
      stdin.removeListener("data", onData);
      try {
        if (!wasRaw && stdin.setRawMode) stdin.setRawMode(false);
        if (wasPaused) stdin.pause();
      } catch {
        // Best effort — never let cleanup throw past the resolve.
      }
    };

    const finish = (
      result:
        | { kind: TerminalBgKind; source: "osc11"; rgb: { r: number; g: number; b: number } }
        | null,
      leftover?: Buffer,
    ): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      cleanup();
      // Hand any non-OSC bytes back to stdin so Ink's input loop sees
      // them. ``unshift`` is part of the Readable contract and prepends
      // to the internal buffer. Skipped when empty to avoid resuming a
      // paused stream needlessly.
      if (leftover && leftover.length > 0) {
        try {
          stdin.unshift(leftover);
        } catch {
          // If stdin is in flowing mode the unshift can throw; in
          // that case the bytes are lost. Acceptable — this is a
          // 100ms boot-time race that almost never trips.
        }
      }
      resolve(result);
    };

    const onData = (chunk: Buffer): void => {
      buffer = Buffer.concat([buffer, chunk]);
      // Use latin1 so each byte maps to one char — keeps regex
      // positions in sync with byte positions for the unshift split.
      const str = buffer.toString("latin1");
      const match = str.match(OSC11_RESPONSE_RE);
      if (match) {
        const r = parseChannel(match[1]!);
        const g = parseChannel(match[2]!);
        const b = parseChannel(match[3]!);
        const luminance = 0.299 * r + 0.587 * g + 0.114 * b;
        const kind: TerminalBgKind = luminance > 128 ? "light" : "dark";
        // Anything BEFORE the match start or AFTER the match end is
        // user input that arrived in our window — hand it back.
        const matchStart = str.indexOf(match[0]);
        const matchEnd = matchStart + match[0].length;
        const leftoverStr = str.slice(0, matchStart) + str.slice(matchEnd);
        finish(
          { kind, source: "osc11", rgb: { r, g, b } },
          leftoverStr.length > 0
            ? Buffer.from(leftoverStr, "latin1")
            : undefined,
        );
      }
    };

    try {
      if (!wasRaw && stdin.setRawMode) stdin.setRawMode(true);
      if (wasPaused) stdin.resume();
      stdin.on("data", onData);
      process.stdout.write(OSC11_QUERY);
    } catch {
      finish(null);
      return;
    }

    const timer = setTimeout(() => {
      // Whatever we accumulated isn't an OSC 11 response — give it
      // back to stdin (might be user keystrokes).
      finish(null, buffer.length > 0 ? buffer : undefined);
    }, timeoutMs);
    // Don't keep the event loop alive just for the probe.
    timer.unref?.();
  });
}

/** Channel parser: 16-bit (``FFFF``) or 8-bit (``FF``) → 0-255.
 *  Per OSC 11 spec the response is 16-bit; some terminals shorten. */
function parseChannel(hex: string): number {
  if (hex.length >= 3) {
    // 16-bit (or longer — treat as 16-bit by taking high byte).
    const n = Number.parseInt(hex, 16);
    return Number.isFinite(n) ? n >> 8 : 0;
  }
  // 8-bit short form.
  const n = Number.parseInt(hex, 16);
  return Number.isFinite(n) ? n : 0;
}
