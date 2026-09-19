/**
 * Session events jsonl → reducer Action mapping for ``/resume <sid>``.
 *
 * Two jobs live in this module:
 *
 * 1. ``streamEventToAction`` — the SHARED ``StreamEvent → Action``
 *    mapping. useStream's applyEvent consumes the exact same function,
 *    so the live SSE path and the resume rebuild path can never drift
 *    apart (one mapping source, two drivers).
 *
 * 2. ``sessionEventToActions`` — maps one line of the events jsonl
 *    (``{ts, source, task_id, event_type, data}`` where ``data`` is
 *    ``StreamEvent.to_dict()``) to 0..3 Actions:
 *      - ``user_input``      → TURN_STARTED (the user-side event that
 *        never flows over SSE; turn.py sidewrites it before the intent
 *        graph streams)
 *      - ``confirm_answer``  → CONFIRM_USER_DECIDED +
 *        CONFIRM_RESOLVED + CONFIRM_DECISION_CONSUMED (the full live
 *        three-action sequence — see the branch comment for why the
 *        slot-clearing tail is load-bearing)
 *      - ``done``            → TURN_DONE (server sidewrites it in the
 *        turn's finally block since the resume feature; for older
 *        files the client synthesises it — see below)
 *      - ``token``/``thinking`` → TOKEN/THINKING_APPENDED mapped off
 *        the OUTER ``event_type`` — the store's buffer flush writes
 *        their ``data`` without a ``type`` field, so they can't ride
 *        the StreamEvent passthrough below
 *      - everything else     → streamEventToAction passthrough
 *
 *    Legacy files (pre-done-sidewrite) have no ``done`` lines at all:
 *    the turn boundary must be synthesised so ``commitPending`` runs
 *    and history items settle. ``RESULT``/``ERROR`` events always end
 *    a turn in practice (the only events after them are ``done``), so
 *    the synthesised boundary appends a TURN_DONE after each. When the
 *    file DOES carry explicit ``done`` lines, the passthrough mapping
 *    emits TURN_DONE and the synthesised one is suppressed (the
 *    caller-side ``pendingDone`` bookkeeping in ``foldSessionEvents``).
 *
 * Nothing here dispatches — pure mapping, so it is trivially testable
 * and reusable from both the Ink TUI and any future headless consumer.
 */

import type { Action } from "../state/reducer.js";
import { isStreamEvent, type StreamEvent } from "../api/events.js";

// ── Shared: live SSE + resume ───────────────────────────────────────

/**
 * Map one StreamEvent (live SSE frame or jsonl ``data`` blob) to its
 * reducer Action. Returns ``null`` for ``done`` — that's a stream-loop
 * concern (useStream breaks its read loop on it; the resume fold has
 * its own turn-boundary logic below).
 *
 * Extracted verbatim from useStream.applyEvent so both drivers share a
 * single mapping source. Behavioural notes carried over:
 *  - tool callId fallback ``${task_id}/${tool_name}`` for pre-M5 files.
 *  - ``result`` content falls back to a JSON-stringified payload.
 *  - usage/context_size coerce undefined → 0.
 *  - memory_compaction unknown phases → null (dev-time protocol drift;
 *    applyEvent logs a warning, the fold just skips).
 */
export function streamEventToAction(evt: StreamEvent): Action | null {
  switch (evt.type) {
    case "token":
      return {
        type: "TOKEN_APPENDED",
        content: evt.content,
        node: evt.node ?? "",
      };
    case "thinking":
      return {
        type: "THINKING_APPENDED",
        content: evt.content,
        node: evt.node ?? "",
      };
    case "llm_start":
      return {
        type: "LLM_STARTED",
        node: evt.node ?? "",
      };
    case "tool_start":
      return {
        type: "TOOL_STARTED",
        callId: pickCallId(evt.call_id, evt.task_id, evt.tool_name),
        name: evt.tool_name,
        node: evt.node ?? "",
      };
    case "tool_end":
      return {
        type: "TOOL_ENDED",
        callId: pickCallId(evt.call_id, evt.task_id, evt.tool_name),
        name: evt.tool_name,
        // ``is_error`` is set when the server converted an
        // ``on_tool_error`` (tool raised) into this terminal event. It
        // still closes the card; the status just flips ✓ → ✗ so the
        // failure is visible.
        status: evt.is_error ? "error" : "success",
        content: evt.content,
      };
    case "node_start":
      return { type: "NODE_STARTED", node: evt.node, phase: evt.phase };
    case "node_end":
      return { type: "NODE_ENDED", node: evt.node };
    case "node_message": {
      // Receipt time drives the web rail timeline's relative
      // timestamps: prefer the server ``timestamp`` when the wire
      // carries one, fall back to the client clock.
      const wireTs = evt.timestamp ? Date.parse(evt.timestamp) : NaN;
      return {
        type: "NODE_MESSAGE",
        content: evt.content,
        node: evt.node ?? "",
        ts: Number.isNaN(wireTs) ? Date.now() : wireTs,
      };
    }
    case "confirm":
      return {
        type: "CONFIRM_RECEIVED",
        content: evt.content,
        taskId: evt.task_id,
        node: evt.node,
        payload: evt.payload,
      };
    case "auto_approved":
      // Auto mode: render the read-only card, no interaction / no wait.
      return {
        type: "AUTO_APPROVED",
        content: evt.content,
        taskId: evt.task_id,
        node: evt.node,
        payload: evt.payload,
      };
    case "result":
      return {
        type: "RESULT_RECEIVED",
        // Legacy /turn results put the envelope in content as a JSON
        // string; newer surfaces use a typed payload dict and leave
        // content empty. RESULT_RECEIVED's reducer expects a string,
        // so stringify the payload when content is absent.
        content: evt.content ?? JSON.stringify(evt.payload ?? {}),
        taskId: evt.task_id,
      };
    case "error":
      return {
        type: "ERROR_RECEIVED",
        message: evt.content,
        taskId: evt.task_id,
      };
    case "usage":
      // Coerce undefined → 0 so the action shape stays accurate. Older
      // servers can drop a 0 field entirely from the wire frame; the
      // reducer also defends against NaN.
      return {
        type: "USAGE_RECEIVED",
        inputTokens: evt.input_tokens ?? 0,
        outputTokens: evt.output_tokens ?? 0,
        cachedTokens: evt.cached_tokens ?? 0,
      };
    case "memory_compaction": {
      const phase = evt.compaction_phase ?? "started";
      const layer = evt.layer ?? "llm_summary";
      const tokensBefore = evt.tokens_before ?? 0;
      if (phase === "started") {
        return {
          type: "MEMORY_COMPACTION_STARTED",
          tokensBefore,
          layer,
        };
      } else if (phase === "completed") {
        return {
          type: "MEMORY_COMPACTION_COMPLETED",
          tokensBefore,
          tokensAfter: evt.tokens_after ?? 0,
          messagesCompacted: evt.messages_compacted ?? 0,
          durationMs: evt.duration_ms ?? 0,
          layer,
        };
      } else if (phase === "failed") {
        return {
          type: "MEMORY_COMPACTION_FAILED",
          tokensBefore,
          durationMs: evt.duration_ms ?? 0,
          layer,
          errorMessage: evt.content ?? "",
        };
      }
      return null;
    }
    case "context_size":
      return {
        type: "CONTEXT_SIZE_RECEIVED",
        currentTokens: Number(evt.context_current_tokens) || 0,
        triggerTokens: Number(evt.context_trigger_tokens) || 0,
        maxTokens: Number(evt.context_max_tokens) || 0,
        messagesCount: Number(evt.context_messages_count) || 0,
      };
    case "fault_window": {
      // content is a JSON-stringified payload (result-event convention —
      // the wire type carries no per-phase fields). Map per phase; a
      // corrupt payload maps to null so the live path logs and the
      // resume fold skips, exactly like memory_compaction's unknown
      // phases. ``exit.reason === "aborted"`` arrives jsonl-only
      // (sidewrite evidence; the SSE side is terminated by error+done),
      // so the resume fold is the one place it ever renders — it still
      // just clears the slot, same as any other exit.
      let payload: Record<string, unknown> | null = null;
      try {
        const parsed: unknown = JSON.parse(evt.content);
        if (parsed && typeof parsed === "object") {
          payload = parsed as Record<string, unknown>;
        }
      } catch {
        payload = null;
      }
      if (!payload) return null;
      const phase = typeof payload["phase"] === "string" ? (payload["phase"] as string) : "";
      const remaining = Number(payload["remaining_sec"]) || 0;
      if (phase === "enter") {
        return {
          type: "FAULT_WINDOW_ENTERED",
          // evt.task_id IS the held turn id — the early-recover
          // endpoint (/sessions/{sid}/turns/{turn_id}/early-recover)
          // targets it, so it must ride the slot.
          turnId: evt.task_id ?? "",
          injectTaskId: typeof payload["inject_task_id"] === "string" ? (payload["inject_task_id"] as string) : "",
          durationSec: Number(payload["duration_sec"]) || 0,
          remainingSec: remaining,
        };
      }
      if (phase === "tick") {
        return { type: "FAULT_WINDOW_TICKED", remainingSec: remaining };
      }
      if (phase === "exit") {
        const raw = payload["reason"];
        const reason =
          raw === "elapsed" || raw === "early" || raw === "aborted"
            ? raw
            : "elapsed";
        return { type: "FAULT_WINDOW_EXITED", reason };
      }
      return null;
    }
    case "done":
      return null;
  }
}

/**
 * Pick a stable per-tool-call key. M5+ backends emit a real ``call_id``
 * (LangChain's ``run_id``); pre-M5 builds don't, so we fall back to
 * ``${task_id}/${tool_name}`` — unique only when the agent doesn't
 * invoke the same tool in parallel. Mirrored from useStream.
 */
function pickCallId(
  callId: string | undefined,
  taskId: string | undefined,
  name: string,
): string {
  if (callId && callId.length > 0) return callId;
  return `${taskId ?? "task"}/${name}`;
}

// ── Resume: events jsonl line → Actions ─────────────────────────────

/** One line of ``memory/tui/<sid>.events.jsonl``. Written by the
 *  server's sidewrite callback (turn_event_stream.py) — ``data`` is a
 *  serialized StreamEvent; ``source`` distinguishes user-side events
 *  (``user_input`` / ``confirm_answer``) that never flow over SSE. */
export interface SessionEventRecord {
  ts?: string;
  source?: string;
  task_id?: string;
  event_type?: string;
  data?: Record<string, unknown>;
}

const asStr = (v: unknown): string => (typeof v === "string" ? v : "");

/**
 * Map one jsonl record to its Actions (user-side events expand into
 * multi-action sequences; the shared StreamEvent passthrough emits
 * 0..1). Turn-boundary synthesis lives in ``foldSessionEvents`` — the
 * file-level view it needs (explicit done lines present?) is not
 * visible from a single record.
 */
export function sessionEventToActions(
  record: SessionEventRecord,
): Action[] {
  const eventType = asStr(record.event_type);
  const data = (record.data ?? {}) as Record<string, unknown>;
  const taskId = asStr(record.task_id);

  if (eventType === "user_input") {
    // The same action useStream's submitTurn dispatches before opening
    // the SSE stream — the user's echo lands in history.
    return [{ type: "TURN_STARTED", input: asStr(data["content"]) }];
  }
  if (eventType === "confirm_answer") {
    // The user's gate decision, expanded into the FULL three-action
    // live sequence (ConfirmMessage's Select → Composer effect):
    //
    //   1. CONFIRM_USER_DECIDED — records the decision semantics
    //      (``currentTurnRejected`` for rejected gates, so a later
    //      commitPending finalises the stepper as failed).
    //   2. CONFIRM_RESOLVED — marks the pending confirm card
    //      resolved (visual) + flips streamState back to responding,
    //      exactly as Composer's effect does after the network call.
    //   3. CONFIRM_DECISION_CONSUMED — clears the ``pendingDecision``
    //      handoff slot. CRITICAL: without this, Composer's
    //      pendingDecision useEffect (which fires the REAL network
    //      resolveInterrupt call) would observe the staged slot after
    //      the fold's render commit and re-fire a network call the
    //      server already processed at write time. The fold dispatch
    //      loop is synchronous, so React 18 batching coalesces the
    //      set-and-clear into one render and the effect never sees a
    //      non-null slot.
    //
    // Answer normalisation happened server-side at write time, so
    // ``content`` is already "approved"/"rejected"/raw text.
    const answer = asStr(data["content"]);
    return [
      { type: "CONFIRM_USER_DECIDED", taskId, answer },
      { type: "CONFIRM_RESOLVED", taskId, answer },
      { type: "CONFIRM_DECISION_CONSUMED" },
    ];
  }

  // token / thinking records in a REAL events file have ``data``
  // WITHOUT a ``type`` field: tui_session_store buffers them
  // (coalescing runs of chunks) and _flush_event_buffers hand-builds
  // ``{"content": ...}`` on the way out — unlike every other event,
  // which lands via StreamEvent.to_dict() and keeps its ``type``.
  // isStreamEvent would therefore reject them; map straight off the
  // outer ``event_type`` instead. ``node`` is absent on the flush
  // path (buffer meta doesn't carry it), so it falls back to "".
  if (eventType === "token" || eventType === "thinking") {
    const content = asStr(data["content"]);
    if (!content) return [];
    return [
      eventType === "token"
        ? { type: "TOKEN_APPENDED", content, node: asStr(data["node"]) }
        : { type: "THINKING_APPENDED", content, node: asStr(data["node"]) },
    ];
  }

  // Everything else is a serialized StreamEvent — run it through the
  // shared mapping. Unrecognised shapes (protocol drift, corrupt
  // salvage) are skipped, matching read_events' skip-and-warn.
  const evt = data as unknown;
  if (!isStreamEvent(evt)) return [];
  const mapped = streamEventToAction(evt as StreamEvent);
  return mapped === null ? [] : [mapped];
}

/**
 * Fold a whole events file into an Action list, honouring the
 * explicit-vs-synthesised ``done`` distinction.
 *
 * ``TURN_DONE`` rules, decided PER TURN SEGMENT (a segment runs from
 * one ``user_input`` record to the next — that sidewrite always opens
 * a turn, so it is the one reliable segment boundary):
 *  - Segment carries explicit ``done`` records (new sessions): map
 *    each to a TURN_DONE at its position — authoritative boundaries.
 *  - Segment has none (legacy): synthesize one after every ``result``
 *    / ``error`` record in THAT segment.
 *
 * Per-segment (not file-wide) because a session can mix both eras:
 * an old session resumed after the done-sidewrite shipped appends
 * new turns WITH done after legacy turns without. A file-wide check
 * would let the tail's done suppress the legacy turns' synthesis —
 * their content then sits in ``pending`` past the next
 * ``user_input``'s TURN_STARTED (``pending: []``) and is silently
 * dropped from the rebuilt history.
 */
export function foldSessionEvents(records: SessionEventRecord[]): Action[] {
  const turnSegments: SessionEventRecord[][] = [];
  let current: SessionEventRecord[] = [];
  for (const record of records) {
    if (asStr(record.event_type) === "user_input" && current.length > 0) {
      turnSegments.push(current);
      current = [];
    }
    current.push(record);
  }
  if (current.length > 0) turnSegments.push(current);

  const actions: Action[] = [];
  for (const segment of turnSegments) {
    const segmentHasDone = segment.some(
      (r) => asStr(r.event_type) === "done",
    );
    for (const record of segment) {
      const eventType = asStr(record.event_type);
      if (eventType === "done") {
        actions.push({ type: "TURN_DONE" });
        continue;
      }
      // Legacy synthesis only when THIS turn carries no explicit done.
      if (
        !segmentHasDone &&
        (eventType === "result" || eventType === "error")
      ) {
        // error needs its ERROR_RECEIVED first; result's RESULT_RECEIVED
        // comes from the shared mapping — both then get the boundary.
        const mapped = sessionEventToActions(record);
        actions.push(...mapped);
        // ERROR records whose data fails isStreamEvent still need the
        // boundary — the turn visibly ended even if the payload is odd.
        if (eventType === "error" && mapped.length === 0) {
          actions.push({
            type: "ERROR_RECEIVED",
            message: asStr((record.data ?? {})["content"]),
            taskId: asStr(record.task_id),
          });
        }
        actions.push({ type: "TURN_DONE" });
        continue;
      }
      actions.push(...sessionEventToActions(record));
    }
  }
  return actions;
}
