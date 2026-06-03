import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { detectTerminalBg, parseColorFgBg } from "./terminalBg.js";

describe("utils / parseColorFgBg", () => {
  // ANSI palette index → bg kind mapping. Each table row pins one
  // documented boundary case (the spec mapping is non-obvious for
  // 7 and 8 specifically, see the JSDoc on parseColorFgBg).
  it("returns null for undefined / empty / single-field input", () => {
    expect(parseColorFgBg(undefined)).toBeNull();
    expect(parseColorFgBg("")).toBeNull();
    expect(parseColorFgBg("15")).toBeNull();
  });

  it("returns null when the bg field is 'default'", () => {
    // Konsole writes the 3-part form when one end is the terminal's
    // own default; we can't tell light or dark from that.
    expect(parseColorFgBg("15;default;default")).toBeNull();
    expect(parseColorFgBg("default;default")).toBeNull();
  });

  it("returns null for malformed numeric bg", () => {
    expect(parseColorFgBg("0;abc")).toBeNull();
    expect(parseColorFgBg("0;42")).toBeNull(); // out of 0-15 palette
    expect(parseColorFgBg("0;-1")).toBeNull();
  });

  it("maps dark palette indices to 'dark'", () => {
    // 0-6 → dark (black + the six dark colour variants)
    for (let i = 0; i <= 6; i++) {
      expect(parseColorFgBg(`15;${i}`)).toBe("dark");
    }
    // 8 → bright black (dark grey) → dark
    expect(parseColorFgBg("15;8")).toBe("dark");
  });

  it("maps light palette indices to 'light'", () => {
    // 7 → white (treated as light, per the rxvt convention noted in
    // the impl's JSDoc — most rxvt-likes use 7 for the actual white
    // background even though it's not 'bright')
    expect(parseColorFgBg("0;7")).toBe("light");
    // 9-15 → bright variants, typically light backgrounds
    for (let i = 9; i <= 15; i++) {
      expect(parseColorFgBg(`0;${i}`)).toBe("light");
    }
  });

  it("uses the LAST field as bg (3-part form support)", () => {
    // Konsole "fg;default;bg" form — bg is last, the middle 'default'
    // is just a separator marking that bg follows.
    expect(parseColorFgBg("0;default;15")).toBe("light");
    expect(parseColorFgBg("15;default;0")).toBe("dark");
  });
});

describe("utils / detectTerminalBg — env override path", () => {
  // We only test the env-override branch here — the OSC 11 path
  // depends on real stdin/stdout TTY plumbing that's painful to
  // mock cleanly. COLORFGBG and OSC paths are covered by manual
  // smoke testing against actual terminals.

  const savedOverride = process.env["BLADE_AI_TERMINAL_BG"];
  const savedCfbg = process.env["COLORFGBG"];

  beforeEach(() => {
    delete process.env["BLADE_AI_TERMINAL_BG"];
    delete process.env["COLORFGBG"];
  });

  afterEach(() => {
    if (savedOverride !== undefined) {
      process.env["BLADE_AI_TERMINAL_BG"] = savedOverride;
    } else {
      delete process.env["BLADE_AI_TERMINAL_BG"];
    }
    if (savedCfbg !== undefined) {
      process.env["COLORFGBG"] = savedCfbg;
    } else {
      delete process.env["COLORFGBG"];
    }
  });

  it("honours BLADE_AI_TERMINAL_BG=light (lowercase)", async () => {
    process.env["BLADE_AI_TERMINAL_BG"] = "light";
    const r = await detectTerminalBg(0); // 0ms timeout: OSC path is skipped
    expect(r.kind).toBe("light");
    expect(r.source).toBe("env_override");
  });

  it("honours BLADE_AI_TERMINAL_BG=DARK (uppercase) via lowercase normalisation", async () => {
    process.env["BLADE_AI_TERMINAL_BG"] = "DARK";
    const r = await detectTerminalBg(0);
    expect(r.kind).toBe("dark");
    expect(r.source).toBe("env_override");
  });

  it("ignores BLADE_AI_TERMINAL_BG=auto (unrecognised value)", async () => {
    // Falls through to next source — should NOT crash, should NOT
    // return "auto" as kind. Without COLORFGBG and without a TTY
    // capable of OSC 11, ends up at fallback 'dark'.
    process.env["BLADE_AI_TERMINAL_BG"] = "auto";
    const r = await detectTerminalBg(0);
    expect(r.source).not.toBe("env_override");
  });

  it("trims whitespace from override value", async () => {
    process.env["BLADE_AI_TERMINAL_BG"] = "  light  ";
    const r = await detectTerminalBg(0);
    expect(r.kind).toBe("light");
    expect(r.source).toBe("env_override");
  });
});
