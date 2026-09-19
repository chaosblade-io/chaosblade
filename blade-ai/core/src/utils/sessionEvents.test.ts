/**
 * Session-events mapping tests for the ``/resume`` visual rebuild.
 *
 * Three layers, matching the module's structure:
 *
 *   1. ``streamEventToAction`` — per-event_type mapping parity with
 *      the live SSE path (useStream.applyEvent). These pin the wire
 *      contract: field names, fallbacks (call_id, result content,
 *      usage coercion), and the ``done → null`` hole that the
 *      stream-loop owns.
 *   2. ``sessionEventToActions`` — jsonl-line level: user_input →
 *      TURN_STARTED, confirm_answer → the full three-action sequence
 *      (the CONFIRM_DECISION_CONSUMED tail is load-bearing: without
 *      it Composer's pendingDecision effect would re-fire a network
 *      resolveInterrupt after the fold), unknown types skipped.
 *   3. ``foldSessionEvents`` — file-level turn-boundary rules:
 *      explicit ``done`` lines win; legacy files (no done) get a
 *      synthesised TURN_DONE after every result/error.
 *
 * Plus an end-to-end fold: a realistic (synthetic) events file run
 * through foldSessionEvents → reducer, asserting the terminal state
 * (history settled, streamState idle, trailing unresolved gate marked
 * interrupted). This is the same dispatch sequence /resume executes,
 * minus the network steps.
 */

import { describe, expect, it } from "vitest";
import {
  foldSessionEvents,
  sessionEventToActions,
  streamEventToAction,
  type SessionEventRecord,
} from "./sessionEvents.js";
import type { StreamEvent } from "../api/events.js";
import type { Action } from "../state/reducer.js";
import { reducer } from "../state/reducer.js";
import { initialAppState, type AppState } from "../state/types.js";

// ── helpers ──────────────────────────────────────────────────────────

/** One jsonl line, the way the server's sidewrite serialises it. */
function line(
  event_type: string,
  data: Record<string, unknown>,
  taskId = "task-1",
  source = "server",
): SessionEventRecord {
  return { ts: "2025-01-01T00:00:00Z", source, task_id: taskId, event_type, data };
}

function foldState(actions: Action[], start: AppState = initialAppState): AppState {
  return actions.reduce((s, a) => reducer(s, a), start);
}

// ── 1. streamEventToAction ───────────────────────────────────────────

describe("streamEventToAction / mapping parity", () => {
  it("token → TOKEN_APPENDED with node fallback", () => {
    expect(
      streamEventToAction({ type: "token", content: "hi", node: "intent" }),
    ).toEqual({ type: "TOKEN_APPENDED", content: "hi", node: "intent" });
    expect(
      streamEventToAction({ type: "token", content: "x" }),
    ).toEqual({ type: "TOKEN_APPENDED", content: "x", node: "" });
  });

  it("thinking → THINKING_APPENDED", () => {
    expect(
      streamEventToAction({ type: "thinking", content: "hmm" }),
    ).toEqual({ type: "THINKING_APPENDED", content: "hmm", node: "" });
  });

  it("llm_start → LLM_STARTED", () => {
    expect(streamEventToAction({ type: "llm_start", node: "plan" })).toEqual({
      type: "LLM_STARTED",
      node: "plan",
    });
  });

  it("tool_start / tool_end use call_id and fall back to task/name", () => {
    expect(
      streamEventToAction({
        type: "tool_start",
        tool_name: "blade_describe",
        call_id: "run-9",
        task_id: "task-1",
      }),
    ).toEqual({
      type: "TOOL_STARTED",
      callId: "run-9",
      name: "blade_describe",
      node: "",
    });
    // Pre-M5 file: no call_id → synthetic key.
    expect(
      streamEventToAction({
        type: "tool_start",
        tool_name: "blade_describe",
        task_id: "task-1",
      }),
    ).toEqual({
      type: "TOOL_STARTED",
      callId: "task-1/blade_describe",
      name: "blade_describe",
      node: "",
    });
    expect(
      streamEventToAction({
        type: "tool_end",
        tool_name: "blade_describe",
        call_id: "run-9",
        task_id: "task-1",
        is_error: true,
        content: "boom",
      }),
    ).toEqual({
      type: "TOOL_ENDED",
      callId: "run-9",
      name: "blade_describe",
      status: "error",
      content: "boom",
    });
  });

  it("node_start / node_end / node_message", () => {
    expect(
      streamEventToAction({ type: "node_start", node: "inject", phase: "execute" }),
    ).toEqual({ type: "NODE_STARTED", node: "inject", phase: "execute" });
    expect(streamEventToAction({ type: "node_end", node: "inject" })).toEqual({
      type: "NODE_ENDED",
      node: "inject",
    });
    const nm = streamEventToAction({
      type: "node_message",
      content: "captured baseline",
      node: "baseline_capture",
      timestamp: "2025-01-01T00:00:05Z",
    });
    expect(nm).toMatchObject({
      type: "NODE_MESSAGE",
      content: "captured baseline",
      node: "baseline_capture",
    });
    expect((nm as { ts: number }).ts).toBe(Date.parse("2025-01-01T00:00:05Z"));
  });

  it("confirm → CONFIRM_RECEIVED with lossless fields", () => {
    const payload = { fault_type: "cpu_fullload", level: 70 };
    expect(
      streamEventToAction({
        type: "confirm",
        content: "confirm injection?",
        task_id: "task-9",
        node: "confirmation_gate",
        payload,
      }),
    ).toEqual({
      type: "CONFIRM_RECEIVED",
      content: "confirm injection?",
      taskId: "task-9",
      node: "confirmation_gate",
      payload,
    });
  });

  it("auto_approved → AUTO_APPROVED", () => {
    expect(
      streamEventToAction({
        type: "auto_approved",
        content: "auto",
        task_id: "task-9",
        node: "n",
        payload: { a: 1 },
      }),
    ).toEqual({
      type: "AUTO_APPROVED",
      content: "auto",
      taskId: "task-9",
      node: "n",
      payload: { a: 1 },
    });
  });

  it("result content falls back to stringified payload", () => {
    expect(
      streamEventToAction({ type: "result", content: "ok", task_id: "task-1" }),
    ).toEqual({ type: "RESULT_RECEIVED", content: "ok", taskId: "task-1" });
    expect(
      streamEventToAction({
        type: "result",
        task_id: "task-1",
        payload: { status: "completed" },
      }),
    ).toEqual({
      type: "RESULT_RECEIVED",
      content: JSON.stringify({ status: "completed" }),
      taskId: "task-1",
    });
  });

  it("error → ERROR_RECEIVED; usage coerces undefined → 0", () => {
    expect(
      streamEventToAction({ type: "error", content: "bad", task_id: "t" }),
    ).toEqual({ type: "ERROR_RECEIVED", message: "bad", taskId: "t" });
    // Wire-drift defense (comment in the mapping): an older server
    // can drop zero-valued fields entirely — the cast simulates that
    // frame; isStreamEvent accepts it (only ``type`` is checked).
    expect(
      streamEventToAction({ type: "usage" } as unknown as StreamEvent),
    ).toEqual({
      type: "USAGE_RECEIVED",
      inputTokens: 0,
      outputTokens: 0,
      cachedTokens: 0,
    });
  });

  it("usage maps cached_tokens → cachedTokens (subset of input), missing field → 0", () => {
    // Cache-hit telemetry: the server forces ``cached_tokens`` onto every
    // ``usage`` frame; it is a SUBSET of ``input_tokens`` (not additive).
    expect(
      streamEventToAction({
        type: "usage",
        input_tokens: 2990,
        output_tokens: 120,
        cached_tokens: 2176,
      }),
    ).toEqual({
      type: "USAGE_RECEIVED",
      inputTokens: 2990,
      outputTokens: 120,
      cachedTokens: 2176,
    });
    // Older server omits cached_tokens entirely → coerced to 0, never NaN.
    expect(
      streamEventToAction({
        type: "usage",
        input_tokens: 100,
        output_tokens: 50,
      } as unknown as StreamEvent),
    ).toEqual({
      type: "USAGE_RECEIVED",
      inputTokens: 100,
      outputTokens: 50,
      cachedTokens: 0,
    });
  });

  it("memory_compaction maps all three phases", () => {
    expect(
      streamEventToAction({
        type: "memory_compaction",
        compaction_phase: "started",
        tokens_before: 9000,
      }),
    ).toEqual({
      type: "MEMORY_COMPACTION_STARTED",
      tokensBefore: 9000,
      layer: "llm_summary",
    });
    expect(
      streamEventToAction({
        type: "memory_compaction",
        compaction_phase: "completed",
        tokens_before: 9000,
        tokens_after: 1000,
        messages_compacted: 12,
        duration_ms: 2500,
      }),
    ).toEqual({
      type: "MEMORY_COMPACTION_COMPLETED",
      tokensBefore: 9000,
      tokensAfter: 1000,
      messagesCompacted: 12,
      durationMs: 2500,
      layer: "llm_summary",
    });
    expect(
      streamEventToAction({
        type: "memory_compaction",
        compaction_phase: "failed",
        tokens_before: 9000,
        duration_ms: 30,
        content: "quota",
      }),
    ).toEqual({
      type: "MEMORY_COMPACTION_FAILED",
      tokensBefore: 9000,
      durationMs: 30,
      layer: "llm_summary",
      errorMessage: "quota",
    });
  });

  it("context_size coerces via Number()", () => {
    expect(
      streamEventToAction({
        type: "context_size",
        context_current_tokens: 5000,
        context_trigger_tokens: 8000,
        context_max_tokens: 16000,
        context_messages_count: 20,
      }),
    ).toEqual({
      type: "CONTEXT_SIZE_RECEIVED",
      currentTokens: 5000,
      triggerTokens: 8000,
      maxTokens: 16000,
      messagesCount: 20,
    });
  });

  it("done → null (the stream loop owns the boundary)", () => {
    expect(streamEventToAction({ type: "done", task_id: "t" })).toBeNull();
  });

  it("fault_window maps all three phases (content is JSON)", () => {
    expect(
      streamEventToAction({
        type: "fault_window",
        task_id: "turn-hold-1",
        content: JSON.stringify({
          phase: "enter",
          inject_task_id: "inject-1",
          duration_sec: 300,
          remaining_sec: 270,
          until_ts: "2026-09-18T12:00:00+08:00",
        }),
      }),
    ).toEqual({
      type: "FAULT_WINDOW_ENTERED",
      turnId: "turn-hold-1",
      injectTaskId: "inject-1",
      durationSec: 300,
      remainingSec: 270,
    });
    expect(
      streamEventToAction({
        type: "fault_window",
        task_id: "turn-hold-1",
        content: JSON.stringify({ phase: "tick", remaining_sec: 240.5 }),
      }),
    ).toEqual({ type: "FAULT_WINDOW_TICKED", remainingSec: 240.5 });
    expect(
      streamEventToAction({
        type: "fault_window",
        task_id: "turn-hold-1",
        content: JSON.stringify({ phase: "exit", reason: "early", remaining_sec: 0 }),
      }),
    ).toEqual({ type: "FAULT_WINDOW_EXITED", reason: "early" });
  });

  it("fault_window exit reason coerces unknown values to elapsed", () => {
    expect(
      streamEventToAction({
        type: "fault_window",
        content: JSON.stringify({ phase: "exit", reason: "who-knows" }),
      }),
    ).toEqual({ type: "FAULT_WINDOW_EXITED", reason: "elapsed" });
  });

  it("fault_window with corrupt/unknown payloads maps to null", () => {
    expect(
      streamEventToAction({ type: "fault_window", content: "{not json" }),
    ).toBeNull();
    expect(
      streamEventToAction({
        type: "fault_window",
        content: JSON.stringify({ phase: "teleported" }),
      }),
    ).toBeNull();
    // Non-object JSON (a bare number) is as corrupt as a broken string.
    expect(
      streamEventToAction({ type: "fault_window", content: "42" }),
    ).toBeNull();
  });
});

// ── 2. sessionEventToActions ─────────────────────────────────────────

describe("sessionEventToActions / jsonl line mapping", () => {
  it("user_input → TURN_STARTED with the echoed text", () => {
    expect(
      sessionEventToActions(
        line("user_input", { content: "inject cpu fullload" }, "", "user"),
      ),
    ).toEqual([{ type: "TURN_STARTED", input: "inject cpu fullload" }]);
  });

  it("confirm_answer → the full three-action live sequence", () => {
    expect(
      sessionEventToActions(
        line("confirm_answer", { content: "approved" }, "task-9", "user"),
      ),
    ).toEqual([
      { type: "CONFIRM_USER_DECIDED", taskId: "task-9", answer: "approved" },
      { type: "CONFIRM_RESOLVED", taskId: "task-9", answer: "approved" },
      { type: "CONFIRM_DECISION_CONSUMED" },
    ]);
  });

  it("unknown event_type → no actions", () => {
    expect(sessionEventToActions(line("what_is_this", {}))).toEqual([]);
    expect(sessionEventToActions({ event_type: undefined })).toEqual([]);
  });

  it("non-StreamEvent data payloads are skipped, not thrown", () => {
    // Corrupt salvage: data isn't a serialised StreamEvent.
    expect(
      sessionEventToActions(line("token", { nope: "garbage" })),
    ).toEqual([]);
    expect(sessionEventToActions(line("result", null as never))).toEqual([]);
  });

  it("passthrough events go through the shared mapping", () => {
    expect(
      sessionEventToActions(line("token", { type: "token", content: "a" })),
    ).toEqual([{ type: "TOKEN_APPENDED", content: "a", node: "" }]);
  });

  it("buffer-flushed token/thinking records (data WITHOUT type) still map", () => {
    // REAL files: tui_session_store coalesces token/thinking runs in
    // memory and _flush_event_buffers hand-builds ``{"content": ...}``
    // — no ``type`` inside data. isStreamEvent would reject them; the
    // outer event_type must carry the mapping. Regression guard for
    // the P0 where the whole agent reply text vanished on resume.
    expect(
      sessionEventToActions(
        line("token", { content: "整段合并后的 agent 回复" }),
      ),
    ).toEqual([
      { type: "TOKEN_APPENDED", content: "整段合并后的 agent 回复", node: "" },
    ]);
    expect(
      sessionEventToActions(
        line("thinking", { content: "merged reasoning" }),
      ),
    ).toEqual([
      { type: "THINKING_APPENDED", content: "merged reasoning", node: "" },
    ]);
  });

  it("empty-content flush records are skipped", () => {
    expect(sessionEventToActions(line("token", {}))).toEqual([]);
    expect(sessionEventToActions(line("thinking", {}))).toEqual([]);
  });
});

// ── 3. foldSessionEvents / turn boundaries ───────────────────────────

describe("foldSessionEvents / turn boundaries", () => {
  it("explicit done lines map to TURN_DONE at their position", () => {
    const actions = foldSessionEvents([
      line("user_input", { content: "hi" }, "", "user"),
      line("result", { type: "result", content: "ok", task_id: "t1" }),
      line("done", { type: "done", task_id: "t1" }),
    ]);
    expect(actions).toEqual([
      { type: "TURN_STARTED", input: "hi" },
      { type: "RESULT_RECEIVED", content: "ok", taskId: "t1" },
      { type: "TURN_DONE" },
    ]);
  });

  it("legacy file (no done) synthesises TURN_DONE after each result", () => {
    const actions = foldSessionEvents([
      line("user_input", { content: "a" }, "", "user"),
      line("result", { type: "result", content: "1", task_id: "t1" }),
      line("user_input", { content: "b" }, "", "user"),
      line("result", { type: "result", content: "2", task_id: "t2" }),
    ]);
    expect(actions).toEqual([
      { type: "TURN_STARTED", input: "a" },
      { type: "RESULT_RECEIVED", content: "1", taskId: "t1" },
      { type: "TURN_DONE" },
      { type: "TURN_STARTED", input: "b" },
      { type: "RESULT_RECEIVED", content: "2", taskId: "t2" },
      { type: "TURN_DONE" },
    ]);
  });

  it("legacy error synthesises ERROR_RECEIVED + TURN_DONE even with corrupt data", () => {
    // The payload fails isStreamEvent (no ``type``) — the boundary must
    // still land so commitPending closes the turn.
    const actions = foldSessionEvents([
      line("user_input", { content: "go" }, "", "user"),
      line("error", { content: "boom" }, "t1"),
    ]);
    expect(actions).toEqual([
      { type: "TURN_STARTED", input: "go" },
      { type: "ERROR_RECEIVED", message: "boom", taskId: "t1" },
      { type: "TURN_DONE" },
    ]);
  });

  it("mixed-era file: done is decided per TURN SEGMENT, not file-wide", () => {
    // A legacy session (turns without done — pre-sidewrite) resumed
    // after the done-sidewrite shipped: the appended turns carry
    // done, the old ones don't. The file-wide check the original
    // implementation used let the tail's done suppress the legacy
    // turns' synthesis — their content then sat in ``pending`` past
    // the next user_input's TURN_STARTED (``pending: []``) and was
    // silently dropped from the rebuilt history. Per-segment
    // (user_input → user_input) is the correct boundary.
    const actions = foldSessionEvents([
      // Legacy turn — no done record.
      line("user_input", { content: "a" }, "", "user"),
      line("token", { content: "legacy reply" }),
      line("error", { type: "error", content: "boom", task_id: "t1" }),
      // New-era turn — explicit done.
      line("user_input", { content: "b" }, "", "user"),
      line("result", { type: "result", content: "2", task_id: "t2" }),
      line("done", { type: "done", task_id: "t2" }),
    ]);
    expect(actions).toEqual([
      { type: "TURN_STARTED", input: "a" },
      { type: "TOKEN_APPENDED", content: "legacy reply", node: "" },
      { type: "ERROR_RECEIVED", message: "boom", taskId: "t1" },
      { type: "TURN_DONE" }, // synthesised: legacy segment
      { type: "TURN_STARTED", input: "b" },
      { type: "RESULT_RECEIVED", content: "2", taskId: "t2" },
      { type: "TURN_DONE" }, // explicit: new-era segment
    ]);
  });

  it("records before the first user_input share that segment's verdict", () => {
    // Headless prefix (defensive): events recorded before any
    // user_input all land in the FIRST segment — its done verdict
    // applies to them, mirroring the file-wide behaviour for the
    // degenerate single-segment file.
    const actions = foldSessionEvents([
      line("result", { type: "result", content: "1", task_id: "t1" }),
      line("done", { type: "done", task_id: "t1" }),
      line("result", { type: "result", content: "2", task_id: "t2" }),
    ]);
    expect(actions).toEqual([
      { type: "RESULT_RECEIVED", content: "1", taskId: "t1" },
      { type: "TURN_DONE" },
      { type: "RESULT_RECEIVED", content: "2", taskId: "t2" },
    ]);
  });

  it("confirm_answer expands into the three-action sequence in-place", () => {
    const actions = foldSessionEvents([
      line("confirm", {
        type: "confirm",
        content: "gate?",
        task_id: "t9",
        node: "confirmation_gate",
        payload: {},
      }),
      line("confirm_answer", { content: "rejected" }, "t9", "user"),
      line("result", { type: "result", content: "stopped", task_id: "t9" }),
      line("done", { type: "done", task_id: "t9" }),
    ]);
    expect(actions[1]).toEqual({
      type: "CONFIRM_USER_DECIDED",
      taskId: "t9",
      answer: "rejected",
    });
    expect(actions[2]).toEqual({
      type: "CONFIRM_RESOLVED",
      taskId: "t9",
      answer: "rejected",
    });
    expect(actions[3]).toEqual({ type: "CONFIRM_DECISION_CONSUMED" });
  });
});

// ── 4. end-to-end: events file → reducer → terminal state ────────────

describe("foldSessionEvents → reducer end-to-end", () => {
  it("a settled legacy turn rebuilds with idle streamState and empty pending", () => {
    const events: SessionEventRecord[] = [
      line("user_input", { content: "inject cpu fullload" }, "", "user"),
      line("node_start", { type: "node_start", node: "intent_clarification" }),
      line("node_end", { type: "node_end", node: "intent_clarification" }),
      line("llm_start", { type: "llm_start", node: "intent" }),
      line("token", { content: "understood" }),
      line("node_message", {
        type: "node_message",
        content: "plan ready",
        node: "planning",
      }),
      line("result", {
        type: "result",
        content: '{"status": "completed"}',
        task_id: "t1",
      }),
    ];
    const state = foldState(foldSessionEvents(events));
    // The user echo, the agent reply and the result item all landed.
    expect(state.streamState).toBe("idle");
    expect(state.pending).toHaveLength(0);
    const kinds = state.history.map((h) => h.kind);
    expect(kinds).toContain("user");
    expect(kinds).toContain("agent");
    expect(kinds).toContain("result");
    // The user echo carries the original input (retry / rerun context).
    expect(state.lastTurnInput).toBe("inject cpu fullload");
  });

  it("an explicit-done file converges to the same terminal state", () => {
    const events: SessionEventRecord[] = [
      line("user_input", { content: "status?" }, "", "user"),
      line("token", { content: "all green" }),
      line("result", { type: "result", content: "ok", task_id: "t1" }),
      line("done", { type: "done", task_id: "t1" }),
    ];
    const state = foldState(foldSessionEvents(events));
    expect(state.streamState).toBe("idle");
    expect(state.pending).toHaveLength(0);
  });

  it("mixed-era file: the legacy turn's content survives into history", () => {
    // The regression lock for the file-wide done check: legacy turns
    // (no done) must get their synthesised boundary so nothing is
    // dropped by the next TURN_STARTED's ``pending: []`` wipe — even
    // when a later turn in the SAME file carries explicit done.
    const events: SessionEventRecord[] = [
      // Legacy turn — no done anywhere in it.
      line("user_input", { content: "q1" }, "", "user"),
      line("token", { content: "legacy agent reply" }),
      line("error", { type: "error", content: "boom", task_id: "t1" }),
      // New-era turn — explicit done.
      line("user_input", { content: "q2" }, "", "user"),
      line("token", { content: "new reply" }),
      line("result", { type: "result", content: "ok", task_id: "t2" }),
      line("done", { type: "done", task_id: "t2" }),
    ];
    const state = foldState([
      { type: "REPLAY_STARTED", taskId: "t2" },
      ...foldSessionEvents(events),
      { type: "REPLAY_ENDED", aborted: false },
    ]);
    const kinds = state.history.map((h) => h.kind);
    // BOTH turns' content landed — the legacy agent reply and its
    // error card are not silently dropped.
    expect(kinds).toContain("agent");
    expect(kinds).toContain("error");
    expect(kinds.filter((k) => k === "user")).toHaveLength(2);
    expect(state.streamState).toBe("idle");
    expect(state.pending).toHaveLength(0);
  });

  it("a mid-fold answered confirm resolves the card and leaves no pendingDecision", () => {
    const events: SessionEventRecord[] = [
      line("user_input", { content: "inject" }, "", "user"),
      line("confirm", {
        type: "confirm",
        content: "approve injection?",
        task_id: "t9",
        node: "confirmation_gate",
        payload: { level: 70 },
      }),
      line("confirm_answer", { content: "approved" }, "t9", "user"),
      line("token", { content: "injecting" }),
      line("result", { type: "result", content: "done", task_id: "t9" }),
      line("done", { type: "done", task_id: "t9" }),
    ];
    const state = foldState(foldSessionEvents(events));
    // The three-action sequence must have fully settled the gate:
    // no live prompt in pending, no decision slot left for Composer's
    // network effect to pick up.
    expect(state.streamState).toBe("idle");
    expect(state.pending).toHaveLength(0);
    expect(state.pendingDecision).toBeNull();
    // …and the resolved card is visible in history.
    const resolvedCard = state.history.find(
      (h) => h.kind === "confirm_prompt",
    );
    expect(resolvedCard).toBeDefined();
    expect((resolvedCard as { resolved?: boolean }).resolved).toBe(true);
    expect((resolvedCard as { answer?: string }).answer).toBe("approved");
  });

  it("an events file ending on an unanswered confirm + the interrupted sentinel settles it", () => {
    const events: SessionEventRecord[] = [
      line("user_input", { content: "inject" }, "", "user"),
      line("confirm", {
        type: "confirm",
        content: "approve injection?",
        task_id: "t9",
        node: "confirmation_gate",
        payload: { level: 70 },
      }),
      // File ends here — server died while the card waited.
    ];
    const actions = foldSessionEvents(events);
    // /resume's trailing-gate patch: mark the last unresolved prompt.
    let lastConfirmTaskId: string | null = null;
    let lastConfirmDecided = false;
    for (const action of actions) {
      if (action.type === "CONFIRM_RECEIVED") {
        lastConfirmTaskId = action.taskId ?? null;
        lastConfirmDecided = false;
      } else if (action.type === "CONFIRM_USER_DECIDED") {
        lastConfirmDecided = true;
      }
    }
    expect(lastConfirmTaskId).toBe("t9");
    expect(lastConfirmDecided).toBe(false);
    // Same sentinel dispatch the handler performs.
    if (lastConfirmTaskId !== null && !lastConfirmDecided) {
      actions.push({
        type: "CONFIRM_RESOLVED",
        taskId: lastConfirmTaskId,
        answer: "interrupted",
      });
    }
    const state = foldState([
      { type: "REPLAY_STARTED", taskId: "t9" },
      ...actions,
      { type: "REPLAY_ENDED", aborted: false },
    ]);
    // The card must not render as a live interactive gate.
    const card = [...state.pending, ...state.history].find(
      (h) => h.kind === "confirm_prompt",
    ) as { resolved?: boolean; answer?: string } | undefined;
    expect(card?.resolved).toBe(true);
    expect(card?.answer).toBe("interrupted");
    expect(state.streamState).toBe("idle");
  });
});
