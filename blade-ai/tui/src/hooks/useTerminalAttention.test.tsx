/**
 * Terminal attention tests.
 *
 * Three layers, mirroring the module split:
 *
 *   1. ``ringTerminalAttention`` / ``clearTerminalAttention`` — the raw
 *      escape-sequence fan-out: BEL, OSC 9;4 indeterminate progress,
 *      OSC 777 + OSC 9 notification frames, control-character
 *      sanitisation, and the ``BLADE_AI_DISABLE_BELL`` opt-out.
 *
 *   2. The OSC 9;4 support gate — the progress marker fires only
 *      behind a positive probe (WT_SESSION / ConEmuANSI /
 *      TERM_PROGRAM / TERM=xterm-kitty / VTE_VERSION ≥ 7900), because
 *      blind emission garbles old VTE frontends and posts a bogus
 *      "4;3;0" notification on pre-0.39 kitty (mise#6654).
 *
 *   3. ``useTerminalAttention`` — the edge behaviour through a real
 *      Ink render with a StoreProvider seeded with a pending
 *      confirm_prompt: rings exactly once per false→true transition
 *      (re-renders while waiting never re-ring), maps the card's node
 *      to the zh title the card itself renders, and clears the icon
 *      marker on true→false / unmount.
 *
 * Active dictionary in tests is zh (vitest env pins BLADE_AI_LANG=zh,
 * same as ConfirmMessage.test.tsx).
 */

import { render as inkRender } from "ink-testing-library";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StoreProvider } from "@blade-ai/core";
import type { ConfirmPromptItem } from "@blade-ai/core";
import {
  clearTerminalAttention,
  ringTerminalAttention,
  useTerminalAttention,
} from "./useTerminalAttention.js";

// ── stdout capture ───────────────────────────────────────────────────

let writes: string[];
// Only the restore method is used after setup — keeping the type
// narrow avoids fighting vitest's overloaded ``write`` MockInstance
// generics.
let writeSpy: { mockRestore: () => void };

// Probe env vars drive the OSC 9;4 support gate (plus the opt-out).
// The host terminal may set any of them — running the suite from
// inside kitty/WezTerm/iTerm2 would otherwise flip which sequences
// fire — so every test runs against a scrubbed env with the
// originals restored afterwards.
const SCRUBBED_ENV = [
  "TERM",
  "TERM_PROGRAM",
  "TERM_PROGRAM_VERSION",
  "WT_SESSION",
  "ConEmuANSI",
  "VTE_VERSION",
  "BLADE_AI_DISABLE_BELL",
] as const;
const savedEnv: Record<string, string | undefined> = {};

beforeEach(() => {
  writes = [];
  writeSpy = vi.spyOn(process.stdout, "write").mockImplementation(
    ((chunk: unknown) => {
      writes.push(String(chunk));
      return true;
    }) as unknown as typeof process.stdout.write,
  );
  for (const key of SCRUBBED_ENV) {
    savedEnv[key] = process.env[key];
    delete process.env[key];
  }
});

afterEach(() => {
  writeSpy.mockRestore();
  for (const key of SCRUBBED_ENV) {
    const value = savedEnv[key];
    if (value === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = value;
    }
  }
});

const allWrites = (): string => writes.join("");

const bellCount = (): number => allWrites().split("\x07").length - 1;

/** Flush React passive effects (useEffect) after an Ink rerender. */
const flushEffects = () => new Promise((r) => setTimeout(r, 10));

// ── sequence fan-out ─────────────────────────────────────────────────

describe("ringTerminalAttention", () => {
  it("writes the BEL + indeterminate progress + both notification frames", () => {
    process.env["WT_SESSION"] = "1";
    ringTerminalAttention("标题", "正文");

    const out = allWrites();
    // The lowest common denominator — one bare BEL.
    expect(out.startsWith("\x07")).toBe(true);
    // Dock/taskbar progress marker (state 3 = indeterminate).
    expect(out).toContain("\x1b]9;4;3;0\x07");
    // kitty / GNOME Terminal / WezTerm notification.
    expect(out).toContain("\x1b]777;notify;标题;正文\x07");
    // iTerm2 single-field notification (title folded into body).
    expect(out).toContain("\x1b]9;标题 — 正文\x07");
  });

  it("strips control characters from the notification fields", () => {
    // A hostile payload must not smuggle its own escape sequences or
    // BEL terminators into the frames.
    process.env["WT_SESSION"] = "1";
    ringTerminalAttention("a\x07b", "c\x1bd\x1b]777;notify;evil;e");

    const out = allWrites();
    expect(out).not.toContain("a\x07b");
    // The injected ESC became spaces — the evil OSC payload stays
    // inert text inside the body field, never a live sequence.
    expect(out).toContain("\x1b]777;notify;a b;c d ]777;notify;evil;e\x07");
    // Exactly the four deliberate BEL terminators of one ring.
    expect(bellCount()).toBe(4);
  });

  it("emits nothing when BLADE_AI_DISABLE_BELL=1", () => {
    process.env["BLADE_AI_DISABLE_BELL"] = "1";
    ringTerminalAttention("标题", "正文");
    expect(writes).toEqual([]);
  });
});

describe("clearTerminalAttention", () => {
  it("removes the dock/taskbar progress marker", () => {
    process.env["WT_SESSION"] = "1";
    clearTerminalAttention();
    expect(allWrites()).toBe("\x1b]9;4;0;0\x07");
  });

  it("emits nothing when BLADE_AI_DISABLE_BELL=1", () => {
    process.env["BLADE_AI_DISABLE_BELL"] = "1";
    clearTerminalAttention();
    expect(writes).toEqual([]);
  });
});

// ── OSC 9;4 support gating ──────────────────────────────────────

describe("OSC 9;4 support gating", () => {
  it("skips the progress marker without a positive support signal", () => {
    ringTerminalAttention("标题", "正文");
    const out = allWrites();
    expect(out).not.toContain("\x1b]9;4");
    // The unconditional floor still fires.
    expect(out.startsWith("\x07")).toBe(true);
    expect(out).toContain("\x1b]777;notify;标题;正文\x07");
  });

  it.each<[string, Record<string, string>]>([
    ["Windows Terminal", { WT_SESSION: "guid" }],
    ["ConEmu", { ConEmuANSI: "ON" }],
    ["WezTerm", { TERM_PROGRAM: "WezTerm" }],
    ["Ghostty", { TERM_PROGRAM: "ghostty" }],
    ["kitty", { TERM: "xterm-kitty" }],
    ["VTE 0.79", { VTE_VERSION: "7900" }],
    ["iTerm2 3.6.6", { TERM_PROGRAM: "iTerm.app", TERM_PROGRAM_VERSION: "3.6.6" }],
    ["iTerm2 3.7.0", { TERM_PROGRAM: "iTerm.app", TERM_PROGRAM_VERSION: "3.7.0" }],
  ])("emits the marker on %s", (_name, env) => {
    Object.assign(process.env, env);
    ringTerminalAttention("标题", "正文");
    expect(allWrites()).toContain("\x1b]9;4;3;0\x07");
  });

  it.each<[string, Record<string, string>]>([
    ["old VTE", { VTE_VERSION: "5202" }],
    ["old iTerm2", { TERM_PROGRAM: "iTerm.app", TERM_PROGRAM_VERSION: "3.5.9" }],
    ["Apple Terminal", { TERM_PROGRAM: "Apple_Terminal" }],
    ["alacritty", { TERM: "alacritty" }],
  ])("suppresses the marker on %s", (_name, env) => {
    Object.assign(process.env, env);
    ringTerminalAttention("标题", "正文");
    expect(allWrites()).not.toContain("\x1b]9;4");
  });

  it("clear is gated by the same probe", () => {
    clearTerminalAttention();
    expect(allWrites()).toBe("");
    process.env["WT_SESSION"] = "1";
    clearTerminalAttention();
    expect(allWrites()).toBe("\x1b]9;4;0;0\x07");
  });
});

// ── exit-time marker clear ──────────────────────────────────

describe("exit-time marker clear", () => {
  // ``exitClearInstalled`` is module-level state that earlier tests
  // already tripped — reset modules per case for a clean probe.
  const freshModule = async () => {
    vi.resetModules();
    return await import("./useTerminalAttention.js");
  };

  it("installs the exit hook once on ring and removes it on clear", async () => {
    const mod = await freshModule();
    const onceSpy = vi.spyOn(process, "once");
    const removeSpy = vi.spyOn(process, "removeListener");
    try {
      process.env["WT_SESSION"] = "1";
      mod.ringTerminalAttention("a", "b");
      mod.ringTerminalAttention("a", "b"); // idempotent
      expect(onceSpy.mock.calls.filter((c) => c[0] === "exit").length).toBe(1);
      mod.clearTerminalAttention();
      expect(
        removeSpy.mock.calls.filter((c) => c[0] === "exit").length,
      ).toBe(1);
    } finally {
      onceSpy.mockRestore();
      removeSpy.mockRestore();
    }
  });

  it("the exit hook writes the clear sequence (stdout-safe)", async () => {
    const mod = await freshModule();
    const onceSpy = vi.spyOn(process, "once");
    try {
      process.env["WT_SESSION"] = "1";
      mod.ringTerminalAttention("a", "b");
      const handler = onceSpy.mock.calls.find(
        (c) => c[0] === "exit",
      )?.[1] as (() => void) | undefined;
      expect(handler).toBeTypeOf("function");
      writes = [];
      handler?.(); // simulate the process firing the exit hook
      expect(allWrites()).toBe("\x1b]9;4;0;0\x07");
    } finally {
      onceSpy.mockRestore();
    }
  });

  it("no exit hook when the marker never fired (unsupported terminal)", async () => {
    const mod = await freshModule();
    const onceSpy = vi.spyOn(process, "once");
    try {
      mod.ringTerminalAttention("a", "b"); // env scrubbed → no marker
      expect(
        onceSpy.mock.calls.some((c) => c[0] === "exit"),
      ).toBe(false);
    } finally {
      onceSpy.mockRestore();
    }
  });
});

// ── hook edge behaviour ──────────────────────────────────────────────

const promptWithNode = (node?: string): ConfirmPromptItem => ({
  kind: "confirm_prompt",
  id: "c-prompt-1",
  taskId: "task-abc",
  node,
  selectedIndex: 0,
  mode: "select",
  feedback: "",
  resolved: false,
});

const Probe: React.FC<{ active: boolean }> = ({ active }) => {
  useTerminalAttention(active);
  return null;
};

const renderProbe = (active: boolean, node?: string) =>
  inkRender(
    <StoreProvider initial={{ pending: [promptWithNode(node)] }}>
      <Probe active={active} />
    </StoreProvider>,
  );

describe("useTerminalAttention", () => {
  it("rings on the false→true edge and names the gate by its node", async () => {
    process.env["WT_SESSION"] = "1";
    const { rerender } = renderProbe(false, "confirmation_gate");
    await flushEffects();
    expect(writes).toEqual([]);

    rerender(
      <StoreProvider initial={{ pending: [promptWithNode("confirmation_gate")] }}>
        <Probe active={true} />
      </StoreProvider>,
    );
    await flushEffects();

    // notification_gate → the same zh title the card renders.
    expect(allWrites()).toContain(
      "\x1b]777;notify;确认执行计划;Blade-AI 正在等待你的批准\x07",
    );
    expect(allWrites()).toContain("\x1b]9;4;3;0\x07");
  });

  it("falls back to the generic attention title for unmapped nodes", async () => {
    const { rerender } = renderProbe(false, "plan_builder");
    rerender(
      <StoreProvider initial={{ pending: [promptWithNode("plan_builder")] }}>
        <Probe active={true} />
      </StoreProvider>,
    );
    await flushEffects();

    expect(allWrites()).toContain(
      "\x1b]777;notify;等待确认;Blade-AI 正在等待你的批准\x07",
    );
  });

  it("never re-rings while waiting (re-renders are not new edges)", async () => {
    const { rerender } = renderProbe(false, "confirmation_gate");
    rerender(
      <StoreProvider initial={{ pending: [promptWithNode("confirmation_gate")] }}>
        <Probe active={true} />
      </StoreProvider>,
    );
    await flushEffects();
    const bellsAfterFirstEdge = bellCount();

    rerender(
      <StoreProvider initial={{ pending: [promptWithNode("confirmation_gate")] }}>
        <Probe active={true} />
      </StoreProvider>,
    );
    rerender(
      <StoreProvider initial={{ pending: [promptWithNode("confirmation_gate")] }}>
        <Probe active={true} />
      </StoreProvider>,
    );
    await flushEffects();

    expect(bellCount()).toBe(bellsAfterFirstEdge);
  });

  it("stays silent during replay (re-enacted CONFIRM_RECEIVED)", async () => {
    // /replay translates a recorded InterruptRequired into a real
    // CONFIRM_RECEIVED — streamState flips to waiting_confirmation
    // even though no live gate is waiting. The user is actively
    // watching the replay, so ringing would be a false alarm.
    process.env["WT_SESSION"] = "1";
    const { rerender } = inkRender(
      <StoreProvider
        initial={{
          isReplaying: true,
          streamState: "waiting_confirmation",
          pending: [promptWithNode("confirmation_gate")],
        }}
      >
        <Probe active={false} />
      </StoreProvider>,
    );
    await flushEffects();
    expect(writes).toEqual([]);

    rerender(
      <StoreProvider
        initial={{
          isReplaying: true,
          streamState: "waiting_confirmation",
          pending: [promptWithNode("confirmation_gate")],
        }}
      >
        <Probe active={true} />
      </StoreProvider>,
    );
    await flushEffects();
    expect(writes).toEqual([]);
  });

  it("clears the marker when the wait ends (true→false)", async () => {
    process.env["WT_SESSION"] = "1";
    const { rerender } = renderProbe(false, "confirmation_gate");
    rerender(
      <StoreProvider initial={{ pending: [promptWithNode("confirmation_gate")] }}>
        <Probe active={true} />
      </StoreProvider>,
    );
    await flushEffects();

    rerender(
      <StoreProvider initial={{ pending: [promptWithNode("confirmation_gate")] }}>
        <Probe active={false} />
      </StoreProvider>,
    );
    await flushEffects();

    expect(allWrites().endsWith("\x1b]9;4;0;0\x07")).toBe(true);
    // And no second notification fired on the way down.
    expect(allWrites().split("]777;notify;").length - 1).toBe(1);
  });

  it("clears the marker on unmount while waiting", async () => {
    process.env["WT_SESSION"] = "1";
    const { unmount } = renderProbe(true, "confirmation_gate");
    await flushEffects();
    expect(allWrites()).toContain("\x1b]9;4;3;0\x07");

    unmount();
    await flushEffects();
    expect(allWrites().endsWith("\x1b]9;4;0;0\x07")).toBe(true);
  });
});
