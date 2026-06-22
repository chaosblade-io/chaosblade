/**
 * Reducer unit tests. Covers the action types most prone to regression:
 *
 *   - TURN_STARTED slash-echo guard (M12 self-check)
 *   - lastTurnInput preservation across TURN_DONE / HISTORY_CLEARED /
 *     MODE_TOGGLED (regression for "lost retry context after /clear")
 *   - TOKEN_APPENDED coalesces into trailing AgentItem (M3 invariant)
 *   - TOOL_STARTED / TOOL_ENDED matching by callId vs name fallback
 *     (M3 self-check fix)
 *   - HISTORY_CLEARED preserves pending mid-stream items
 *
 * Smoke-slash exercises slash-command handlers end-to-end; these unit
 * tests pin reducer invariants in isolation so a refactor that changes
 * a reducer branch fails *here* with a precise diff, not in a
 * sprawling smoke output.
 */

import { describe, expect, it } from "vitest";
import { reducer, type Action } from "./reducer.js";
import { initialAppState, type AppState } from "./types.js";

function fold(actions: Action[], start: AppState = initialAppState): AppState {
  return actions.reduce((s, a) => reducer(s, a), start);
}

describe("reducer / TURN_STARTED", () => {
  it("captures the input as lastTurnInput for /retry", () => {
    const s = reducer(initialAppState, {
      type: "TURN_STARTED",
      input: "inject CPU stress 80%",
    });
    expect(s.lastTurnInput).toBe("inject CPU stress 80%");
  });

  it("pushes the user echo into history immediately", () => {
    const s = reducer(initialAppState, {
      type: "TURN_STARTED",
      input: "hello",
    });
    expect(s.history).toHaveLength(1);
    const item = s.history[0]!;
    expect(item.kind).toBe("user");
    if (item.kind === "user") expect(item.text).toBe("hello");
  });

  it("flips streamState to responding", () => {
    const s = reducer(initialAppState, {
      type: "TURN_STARTED",
      input: "hi",
    });
    expect(s.streamState).toBe("responding");
  });

  it("does NOT update lastTurnInput on slash-command echoes", () => {
    // Composer routes slash commands through TURN_STARTED so they
    // appear in scrollback. /retry is not a re-submittable turn —
    // letting the literal "/retry" string bleed into lastTurnInput
    // would make a second /retry resubmit the slash itself.
    const s = fold([
      { type: "TURN_STARTED", input: "real input" },
      { type: "TURN_DONE" },
      { type: "TURN_STARTED", input: "/retry" },
    ]);
    expect(s.lastTurnInput).toBe("real input");
  });

  it("treats leading whitespace before a slash as still slash-echo", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "real input" },
      { type: "TURN_STARTED", input: "  /help" },
    ]);
    expect(s.lastTurnInput).toBe("real input");
  });

  it("treats mid-string slashes as natural-language input", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "real input" },
      { type: "TURN_STARTED", input: "tell me how /retry works" },
    ]);
    expect(s.lastTurnInput).toBe("tell me how /retry works");
  });
});

describe("reducer / lastTurnInput durability", () => {
  it("survives TURN_DONE", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hello" },
      { type: "TURN_DONE" },
    ]);
    expect(s.lastTurnInput).toBe("hello");
  });

  it("survives HISTORY_CLEARED so /retry works after /clear", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hello" },
      { type: "TURN_DONE" },
      { type: "HISTORY_CLEARED" },
    ]);
    expect(s.lastTurnInput).toBe("hello");
    expect(s.history).toHaveLength(0);
  });

  it("survives MODE_TOGGLED", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hello" },
      { type: "TURN_DONE" },
      { type: "MODE_TOGGLED", mode: "auto" },
    ]);
    expect(s.lastTurnInput).toBe("hello");
    expect(s.config.permissionMode).toBe("auto");
  });
});

describe("reducer / DISPLAY_MODE_CHANGED", () => {
  // ``/mode`` (display density) writes through DISPLAY_MODE_CHANGED.
  // It's orthogonal to MODE_TOGGLED (permission mode); both must
  // coexist on the same ``state.config`` without clobbering each
  // other.
  it("updates only the displayMode field", () => {
    const baseline = initialAppState.config;
    const s = fold([{ type: "DISPLAY_MODE_CHANGED", mode: "dense" }]);
    expect(s.config.displayMode).toBe("dense");
    // permissionMode untouched.
    expect(s.config.permissionMode).toBe(baseline.permissionMode);
  });

  it("coexists with MODE_TOGGLED", () => {
    const s = fold([
      { type: "DISPLAY_MODE_CHANGED", mode: "working" },
      { type: "MODE_TOGGLED", mode: "auto" },
    ]);
    expect(s.config.displayMode).toBe("working");
    expect(s.config.permissionMode).toBe("auto");
  });

  it("survives TURN_STARTED — display mode is a sticky session preference", () => {
    const s = fold([
      { type: "DISPLAY_MODE_CHANGED", mode: "dense" },
      { type: "TURN_STARTED", input: "go" },
    ]);
    expect(s.config.displayMode).toBe("dense");
  });
});

describe("reducer / locator allocation", () => {
  // ``/show /copy /rerun /expand`` resolve user-typed E#/T# tokens
  // against ``state.locators.byId``. The reducer is the only side
  // that can mint these IDs — these tests pin the allocation
  // contract so a refactor of TOOL_ENDED / RESULT_RECEIVED that
  // forgets to bump the counter, or that double-allocates on
  // replay, fails here with a precise diff.
  it("assigns T1 to the first finalised tool", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c1",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
    ]);
    expect(s.locators.byId["T1"]).toBeDefined();
    expect(s.locators.byId["T1"]?.kind).toBe("tool");
    expect(s.locators.nextToolN).toBe(2);
    expect(s.locators.nextExperimentN).toBe(1);
  });

  it("walks T1 → T2 → T3 across consecutive TOOL_ENDEDs", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      { type: "TOOL_STARTED", callId: "c2", name: "blade", node: "n" },
      { type: "TOOL_STARTED", callId: "c3", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c1",
        name: "kubectl",
        status: "success",
        content: "a",
      },
      {
        type: "TOOL_ENDED",
        callId: "c2",
        name: "blade",
        status: "success",
        content: "b",
      },
      {
        type: "TOOL_ENDED",
        callId: "c3",
        name: "kubectl",
        status: "success",
        content: "c",
      },
    ]);
    expect(Object.keys(s.locators.byId).sort()).toEqual(["T1", "T2", "T3"]);
    expect(s.locators.nextToolN).toBe(4);
    // Locators land on the FINAL tool snapshot.
    expect(
      (s.locators.byId["T1"] as { name: string }).name,
    ).toBe("kubectl");
    expect(
      (s.locators.byId["T2"] as { name: string }).name,
    ).toBe("blade");
  });

  it("assigns E1 to the first RESULT_RECEIVED + captures lastTurnInput", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "inject CPU stress 80%" },
      {
        type: "RESULT_RECEIVED",
        content: JSON.stringify({
          status: "success",
          data: {
            task_id: "task-A",
            task_state: "injected",
            blade_uid: "uid-1",
          },
        }),
        taskId: "task-A",
      },
    ]);
    const e1 = s.locators.byId["E1"];
    expect(e1).toBeDefined();
    if (e1 && e1.kind === "result") {
      expect(e1.taskId).toBe("task-A");
      // The user's NL input was captured so /rerun has something to
      // surface. Without this snapshot /rerun would only know the
      // most recent turn — useless for older results.
      expect(e1.userInput).toBe("inject CPU stress 80%");
    }
    expect(s.locators.nextExperimentN).toBe(2);
    expect(s.locators.nextToolN).toBe(1);
  });

  it("HISTORY_CLEARED resets locators to T1/E1", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c1",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
      { type: "HISTORY_CLEARED" },
    ]);
    expect(s.locators.byId).toEqual({});
    expect(s.locators.nextToolN).toBe(1);
    expect(s.locators.nextExperimentN).toBe(1);
  });

  it("RESULT_RECEIVED captures lastTaskId for /recover latest", () => {
    // Mirror of Python's ``conversation.last_task_id`` — the most
    // recently completed experiment's id, lookupable via
    // ``/recover latest`` / ``/review`` (no arg). Lock the wiring
    // here so a future refactor that drops the slot would break loud.
    const s = fold([
      { type: "TURN_STARTED", input: "inject CPU" },
      {
        type: "RESULT_RECEIVED",
        content: JSON.stringify({
          status: "success",
          data: { task_id: "task-A", task_state: "injected" },
        }),
        taskId: "task-A",
      },
    ]);
    expect(s.lastTaskId).toBe("task-A");
  });

  it("RESULT_RECEIVED prefers payload task_id over outer turn id for /recover latest", () => {
    // /turn result frames are keyed by the conversation turn id, while
    // the inject pipeline id recover needs is in content.data.task_id.
    const s = fold([
      { type: "TURN_STARTED", input: "inject CPU" },
      {
        type: "RESULT_RECEIVED",
        content: JSON.stringify({
          status: "success",
          data: { task_id: "task-inject", task_state: "injected" },
        }),
        taskId: "turn-abc123",
      },
    ]);
    expect(s.lastTaskId).toBe("task-inject");
  });

  it("RESULT_RECEIVED preserves recover operation and partial status", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "/recover task-A" },
      {
        type: "RESULT_RECEIVED",
        content: JSON.stringify({
          status: "success",
          data: {
            task_id: "task-R",
            operation: "recover",
            task_state: "partial_recovered",
            fault_type: "pod-network-loss",
            blade_uid: "uid-1",
          },
        }),
        taskId: "task-R",
      },
    ]);
    const result = s.history.find((item) => item.kind === "result");
    expect(result).toBeDefined();
    if (result && result.kind === "result") {
      expect(result.taskId).toBe("task-R");
      expect(result.operation).toBe("recover");
      expect(result.status).toBe("partial");
    }
  });

  it("recover results do not overwrite latest injectable task id", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "inject CPU" },
      {
        type: "RESULT_RECEIVED",
        content: JSON.stringify({
          status: "success",
          data: { task_id: "task-inject", task_state: "injected" },
        }),
        taskId: "turn-inject",
      },
      { type: "TURN_DONE" },
      { type: "TURN_STARTED", input: "/recover task-inject" },
      {
        type: "RESULT_RECEIVED",
        content: JSON.stringify({
          status: "success",
          data: {
            task_id: "task-recover",
            operation: "recover",
            task_state: "recovered",
          },
        }),
        taskId: "task-recover",
      },
    ]);
    expect(s.lastTaskId).toBe("task-inject");
  });

  it("lastTaskId follows the MOST recent task, not the first", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "first" },
      {
        type: "RESULT_RECEIVED",
        content: JSON.stringify({ status: "success", data: { task_state: "injected" } }),
        taskId: "task-1",
      },
      { type: "TURN_DONE" },
      { type: "TURN_STARTED", input: "second" },
      {
        type: "RESULT_RECEIVED",
        content: JSON.stringify({ status: "success", data: { task_state: "injected" } }),
        taskId: "task-2",
      },
    ]);
    expect(s.lastTaskId).toBe("task-2");
  });

  it("RESULT_RECEIVED with empty taskId leaves prior lastTaskId intact", () => {
    // Chat-only / replay results sometimes arrive with no taskId —
    // shadowing the prior "real" id with empty would break
    // /recover latest after harmless intermediate events.
    const s = fold([
      { type: "TURN_STARTED", input: "first" },
      {
        type: "RESULT_RECEIVED",
        content: JSON.stringify({ status: "success", data: { task_state: "injected" } }),
        taskId: "task-real",
      },
      // Second result with no taskId — must not blank lastTaskId.
      {
        type: "RESULT_RECEIVED",
        content: JSON.stringify({ status: "success", data: {} }),
      },
    ]);
    expect(s.lastTaskId).toBe("task-real");
  });

  it("HISTORY_CLEARED also clears lastTaskId", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "first" },
      {
        type: "RESULT_RECEIVED",
        content: JSON.stringify({ status: "success", data: { task_state: "injected" } }),
        taskId: "task-A",
      },
      { type: "HISTORY_CLEARED" },
    ]);
    // Once history is gone the id refers to nothing the user can see —
    // wipe alongside the locator reset.
    expect(s.lastTaskId).toBeUndefined();
  });

  it("survives TURN_STARTED — locators are session-scoped, not turn-scoped", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "first turn" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c1",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
      { type: "TURN_DONE" },
      // Second turn — ``/show T1`` from the first turn must still
      // resolve. If TURN_STARTED reset locators the user couldn't
      // reference any prior tool from a follow-up command.
      { type: "TURN_STARTED", input: "follow up" },
    ]);
    expect(s.locators.byId["T1"]).toBeDefined();
    expect(s.locators.nextToolN).toBe(2);
  });
});

describe("reducer / RESULT_RECEIVED commit-direct-to-history", () => {
  // Locks in the方案 B contract: result lands in ``history`` directly,
  // NOT in ``pending``. Pending is fully drained when result arrives so
  // the agent message + tool groups that produced it ride along to
  // history in the same dispatch — preventing the +11 row dyn-frame
  // jump that occurred when result joined pending alongside an
  // already-large agent body.
  it("commits result directly to history, leaves pending empty", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "inject CPU" },
      { type: "TOKEN_APPENDED", content: "Working on it", node: "agent" },
      {
        type: "RESULT_RECEIVED",
        content: JSON.stringify({
          status: "success",
          data: { task_id: "task-X", task_state: "injected" },
        }),
        taskId: "task-X",
      },
    ]);
    // Pending must be empty after RESULT_RECEIVED — result didn't queue
    // up alongside the agent message.
    expect(s.pending).toHaveLength(0);
    // history contains agent + result, in that order. Order matters:
    // result is the *outcome* of the agent's work, so it must appear
    // AFTER the agent message in scrollback.
    const kinds = s.history.map((it) => it.kind);
    const agentIdx = kinds.lastIndexOf("agent");
    const resultIdx = kinds.lastIndexOf("result");
    expect(agentIdx).toBeGreaterThan(-1);
    expect(resultIdx).toBeGreaterThan(agentIdx);
  });

  it("flushes an in-flight thinking session before committing result", () => {
    // commitThinking is called at the top of RESULT_RECEIVED so a
    // thinking session that was open when result arrived gets a
    // ``▸ Thought for Ns`` row in scrollback rather than being silently
    // discarded.
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      { type: "THINKING_APPENDED", content: "deciding…", node: "agent" },
      {
        type: "RESULT_RECEIVED",
        content: JSON.stringify({
          status: "success",
          data: { task_id: "t", task_state: "injected" },
        }),
        taskId: "t",
      },
    ]);
    const kinds = s.history.map((it) => it.kind);
    expect(kinds).toContain("thinking");
    expect(kinds).toContain("result");
    // thoughtBuffer must be cleared as a side-effect of commitThinking
    // — no leaked partial thinking state into TURN_DONE.
    expect(s.thoughtBuffer).toBe("");
  });

  it("does NOT reset streamState — turn is still in flight after RESULT_RECEIVED", () => {
    // The turn isn't over until TURN_DONE arrives behind result. If
    // RESULT_RECEIVED reset streamState to "idle", the LoadingIndicator
    // would visibly disappear mid-turn. Guard against future refactors
    // that try to reuse commitPending here.
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      {
        type: "RESULT_RECEIVED",
        content: JSON.stringify({
          status: "success",
          data: { task_id: "t", task_state: "injected" },
        }),
        taskId: "t",
      },
    ]);
    expect(s.streamState).toBe("responding");
  });
});

describe("reducer / TOKEN_APPENDED coalescing", () => {
  it("appends to a single trailing AgentItem, not multiple", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "TOKEN_APPENDED", content: "Hello", node: "n1" },
      { type: "TOKEN_APPENDED", content: ", ", node: "n1" },
      { type: "TOKEN_APPENDED", content: "world!", node: "n1" },
    ]);
    const agentItems = s.pending.filter((i) => i.kind === "agent");
    expect(agentItems).toHaveLength(1);
    if (agentItems[0]?.kind === "agent") {
      expect(agentItems[0].text).toBe("Hello, world!");
    }
  });

  it("commits the head fragment to history when an extending token crosses a \\n\\n boundary", () => {
    // Phase 2.3 — mid-stream Static commit. First TOKEN_APPENDED
    // creates the agent tail. Second TOKEN_APPENDED extends past
    // a paragraph boundary; findLastSafeSplitPoint returns the offset
    // just past the \n\n, so the head fragment ("Para one.\n\n")
    // moves to history and the tail ("Para two streaming") becomes
    // a continuation AgentItem in pending.
    //
    // The split deliberately only fires when we have an existing
    // agent tail to extend — see the reducer comment for why
    // (chronology with non-agent items in pending).
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "TOKEN_APPENDED", content: "Para one.", node: "n" },
      { type: "TOKEN_APPENDED", content: "\n\nPara two streaming", node: "n" },
    ]);
    const inHistory = s.history.filter((i) => i.kind === "agent");
    const inPending = s.pending.filter((i) => i.kind === "agent");
    expect(inHistory).toHaveLength(1);
    expect(inPending).toHaveLength(1);
    if (inHistory[0]?.kind === "agent") {
      expect(inHistory[0].text).toBe("Para one.\n\n");
      // First fragment keeps the ⏺ glyph — continuation is unset/false.
      expect(inHistory[0].continuation ?? false).toBe(false);
    }
    if (inPending[0]?.kind === "agent") {
      expect(inPending[0].text).toBe("Para two streaming");
      // Tail is a continuation — no glyph, no marginTop in render.
      expect(inPending[0].continuation).toBe(true);
    }
  });

  it("does NOT split the very first TOKEN_APPENDED of an agent reply", () => {
    // Without an existing agent tail to extend, the split is deferred
    // to the next token — even when the first chunk already contains
    // a paragraph break. This preserves chronology when pending
    // contains non-agent items (e.g. a running tool group) that would
    // otherwise be reordered behind a prematurely-committed head.
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "TOKEN_APPENDED", content: "Para one.\n\nPara two", node: "n" },
    ]);
    const inHistory = s.history.filter((i) => i.kind === "agent");
    const inPending = s.pending.filter((i) => i.kind === "agent");
    expect(inHistory).toHaveLength(0);
    expect(inPending).toHaveLength(1);
    if (inPending[0]?.kind === "agent") {
      expect(inPending[0].text).toBe("Para one.\n\nPara two");
      expect(inPending[0].continuation ?? false).toBe(false);
    }
  });

  it("does NOT split while a running tool group sits in front of the agent tail", () => {
    // Chronology guard: if a running tool group is in pending before
    // the agent tail (rare but legal — server may stream a token while
    // tool is still in flight, or a future LangGraph topology may
    // interleave them), committing the head fragment to history NOW
    // would put it ahead of the toolGroup in scrollback when the
    // group eventually flushes. The split must wait until the tail
    // is alone in pending.
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      { type: "TOKEN_APPENDED", content: "Para one.", node: "n" },
      { type: "TOKEN_APPENDED", content: "\n\nPara two streaming", node: "n" },
    ]);
    // Nothing should land in history — toolGroup is still running so
    // flushLeadingStable can't drain pending past it, and our split
    // guard sees pending.length > 1 and refuses to commit a head.
    const inHistoryAgents = s.history.filter((i) => i.kind === "agent");
    expect(inHistoryAgents).toHaveLength(0);
    // Agent text accumulated in pending as a single item.
    const inPendingAgents = s.pending.filter((i) => i.kind === "agent");
    expect(inPendingAgents).toHaveLength(1);
    if (inPendingAgents[0]?.kind === "agent") {
      expect(inPendingAgents[0].text).toBe("Para one.\n\nPara two streaming");
      expect(inPendingAgents[0].continuation ?? false).toBe(false);
    }
  });

  it("chains continuations across consecutive splits", () => {
    // Three paragraphs spread across three TOKEN_APPENDED events:
    //   Para A → first event creates initial AgentItem
    //   "\n\nPara B" → split fires: A→history, B(continuation)→pending
    //   "\n\nPara C streaming" → split fires AGAIN: B→history (continuation
    //     inherited as true → still no glyph), C(continuation)→pending
    //
    // Verifies that subsequent splits propagate ``continuation: true``
    // through the head fragment so multi-paragraph replies don't
    // sprout new ⏺ glyphs mid-block.
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "TOKEN_APPENDED", content: "Para A.", node: "n" },
      { type: "TOKEN_APPENDED", content: "\n\nPara B.", node: "n" },
      { type: "TOKEN_APPENDED", content: "\n\nPara C streaming", node: "n" },
    ]);
    const inHistory = s.history.filter((i) => i.kind === "agent");
    const inPending = s.pending.filter((i) => i.kind === "agent");
    expect(inHistory).toHaveLength(2);
    expect(inPending).toHaveLength(1);
    if (inHistory[0]?.kind === "agent") {
      // First head — original ⏺ glyph carrier.
      expect(inHistory[0].text).toBe("Para A.\n\n");
      expect(inHistory[0].continuation ?? false).toBe(false);
    }
    if (inHistory[1]?.kind === "agent") {
      // Second head — continuation propagates from the prior tail.
      expect(inHistory[1].text).toBe("Para B.\n\n");
      expect(inHistory[1].continuation).toBe(true);
    }
    if (inPending[0]?.kind === "agent") {
      expect(inPending[0].text).toBe("Para C streaming");
      expect(inPending[0].continuation).toBe(true);
    }
  });

  it("does NOT split when the tail is inside an unclosed code block", () => {
    // After the first chunk establishes the agent tail, the second
    // chunk adds an unclosed fence. Splitting would orphan the open
    // fence — findLastSafeSplitPoint returns the start of the block,
    // so the split DOES fire but only commits the prose before the
    // fence; the code block stays in pending until it closes.
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "TOKEN_APPENDED", content: "Para one.", node: "n" },
      {
        type: "TOKEN_APPENDED",
        content: "\n\n```python\ndef streaming():",
        node: "n",
      },
    ]);
    const inHistory = s.history.filter((i) => i.kind === "agent");
    const inPending = s.pending.filter((i) => i.kind === "agent");
    expect(inHistory).toHaveLength(1);
    expect(inPending).toHaveLength(1);
    if (inHistory[0]?.kind === "agent") {
      expect(inHistory[0].text).toBe("Para one.\n\n");
    }
    if (inPending[0]?.kind === "agent") {
      // The open-fence content remained in pending; only prose before
      // the fence was committed.
      expect(inPending[0].text).toBe("```python\ndef streaming():");
      expect(inPending[0].continuation).toBe(true);
    }
  });

  it("starts a fresh AgentItem after a tool group breaks the streak", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "TOKEN_APPENDED", content: "first", node: "n" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      { type: "TOKEN_APPENDED", content: "second", node: "n" },
    ]);
    // After the agent-not-tail flush rule, the first AgentItem is no
    // longer the trailing item once the second TOKEN_APPENDED creates
    // a new AgentItem behind the (still-running) tool_group; the
    // older ``first`` agent flushes to history. The newer ``second``
    // stays in pending as the live trailing item. The contract is
    // "two distinct agent items" — we just check that across both
    // halves, AND verify the older one really did land in history.
    const allAgents = [...s.history, ...s.pending].filter(
      (i) => i.kind === "agent",
    );
    expect(allAgents).toHaveLength(2);
    // First agent (text "first") is in history (flushed by stable
    // rule); second (text "second") is still in pending.
    const inHistory = s.history.filter((i) => i.kind === "agent");
    const inPending = s.pending.filter((i) => i.kind === "agent");
    expect(inHistory).toHaveLength(1);
    expect(inPending).toHaveLength(1);
  });

  describe("short-agent protection (Qwen reasoning/content interleave)", () => {
    // Guards the MIN_AGENT_LEN_FOR_SPLIT=50 rule added 2026-05-26 after
    // task-f8320b6ff844 showed orphan blocks from interleaved thinking:
    // Qwen's enable_thinking mode emits reasoning_content chunks
    // interleaved with content chunks. Without this guard, the second
    // token's commitThinking + flushLeadingStable would flush the
    // short agent to history, leaving the rest of the same logical
    // reply as a separate agent block.

    it("keeps agent intact when mid-content thinking arrives on a SHORT agent (<50 chars)", () => {
      const s = fold([
        { type: "TURN_STARTED", input: "hi" },
        // First reasoning burst — commits as ThinkingItem on the next
        // TOKEN_APPENDED ("I"), which is expected (agent is empty so
        // protection doesn't apply yet).
        { type: "THINKING_APPENDED", content: "thinking 4s", node: "n" },
        { type: "TOKEN_APPENDED", content: "I", node: "n" },
        // Second reasoning burst arriving mid-content. With the guard,
        // this should NOT commit a ThinkingItem because the agent is
        // still 1 char (< 50) — the buffer is preserved.
        { type: "THINKING_APPENDED", content: "thinking <1s", node: "n" },
        { type: "TOKEN_APPENDED", content: " acknowledge", node: "n" },
      ]);
      const allAgents = [...s.history, ...s.pending].filter(
        (i) => i.kind === "agent",
      );
      // Single connected agent — no "I" orphan.
      expect(allAgents).toHaveLength(1);
      const agent = allAgents[0];
      if (agent?.kind === "agent") {
        expect(agent.text).toBe("I acknowledge");
      }
      // Mid-content thinking is suppressed: commitThinking sees
      // suppressMidContentThinking=true and clears the buffer without
      // creating a ThinkingItem.
      expect(s.thoughtBuffer).toBe("");
    });

    it("suppresses mid-content thinking even when agent exceeds threshold", () => {
      // With suppressMidContentThinking, mid-content reasoning chunks
      // are discarded regardless of agent length — no trailing or
      // interleaved ThinkingItems.
      const longText =
        "This is a sufficiently long agent response that exceeds the fifty character threshold for splitting";
      const s = fold([
        { type: "TURN_STARTED", input: "hi" },
        { type: "THINKING_APPENDED", content: "early thought", node: "n" },
        // Agent grows past 50 chars in a single TOKEN_APPENDED.
        {
          type: "TOKEN_APPENDED",
          content: longText,
          node: "n",
        },
        // Mid-content thinking — suppressed by the flag.
        { type: "THINKING_APPENDED", content: "mid", node: "n" },
        { type: "TOKEN_APPENDED", content: " continuing", node: "n" },
      ]);
      // Only the initial "early thought" produces a ThinkingItem.
      // The "mid" thinking is suppressed.
      const thinking = [...s.history, ...s.pending].filter(
        (i) => i.kind === "thinking",
      );
      expect(thinking).toHaveLength(1);
      // Agent text continues seamlessly (no split).
      const agents = [...s.history, ...s.pending].filter(
        (i) => i.kind === "agent",
      );
      expect(agents).toHaveLength(1);
      if (agents[0]?.kind === "agent") {
        expect(agents[0].text).toBe(longText + " continuing");
      }
      // Buffer is drained (discarded by suppress, not committed).
      expect(s.thoughtBuffer).toBe("");
    });

    it("keeps CJK mid-sentence fragment intact (regression: 12-char split)", () => {
      // Reproduces the user-reported rendering issue: Qwen emits
      // "我必须拒绝这个请求。作为" (12 chars) then a second thinking
      // chunk, then the rest of the response. With threshold=8, this
      // split; with threshold=50, it stays intact.
      const s = fold([
        { type: "TURN_STARTED", input: "全集群注入" },
        { type: "THINKING_APPENDED", content: "analyzing request", node: "n" },
        { type: "TOKEN_APPENDED", content: "我必须拒绝这个请求。作为", node: "n" },
        { type: "THINKING_APPENDED", content: "safety check", node: "n" },
        { type: "TOKEN_APPENDED", content: "专业工具，我需要指出安全风险。", node: "n" },
      ]);
      const allAgents = [...s.history, ...s.pending].filter(
        (i) => i.kind === "agent",
      );
      expect(allAgents).toHaveLength(1);
      if (allAgents[0]?.kind === "agent") {
        expect(allAgents[0].text).toBe(
          "我必须拒绝这个请求。作为专业工具，我需要指出安全风险。",
        );
      }
      // Mid-content thinking is suppressed: commitThinking clears
      // the buffer on the second TOKEN_APPENDED dispatch.
      expect(s.thoughtBuffer).toBe("");
    });

    it("suppresses deferred thinking at TURN_DONE (no trailing ThinkingItem)", () => {
      const s = fold([
        { type: "TURN_STARTED", input: "hi" },
        { type: "THINKING_APPENDED", content: "ephemeral", node: "n" },
        { type: "TOKEN_APPENDED", content: "x", node: "n" },
        // Second thinking arrives mid-content. Suppress is armed
        // after TOKEN_APPENDED, so this thinking is discarded.
        { type: "THINKING_APPENDED", content: "deferred", node: "n" },
        { type: "TURN_DONE" },
      ]);
      // Only the initial "ephemeral" thinking produces a ThinkingItem.
      // The "deferred" mid-content thinking is suppressed — no trailing
      // "▸ 思考用时 <1s" after the response.
      const thinking = [...s.history, ...s.pending].filter(
        (i) => i.kind === "thinking",
      );
      expect(thinking).toHaveLength(1);
      // Buffer is drained at turn end.
      expect(s.thoughtBuffer).toBe("");
    });
  });
});

describe("reducer / TOOL_STARTED + TOOL_ENDED matching", () => {
  it("groups consecutive TOOL_STARTED into one ToolGroupItem", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      { type: "TOOL_STARTED", callId: "c2", name: "blade", node: "n" },
    ]);
    const groups = s.pending.filter((i) => i.kind === "tool_group");
    expect(groups).toHaveLength(1);
    if (groups[0]?.kind === "tool_group") {
      expect(groups[0].tools).toHaveLength(2);
    }
  });

  it("matches TOOL_ENDED by callId when present", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      { type: "TOOL_STARTED", callId: "c2", name: "kubectl", node: "n" },
      // Same name, different callId — must match c2 only.
      {
        type: "TOOL_ENDED",
        callId: "c2",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
    ]);
    const group = s.pending.find((i) => i.kind === "tool_group");
    if (group?.kind === "tool_group") {
      const c1 = group.tools.find((t) => t.callId === "c1");
      const c2 = group.tools.find((t) => t.callId === "c2");
      expect(c1?.status).toBe("running");
      expect(c2?.status).toBe("success");
    } else {
      throw new Error("expected tool_group in pending");
    }
  });

  it("falls back to name match when callId mismatches (tool followed by interrupt)", () => {
    // Reproduces the ``submit_fault_intent`` ghosting:
    // LangGraph's astream_events can emit ``on_tool_end`` with a
    // different ``run_id`` than the preceding ``on_tool_start`` when
    // the tool's owning node transitions immediately into an
    // ``interrupt()`` (intent_clarification → intent_confirm). Strict
    // callId match misses → tool stays "running" forever even after
    // the user answers the confirm card. The two-pass matcher
    // (strict callId, then name fallback) recovers the state machine
    // without requiring server-side run_id parity.
    const s = fold([
      { type: "TURN_STARTED", input: "submit fault" },
      {
        type: "TOOL_STARTED",
        callId: "run-A",
        name: "submit_fault_intent",
        node: "clarification_tools",
      },
      {
        type: "TOOL_ENDED",
        callId: "run-B",
        name: "submit_fault_intent",
        status: "success",
        content: "✓ submitted",
      },
    ]);
    const group = [...s.history, ...s.pending].find(
      (i) => i.kind === "tool_group",
    );
    expect(group).toBeDefined();
    if (group && group.kind === "tool_group") {
      expect(group.tools[0]?.status).toBe("success");
      expect(group.tools[0]?.raw).toContain("submitted");
    }
  });

  it("does NOT mis-match by name when strict callId match succeeds", () => {
    // Two concurrent invocations of the same tool with distinct
    // callIds. A TOOL_ENDED carrying ``run-2`` must end ONLY ``run-2``,
    // not the older ``run-1`` that's still running. The strict pass
    // should hit; the name fallback shouldn't fire.
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "TOOL_STARTED", callId: "run-1", name: "kubectl", node: "n" },
      { type: "TOOL_STARTED", callId: "run-2", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "run-2",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
    ]);
    const group = [...s.history, ...s.pending].find(
      (i) => i.kind === "tool_group",
    );
    if (group && group.kind === "tool_group") {
      const r1 = group.tools.find((t) => t.callId === "run-1");
      const r2 = group.tools.find((t) => t.callId === "run-2");
      expect(r1?.status).toBe("running");
      expect(r2?.status).toBe("success");
    }
  });

  it("falls back to name match when callId is empty", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
    ]);
    // After TOOL_ENDED the leading-stable flush moves the now-done
    // tool_group straight to history — pending is empty. Look in
    // both halves so this test passes regardless of which side the
    // flush lands the group.
    const group = [...s.history, ...s.pending].find(
      (i) => i.kind === "tool_group",
    );
    expect(group).toBeDefined();
    if (group && group.kind === "tool_group") {
      expect(group.tools[0]?.status).toBe("success");
    }
  });
});

describe("reducer / PhaseStepper", () => {
  it("does NOT materialise the stepper for an intent-only turn", () => {
    // Chat / capability Q&A turns never leave intent_clarification —
    // showing a 5-step progress strip would mislead the user about
    // the scope of the turn.
    const s = fold([
      { type: "TURN_STARTED", input: "你好" },
      { type: "NODE_STARTED", node: "intent_clarification", phase: "intent" },
      { type: "TOKEN_APPENDED", content: "你好！", node: "intent_clarification" },
    ]);
    // Stepper now lives in its own slot, not in pending. Verify both:
    // mid-turn lookup is null, and pending stays clean of any
    // ``phase_stepper`` items.
    expect(s.currentPhaseStepper).toBeNull();
    expect(s.pending.find((p) => p.kind === "phase_stepper")).toBeUndefined();
  });

  it("materialises the stepper on the first non-intent step", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "inject cpu" },
      { type: "NODE_STARTED", node: "intent_clarification", phase: "intent" },
      // Layer-1 confirm wait — graph tags ``intent_confirm`` with
      // ``phase=safety`` so the strip can paint while the user reads
      // the confirm card, but mapNodeToStep demotes it back to
      // ``intent`` (it's confirming intent, not running a safety
      // check). So this step does NOT materialise the stepper.
      { type: "NODE_STARTED", node: "intent_confirm", phase: "safety" },
    ]);
    expect(s.currentPhaseStepper).toBeNull();

    // The first non-intent step (agent_loop) finally materialises it.
    const after = fold(
      [{ type: "NODE_STARTED", node: "agent_loop", phase: "inject" }],
      s,
    );
    const stepper = after.currentPhaseStepper;
    expect(stepper).not.toBeNull();
    // Mid-turn stepper lives in its own slot, never in pending —
    // see ``currentPhaseStepper`` JSDoc for the leading-stable-flush
    // rationale.
    expect(after.pending.find((p) => p.kind === "phase_stepper")).toBeUndefined();
    if (stepper && stepper.kind === "phase_stepper") {
      expect(stepper.mode).toBe("inject");
      expect(stepper.steps).toHaveLength(5);
      expect(stepper.steps.map((x) => x.phase)).toEqual([
        "intent",
        "agent_loop",
        "safety",
        "execute",
        "verify",
      ]);
      // intent → completed (the run already moved past intent), the
      // active step (agent_loop) → in_progress, downstream pending.
      expect(stepper.steps.map((x) => x.status)).toEqual([
        "completed",
        "in_progress",
        "pending",
        "pending",
        "pending",
      ]);
    }
  });

  it("repaints the same stepper instance on subsequent step transitions", () => {
    // Walks the full real-graph sequence (matching ``graph.py``):
    // agent_loop → safety_check → confirmation_gate → execute_loop →
    // verifier_loop. Each transition should advance the strip by
    // exactly one row.
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      { type: "NODE_STARTED", node: "agent_loop", phase: "inject" },
      { type: "NODE_STARTED", node: "safety_check", phase: "safety" },
      { type: "NODE_STARTED", node: "execute_loop", phase: "inject" },
      { type: "NODE_STARTED", node: "verifier_loop", phase: "verify" },
    ]);
    const stepper = s.currentPhaseStepper;
    expect(stepper).not.toBeNull();
    if (stepper) {
      expect(stepper.steps.map((x) => x.status)).toEqual([
        "completed", // intent
        "completed", // agent_loop
        "completed", // safety
        "completed", // execute
        "in_progress", // verify
      ]);
    }
  });

  it("does NOT regress already-progressed steps when an earlier step replays", () => {
    // LangGraph re-emits earlier phase events on Command(resume=...)
    // after an interrupt (the same replay channel that produced
    // duplicate ToolGroup cards before the TOOL_STARTED guard). When
    // a replayed ``agent_loop`` event arrives after the stepper has
    // already advanced to ``safety``, the active row must NOT roll
    // back — otherwise the user sees the strip flicker back and
    // re-progress.
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      { type: "NODE_STARTED", node: "agent_loop", phase: "inject" },
      { type: "NODE_STARTED", node: "safety_check", phase: "safety" },
      // Replay frame after a confirmation-gate resume:
      { type: "NODE_STARTED", node: "agent_loop", phase: "inject" },
    ]);
    const stepper = s.currentPhaseStepper;
    expect(stepper).not.toBeNull();
    if (stepper) {
      // agent_loop stays completed (NOT regressed to in_progress);
      // safety stays in_progress (NOT regressed to pending).
      expect(stepper.steps.map((x) => x.status)).toEqual([
        "completed", // intent
        "completed", // agent_loop
        "in_progress", // safety
        "pending", // execute
        "pending", // verify
      ]);
    }
  });

  it("does NOT create a stepper for the recovery phase", () => {
    // Recover invocations are a separate graph and a separate task_id
    // space from the original injection. PendingTasksCard at boot
    // already surfaces unfinished tasks; ``blade --timeout`` provides
    // time-bounded auto-cleanup. Showing a stepper here would imply
    // the inject pipeline is "still running" — false. Future recover-
    // mode stepper is reserved for ``mode: "recover"``.
    const s = fold([
      { type: "TURN_STARTED", input: "/recover task-abc" },
      { type: "NODE_STARTED", node: "recover_verifier_loop", phase: "recovery" },
    ]);
    expect(s.currentPhaseStepper).toBeNull();
    expect(s.pending.find((p) => p.kind === "phase_stepper")).toBeUndefined();
  });

  it("ignores re-entry into the same step (no churn)", () => {
    // The ``execute`` step covers multiple nodes — baseline_capture,
    // execute_loop, direct_execute. Each emits its own NODE_STARTED
    // but the strip should stay parked on the same row.
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      { type: "NODE_STARTED", node: "agent_loop", phase: "inject" },
      { type: "NODE_STARTED", node: "safety_check", phase: "safety" },
      { type: "NODE_STARTED", node: "baseline_capture", phase: "inject" },
      // Same ``execute`` step, different node — must NOT advance.
      { type: "NODE_STARTED", node: "execute_loop", phase: "inject" },
    ]);
    const stepper = s.currentPhaseStepper;
    expect(stepper).not.toBeNull();
    if (stepper) {
      // Strip parked on ``execute`` (index 3). agent_loop / safety
      // already completed; verify still pending.
      expect(stepper.steps.map((x) => x.status)).toEqual([
        "completed", // intent
        "completed", // agent_loop
        "completed", // safety
        "in_progress", // execute
        "pending", // verify
      ]);
    }
  });

  it("rounds every step up to completed on TURN_DONE", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      { type: "NODE_STARTED", node: "agent_loop", phase: "inject" },
      { type: "NODE_STARTED", node: "verifier_loop", phase: "verify" },
      { type: "TURN_DONE" },
    ]);
    const stepper = s.history.find((p) => p.kind === "phase_stepper");
    expect(stepper).toBeDefined();
    if (stepper && stepper.kind === "phase_stepper") {
      // Successful turn: ``in_progress`` and any later ``pending`` are
      // both rounded up to ``completed``. The pipeline reached its
      // terminal node by definition. Skipped intermediate steps
      // (safety / execute were never explicitly fired in this stub
      // trajectory) also flip to completed under the round-up rule.
      expect(stepper.steps.every((x) => x.status === "completed")).toBe(true);
      expect(stepper.steps).toHaveLength(5);
    }
  });

  it("marks the active step failed and leaves later pending steps untouched on TURN_ABORTED", () => {
    // Aborted mid-inject: Esc / network drop / unhandled exception.
    // The strip must NOT optimistically round everything up — that
    // contradicts the ResultCard's ``Injection failed`` and hides
    // *where* the pipeline actually broke.
    const s = fold([
      { type: "TURN_STARTED", input: "inject cpu" },
      { type: "NODE_STARTED", node: "agent_loop", phase: "inject" },
      { type: "TURN_ABORTED", reason: "Cancelled by user" },
    ]);
    const stepper = s.history.find((p) => p.kind === "phase_stepper");
    expect(stepper).toBeDefined();
    if (stepper && stepper.kind === "phase_stepper") {
      // Honest record: intent completed, agent_loop failed (where
      // the abort caught us), the rest never ran.
      expect(stepper.steps.map((x) => [x.phase, x.status])).toEqual([
        ["intent", "completed"],
        ["agent_loop", "failed"],
        ["safety", "pending"],
        ["execute", "pending"],
        ["verify", "pending"],
      ]);
    }
  });

  it("flushes thinking/tool_group from pending even while the stepper is active mid-turn", () => {
    // Regression for the ink-fullscreen-redraw flicker. The stepper
    // used to live at pending[0]; while it was there, the
    // leading-stable flush in TOKEN_APPENDED couldn't peel anything
    // off because the stepper itself is never stable until TURN_DONE.
    // The fix moves the stepper into ``state.currentPhaseStepper``
    // so pending starts with stable items that DO flush — keeping
    // the dynamic-area output height under ``stdout.rows`` and
    // dodging Ink's fullscreen redraw branch.
    const s = fold([
      { type: "TURN_STARTED", input: "inject cpu" },
      // Stepper materialises (lives outside pending now). agent_loop
      // is the first step that actually creates the strip — Layer-1
      // ``intent_confirm`` is mapped back to the ``intent`` step and
      // never materialises on its own.
      { type: "NODE_STARTED", node: "agent_loop", phase: "inject" },
      // A thinking session + a tool call land in pending.
      { type: "THINKING_APPENDED", content: "decomposing…", node: "n" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c1",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
      // First agent token triggers the leading-stable flush.
      { type: "TOKEN_APPENDED", content: "Here", node: "n" },
    ]);
    // Stepper is still live in its own slot — not flushed yet.
    expect(s.currentPhaseStepper).not.toBeNull();
    // Thinking + completed tool_group flushed past it into history.
    expect(s.history.filter((i) => i.kind === "thinking")).toHaveLength(1);
    expect(s.history.filter((i) => i.kind === "tool_group")).toHaveLength(1);
    // Pending is now small (just the streaming agent item) — well
    // under any plausible stdout.rows, so Ink's fullscreen branch
    // doesn't trip per frame.
    expect(s.pending.filter((i) => i.kind === "thinking")).toHaveLength(0);
    expect(s.pending.filter((i) => i.kind === "tool_group")).toHaveLength(0);
    expect(s.pending.filter((i) => i.kind === "agent")).toHaveLength(1);
  });

  it("flushes pending built up across multiple phase transitions on first TOKEN_APPENDED", () => {
    // Realistic inject-turn shape: tool calls accumulate across
    // several phases (safety → inject → verify), with NO token text
    // until the very end. The dedicated ``currentPhaseStepper`` slot
    // must absorb every phase transition without ever pushing into
    // pending — so that when the FIRST token finally arrives,
    // pending starts with a stable item (thinking_0) and the
    // leading-stable flush peels every accumulated round into
    // history in one sweep.
    //
    // Failure mode this guards against: a future "helpful" change
    // that re-injects the stepper into pending on phase transition
    // (e.g. for ordering convenience) would land it at the front,
    // blocking the flush exactly the way the original bug did and
    // re-introducing the Ink fullscreen-redraw flicker.
    const s = fold([
      { type: "TURN_STARTED", input: "inject cpu" },
      // Step 1: agent_loop — stepper materialises in slot.
      { type: "NODE_STARTED", node: "agent_loop", phase: "inject" },
      { type: "THINKING_APPENDED", content: "planning…", node: "n" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c1",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
      // Step 2: safety_check — strip advances to ``safety``.
      { type: "NODE_STARTED", node: "safety_check", phase: "safety" },
      { type: "THINKING_APPENDED", content: "checking…", node: "n" },
      { type: "TOOL_STARTED", callId: "c2", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c2",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
      // Step 3: execute_loop — strip advances to ``execute``.
      { type: "NODE_STARTED", node: "execute_loop", phase: "inject" },
      { type: "THINKING_APPENDED", content: "executing…", node: "n" },
      { type: "TOOL_STARTED", callId: "c3", name: "blade_create", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c3",
        name: "blade_create",
        status: "success",
        content: "ok",
      },
      // Step 4: verifier_loop — strip advances to ``verify``.
      { type: "NODE_STARTED", node: "verifier_loop", phase: "verify" },
      { type: "THINKING_APPENDED", content: "verifying…", node: "n" },
      { type: "TOOL_STARTED", callId: "c4", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c4",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
      // First token arrives — should drain everything stable from
      // pending in a single leading-stable sweep.
      { type: "TOKEN_APPENDED", content: "All done.", node: "n" },
    ]);
    // Strip remained in its slot through every transition; index 4
    // in INJECT_PHASE_ORDER ([intent, agent_loop, safety, execute,
    // verify]) is the ``verify`` step.
    expect(s.currentPhaseStepper).not.toBeNull();
    expect(s.currentPhaseStepper?.steps[4]?.status).toBe("in_progress");
    // All four thinking sessions + all four tool_groups peeled out
    // of pending and into history on that first flush.
    expect(s.history.filter((i) => i.kind === "thinking")).toHaveLength(4);
    expect(s.history.filter((i) => i.kind === "tool_group")).toHaveLength(4);
    // Pending shrunk down to just the trailing streamed agent
    // block; no stable item left behind, no phase_stepper either.
    expect(s.pending.filter((i) => i.kind === "thinking")).toHaveLength(0);
    expect(s.pending.filter((i) => i.kind === "tool_group")).toHaveLength(0);
    expect(s.pending.filter((i) => i.kind === "phase_stepper")).toHaveLength(0);
    expect(s.pending.filter((i) => i.kind === "agent")).toHaveLength(1);
  });

  it("does not move pending into history on phase transition alone", () => {
    // NODE_STARTED is purely a stepper-slot mutation — it must NEVER
    // itself trigger the leading-stable flush. The flush triggers
    // changed over time; today they are TOKEN_APPENDED, TOOL_STARTED,
    // and TOOL_ENDED. NODE_STARTED is verified by the dispatch order
    // below: the TOOL_ENDED at index 5 has already flushed everything,
    // so the two NODE_STARTED actions that follow operate on an empty
    // pending and produce no further flushes — exactly the invariant.
    const s = fold([
      { type: "TURN_STARTED", input: "inject cpu" },
      { type: "NODE_STARTED", node: "agent_loop", phase: "inject" },
      { type: "THINKING_APPENDED", content: "thinking", node: "n" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c1",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
      // After TOOL_ENDED's flush: history has [user, Thinking, ToolGroup].
      // Two step transitions follow — pending stays empty, history
      // unchanged.
      { type: "NODE_STARTED", node: "execute_loop", phase: "inject" },
      { type: "NODE_STARTED", node: "verifier_loop", phase: "verify" },
    ]);
    // Strip rolled forward to verify (index 4 in the 5-step layout).
    expect(s.currentPhaseStepper?.steps[4]?.status).toBe("in_progress");
    // Stable items already flushed by TOOL_ENDED — pending empty.
    expect(s.pending).toHaveLength(0);
    // History has the flushed pair plus the user echo.
    expect(s.history.filter((i) => i.kind === "thinking")).toHaveLength(1);
    expect(s.history.filter((i) => i.kind === "tool_group")).toHaveLength(1);
  });

  it("appends the finalised stepper as the LAST item of the turn block at TURN_DONE", () => {
    // Placement invariant: stepper lands AFTER tool/thinking/agent
    // items (which already flushed mid-turn via the leading-stable
    // path) AND AFTER the optional ``turn_usage`` summary — i.e.
    // the strip is the last item before the next prompt. The
    // user-facing rule: "the todo strip is always glued to the
    // input box". An older revision sandwiched the token tally
    // between the strip and the input, which read as "the strip
    // belongs to the previous turn". Strict
    // chronological ordering (stepper right after user echo) is
    // unattainable with Ink's append-only Static — the
    // leading-stable flush has already deposited downstream items
    // into history before commitPending sees the stepper.
    const s = fold([
      { type: "TURN_STARTED", input: "inject cpu" },
      { type: "NODE_STARTED", node: "agent_loop", phase: "inject" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c1",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
      { type: "TOKEN_APPENDED", content: "Done.", node: "n" },
      { type: "USAGE_RECEIVED", inputTokens: 100, outputTokens: 50 },
      { type: "TURN_DONE" },
    ]);
    const userIdx = s.history.findIndex((i) => i.kind === "user");
    const toolIdx = s.history.findIndex((i) => i.kind === "tool_group");
    const agentIdx = s.history.findIndex((i) => i.kind === "agent");
    const stepperIdx = s.history.findIndex((i) => i.kind === "phase_stepper");
    const usageIdx = s.history.findIndex((i) => i.kind === "turn_usage");
    expect(userIdx).toBeGreaterThanOrEqual(0);
    expect(toolIdx).toBeGreaterThan(userIdx);
    expect(agentIdx).toBeGreaterThan(toolIdx);
    // Token tally is BEFORE the stepper now — the stepper is the
    // last row, sitting directly above the InputPrompt.
    expect(usageIdx).toBeGreaterThan(agentIdx);
    expect(stepperIdx).toBeGreaterThan(usageIdx);
    // Stepper is the very last entry of the turn block (no other
    // items appear after it; agent-loop / tool / usage all precede
    // it, and the next user echo opens a fresh turn).
    expect(stepperIdx).toBe(s.history.length - 1);
    // Slot cleared after commit.
    expect(s.currentPhaseStepper).toBeNull();
  });

  it("preserves a clean prefix on TURN_ABORTED when no step is active", () => {
    // Edge case: turn aborts BEFORE any non-intent phase fires (user
    // hits Esc during the very first kubectl call). The stepper never
    // materialises in the first place, so the failed-finalise path
    // doesn't run — verify the assertion holds.
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      { type: "NODE_STARTED", node: "intent_clarification", phase: "intent" },
      { type: "TURN_ABORTED", reason: "Cancelled" },
    ]);
    expect(s.history.find((p) => p.kind === "phase_stepper")).toBeUndefined();
  });

  it("marks active step failed on TURN_DONE when user rejected at a confirm card", () => {
    // Regression: when the user clicks "reject" on Layer-2
    // ``confirmation_gate``, the server graph routes through the
    // ``reject`` node to END — which the TS side observes as a
    // *clean* ``done`` event → TURN_DONE. Before this fix, the
    // stepper finalised with ``failed=false`` and rounded EVERY
    // step up to ``completed``, painting a misleading "all green
    // ✓" strip even though execute / verify never ran. Users
    // (correctly) complained: "我拒绝了为什么 todos 还是全勾".
    //
    // Tracking the rejection in ``state.currentTurnRejected`` and
    // OR-ing it into the ``failed`` flag passed to
    // ``finalisePhaseStepper`` produces the honest scrollback:
    // ``[✓ ✓ ✗ ○ ○]`` — intent + agent_loop completed, safety
    // marked failed (where the user said no), execute + verify
    // stay pending (they truly never ran).
    const s = fold([
      { type: "TURN_STARTED", input: "inject cpu" },
      // Layer-1 approved.
      { type: "NODE_STARTED", node: "agent_loop", phase: "inject" },
      { type: "NODE_STARTED", node: "safety_check", phase: "safety" },
      // Layer-2 confirm card displayed.
      {
        type: "CONFIRM_RECEIVED",
        content: "Confirm execution plan",
        node: "confirmation_gate",
      },
      // User rejects Layer 2.
      {
        type: "CONFIRM_USER_DECIDED",
        taskId: "task-x",
        answer: "rejected",
      },
      { type: "CONFIRM_RESOLVED", taskId: "task-x", answer: "rejected" },
      // Server's reject → END → ``done`` event → TURN_DONE.
      { type: "TURN_DONE" },
    ]);
    const stepper = s.history.find((p) => p.kind === "phase_stepper");
    expect(stepper).toBeDefined();
    if (stepper && stepper.kind === "phase_stepper") {
      expect(stepper.steps.map((x) => [x.phase, x.status])).toEqual([
        ["intent", "completed"],
        ["agent_loop", "completed"],
        ["safety", "failed"],
        ["execute", "pending"],
        ["verify", "pending"],
      ]);
    }
  });

  it("does NOT mark stepper failed when user approved every confirm card", () => {
    // Sanity counter-test: ``currentTurnRejected`` MUST flip back
    // to false on a subsequent ``approved`` decision so an
    // approved-then-approved sequence ends clean. Without the
    // latest-decision-wins semantics a Layer-1 reject followed by
    // a Layer-2 approve would erroneously paint the stepper as
    // failed.
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      { type: "NODE_STARTED", node: "agent_loop", phase: "inject" },
      // Layer-1 was rejected first (user changed their mind).
      {
        type: "CONFIRM_USER_DECIDED",
        taskId: "task-y",
        answer: "rejected",
      },
      // Layer-2 approved — latest decision wins.
      {
        type: "CONFIRM_USER_DECIDED",
        taskId: "task-y",
        answer: "approved",
      },
    ]);
    expect(s.currentTurnRejected).toBe(false);
  });

  it("resets currentTurnRejected on TURN_STARTED", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      {
        type: "CONFIRM_USER_DECIDED",
        taskId: "task-z",
        answer: "rejected",
      },
      { type: "TURN_DONE" },
      // Follow-up turn must NOT inherit the rejection.
      { type: "TURN_STARTED", input: "another inject" },
    ]);
    expect(s.currentTurnRejected).toBe(false);
  });

  it("ignores unknown phase strings", () => {
    // Older servers / future custom events may emit phases the TS
    // side doesn't model. We must not crash or pollute pending with
    // an unknown stepper.
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      // ``phase`` typing is intentionally ``string`` on the action so
      // legacy servers / new custom events compile without forcing a
      // schema bump; the reducer's ``isKnownPhase`` guard rejects
      // strings outside PHASE_ORDER at runtime.
      { type: "NODE_STARTED", node: "future_node", phase: "exotic" },
    ]);
    expect(s.pending.find((p) => p.kind === "phase_stepper")).toBeUndefined();
  });
});

describe("reducer / TOOL replay guard", () => {
  it("drops a TOOL_STARTED with a callId already seen this turn", () => {
    // Reproduces the LangGraph multi-interrupt replay scenario:
    // graph runs phase1_tools (one tool with run_id=run-1), pauses at
    // confirmation_gate, and on resume re-emits the run-1 tool_start
    // event. Without the guard the second TOOL_STARTED would create
    // a duplicate ToolGroup and the user sees phase1 cards twice.
    const s = fold([
      { type: "TURN_STARTED", input: "inject cpu" },
      { type: "TOOL_STARTED", callId: "run-1", name: "kubectl", node: "phase1" },
      { type: "TOOL_ENDED", callId: "run-1", name: "kubectl", status: "success", content: "ok" },
      // Replay frame after Command(resume="approved"):
      { type: "TOOL_STARTED", callId: "run-1", name: "kubectl", node: "phase1" },
    ]);
    // After TOOL_ENDED's leading-stable flush the original group is
    // in history; the replayed TOOL_STARTED is dropped by the
    // run-id guard so no new group materialises in pending.
    const groups = [...s.history, ...s.pending].filter(
      (p) => p.kind === "tool_group",
    );
    expect(groups).toHaveLength(1);
    if (groups[0]?.kind === "tool_group") {
      expect(groups[0].tools).toHaveLength(1);
      expect(groups[0].tools[0]?.status).toBe("success");
    }
    expect(s.seenToolCallIds).toContain("run-1");
  });

  it("treats the same callId as fresh again after a new TURN_STARTED", () => {
    // The seen-set is per turn: a follow-up turn from the user must
    // not inherit it, otherwise legitimate new tool calls would be
    // silently dropped if their (vanishingly unlikely) UUID happened
    // to clash. Reset on TURN_STARTED keeps the set bounded.
    const s = fold([
      { type: "TURN_STARTED", input: "first turn" },
      { type: "TOOL_STARTED", callId: "run-x", name: "kubectl", node: "phase1" },
      { type: "TURN_DONE" },
      { type: "TURN_STARTED", input: "second turn" },
    ]);
    expect(s.seenToolCallIds).toEqual([]);
  });

  it("does not deduplicate when callId is empty (legacy server)", () => {
    // Pre-call_id servers send empty run_ids; with no stable id we
    // fall back to the previous behaviour (every TOOL_STARTED creates
    // its own ToolItem). The guard must NOT fire on empty callIds or
    // it would collapse legitimate parallel tool calls into one.
    const s = fold([
      { type: "TURN_STARTED", input: "legacy" },
      { type: "TOOL_STARTED", callId: "", name: "kubectl", node: "phase1" },
      { type: "TOOL_STARTED", callId: "", name: "kubectl", node: "phase1" },
    ]);
    const group = s.pending.find((p) => p.kind === "tool_group");
    expect(group?.kind).toBe("tool_group");
    if (group?.kind === "tool_group") {
      expect(group.tools).toHaveLength(2);
    }
  });
});

describe("reducer / CONFIRM_RECEIVED", () => {
  it("appends a confirm_context to history (Layer 1 shape)", () => {
    // Post-split: heavy context body lands in Static history once;
    // pending only carries the live select widget.
    const payload = {
      type: "intent_confirm",
      fault_intent: { fault_type: "node-cpu-fullload" },
      summary: "...",
      intent_confidence: 0.9,
    };
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      {
        type: "CONFIRM_RECEIVED",
        content: "summary text",
        taskId: "task-1",
        node: "intent_confirm",
        payload,
      },
    ]);
    const ctx = s.history.find((p) => p.kind === "confirm_context");
    expect(ctx).toBeDefined();
    if (ctx && ctx.kind === "confirm_context") {
      expect(ctx.node).toBe("intent_confirm");
      expect(ctx.payload).toEqual(payload);
      expect(ctx.taskId).toBe("task-1");
    }
    const prompt = s.pending.find((p) => p.kind === "confirm_prompt");
    expect(prompt).toBeDefined();
    if (prompt && prompt.kind === "confirm_prompt") {
      expect(prompt.taskId).toBe("task-1");
      expect(prompt.resolved).toBe(false);
    }
    expect(s.streamState).toBe("waiting_confirmation");
  });

  it("appends a confirm_context to history (Layer 2 shape)", () => {
    const payload = {
      skill_name: "node-cpu-fullload",
      plan_summary: "blade create node cpu fullload",
      safety_status: "safe",
    };
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      {
        type: "CONFIRM_RECEIVED",
        content: payload.plan_summary,
        taskId: "task-2",
        node: "confirmation_gate",
        payload,
      },
    ]);
    const ctx = s.history.find((p) => p.kind === "confirm_context");
    expect(ctx).toBeDefined();
    if (ctx && ctx.kind === "confirm_context") {
      expect(ctx.node).toBe("confirmation_gate");
      expect(ctx.payload).toEqual(payload);
    }
  });

  // The next 3 tests lock in the CONFIRM_RECEIVED force-flush contract:
  // by the time confirm arrives, server has stopped emitting tokens for
  // this LLM call, so any agent / tool_group / thinking still in pending
  // must be force-flushed to history BEFORE pushing context — otherwise
  // (a) context shows ABOVE agent in scrollback (wrong chronological
  // order) and (b) the dyn frame stays bloated during the
  // waiting_confirmation window (more re-render cost, more risk of
  // Ink cursor desync).

  it("force-flushes a tail-agent into history before pushing context", () => {
    // Tail-agent is NOT eligible for flushLeadingStable
    // (``stable iff !isTail`` rule) so without the force-flush it
    // would stay in pending, and context would push past it,
    // reversing chronological order.
    const s = fold([
      { type: "TURN_STARTED", input: "inject CPU" },
      { type: "TOKEN_APPENDED", content: "I will inject", node: "agent" },
      {
        type: "CONFIRM_RECEIVED",
        content: "plan",
        taskId: "task-3",
        node: "confirmation_gate",
        payload: {},
      },
    ]);
    // Agent must be in history.
    const agentInHistory = s.history.filter((i) => i.kind === "agent");
    expect(agentInHistory).toHaveLength(1);
    // Pending must contain ONLY the new prompt — agent is gone.
    expect(s.pending.filter((i) => i.kind === "agent")).toHaveLength(0);
    expect(s.pending.filter((i) => i.kind === "confirm_prompt")).toHaveLength(1);
  });

  it("preserves chronological order: agent BEFORE context in history", () => {
    // Agent text was streamed first chronologically, so it must
    // appear ABOVE context in scrollback order. Without the
    // force-flush, context lands first (TURN_DONE later flushes
    // agent, putting it AFTER context — inverted order).
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      { type: "TOKEN_APPENDED", content: "I will inject", node: "agent" },
      {
        type: "CONFIRM_RECEIVED",
        content: "plan",
        taskId: "t",
        node: "confirmation_gate",
        payload: {},
      },
    ]);
    const kinds = s.history.map((i) => i.kind);
    const agentIdx = kinds.indexOf("agent");
    const contextIdx = kinds.indexOf("confirm_context");
    expect(agentIdx).toBeGreaterThan(-1);
    expect(contextIdx).toBeGreaterThan(agentIdx);
  });

  it("keeps an UNRESOLVED prior confirm_prompt in pending across a new confirm", () => {
    // Two confirms back-to-back without resolution: the first prompt
    // is the live UI the user is interacting with — flushing it as
    // "unresolved forever" into Static would be wrong. Force-flush
    // explicitly skips this case via the toKeep branch.
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      {
        type: "CONFIRM_RECEIVED",
        content: "intent",
        taskId: "task-A",
        node: "intent_confirm",
        payload: {},
      },
      // Note: NO CONFIRM_RESOLVED for task-A before task-B fires.
      {
        type: "CONFIRM_RECEIVED",
        content: "plan",
        taskId: "task-B",
        node: "confirmation_gate",
        payload: {},
      },
    ]);
    // Both prompts must still be in pending (neither got force-flushed
    // because both are unresolved).
    expect(
      s.pending.filter((i) => i.kind === "confirm_prompt"),
    ).toHaveLength(2);
    // Both contexts went to history at receipt time.
    expect(
      s.history.filter((i) => i.kind === "confirm_context"),
    ).toHaveLength(2);
  });

  it("accumulates two confirm_context cards across both interrupt layers", () => {
    // Both intent_confirm and confirmation_gate fire on a single turn.
    // Each lands a separate context card in history; pending holds
    // both prompts until the user resolves them.
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      {
        type: "CONFIRM_RECEIVED",
        content: "intent",
        taskId: "task-3",
        node: "intent_confirm",
        payload: { fault_intent: {}, summary: "intent" },
      },
      {
        type: "CONFIRM_RECEIVED",
        content: "plan",
        taskId: "task-3",
        node: "confirmation_gate",
        payload: { plan_summary: "plan", safety_status: "safe" },
      },
    ]);
    const contexts = s.history.filter((p) => p.kind === "confirm_context");
    expect(contexts).toHaveLength(2);
    const prompts = s.pending.filter((p) => p.kind === "confirm_prompt");
    expect(prompts).toHaveLength(2);
  });

  it("tolerates absent node/payload (back-compat with pre-fix server)", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      {
        type: "CONFIRM_RECEIVED",
        content: "raw plan summary",
        taskId: "task-4",
      },
    ]);
    const ctx = s.history.find((p) => p.kind === "confirm_context");
    expect(ctx).toBeDefined();
    if (ctx && ctx.kind === "confirm_context") {
      expect(ctx.node).toBeUndefined();
      expect(ctx.payload).toBeUndefined();
      expect(ctx.content).toBe("raw plan summary");
    }
  });
});

describe("reducer / HISTORY_CLEARED", () => {
  it("clears history but preserves pending mid-stream items", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "TURN_DONE" },
      { type: "TURN_STARTED", input: "hi2" },
      { type: "TOKEN_APPENDED", content: "streaming…", node: "n" },
      { type: "HISTORY_CLEARED" },
    ]);
    expect(s.history).toHaveLength(0);
    expect(s.pending.length).toBeGreaterThan(0);
  });

  it("bumps historyRemountKey so <Static> remounts", () => {
    const before = initialAppState.historyRemountKey;
    const s = reducer(initialAppState, { type: "HISTORY_CLEARED" });
    expect(s.historyRemountKey).toBe(before + 1);
  });
});

describe("reducer / thinking session commit", () => {
  it("stamps thoughtStartedAt on LLM_STARTED, not THINKING_APPENDED", () => {
    const before = Date.now();
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "LLM_STARTED", node: "n" },
      { type: "THINKING_APPENDED", content: "Let me think…", node: "n" },
    ]);
    expect(s.thoughtBuffer).toBe("Let me think…");
    expect(s.thoughtStartedAt).toBeGreaterThanOrEqual(before);
  });

  it("preserves thoughtStartedAt across subsequent thinking chunks", () => {
    const s1 = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "LLM_STARTED", node: "n" },
      { type: "THINKING_APPENDED", content: "first", node: "n" },
    ]);
    const startedAt = s1.thoughtStartedAt;
    // Second chunk later in the same session — start time must NOT
    // advance, otherwise duration would always be ~0 at commit.
    const s2 = reducer(s1, {
      type: "THINKING_APPENDED",
      content: " more",
      node: "n",
    });
    expect(s2.thoughtStartedAt).toBe(startedAt);
    expect(s2.thoughtBuffer).toBe("first more");
  });

  describe("hasActiveThinking edge-trigger (2026-05-26 perf)", () => {
    // The LoadingIndicator subscribes to ``hasActiveThinking`` instead
    // of ``thoughtBuffer`` so it doesn't re-render on every token
    // chunk. These tests pin the edge-trigger semantics so a future
    // refactor can't quietly turn it back into per-chunk noise.

    it("flips false → true on the FIRST THINKING_APPENDED of a session", () => {
      const s = fold([
        { type: "TURN_STARTED", input: "hi" },
      ]);
      expect(s.hasActiveThinking).toBe(false);
      const next = reducer(s, {
        type: "THINKING_APPENDED", content: "first chunk", node: "n",
      });
      expect(next.hasActiveThinking).toBe(true);
    });

    it("stays true across N subsequent THINKING_APPENDED chunks", () => {
      const s = fold([
        { type: "TURN_STARTED", input: "hi" },
        { type: "THINKING_APPENDED", content: "a", node: "n" },
        { type: "THINKING_APPENDED", content: "b", node: "n" },
        { type: "THINKING_APPENDED", content: "c", node: "n" },
        { type: "THINKING_APPENDED", content: "d", node: "n" },
      ]);
      // 4 dispatches, only 1 edge. Subscribers should not have been
      // notified of a "change" for the 2nd/3rd/4th appends.
      expect(s.hasActiveThinking).toBe(true);
      expect(s.thoughtBuffer).toBe("abcd");
    });

    it("flips true → false when commitThinking finalises the session (TOKEN_APPENDED)", () => {
      const s = fold([
        { type: "TURN_STARTED", input: "hi" },
        { type: "THINKING_APPENDED", content: "x", node: "n" },
        { type: "TOKEN_APPENDED", content: "Hi", node: "n" },
      ]);
      expect(s.hasActiveThinking).toBe(false);
    });

    it("flips true → false when commitThinking finalises via TOOL_STARTED", () => {
      const s = fold([
        { type: "TURN_STARTED", input: "hi" },
        { type: "THINKING_APPENDED", content: "x", node: "n" },
        {
          type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n",
        },
      ]);
      expect(s.hasActiveThinking).toBe(false);
    });

    it("resets to false on TURN_STARTED (new turn clears any leftover)", () => {
      const s = fold([
        { type: "TURN_STARTED", input: "first" },
        { type: "THINKING_APPENDED", content: "stale", node: "n" },
        { type: "TURN_STARTED", input: "second" },
      ]);
      expect(s.hasActiveThinking).toBe(false);
    });

    it("maintains the invariant hasActiveThinking === (thoughtBuffer.length > 0) across a mixed sequence", () => {
      // Drives the reducer through every transition path that touches
      // either field — TURN_STARTED, THINKING_APPENDED (first + Nth),
      // TOKEN_APPENDED (commits), TOOL_STARTED (commits), TURN_DONE
      // (commits), REPLAY_STARTED (resets) — and asserts the invariant
      // at every intermediate state. Guards against a future refactor
      // that updates ``thoughtBuffer`` without re-deriving the boolean,
      // which would silently break the LoadingIndicator's header swap.
      const sequence: Action[] = [
        { type: "TURN_STARTED", input: "hi" },
        { type: "THINKING_APPENDED", content: "a", node: "n" },
        { type: "THINKING_APPENDED", content: "b", node: "n" },
        { type: "TOKEN_APPENDED", content: "x", node: "n" },
        { type: "THINKING_APPENDED", content: "c", node: "n" },
        { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
        { type: "TURN_STARTED", input: "again" },
        { type: "THINKING_APPENDED", content: "d", node: "n" },
        { type: "TURN_DONE" },
        { type: "REPLAY_STARTED", taskId: "t1" },
      ];
      let state: AppState = initialAppState;
      for (const action of sequence) {
        state = reducer(state, action);
        expect(state.hasActiveThinking).toBe(state.thoughtBuffer.length > 0);
      }
    });
  });

  it("preserves a ThinkingItem across the first TOKEN_APPENDED after thinking", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "THINKING_APPENDED", content: "decomposing…", node: "n" },
      { type: "TOKEN_APPENDED", content: "Hello", node: "n" },
    ]);
    // The thinking item is committed by ``commitThinking`` and then
    // immediately flushed to ``history`` by the leading-stable flush
    // (it's a frozen item before the new agent item starts streaming).
    // Either way, exactly one thinking item must exist somewhere in
    // history+pending, the buffer must be empty, and the start
    // timestamp reset.
    const allThinking = [...s.history, ...s.pending].filter(
      (i) => i.kind === "thinking",
    );
    expect(allThinking).toHaveLength(1);
    expect(s.thoughtBuffer).toBe("");
    expect(s.thoughtStartedAt).toBe(0);
  });

  it("preserves a ThinkingItem across TOOL_STARTED interrupting thinking", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "THINKING_APPENDED", content: "considering…", node: "n" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
    ]);
    // commitThinking pushes the ThinkingItem into pending, then the
    // TOOL_STARTED-time leading-stable flush (added to keep the
    // dynamic frame from overflowing on long turns) immediately
    // moves it to history. Either side is acceptable; total must
    // be exactly one.
    const thinking = [...s.history, ...s.pending].filter(
      (i) => i.kind === "thinking",
    );
    expect(thinking).toHaveLength(1);
    expect(s.thoughtBuffer).toBe("");
  });

  it("commits one ThinkingItem per discrete session (thinking → tool → thinking → reply)", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "THINKING_APPENDED", content: "round 1", node: "n" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c1",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
      { type: "THINKING_APPENDED", content: "round 2", node: "n" },
      { type: "TOKEN_APPENDED", content: "answer", node: "n" },
    ]);
    // Each session commits its own ThinkingItem; the leading-stable
    // flush may move them to history immediately. Total across
    // history+pending must be 2.
    const allThinking = [...s.history, ...s.pending].filter(
      (i) => i.kind === "thinking",
    );
    expect(allThinking).toHaveLength(2);
  });

  it("commits the trailing thinking buffer on TURN_DONE", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "THINKING_APPENDED", content: "last words…", node: "n" },
      { type: "TURN_DONE" },
    ]);
    const thinkingInHistory = s.history.filter((i) => i.kind === "thinking");
    expect(thinkingInHistory).toHaveLength(1);
    expect(s.thoughtBuffer).toBe("");
    expect(s.thoughtStartedAt).toBe(0);
  });

  it("commits the trailing thinking buffer on TURN_ABORTED", () => {
    // Half-finished thought when the user hits Esc — keep it in
    // scrollback as a ThinkingItem so the user sees that the agent
    // *was* mid-CoT before the cancellation, not just a bare
    // Cancelled-by-user line.
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "THINKING_APPENDED", content: "half-thought", node: "n" },
      { type: "TURN_ABORTED", reason: "Cancelled by user" },
    ]);
    const thinkingInHistory = s.history.filter((i) => i.kind === "thinking");
    expect(thinkingInHistory).toHaveLength(1);
  });

  it("does NOT commit a ThinkingItem when no session ran", () => {
    // Pure chat reply with no thinking events: token straight to
    // agent item. No spurious ▸ Thought row.
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "TOKEN_APPENDED", content: "answer", node: "n" },
      { type: "TURN_DONE" },
    ]);
    expect(s.history.find((i) => i.kind === "thinking")).toBeUndefined();
  });

  it("resets thoughtStartedAt on TURN_STARTED (next turn starts fresh)", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "THINKING_APPENDED", content: "x", node: "n" },
      { type: "TURN_DONE" },
      { type: "TURN_STARTED", input: "second" },
    ]);
    expect(s.thoughtBuffer).toBe("");
    expect(s.thoughtStartedAt).toBe(0);
  });

  it("does NOT touch thoughtSubject on THINKING_APPENDED", () => {
    // Pre-fix the reducer ran extractThoughtSubject(buffer) on every
    // chunk for a derived field that the new LoadingIndicator hook
    // never reads — a wasted O(N) per dispatch. The new behaviour
    // leaves thoughtSubject untouched during thinking so it can stay
    // pinned to a prior tool name (informative header) until the
    // next subject-setting event.
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c1",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
      // After the tool completes, thoughtSubject is "kubectl". A
      // late thinking burst must NOT clobber it with extracted
      // phrases — we want the buffer to drive the body block while
      // the header keeps showing "kubectl" until something else
      // takes over.
      { type: "THINKING_APPENDED", content: "Now let me reflect.", node: "n" },
    ]);
    expect(s.thoughtSubject).toBe("kubectl");
    expect(s.thoughtBuffer).toBe("Now let me reflect.");
  });

  it("LLM_STARTED stamps thoughtStartedAt before THINKING_APPENDED arrives", () => {
    // The core fix: LLM_STARTED (on_chat_model_start) fires before
    // any reasoning_content chunks. thoughtStartedAt is stamped at
    // LLM_STARTED, and THINKING_APPENDED must NOT overwrite it. This
    // ensures "思考用时" includes the prefill (prompt-processing)
    // phase, not just the thinking-token streaming duration.
    const s = fold([
      { type: "TURN_STARTED", input: "complex question" },
      { type: "LLM_STARTED", node: "agent_loop" },
      { type: "THINKING_APPENDED", content: "reasoning…", node: "agent_loop" },
    ]);
    expect(s.thoughtStartedAt).toBeGreaterThan(0);
    expect(s.thoughtBuffer).toBe("reasoning…");
  });

  it("LLM_STARTED does not create phantom ThinkingItem when no thinking follows", () => {
    // If the LLM call starts but no reasoning_content arrives (e.g.
    // non-thinking model or simple response), commitThinking should
    // clear thoughtStartedAt WITHOUT creating a ThinkingItem.
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "LLM_STARTED", node: "agent_loop" },
      { type: "TOKEN_APPENDED", content: "answer", node: "agent_loop" },
      { type: "TURN_DONE" },
    ]);
    expect(s.history.find((i) => i.kind === "thinking")).toBeUndefined();
    expect(s.thoughtStartedAt).toBe(0);
  });

  it("LLM_STARTED + THINKING_APPENDED + TOKEN_APPENDED produces ThinkingItem with accurate duration", () => {
    // Verify the full flow: LLM_STARTED stamps thoughtStartedAt,
    // THINKING_APPENDED preserves it, TOKEN_APPENDED commits a
    // ThinkingItem with duration measured from LLM_STARTED time.
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "LLM_STARTED", node: "agent_loop" },
      { type: "THINKING_APPENDED", content: "thinking…", node: "agent_loop" },
      { type: "TOKEN_APPENDED", content: "answer", node: "agent_loop" },
    ]);
    const thinking = [...s.history, ...s.pending].find(
      (i) => i.kind === "thinking",
    );
    expect(thinking).toBeDefined();
    if (thinking?.kind === "thinking") {
      expect(thinking.durationMs).toBeGreaterThanOrEqual(0);
    }
    expect(s.thoughtStartedAt).toBe(0);
  });
});

describe("reducer / USAGE_RECEIVED + TurnUsageItem", () => {
  it("accumulates input + output tokens across multiple usage events", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "USAGE_RECEIVED", inputTokens: 100, outputTokens: 50 },
      { type: "USAGE_RECEIVED", inputTokens: 80, outputTokens: 40 },
    ]);
    expect(s.turnInputTokens).toBe(180);
    expect(s.turnOutputTokens).toBe(90);
  });

  it("resets per-turn token counters on TURN_STARTED", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "first" },
      { type: "USAGE_RECEIVED", inputTokens: 100, outputTokens: 50 },
      { type: "TURN_DONE" },
      { type: "TURN_STARTED", input: "second" },
    ]);
    expect(s.turnInputTokens).toBe(0);
    expect(s.turnOutputTokens).toBe(0);
  });

  it("ignores zero-token usage events (no state churn)", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "USAGE_RECEIVED", inputTokens: 0, outputTokens: 0 },
    ]);
    expect(s.turnInputTokens).toBe(0);
    expect(s.turnOutputTokens).toBe(0);
  });

  it("clamps negative token deltas to zero (defensive against bad payloads)", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "USAGE_RECEIVED", inputTokens: -5, outputTokens: 50 },
    ]);
    expect(s.turnInputTokens).toBe(0);
    expect(s.turnOutputTokens).toBe(50);
  });

  it("coerces undefined token field to 0 instead of poisoning state with NaN", () => {
    // Regression: the server's ``StreamEvent.to_dict`` historically
    // stripped any falsy field, including ``output_tokens=0`` /
    // ``input_tokens=0`` from a real ``usage`` event (DashScope's
    // prompt_cache_hit case can legitimately report 0 on one side
    // with a non-zero completion on the other). Wire frame loses the
    // 0, TS side reads ``undefined``, naive ``Math.max(0, undefined)``
    // returns ``NaN``, and ``state.turnInputTokens + NaN`` permanently
    // poisons the running total — LoadingIndicator's ``↓ N tokens``
    // tail and the end-of-turn TurnUsageItem both go silent for the
    // remainder of the turn. The fix is twofold: ``streaming.py``
    // ``to_dict`` now preserves explicit 0 for ``usage`` events, and
    // the reducer coerces undefined → 0 belt-and-braces. This test
    // pins the second half — even if a future server build drops a
    // field, the running total never goes ``NaN``.
    //
    // ``as`` cast required because the action type insists both fields
    // are ``number`` — at runtime they can land as ``undefined``
    // (older server, serialisation drift), which is exactly what we
    // want to reproduce here.
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      {
        type: "USAGE_RECEIVED",
        inputTokens: 100,
        outputTokens: undefined as unknown as number,
      },
    ]);
    expect(s.turnInputTokens).toBe(100);
    expect(s.turnOutputTokens).toBe(0);
    expect(Number.isNaN(s.turnInputTokens)).toBe(false);
    expect(Number.isNaN(s.turnOutputTokens)).toBe(false);

    // And the reverse: undefined input, real output.
    const s2 = fold([
      { type: "TURN_STARTED", input: "hi" },
      {
        type: "USAGE_RECEIVED",
        inputTokens: undefined as unknown as number,
        outputTokens: 42,
      },
    ]);
    expect(s2.turnInputTokens).toBe(0);
    expect(s2.turnOutputTokens).toBe(42);
    expect(Number.isNaN(s2.turnInputTokens)).toBe(false);
  });

  it("appends a TurnUsageItem to history at TURN_DONE when tokens > 0", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "USAGE_RECEIVED", inputTokens: 198, outputTokens: 89 },
      { type: "TOKEN_APPENDED", content: "answer", node: "n" },
      { type: "TURN_DONE" },
    ]);
    const usage = s.history.filter((i) => i.kind === "turn_usage");
    expect(usage).toHaveLength(1);
    if (usage[0]?.kind === "turn_usage") {
      expect(usage[0].inputTokens).toBe(198);
      expect(usage[0].outputTokens).toBe(89);
    }
  });

  it("appends a TurnUsageItem on TURN_ABORTED so cancelled turns still report usage", () => {
    // Mirrors the Python tracer behaviour — even an aborted turn
    // consumed tokens before cancellation. Honest accounting.
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "USAGE_RECEIVED", inputTokens: 50, outputTokens: 20 },
      { type: "TURN_ABORTED", reason: "Cancelled by user" },
    ]);
    const usage = s.history.filter((i) => i.kind === "turn_usage");
    expect(usage).toHaveLength(1);
  });

  it("does NOT append a TurnUsageItem when no usage events arrived", () => {
    // Older servers that never emit ``usage`` events produce turns
    // identical to the prior shape — no surprise empty-summary line.
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "TOKEN_APPENDED", content: "answer", node: "n" },
      { type: "TURN_DONE" },
    ]);
    expect(s.history.find((i) => i.kind === "turn_usage")).toBeUndefined();
  });

  it("places the TurnUsageItem at the END of the turn block", () => {
    // Ordering invariant: usage row must be the LAST item committed
    // for a turn (sits at the bottom of the scrollback block, just
    // above the next user prompt).
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "USAGE_RECEIVED", inputTokens: 100, outputTokens: 50 },
      { type: "TOKEN_APPENDED", content: "answer", node: "n" },
      { type: "TURN_DONE" },
    ]);
    const last = s.history[s.history.length - 1];
    expect(last?.kind).toBe("turn_usage");
  });
});

describe("reducer / TOKEN_APPENDED leading-stable flush", () => {
  // Generalises the original tool_group-only flush to also evict a
  // leading ``thinking`` row. Without this, a turn that opens with a
  // thinking session leaves a permanent ThinkingItem at index 0 of
  // pending, which blocks the flush of any completed tool_group sitting
  // behind it — so a long agent reply would push that tool_group off
  // the viewport in a half-rendered state.

  it("flushes a leading ThinkingItem to history when a token starts streaming", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "THINKING_APPENDED", content: "decomposing…", node: "n" },
      { type: "TOKEN_APPENDED", content: "answer", node: "n" },
    ]);
    // ThinkingItem moved to history; pending only has the streaming
    // AgentItem.
    expect(s.history.filter((i) => i.kind === "thinking")).toHaveLength(1);
    expect(s.pending.filter((i) => i.kind === "thinking")).toHaveLength(0);
    expect(s.pending.filter((i) => i.kind === "agent")).toHaveLength(1);
  });

  it("flushes a leading [ThinkingItem, completed ToolGroup] pair", () => {
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "THINKING_APPENDED", content: "first", node: "n" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c1",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
      { type: "TOKEN_APPENDED", content: "answer", node: "n" },
    ]);
    expect(s.history.filter((i) => i.kind === "thinking")).toHaveLength(1);
    expect(s.history.filter((i) => i.kind === "tool_group")).toHaveLength(1);
    expect(s.pending.filter((i) => i.kind === "agent")).toHaveLength(1);
  });

  it("stops the flush at a still-running tool_group", () => {
    // Mid-tool stable thinking item flushes; a running tool_group
    // must NOT be flushed (would freeze it half-rendered) — but
    // anything sitting ABOVE the running tool_group (the leading
    // ThinkingItem) should still flush.
    const s = fold([
      { type: "TURN_STARTED", input: "hi" },
      { type: "THINKING_APPENDED", content: "first", node: "n" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      // c1 still running; agent emits inter-tool commentary token.
      { type: "TOKEN_APPENDED", content: "while we wait…", node: "n" },
    ]);
    // Thinking item flushed; tool_group stays in pending (still
    // running) so the next TOOL_ENDED can mark it.
    expect(s.history.filter((i) => i.kind === "thinking")).toHaveLength(1);
    expect(s.pending.filter((i) => i.kind === "tool_group")).toHaveLength(1);
  });
});

describe("reducer / CONFIRM_RESOLVED triggers flush", () => {
  it("drains Phase 1 leftovers + the resolved confirm card on user-resolve", () => {
    // Reproduces the user-reported "execute_loop starts → flicker"
    // symptom: between confirm-resolve and the first execute_loop
    // event (typically several seconds of server-side
    // baseline_capture + LLM warm-up), pending used to carry every
    // Phase 1 leftover plus the resolved confirm card. Each
    // NODE_STARTED re-render tripped fullscreen-redraw because the
    // dynamic frame stayed huge. Flushing on CONFIRM_RESOLVED drains
    // the leftovers before the bridge phase begins.
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      // Phase 1 build-up: thinking + tool group + agent text
      { type: "THINKING_APPENDED", content: "decompose…", node: "n" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c1",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
      { type: "TOKEN_APPENDED", content: "plan summary", node: "n" },
      // confirm card lands
      {
        type: "CONFIRM_RECEIVED",
        content: "plan",
        taskId: "task-1",
        node: "confirmation_gate",
        payload: { plan_summary: "p" },
      },
      // user resolves — should drain leftovers
      {
        type: "CONFIRM_RESOLVED",
        taskId: "task-1",
        answer: "approved",
      },
    ]);
    // Pending must be empty (or near-empty) after resolve. Everything
    // — thinking, tool_group, agent text, AND the resolved confirm
    // prompt itself — should have flushed to history.
    expect(
      s.pending.filter((i) => i.kind === "thinking"),
    ).toHaveLength(0);
    expect(
      s.pending.filter((i) => i.kind === "tool_group"),
    ).toHaveLength(0);
    expect(
      s.pending.filter((i) => i.kind === "confirm_prompt"),
    ).toHaveLength(0);
    // History got the prompt + its context (1 each). Plus the
    // upstream thinking + tool_group from Phase 1.
    expect(
      s.history.filter((i) => i.kind === "confirm_prompt"),
    ).toHaveLength(1);
    expect(
      s.history.filter((i) => i.kind === "confirm_context"),
    ).toHaveLength(1);
    expect(s.history.filter((i) => i.kind === "thinking")).toHaveLength(1);
    expect(s.history.filter((i) => i.kind === "tool_group")).toHaveLength(1);
  });

  it("does NOT flush an unresolved confirm prompt (only the matching one resolves)", () => {
    // Two confirms in flight (Layer 1 + Layer 2 back to back). Only
    // the targeted prompt resolves; the other stays unresolved → not
    // stable → flush stops at it. Both contexts have already landed
    // in history (immutable on receipt) regardless.
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      {
        type: "CONFIRM_RECEIVED",
        content: "intent",
        taskId: "task-A",
        node: "intent_confirm",
        payload: { fault_intent: {} },
      },
      {
        type: "CONFIRM_RECEIVED",
        content: "plan",
        taskId: "task-B",
        node: "confirmation_gate",
        payload: { plan_summary: "p" },
      },
      {
        type: "CONFIRM_RESOLVED",
        taskId: "task-B",
        answer: "approved",
      },
    ]);
    // Task-A unresolved → blocks flush. Both prompts still in pending.
    expect(
      s.pending.filter((i) => i.kind === "confirm_prompt"),
    ).toHaveLength(2);
    expect(
      s.history.filter((i) => i.kind === "confirm_prompt"),
    ).toHaveLength(0);
    // Both contexts went straight to history at receipt time.
    expect(
      s.history.filter((i) => i.kind === "confirm_context"),
    ).toHaveLength(2);
  });
});

describe("reducer / CONSTRAIN_HEIGHT_TOGGLED", () => {
  it("flips constrainHeight on each dispatch", () => {
    const s1 = fold([{ type: "CONSTRAIN_HEIGHT_TOGGLED" }]);
    expect(s1.constrainHeight).toBe(false);
    const s2 = fold([
      { type: "CONSTRAIN_HEIGHT_TOGGLED" },
      { type: "CONSTRAIN_HEIGHT_TOGGLED" },
    ]);
    expect(s2.constrainHeight).toBe(true);
  });

  it("starts as true (cap engaged by default)", () => {
    const s = fold([]);
    expect(s.constrainHeight).toBe(true);
  });
});

describe("reducer / PHRASE_TICK", () => {
  it("idlePhrase starts empty", () => {
    const s = fold([]);
    expect(s.idlePhrase).toBe("");
  });

  it("PHRASE_TICK overwrites idlePhrase with the action payload", () => {
    const s = fold([
      { type: "PHRASE_TICK", phrase: "evaluating blast radius" },
    ]);
    expect(s.idlePhrase).toBe("evaluating blast radius");
  });

  it("returns the same state reference when phrase is unchanged", () => {
    // Idempotency guard: dispatching the same phrase twice should
    // skip the spread + reassign so React bail-outs the re-render.
    const s1 = fold([{ type: "PHRASE_TICK", phrase: "checking safety" }]);
    const s2 = reducer(s1, { type: "PHRASE_TICK", phrase: "checking safety" });
    expect(s2).toBe(s1);
  });

  it("sequential ticks rotate idlePhrase", () => {
    const s = fold([
      { type: "PHRASE_TICK", phrase: "thinking" },
      { type: "PHRASE_TICK", phrase: "drafting fault plan" },
      { type: "PHRASE_TICK", phrase: "observing system response" },
    ]);
    expect(s.idlePhrase).toBe("observing system response");
  });
});

describe("reducer / flushLeadingStable partial tool_group split", () => {
  it("splits a leading-completed prefix out of a still-running tool_group", () => {
    // Boundary case during concurrent tool calls (verifier_loop fires
    // multiple kubectl checks in parallel): many tools share one
    // tool_group, the leading prefix completes, others are still
    // running. Without the split, the entire group sits in pending
    // until the LAST tool finishes — the dynamic frame piles up and
    // trips overflow. With the split, completed tools migrate to
    // history immediately while the residual running tools stay live.
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      // Three concurrent TOOL_STARTED before any TOOL_ENDED — all
      // accumulate in one tool_group at pending head.
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      { type: "TOOL_STARTED", callId: "c2", name: "kubectl", node: "n" },
      { type: "TOOL_STARTED", callId: "c3", name: "kubectl", node: "n" },
      // Only the FIRST finishes. flushLeadingStable runs after
      // TOOL_ENDED — partial-split should harvest c1, leave c2+c3.
      {
        type: "TOOL_ENDED",
        callId: "c1",
        name: "kubectl",
        status: "success",
        content: "ok1",
      },
    ]);
    const histGroups = s.history.filter((i) => i.kind === "tool_group");
    const pendGroups = s.pending.filter((i) => i.kind === "tool_group");
    expect(histGroups).toHaveLength(1);
    if (histGroups[0]?.kind === "tool_group") {
      expect(histGroups[0].tools).toHaveLength(1);
      expect(histGroups[0].tools[0]?.callId).toBe("c1");
      expect(histGroups[0].tools[0]?.status).toBe("success");
    }
    expect(pendGroups).toHaveLength(1);
    if (pendGroups[0]?.kind === "tool_group") {
      expect(pendGroups[0].tools).toHaveLength(2);
      expect(pendGroups[0].tools.map((t) => t.callId)).toEqual(["c2", "c3"]);
      expect(
        pendGroups[0].tools.every((t) => t.status === "running"),
      ).toBe(true);
    }
  });

  it("allocates a fresh id for each partial-flushed group across repeated splits", () => {
    // Audit regression — previously both flushed prefix and kept
    // remainder spread {...item} so they shared the same id, which
    // produced duplicate React keys inside Ink's <Static> when a
    // 3-tool concurrent group split twice.
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      { type: "TOOL_STARTED", callId: "c2", name: "kubectl", node: "n" },
      { type: "TOOL_STARTED", callId: "c3", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c1",
        name: "kubectl",
        status: "success",
        content: "ok1",
      },
      {
        type: "TOOL_ENDED",
        callId: "c2",
        name: "kubectl",
        status: "success",
        content: "ok2",
      },
    ]);
    const histGroups = s.history.filter((i) => i.kind === "tool_group");
    expect(histGroups).toHaveLength(2);
    const ids = histGroups.map((g) => g.id);
    expect(new Set(ids).size).toBe(ids.length);
    const pendGroups = s.pending.filter((i) => i.kind === "tool_group");
    expect(pendGroups).toHaveLength(1);
    // Pending head retains the ORIGINAL group id so subsequent
    // TOOL_ENDED dispatches still find the running tool inside it.
    expect(ids).not.toContain(pendGroups[0]!.id);
  });

  it("does not split when the leading tool is itself running", () => {
    // The first tool in the group is still running → no completed
    // prefix to harvest → group stays whole in pending.
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      // No TOOL_ENDED for c1 yet.
      { type: "TOKEN_APPENDED", content: "thinking…", node: "n" },
    ]);
    const pendGroups = s.pending.filter((i) => i.kind === "tool_group");
    const histGroups = s.history.filter((i) => i.kind === "tool_group");
    expect(pendGroups).toHaveLength(1);
    expect(histGroups).toHaveLength(0);
    if (pendGroups[0]?.kind === "tool_group") {
      expect(pendGroups[0].tools).toHaveLength(1);
      expect(pendGroups[0].tools[0]?.status).toBe("running");
    }
  });
});

describe("reducer / THINKING_APPENDED triggers flush on first chunk", () => {
  it("drains stale leftovers when a new thinking session begins", () => {
    // Mid-turn transition (e.g. confirm-gate → execute_loop): the
    // first THINKING_APPENDED of the new session should flush
    // anything still parked in pending so the live thinking body
    // block doesn't render on top of a tall stale frame.
    const s = fold([
      { type: "TURN_STARTED", input: "inject" },
      { type: "THINKING_APPENDED", content: "phase1 thoughts", node: "n" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c1",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
      // First THINKING of NEW session — should flush prior thinking +
      // done tool group.
      { type: "THINKING_APPENDED", content: "phase2 thoughts", node: "n" },
    ]);
    // Prior thinking + tool_group flushed to history; the new
    // thinking session is in the buffer (no ThinkingItem yet — it
    // commits when a token / tool / TURN_DONE arrives).
    expect(s.history.filter((i) => i.kind === "thinking")).toHaveLength(1);
    expect(s.history.filter((i) => i.kind === "tool_group")).toHaveLength(1);
    expect(s.thoughtBuffer).toBe("phase2 thoughts");
  });

  it("does NOT re-flush on subsequent THINKING_APPENDED chunks of the same session", () => {
    // Mid-session chunks should NOT trigger another flush — that's
    // the per-token waste we want to avoid. We test by setting up
    // pending with a stable item, dispatching first thinking chunk
    // (which flushes), then a second chunk; the second chunk should
    // be a no-op for pending shape.
    const s1 = fold([
      { type: "TURN_STARTED", input: "inject" },
      { type: "TOOL_STARTED", callId: "c1", name: "kubectl", node: "n" },
      {
        type: "TOOL_ENDED",
        callId: "c1",
        name: "kubectl",
        status: "success",
        content: "ok",
      },
      // First chunk → flush leftovers.
      { type: "THINKING_APPENDED", content: "first", node: "n" },
    ]);
    expect(s1.thoughtBuffer).toBe("first");
    // Second chunk: appends to buffer, no further pending changes.
    const s2 = reducer(s1, {
      type: "THINKING_APPENDED",
      content: " second",
      node: "n",
    });
    expect(s2.thoughtBuffer).toBe("first second");
    // pending still empty; nothing to flush.
    expect(s2.pending).toEqual(s1.pending);
  });
});

describe("reducer / Phase 4 memory compaction lifecycle", () => {
  // The PreReasoningHook fires synchronously inside agent_loop while
  // the LangGraph stream is otherwise silent. The 3 actions form a
  // small state machine that drives a dedicated spinner during the
  // call and lands a finalised history row at the end.
  //
  // Pin the contract so a refactor that drops a slot transition
  // (e.g. forgets to clear ``currentCompaction`` on COMPLETED)
  // immediately fails here instead of stranding the spinner in
  // production.

  it("STARTED parks the live slot with tokensBefore + layer", () => {
    const s = reducer(initialAppState, {
      type: "MEMORY_COMPACTION_STARTED",
      tokensBefore: 12000,
      layer: "llm_summary",
    });
    expect(s.currentCompaction).not.toBeNull();
    expect(s.currentCompaction?.tokensBefore).toBe(12000);
    expect(s.currentCompaction?.layer).toBe("llm_summary");
    // The spinner mutex in useLoadingIndicator reads this slot's
    // truthiness — ensure ``startedAt`` lands as a real timestamp
    // so the indicator's elapsed-seconds tail starts from "now".
    expect(s.currentCompaction?.startedAt).toBeGreaterThan(0);
    // History / pending untouched at STARTED — the row only
    // materialises at COMPLETED / FAILED.
    expect(s.pending).toEqual([]);
    expect(s.history).toEqual([]);
  });

  it("COMPLETED clears the slot and appends a succeeded MemoryCompactionItem", () => {
    const s = fold([
      {
        type: "MEMORY_COMPACTION_STARTED",
        tokensBefore: 12000,
        layer: "llm_summary",
      },
      {
        type: "MEMORY_COMPACTION_COMPLETED",
        tokensBefore: 12000,
        tokensAfter: 4500,
        messagesCompacted: 23,
        durationMs: 6234,
        layer: "llm_summary",
      },
    ]);
    expect(s.currentCompaction).toBeNull();
    expect(s.pending).toHaveLength(1);
    const item = s.pending[0];
    if (item && item.kind === "memory_compaction") {
      expect(item.succeeded).toBe(true);
      expect(item.tokensBefore).toBe(12000);
      expect(item.tokensAfter).toBe(4500);
      expect(item.messagesCompacted).toBe(23);
      expect(item.durationMs).toBe(6234);
      expect(item.layer).toBe("llm_summary");
    } else {
      expect.fail(`expected memory_compaction item, got ${item?.kind}`);
    }
  });

  it("FAILED clears the slot and appends a failed item with errorMessage", () => {
    const s = fold([
      {
        type: "MEMORY_COMPACTION_STARTED",
        tokensBefore: 8000,
        layer: "llm_summary",
      },
      {
        type: "MEMORY_COMPACTION_FAILED",
        tokensBefore: 8000,
        durationMs: 1234,
        layer: "llm_summary",
        errorMessage: "rate limit exceeded",
      },
    ]);
    expect(s.currentCompaction).toBeNull();
    expect(s.pending).toHaveLength(1);
    const item = s.pending[0];
    if (item && item.kind === "memory_compaction") {
      expect(item.succeeded).toBe(false);
      expect(item.tokensBefore).toBe(8000);
      // Failed runs return no compacted output — pin the explicit 0
      // so the renderer doesn't accidentally show "saved 0 tokens".
      expect(item.tokensAfter).toBe(0);
      expect(item.messagesCompacted).toBe(0);
      expect(item.errorMessage).toBe("rate limit exceeded");
    } else {
      expect.fail(`expected memory_compaction item, got ${item?.kind}`);
    }
  });

  it("TURN_STARTED defensively clears a leaked currentCompaction", () => {
    // If a turn somehow ended without a COMPLETED/FAILED event
    // (server crash, network drop), the next turn must NOT inherit
    // the spinner. Lock the reset so a future refactor that drops
    // this clean-up surfaces here.
    const leaked: AppState = {
      ...initialAppState,
      currentCompaction: {
        startedAt: Date.now() - 5000,
        tokensBefore: 8000,
        layer: "llm_summary",
      },
    };
    const s = reducer(leaked, { type: "TURN_STARTED", input: "hello" });
    expect(s.currentCompaction).toBeNull();
  });

  it("commitPending defensively clears currentCompaction at TURN_DONE", () => {
    // TURN_DONE → commitPending. Mirror the leak scenario but
    // exercise the end-of-turn path. Catches regressions where
    // a future commitPending refactor forgets the clean-up.
    const leaked: AppState = {
      ...initialAppState,
      streamState: "responding",
      currentCompaction: {
        startedAt: Date.now() - 5000,
        tokensBefore: 8000,
        layer: "llm_summary",
      },
    };
    const s = reducer(leaked, { type: "TURN_DONE" });
    expect(s.currentCompaction).toBeNull();
  });

  it("multiple compactions in one turn each produce a history row", () => {
    // ReAct loop may compact more than once: hook fires before each
    // reasoning step. Pin that the reducer supports back-to-back
    // STARTED/COMPLETED cycles without losing slots.
    const s = fold([
      { type: "TURN_STARTED", input: "long convo" },
      {
        type: "MEMORY_COMPACTION_STARTED",
        tokensBefore: 9000,
        layer: "llm_summary",
      },
      {
        type: "MEMORY_COMPACTION_COMPLETED",
        tokensBefore: 9000,
        tokensAfter: 3000,
        messagesCompacted: 15,
        durationMs: 4000,
        layer: "llm_summary",
      },
      {
        type: "MEMORY_COMPACTION_STARTED",
        tokensBefore: 7000,
        layer: "llm_summary",
      },
      {
        type: "MEMORY_COMPACTION_COMPLETED",
        tokensBefore: 7000,
        tokensAfter: 2500,
        messagesCompacted: 10,
        durationMs: 3500,
        layer: "llm_summary",
      },
    ]);
    const compactionItems = s.pending.filter(
      (it) => it.kind === "memory_compaction",
    );
    expect(compactionItems).toHaveLength(2);
    expect(s.currentCompaction).toBeNull();
  });
});

describe("reducer / CONTEXT_SIZE_RECEIVED", () => {
  // Regression guard for the Footer state-size indicator pipeline:
  // hook emits → useStream dispatches → reducer must store the four
  // numbers AND persist them across TURN_STARTED.

  it("stores all four fields on snapshot", () => {
    const s = reducer(initialAppState, {
      type: "CONTEXT_SIZE_RECEIVED",
      currentTokens: 95_000,
      triggerTokens: 108_800,
      maxTokens: 128_000,
      messagesCount: 47,
    });
    expect(s.contextCurrentTokens).toBe(95_000);
    expect(s.contextTriggerTokens).toBe(108_800);
    expect(s.contextMaxTokens).toBe(128_000);
    expect(s.contextMessagesCount).toBe(47);
  });

  it("seeds contextMaxTokens with the server default at boot", () => {
    // The Footer needs proper numbers from the very first render —
    // before any context_size event arrives. We seed with the
    // server-side default (128k) so the indicator renders
    // "0.0k / 128k (0.0%)" at boot.
    expect(initialAppState.contextMaxTokens).toBe(128_000);
    expect(initialAppState.contextCurrentTokens).toBe(0);
    expect(initialAppState.contextError).toBe(false);
  });

  it("clears contextError on a fresh snapshot", () => {
    // If an ERROR_RECEIVED flipped contextError to true, the next
    // successful context_size event signals recovery and the Footer
    // should switch the tail back from "(error)" to live percent.
    const s = fold([
      {
        type: "ERROR_RECEIVED",
        message: "stream blew up",
        taskId: "turn-x",
      },
      {
        type: "CONTEXT_SIZE_RECEIVED",
        currentTokens: 50_000,
        triggerTokens: 108_800,
        maxTokens: 128_000,
        messagesCount: 20,
      },
    ]);
    expect(s.contextError).toBe(false);
    expect(s.contextCurrentTokens).toBe(50_000);
  });

  it("ERROR_RECEIVED sets contextError so Footer signals stale data", () => {
    const s = reducer(initialAppState, {
      type: "ERROR_RECEIVED",
      message: "boom",
      taskId: "turn-x",
    });
    expect(s.contextError).toBe(true);
  });

  it("a later snapshot supersedes the earlier one (replace, not accumulate)", () => {
    const s = fold([
      {
        type: "CONTEXT_SIZE_RECEIVED",
        currentTokens: 100_000,
        triggerTokens: 108_800,
        maxTokens: 128_000,
        messagesCount: 50,
      },
      // compaction fired between the two; current drops
      {
        type: "CONTEXT_SIZE_RECEIVED",
        currentTokens: 35_000,
        triggerTokens: 108_800,
        maxTokens: 128_000,
        messagesCount: 12,
      },
    ]);
    expect(s.contextCurrentTokens).toBe(35_000);
    expect(s.contextMessagesCount).toBe(12);
  });

  it("survives TURN_STARTED (cross-turn persistence)", () => {
    // The hook fires regardless of turn boundary, and the Footer
    // should keep displaying the last measurement even between
    // turns. Resetting on TURN_STARTED would cause the indicator
    // to flicker back to "no data" between turns.
    const s = fold([
      {
        type: "CONTEXT_SIZE_RECEIVED",
        currentTokens: 80_000,
        triggerTokens: 108_800,
        maxTokens: 128_000,
        messagesCount: 30,
      },
      { type: "TURN_STARTED", input: "next request" },
    ]);
    expect(s.contextCurrentTokens).toBe(80_000);
    expect(s.contextMaxTokens).toBe(128_000);
  });

  it("ignores all-zero snapshots so a protocol glitch doesn't wipe the slot", () => {
    // First a real snapshot, then a buggy all-zero one. The Footer
    // should keep showing the real numbers, not flip back to
    // "no data" mode (which would render as ns:default).
    const s = fold([
      {
        type: "CONTEXT_SIZE_RECEIVED",
        currentTokens: 50_000,
        triggerTokens: 108_800,
        maxTokens: 128_000,
        messagesCount: 20,
      },
      {
        type: "CONTEXT_SIZE_RECEIVED",
        currentTokens: 0,
        triggerTokens: 0,
        maxTokens: 0,
        messagesCount: 0,
      },
    ]);
    expect(s.contextCurrentTokens).toBe(50_000);
    expect(s.contextMaxTokens).toBe(128_000);
  });
});

describe("reducer / COMPACT_MANUAL lifecycle", () => {
  // Regression guard for ManualCompactIndicator's data source.
  // The /compact slash handler dispatches STARTED before opening the
  // SSE stream and DONE in its finally. The reducer must:
  //   - open a slot with a wall-clock startedAt on STARTED
  //   - close (null) the slot on DONE
  //   - be idempotent on DONE-when-already-closed

  it("STARTED opens the slot with current time", () => {
    const before = Date.now();
    const s = reducer(initialAppState, { type: "COMPACT_MANUAL_STARTED" });
    const after = Date.now();
    expect(s.currentManualCompact).not.toBeNull();
    if (s.currentManualCompact) {
      expect(s.currentManualCompact.startedAt).toBeGreaterThanOrEqual(before);
      expect(s.currentManualCompact.startedAt).toBeLessThanOrEqual(after);
    }
  });

  it("DONE closes the slot", () => {
    const s = fold([
      { type: "COMPACT_MANUAL_STARTED" },
      { type: "COMPACT_MANUAL_DONE" },
    ]);
    expect(s.currentManualCompact).toBeNull();
  });

  it("DONE is a no-op when slot is already null", () => {
    // Initial state already has currentManualCompact=null. Dispatching
    // DONE again must not throw or change the state reference.
    const s = reducer(initialAppState, { type: "COMPACT_MANUAL_DONE" });
    expect(s).toBe(initialAppState);
  });

  it("STARTED twice overwrites the slot (defensive against stale state)", () => {
    // Race scenario: previous /compact's finally was skipped (e.g.
    // unhandled exception). The next STARTED must reset startedAt
    // rather than refusing the open — otherwise the elapsed timer
    // would show stale seconds for the new run.
    const first = reducer(initialAppState, { type: "COMPACT_MANUAL_STARTED" });
    const firstStartedAt = first.currentManualCompact?.startedAt ?? 0;
    // Force a measurable delta — Date.now() granularity on most
    // platforms is 1ms, so a synchronous reducer call inside a tight
    // loop can land on the same ms. A busy loop here would be flaky;
    // instead we mock Date.now via the second STARTED's natural delay
    // by checking the slot is REPLACED (not merged) and the startedAt
    // is monotonically forward.
    const second = reducer(first, { type: "COMPACT_MANUAL_STARTED" });
    expect(second.currentManualCompact).not.toBeNull();
    if (second.currentManualCompact) {
      expect(second.currentManualCompact.startedAt).toBeGreaterThanOrEqual(
        firstStartedAt,
      );
    }
    // And it's a new object reference, not the old one.
    expect(second.currentManualCompact).not.toBe(first.currentManualCompact);
  });

  it("initial state has the slot null", () => {
    expect(initialAppState.currentManualCompact).toBeNull();
  });
});
