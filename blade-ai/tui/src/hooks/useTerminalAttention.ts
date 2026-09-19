/**
 * Terminal-level attention signals for a pending confirmation card.
 *
 * Problem: when a confirm gate fires the user is often heads-down in
 * another window — the card sits in the TUI, the gate times out, and
 * nobody noticed. Terminals offer no iOS-style "badge on the app
 * icon" API to a child process, but three de-facto-standard escape
 * sequences combined come close. IMPORTANT: these are interpreted by
 * the terminal emulator the human sits in front of, not by the OS the
 * process runs on — SSH'ing to a Linux server from a local kitty gets
 * kitty's behaviour, whatever the server is.
 *
 *   1. ``BEL`` (\x07) — the universal floor, works on every platform
 *      and every emulator: macOS Terminal.app bounces its dock icon,
 *      iTerm2 rings, Windows Terminal flashes the taskbar entry,
 *      X11/Wayland terminals map it to the window urgency hint
 *      (taskbar flash / Ubuntu dock attention marker), tmux/screen
 *      flag the pane with a bell marker. No opt-in needed anywhere.
 *
 *   2. ``OSC 9 ; 4 ; 3 ; 0`` — the ConEmu-origin "indeterminate
 *      progress" marker on the terminal's own icon: the closest
 *      analogue to an app-icon badge, persisting until cleared with
 *      ``OSC 9 ; 4 ; 0 ; 0`` when the gate resolves. Support is recent
 *      and spotty — Windows Terminal / ConEmu (Windows), WezTerm,
 *      Ghostty (≥1.2), kitty (≥0.39), iTerm2 (≥3.6.6, late 2025) and
 *      VTE ≥0.79 frontends (GNOME Terminal on 2025-era distros) — and
 *      emitting it blind is NOT safe: old VTE frontends print the raw
 *      sequence as garbage (mise#6654) and pre-0.39 kitty parses it as
 *      a legacy OSC 9 notification ("4;3;0"). So it fires only behind
 *      a positive support probe — the same scoping rust's cargo
 *      applies to its progress reporting.
 *
 *   3. ``OSC 9 ; message`` (iTerm2's notification form; kitty and
 *      Ghostty implement it for compat) and ``OSC 777 ; notify`` (the
 *      rxvt-unicode form; WezTerm, urxvt family) — desktop
 *      notification banners naming the waiting gate. kitty's own
 *      protocol is OSC 99 and it does NOT take 777, which is why both
 *      forms are sent. These two have decades of ecosystem use;
 *      unsupported terminals swallow unknown OSC silently.
 *
 * Deliberately NOT used: OSC 0/2 (set terminal title). We cannot read
 * the user's original title back, so any "restore" would clobber it
 * with a guess.
 *
 * Opt-out: ``BLADE_AI_DISABLE_BELL=1`` disables the whole group (read
 * at call time, so it can be toggled from a wrapper script).
 */

import { useEffect, useRef } from "react";
import { t, useAppSelector } from "@blade-ai/core";
import type { ConfirmPromptItem } from "@blade-ai/core";

const BEL = "\x07";
/** OSC 9;4 — state 3 = indeterminate progress, 0 = remove marker. */
const OSC_PROGRESS_INDETERMINATE = "\x1b]9;4;3;0" + BEL;
const OSC_PROGRESS_REMOVE = "\x1b]9;4;0;0" + BEL;

/** Strip control characters so a hostile card payload can't smuggle
 *  its own escape sequences into the notification frames. */
const sanitize = (s: string): string =>
  s.replace(/[\x00-\x1f\x7f]/g, " ").trim();

const osc9Notify = (message: string): string =>
  "\x1b]9;" + sanitize(message) + BEL;
const osc777Notify = (title: string, body: string): string =>
  "\x1b]777;notify;" + sanitize(title) + ";" + sanitize(body) + BEL;

const attentionDisabled = (): boolean =>
  process.env["BLADE_AI_DISABLE_BELL"] === "1";

/** Best-effort marker removal if the process exits without React
 *  unmounting (unmount-threw branch in cli.tsx's signal handler, a
 *  mid-wait crash, ``process.exit`` from a failsafe path). Same
 *  pattern as synchronizedOutput's exit handler — synchronous write
 *  only, stderr/stdout failures swallowed. SIGKILL still leaks the
 *  marker until the terminal clears it; that is true of every OSC
 *  9;4 consumer (cargo, mise, systemd) and cannot be fixed from the
 *  child process. */
let exitClearInstalled = false;
const clearMarkerOnExit = (): void => {
  try {
    process.stdout?.write(OSC_PROGRESS_REMOVE);
  } catch {
    // stdout may already be closed during shutdown.
  }
};
const ensureExitClear = (): void => {
  if (exitClearInstalled) return;
  exitClearInstalled = true;
  process.once("exit", clearMarkerOnExit);
};

/** ``"3.6.6" >= [3, 6, 6]`` — numeric dot-parts compared left to
 *  right, missing parts count as 0, and a longer actual version than
 *  the minimum passes (3.7.0 > 3.6.6). */
const versionAtLeast = (v: string, min: readonly number[]): boolean => {
  const parts = v.split(".").map((p) => Number.parseInt(p, 10));
  for (let i = 0; i < min.length; i++) {
    const raw = parts[i];
    const got = raw !== undefined && Number.isFinite(raw) ? raw : 0;
    const floor = min[i] ?? 0;
    if (got > floor) return true;
    if (got < floor) return false;
  }
  return true;
};

/** Positive identification of a terminal that understands the
 *  ConEmu-style OSC 9;4 progress marker. An allowlist, not a
 *  blocklist: terminals NOT positively identified are excluded
 *  because emitting blind garbles output on old VTE frontends
 *  (mise#6654) and posts a bogus "4;3;0" notification on pre-0.39
 *  kitty. Same scoping rust's cargo applies. Signals:
 *    WT_SESSION              Windows Terminal
 *    ConEmuANSI              ConEmu
 *    TERM_PROGRAM            ghostty / WezTerm / iTerm.app (≥3.6.6)
 *    TERM=xterm-kitty        kitty (≥0.39; version not detectable)
 *    VTE_VERSION ≥ 7900      VTE 0.79+ frontends (GNOME Terminal…) */
const progressMarkerSupported = (): boolean => {
  const env = process.env;
  if (env["WT_SESSION"] !== undefined) return true;
  if (env["ConEmuANSI"] !== undefined && env["ConEmuANSI"] !== "") {
    return true;
  }
  const program = env["TERM_PROGRAM"] ?? "";
  if (program === "ghostty" || program === "WezTerm") return true;
  if (program === "iTerm.app") {
    return versionAtLeast(env["TERM_PROGRAM_VERSION"] ?? "", [3, 6, 6]);
  }
  if ((env["TERM"] ?? "").startsWith("xterm-kitty")) return true;
  const vte = Number.parseInt(env["VTE_VERSION"] ?? "", 10);
  return Number.isFinite(vte) && vte >= 7900;
};

/** Fire every attention signal at once. Split from the hook so the
 *  sequences are unit-testable without a React tree. */
export function ringTerminalAttention(title: string, body: string): void {
  if (attentionDisabled() || !process.stdout) return;
  process.stdout.write(BEL);
  // Gated: only where OSC 9;4 is positively supported (see
  // progressMarkerSupported — blind emission garbles old VTE).
  if (progressMarkerSupported()) {
    ensureExitClear();
    process.stdout.write(OSC_PROGRESS_INDETERMINATE);
  }
  // iTerm2's OSC 9 is single-field — fold title into the body.
  process.stdout.write(osc9Notify(title + " — " + body));
  process.stdout.write(osc777Notify(title, body));
}

/** Remove the dock/taskbar progress marker once the gate resolves.
 *  Gated by the same probe as the set — if we never set it, there is
 *  nothing to clear, and on unsupported terminals the clear sequence
 *  carries the same garble risk as the set. */
export function clearTerminalAttention(): void {
  if (attentionDisabled() || !process.stdout) return;
  if (!progressMarkerSupported()) return;
  if (exitClearInstalled) {
    process.removeListener("exit", clearMarkerOnExit);
    exitClearInstalled = false;
  }
  process.stdout.write(OSC_PROGRESS_REMOVE);
}

/** Confirm-card node → the title the card itself already renders,
 *  so the notification names the same gate the user will see. */
const NODE_TITLE_KEYS: Record<string, string> = {
  intent_confirm: "confirm.intent.title",
  confirmation_gate: "confirm.execution.title",
  tool_screener: "confirm.targetChange.title",
  plan_change_confirm: "confirm.planChange.title",
};

/**
 * Ring the terminal when ``active`` flips true (a confirmation card
 * started waiting) and clear the icon marker when it flips false
 * (user decided, turn aborted, or timeout ended the wait).
 *
 * Silent during replay (``state.isReplaying``): /replay re-enacts a
 * recorded ``InterruptRequired`` as a real CONFIRM_RECEIVED, which
 * flips ``streamState`` to ``waiting_confirmation`` — but the user is
 * actively watching the re-enactment; ringing then would claim a live
 * gate is waiting when none is.
 *
 * Edge-triggered via the effect dependency on ``active`` alone:
 * re-renders while waiting (tokens, phase events…) never re-ring.
 * The node→title lookup goes through a ref for the same reason — a
 * mid-wait node change is not a new attention event.
 */
export function useTerminalAttention(active: boolean): void {
  const replaying = useAppSelector((s) => s.isReplaying);
  const replayingRef = useRef(replaying);
  replayingRef.current = replaying;
  const node = useAppSelector((s) => {
    const prompt = s.pending.find(
      (item): item is ConfirmPromptItem =>
        item.kind === "confirm_prompt" && !item.resolved,
    );
    return prompt?.node ?? "";
  });
  const nodeRef = useRef(node);
  nodeRef.current = node;

  useEffect(() => {
    if (!active || replayingRef.current) return;
    const titleKey =
      NODE_TITLE_KEYS[nodeRef.current] ?? "confirm.attention.title";
    ringTerminalAttention(t(titleKey), t("confirm.attention.body"));
    return () => clearTerminalAttention();
    // Deps are [active] only — see docblock above.
  }, [active]);
}
