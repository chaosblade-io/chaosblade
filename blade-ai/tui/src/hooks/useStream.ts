/**
 * Bridge between the SSE client and the reducer.
 *
 * Public API:
 *
 *   const { submitTurn, cancelTurn, busy } = useStream(client, sessionId);
 *
 * - ``submitTurn(input)``: dispatches TURN_STARTED, opens an SSE stream,
 *   pumps each event into a corresponding reducer action, and dispatches
 *   TURN_DONE / TURN_ABORTED at the end.
 * - ``cancelTurn()``: aborts the in-flight fetch and POSTs to
 *   /sessions/:id/cancel so the server-side LangGraph task is cancelled.
 * - ``busy``: convenience flag from the reducer's streamState.
 */

import { useCallback, useRef } from "react";
import type { BladeClient } from "../api/client.js";
import type { StreamEvent } from "../api/events.js";
import { useAppDispatch, useAppSelector } from "../state/store.js";

export interface SubmitTurnOpts {
  /**
   * When true and a previous turn is still in flight, abort it
   * gracefully instead of as a user cancellation:
   *
   *   1. Dispatch TURN_TRANSITION so the OLD turn's pending (e.g.
   *      a resolved confirm card the user just answered) commits
   *      to history before TURN_STARTED's ``pending: []`` clear
   *      wipes it.
   *   2. Mark the upcoming abort as a "supersede" so OLD's catch
   *      block in this hook silently exits instead of dispatching
   *      TURN_ABORTED (which would inject a misleading
   *      "Cancelled by user" error item AND clobber streamState
   *      back to "idle" while NEW turn is still streaming).
   *
   * Used by ConfirmMessage's feedback path: after resolving a
   * confirm with rejection + free-form text, Composer's effect
   * fires ``submitTurn(feedback, { supersedePrevious: true })`` so
   * the typed text becomes the user's next conversation message
   * cleanly. Esc / Ctrl+C cancellation does NOT use this flag —
   * the user-facing TURN_ABORTED + error item is correct there.
   */
  supersedePrevious?: boolean;
  /**
   * Phase 3c.2 — when true, send ``dry_run: true`` to the server so
   * the agent runs intent + planning without actually injecting.
   * Used by the ``/plan <NL>`` slash command. Default false matches
   * the historical ``/run`` semantics so existing callers don't
   * change behaviour.
   */
  dryRun?: boolean;
}

export interface UseStreamApi {
  submitTurn: (input: string, opts?: SubmitTurnOpts) => Promise<void>;
  cancelTurn: () => void;
  resolveConfirm: (taskId: string, answer: "approved" | "rejected") => Promise<void>;
  /**
   * Allocate a fresh AbortController for a /replay run. Cancels any
   * previously-active replay before returning. The /replay handler
   * passes the resulting signal into ``replayRecording``.
   */
  beginReplay: () => AbortController;
  /** Abort the in-flight replay (called from Composer Esc handler). */
  cancelReplay: () => void;
  busy: boolean;
  /** Whether the agent is paused on a confirmation prompt. */
  awaitingConfirmation: boolean;
}

export function useStream(client: BladeClient, sessionId: string): UseStreamApi {
  const dispatch = useAppDispatch();
  const streamState = useAppSelector((s) => s.streamState);
  const permissionMode = useAppSelector((s) => s.config.permissionMode);
  const abortRef = useRef<AbortController | null>(null);
  /** Replay-only abort controller. Separate from ``abortRef`` (SSE
   * turn) because the two can never overlap (REPLAY_STARTED puts us
   * in pseudo-busy and InputPrompt is disabled), but we want to
   * route Esc to the *active* abort source without ambiguity. */
  const replayAbortRef = useRef<AbortController | null>(null);
  /**
   * Set true by ``submitTurn`` immediately before it aborts a
   * still-running previous turn IFF the caller passed
   * ``supersedePrevious: true``. The OLD turn's catch block reads
   * this on the next microtask and treats the AbortError as a
   * graceful supersede (no TURN_ABORTED dispatch, no error item),
   * then resets it to false so subsequent unrelated aborts (Esc
   * cancel, network drop) take the normal user-cancellation path.
   * Lives on a ref because both the setter (NEW submitTurn) and
   * the reader (OLD submitTurn's catch) hold their own closures —
   * a useState would only expose the value at each closure's mount
   * time, defeating the cross-invocation handoff.
   */
  const supersedeAbortRef = useRef<boolean>(false);

  const submitTurn = useCallback(
    async (input: string, opts?: SubmitTurnOpts) => {
      const trimmed = input.trim();
      if (!trimmed) return;

      // Graceful supersede path. Only when the caller asked for it
      // AND there's actually a previous turn to supersede. Two
      // sub-effects, both racing the next sync block:
      //   (1) Commit OLD's pending → history via TURN_TRANSITION,
      //       BEFORE the upcoming TURN_STARTED clears pending.
      //       Otherwise the resolved confirm card the user just
      //       answered (still in pending awaiting OLD's TURN_DONE)
      //       gets wiped and never reaches scrollback.
      //   (2) Flip ``supersedeAbortRef`` so OLD's for-await catch
      //       (firing in a microtask after the abort below) knows
      //       to silently exit instead of dispatching TURN_ABORTED.
      //       Otherwise OLD's catch would inject a misleading
      //       "Cancelled by user" error item AND TURN_ABORTED's
      //       commitPending would set streamState back to "idle"
      //       — re-enabling the InputPrompt while NEW turn is
      //       still streaming.
      //
      // The flag is consumed (reset to false) inside OLD's catch.
      // If for some reason OLD never throws (e.g. it raced past us
      // and finished cleanly), the flag stays true, which is fine
      // for the very next abort — that next abort would also be a
      // graceful supersede. Belt-and-braces: the catch resets it
      // unconditionally so unrelated future Esc cancellations get
      // the normal TURN_ABORTED path.
      if (opts?.supersedePrevious === true && abortRef.current != null) {
        dispatch({ type: "TURN_TRANSITION" });
        supersedeAbortRef.current = true;
      }
      // Cancel any leftover stream first (defensive — the UI should
      // already gate against a second submit while busy).
      abortRef.current?.abort();

      const controller = new AbortController();
      abortRef.current = controller;

      dispatch({ type: "TURN_STARTED", input: trimmed });

      // Stream throttling. SSE delivers token AND thinking events at the
      // LLM's emit rate — often 30–80 events/s during fluent generation
      // and during chain-of-thought streaming. Each event would
      // otherwise trigger a reducer dispatch → React re-render → Ink
      // dynamic-area redraw. At 30–80 dispatches/s the JS event loop
      // gets choked, which starves two timers we don't own:
      //
      //   - ``ink-spinner``'s setInterval (12.5 fps for "dots") —
      //     ticks land late, the spinner skips frames and looks
      //     "stuttery" instead of rotating.
      //   - ``useInput``'s key-event delivery — keystrokes queue
      //     behind reducer work, so typing into the InputPrompt feels
      //     laggy (50–200 ms perceived input lag during thinking).
      //
      // We coalesce consecutive same-kind events in small buffers and
      // flush on a 50ms timer (~20 FPS). qwen-code uses the same
      // pattern at 60ms; we keep 50ms for token-stream parity. Users
      // can't perceive 0–50ms latency on streamed text — characters
      // still appear in order, just in 1–4-char clumps instead of 1
      // at a time. Other event types (tool_start / tool_end / confirm
      // / result / error / done / node_start / node_end / usage)
      // trigger an immediate flush of BOTH buffers so the buffered
      // text appears BEFORE the structural event takes the dynamic
      // area in a different direction (e.g. agent text → tool group —
      // we want the text rendered before the tool box).
      //
      // Two parallel buffers (instead of qwen-code's single unified
      // queue) because tokens and thinking come from disjoint phases
      // of an LLM turn — they don't interleave in practice, so we
      // don't need to merge across kinds.
      let tokenBuffer = "";
      let tokenNode = "";
      let tokenFlushTimer: ReturnType<typeof setTimeout> | null = null;
      const flushTokens = () => {
        if (tokenFlushTimer) {
          clearTimeout(tokenFlushTimer);
          tokenFlushTimer = null;
        }
        if (tokenBuffer.length > 0) {
          dispatch({
            type: "TOKEN_APPENDED",
            content: tokenBuffer,
            node: tokenNode,
          });
          tokenBuffer = "";
          tokenNode = "";
        }
      };

      let thinkingBuffer = "";
      let thinkingNode = "";
      let thinkingFlushTimer: ReturnType<typeof setTimeout> | null = null;
      const flushThinking = () => {
        if (thinkingFlushTimer) {
          clearTimeout(thinkingFlushTimer);
          thinkingFlushTimer = null;
        }
        if (thinkingBuffer.length > 0) {
          dispatch({
            type: "THINKING_APPENDED",
            content: thinkingBuffer,
            node: thinkingNode,
          });
          thinkingBuffer = "";
          thinkingNode = "";
        }
      };

      const flushStreamBuffers = () => {
        flushTokens();
        flushThinking();
      };

      try {
        for await (const evt of client.streamTurn(
          sessionId,
          {
            input: trimmed,
            permission_mode: permissionMode,
            // Phase 3c.2 — only set dry_run when caller opted in.
            // streamTurn omits the field from the wire body when
            // falsy, so legacy ``/run`` paths stay byte-identical.
            dry_run: opts?.dryRun === true,
          },
          controller.signal,
        )) {
          if (evt.type === "token") {
            tokenBuffer += evt.content;
            if (evt.node) tokenNode = evt.node;
            if (!tokenFlushTimer) {
              tokenFlushTimer = setTimeout(flushTokens, 50);
            }
            continue;
          }
          if (evt.type === "thinking") {
            thinkingBuffer += evt.content;
            if (evt.node) thinkingNode = evt.node;
            if (!thinkingFlushTimer) {
              thinkingFlushTimer = setTimeout(flushThinking, 50);
            }
            continue;
          }
          // Structural event: drain BOTH buffers first so the streamed
          // text appears in scrollback before the tool/confirm/result
          // takes the dynamic area in a different direction.
          flushStreamBuffers();
          applyEvent(dispatch, evt);
          if (evt.type === "done") break;
        }
        // Drain anything that arrived in the final 50ms window before
        // ``done`` (or before the iterator ended naturally).
        flushStreamBuffers();
        dispatch({ type: "TURN_DONE" });
      } catch (err) {
        if (controller.signal.aborted) {
          // Was this abort triggered by a graceful supersede (e.g.
          // ConfirmMessage's feedback path firing
          // ``submitTurn(feedback, { supersedePrevious: true })``)?
          // If so, skip TURN_ABORTED entirely — the new turn has
          // already taken over the screen, OLD's pending was
          // committed via TURN_TRANSITION before this catch ran,
          // and dispatching TURN_ABORTED here would (a) inject a
          // bogus "Cancelled by user" error item into scrollback
          // and (b) flip streamState back to "idle" via
          // commitPending, prematurely re-enabling the InputPrompt
          // while NEW turn is mid-stream.
          //
          // Always reset the flag (regardless of whether it fired)
          // so the NEXT unrelated abort — Esc cancellation, network
          // drop, etc. — takes the normal user-cancellation path.
          if (supersedeAbortRef.current) {
            supersedeAbortRef.current = false;
            return;
          }
          dispatch({ type: "TURN_ABORTED", reason: "Cancelled by user" });
          return;
        }
        const reason = err instanceof Error ? err.message : String(err);
        dispatch({ type: "TURN_ABORTED", reason });
      } finally {
        // Cancel any pending flush timers so they can't fire dispatches
        // after TURN_DONE / TURN_ABORTED has resolved. (Throttle window
        // is 50ms; without this, an aborted turn could append one final
        // batch of tokens or thinking content to the previous turn.)
        if (tokenFlushTimer) {
          clearTimeout(tokenFlushTimer);
          tokenFlushTimer = null;
        }
        if (thinkingFlushTimer) {
          clearTimeout(thinkingFlushTimer);
          thinkingFlushTimer = null;
        }
        // Definitively close the underlying HTTP request. Without this,
        // Node's fetch (undici) returns the connection to its keep-alive
        // pool in a state that the server side may have already
        // half-closed, and the NEXT request — typically the exit
        // path's PATCH /sessions/:id/stats — picks up that stale
        // socket and hangs until its own AbortSignal fires (~3s).
        // Aborting after a clean stream completion is a no-op for the
        // already-finished response but guarantees the connection is
        // gone from the pool.
        try {
          controller.abort();
        } catch {
          // ignore
        }
        if (abortRef.current === controller) {
          abortRef.current = null;
        }
      }
    },
    [client, sessionId, dispatch, permissionMode],
  );

  const cancelTurn = useCallback(() => {
    // Only act when there's actually an SSE turn in flight. Composer's
    // Esc handler unconditionally calls both ``cancelTurn`` and
    // ``cancelReplay`` (the protocol-level idempotency contract);
    // without this guard, a /replay-only run still POSTs to
    // ``/cancel`` for every Esc — a wasted round trip per keystroke.
    if (!abortRef.current) return;
    abortRef.current.abort();
    // Best-effort: the server may already have finished or be
    // shutting down; we don't want to surface failures here.
    client.cancelTurn(sessionId).catch(() => undefined);
  }, [client, sessionId]);

  const resolveConfirm = useCallback(
    async (taskId: string, answer: "approved" | "rejected") => {
      // Optimistically reflect the answer in the UI; the SSE stream
      // is still open and will continue spitting events after the
      // server runs Command(resume=...).
      dispatch({ type: "CONFIRM_RESOLVED", taskId, answer });
      try {
        await client.resolveInterrupt(sessionId, {
          interrupt_id: taskId,
          answer,
        });
      } catch (err) {
        // On HTTP failure (network blip, server gone), surface as a
        // turn-ending error so the UI doesn't hang in
        // ``waiting_confirmation`` forever.
        const reason = err instanceof Error ? err.message : String(err);
        dispatch({ type: "TURN_ABORTED", reason: `confirm failed: ${reason}` });
        abortRef.current?.abort();
      }
    },
    [client, sessionId, dispatch],
  );

  const beginReplay = useCallback((): AbortController => {
    // Cancel any leftover replay first — only one at a time. The
    // existing controller's listeners are torn down inside the
    // sleep() helper in utils/replay.ts.
    replayAbortRef.current?.abort();
    const ctrl = new AbortController();
    replayAbortRef.current = ctrl;
    return ctrl;
  }, []);

  const cancelReplay = useCallback((): void => {
    replayAbortRef.current?.abort();
    // Don't null the ref here — the active replay's await sleep()
    // is racing the abort; let beginReplay() replace it on next call.
  }, []);

  return {
    submitTurn,
    cancelTurn,
    resolveConfirm,
    beginReplay,
    cancelReplay,
    busy:
      streamState === "responding" ||
      streamState === "waiting_confirmation",
    awaitingConfirmation: streamState === "waiting_confirmation",
  };
}

function applyEvent(
  dispatch: (a: import("../state/reducer.js").Action) => void,
  evt: StreamEvent,
): void {
  switch (evt.type) {
    case "token":
      dispatch({
        type: "TOKEN_APPENDED",
        content: evt.content,
        node: evt.node ?? "",
      });
      return;
    case "thinking":
      dispatch({
        type: "THINKING_APPENDED",
        content: evt.content,
        node: evt.node ?? "",
      });
      return;
    case "tool_start":
      dispatch({
        type: "TOOL_STARTED",
        callId: pickCallId(evt.call_id, evt.task_id, evt.tool_name),
        name: evt.tool_name,
        node: evt.node ?? "",
      });
      return;
    case "tool_end":
      dispatch({
        type: "TOOL_ENDED",
        callId: pickCallId(evt.call_id, evt.task_id, evt.tool_name),
        name: evt.tool_name,
        status: "success",
        content: evt.content,
      });
      return;
    case "node_start":
      dispatch({ type: "NODE_STARTED", node: evt.node, phase: evt.phase });
      return;
    case "node_end":
      dispatch({ type: "NODE_ENDED", node: evt.node });
      return;
    case "confirm":
      dispatch({
        type: "CONFIRM_RECEIVED",
        content: evt.content,
        taskId: evt.task_id,
        node: evt.node,
        payload: evt.payload,
      });
      return;
    case "result":
      dispatch({
        type: "RESULT_RECEIVED",
        content: evt.content,
        taskId: evt.task_id,
      });
      return;
    case "error":
      dispatch({
        type: "ERROR_RECEIVED",
        message: evt.content,
        taskId: evt.task_id,
      });
      return;
    case "usage":
      // Coerce undefined → 0 here so the action shape stays accurate
      // (``inputTokens: number``, not ``number | undefined``). Older
      // servers can drop a 0 field entirely from the wire frame; the
      // reducer also defends against ``NaN`` in case any future caller
      // forgets this nullish guard.
      dispatch({
        type: "USAGE_RECEIVED",
        inputTokens: evt.input_tokens ?? 0,
        outputTokens: evt.output_tokens ?? 0,
      });
      return;
    case "memory_compaction": {
      // Phase 4 — server emits started / completed / failed lifecycle
      // events. The wire frame's falsy-strip drops 0 fields, so a
      // ``started`` event arrives without ``tokens_after`` /
      // ``messages_compacted`` / ``duration_ms`` (all unknown at
      // start time); we defend with ``?? 0`` everywhere. The phase
      // discriminator should always be present, but defaults to
      // ``"started"`` to keep the spinner showing if the server
      // forgets to populate it.
      const phase = evt.compaction_phase ?? "started";
      const layer = evt.layer ?? "llm_summary";
      const tokensBefore = evt.tokens_before ?? 0;
      if (phase === "started") {
        dispatch({
          type: "MEMORY_COMPACTION_STARTED",
          tokensBefore,
          layer,
        });
      } else if (phase === "completed") {
        dispatch({
          type: "MEMORY_COMPACTION_COMPLETED",
          tokensBefore,
          tokensAfter: evt.tokens_after ?? 0,
          messagesCompacted: evt.messages_compacted ?? 0,
          durationMs: evt.duration_ms ?? 0,
          layer,
        });
      } else if (phase === "failed") {
        dispatch({
          type: "MEMORY_COMPACTION_FAILED",
          tokensBefore,
          durationMs: evt.duration_ms ?? 0,
          layer,
          // ``content`` carries the exception message on the wire
          // (free-form). Preserve it so the user sees a real reason
          // instead of "compaction failed".
          errorMessage: evt.content ?? "",
        });
      } else {
        // Unknown phase — protocol drift between server and client.
        // Logged so dev-time mismatches surface; production noise
        // is bounded since the only way to land here is a server
        // change that bypassed the client update.
        // eslint-disable-next-line no-console
        console.warn(
          `[useStream] unknown memory_compaction phase: ${String(phase)}`,
        );
      }
      return;
    }
    case "done":
      // Handled by the caller's loop — break + dispatch TURN_DONE.
      return;
  }
}

/**
 * Pick a stable per-tool-call key. M5+ backends emit a real
 * ``call_id`` (LangChain's ``run_id``); pre-M5 builds don't, so we
 * fall back to ``${task_id}/${tool_name}`` — unique only when the
 * agent doesn't invoke the same tool in parallel.
 *
 * The TS-side TOOL_ENDED matcher uses ``matched`` early-out so even
 * the fallback case at worst marks one wrong instance done — not the
 * end of the world, just a UI quirk on legacy servers.
 */
function pickCallId(
  callId: string | undefined,
  taskId: string | undefined,
  name: string,
): string {
  if (callId && callId.length > 0) return callId;
  return `${taskId ?? "task"}/${name}`;
}
