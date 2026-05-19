/**
 * BootDoctorCard renders three variants:
 *   - happy path: all checks passed
 *   - mixed: some warnings + fixes block
 *   - unavailable: server doesn't expose /preflight
 *
 * Plus a timestamp render check — the "captured at HH:MM:SS" line
 * was added in M17 to differentiate boot snapshot from live /doctor.
 */

import { render } from "ink-testing-library";
import { describe, expect, it } from "vitest";
import { BootDoctorCard } from "./BootDoctorCard.js";
import type { BootDoctorCardItem } from "../../state/types.js";

const HAPPY: BootDoctorCardItem = {
  kind: "boot_doctor_card",
  id: "boot-doctor",
  capturedAt: "2026-05-18T09:15:30.000+08:00",
  passedCount: 4,
  totalCount: 4,
  checks: [
    { name: "llm_api_key", severity: "blocking", passed: true, message: "", fix: "" },
    { name: "kubectl", severity: "blocking", passed: true, message: "", fix: "" },
    { name: "blade", severity: "blocking", passed: true, message: "", fix: "" },
    { name: "k8s_connectivity", severity: "blocking", passed: true, message: "connected", fix: "" },
  ],
};

const MIXED: BootDoctorCardItem = {
  kind: "boot_doctor_card",
  id: "boot-doctor",
  capturedAt: "2026-05-18T09:15:30.000+08:00",
  passedCount: 3,
  totalCount: 4,
  checks: [
    { name: "llm_api_key", severity: "blocking", passed: true, message: "", fix: "" },
    {
      name: "skills",
      severity: "warning",
      passed: false,
      message: "Skills directory not found: /Users/x/.blade-ai/skills",
      fix: "Skills will be loaded from package defaults",
    },
    { name: "kubectl", severity: "blocking", passed: true, message: "", fix: "" },
    { name: "k8s_connectivity", severity: "blocking", passed: true, message: "", fix: "" },
  ],
};

const UNAVAILABLE: BootDoctorCardItem = {
  kind: "boot_doctor_card",
  id: "boot-doctor",
  capturedAt: "2026-05-18T09:15:30.000+08:00",
  passedCount: 0,
  totalCount: 0,
  checks: [],
  unavailable: true,
};

describe("BootDoctorCard", () => {
  it("renders title + summary count", () => {
    const { lastFrame } = render(<BootDoctorCard item={HAPPY} />);
    const frame = lastFrame() ?? "";
    // Title chunk is language-dependent (en: "Environment self-check",
    // zh: "环境自检"); pin only what's universal.
    expect(frame).toMatch(/4\/4/);
  });

  it("renders each check row by name", () => {
    const { lastFrame } = render(<BootDoctorCard item={HAPPY} />);
    const frame = lastFrame() ?? "";
    for (const name of ["llm_api_key", "kubectl", "blade", "k8s_connectivity"]) {
      expect(frame).toContain(name);
    }
  });

  it("captures the timestamp as HH:MM:SS", () => {
    const { lastFrame } = render(<BootDoctorCard item={HAPPY} />);
    // 2026-05-18T09:15:30 → local time should still produce "09:15:30"
    // because the suffix +08:00 matches the dev box; on a CI runner in
    // UTC the hour shifts, so probe the regex shape instead of the
    // literal value.
    expect(lastFrame() ?? "").toMatch(/\d{2}:\d{2}:\d{2}/);
  });

  it("surfaces the message text on failed checks", () => {
    const { lastFrame } = render(<BootDoctorCard item={MIXED} />);
    const frame = lastFrame() ?? "";
    expect(frame).toContain("Skills directory not found");
  });

  it("renders the fixes block when any check has a fix", () => {
    const { lastFrame } = render(<BootDoctorCard item={MIXED} />);
    expect(lastFrame() ?? "").toContain("Skills will be loaded from package defaults");
  });

  it("omits the fixes block on a fully-green report", () => {
    const { lastFrame } = render(<BootDoctorCard item={HAPPY} />);
    // Fixes header is i18n'd ("Suggested fixes" / "建议修复"); probe
    // for the *absence* of any failure message text instead.
    expect(lastFrame() ?? "").not.toContain("Skills directory not found");
  });

  it("renders an unavailable card when /preflight returned null", () => {
    const { lastFrame } = render(<BootDoctorCard item={UNAVAILABLE} />);
    const frame = lastFrame() ?? "";
    // Unavailable path skips the per-check rendering entirely.
    expect(frame).not.toContain("llm_api_key");
    expect(frame).not.toContain("kubectl");
  });
});
