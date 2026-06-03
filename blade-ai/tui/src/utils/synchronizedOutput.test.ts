/**
 * Tests for ``terminalSupportsSynchronizedOutput`` — the only piece
 * worth unit-testing in synchronizedOutput.ts. The install/restore
 * dance monkey-patches process.stdout and exercising it would either
 * touch real stdout or require a stub that's larger than the helper.
 *
 * Contract to lock:
 *   · Explicit disable wins over everything else.
 *   · Explicit force-on wins over the auto-disable for SSH/TMUX.
 *   · iTerm2 / WezTerm allow-list hit by TERM_PROGRAM.
 *   · kitty allow-list hit by KITTY_WINDOW_ID OR TERM contains "kitty".
 *   · SSH / TMUX env vars trigger auto-disable (so multi-hop terminals
 *     don't get the sequence and mishandle it).
 *   · Unrecognised terminal → false.
 */

import { describe, expect, it } from "vitest";
import { terminalSupportsSynchronizedOutput } from "./synchronizedOutput.js";

const env = (overrides: Record<string, string | undefined> = {}) =>
  ({ ...overrides }) as NodeJS.ProcessEnv;

describe("terminalSupportsSynchronizedOutput", () => {
  it("returns false for an unknown terminal", () => {
    expect(terminalSupportsSynchronizedOutput(env({ TERM: "xterm-256color" }))).toBe(false);
  });

  it("returns true for iTerm.app", () => {
    expect(
      terminalSupportsSynchronizedOutput(env({ TERM_PROGRAM: "iTerm.app" })),
    ).toBe(true);
  });

  it("returns true for WezTerm", () => {
    expect(
      terminalSupportsSynchronizedOutput(env({ TERM_PROGRAM: "WezTerm" })),
    ).toBe(true);
  });

  it("returns true when KITTY_WINDOW_ID is set", () => {
    expect(
      terminalSupportsSynchronizedOutput(env({ KITTY_WINDOW_ID: "1" })),
    ).toBe(true);
  });

  it("returns true when TERM contains 'kitty'", () => {
    expect(
      terminalSupportsSynchronizedOutput(env({ TERM: "xterm-kitty" })),
    ).toBe(true);
  });

  it("disables under TMUX even on iTerm.app (nested terminal hazard)", () => {
    expect(
      terminalSupportsSynchronizedOutput(
        env({ TMUX: "/tmp/tmux-501/default,1234,0", TERM_PROGRAM: "iTerm.app" }),
      ),
    ).toBe(false);
  });

  it("disables under SSH_TTY / SSH_CLIENT (remote terminal hazard)", () => {
    expect(
      terminalSupportsSynchronizedOutput(
        env({ SSH_TTY: "/dev/pts/1", TERM_PROGRAM: "iTerm.app" }),
      ),
    ).toBe(false);
    expect(
      terminalSupportsSynchronizedOutput(
        env({ SSH_CLIENT: "1.2.3.4 5678 22", TERM_PROGRAM: "WezTerm" }),
      ),
    ).toBe(false);
  });

  it("explicit BLADE_AI_DISABLE_SYNCHRONIZED_OUTPUT=1 wins over everything", () => {
    expect(
      terminalSupportsSynchronizedOutput(
        env({
          BLADE_AI_DISABLE_SYNCHRONIZED_OUTPUT: "1",
          TERM_PROGRAM: "iTerm.app",
        }),
      ),
    ).toBe(false);
  });

  it("explicit BLADE_AI_FORCE_SYNCHRONIZED_OUTPUT=1 wins over SSH auto-disable", () => {
    expect(
      terminalSupportsSynchronizedOutput(
        env({
          BLADE_AI_FORCE_SYNCHRONIZED_OUTPUT: "1",
          SSH_TTY: "/dev/pts/1",
          TERM: "xterm-256color",
        }),
      ),
    ).toBe(true);
  });

  it("BLADE_AI_SYNCHRONIZED_OUTPUT=0 and =1 are honoured as shorthands", () => {
    expect(
      terminalSupportsSynchronizedOutput(
        env({
          BLADE_AI_SYNCHRONIZED_OUTPUT: "0",
          TERM_PROGRAM: "iTerm.app",
        }),
      ),
    ).toBe(false);
    expect(
      terminalSupportsSynchronizedOutput(
        env({ BLADE_AI_SYNCHRONIZED_OUTPUT: "1", TERM: "xterm" }),
      ),
    ).toBe(true);
  });
});
