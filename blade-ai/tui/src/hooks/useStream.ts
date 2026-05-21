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
import {
  resetStreamingCounters,
  streamingResponseCharsRef,
} from "../state/streamingRefs.js";
import { perfFlush, perfMark } from "../utils/perfTrace.js";

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
  /**
   * Allocate a fresh AbortController for a manual /compact run.
   * Mirror of ``beginReplay`` — gives the /compact slash handler an
   * AbortSignal to pass into ``streamCompactSession`` so Esc from
   * the Composer can interrupt the in-flight compaction. Cancels
   * any previously-active manual compact before returning (defensive
   * — UI gates against concurrent /compact runs anyway).
   */
  beginManualCompact: () => AbortController;
  /** Abort the in-flight manual /compact (called from Composer Esc
   *  handler when ``state.currentManualCompact`` is non-null). */
  cancelManualCompact: () => void;
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
      // Phase 2.1 — reset the module-level streaming counters so the
      // live tokens estimate in LoadingIndicator starts from 0 for
      // this turn. The ref is read by ``useAnimationFrame`` inside
      // ``useLoadingIndicator``; snap-down on decrease clears any
      // stale value the previous turn left before the next animation
      // frame fires. Other turn-start paths (REPLAY_STARTED) call the
      // same helper from their handlers — keep them in sync.
      resetStreamingCounters();

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
      // flush on a per-kind throttle timer. The two kinds use
      // DIFFERENT throttle windows because they have different visual
      // costs:
      //
      //   - ``TOKEN_THROTTLE_MS = 60`` — token events grow the agent
      //     message in pending, which forces a redraw of the visible
      //     pending body on every dispatch. The probe used to show
      //     this as the dominant flicker source at 8Hz natural token
      //     arrival × 13-row pending payload (5–15Hz being the worst
      //     range for human perception), and we previously parked at
      //     180ms to drop the rate to ~5Hz.
      //
      //     Phase 2 erased that ceiling: AgentMessage now caps the
      //     pending body to ``PENDING_AGENT_MAX_VISIBLE = 8`` rows
      //     (qwen-code-style MaxSizedBox), so the redraw payload is
      //     small and constant regardless of total reply size; every
      //     downstream history component is wrapped in ``React.memo``
      //     so the per-token MainContent re-render walks only the
      //     pending area; Composer subscribes narrowly to its slice
      //     of AppStore so the chrome no longer re-renders per
      //     dispatch. With those guards, 60ms (~16Hz) is the new
      //     sweet spot — close to qwen-code / Claude Code (60–80ms),
      //     short enough that prose streams letter-fluid without
      //     bunching, and the small bounded redraw payload doesn't
      //     reintroduce flicker.
      //
      //     If a future change removes the AgentMessage cap or the
      //     memo wrappers, raise this back to ~180 — without them
      //     16Hz of unbounded redraws will shimmer again.
      //
      //   - ``THINKING_THROTTLE_MS = 50`` — thinking events update
      //     ``state.thoughtBuffer``, which only drives
      //     ``LoadingIndicator``'s ``headerLabel`` (a single text
      //     row, no body rendering since the qwen-code-style
      //     single-line redesign). The dyn-frame height is constant
      //     during thinking, so the redraw is cheap. Keeping 50ms
      //     here means the cycler/thinking subject feels responsive
      //     without paying the token-streaming flicker cost.
      //
      // Other event types (tool_start / tool_end / confirm / result
      // / error / done / node_start / node_end / usage) trigger an
      // immediate flush of BOTH buffers so the buffered text appears
      // BEFORE the structural event takes the dynamic area in a
      // different direction (e.g. agent text → tool group — we want
      // the text rendered before the tool box).
      //
      // Two parallel buffers (instead of qwen-code's single unified
      // queue) because tokens and thinking come from disjoint phases
      // of an LLM turn — they don't interleave in practice, so we
      // don't need to merge across kinds.
      // Throttle windows — see the comment block above for why these
      // two kinds use different values.
      const TOKEN_THROTTLE_MS = 60;
      const THINKING_THROTTLE_MS = 50;

      let tokenBuffer = "";
      let tokenNode = "";
      let tokenFlushTimer: ReturnType<typeof setTimeout> | null = null;
      const flushTokens = () => {
        if (tokenFlushTimer) {
          clearTimeout(tokenFlushTimer);
          tokenFlushTimer = null;
        }
        if (tokenBuffer.length > 0) {
          // Perf trace #2: every TOKEN_APPENDED dispatch fire. Compare
          // count to token.raw to see throttle ratio; compare ts
          // deltas to see actual dispatch cadence vs the 60 ms target.
          perfMark("flushTokens", { len: tokenBuffer.length });
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
            // Phase 2.1 — increment the module-level char counter on
            // EVERY raw token event (not throttled with the dispatch).
            // The producer side is intentionally zero-React-work; the
            // consumer (``useAnimationFrame`` in LoadingIndicator)
            // polls this ref at a fixed cadence and tweens the
            // displayed value smoothly. This lets the live tokens
            // figure climb at ~10 Hz visual smoothness regardless of
            // the bursty LLM emission pattern (often "wait 200ms,
            // burst 60 chars, wait 200ms…") that the previous
            // dispatch-driven counter exposed as a 5 Hz step ladder.
            //
            // ``signal.aborted`` guard: under supersedePrevious (e.g.
            // ConfirmMessage's feedback path firing
            // ``submitTurn(feedback, { supersedePrevious: true })``),
            // OLD's for-await may consume one or two more SSE events
            // between the abort() call and the underlying reader
            // throwing AbortError. Those late events would otherwise
            // increment the ref *after* NEW's TURN_STARTED reset it
            // to 0, producing a small phantom offset on the new
            // turn's live tokens display. Skipping the ref bump on
            // an already-aborted controller keeps the counter clean.
            // We do NOT skip the buffer/timer paths the same way:
            // those drain inside the catch's ``finally`` block, so
            // the leftover bytes are harmless (timers cleared, no
            // dispatch fires after the catch returns).
            if (!controller.signal.aborted) {
              streamingResponseCharsRef.current += evt.content.length;
            }
            // Perf trace #1: raw SSE token event rate. Lets us see
            // actual LLM emit rate independent of throttled dispatch.
            perfMark("token.raw", { bytes: evt.content.length });
            if (evt.node) tokenNode = evt.node;
            if (!tokenFlushTimer) {
              tokenFlushTimer = setTimeout(flushTokens, TOKEN_THROTTLE_MS);
            }
            continue;
          }
          if (evt.type === "thinking") {
            thinkingBuffer += evt.content;
            if (evt.node) thinkingNode = evt.node;
            if (!thinkingFlushTimer) {
              thinkingFlushTimer = setTimeout(
                flushThinking,
                THINKING_THROTTLE_MS,
              );
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
        // Drain anything still buffered before ``done`` (or before
        // the iterator ended naturally). Worst case = TOKEN_THROTTLE_MS
        // (200) of pending text — ``flushStreamBuffers`` clears the
        // timers and dispatches synchronously so the final reply chunk
        // is visible before TURN_DONE flips the UI to idle.
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
        // after TURN_DONE / TURN_ABORTED has resolved. (Throttle windows
        // are 200ms tokens / 50ms thinking; without this, an aborted
        // turn could append one final batch of tokens or thinking
        // content to the previous turn after the abort handler ran.)
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
        // Perf trace #5: flush the buffered marks to disk now that
        // the turn boundary has resolved (TURN_DONE / TURN_ABORTED
        // / supersede). Writing here keeps the log file in sync
        // with what the user just saw on screen, so reading the
        // log post-mortem maps cleanly to "this turn took N ms".
        perfFlush("turn-end");
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

  /** Manual /compact AbortController — separate from turn and replay
   *  because /compact runs while ``streamState === "idle"`` (its own
   *  busy gate is ``state.currentManualCompact !== null``). Keeping
   *  three independent refs means each surface's Esc handler can
   *  target exactly the right in-flight operation without ambiguity. */
  const manualCompactAbortRef = useRef<AbortController | null>(null);

  const beginManualCompact = useCallback((): AbortController => {
    // Defensive: cancel any leftover compact first. UI gates against
    // concurrent /compact calls (the handler refuses if
    // currentManualCompact is already set), so this should be a
    // no-op in normal flow.
    manualCompactAbortRef.current?.abort();
    const ctrl = new AbortController();
    manualCompactAbortRef.current = ctrl;
    return ctrl;
  }, []);

  const cancelManualCompact = useCallback((): void => {
    manualCompactAbortRef.current?.abort();
    // Don't null here — the active for-await is racing the abort.
    // beginManualCompact() replaces the ref on next call.
  }, []);

  return {
    submitTurn,
    cancelTurn,
    resolveConfirm,
    beginReplay,
    cancelReplay,
    beginManualCompact,
    cancelManualCompact,
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
        // Legacy /turn results put the envelope in content as a
        // JSON string. The newer /compact route uses payload as a
        // typed dict and leaves content empty. Fall back to a
        // JSON-stringified payload so RESULT_RECEIVED's reducer
        // (which expects a string) sees the same shape either
        // way. The /compact handler doesn't go through this
        // dispatcher — it consumes its own stream directly — so
        // this fallback is just defensive in case some future
        // surface routes /compact-style events through useStream.
        content: evt.content ?? JSON.stringify(evt.payload ?? {}),
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
    case "context_size":
      // PreReasoningHook fired one of these AFTER deciding whether
      // to compact. The post-hook ``current_tokens`` is what the
      // NEXT LLM call will see, so dispatching it drives the
      // Footer indicator to visibly drop on compaction and grow as
      // tool messages stack up. All four fields are forced onto the
      // wire by the server (see streaming.py to_dict exception), so
      // ``Number(x) || 0`` is safe even when the actual value is 0.
      dispatch({
        type: "CONTEXT_SIZE_RECEIVED",
        currentTokens: Number(evt.context_current_tokens) || 0,
        triggerTokens: Number(evt.context_trigger_tokens) || 0,
        maxTokens: Number(evt.context_max_tokens) || 0,
        messagesCount: Number(evt.context_messages_count) || 0,
      });
      return;
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
