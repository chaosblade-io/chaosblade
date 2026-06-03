/**
 * Headless smoke for the M4 slash command surface.
 *
 *   - SlashCommandRegistry.filter(prefix) finds expected commands.
 *   - parseSlashLine handles edge cases (empty, only-slash, args).
 *   - client.listTasks() round-trips the /api/v1/metric envelope.
 *   - The /help command produces a LogItem with bold spans.
 *
 * Run with:
 *   BLADE_AI_SERVER=http://127.0.0.1:PORT node scripts/smoke-slash.mjs
 */

import { tsImport } from "tsx/esm/api";

// === Hang diagnostics + watchdog =================================
//
// CI history: this script silently hangs on GitHub-hosted Linux
// runners (Node 22) after smoke-i18n.mjs prints success. Local runs
// (macOS Node 24, with or without LC_ALL/non-TTY stdout) finish in
// ~1s — root cause not yet localized. Until it is, the script:
//
//   (a) installs a global watchdog: process.exit(1) after
//       ``SMOKE_SLASH_TIMEOUT_MS`` so CI fails fast instead of
//       waiting hours, and the stderr line tells you the last
//       probe label that fired (i.e. the closest hint where the
//       hang occurred);
//   (b) emits a ``[probe] <label>`` line to stderr at every
//       tsImport boundary and major test-block boundary, so the
//       CI log shows exactly where execution stopped.
//
// Both blocks are diagnostic-only and can be deleted once the
// underlying CI hang is fixed.
const PROBE_TIMEOUT_MS = Number(
  process.env.SMOKE_SLASH_TIMEOUT_MS ?? 120_000,
);
let lastProbe = "<entry>";
function probe(label) {
  lastProbe = label;
  process.stderr.write(`[probe] ${label}\n`);
}
const watchdog = setTimeout(() => {
  process.stderr.write(
    `\n[probe] WATCHDOG ${PROBE_TIMEOUT_MS}ms elapsed without natural exit.\n` +
      `[probe] Last probe label reached: ${lastProbe}\n` +
      `[probe] Active handles: ${process._getActiveHandles?.().length ?? "?"}; ` +
      `requests: ${process._getActiveRequests?.().length ?? "?"}\n`,
  );
  process.exit(1);
}, PROBE_TIMEOUT_MS);
watchdog.unref?.();
probe("entry");

probe("tsImport state/commands.ts");
const cmdMod = await tsImport("../src/state/commands.ts", import.meta.url);
probe("tsImport state/reducer.ts");
const reducerMod = await tsImport("../src/state/reducer.ts", import.meta.url);
probe("tsImport state/types.ts");
const typesMod = await tsImport("../src/state/types.ts", import.meta.url);
probe("tsImport api/client.ts");
const clientMod = await tsImport("../src/api/client.ts", import.meta.url);
probe("tsImport utils/replay.ts");
const replayMod = await tsImport("../src/utils/replay.ts", import.meta.url);
probe("tsImport utils/errorHints.ts");
const hintsMod = await tsImport("../src/utils/errorHints.ts", import.meta.url);
probe("tsImport utils/cursorMath.ts");
const cursorMath = await tsImport("../src/utils/cursorMath.ts", import.meta.url);
probe("imports done");

const { buildRegistry, parseSlashLine } = cmdMod;
const { reducer } = reducerMod;
const { initialAppState } = typesMod;
const { BladeClient } = clientMod;
const { replayRecording, recordedEventToAction } = replayMod;
const { suggestionsForError } = hintsMod;
const { cursorToLineCol, lineColToCursor, lineStartIdx, lineEndIdx } = cursorMath;

const failures = [];
function assert(cond, msg) {
  if (!cond) failures.push(msg);
}

probe("block: parseSlashLine");
// --- parseSlashLine -----------------------------------------------
{
  const r1 = parseSlashLine("");
  assert(r1 === null, "parseSlashLine('') should be null");

  const r2 = parseSlashLine("hello");
  assert(r2 === null, "parseSlashLine('hello') should be null");

  const r3 = parseSlashLine("/");
  assert(r3 === null, "parseSlashLine('/') should be null");

  const r4 = parseSlashLine("/help");
  assert(r4 && r4.name === "help" && r4.args.length === 0, "parseSlashLine('/help')");

  const r5 = parseSlashLine("/tasks  10  inject");
  assert(r5 && r5.name === "tasks" && r5.args.length === 2 && r5.args[0] === "10" && r5.args[1] === "inject",
    `parseSlashLine('/tasks 10 inject') → ${JSON.stringify(r5)}`);

  const r6 = parseSlashLine("/HELP");
  assert(r6 && r6.name === "help", "parseSlashLine should lowercase name");
}

// --- SlashCommandRegistry -----------------------------------------
const reg = buildRegistry();
{
  const all = reg.list();
  assert(all.length >= 5, `expected at least 5 built-in commands, got ${all.length}`);

  const help = reg.get("help");
  assert(help && help.name === "help", "registry.get('help')");

  const helpAlias = reg.get("?");
  assert(helpAlias && helpAlias.name === "help", "alias '?' → help");

  const helpFiltered = reg.filter("he");
  assert(
    helpFiltered.some((c) => c.name === "help"),
    `filter('he') should include 'help', got ${helpFiltered.map((c) => c.name).join(",")}`,
  );

  const empty = reg.filter("xyz_no_match");
  assert(empty.length === 0, "filter('xyz_no_match') should be empty");
}

// --- /help handler dispatches a LogItem ---------------------------
{
  let state = { ...initialAppState };
  const dispatch = (a) => { state = reducer(state, a); };
  const help = reg.get("help");
  await help.handler(
    {
      client: null,
      sessionId: "sess-test",
      state,
      registry: reg,
      dispatch,
      exit: () => {},
    },
    [],
  );
  const logs = state.history.filter((h) => h.kind === "log");
  assert(logs.length === 1, `expected 1 log item from /help, got ${logs.length}`);
  if (logs.length === 1) {
    // Group headers are wrapped in **bold**; count them rather than
    // pinning to one English literal so the assertion survives the
    // M9 i18n + M10 changes.
    const headerCount = (logs[0].text.match(/^\*\*[^*\n]+\*\*$/gm) ?? []).length;
    // Group taxonomy switched to general/business/skills/dynamic in
    // Phase 0.1 (alignment with Python). With the current built-in
    // set we have ``general`` + ``business`` populated; ``skills``
    // and ``dynamic`` are empty until Phase 4 wires the skill
    // commands. ``renderHelp`` skips empty groups so 2 headers is
    // the correct count today; relax the floor and assert each
    // populated group is actually labelled.
    assert(headerCount >= 2,
      `/help text should have ≥2 bold group headers; got ${headerCount}`);
    assert(logs[0].text.includes("**通用**") || logs[0].text.includes("**General**"),
      "/help text should label the 'general' group");
    assert(logs[0].text.includes("**业务**") || logs[0].text.includes("**Business**"),
      "/help text should label the 'business' group");
    assert(logs[0].text.includes("/help"), "/help text should mention /help itself");
    // Bug 7 regression: ID must follow the reducer-allocated 'log-N' shape.
    assert(/^log-\d+$/.test(logs[0].id), `LogItem id should match /^log-\\d+$/, got '${logs[0].id}'`);
  }
}

// --- /retry registry presence + four behaviour branches -----------
{
  const retry = reg.get("retry");
  assert(retry, "/retry command should be registered");
  // Phase 0.1 collapsed the per-feature groups (session/tasks/history)
  // into Python's four-group taxonomy. ``/retry`` lives under
  // ``general`` now (it's a generic recovery command, not a
  // session-info inspector).
  assert(retry.group === "general", `/retry group should be 'general'; got ${retry.group}`);
}

// /retry with no prior input → warn, do not call submitTurn
{
  let state = { ...initialAppState };
  const dispatch = (a) => { state = reducer(state, a); };
  let calls = 0;
  await reg.get("retry").handler(
    {
      client: null,
      sessionId: "s",
      state,
      registry: reg,
      dispatch,
      exit: () => {},
      submitTurn: async () => { calls += 1; },
    },
    [],
  );
  const logs = state.history.filter((h) => h.kind === "log");
  assert(logs.length === 1, `/retry no-input: expected 1 log, got ${logs.length}`);
  assert(logs[0].level === "warn", "/retry no-input: log level should be warn");
  assert(calls === 0, `/retry no-input: submitTurn should not fire; got ${calls}`);
}

// /retry mid-stream → warn, do not call submitTurn
{
  let state = {
    ...initialAppState,
    lastTurnInput: "inject CPU stress 80%",
    streamState: "responding",
  };
  const dispatch = (a) => { state = reducer(state, a); };
  let calls = 0;
  await reg.get("retry").handler(
    {
      client: null,
      sessionId: "s",
      state,
      registry: reg,
      dispatch,
      exit: () => {},
      submitTurn: async () => { calls += 1; },
    },
    [],
  );
  const logs = state.history.filter((h) => h.kind === "log");
  assert(logs.length === 1, `/retry busy: expected 1 log, got ${logs.length}`);
  assert(logs[0].level === "warn", "/retry busy: log level should be warn");
  assert(calls === 0, `/retry busy: submitTurn should not fire; got ${calls}`);
}

// /retry with submitTurn unwired → warn cleanly (no crash)
{
  let state = {
    ...initialAppState,
    lastTurnInput: "inject CPU stress 80%",
  };
  const dispatch = (a) => { state = reducer(state, a); };
  await reg.get("retry").handler(
    {
      client: null,
      sessionId: "s",
      state,
      registry: reg,
      dispatch,
      exit: () => {},
      // no submitTurn
    },
    [],
  );
  const logs = state.history.filter((h) => h.kind === "log");
  assert(logs.length === 1, `/retry no-submit: expected 1 log, got ${logs.length}`);
  assert(logs[0].level === "warn", "/retry no-submit: log level should be warn");
}

// /retry happy path → resubmits exact lastTurnInput
{
  let state = {
    ...initialAppState,
    lastTurnInput: "inject CPU stress 80%",
  };
  const dispatch = (a) => { state = reducer(state, a); };
  let received = null;
  await reg.get("retry").handler(
    {
      client: null,
      sessionId: "s",
      state,
      registry: reg,
      dispatch,
      exit: () => {},
      submitTurn: async (input) => { received = input; },
    },
    [],
  );
  assert(received === "inject CPU stress 80%",
    `/retry happy: submitTurn should receive verbatim input; got ${JSON.stringify(received)}`);
  // Should also drop a "retrying" info log into history
  const logs = state.history.filter((h) => h.kind === "log");
  assert(logs.length === 1, `/retry happy: expected 1 log, got ${logs.length}`);
  assert(logs[0].level === "info", `/retry happy: log level should be info; got ${logs[0].level}`);
}

// --- TURN_STARTED captures lastTurnInput for /retry ----------------
{
  let state = { ...initialAppState };
  state = reducer(state, { type: "TURN_STARTED", input: "first turn" });
  assert(state.lastTurnInput === "first turn",
    `lastTurnInput should be 'first turn'; got ${JSON.stringify(state.lastTurnInput)}`);
  state = reducer(state, { type: "TURN_DONE" });
  assert(state.lastTurnInput === "first turn",
    "lastTurnInput should survive TURN_DONE so /retry works after error");
  state = reducer(state, { type: "TURN_STARTED", input: "second turn" });
  assert(state.lastTurnInput === "second turn",
    "TURN_STARTED should overwrite lastTurnInput");
  // Slash-echo guard: Composer dispatches TURN_STARTED for slash
  // commands too (so they appear in history). lastTurnInput must
  // ignore those — otherwise /retry sees its own slash literal.
  state = reducer(state, { type: "TURN_STARTED", input: "/retry" });
  assert(state.lastTurnInput === "second turn",
    `slash echo should NOT overwrite lastTurnInput; got ${JSON.stringify(state.lastTurnInput)}`);
  state = reducer(state, { type: "TURN_STARTED", input: "  /help" });
  assert(state.lastTurnInput === "second turn",
    "leading whitespace + slash should still be detected as echo");
  // Non-slash inputs that just contain a slash mid-string ARE NL turns.
  state = reducer(state, { type: "TURN_STARTED", input: "tell me how /retry works" });
  assert(state.lastTurnInput === "tell me how /retry works",
    "input containing /retry mid-string is NL, not a slash echo");
}

probe("block: /doctor + reachable client");
// --- /doctor handler renders diagnostics when client is reachable -
{
  let state = { ...initialAppState };
  const dispatch = (a) => { state = reducer(state, a); };
  const stubClientOk = {
    url: "http://stub:8080",
    health: async () => true,
    getSessionState: async () => ({ cluster: "kind-test", namespace: "demo" }),
    // /doctor now fans out a 4th probe; stub returns an empty
    // checks array so the handler still runs the rest of its mapping.
    getPreflight: async () => ({ checks: [], passed_count: 0, total_count: 0 }),
    getServerVersion: async () => "0.3.0-alpha.1",
  };
  const doctor = reg.get("doctor");
  assert(doctor, "/doctor command should be registered");
  await doctor.handler(
    {
      client: stubClientOk,
      sessionId: "sess-doctor",
      state,
      registry: reg,
      dispatch,
      exit: () => {},
    },
    [],
  );
  // /doctor now appends a ``runtime_doctor_card`` history item instead
  // of a flat LogItem. Verify the card carries the cluster / URL /
  // version probes the test expects.
  const docCards = state.history.filter((h) => h.kind === "runtime_doctor_card");
  assert(docCards.length === 1, `expected 1 runtime_doctor_card from /doctor, got ${docCards.length}`);
  if (docCards.length === 1) {
    const card = docCards[0];
    assert(card.reachable === true, `card.reachable should be true; got ${card.reachable}`);
    assert(card.cluster === "kind-test",
      `card.cluster should be 'kind-test'; got: ${card.cluster}`);
    assert(card.serverUrl === "http://stub:8080",
      `card.serverUrl should be 'http://stub:8080'; got: ${card.serverUrl}`);
    assert(card.serverVersion === "0.3.0-alpha.1",
      `card.serverVersion should round-trip; got: ${card.serverVersion}`);
  }
}

// --- /doctor handler renders "?" when /api/v1/version returns null --
{
  let state = { ...initialAppState };
  const dispatch = (a) => { state = reducer(state, a); };
  const stubClientNoVersion = {
    url: "http://stub:8080",
    health: async () => true,
    getSessionState: async () => ({ cluster: "kind-test", namespace: "demo" }),
    // /doctor now fans out a 4th probe; stub returns an empty
    // checks array so the handler still runs the rest of its mapping.
    getPreflight: async () => ({ checks: [], passed_count: 0, total_count: 0 }),
    getServerVersion: async () => null,  // server up but /version endpoint missing
  };
  await reg.get("doctor").handler(
    {
      client: stubClientNoVersion,
      sessionId: "sess-doctor-noversion",
      state,
      registry: reg,
      dispatch,
      exit: () => {},
    },
    [],
  );
  // Card-shaped now: the renderer is the one that turns a null
  // ``serverVersion`` into ``?``. Smoke just verifies the card stored
  // null so the renderer has the input it needs.
  const card = state.history.find((h) => h.kind === "runtime_doctor_card");
  assert(card, "/doctor should append a runtime_doctor_card");
  assert(card?.serverVersion === null,
    `/doctor card.serverVersion should be null when probe returned null; got: ${card?.serverVersion}`);
  assert(card?.reachable === true,
    `/doctor card.reachable should still be true (server up, just no /version); got: ${card?.reachable}`);
}

// --- /doctor handler degrades gracefully when server is unreachable
{
  let state = { ...initialAppState };
  const dispatch = (a) => { state = reducer(state, a); };
  const stubClientDown = {
    url: "http://nowhere:8080",
    health: async () => false,
    getSessionState: async () => { throw new Error("should not be called"); },
    getServerVersion: async () => null,
    // Server unreachable so getPreflight should never return useful
    // data; the stub matches what BladeClient does on fetch failure
    // (returns null). Handler must tolerate this shape.
    getPreflight: async () => null,
  };
  const doctor = reg.get("doctor");
  await doctor.handler(
    {
      client: stubClientDown,
      sessionId: "sess-doctor-down",
      state,
      registry: reg,
      dispatch,
      exit: () => {},
    },
    [],
  );
  const downCards = state.history.filter((h) => h.kind === "runtime_doctor_card");
  assert(downCards.length === 1, `expected 1 runtime_doctor_card from /doctor (down), got ${downCards.length}`);
  if (downCards.length === 1) {
    const card = downCards[0];
    assert(card.reachable === false,
      `card.reachable should be false when server unreachable; got: ${card.reachable}`);
    assert(card.serverUrl === "http://nowhere:8080",
      `card.serverUrl should still be set even when unreachable; got: ${card.serverUrl}`);
    // Cluster probe is skipped server-down; the card's cluster falls
    // back to empty string.
    assert(card.cluster === "",
      `card.cluster should be empty when server is down; got: ${card.cluster}`);
  }
}

// --- /clear handler clears history + bumps remount key -----------
{
  let state = {
    ...initialAppState,
    history: [
      { kind: "user", id: "u-1", text: "hi" },
      { kind: "agent", id: "a-2", text: "hi back" },
    ],
  };
  const initialKey = state.historyRemountKey;
  const dispatch = (a) => { state = reducer(state, a); };
  const clear = reg.get("clear");
  await clear.handler(
    {
      client: null,
      sessionId: "s",
      state,
      registry: reg,
      dispatch,
      exit: () => {},
    },
    [],
  );
  assert(state.history.length === 0, `clear should empty history, got ${state.history.length}`);
  // Bug 1 regression: Static must remount or the screen won't refresh.
  assert(
    state.historyRemountKey === initialKey + 1,
    `historyRemountKey should bump by 1, got ${state.historyRemountKey} (was ${initialKey})`,
  );
}

// --- /permission toggle reads ctx.state + announces new value ----
//
// Phase 1.2 (alignment with Python) split the legacy ``/mode
// auto|confirm`` into ``/permission`` (permission mode) and
// ``/mode`` (display density: calm/working/dense). This block
// pins the permission toggle's semantics + log shape on its new
// home; the ``/mode`` block below covers display density.
{
  let state = { ...initialAppState };
  state.config = { ...state.config, permissionMode: "auto" };
  const dispatch = (a) => { state = reducer(state, a); };
  const permission = reg.get("permission");
  assert(permission, "/permission command should be registered");

  // No-arg toggle: handler must observe ctx.state.config.permissionMode
  // and dispatch the *opposite* value.
  await permission.handler(
    {
      client: null,
      sessionId: "s",
      state, // pre-toggle snapshot
      registry: reg,
      dispatch,
      exit: () => {},
    },
    [],
  );
  assert(state.config.permissionMode === "confirm", "/permission toggle: auto → confirm");
  // Log line should announce the new value, not just "toggled".
  const lastLog = [...state.history].reverse().find((h) => h.kind === "log");
  assert(
    lastLog && lastLog.text.includes("**confirm**"),
    `/permission toggle log should contain '**confirm**', got: ${lastLog?.text}`,
  );

  // Explicit value with fresh snapshot.
  await permission.handler(
    {
      client: null,
      sessionId: "s",
      state,
      registry: reg,
      dispatch,
      exit: () => {},
    },
    ["auto"],
  );
  assert(state.config.permissionMode === "auto", "/permission auto explicit");

  // Unknown value should warn, not silently ignore.
  await permission.handler(
    {
      client: null,
      sessionId: "s",
      state,
      registry: reg,
      dispatch,
      exit: () => {},
    },
    ["bogus"],
  );
  const warns = state.history.filter((h) => h.kind === "log" && h.level === "warn");
  assert(warns.length >= 1, "/permission bogus should produce a warn log");
  assert(state.config.permissionMode === "auto", "/permission bogus should not change mode");
}

// --- /mode (display density) cycles + dispatches DISPLAY_MODE_CHANGED -
//
// Bare ``/mode`` cycles calm → working → dense → calm. Explicit
// subcommand (``/mode dense``) goes through the per-density sub
// handler; this block exercises both via the bare-root entry point
// since smoke is a non-React harness — we wire the sub call
// through ``cmd.subcommands[name].handler`` directly to mirror
// the Composer's dispatch path.
{
  let state = { ...initialAppState };
  // initialAppState's displayMode default is "calm" — verify we
  // start there so the cycle assertions below hold.
  assert(state.config.displayMode === "calm", "initial displayMode should be calm");
  const dispatch = (a) => { state = reducer(state, a); };
  const mode = reg.get("mode");
  assert(mode, "/mode command should be registered");

  const ctx = (snapshot) => ({
    client: null,
    sessionId: "s",
    state: snapshot,
    registry: reg,
    dispatch,
    exit: () => {},
  });

  // Bare /mode: calm → working.
  await mode.handler(ctx(state), []);
  assert(state.config.displayMode === "working", `bare /mode cycle: calm→working, got ${state.config.displayMode}`);

  // Bare /mode again: working → dense.
  await mode.handler(ctx(state), []);
  assert(state.config.displayMode === "dense", `bare /mode cycle: working→dense, got ${state.config.displayMode}`);

  // Bare /mode wraps: dense → calm.
  await mode.handler(ctx(state), []);
  assert(state.config.displayMode === "calm", `bare /mode wrap: dense→calm, got ${state.config.displayMode}`);

  // Direct sub: /mode dense.
  const denseSub = mode.subcommands?.dense;
  assert(denseSub, "/mode should have a 'dense' subcommand");
  await denseSub.handler(ctx(state), []);
  assert(state.config.displayMode === "dense", `/mode dense explicit, got ${state.config.displayMode}`);

  // Bogus arg on bare-root → warn (handler treats unrecognised
  // positional arg as a typo of the cycle entry point).
  await mode.handler(ctx(state), ["bogus"]);
  const warns = state.history.filter((h) => h.kind === "log" && h.level === "warn");
  assert(warns.length >= 1, "/mode bogus should produce a warn log");
  assert(state.config.displayMode === "dense", "/mode bogus should not change displayMode");
}

probe("block: listTasks envelope failure");
// --- listTasks throws on envelope failure (Bug 5) -----------------
{
  const realFetch = globalThis.fetch;
  // Failure envelope.
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({ status: "fail", code: "INTERNAL_ERROR", message: "boom" }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  try {
    const c = new BladeClient("http://stub");
    let threw = false;
    let msg = "";
    try {
      await c.listTasks();
    } catch (err) {
      threw = true;
      msg = err instanceof Error ? err.message : String(err);
    }
    assert(threw, "listTasks should throw on envelope status=fail");
    assert(msg.includes("boom"), `error message should surface server reason, got: ${msg}`);
  } finally {
    globalThis.fetch = realFetch;
  }
}

// --- callId concurrency: parallel same-name tools route correctly (M5.4) ---
{
  let state = { ...initialAppState };
  const dispatch = (a) => { state = reducer(state, a); };

  // Two concurrent kubectl calls with distinct call_ids.
  dispatch({ type: "TURN_STARTED", input: "test" });
  dispatch({ type: "TOOL_STARTED", callId: "uuid-A", name: "kubectl", node: "agent_loop" });
  dispatch({ type: "TOOL_STARTED", callId: "uuid-B", name: "kubectl", node: "agent_loop" });

  // Group should have both running.
  const tg = state.pending.find((p) => p.kind === "tool_group");
  assert(tg, "after two tool_starts there should be a tool_group in pending");
  assert(tg.tools.length === 2, `tool_group should hold 2 tools, got ${tg.tools.length}`);
  assert(tg.tools[0].callId === "uuid-A" && tg.tools[1].callId === "uuid-B",
    `expected callIds A,B; got ${tg.tools.map((t) => t.callId).join(",")}`);
  assert(tg.tools.every((t) => t.status === "running"), "both should be running");

  // End uuid-B first — only the matching one flips.
  dispatch({ type: "TOOL_ENDED", callId: "uuid-B", name: "kubectl", status: "success", content: "B done" });
  const tg2 = state.pending.find((p) => p.kind === "tool_group");
  const toolA = tg2.tools.find((t) => t.callId === "uuid-A");
  const toolB = tg2.tools.find((t) => t.callId === "uuid-B");
  assert(toolA.status === "running", `uuid-A should still be running, got ${toolA.status}`);
  assert(toolB.status === "success", `uuid-B should be success, got ${toolB.status}`);

  // End uuid-A. After both tools end the leading-stable flush in
  // TOOL_ENDED moves the now-stable tool_group from pending into
  // history (no running tools left → group is final → flushable).
  // Look in BOTH halves so this assertion stays valid regardless
  // of which side the flush deposits the group on; the unit-test
  // counterpart in reducer.test.ts uses the same pattern.
  dispatch({ type: "TOOL_ENDED", callId: "uuid-A", name: "kubectl", status: "success", content: "A done" });
  const tg3 = [...state.history, ...state.pending].find(
    (p) => p.kind === "tool_group",
  );
  assert(tg3, "tool_group should still exist after both ended (in history or pending)");
  assert(tg3.tools.every((t) => t.status === "success"), "both should be success after both ended");
}

// --- Phase 2: getMetric / listSkills throw on envelope failure ----
//
// Mirrors the listTasks failure-envelope test for the new client
// methods so a future refactor of the unwrap helper can't silently
// swallow ``status: "fail"`` responses on /review or /experiments.
{
  for (const [methodName, args] of [
    ["getMetric", ["task-abc"]],
    ["listSkills", []],
  ]) {
    const realFetch = globalThis.fetch;
    globalThis.fetch = async () =>
      new Response(
        JSON.stringify({ status: "fail", code: "TASK_NOT_FOUND", message: `${methodName} boom` }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    try {
      const c = new BladeClient("http://stub");
      let threw = false;
      let msg = "";
      try {
        await c[methodName](...args);
      } catch (err) {
        threw = true;
        msg = err instanceof Error ? err.message : String(err);
      }
      assert(threw, `${methodName} should throw on envelope status=fail`);
      assert(msg.includes("boom"),
        `${methodName} error should surface server reason, got: ${msg}`);
    } finally {
      globalThis.fetch = realFetch;
    }
  }
}

// --- Phase 2: recoverTask returns the full envelope (success + fail) ---
//
// Unlike listTasks/getMetric/listSkills which throw on ``status:fail``,
// recoverTask is documented to hand the entire envelope back so the
// handler can render the failure_reason card. Pin both branches.
{
  const realFetch = globalThis.fetch;
  // Success branch.
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        status: "success",
        data: { task_id: "t1", result: "recovered", blade_uid: "u1", targets: [] },
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  try {
    const c = new BladeClient("http://stub");
    const env = await c.recoverTask("t1");
    assert(env.status === "success", `recover success: status=${env.status}`);
    assert(env.data?.result === "recovered", "recover success: data.result");
  } finally {
    globalThis.fetch = realFetch;
  }
  // Fail branch — must NOT throw, the handler reads env.status.
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        status: "fail",
        code: "RECOVERY_FAILED",
        message: "verification failed",
        data: { task_id: "t1", result: "failed", error: "still injected" },
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  try {
    const c = new BladeClient("http://stub");
    const env = await c.recoverTask("t1");
    assert(env.status === "fail", `recover fail: status=${env.status}`);
    assert(env.data?.error === "still injected", "recover fail: data.error preserved");
  } finally {
    globalThis.fetch = realFetch;
  }
}

// --- Phase 2: /review E# locator path resolves through state.locators ---
//
// Drives the /review handler with a fake state where locator E1 maps
// to a result item carrying taskId="t-from-locator". A fake getMetric
// captures the taskId it was called with, so we can assert the locator
// translation actually happened (not just that the parser accepted
// the E# token).
{
  let capturedTaskId = null;
  const fakeClient = {
    listTasks: async () => ({ total: 0, tasks: [] }),
    getMetric: async (id) => {
      capturedTaskId = id;
      return { task_id: id, status: "success", phase: "completed" };
    },
  };
  let state = { ...initialAppState };
  // Seed a result item with locator E1 → taskId "t-from-locator".
  state = reducer(state, { type: "TURN_STARTED", input: "inject cpu" });
  state = reducer(state, {
    type: "RESULT_RECEIVED",
    content: '{"status":"success","summary":"ok","fault_type":"cpu","blade_uid":"u","duration_ms":1000}',
    taskId: "t-from-locator",
  });
  state = reducer(state, { type: "TURN_DONE" });
  // Sanity: locator E1 should now resolve to the result with that taskId.
  const locItem = state.locators.byId["E1"];
  assert(locItem, "E1 locator should be allocated after RESULT_RECEIVED");
  assert(locItem?.taskId === "t-from-locator",
    `E1 should map to t-from-locator, got: ${locItem?.taskId}`);

  const ctx = {
    client: fakeClient,
    sessionId: "sess-test",
    state,
    registry: reg,
    dispatch: (a) => { state = reducer(state, a); },
    exit: () => {},
  };
  const reviewCmd = reg.get("review");
  assert(reviewCmd, "/review should be registered");
  await reviewCmd.handler(ctx, ["E1"]);
  assert(capturedTaskId === "t-from-locator",
    `getMetric should be called with the locator-resolved taskId, got: ${capturedTaskId}`);
}

// --- listTasks accepts envelope status="success" (regression for envelope-status mismatch) ---
{
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        status: "success",
        data: { total: 2, tasks: [{ task_id: "x" }, { task_id: "y" }] },
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  try {
    const c = new BladeClient("http://stub");
    const result = await c.listTasks();
    assert(result.total === 2, `expected total=2, got ${result.total}`);
    assert(Array.isArray(result.tasks) && result.tasks.length === 2, "tasks array");
  } finally {
    globalThis.fetch = realFetch;
  }
}

// --- M6.1: recordedEventToAction mapping --------------------------
{
  // TokenReceived → TOKEN_APPENDED
  const tokenAction = recordedEventToAction({
    ts: "2026-05-15T17:44:05Z",
    type: "TokenReceived",
    data: { content: "hello", node: "agent_loop" },
  });
  assert(tokenAction && tokenAction.type === "TOKEN_APPENDED",
    `TokenReceived should map to TOKEN_APPENDED, got ${JSON.stringify(tokenAction)}`);
  assert(tokenAction.content === "hello", "content preserved");

  // ThinkingReceived → THINKING_APPENDED
  const thinking = recordedEventToAction({
    type: "ThinkingReceived",
    data: { content: "weighing", node: "intent" },
  });
  assert(thinking && thinking.type === "THINKING_APPENDED", "ThinkingReceived");

  // ToolStarted → TOOL_STARTED with synthesized callId
  const toolStart = recordedEventToAction({
    type: "ToolStarted",
    data: { tool_name: "kubectl", node: "agent", task_id: "t1" },
  });
  assert(toolStart && toolStart.type === "TOOL_STARTED", "ToolStarted");
  assert(toolStart.callId.includes("kubectl"), `callId should include tool name; got ${toolStart.callId}`);

  // PhaseChanged with "Starting" → NODE_STARTED
  const ns = recordedEventToAction({
    type: "PhaseChanged",
    data: { source: "intent_clarification", message: "Starting intent_clarification" },
  });
  assert(ns && ns.type === "NODE_STARTED", "PhaseChanged Starting → NODE_STARTED");

  // PhaseChanged with "Completed" → NODE_ENDED
  const ne = recordedEventToAction({
    type: "PhaseChanged",
    data: { source: "intent", message: "Completed intent" },
  });
  assert(ne && ne.type === "NODE_ENDED", "PhaseChanged Completed → NODE_ENDED");

  // Unknown / not-mapped → null
  const skipped = recordedEventToAction({ type: "ProgressUpdate", data: {} });
  assert(skipped === null, "ProgressUpdate should be skipped");

  // TaskResult → RESULT_RECEIVED with JSON-stringified envelope
  const taskRes = recordedEventToAction({
    type: "TaskResult",
    data: { task_id: "t1", data: { task_state: "completed" } },
  });
  assert(taskRes && taskRes.type === "RESULT_RECEIVED", "TaskResult");
  const parsed = JSON.parse(taskRes.content);
  assert(parsed.status === "success" && parsed.data.task_state === "completed",
    `RESULT envelope structure unexpected: ${taskRes.content}`);
}

// --- M7.2 self-check: line nav within / out of bounds -----------
// Pure-function-level reproduction of the InputPrompt boundary logic.
// Verifies: ↑ at line 0 yields the same cursor (movement is no-op,
// caller falls through). ↓ at last line is also a no-op at this
// layer — the InputPrompt handler then routes to history.
{
  const cps = Array.from("line-1\nline-2\nline-3");

  // ↑ from somewhere in the middle (line=1, col=3) → line=0, col=3.
  let cur = lineColToCursor(cps, 1, 3); // -> 10
  let lc = cursorToLineCol(cps, cur);
  assert(lc.line === 1 && lc.col === 3, `seed cursor: ${JSON.stringify(lc)}`);
  // emulate ↑
  if (lc.line > 0) {
    cur = lineColToCursor(cps, lc.line - 1, lc.col);
  }
  lc = cursorToLineCol(cps, cur);
  assert(lc.line === 0 && lc.col === 3, `↑ in middle: ${JSON.stringify(lc)}`);

  // ↑ from line 0 — InputPrompt's `if (line > 0)` skips, falling
  // through to history. We model the boundary detection here.
  cur = lineColToCursor(cps, 0, 2);
  lc = cursorToLineCol(cps, cur);
  const atTop = lc.line === 0;
  assert(atTop, "should detect first-line boundary for ↑ fall-through");

  // ↓ from line=2 (last line) — total lines = 3 (newline count + 1).
  let newlineCount = 0;
  for (const cp of cps) if (cp === "\n") newlineCount += 1;
  cur = lineColToCursor(cps, 2, 2);
  lc = cursorToLineCol(cps, cur);
  const atBottom = lc.line >= newlineCount;
  assert(atBottom, "should detect last-line boundary for ↓ fall-through");
}

// --- M9: i18n basic resolution ----------------------------------
// Verify the i18n module is well-typed and looks up keys correctly.
// We can't easily switch languages mid-process (resolution is locked
// at module load), but we can confirm:
//   - tArr returns the active dict's array
//   - t() renders {param} interpolation
//   - missing keys gracefully return the key string (visible marker)
{
  const i18n = await tsImport("../src/i18n/index.ts", import.meta.url);
  const { t, tArr, ACTIVE_LANG } = i18n;

  // ACTIVE_LANG must be one of en | zh.
  assert(["en", "zh"].includes(ACTIVE_LANG),
    `ACTIVE_LANG should be en|zh; got ${ACTIVE_LANG}`);

  // Thinking phrases array — non-empty.
  const phrases = tArr("thinking.phrases");
  assert(phrases.length >= 4, `thinking.phrases too short: ${phrases.length}`);

  // String key works.
  const placeholder = t("input.placeholder");
  assert(placeholder && placeholder.length > 0, "input.placeholder non-empty");

  // {param} interpolation.
  const replayUsage = t("replay.starting", { id: "abc", n: 5, speed: "4x" });
  assert(replayUsage.includes("abc") && replayUsage.includes("5") &&
    replayUsage.includes("4x"),
    `interpolation should fill all params: ${replayUsage}`);

  // Missing key returns the key itself.
  const missing = t("does.not.exist.in.either.dict");
  assert(missing === "does.not.exist.in.either.dict",
    `missing key should return as-is; got ${missing}`);

  // suggestionsForError must return localized label/suggestions.
  const hint = suggestionsForError("kubeconfig not found");
  assert(hint && hint.label && hint.label.length > 0,
    `cluster-unreachable hint should have a label`);
  assert(hint.suggestions.length >= 2,
    `cluster-unreachable suggestions should be > 1; got ${hint.suggestions.length}`);
}

// --- M7.2: cursorMath multi-line cursor utilities ----------------
{
  // "ab\ncd\nef" → codepoints ['a','b','\n','c','d','\n','e','f'] (length 8)
  const cps = Array.from("ab\ncd\nef");
  assert(cps.length === 8, `codepoints length: ${cps.length}`);

  // cursor=0 → line 0 col 0
  let lc = cursorToLineCol(cps, 0);
  assert(lc.line === 0 && lc.col === 0, `cursor=0 → ${JSON.stringify(lc)}`);

  // cursor=2 (just before first \n) → line 0 col 2
  lc = cursorToLineCol(cps, 2);
  assert(lc.line === 0 && lc.col === 2, `cursor=2 → ${JSON.stringify(lc)}`);

  // cursor=3 (just after first \n) → line 1 col 0
  lc = cursorToLineCol(cps, 3);
  assert(lc.line === 1 && lc.col === 0, `cursor=3 → ${JSON.stringify(lc)}`);

  // cursor=8 (end) → line 2 col 2
  lc = cursorToLineCol(cps, 8);
  assert(lc.line === 2 && lc.col === 2, `cursor=8 → ${JSON.stringify(lc)}`);

  // Inverse: (line=1, col=1) → cursor=4
  let cur = lineColToCursor(cps, 1, 1);
  assert(cur === 4, `(1,1) → ${cur}`);

  // (line=2, col=99) clamps to end of line 2 (= 8, full length)
  cur = lineColToCursor(cps, 2, 99);
  assert(cur === 8, `(2,99) clamp → ${cur}`);

  // (line=99, col=0) past EOF → end of buffer
  cur = lineColToCursor(cps, 99, 0);
  assert(cur === 8, `(99,0) → ${cur}`);

  // lineStart / lineEnd
  // cursor=4 (in middle of "cd") → line starts at 3, ends at 5
  assert(lineStartIdx(cps, 4) === 3, `lineStart 4 → 3`);
  assert(lineEndIdx(cps, 4) === 5, `lineEnd 4 → 5`);

  // Single-line: cursor=2 of "abc" → start=0, end=3
  const cps2 = Array.from("abc");
  assert(lineStartIdx(cps2, 2) === 0, `single line start`);
  assert(lineEndIdx(cps2, 2) === 3, `single line end`);

  // Emoji safety: cursor crosses a 👍 (codepoint 1) cleanly
  const cps3 = Array.from("a👍b");
  assert(cps3.length === 3, `'a👍b' codepoints = 3`);
  // (0,0) → 0; (0,1) → 1 (between 'a' and '👍'); (0,2) → 2; (0,3) → 3
  assert(lineColToCursor(cps3, 0, 2) === 2, `emoji col 2`);
}

// --- M8.1: REPLAY_STARTED → REPLAY_ENDED state machine -----------
// Reducer-level verification that the replay lifecycle correctly
// flips streamState into pseudo-busy and back, and that REPLAY_ENDED
// commits pending into history regardless of aborted flag.
{
  let state = { ...initialAppState };
  const dispatch = (a) => { state = reducer(state, a); };

  dispatch({ type: "REPLAY_STARTED", taskId: "task-xyz" });
  assert(state.streamState === "responding",
    `REPLAY_STARTED should set responding; got ${state.streamState}`);
  assert(state.thoughtSubject.includes("task-xyz"),
    `subject should mention task; got '${state.thoughtSubject}'`);
  assert(state.taskId === "task-xyz", "taskId tracked");

  // Push a couple of replay events.
  dispatch({ type: "TOKEN_APPENDED", content: "abc", node: "agent_loop" });
  dispatch({ type: "TOKEN_APPENDED", content: "def", node: "agent_loop" });
  assert(state.pending.length === 1, `pending should have 1 agent item; got ${state.pending.length}`);

  // Normal end: pending → history, streamState → idle.
  dispatch({ type: "REPLAY_ENDED", aborted: false });
  assert(state.streamState === "idle", `REPLAY_ENDED should reset to idle; got ${state.streamState}`);
  assert(state.pending.length === 0, `REPLAY_ENDED should drain pending`);
  const agent = state.history.find((h) => h.kind === "agent");
  assert(agent && agent.text === "abcdef", `agent text should be concatenated; got ${agent?.text}`);
  assert(state.thoughtSubject === "", "thoughtSubject reset");

  // Aborted end: same commit semantics — partial content still lands
  // in history (so the user sees what was replayed).
  let s2 = { ...initialAppState };
  const d2 = (a) => { s2 = reducer(s2, a); };
  d2({ type: "REPLAY_STARTED", taskId: "abort-test" });
  d2({ type: "TOKEN_APPENDED", content: "partial", node: "x" });
  d2({ type: "REPLAY_ENDED", aborted: true });
  assert(s2.streamState === "idle", "aborted replay still ends idle");
  const partial = s2.history.find((h) => h.kind === "agent");
  assert(partial && partial.text === "partial",
    `aborted replay should preserve partial agent text; got ${partial?.text}`);
}

probe("block: M7.1 replay timing+abort");
// --- M7.1: replayRecording timing + abort ------------------------
{
  // Build a synthetic recording where each event is 100ms apart.
  const baseTs = "2026-05-17T12:00:00.000Z";
  const events = [];
  for (let i = 0; i < 5; i += 1) {
    const ts = new Date(Date.parse(baseTs) + i * 100).toISOString();
    events.push({
      ts,
      type: "TokenReceived",
      data: { content: `t${i} `, node: "agent_loop" },
    });
  }

  // Instant mode: should complete in well under the 500ms wall-time
  // a real-time replay would take.
  {
    let state = { ...initialAppState };
    const dispatch = (a) => { state = reducer(state, a); };
    const t0 = Date.now();
    const stats = await replayRecording(events, dispatch, "t-instant", { speed: Infinity });
    const elapsed = Date.now() - t0;
    assert(stats.converted === 5, `instant converted: ${stats.converted}`);
    assert(stats.aborted === false, "instant not aborted");
    assert(elapsed < 100, `instant elapsed should be ~0; got ${elapsed}ms`);
  }

  // 4× speed: 5 events × 100ms / 4 = ~100ms wall-clock
  {
    let state = { ...initialAppState };
    const dispatch = (a) => { state = reducer(state, a); };
    const t0 = Date.now();
    // M8: caller wraps the replay so pending commits.
    dispatch({ type: "REPLAY_STARTED", taskId: "t-4x" });
    const stats = await replayRecording(events, dispatch, "t-4x", { speed: 4 });
    dispatch({ type: "REPLAY_ENDED", aborted: stats.aborted });
    const elapsed = Date.now() - t0;
    assert(stats.converted === 5, `4x converted: ${stats.converted}`);
    assert(elapsed >= 80 && elapsed < 250,
      `4x elapsed should be ~100ms (4 gaps × 25ms); got ${elapsed}ms`);
    // Final state: agent message holds concatenated tokens.
    const agent = state.history.find((h) => h.kind === "agent");
    assert(agent && agent.text === "t0 t1 t2 t3 t4 ",
      `concat tokens: ${agent?.text}`);
  }

  // Abort mid-replay: signal abort after 30ms; expect at most a few
  // events committed.
  {
    let state = { ...initialAppState };
    const dispatch = (a) => { state = reducer(state, a); };
    const ac = new AbortController();
    setTimeout(() => ac.abort(), 30);
    dispatch({ type: "REPLAY_STARTED", taskId: "t-abort" });
    const stats = await replayRecording(events, dispatch, "t-abort",
      { speed: 1, signal: ac.signal });
    dispatch({ type: "REPLAY_ENDED", aborted: stats.aborted });
    assert(stats.aborted === true, "abort flag should be true");
    assert(stats.converted < 5, `abort should stop early; got ${stats.converted}/5`);
    // Even on abort, the partial agent text should still land in
    // history (REPLAY_ENDED commits pending regardless of aborted).
    assert(state.streamState === "idle", "after abort, streamState should be idle");
    assert(state.pending.length === 0, "after abort + REPLAY_ENDED, pending should be empty");
  }
}

// --- M6.1: replayRecording end-to-end on synthetic events ---------
// Also a regression for the M6.1 self-check finding: replayRecording
// must NOT inject its own ``(replay) <task_id>`` user echo on top of
// whatever the Composer already wrote.
{
  let state = { ...initialAppState };
  const dispatch = (a) => { state = reducer(state, a); };

  // Simulate Composer's pre-handler echo of the slash command.
  dispatch({ type: "TURN_STARTED", input: "/replay t-test" });
  dispatch({ type: "TURN_DONE" });

  const events = [
    { ts: "1", type: "PhaseChanged", data: { source: "intent_clarification", message: "Starting intent_clarification" } },
    { ts: "2", type: "ThinkingReceived", data: { content: "thinking…", node: "intent_clarification" } },
    { ts: "3", type: "TokenReceived", data: { content: "Hello, ", node: "agent_loop" } },
    { ts: "4", type: "TokenReceived", data: { content: "world!", node: "agent_loop" } },
    { ts: "5", type: "TaskResult", data: { task_id: "t-test", data: { task_state: "completed" } } },
  ];
  // ``ts: "1"`` etc. aren't valid ISO strings; replayRecording falls
  // back to instant when Date.parse returns NaN, which is what the
  // M6 test originally relied on. M7 made the function async so we
  // await the result.
  //
  // M8 self-check: replayRecording no longer dispatches its own
  // TURN_DONE — caller is responsible. We mimic the real
  // /replay handler's lifecycle here (REPLAY_STARTED → events →
  // REPLAY_ENDED) so pending lands in history.
  dispatch({ type: "REPLAY_STARTED", taskId: "t-test" });
  const stats = await replayRecording(events, dispatch, "t-test", { speed: Infinity });
  dispatch({ type: "REPLAY_ENDED", aborted: stats.aborted });
  assert(stats.converted === 5, `expected 5 converted, got ${stats.converted}`);

  // Exactly ONE user echo — the original Composer-written one. No
  // ``(replay) t-test`` duplicate from inside replayRecording.
  const userItems = state.history.filter((h) => h.kind === "user");
  assert(userItems.length === 1, `expected exactly 1 user echo, got ${userItems.length}`);
  assert(userItems[0].text === "/replay t-test",
    `user echo should be the original slash line, got: ${userItems[0].text}`);
  assert(!userItems.some((u) => u.text.includes("(replay)")),
    `replayRecording must NOT inject its own user echo (M6.1 self-check regression)`);

  const agentItems = state.history.filter((h) => h.kind === "agent");
  const resultItems = state.history.filter((h) => h.kind === "result");
  assert(agentItems.length === 1 && agentItems[0].text === "Hello, world!",
    `agent text should be concat tokens, got: ${agentItems[0]?.text}`);
  assert(resultItems.length === 1 && resultItems[0].taskId === "t-test", "result item present");
}

// --- M8 self-check: replayRecording no longer double-commits -----
// Verify replayRecording is "events-only" and leaves pending uncommitted
// when called WITHOUT REPLAY_STARTED/ENDED wrapping. Without this
// regression, the M8 fix would silently revert.
{
  let state = { ...initialAppState };
  const dispatch = (a) => { state = reducer(state, a); };

  const events = [
    { ts: null, type: "TokenReceived", data: { content: "hi", node: "x" } },
  ];
  await replayRecording(events, dispatch, "t-self", { speed: Infinity });

  // After the call, pending should hold the 1 agent item — replayRecording
  // didn't dispatch TURN_DONE, so it was NOT committed to history.
  // streamState should still be idle (we never dispatched REPLAY_STARTED).
  assert(state.pending.length === 1, `pending should hold uncommitted item; got ${state.pending.length}`);
  assert(state.history.filter((h) => h.kind === "agent").length === 0,
    "history should NOT have the agent item until caller commits");
  assert(state.streamState === "idle", "streamState unchanged when caller doesn't wrap");
}

// --- M6.2: paste containing \\n stays in buffer (does not submit) ---
// We can't mount the InputPrompt component without ink-testing-library,
// so we exercise the reducer-side guarantee directly: TURN_STARTED with
// a multi-line input writes the raw newline to the user item.
{
  let state = { ...initialAppState };
  const dispatch = (a) => { state = reducer(state, a); };
  dispatch({ type: "TURN_STARTED", input: "line1\nline2\nline3" });
  const u = state.history.find((h) => h.kind === "user");
  assert(u && u.text === "line1\nline2\nline3", `multi-line user text preserved`);
}

// --- M6.3 + M9: errorHints keyword match (language-agnostic) ----
// After M9 the labels are localized so we can't assert exact strings
// like "INIT FAILED". Instead we verify the *pattern routing* works:
//   - each keyword family returns a non-null hint
//   - hints are distinct for distinct families (no spurious collisions)
//   - the unrelated message returns null
{
  const a = suggestionsForError("Failed to initialize agent runner: connection refused");
  assert(a !== null, "init-failed should match");
  assert(a.suggestions.length >= 1, "init-failed has at least 1 suggestion");

  const b = suggestionsForError("kubeconfig not found");
  assert(b !== null, "kubeconfig should match cluster-unreachable");

  // ``init-failed`` vs ``kubeconfig`` must route to different hints.
  // We check this by comparing labels — language doesn't matter.
  assert(a.label !== b.label,
    `init-failed and kubeconfig patterns should have distinct labels; both got '${a.label}'`);

  const c = suggestionsForError("stream interrupted");
  assert(c !== null, "stream pattern should match");
  assert(c.label !== a.label && c.label !== b.label,
    "stream pattern should be distinct from init-failed and cluster");

  const d = suggestionsForError("unknown command: /xyz");
  assert(d !== null, "command-failed should match");
  assert(d.suggestions.length >= 1, "command-failed has suggestions");

  const e = suggestionsForError("absolutely random nonsense");
  assert(e === null, "unmatched message returns null");
}

probe("block: M6.1 path traversal (last block before first stdout)");
// --- M6.1 self-check Bug 1: path traversal rejected ---------------
// Two layers of defense:
//   1. FastAPI's path matcher refuses to bind ``/recordings/{task_id}``
//      when the request URL contains a ``/`` (URL-encoded or raw)
//      inside what should be a single segment — Starlette returns
//      generic ``{"detail": "Not Found"}`` HTTP 404 before any of
//      our code runs.
//   2. ``_safe_recording_path`` rejects task_ids containing chars
//      outside ``[A-Za-z0-9_-]`` (NUL, dots, slashes that *did* slip
//      past url decoding) and any resolved path that escapes the
//      recordings directory.
// Both layers must surface as some flavor of 4xx for the client.
{
  const liveUrl = process.env.BLADE_AI_SERVER;
  if (liveUrl) {
    const c = new BladeClient(liveUrl);
    const must4xx = async (id, label) => {
      let threw = false;
      let msg = "";
      try {
        await c.getRecording(id);
      } catch (err) {
        threw = true;
        msg = err instanceof Error ? err.message : String(err);
      }
      assert(threw, `${label}: client should throw`);
      // Either FastAPI's "HTTP 404" or our envelope "not found".
      assert(/HTTP 4\d\d|not found/i.test(msg),
        `${label}: error should be 4xx-ish; got: ${msg}`);
    };
    await must4xx("../sessions/fakeid", "URL-encoded ../ path");
    await must4xx("..", "bare ..");
    await must4xx("foo\x00bar", "NUL char");
    await must4xx("not-a-real-task", "valid charset, missing file");

    // Hand-craft a bare-path-traversal raw URL to confirm FastAPI's
    // generic 404 path is what surfaces (not our handler):
    const rawResp = await fetch(
      `${liveUrl}/api/v1/recordings/..%2Fsessions%2Ffakeid`,
    );
    assert(rawResp.status === 404,
      `raw URL-encoded traversal should hit FastAPI 404; got ${rawResp.status}`);
  } else {
    console.log("(skip path-traversal test — no live server)");
  }
}

// --- M6.3: listRecordings ISO-formatted modified_at -------------
{
  const liveUrl = process.env.BLADE_AI_SERVER;
  if (liveUrl) {
    const c = new BladeClient(liveUrl);
    try {
      const data = await c.listRecordings();
      const items = (data.recordings) ?? [];
      if (items.length > 0) {
        const first = items[0];
        // Bug 3 regression: modified_at must now be an ISO string.
        assert(typeof first.modified_at === "string",
          `modified_at should be string, got ${typeof first.modified_at}: ${first.modified_at}`);
        assert(/^\d{4}-\d{2}-\d{2}T/.test(first.modified_at),
          `modified_at should be ISO-shaped, got '${first.modified_at}'`);
      }
    } catch (err) {
      assert(false, `listRecordings unexpectedly threw: ${err.message}`);
    }
  }
}

// --- Phase 2: /tasks filter dispatch through fake client ---------
//
// Smoke-level coverage for the new filter-aware /tasks handler — we
// can't run vitest with a network mock easily, so instead we drive
// the registry handler with a stub client and assert the LogItem
// the handler appends contains the filter token. Catches regressions
// where parseTasksArgs silently routes ``failed`` to the wrong slot.
{
  const fakeData = {
    total: 3,
    tasks: [
      { task_id: "t1", phase: "completed", status: "success", gmt_create: "2026-05-19T10:00:00" },
      { task_id: "t2", phase: "executing", status: "running", gmt_create: "2026-05-19T10:01:00" },
      { task_id: "t3", phase: "executing", status: "failed", gmt_create: "2026-05-19T10:02:00" },
    ],
  };
  const fakeClient = { listTasks: async () => fakeData };
  let state = { ...initialAppState };
  const dispatch = (a) => { state = reducer(state, a); };
  const ctx = {
    client: fakeClient,
    sessionId: "sess-test",
    state,
    registry: reg,
    dispatch,
    exit: () => {},
  };

  const tasksCmd = reg.get("tasks");
  assert(tasksCmd, "/tasks should be registered");

  // active filter → only t2 (executing + non-failed)
  await tasksCmd.handler(ctx, ["active"]);
  const lastLog = state.history.findLast?.((it) => it.kind === "log") ||
    [...state.history].reverse().find((it) => it.kind === "log") ||
    [...state.pending].reverse().find((it) => it.kind === "log");
  assert(lastLog, "tasks handler should append a LogItem");
  if (lastLog) {
    assert(lastLog.text.includes("active"),
      `tasks-active LogItem should mention filter, got: ${lastLog.text.slice(0, 80)}`);
    assert(lastLog.text.includes("t2"),
      `tasks-active should show t2, got: ${lastLog.text.slice(0, 200)}`);
    assert(!lastLog.text.includes("t1") || lastLog.text.indexOf("t2") < lastLog.text.indexOf("t1"),
      `tasks-active should NOT include t1 in the row body before t2`);
  }
}

// --- Phase 2: /recover list filter through fake client ----------
{
  const fakeData = {
    total: 2,
    tasks: [
      { task_id: "t-running", phase: "executing", status: "injected", gmt_create: "2026-05-19T10:00:00" },
      { task_id: "t-done", phase: "completed", status: "success", gmt_create: "2026-05-19T09:00:00" },
    ],
  };
  const fakeClient = { listTasks: async () => fakeData };
  let state = { ...initialAppState };
  const dispatch = (a) => { state = reducer(state, a); };
  const ctx = {
    client: fakeClient,
    sessionId: "sess-test",
    state,
    registry: reg,
    dispatch,
    exit: () => {},
  };

  const recoverCmd = reg.get("recover");
  assert(recoverCmd?.subcommands?.list, "/recover list sub should exist");
  await recoverCmd.subcommands.list.handler(ctx, []);

  const lastLog = [...state.history, ...state.pending]
    .reverse()
    .find((it) => it.kind === "log");
  assert(lastLog, "/recover list should append a LogItem");
  if (lastLog) {
    assert(lastLog.text.includes("t-running"),
      `/recover list should include t-running (injected), got: ${lastLog.text.slice(0, 200)}`);
    assert(!lastLog.text.includes("t-done"),
      `/recover list should NOT include t-done (success), got: ${lastLog.text.slice(0, 200)}`);
  }
}

// --- Phase 3c.2: /plan handler calls submitTurn with dryRun=true ---
//
// Most important regression to lock down: a refactor that drops the
// dryRun flag would silently make /plan inject for real. We can't
// catch that with a unit test (handler dispatch needs a wired ctx),
// so the smoke verifies it via a fake submitTurn that captures
// what the handler actually passed.
{
  const captured = [];
  let state = { ...initialAppState };
  const dispatch = (a) => { state = reducer(state, a); };
  const ctx = {
    client: null,
    sessionId: "sess-test",
    state,
    registry: reg,
    dispatch,
    exit: () => {},
    beginReplay: () => new AbortController(),
    submitTurn: async (input, opts) => {
      captured.push({ input, opts });
    },
  };
  const plan = reg.get("plan");
  assert(plan, "/plan should be registered");
  await plan.handler(ctx, ["inject", "cpu", "fault", "on", "node-1"]);
  assert(captured.length === 1, `expected 1 submitTurn call, got ${captured.length}`);
  assert(captured[0].input === "inject cpu fault on node-1",
    `submitTurn input should be the joined NL, got: ${captured[0].input}`);
  assert(captured[0].opts?.dryRun === true,
    `/plan MUST pass dryRun=true, got: ${JSON.stringify(captured[0].opts)}`);
}

// --- Phase 3c.2: /plan refuses mid-stream + empty-NL warns -------
{
  // Mid-stream: handler must NOT call submitTurn.
  let called = 0;
  let state = { ...initialAppState };
  state = reducer(state, { type: "TURN_STARTED", input: "test" });
  // streamState is now "responding".
  const ctx = {
    client: null,
    sessionId: "sess-test",
    state,
    registry: reg,
    dispatch: (a) => { state = reducer(state, a); },
    exit: () => {},
    beginReplay: () => new AbortController(),
    submitTurn: async () => { called++; },
  };
  await reg.get("plan").handler(ctx, ["inject", "cpu"]);
  assert(called === 0, "/plan must NOT call submitTurn while streaming");
  const lastWarn = [...state.history, ...state.pending]
    .reverse().find((it) => it.kind === "log" && it.level === "warn");
  assert(lastWarn, "/plan during stream must produce a warn LogItem");

  // Empty NL: handler must NOT call submitTurn.
  let called2 = 0;
  let state2 = { ...initialAppState };
  const ctx2 = {
    client: null,
    sessionId: "sess-test",
    state: state2,
    registry: reg,
    dispatch: (a) => { state2 = reducer(state2, a); },
    exit: () => {},
    beginReplay: () => new AbortController(),
    submitTurn: async () => { called2++; },
  };
  await reg.get("plan").handler(ctx2, []);
  assert(called2 === 0, "/plan with empty NL must NOT call submitTurn");
}

// --- Phase 3c.2: streamTurn carries dry_run in body when truthy ---
//
// Verifies the wire shape end-to-end: when the caller passes
// ``dry_run: true`` to streamTurn, the body that hits the server
// includes the field. When false / omitted, the field is dropped
// from the body so legacy ``/run`` stays byte-identical.
{
  for (const dryRun of [true, false, undefined]) {
    const realFetch = globalThis.fetch;
    let capturedBody = null;
    globalThis.fetch = async (_url, init) => {
      capturedBody = init?.body;
      // streamTurn iterates the response body; return an empty SSE
      // stream so the for-await terminates immediately.
      return new Response("", { status: 200 });
    };
    try {
      const c = new BladeClient("http://stub");
      const iter = c.streamTurn(
        "sess-1",
        { input: "test", dry_run: dryRun },
      );
      // Consume the (empty) iterator.
      for await (const _ of iter) { void _; }
      const parsed = JSON.parse(capturedBody ?? "{}");
      if (dryRun === true) {
        assert(parsed.dry_run === true,
          `dry_run=true should land on the wire, got: ${JSON.stringify(parsed)}`);
      } else {
        assert(parsed.dry_run === undefined,
          `dry_run=${dryRun} should be omitted from wire body, got: ${JSON.stringify(parsed)}`);
      }
    } finally {
      globalThis.fetch = realFetch;
    }
  }
}

// --- Phase 3c.1: getModel / setModel throw on envelope fail -------
{
  const cases = [
    ["getModel", []],
    ["setModel", ["qwen-test"]],
  ];
  for (const [methodName, args] of cases) {
    const realFetch = globalThis.fetch;
    globalThis.fetch = async () =>
      new Response(
        JSON.stringify({
          status: "fail",
          code: 1002,
          message: `${methodName} boom`,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    try {
      const c = new BladeClient("http://stub");
      let threw = false;
      let msg = "";
      try {
        await c[methodName](...args);
      } catch (err) {
        threw = true;
        msg = err instanceof Error ? err.message : String(err);
      }
      assert(threw, `${methodName} must throw on status=fail`);
      assert(msg.includes("boom"),
        `${methodName} should surface server message, got: ${msg}`);
    } finally {
      globalThis.fetch = realFetch;
    }
  }
}

// --- Phase 3c.1: setModel sends model_name in JSON body -----------
{
  const realFetch = globalThis.fetch;
  let captured = null;
  globalThis.fetch = async (url, init) => {
    captured = { url, method: init?.method, body: init?.body };
    return new Response(
      JSON.stringify({
        status: "success",
        data: { active: "qwen-test", restart_required: true },
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };
  try {
    const c = new BladeClient("http://stub");
    const data = await c.setModel("qwen-test");
    assert(captured?.url === "http://stub/api/v1/model",
      `URL should be /api/v1/model, got: ${captured?.url}`);
    assert(captured?.method === "POST", `method=POST expected, got: ${captured?.method}`);
    assert(captured?.body && JSON.parse(captured.body).model_name === "qwen-test",
      `body should carry model_name, got: ${captured?.body}`);
    assert(data.restart_required === true,
      "data.restart_required should round-trip");
  } finally {
    globalThis.fetch = realFetch;
  }
}

// --- Phase 3b: showSkill / reloadSkills / installSkill / enableSkill /
//               disableSkill throw on envelope fail ---
{
  const cases = [
    ["showSkill", ["node-cpu"]],
    ["reloadSkills", []],
    ["installSkill", ["/tmp/foo"]],
    ["enableSkill", ["my-skill"]],
    ["disableSkill", ["my-skill"]],
  ];
  for (const [methodName, args] of cases) {
    const realFetch = globalThis.fetch;
    globalThis.fetch = async () =>
      new Response(
        JSON.stringify({
          status: "fail",
          code: 1002,
          message: `${methodName} boom`,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    try {
      const c = new BladeClient("http://stub");
      let threw = false;
      let msg = "";
      try {
        await c[methodName](...args);
      } catch (err) {
        threw = true;
        msg = err instanceof Error ? err.message : String(err);
      }
      assert(threw, `${methodName} must throw on status=fail`);
      assert(msg.includes("boom"),
        `${methodName} should surface server message, got: ${msg}`);
    } finally {
      globalThis.fetch = realFetch;
    }
  }
}

// --- Phase 3b: skill admin URLs encode the name in the path -------
//
// Capture the wire shape so a future refactor that drops
// ``encodeURIComponent`` lets weird names through to the server in
// raw form. Server-side validation would catch them but we want the
// regression to fail HERE first.
{
  const realFetch = globalThis.fetch;
  let captured = null;
  globalThis.fetch = async (url, init) => {
    captured = { url, method: init?.method };
    return new Response(
      JSON.stringify({ status: "success", data: { name: "x", was_disabled: true, disabled_skills: [] } }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };
  try {
    const c = new BladeClient("http://stub");
    await c.enableSkill("node-cpu");
    assert(captured?.url === "http://stub/api/v1/skills/node-cpu/enable",
      `enable URL should encode name, got: ${captured?.url}`);
    assert(captured?.method === "POST", `method=POST expected, got: ${captured?.method}`);
    await c.disableSkill("node-cpu");
    assert(captured?.url === "http://stub/api/v1/skills/node-cpu/disable",
      `disable URL should encode name, got: ${captured?.url}`);
    await c.showSkill("node-cpu");
    assert(captured?.url === "http://stub/api/v1/skills/node-cpu",
      `show URL should encode name, got: ${captured?.url}`);
  } finally {
    globalThis.fetch = realFetch;
  }
}

// --- Phase 3a: getConfig / setConfig / unsetConfig / getMemoryInfo /
//               clearMemory / compactSession throw on envelope fail ---
//
// Same fail-envelope contract as listTasks/getMetric/listSkills —
// each new method must throw with the server's message preserved so
// the slash handlers can render an actionable warning.
{
  const cases = [
    ["getConfig", []],
    ["setConfig", ["model_name", "qwen-test"]],
    ["unsetConfig", ["model_name"]],
    ["getMemoryInfo", ["sess-1"]],
    ["clearMemory", ["sess-1"]],
    ["compactSession", ["sess-1"]],
  ];
  for (const [methodName, args] of cases) {
    const realFetch = globalThis.fetch;
    globalThis.fetch = async () =>
      new Response(
        JSON.stringify({
          status: "fail",
          code: 1002,
          message: `${methodName} boom`,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    try {
      const c = new BladeClient("http://stub");
      let threw = false;
      let msg = "";
      try {
        await c[methodName](...args);
      } catch (err) {
        threw = true;
        msg = err instanceof Error ? err.message : String(err);
      }
      assert(threw, `${methodName} must throw on status=fail`);
      assert(msg.includes("boom"),
        `${methodName} should surface server message, got: ${msg}`);
    } finally {
      globalThis.fetch = realFetch;
    }
  }
}

// --- Phase 3a: /config set wires through to the right URL ---------
//
// Captures the URL + body the client sends so a future refactor of
// ``setConfig`` that drops the ``encodeURIComponent(key)`` or
// switches POST to PATCH fails here loudly. The whitelist itself
// is server-side; we only verify the wire shape.
{
  const realFetch = globalThis.fetch;
  let captured = null;
  globalThis.fetch = async (url, init) => {
    captured = { url, method: init?.method, body: init?.body };
    return new Response(
      JSON.stringify({
        status: "success",
        data: { key: "model_name", value: "qwen-test", hot_reload: true },
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };
  try {
    const c = new BladeClient("http://stub");
    const data = await c.setConfig("model_name", "qwen-test");
    assert(captured?.url === "http://stub/api/v1/config/model_name",
      `URL should encode key in path, got: ${captured?.url}`);
    assert(captured?.method === "POST", `method=POST expected, got: ${captured?.method}`);
    assert(captured?.body && JSON.parse(captured.body).value === "qwen-test",
      `body should carry value, got: ${captured?.body}`);
    assert(data.hot_reload === true, "data.hot_reload should round-trip");
  } finally {
    globalThis.fetch = realFetch;
  }
}

// --- Phase 3a: /compact handler refuses mid-stream (stream-safe gate) ---
//
// /compact rewrites the same LangGraph thread the active turn reads
// from. Composer's gate already refuses, but the handler also has a
// defensive check; pin it here so a refactor that strips the gate
// still won't let the handler issue a request mid-stream.
{
  let captured = null;
  const fakeClient = {
    compactSession: async (sid) => {
      captured = sid;
      return { tokens_before: 100, tokens_after: 50, tokens_saved: 50, compacted: true, layer: "lightweight" };
    },
  };
  let state = { ...initialAppState };
  // Simulate a turn in flight — streamState moves out of "idle".
  state = reducer(state, { type: "TURN_STARTED", input: "test" });
  // streamState is "responding" now (TURN_STARTED sets it).
  assert(state.streamState !== "idle",
    `precondition: streamState should not be idle, got: ${state.streamState}`);

  const ctx = {
    client: fakeClient,
    sessionId: "sess-test",
    state,
    registry: reg,
    dispatch: (a) => { state = reducer(state, a); },
    exit: () => {},
  };
  const compact = reg.get("compact");
  assert(compact, "/compact should be registered");
  await compact.handler(ctx, []);
  assert(captured === null,
    "/compact handler must NOT call the API while streaming");
  const lastWarn = [...state.history, ...state.pending]
    .reverse().find((it) => it.kind === "log" && it.level === "warn");
  assert(lastWarn, "/compact during stream must produce a warn LogItem");
}

// --- Phase 2: /recover latest translates to state.lastTaskId -------
//
// Mirrors Python ``_cmd_recover``'s ``if task_id == "latest": task_id =
// last_task_id``. Drives the bare-recover handler with a fake state
// where lastTaskId is set, captures the id passed to recoverTask, and
// asserts the keyword was unwrapped to the real id BEFORE the call
// fired (the API never sees the literal string "latest").
{
  let captured = null;
  const fakeClient = {
    listTasks: async () => ({ total: 0, tasks: [] }),
    recoverTask: async (id) => {
      captured = id;
      return { status: "success", data: { task_id: id, result: "recovered" } };
    },
  };
  let state = { ...initialAppState };
  // Seed lastTaskId via a real RESULT_RECEIVED so the wiring + payload
  // shape match production. Catches a regression where lastTaskId is
  // populated only by some side-channel that the e2e flow doesn't
  // reach.
  state = reducer(state, { type: "TURN_STARTED", input: "inject cpu" });
  state = reducer(state, {
    type: "RESULT_RECEIVED",
    content: JSON.stringify({
      status: "success",
      data: { task_id: "task-from-state", task_state: "injected" },
    }),
    taskId: "task-from-state",
  });
  state = reducer(state, { type: "TURN_DONE" });
  assert(state.lastTaskId === "task-from-state",
    `seeding step: lastTaskId should be 'task-from-state', got: ${state.lastTaskId}`);

  const ctx = {
    client: fakeClient,
    sessionId: "sess-test",
    state,
    registry: reg,
    dispatch: (a) => { state = reducer(state, a); },
    exit: () => {},
  };
  const recoverCmd = reg.get("recover");
  await recoverCmd.handler(ctx, ["latest"]);
  assert(captured === "task-from-state",
    `/recover latest should call recoverTask with state.lastTaskId, got: ${captured}`);

  // Also lock the empty-state path: when no task has finished yet,
  // /recover latest must NOT call the API and must surface the
  // recover.no_latest message.
  let captured2 = null;
  const fakeClient2 = {
    recoverTask: async (id) => { captured2 = id; return { status: "success", data: {} }; },
  };
  let state2 = { ...initialAppState };
  const ctx2 = {
    client: fakeClient2,
    sessionId: "sess-test",
    state: state2,
    registry: reg,
    dispatch: (a) => { state2 = reducer(state2, a); },
    exit: () => {},
  };
  await recoverCmd.handler(ctx2, ["latest"]);
  assert(captured2 === null,
    "/recover latest with no lastTaskId must NOT hit the API");
  const lastWarn = [...state2.history, ...state2.pending]
    .reverse().find((it) => it.kind === "log" && it.level === "warn");
  assert(lastWarn, "/recover latest empty path should append a warn LogItem");
}

// --- live: client.listTasks (only if BLADE_AI_SERVER set) ---------
const server = process.env.BLADE_AI_SERVER;
if (server) {
  const client = new BladeClient(server);
  // wait for health
  let healthy = false;
  for (let i = 0; i < 100; i++) {
    if (await client.health()) { healthy = true; break; }
    await new Promise((r) => setTimeout(r, 200));
  }
  assert(healthy, "server health did not respond within 20s");

  const tasks = await client.listTasks();
  assert(typeof tasks === "object" && tasks !== null, "listTasks should return object");
  assert("tasks" in tasks || "total" in tasks, `listTasks shape unexpected: ${JSON.stringify(Object.keys(tasks))}`);
  console.log(`live: listTasks returned ${tasks.total ?? "?"} task(s)`);
} else {
  console.log("(BLADE_AI_SERVER not set — skipping live listTasks check)");
}

// --- summary -------------------------------------------------------
probe("summary");
console.log("");
if (failures.length > 0) {
  console.error("--- FAILURES ---");
  for (const f of failures) console.error("  - " + f);
  clearTimeout(watchdog);
  process.exit(1);
}
console.log("✓ all M4 slash assertions passed");
clearTimeout(watchdog);
process.exit(0);
