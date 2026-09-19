/**
 * ``/resume`` handler tests — wired-ctx level.
 *
 * commands.test.ts covers pure functions only (its header note); the
 * /resume handler needs a real ctx (client stub + dispatch closure
 * feeding the actual reducer), so these live here instead of in the
 * smoke script — the assertions are about the reducer-state contract
 * (session switch, streamState settle, error paths), which is easier
 * to pin precisely in-process than by diffing smoke output.
 *
 * The handler's dispatch sequence IS the /resume visual-rebuild
 * protocol; these tests lock its observable effects:
 *   - bare /resume      → list log, no state mutation
 *   - missing jsonl     → resume.no_events warn, no REPLAY_STARTED
 *   - happy path        → SESSION_INITIALIZED switches session.id,
 *                         streamState back to idle, resumeSession
 *                         called BEFORE any visual dispatch
 *   - non-idle guard    → refuse before any network call
 */

import { describe, expect, it, vi } from "vitest";
import {
  buildRegistry,
  runSessionResume,
  type SlashCommandContext,
} from "./commands.js";
import { reducer } from "./reducer.js";
import { initialAppState, type AppState, type LogItem } from "./types.js";
import {
  BladeApiError,
  RESPONSE_CODE_TASK_NOT_FOUND,
  type ResumableSessionItem,
} from "../api/client.js";
import { configureI18n } from "../i18n/index.js";

configureI18n("en");

// ── harness ──────────────────────────────────────────────────────────

type ClientStub = {
  listResumableSessions: ReturnType<typeof vi.fn>;
  getMemoryEvents: ReturnType<typeof vi.fn>;
  resumeSession: ReturnType<typeof vi.fn>;
  getSessionState: ReturnType<typeof vi.fn>;
};

function makeClient(overrides: Partial<ClientStub> = {}): ClientStub {
  return {
    listResumableSessions: vi.fn().mockResolvedValue([]),
    getMemoryEvents: vi.fn().mockResolvedValue([]),
    resumeSession: vi.fn().mockResolvedValue({ conversationThreadId: "conv-abc" }),
    getSessionState: vi.fn().mockResolvedValue({
      cluster: "prod-cluster",
      namespace: "default",
      model_name: "qwen-max",
    }),
    ...overrides,
  };
}

function makeCtx(
  client: ClientStub,
  state: AppState = initialAppState,
  opts: { supportsResume?: boolean; clearScreen?: () => void } = {},
): { ctx: SlashCommandContext; states: AppState[] } {
  const registry = buildRegistry();
  const states: AppState[] = [state];
  const dispatch = (action: Action0): void => {
    states.push(reducer(states[states.length - 1]!, action));
  };
  const ctx: SlashCommandContext = {
    // The handler only calls the four stubbed methods; the cast keeps
    // the stub small instead of implementing the whole BladeClient.
    client: client as unknown as SlashCommandContext["client"],
    sessionId: "sess_boot",
    // The Ink TUI host sets this (store-driven session switch is
    // wired there); default it on here and opt OUT in the
    // host-gate test below.
    supportsResume: opts.supportsResume ?? true,
    state,
    registry,
    dispatch,
    exit: () => undefined,
    beginReplay: () => new AbortController(),
    beginManualCompact: () => new AbortController(),
    hostVersion: "test",
    // Host-injected viewport wipe — the Ink TUI writes ANSI; tests
    // inject a spy to lock the /clear-parity call ordering.
    clearScreen: opts.clearScreen,
  };
  return { ctx, states };
}

type Action0 = Parameters<typeof reducer>[1];

const REG = buildRegistry();

async function runResume(
  client: ClientStub,
  args: string[],
  state: AppState = initialAppState,
): Promise<{ states: AppState[]; logs: { level: string; text: string }[] }> {
  const { ctx, states } = makeCtx(client, state);
  await REG.get("resume")!.handler(ctx, args);
  const final = states[states.length - 1]!;
  const logs = final.history
    .filter((h): h is LogItem => h.kind === "log")
    .map((h) => ({ level: h.level, text: h.text }));
  return { states, logs };
}

// ── tests ────────────────────────────────────────────────────────────

describe("/resume bare (list)", () => {
  it("renders the resumable sessions and a usage tail, no state mutation", async () => {
    const items: ResumableSessionItem[] = [
      {
        tui_session_id: "sess_aaa",
        size_bytes: 2048,
        event_count: 42,
        started_at: "2025-06-01T10:30:00Z",
        modified_at: Date.now() / 1000,
        first_input: "inject cpu fullload",
      },
    ];
    const client = makeClient({
      listResumableSessions: vi.fn().mockResolvedValue(items),
    });
    const { states, logs } = await runResume(client, []);
    expect(client.listResumableSessions).toHaveBeenCalledTimes(1);
    // One log item carrying head + row + usage hint.
    const text = logs.map((l) => l.text).join("\n");
    expect(text).toContain("sess_aaa");
    expect(text).toContain("42");
    expect(text).toContain("inject cpu fullload");
    expect(text).toContain("/resume <tui_session_id>");
    // The timestamp column derives from ``modified_at`` (the mtime
    // the listing sorts by), NOT ``started_at`` — creation can sit
    // days earlier and would contradict the "newest first" order.
    expect(text).not.toContain("2025-06-01");
    expect(text).toMatch(/\d{2}-\d{2} \d{2}:\d{2}/);
    // No turn machinery fired: streamState untouched, no replay frame.
    const final = states[states.length - 1]!;
    expect(final.streamState).toBe("idle");
    expect(final.session.id).toBe("");
  });

  it("empty list renders the empty notice", async () => {
    const { logs } = await runResume(makeClient(), []);
    expect(logs[0]?.text).toContain("no resumable sessions");
    expect(logs[0]?.level).toBe("info");
  });

  it("list failure is a warn, not a thrown error", async () => {
    const client = makeClient({
      listResumableSessions: vi
        .fn()
        .mockRejectedValue(new Error("HTTP 500")),
    });
    const { logs } = await runResume(client, []);
    expect(logs[0]?.level).toBe("warn");
    expect(logs[0]?.text).toContain("HTTP 500");
  });
});

describe("/resume <sid> error paths", () => {
  it("missing events jsonl (code TASK_NOT_FOUND) → dedicated no-fallback warn, nothing dispatched", async () => {
    // The server's fail envelope keeps HTTP 200 and carries the reason
    // as ``code``; the handler must key off the CODE, not the message
    // prose (rewordable without notice) — this mock carries the real
    // server wording, but the assertions below would hold under any
    // rewording because only the code matters.
    const client = makeClient({
      getMemoryEvents: vi
        .fn()
        .mockRejectedValue(
          new BladeApiError(
            "getMemoryEvents",
            RESPONSE_CODE_TASK_NOT_FOUND,
            "no events jsonl for session 'sess_x'",
          ),
        ),
    });
    const { states, logs } = await runResume(client, ["sess_x"]);
    expect(logs[0]?.level).toBe("warn");
    expect(logs[0]?.text).toContain("sess_x");
    // The server was never asked to rehydrate (order contract: events
    // first, THEN resume).
    expect(client.resumeSession).not.toHaveBeenCalled();
    // No REPLAY_STARTED fired — history carries only the warn log.
    const final = states[states.length - 1]!;
    expect(final.streamState).toBe("idle");
    expect(final.isReplaying).toBe(false);
  });

  it("a reworded message with the right code still hits the no-events path", async () => {
    // The lock the code-based branch buys: prose is user-facing and
    // free to change; this mock uses a wording that would NEVER match
    // the old ``includes("no events jsonl")`` string probe.
    const client = makeClient({
      getMemoryEvents: vi
        .fn()
        .mockRejectedValue(
          new BladeApiError(
            "getMemoryEvents",
            RESPONSE_CODE_TASK_NOT_FOUND,
            "audit trail absent",
          ),
        ),
    });
    const { logs } = await runResume(client, ["sess_x"]);
    expect(logs[0]?.level).toBe("warn");
    expect(logs[0]?.text).toContain("no fallback chain");
    expect(logs[0]?.text).toContain("sess_x");
  });

  it("a fail envelope with a DIFFERENT code is not the no-events path", async () => {
    // Branching on BladeApiError-ness alone would misroute every other
    // fail envelope into the dedicated wording. Code 500 must fall
    // through to the generic resume.failed log carrying the message.
    const client = makeClient({
      getMemoryEvents: vi
        .fn()
        .mockRejectedValue(
          new BladeApiError("getMemoryEvents", 500, "store not initialised"),
        ),
    });
    const { logs } = await runResume(client, ["sess_x"]);
    const text = logs.map((l) => l.text).join("\n");
    expect(text).not.toContain("no fallback chain");
    expect(logs[0]?.level).toBe("warn");
    expect(text).toContain("store not initialised");
  });

  it("refuses when a turn is in flight (defense-in-depth)", async () => {
    const client = makeClient();
    const busyState = reducer(initialAppState, {
      type: "TURN_STARTED",
      input: "live turn",
    });
    const { logs } = await runResume(client, ["sess_x"], busyState);
    expect(logs[0]?.level).toBe("warn");
    expect(client.getMemoryEvents).not.toHaveBeenCalled();
  });

  it("generic fetch failure after REPLAY_STARTED is caught and closed", async () => {
    // resumeSession (step 2) failing AFTER getMemoryEvents succeeded:
    // the handler's catch must still log — no dispatch was issued yet,
    // so the state stays consistent.
    const client = makeClient({
      getMemoryEvents: vi.fn().mockResolvedValue([
        { event_type: "user_input", data: { content: "hi" }, source: "user" },
      ]),
      resumeSession: vi.fn().mockRejectedValue(new Error("HTTP 503")),
    });
    const { states, logs } = await runResume(client, ["sess_x"]);
    expect(logs[0]?.level).toBe("warn");
    expect(logs[0]?.text).toContain("503");
    const final = states[states.length - 1]!;
    expect(final.streamState).toBe("idle");
    expect(final.session.id).toBe(""); // never switched
  });
});

describe("/resume <sid> happy path", () => {
  const EVENTS = [
    { ts: "1", source: "user", task_id: "", event_type: "user_input", data: { content: "inject cpu" } },
    { ts: "2", source: "server", task_id: "t1", event_type: "result", data: { type: "result", content: "ok", task_id: "t1" } },
    { ts: "3", source: "server", task_id: "t1", event_type: "done", data: { type: "done", task_id: "t1" } },
  ];

  it("switches the store session, settles idle, and logs the summary", async () => {
    const client = makeClient({
      getMemoryEvents: vi.fn().mockResolvedValue(EVENTS),
    });
    const { states, logs } = await runResume(client, ["sess_old"]);
    const final = states[states.length - 1]!;

    // Session switch — the field Composer's useStream subscribes to.
    expect(final.session.id).toBe("sess_old");
    // Header fields re-read from the resumed session's state.
    expect(final.session.cluster).toBe("prod-cluster");
    expect(final.session.modelName).toBe("qwen-max");
    // Turn machinery settled.
    expect(final.streamState).toBe("idle");
    expect(final.isReplaying).toBe(false);
    expect(final.pending).toHaveLength(0);
    // Visual rebuild: user echo + result landed in history.
    const kinds = final.history.map((h) => h.kind);
    expect(kinds).toContain("user");
    expect(kinds).toContain("result");
    // Summary log last.
    const last = logs[logs.length - 1]!;
    expect(last.level).toBe("ok");
    expect(last.text).toContain("sess_old");
  });

  it("carries the restoring notice as a TRANSIENT spinner, not a persisted log", async () => {
    const client = makeClient({
      getMemoryEvents: vi.fn().mockResolvedValue(EVENTS),
    });
    const { states, logs } = await runResume(client, ["sess_old"]);
    const final = states[states.length - 1]!;

    // During the replay the notice lived on the spinner row…
    const spinnerTexts = states
      .map((s) => s.bootProgress)
      .filter((p): p is string => typeof p === "string");
    expect(spinnerTexts.some((p) => p.includes("sess_old"))).toBe(true);
    // …and it is retired when the restore finishes — a spinner row
    // must never outlive the operation it announces.
    expect(final.bootProgress).toBeNull();
    // Nothing persisted: a log line stays in scrollback forever
    // AFTER the restore it announces has finished.
    const texts = logs.map((l) => l.text).join("\n");
    expect(texts).not.toContain("resuming session");
    // The durable record is the done confirmation.
    expect(texts).toContain("resumed **sess_old**");
  });

  it("preserveBootCards keeps the boot-laid cards at the head of the rebuilt history", async () => {
    const client = makeClient({
      getMemoryEvents: vi.fn().mockResolvedValue(EVENTS),
    });
    // Simulate the boot path (BootRunner Phase 3a.5): welcome +
    // doctor + pending cards dispatched BEFORE the takeover, plus a
    // non-card log interleaved between them that the clear must
    // drop.
    let state: AppState = initialAppState;
    const dispatch = (a: Action0): void => {
      state = reducer(state, a);
    };
    dispatch({
      type: "HISTORY_APPENDED",
      item: {
        kind: "welcome_card",
        id: "boot-welcome",
        modelName: "m",
        permissionMode: "confirm",
        kubeconfig: "",
        namespace: "default",
        version: "v",
      },
    });
    dispatch({
      type: "HISTORY_APPENDED",
      item: {
        kind: "boot_doctor_card",
        id: "boot-doctor",
        capturedAt: "t",
        passedCount: 1,
        totalCount: 1,
        checks: [],
      },
    });
    dispatch({ type: "LOG_APPENDED", level: "info", text: "boot noise" });
    dispatch({
      type: "HISTORY_APPENDED",
      item: { kind: "pending_tasks_card", id: "boot-pending", tasks: [] },
    });

    const outcome = await runSessionResume(
      {
        client: client as unknown as SlashCommandContext["client"],
        dispatch,
        pushLog: (text, level) =>
          dispatch({ type: "LOG_APPENDED", text, level }),
        preserveBootCards: true,
        fallbackHeader: { cluster: "", namespace: "default", modelName: "" },
      },
      "sess_old",
    );
    expect(outcome).toBe("ok");
    const kinds = state.history.map((h) => h.kind);
    // Boot cards survive at the head, in dispatch order — fresh-boot
    // visual parity (welcome → doctor → pending → replayed turns).
    expect(kinds.slice(0, 3)).toEqual([
      "welcome_card",
      "boot_doctor_card",
      "pending_tasks_card",
    ]);
    expect(kinds).toContain("user");
    expect(kinds).toContain("result");
    const logTexts = state.history
      .filter((h): h is LogItem => h.kind === "log")
      .map((l) => l.text);
    // The interleaved non-card log is gone; the done line is the
    // durable confirmation.
    expect(logTexts).not.toContain("boot noise");
    expect(logTexts.some((t) => t.includes("resumed **sess_old**"))).toBe(
      true,
    );
  });

  it("retires the transient spinner when the fold throws mid-way", async () => {
    // A stuck "resuming…" spinner row (bootProgress never nulled) is
    // the persistence bug all over again — on the FAILURE path: the
    // slash handler's catch only logs the failure, so the HIDE must
    // fire from a finally inside runSessionResume itself, covering
    // throws out of the fold loop and not only the success return.
    const client = makeClient({
      getMemoryEvents: vi.fn().mockResolvedValue(EVENTS),
    });
    let state: AppState = initialAppState;
    const seen: string[] = [];
    const dispatch = (a: Action0): void => {
      seen.push(a.type);
      // TURN_STARTED is the fold's action for a user_input event —
      // throwing here aborts the replay AFTER BOOT_PROGRESS_SHOW
      // has fired, the exact window the finally must cover.
      if (a.type === "TURN_STARTED") throw new Error("boom");
      state = reducer(state, a);
    };
    await expect(
      runSessionResume(
        {
          client: client as unknown as SlashCommandContext["client"],
          dispatch,
          pushLog: (text, level) =>
            dispatch({ type: "LOG_APPENDED", text, level }),
          fallbackHeader: { cluster: "", namespace: "default", modelName: "" },
        },
        "sess_old",
      ),
    ).rejects.toThrow("boom");
    expect(seen).toContain("BOOT_PROGRESS_SHOW");
    // HIDE is the LAST dispatch — it fired from the finally AFTER
    // the fold threw, before the exception reached the caller.
    expect(seen[seen.length - 1]).toBe("BOOT_PROGRESS_HIDE");
    expect(state.bootProgress).toBeNull();
  });

  it("calls server-side rehydrate exactly once, after the events fetch", async () => {
    const client = makeClient({
      getMemoryEvents: vi.fn().mockResolvedValue(EVENTS),
    });
    await runResume(client, ["sess_old"]);
    expect(client.resumeSession).toHaveBeenCalledTimes(1);
    expect(client.resumeSession).toHaveBeenCalledWith("sess_old");
    expect(client.getSessionState).toHaveBeenCalledWith("sess_old");
  });

  it("commits exactly ONE turn-usage row for a legacy tail (usage + result, no done)", async () => {
    // Real-world shape (sess_8f61e7a9e2cf): the last turn's segment
    // carries usage events and ends at ``result`` with NO ``done``
    // sidewrite (legacy file). The fold synthesises the TURN_DONE,
    // then runSessionResume dispatches REPLAY_ENDED — two boundary
    // actions back-to-back. Before the counter-drain fix this
    // re-emitted the usage row: the user saw two identical
    // "⚡ 本轮共 688.3k tokens" lines with the SAME fold-time
    // timestamp at the tail of every resumed session like this.
    const client = makeClient({
      getMemoryEvents: vi.fn().mockResolvedValue([
        {
          ts: "1",
          source: "user",
          task_id: "",
          event_type: "user_input",
          data: { content: "10分钟" },
        },
        {
          ts: "2",
          source: "pipeline",
          task_id: "t1",
          event_type: "usage",
          data: { type: "usage", input_tokens: 650400, output_tokens: 37800 },
        },
        {
          ts: "3",
          source: "pipeline",
          task_id: "t1",
          event_type: "result",
          data: { type: "result", content: "ok", task_id: "t1" },
        },
      ]),
    });
    const { states } = await runResume(client, ["sess_old"]);
    const final = states[states.length - 1]!;
    const usage = final.history.filter((h) => h.kind === "turn_usage");
    expect(usage).toHaveLength(1);
    if (usage[0]?.kind === "turn_usage") {
      expect(usage[0].inputTokens).toBe(650400);
      expect(usage[0].outputTokens).toBe(37800);
    }
  });

  it("clears the boot session's history (welcome card does not survive)", async () => {
    const client = makeClient({
      getMemoryEvents: vi.fn().mockResolvedValue(EVENTS),
    });
    const booted = reducer(initialAppState, {
      type: "SESSION_INITIALIZED",
      session: { id: "sess_boot", cluster: "c", namespace: "default", modelName: "m" },
    });
    const { states } = await runResume(client, ["sess_old"], booted);
    const final = states[states.length - 1]!;
    // No item from the boot session survives; the rebuilt history only
    // carries the resumed session's items (logs + user + result).
    for (const h of final.history) {
      expect(h.kind).not.toBe("welcome_card");
    }
    // Session switched off the boot id.
    expect(final.session.id).toBe("sess_old");
  });

  it("wipes the viewport before ANY state dispatch (/clear parity)", async () => {
    // HISTORY_CLEARED resets the store, never the terminal: without the
    // host's ANSI wipe the boot session's burn-in stays on screen and
    // the folded history renders below it — two sessions mixed on one
    // viewport. The wipe must fire exactly once, and while the boot
    // state is still the ONLY recorded state (i.e. before even
    // REPLAY_STARTED dispatched).
    const client = makeClient({
      getMemoryEvents: vi.fn().mockResolvedValue(EVENTS),
    });
    const wipeAt: number[] = [];
    const { ctx, states } = makeCtx(client, initialAppState, {
      clearScreen: () => { wipeAt.push(states.length); },
    });
    await REG.get("resume")!.handler(ctx, ["sess_old"]);
    expect(wipeAt).toHaveLength(1);
    expect(wipeAt[0]).toBe(1); // pre-REPLAY_STARTED, pre-HISTORY_CLEARED
  });

  it("does not wipe the viewport when the resume fails early", async () => {
    // A failed events fetch must leave the terminal untouched — the
    // boot session's on-screen history is still LIVE state; wiping it
    // would blank the viewport while the store still holds every item.
    const client = makeClient({
      getMemoryEvents: vi
        .fn()
        .mockRejectedValue(
          new BladeApiError(
            "getMemoryEvents",
            RESPONSE_CODE_TASK_NOT_FOUND,
            "no events jsonl for session 'sess_x'",
          ),
        ),
    });
    const clearScreen = vi.fn();
    const { ctx } = makeCtx(client, initialAppState, { clearScreen });
    await REG.get("resume")!.handler(ctx, ["sess_x"]);
    expect(clearScreen).not.toHaveBeenCalled();
  });
});

describe("/resume <sid> unresolved confirm gates", () => {
  it("a confirm followed by error+done lands RESOLVED in history (no ghost card)", async () => {
    // The server can end a turn while the gate waits (timeout / graph
    // exception → error event + done) — the user never answered. The
    // fold must flush the open gate BEFORE TURN_DONE's commitPending
    // commits it into history; committed unresolved, the card would
    // render as a live keyboard-active Select inside <Static> (the
    // exact ghost-card the Step-7 comment promises never happens).
    const client = makeClient({
      getMemoryEvents: vi.fn().mockResolvedValue([
        { ts: "1", source: "user", task_id: "", event_type: "user_input", data: { content: "inject" } },
        {
          ts: "2", source: "server", task_id: "t9", event_type: "confirm",
          data: { type: "confirm", content: "approve?", task_id: "t9", node: "confirmation_gate", payload: { level: 70 } },
        },
        { ts: "3", source: "server", task_id: "t9", event_type: "error", data: { type: "error", content: "turn timed out", task_id: "t9" } },
        { ts: "4", source: "server", task_id: "t9", event_type: "done", data: { type: "done", task_id: "t9" } },
      ]),
    });
    const { states } = await runResume(client, ["sess_old"]);
    const final = states[states.length - 1]!;

    // The turn machinery settled and the error landed.
    expect(final.streamState).toBe("idle");
    expect(final.pending).toHaveLength(0);
    expect(final.history.some((h) => h.kind === "error")).toBe(true);
    // THE assertion: the card committed to history carries the
    // interrupted sentinel — resolved, never a live Select.
    const card = final.history.find(
      (h): h is Extract<(typeof final.history)[number], { kind: "confirm_prompt" }> =>
        h.kind === "confirm_prompt",
    );
    expect(card).toBeDefined();
    expect(card!.resolved).toBe(true);
    expect(card!.answer).toBe("interrupted");
    // No decision slot left for Composer's network effect to re-fire.
    expect(final.pendingDecision).toBeNull();
  });

  it("double gate (L1 open + L2 open) flushes BOTH before the boundary", async () => {
    // The rare protocol race CONFIRM_RECEIVED defends against: a
    // second confirm fires while the first card is still unanswered.
    // Both cards are live in pending when the turn ends — the FIFO
    // must sentinel-flush each, not just the last one.
    const client = makeClient({
      getMemoryEvents: vi.fn().mockResolvedValue([
        { ts: "1", source: "user", task_id: "", event_type: "user_input", data: { content: "inject" } },
        {
          ts: "2", source: "server", task_id: "t9", event_type: "confirm",
          data: { type: "confirm", content: "L1 gate?", task_id: "t9", node: "intent_confirm" },
        },
        {
          ts: "3", source: "server", task_id: "t9", event_type: "confirm",
          data: { type: "confirm", content: "L2 gate?", task_id: "t9", node: "confirmation_gate" },
        },
        { ts: "4", source: "server", task_id: "t9", event_type: "error", data: { type: "error", content: "boom", task_id: "t9" } },
        { ts: "5", source: "server", task_id: "t9", event_type: "done", data: { type: "done", task_id: "t9" } },
      ]),
    });
    const { states } = await runResume(client, ["sess_old"]);
    const final = states[states.length - 1]!;
    const cards = final.history.filter(
      (h): h is Extract<(typeof final.history)[number], { kind: "confirm_prompt" }> =>
        h.kind === "confirm_prompt",
    );
    // Both gates existed, both landed, BOTH carry the sentinel.
    expect(cards).toHaveLength(2);
    for (const c of cards) {
      expect(c.resolved).toBe(true);
      expect(c.answer).toBe("interrupted");
    }
    expect(final.pending).toHaveLength(0);
  });

  it("an answered gate is NOT re-flushed (answer semantics preserved)", async () => {
    // confirm → confirm_answer(approved) → result → done: the three-
    // action sequence settles the card with the REAL answer; the
    // boundary flush must skip gates closed by CONFIRM_USER_DECIDED,
    // or every answered card in a rebuilt history would read
    // "interrupted".
    const client = makeClient({
      getMemoryEvents: vi.fn().mockResolvedValue([
        { ts: "1", source: "user", task_id: "", event_type: "user_input", data: { content: "inject" } },
        {
          ts: "2", source: "server", task_id: "t9", event_type: "confirm",
          data: { type: "confirm", content: "approve?", task_id: "t9", node: "confirmation_gate" },
        },
        { ts: "3", source: "user", task_id: "t9", event_type: "confirm_answer", data: { content: "approved" } },
        { ts: "4", source: "server", task_id: "t9", event_type: "result", data: { type: "result", content: "ok", task_id: "t9" } },
        { ts: "5", source: "server", task_id: "t9", event_type: "done", data: { type: "done", task_id: "t9" } },
      ]),
    });
    const { states } = await runResume(client, ["sess_old"]);
    const final = states[states.length - 1]!;
    const card = final.history.find(
      (h): h is Extract<(typeof final.history)[number], { kind: "confirm_prompt" }> =>
        h.kind === "confirm_prompt",
    );
    expect(card).toBeDefined();
    expect(card!.resolved).toBe(true);
    expect(card!.answer).toBe("approved");
    expect(final.pendingDecision).toBeNull();
  });
});
