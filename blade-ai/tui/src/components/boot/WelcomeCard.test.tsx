/**
 * WelcomeCard render assertions via ink-testing-library.
 *
 * We don't pin the exact frame because Ink's borders + spaces +
 * potential ANSI codes are fragile across versions; instead we probe
 * for the substrings that must be present for the card to be useful.
 *
 * Narrow-mode test: process.stdout.columns is mutated for the duration
 * of one render, then restored. ink-testing-library's render captures
 * a one-shot frame so we don't need to manage the SIGWINCH listener
 * the real useTerminalSize hook subscribes to.
 */

import { render } from "ink-testing-library";
import { afterEach, describe, expect, it } from "vitest";
import { WelcomeCard } from "./WelcomeCard.js";
import type { WelcomeCardItem } from "../../state/types.js";

const SAMPLE: WelcomeCardItem = {
  kind: "welcome_card",
  id: "boot-welcome",
  modelName: "qwen3.6-max-preview",
  permissionMode: "confirm",
  kubeconfig: "/Users/dev/.kube/config",
  namespace: "default",
  version: "0.1.0",
};

const ORIGINAL_COLS = process.stdout.columns;
afterEach(() => {
  // Restore columns so other tests aren't affected. Using
  // Object.defineProperty rather than direct assignment because
  // process.stdout.columns is a getter on some Node builds.
  Object.defineProperty(process.stdout, "columns", {
    value: ORIGINAL_COLS,
    configurable: true,
    writable: true,
  });
});

function setCols(n: number): void {
  Object.defineProperty(process.stdout, "columns", {
    value: n,
    configurable: true,
    writable: true,
  });
}

describe("WelcomeCard / wide terminal", () => {
  it("shows brand line with version", () => {
    setCols(120);
    const { lastFrame } = render(<WelcomeCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("Blade-ai");
    expect(frame).toContain("v0.1.0");
  });

  it("renders the ASCII logo (wide terminal keeps logo)", () => {
    setCols(120);
    const { lastFrame } = render(<WelcomeCard item={SAMPLE} />);
    // Logo first line uses U+2584/2580 box-drawing chars.
    expect(lastFrame() ?? "").toMatch(/█▄▄|█▄█/);
  });

  it("shows model name + permission mode label", () => {
    setCols(120);
    const { lastFrame } = render(<WelcomeCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("qwen3.6-max-preview");
    expect(frame).toMatch(/mode/);
  });

  it("includes runtime fields (kubeconfig + namespace)", () => {
    setCols(120);
    const { lastFrame } = render(<WelcomeCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("kubeconfig");
    expect(frame).toContain("namespace");
    expect(frame).toContain("default");
  });

  it("collapses HOME prefix to ~", () => {
    setCols(120);
    const home = process.env["HOME"] ?? "";
    if (!home) return; // CI sandbox without HOME — skip silently
    const item = { ...SAMPLE, kubeconfig: `${home}/.kube/test-config` };
    const { lastFrame } = render(<WelcomeCard item={item} />);
    expect(lastFrame() ?? "").toContain("~/.kube/test-config");
  });
});

describe("WelcomeCard / narrow terminal", () => {
  it("drops the ASCII logo when terminal is very narrow", () => {
    setCols(35);
    const { lastFrame } = render(<WelcomeCard item={SAMPLE} />);
    // Logo line chars should NOT appear in a 35-col render.
    expect(lastFrame() ?? "").not.toMatch(/█▄▄/);
  });

  it("keeps brand + version even when narrow", () => {
    setCols(35);
    const { lastFrame } = render(<WelcomeCard item={SAMPLE} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("Blade-ai");
    expect(frame).toContain("v0.1.0");
  });
});
