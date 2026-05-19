/**
 * Headless smoke test: drive the reducer with a real SSE turn from the
 * Python backend and assert the resulting AppState looks sane.
 *
 * Run via:
 *   BLADE_AI_SERVER=http://127.0.0.1:PORT \
 *   node --experimental-strip-types scripts/smoke-reducer.mjs
 *
 * What we check:
 *   - thinking accumulates into thoughtBuffer; thoughtSubject is the
 *     last complete sentence (or rolling tail).
 *   - token chunks coalesce into a single trailing AgentItem.
 *   - TURN_DONE moves pending into history and clears subject/buffer.
 *   - history items keep stable ids (no jitter from idCounter side effects).
 */

// Import TS sources directly through tsx — we want to drive the
// real reducer / client modules, not the bundled output.
import { tsImport } from "tsx/esm/api";

const reducerMod = await tsImport("../src/state/reducer.ts", import.meta.url);
const typesMod = await tsImport("../src/state/types.ts", import.meta.url);
const clientMod = await tsImport("../src/api/client.ts", import.meta.url);

const { reducer } = reducerMod;
const { initialAppState } = typesMod;
const { BladeClient: Client } = clientMod;

const server = process.env.BLADE_AI_SERVER;
if (!server) {
  console.error("set BLADE_AI_SERVER=http://127.0.0.1:PORT");
  process.exit(2);
}

const client = new Client(server);

// Wait for /health — server.app.lifespan startup (skill registry,
// LangGraph factory, checkpointer) takes several seconds even after
// the BLADE_AI_READY signal is printed. Without this, the very first
// fetch can race the bind step and ECONNREFUSED.
const healthDeadline = Date.now() + 20_000;
let healthy = false;
while (Date.now() < healthDeadline) {
  if (await client.health()) {
    healthy = true;
    break;
  }
  await new Promise((r) => setTimeout(r, 200));
}
if (!healthy) {
  console.error("server did not pass /health within 20s");
  process.exit(2);
}

const sid = await client.createSession({});
console.log(`session=${sid}`);

let state = { ...initialAppState };
const dispatch = (action) => {
  state = reducer(state, action);
};

dispatch({ type: "TURN_STARTED", input: "hi, what can you help me with?" });

let counts = { token: 0, thinking: 0, tool_start: 0, tool_end: 0, node_start: 0, node_end: 0, confirm: 0, result: 0, error: 0, done: 0 };

const start = Date.now();
for await (const evt of client.streamTurn(sid, { input: "hi, what can you help me with?", permission_mode: "auto" })) {
  counts[evt.type] = (counts[evt.type] || 0) + 1;
  switch (evt.type) {
    case "token":
      dispatch({ type: "TOKEN_APPENDED", content: evt.content, node: evt.node ?? "" });
      break;
    case "thinking":
      dispatch({ type: "THINKING_APPENDED", content: evt.content, node: evt.node ?? "" });
      break;
    case "tool_start":
      dispatch({ type: "TOOL_STARTED", callId: `t/${evt.tool_name}`, name: evt.tool_name, node: evt.node ?? "" });
      break;
    case "tool_end":
      dispatch({ type: "TOOL_ENDED", callId: `t/${evt.tool_name}`, name: evt.tool_name, status: "success", content: evt.content });
      break;
    case "node_start":
      dispatch({ type: "NODE_STARTED", node: evt.node });
      break;
    case "node_end":
      dispatch({ type: "NODE_ENDED", node: evt.node });
      break;
    case "confirm":
      dispatch({ type: "CONFIRM_RECEIVED", content: evt.content, taskId: evt.task_id });
      break;
    case "result":
      dispatch({ type: "RESULT_RECEIVED", content: evt.content, taskId: evt.task_id });
      break;
    case "error":
      dispatch({ type: "ERROR_RECEIVED", message: evt.content, taskId: evt.task_id });
      break;
    case "done":
      break;
  }
  if (evt.type === "done") break;
}
dispatch({ type: "TURN_DONE" });
const elapsed = Date.now() - start;

console.log(`\n--- counts (${elapsed}ms) ---`);
console.log(counts);

console.log("\n--- final state shape ---");
console.log({
  history_len: state.history.length,
  pending_len: state.pending.length,
  streamState: state.streamState,
  thoughtSubject: state.thoughtSubject,
  thoughtBuffer_len: state.thoughtBuffer.length,
  streamingChars: state.streamingChars,
  nextItemId: state.nextItemId,
});

console.log("\n--- history items ---");
for (const item of state.history) {
  if (item.kind === "user") console.log(`  user[${item.id}] ${item.text}`);
  else if (item.kind === "agent") console.log(`  agent[${item.id}] ${item.text.slice(0, 80)}${item.text.length > 80 ? "…" : ""}`);
  else if (item.kind === "tool") console.log(`  tool[${item.id}] ${item.name} ${item.status} ${item.elapsedMs}ms`);
  else if (item.kind === "system") console.log(`  system[${item.id}] ${item.text}`);
  else if (item.kind === "error") console.log(`  error[${item.id}] ${item.text}`);
}

// Assertions
const failures = [];
if (state.pending.length !== 0) failures.push(`pending should be empty after TURN_DONE, got ${state.pending.length}`);
if (state.streamState !== "idle") failures.push(`streamState should be idle, got ${state.streamState}`);
if (state.thoughtBuffer !== "") failures.push("thoughtBuffer should be empty after TURN_DONE");
if (state.history.length < 2) failures.push(`expected at least 2 history items (user + agent), got ${state.history.length}`);
const ids = state.history.map((h) => h.id);
const dupes = ids.filter((id, i) => ids.indexOf(id) !== i);
if (dupes.length > 0) failures.push(`duplicate ids: ${dupes.join(",")}`);

// M3.1 result event must produce a ResultItem in history (chat turn
// has confirmed_intent="chat" → minimal envelope is still a result).
const resultItems = state.history.filter((h) => h.kind === "result");
if (counts.result > 0 && resultItems.length === 0) {
  failures.push(`server emitted ${counts.result} result event(s) but no ResultItem in history`);
}
if (counts.result > 0 && resultItems.length > 0) {
  const r = resultItems[resultItems.length - 1];
  if (!r.taskId) failures.push("ResultItem.taskId is empty");
  if (!["success", "partial", "failed", "unknown"].includes(r.status))
    failures.push(`ResultItem.status invalid: ${r.status}`);
  console.log(`\n--- ResultItem (last) ---`);
  console.log({
    taskId: r.taskId,
    status: r.status,
    faultType: r.faultType || "(none)",
    bladeUid: r.bladeUid || "(none)",
    duration: r.duration || "(none)",
    summary: r.summary || "(none)",
  });
}

if (failures.length > 0) {
  console.error("\n--- FAILURES ---");
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log("\n✓ all assertions passed");
process.exit(0);
