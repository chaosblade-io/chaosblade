import { describe, expect, it } from "vitest";
import { isStreamEvent } from "./events.js";

describe("api / isStreamEvent type guard", () => {
  // Regression guard: ``StreamEventType`` and the runtime
  // ``isStreamEvent`` switch MUST stay in lockstep. Adding a new
  // type to the union without updating ``isStreamEvent`` causes
  // ``parseFrame`` to silently reject every frame of that type —
  // the symptom is "server emits the event, TS TUI never receives
  // it, the corresponding reducer case never fires, the UI just
  // doesn't update". Discovered the hard way during context_size
  // wire-up: forgot to add ``t === "context_size"`` here and spent
  // 20 min instrumenting both ends before finding it.

  const allTypes = [
    "token",
    "thinking",
    "llm_start",
    "tool_start",
    "tool_end",
    "node_start",
    "node_end",
    "confirm",
    "result",
    "error",
    "usage",
    "memory_compaction",
    "context_size",
    "done",
  ];

  it("accepts every documented StreamEventType discriminator", () => {
    for (const t of allTypes) {
      expect(isStreamEvent({ type: t })).toBe(true);
    }
  });

  it("rejects non-objects", () => {
    expect(isStreamEvent(null)).toBe(false);
    expect(isStreamEvent(undefined)).toBe(false);
    expect(isStreamEvent("token")).toBe(false);
    expect(isStreamEvent(42)).toBe(false);
  });

  it("rejects objects with unknown type", () => {
    expect(isStreamEvent({ type: "future_event" })).toBe(false);
    expect(isStreamEvent({ type: "" })).toBe(false);
    expect(isStreamEvent({})).toBe(false);
  });

  it("accepts context_size frame with full payload", () => {
    // The exact wire shape from the /turn SSE for context_size.
    // If this test fails, the Footer indicator silently breaks.
    const frame = {
      type: "context_size",
      task_id: "turn-abc",
      context_current_tokens: 95000,
      context_trigger_tokens: 108800,
      context_max_tokens: 128000,
      context_messages_count: 47,
    };
    expect(isStreamEvent(frame)).toBe(true);
  });
});
