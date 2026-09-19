/**
 * Unit tests for ``parseResumeArgv`` — the argv seam between the
 * Python CLI's ``blade-ai resume -i <sid>`` (execvp passthrough) and
 * BootRunner's takeover branch.
 *
 * The three-state contract:
 *   string    → sid to resume
 *   null      → normal boot (no flag present)
 *   undefined → flag present, value missing (cli.tsx fails loud)
 */

import { describe, expect, it } from "vitest";
import { parseResumeArgv } from "./parseResumeArgv.js";

describe("parseResumeArgv", () => {
  it("returns null when no --resume flag is present", () => {
    expect(parseResumeArgv(["node", "/path/to/cli.js"])).toBeNull();
  });

  it("reads the sid following a space-separated --resume", () => {
    expect(
      parseResumeArgv(["node", "cli.js", "--resume", "sess-abc123"]),
    ).toBe("sess-abc123");
  });

  it("reads the sid from the --resume= form", () => {
    expect(parseResumeArgv(["node", "cli.js", "--resume=sess-xyz"])).toBe(
      "sess-xyz",
    );
  });

  it("returns undefined for a trailing --resume with no value", () => {
    expect(parseResumeArgv(["node", "cli.js", "--resume"])).toBeUndefined();
  });

  it("returns undefined for --resume= with an empty value", () => {
    expect(parseResumeArgv(["node", "cli.js", "--resume="])).toBeUndefined();
  });

  it("returns undefined when the value after --resume is an empty string", () => {
    expect(parseResumeArgv(["node", "cli.js", "--resume", ""])).toBeUndefined();
  });

  it("ignores unrelated flags before --resume", () => {
    expect(
      parseResumeArgv(["node", "cli.js", "--foo", "bar", "--resume", "s1"]),
    ).toBe("s1");
  });

  it("takes the first --resume occurrence", () => {
    expect(
      parseResumeArgv([
        "node",
        "cli.js",
        "--resume",
        "first",
        "--resume",
        "second",
      ]),
    ).toBe("first");
  });
});
