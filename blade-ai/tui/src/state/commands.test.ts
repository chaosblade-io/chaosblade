/**
 * Slash-registry pure-function tests.
 *
 * Handler dispatch tests live in scripts/smoke-slash.mjs because they
 * need a wired ctx (client stub, dispatch closure) — easier to do in a
 * single end-to-end script than to set up here. This file covers the
 * pure-function surface that has no React/IO dependency: the parser
 * and the registry's filter / lookup.
 */

import { describe, expect, it } from "vitest";
import {
  buildRegistry,
  parseSlashCommand,
  parseSlashLine,
  parseTasksArgs,
  passesTasksFilter,
  SlashCommandRegistry,
  type SlashCommand,
} from "./commands.js";

describe("parseSlashLine", () => {
  it("returns null for non-slash input", () => {
    expect(parseSlashLine("hello")).toBeNull();
    expect(parseSlashLine("")).toBeNull();
  });

  it("extracts a bare command name", () => {
    const r = parseSlashLine("/help");
    expect(r).toEqual({ name: "help", args: [] });
  });

  it("lowercases the command name", () => {
    const r = parseSlashLine("/HELP");
    expect(r?.name).toBe("help");
  });

  it("splits whitespace-separated args", () => {
    const r = parseSlashLine("/replay task-123 4x");
    expect(r).toEqual({ name: "replay", args: ["task-123", "4x"] });
  });

  it("collapses runs of whitespace", () => {
    const r = parseSlashLine("/replay   task-1");
    expect(r).toEqual({ name: "replay", args: ["task-1"] });
  });

  it("returns null on a lonely slash", () => {
    expect(parseSlashLine("/")).toBeNull();
  });

  it("handles trailing whitespace", () => {
    const r = parseSlashLine("/help   ");
    expect(r).toEqual({ name: "help", args: [] });
  });
});

describe("registry / filter + lookup", () => {
  const reg = buildRegistry();

  it("registers all M1–M12 built-in commands", () => {
    const visible = reg.list().map((c) => c.name);
    // Spot-check rather than pin the exact list — new commands are
    // expected to be added; missing one of these is a regression.
    for (const expected of [
      "help",
      "clear",
      "exit",
      "mode",
      "permission",
      "session",
      "tasks",
      "recordings",
      "replay",
      "doctor",
      "retry",
      "run",
    ]) {
      expect(visible).toContain(expected);
    }
    // Hidden aliases are callable but absent from the visible list.
    // ``/status`` and ``/inject`` were renamed (to ``/session`` and
    // ``/run`` respectively) but kept around as hidden entries so
    // muscle memory keeps working. Assert both invariants.
    expect(visible).not.toContain("status");
    expect(visible).not.toContain("inject");
    const all = reg.list({ includeHidden: true }).map((c) => c.name);
    expect(all).toContain("status");
    expect(all).toContain("inject");
    expect(reg.get("status")?.name).toBe("status"); // resolves
    expect(reg.get("inject")?.name).toBe("inject"); // resolves
  });

  it("lists commands sorted by name", () => {
    const names = reg.list().map((c) => c.name);
    const sorted = [...names].sort((a, b) => a.localeCompare(b));
    expect(names).toEqual(sorted);
  });

  it("filter('') returns the full list", () => {
    expect(reg.filter("").length).toBe(reg.list().length);
  });

  it("filter prefix-matches on names and aliases", () => {
    expect(reg.filter("hel").map((c) => c.name)).toContain("help");
    // ``?`` is an alias of ``help`` per buildRegistry. filter("?")
    // should still surface help.
    expect(reg.filter("?").map((c) => c.name)).toContain("help");
  });

  it("get() resolves by canonical name and alias", () => {
    expect(reg.get("help")?.name).toBe("help");
    expect(reg.get("?")?.name).toBe("help");
    expect(reg.get("quit")?.name).toBe("exit");
  });

  it("get() is case-insensitive", () => {
    expect(reg.get("HELP")?.name).toBe("help");
  });

  it("get() returns undefined for unknown commands", () => {
    expect(reg.get("nonexistent")).toBeUndefined();
  });
});

describe("parseSlashCommand", () => {
  // Build a tiny fixture registry — no need to touch the production
  // commands. We're testing parsing semantics, not the commands
  // themselves.
  function makeReg(): SlashCommandRegistry {
    const subHandler = async () => {};
    const cmds: SlashCommand[] = [
      {
        name: "help",
        description: "h",
        group: "general",
        aliases: ["?"],
        handler: async () => {},
      },
      {
        name: "exit",
        description: "x",
        group: "general",
        aliases: ["quit"],
        handler: async () => {},
      },
      {
        name: "skills",
        description: "s",
        group: "skills",
        subcommands: {
          list: { name: "list", description: "ls", handler: subHandler },
          install: {
            name: "install",
            description: "i",
            usage: "<src>",
            handler: subHandler,
          },
        },
        handler: async () => {},
      },
      {
        name: "old",
        description: "deprecated",
        group: "general",
        hidden: true,
        handler: async () => {},
      },
    ];
    return new SlashCommandRegistry(cmds);
  }

  it("returns null for non-slash input", () => {
    expect(parseSlashCommand("hello", makeReg())).toBeNull();
  });

  it("returns null for unknown root", () => {
    expect(parseSlashCommand("/nope", makeReg())).toBeNull();
  });

  it("resolves alias to canonical root", () => {
    const r = parseSlashCommand("/quit", makeReg());
    expect(r?.root).toBe("exit");
    expect(r?.sub).toBe("");
  });

  it("recognises subcommand match", () => {
    const r = parseSlashCommand("/skills list", makeReg());
    expect(r).toEqual({
      root: "skills",
      sub: "list",
      args: [],
      rawArgs: "",
    });
  });

  it("captures args after sub", () => {
    const r = parseSlashCommand("/skills install foo bar", makeReg());
    expect(r?.root).toBe("skills");
    expect(r?.sub).toBe("install");
    expect(r?.args).toEqual(["foo", "bar"]);
    expect(r?.rawArgs).toBe("foo bar");
  });

  it("falls back to bare-root when next token isn't a sub", () => {
    const r = parseSlashCommand("/skills nonsense extra", makeReg());
    expect(r).toEqual({
      root: "skills",
      sub: "",
      args: ["nonsense", "extra"],
      rawArgs: "nonsense extra",
    });
  });

  it("preserves rawArgs whitespace verbatim for bare-root commands", () => {
    // Verbatim raw is the load-bearing path for /run <NL> and
    // /config set <key> <value with spaces>.
    const r = parseSlashCommand("/help   foo  bar", makeReg());
    expect(r?.rawArgs).toBe("foo  bar");
  });

  it("matches sub case-insensitively", () => {
    const r = parseSlashCommand("/skills LIST", makeReg());
    expect(r?.sub).toBe("list");
  });

  it("matches root case-insensitively", () => {
    const r = parseSlashCommand("/SKILLS list", makeReg());
    expect(r?.root).toBe("skills");
    expect(r?.sub).toBe("list");
  });

  it("hidden command resolves through registry.get but not list", () => {
    const reg = makeReg();
    const r = parseSlashCommand("/old", reg);
    expect(r?.root).toBe("old"); // still callable
    // ...but not in the visible list.
    expect(reg.list().some((c) => c.name === "old")).toBe(false);
    expect(reg.list({ includeHidden: true }).some((c) => c.name === "old")).toBe(
      true,
    );
  });

  it("returns null for lonely /", () => {
    expect(parseSlashCommand("/", makeReg())).toBeNull();
    expect(parseSlashCommand("/   ", makeReg())).toBeNull();
  });
});

describe("registry / streamSafe per-sub strict semantics", () => {
  // Composer's stream-safe gate uses per-(root, sub) classification:
  //   - sub matched → sub.streamSafe alone decides (parent's flag
  //     does NOT inherit through to the sub)
  //   - no sub matched → parent.streamSafe decides
  //
  // These tests pin the contract so a future regression where the
  // gate quietly switches back to "either parent OR sub" semantics
  // fails here — the practical risk is destructive subs slipping
  // through under a streamSafe-marked parent (e.g. ``/skills``
  // bare = list (safe), ``/skills install`` = network mutation
  // (unsafe); the parent's streamSafe must NOT whitelist install).
  function makeReg(): SlashCommandRegistry {
    return new SlashCommandRegistry([
      {
        name: "skills",
        description: "skills",
        group: "skills",
        streamSafe: true, // bare /skills lists — safe
        subcommands: {
          list: {
            name: "list",
            description: "ls",
            streamSafe: true,
            handler: async () => {},
          },
          install: {
            // Mutating sub — must NOT inherit parent streamSafe.
            name: "install",
            description: "i",
            handler: async () => {},
          },
        },
        handler: async () => {},
      },
    ]);
  }

  it("bare root inherits root streamSafe", () => {
    const reg = makeReg();
    const cmd = reg.get("skills")!;
    expect(cmd.streamSafe).toBe(true);
  });

  it("safe sub stays safe", () => {
    const reg = makeReg();
    const sub = reg.get("skills")!.subcommands!["list"]!;
    expect(sub.streamSafe).toBe(true);
  });

  it("unsafe sub does NOT inherit parent streamSafe", () => {
    const reg = makeReg();
    const sub = reg.get("skills")!.subcommands!["install"]!;
    // ``streamSafe`` is undefined on the sub — Composer's gate must
    // treat that as "block mid-stream" regardless of parent's flag.
    expect(sub.streamSafe).toBeUndefined();
  });
});

describe("registry / dynamic + groups", () => {
  it("buildRegistry accepts dynamic skill commands", () => {
    const dyn: SlashCommand[] = [
      {
        name: "k8s-restart",
        description: "auto-injected by skills loader",
        group: "general", // overridden to "dynamic"
        origin: "/path/to/skill",
        handler: async () => {},
      },
    ];
    const reg = buildRegistry({ dynamicCommands: dyn });
    const cmd = reg.get("k8s-restart");
    expect(cmd).toBeDefined();
    // The dynamic flag forces the group to "dynamic" regardless of
    // whatever the skill loader passed in — that's the contract.
    expect(cmd?.group).toBe("dynamic");
  });

  it("dynamic commands cannot displace built-ins with the same name", () => {
    const dyn: SlashCommand[] = [
      {
        name: "help", // collision with built-in
        description: "rogue skill",
        group: "general",
        handler: async () => {
          throw new Error("rogue skill should never run");
        },
      },
    ];
    const reg = buildRegistry({ dynamicCommands: dyn });
    // Built-in /help wins; the rogue dynamic was silently dropped.
    expect(reg.get("help")?.description).not.toBe("rogue skill");
  });

  it("listByGroup buckets commands in display order", () => {
    const reg = buildRegistry();
    const buckets = reg.listByGroup();
    // Every built-in lands in one of the four canonical groups.
    const total =
      buckets.general.length +
      buckets.business.length +
      buckets.skills.length +
      buckets.dynamic.length;
    expect(total).toBe(reg.list().length);
  });
});

describe("Phase 2 / /tasks filter parsing", () => {
  it("defaults to {filter: 'all', limit: 10} for empty args", () => {
    expect(parseTasksArgs([])).toEqual({ filter: "all", limit: 10 });
  });

  it("recognises filter token in any position", () => {
    expect(parseTasksArgs(["active"])).toEqual({ filter: "active", limit: 10 });
    expect(parseTasksArgs(["failed"])).toEqual({ filter: "failed", limit: 10 });
    expect(parseTasksArgs(["all"])).toEqual({ filter: "all", limit: 10 });
  });

  it("recognises numeric limit in any position", () => {
    expect(parseTasksArgs(["20"])).toEqual({ filter: "all", limit: 20 });
    expect(parseTasksArgs(["20", "active"])).toEqual({
      filter: "active",
      limit: 20,
    });
    // Either order — Python TUI accepts the same.
    expect(parseTasksArgs(["active", "5"])).toEqual({
      filter: "active",
      limit: 5,
    });
  });

  it("ignores unknown tokens (no crash)", () => {
    expect(parseTasksArgs(["foo"])).toEqual({ filter: "all", limit: 10 });
    expect(parseTasksArgs(["foo", "active", "bar"])).toEqual({
      filter: "active",
      limit: 10,
    });
  });

  it("filter token is lower-cased before matching", () => {
    expect(parseTasksArgs(["ACTIVE"])).toEqual({ filter: "active", limit: 10 });
    expect(parseTasksArgs(["Failed"])).toEqual({ filter: "failed", limit: 10 });
  });

  it("rejects non-positive limits (zero, negative, NaN)", () => {
    expect(parseTasksArgs(["0"])).toEqual({ filter: "all", limit: 10 });
    expect(parseTasksArgs(["-3"])).toEqual({ filter: "all", limit: 10 });
  });
});

describe("Phase 2 / passesTasksFilter", () => {
  // Pinned to Python's _ACTIVE_PHASES / _FAILED_STATUSES literals so a
  // future drift on either side fails here loudly. The handler relies
  // on the ports staying in lockstep — without these the ``active``
  // slice could silently start hiding tasks the Python TUI shows.

  it("'all' includes every task", () => {
    expect(passesTasksFilter({}, "all")).toBe(true);
    expect(
      passesTasksFilter({ phase: "wibble", status: "wobble" }, "all"),
    ).toBe(true);
  });

  it("'active' requires a mid-pipeline phase AND non-failed status", () => {
    for (const phase of ["planning", "executing", "verifying", "dry_run_planned"]) {
      expect(passesTasksFilter({ phase, status: "running" }, "active")).toBe(
        true,
      );
    }
    // failed status defeats active even when phase qualifies
    expect(
      passesTasksFilter({ phase: "executing", status: "failed" }, "active"),
    ).toBe(false);
    // non-active phase rejected
    expect(
      passesTasksFilter({ phase: "completed", status: "ok" }, "active"),
    ).toBe(false);
  });

  it("'failed' matches the FAILED_STATUSES set, regardless of phase", () => {
    for (const status of ["failed", "error"]) {
      expect(passesTasksFilter({ phase: "any", status }, "failed")).toBe(true);
    }
    expect(
      passesTasksFilter({ phase: "executing", status: "running" }, "failed"),
    ).toBe(false);
  });

  it("missing fields default to empty (no crash)", () => {
    expect(passesTasksFilter({}, "active")).toBe(false);
    expect(passesTasksFilter({}, "failed")).toBe(false);
  });

  it("phase comparison is case-sensitive (Python does NOT lowercase phase)", () => {
    // Python's `_passes_filter` compares ``phase`` against the lower-
    // case literals in ``_ACTIVE_PHASES`` without any case normalisation.
    // Pin TS to the same strictness — a future regression where TS
    // becomes more lenient than Python (matching "Executing" while
    // Python rejects it) would silently change the row count between
    // the two TUIs on the same data.
    expect(
      passesTasksFilter({ phase: "EXECUTING", status: "running" }, "active"),
    ).toBe(false);
    expect(
      passesTasksFilter({ phase: "Executing", status: "running" }, "active"),
    ).toBe(false);
    expect(
      passesTasksFilter({ phase: "executing", status: "running" }, "active"),
    ).toBe(true);
  });

  it("status comparison IS lowercased (matches Python's status.lower())", () => {
    // Python explicitly lowers ``status`` before the membership check
    // — both TUIs accept ``FAILED`` / ``Failed`` / ``failed`` for the
    // failed slice. Locking this asymmetry with phase prevents a
    // refactor that "fixes" one to match the other inadvertently.
    expect(
      passesTasksFilter({ phase: "any", status: "FAILED" }, "failed"),
    ).toBe(true);
    expect(
      passesTasksFilter({ phase: "any", status: "Error" }, "failed"),
    ).toBe(true);
  });
});

describe("Phase 2 / new command registration", () => {
  const reg = buildRegistry();

  it("registers /review /experiments /recover /skills as visible business/skills commands", () => {
    const visible = reg.list().map((c) => c.name);
    for (const name of ["review", "experiments", "recover", "skills"]) {
      expect(visible).toContain(name);
    }
    // Group placement: business for the metric/recover trio, skills
    // for the catalog command. Drift here means /help renders them
    // under the wrong header.
    expect(reg.get("review")?.group).toBe("business");
    expect(reg.get("experiments")?.group).toBe("business");
    expect(reg.get("recover")?.group).toBe("business");
    expect(reg.get("skills")?.group).toBe("skills");
  });

  it("/recover exposes a 'list' subcommand", () => {
    const recover = reg.get("recover")!;
    expect(recover.subcommands).toBeDefined();
    expect(recover.subcommands?.list?.name).toBe("list");
    expect(recover.subcommands?.list?.streamSafe).toBe(true);
  });

  it("/skills exposes a 'list' subcommand", () => {
    const skills = reg.get("skills")!;
    expect(skills.subcommands).toBeDefined();
    expect(skills.subcommands?.list?.name).toBe("list");
    expect(skills.subcommands?.list?.streamSafe).toBe(true);
  });

  it("/recover bare-root is NOT stream-safe (real cluster mutation)", () => {
    // Defensive — a future refactor flipping this flag would let the
    // recovery hit the cluster mid-stream while another turn is in
    // flight. Pin the safety property here.
    expect(reg.get("recover")?.streamSafe).not.toBe(true);
  });

  it("/review parses 'E1' arg through parseSlashCommand", () => {
    const r = parseSlashCommand("/review E1", reg);
    expect(r?.root).toBe("review");
    expect(r?.sub).toBe("");
    expect(r?.args).toEqual(["E1"]);
  });

  it("parseSlashCommand routes '/recover list' to the sub", () => {
    const r = parseSlashCommand("/recover list", reg);
    expect(r?.root).toBe("recover");
    expect(r?.sub).toBe("list");
    expect(r?.args).toEqual([]);
  });

  it("parseSlashCommand routes '/recover task-abc' to bare-root", () => {
    const r = parseSlashCommand("/recover task-abc", reg);
    expect(r?.root).toBe("recover");
    expect(r?.sub).toBe("");
    expect(r?.args).toEqual(["task-abc"]);
  });

  it("parseSlashCommand routes '/skills list' to the sub", () => {
    const r = parseSlashCommand("/skills list", reg);
    expect(r?.root).toBe("skills");
    expect(r?.sub).toBe("list");
  });

  it("/tasks accepts the [active|failed|all] [N] usage shape", () => {
    const tasks = reg.get("tasks")!;
    expect(tasks.usage).toContain("active");
    expect(tasks.usage).toContain("failed");
    expect(tasks.usage).toContain("all");
    expect(tasks.streamSafe).toBe(true);
  });
});

describe("Phase 3a / /config /memory /compact registration", () => {
  const reg = buildRegistry();

  it("registers all three commands as visible", () => {
    const visible = reg.list().map((c) => c.name);
    for (const name of ["config", "memory", "compact"]) {
      expect(visible).toContain(name);
    }
    expect(reg.get("config")?.group).toBe("skills");
    expect(reg.get("memory")?.group).toBe("skills");
    expect(reg.get("compact")?.group).toBe("business");
  });

  it("/config exposes list/get/set/unset/path subs", () => {
    const cfg = reg.get("config")!;
    const subs = cfg.subcommands ?? {};
    for (const sub of ["list", "get", "set", "unset", "path"]) {
      expect(subs[sub]).toBeDefined();
    }
  });

  it("/config write subs are NOT stream-safe; read subs ARE", () => {
    // Defence-in-depth: settings.reload() in the middle of a stream
    // could yank state from the in-flight turn. Pin the safety
    // contract so a future "let's just mark them all safe" refactor
    // breaks loud here instead of in production.
    const cfg = reg.get("config")!;
    expect(cfg.subcommands?.list?.streamSafe).toBe(true);
    expect(cfg.subcommands?.get?.streamSafe).toBe(true);
    expect(cfg.subcommands?.path?.streamSafe).toBe(true);
    expect(cfg.subcommands?.set?.streamSafe).toBeUndefined();
    expect(cfg.subcommands?.unset?.streamSafe).toBeUndefined();
  });

  it("/memory exposes show/clear/path subs", () => {
    const mem = reg.get("memory")!;
    const subs = mem.subcommands ?? {};
    for (const sub of ["show", "clear", "path"]) {
      expect(subs[sub]).toBeDefined();
    }
  });

  it("/memory clear is NOT stream-safe (deletes the live session file)", () => {
    const mem = reg.get("memory")!;
    expect(mem.subcommands?.show?.streamSafe).toBe(true);
    expect(mem.subcommands?.path?.streamSafe).toBe(true);
    expect(mem.subcommands?.clear?.streamSafe).toBeUndefined();
  });

  it("/compact bare-root is NOT stream-safe (mutates the active checkpoint)", () => {
    // Compaction emits RemoveMessage tombstones into the same
    // LangGraph thread the in-flight turn is reading from. Pin the
    // gate so this stays unsafe forever.
    expect(reg.get("compact")?.streamSafe).not.toBe(true);
  });

  it("parseSlashCommand routes /config get / set / unset to the sub", () => {
    expect(parseSlashCommand("/config get model_name", reg)?.sub).toBe("get");
    expect(
      parseSlashCommand("/config set timeout_kubectl 45", reg)?.sub,
    ).toBe("set");
    expect(parseSlashCommand("/config unset model_name", reg)?.sub).toBe(
      "unset",
    );
  });

  it("parseSlashCommand /config set carries the value-with-spaces in args", () => {
    // ``/config set api_base_url https://foo/v1`` — args[1..] joined
    // back lets the value preserve spaces / colons. Lock the parser
    // contract so the handler's join logic stays valid.
    const r = parseSlashCommand("/config set api_base_url https://foo/v1", reg);
    expect(r?.sub).toBe("set");
    expect(r?.args).toEqual(["api_base_url", "https://foo/v1"]);
  });
});

describe("Phase 3b / /skills show/reload/install/enable/disable", () => {
  const reg = buildRegistry();

  it("registers list/show/reload/install/enable/disable subs", () => {
    const skills = reg.get("skills")!;
    const subs = skills.subcommands ?? {};
    for (const name of [
      "list",
      "show",
      "reload",
      "install",
      "enable",
      "disable",
    ]) {
      expect(subs[name]).toBeDefined();
    }
  });

  it("read subs (list, show) are stream-safe; mutating subs are NOT", () => {
    // Defence-in-depth: re-scanning the skills directory or flipping
    // disabled_skills mid-stream could yank state from the in-flight
    // turn. Pin each gate so a future "let's relax this" refactor
    // breaks loud here instead of silently shipping unsafe behaviour.
    const subs = reg.get("skills")!.subcommands!;
    expect(subs.list?.streamSafe).toBe(true);
    expect(subs.show?.streamSafe).toBe(true);
    expect(subs.reload?.streamSafe).toBeUndefined();
    expect(subs.install?.streamSafe).toBeUndefined();
    expect(subs.enable?.streamSafe).toBeUndefined();
    expect(subs.disable?.streamSafe).toBeUndefined();
  });

  it("parseSlashCommand routes each new sub correctly", () => {
    expect(parseSlashCommand("/skills show node-cpu", reg)?.sub).toBe("show");
    expect(parseSlashCommand("/skills reload", reg)?.sub).toBe("reload");
    // install/enable/disable carry args after the sub.
    const inst = parseSlashCommand(
      "/skills install https://github.com/foo/skills.git",
      reg,
    );
    expect(inst?.sub).toBe("install");
    expect(inst?.args).toEqual(["https://github.com/foo/skills.git"]);
    const en = parseSlashCommand("/skills enable my-skill", reg);
    expect(en?.sub).toBe("enable");
    expect(en?.args).toEqual(["my-skill"]);
    const dis = parseSlashCommand("/skills disable my-skill", reg);
    expect(dis?.sub).toBe("disable");
    expect(dis?.args).toEqual(["my-skill"]);
  });

  it("/skills install joins multi-token sources back together", () => {
    // ``/skills install /path with spaces/skills`` — handler joins
    // args back. Pin the parser tokenisation so a regression in the
    // join logic shows up here.
    const r = parseSlashCommand(
      "/skills install /tmp/my skills/dir",
      reg,
    );
    expect(r?.sub).toBe("install");
    expect(r?.args).toEqual(["/tmp/my", "skills/dir"]);
  });
});

describe("Phase 3c.1 / /model list + set", () => {
  const reg = buildRegistry();

  it("registers /model with list + set subs in the skills group", () => {
    const model = reg.get("model");
    expect(model).toBeDefined();
    expect(model?.group).toBe("skills");
    const subs = model?.subcommands ?? {};
    expect(subs.list).toBeDefined();
    expect(subs.set).toBeDefined();
  });

  it("/model list is stream-safe; /model set is NOT", () => {
    // ``/model set`` triggers a server-side ConfigStore.set which
    // calls settings.reload() under the lock. Mid-stream that could
    // yank state from the in-flight turn (mostly harmless because
    // model_name is cold, but we don't rely on that — pin the gate).
    const subs = reg.get("model")?.subcommands ?? {};
    expect(subs.list?.streamSafe).toBe(true);
    expect(subs.set?.streamSafe).toBeUndefined();
  });

  it("parseSlashCommand routes /model list + /model set", () => {
    expect(parseSlashCommand("/model list", reg)?.sub).toBe("list");
    const setR = parseSlashCommand("/model set qwen-max", reg);
    expect(setR?.sub).toBe("set");
    expect(setR?.args).toEqual(["qwen-max"]);
  });

  it("/model bare (no sub) routes to the bare-root handler", () => {
    // ``/model`` with no sub → bare handler shows usage. Pin the
    // contract so a parser regression that swallows ``model`` as
    // sub of itself fails here.
    const r = parseSlashCommand("/model", reg);
    expect(r?.root).toBe("model");
    expect(r?.sub).toBe("");
  });
});

describe("Phase 3 conformance / stream-safe matrix lock", () => {
  // Exhaustive table of every Phase 3 command + sub paired with its
  // expected stream-safe classification. The pattern is "read = safe,
  // write = unsafe" — a regression that flips a write to safe (or
  // vice versa) breaks here loudly instead of in production where the
  // user would silently race a settings.reload() against an in-flight
  // turn.
  //
  // Adding a new command/sub: extend this table at the same time as
  // ``buildRegistry``. The test fails if either side drifts.
  const reg = buildRegistry();
  type Spec = { root: string; sub: string; safe: boolean };
  const matrix: Spec[] = [
    // /config — read safe, write unsafe.
    { root: "config", sub: "list", safe: true },
    { root: "config", sub: "get", safe: true },
    { root: "config", sub: "set", safe: false },
    { root: "config", sub: "unset", safe: false },
    { root: "config", sub: "path", safe: true },
    // /memory — read safe, clear unsafe (deletes session file).
    { root: "memory", sub: "show", safe: true },
    { root: "memory", sub: "clear", safe: false },
    { root: "memory", sub: "path", safe: true },
    // /skills — read safe (list/show/path), mutate unsafe (rest).
    { root: "skills", sub: "list", safe: true },
    { root: "skills", sub: "show", safe: true },
    { root: "skills", sub: "reload", safe: false },
    { root: "skills", sub: "install", safe: false },
    { root: "skills", sub: "enable", safe: false },
    { root: "skills", sub: "disable", safe: false },
    { root: "skills", sub: "path", safe: true },
    // /model — read safe, write unsafe.
    { root: "model", sub: "list", safe: true },
    { root: "model", sub: "set", safe: false },
    // /recordings — both subs safe (list reads server, export reads
    // server + writes local FS; the server isn't mutated).
    { root: "recordings", sub: "list", safe: true },
    { root: "recordings", sub: "export", safe: true },
  ];

  for (const { root, sub, safe } of matrix) {
    it(`/${root} ${sub} streamSafe=${safe}`, () => {
      const cmd = reg.get(root);
      expect(cmd, `/${root} should be registered`).toBeDefined();
      const subSpec = cmd?.subcommands?.[sub];
      expect(subSpec, `/${root} ${sub} should be a sub`).toBeDefined();
      // Lock the literal value: ``true`` for safe, ``undefined`` for
      // unsafe (the SlashSubcommand contract: omit the field to fail
      // closed at the gate).
      if (safe) {
        expect(subSpec?.streamSafe).toBe(true);
      } else {
        expect(subSpec?.streamSafe).toBeUndefined();
      }
    });
  }

  // Bare-root unsafe-by-default commands. Each of these calls into a
  // mutating server endpoint; the registry must NOT mark them
  // streamSafe at the root level.
  for (const root of ["compact", "plan"]) {
    it(`/${root} bare-root is NOT stream-safe`, () => {
      const cmd = reg.get(root);
      expect(cmd, `/${root} should be registered`).toBeDefined();
      expect(cmd?.streamSafe).not.toBe(true);
    });
  }
});

describe("Phase 3c.2 / /plan dry-run registration", () => {
  const reg = buildRegistry();

  it("registers /plan in the business group with dispatchesOwnTurn", () => {
    // Dispatching its own turn matters: ``submitTurn(nl, {dryRun:true})``
    // already pushes a real ``TURN_STARTED`` with the unwrapped NL.
    // Without the flag the user would see ``/plan inject cpu`` AND
    // ``inject cpu`` echoed back-to-back. Pin the flag so a refactor
    // that drops it surfaces here.
    const plan = reg.get("plan");
    expect(plan).toBeDefined();
    expect(plan?.group).toBe("business");
    expect(plan?.dispatchesOwnTurn).toBe(true);
  });

  it("/plan is NOT stream-safe (handler refuses mid-stream)", () => {
    // ``streamSafe`` falsy at the gate means Composer rejects /plan
    // when streamState !== "idle". The handler ALSO checks streamState
    // for defence-in-depth; the smoke covers that path.
    expect(reg.get("plan")?.streamSafe).not.toBe(true);
  });

  it("parseSlashCommand carries the verbatim NL in rawArgs", () => {
    // The handler joins args[] to rebuild the NL — but rawArgs is
    // also available for any future shift to verbatim-preserving
    // input. Lock the parser contract.
    const r = parseSlashCommand("/plan inject cpu fault on node-1", reg);
    expect(r?.root).toBe("plan");
    expect(r?.sub).toBe("");
    expect(r?.args).toEqual(["inject", "cpu", "fault", "on", "node-1"]);
    expect(r?.rawArgs).toBe("inject cpu fault on node-1");
  });
});
