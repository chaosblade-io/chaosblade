/**
 * Bottom region — owns the LoadingIndicator + (optional sticky) todo
 * list + InputPrompt + Footer stack. Connects user input to the SSE
 * stream.
 *
 * Bottom-region layout (top → bottom):
 *
 *   ⠋ thinking …                       ← LoadingIndicator (dynamic)
 *   ╭ ⚡ Inject todos ────────────╮    ← active todo list (sticky)
 *   │ 1. ✔ Intent                  │      pinned RIGHT ABOVE the
 *   │ 2. ⚡ Plan                    │      InputPrompt while the turn
 *   │ 3. ○ Safety check            │      is in flight
 *   │ 4. ○ Inject                  │
 *   │ 5. ○ Verify                  │
 *   ╰─────────────────────────────╯
 *   ─────────────────────────────────  ← InputPrompt fence
 *   ❯ ▌ Type your message · /help…
 *   ─────────────────────────────────
 *   ? for help            confirm · ns:default   ← Footer
 *
 * Sticky todo list:
 *   - The active inject-pipeline strip lives in
 *     ``state.currentPhaseStepper`` (NOT in pending — it would
 *     otherwise block the leading-stable flush). Composer pins the
 *     strip directly above the InputPrompt so it stays visible
 *     regardless of how much agent output streams above it. At
 *     TURN_DONE / TURN_ABORTED ``commitPending`` finalises the
 *     strip and appends it to pending → Static history.
 *
 * Why LoadingIndicator goes ABOVE the strip:
 *   The user reads the todo list as "the work that's queued for the
 *   slot I'm typing into". The thinking / replying spinner is
 *   transient agent chrome — sticking it BETWEEN the strip and the
 *   InputPrompt visually pushed the strip away from the input on
 *   every spinner tick. Putting LoadingIndicator above the strip
 *   keeps the strip → InputPrompt anchor stable across stream
 *   states; the spinner slot is empty when no LLM call is in
 *   flight, so it doesn't add chrome on idle either.
 *
 * Visibility rules:
 *   - awaiting confirmation: LoadingIndicator HIDDEN, InputPrompt
 *     rendered ``disabled`` (passive dim, no cursor). The ``Select``
 *     widget inside the ``ConfirmMessage`` card owns the keyboard
 *     for the dialog. The spinner is intentionally suppressed in
 *     this state — its 12.5 fps tick was forcing fullscreen redraws
 *     on a confirm card taller than the viewport, producing visible
 *     flicker + scroll-position hijack while the user reads the
 *     dialog. ``isStreaming`` stays ``true`` for callers that gate
 *     on "turn in flight".
 *   - responding (no confirm): LoadingIndicator visible, InputPrompt
 *     rendered ``enterLocked`` — full active visual + typing accepted
 *     so the user can draft their next message while the agent
 *     finishes, but Enter does not queue a second submit and
 *     Esc / Ctrl+C defer to Composer's cancel-turn handler instead
 *     of clearing the local buffer or closing the app.
 *   - idle: LoadingIndicator hidden, InputPrompt fully active.
 *
 * Footer is always visible (subject to terminal width >= 40 cols);
 * it carries the session-signal line that anchors the bottom region
 * across every state transition.
 *
 * Esc / Ctrl+C inside the InputPrompt:
 *   - while busy + waiting_confirmation → ConfirmMessage's Select
 *     owns Esc (translates to "rejected"); Composer doesn't react.
 *   - while busy (responding) → cancel the turn
 *   - while idle with text  → clear the text
 *   - while idle empty → onExit (closes the app)
 *
 * Cross-component pubsub:
 *   - ConfirmMessage's Select dispatches CONFIRM_USER_DECIDED on
 *     selection / feedback submit. State.pendingDecision is the
 *     handoff slot; Composer's useEffect picks it up and runs the
 *     network calls (resolveInterrupt, optional follow-up
 *     submitTurn for free-form feedback). Only Composer holds the
 *     useStream instance with the abort controllers + SSE iterator,
 *     so the network side-effects belong here even though the UI
 *     event originates inside the confirm card.
 */

import { Box, useApp, useInput } from "ink";
import { useCallback, useEffect, useMemo, useRef } from "react";
import type { BladeClient } from "../api/client.js";
import { useStream } from "../hooks/useStream.js";
import { t } from "../i18n/index.js";
import {
  useAppDispatch,
  useAppSelector,
  useAppStateGetter,
} from "../state/store.js";
import {
  buildRegistry,
  parseSlashCommand,
  parseSlashLine,
  type SlashCommandContext,
} from "../state/commands.js";
import { Footer } from "./Footer.js";
import { InputPrompt } from "./InputPrompt.js";
import { LoadingIndicator } from "./LoadingIndicator.js";
import { ManualCompactIndicator } from "./ManualCompactIndicator.js";
import { MemoryCompactingIndicator } from "./MemoryCompactingIndicator.js";
import { PhaseStepperCard } from "./PhaseStepperCard.js";
import {
  setProbeControlsRef,
  setProbeFooterRef,
  setProbeInputRef,
  setProbeLoadingRef,
  setProbeStepperRef,
} from "../utils/overflowProbe.js";
import { setChromeMeasureRef } from "../state/chromeMeasureRef.js";
import type { DOMElement } from "ink";
import { getPool, pickRandomDistinct } from "../utils/phrasePool.js";

interface Props {
  client: BladeClient;
  sessionId: string;
}

export const Composer: React.FC<Props> = ({ client, sessionId }) => {
  const {
    submitTurn,
    cancelTurn,
    resolveConfirm,
    beginReplay,
    cancelReplay,
    beginManualCompact,
    cancelManualCompact,
    busy,
    awaitingConfirmation,
  } = useStream(client, sessionId);
  // Phase 1.2 — replaced ``useAppState()`` (whole-tree subscription
  // that re-rendered Composer on every reducer dispatch) with two
  // narrow tools:
  //   · ``useAppSelector(s => s.streamState)`` — primitive, re-renders
  //     this component ONLY when streamState transitions.
  //   · ``useAppStateGetter()`` — returns a stable getter that reads
  //     the latest state on demand (slash command handlers need a
  //     state snapshot at invoke time; they don't need a live binding).
  // Combined: Composer used to re-render on every TOKEN_APPENDED;
  // now it re-renders only on the small set of state slices it
  // actually needs.
  const streamState = useAppSelector((s) => s.streamState);
  const getAppState = useAppStateGetter();
  const dispatch = useAppDispatch();
  const app = useApp();
  const pendingDecision = useAppSelector((s) => s.pendingDecision);
  // Sticky stepper. Lives in its own state slot (``currentPhaseStepper``)
  // — NOT in ``pending`` — so its mid-turn mutation doesn't block the
  // leading-stable flush in TOKEN_APPENDED. With the stepper at
  // pending[0] (its old home) every thinking / tool_group sat behind
  // it stayed pending all the way to TURN_DONE, growing the dynamic
  // area past stdout.rows and tripping Ink's fullscreen-redraw
  // branch on every frame — the visible flicker + scroll-position
  // thrash users saw during inject. Reading from a dedicated slot
  // here keeps the flush window open while still rendering the
  // strip pinned above the InputPrompt.
  const activeStepper = useAppSelector((s) => s.currentPhaseStepper);
  // Phase 4 — narrow selector so the indicator only re-renders on
  // the slot's own transitions (null ↔ object), not on every token /
  // phase event during streaming. The selector returns a primitive
  // boolean for the conditional mount; the indicator itself reads
  // the slot fields via its own narrow selector inside the
  // component.
  const compactionInFlight = useAppSelector(
    (s) => s.currentCompaction !== null,
  );

  // Single registry for the lifetime of the Composer. Built once;
  // commands are static for now (no skill-driven dynamic entries yet).
  const registry = useMemo(() => buildRegistry(), []);

  // Composite ref callback for the outermost controls Box. Fans the
  // same DOM element to two consumers:
  //
  //   1. ``setProbeControlsRef`` — the BLADE_AI_DEBUG_OVERFLOW probe,
  //      already there for diagnostics.
  //   2. ``setChromeMeasureRef`` — Phase 3.2 measureElement source.
  //      MainContent reads this in a ``useLayoutEffect`` and calls
  //      ``measureElement`` to learn the chrome height precisely,
  //      replacing the prior hard-coded ``CHROME_ROWS_RESERVE=26``.
  //
  // useCallback keeps the function identity stable across re-renders
  // so Ink doesn't unmount + re-mount the Box ref every time Composer
  // re-renders. Both setters are module-level functions with stable
  // identity, so empty deps are correct.
  const attachControlsRef = useCallback((el: DOMElement | null) => {
    setProbeControlsRef(el);
    setChromeMeasureRef(el);
  }, []);

  // Composer's Esc/Ctrl+C handling now only fires when ``busy`` AND
  // we're NOT waiting on a confirm dialog — Select owns the keyboard
  // during waiting_confirmation. Without this gate, both
  // useInput handlers would fire on the same Esc keystroke and the
  // user would (a) reject the confirm via Select AND (b) cancel
  // the whole turn via Composer in one keystroke. The gate also
  // means there's no need for the old Y/N branch anymore — Select
  // dispatches the user's choice through CONFIRM_USER_DECIDED and
  // the effect below runs the network calls.
  // Manual /compact in-flight flag — gates a separate Esc binding
  // below because /compact runs while ``streamState === "idle"``
  // (so ``busy`` is false and the existing turn/replay binding is
  // inactive). Narrow selector — only flips when the slot opens or
  // closes, not on every reducer dispatch.
  const manualCompactInFlight = useAppSelector(
    (s) => s.currentManualCompact !== null,
  );

  useInput(
    (input, key) => {
      const isExitKey = key.escape || (key.ctrl && input === "c");
      if (!isExitKey) return;
      // Busy + responding without a confirm. Cancel both possible
      // sources of busy-ness. cancelTurn aborts the SSE fetch
      // (no-op for replay); cancelReplay aborts the setTimeout
      // chain in utils/replay.ts (no-op when no replay). Both
      // idempotent so we can call them blindly without a
      // turn-vs-replay discriminator.
      cancelReplay();
      cancelTurn();
    },
    { isActive: busy && !awaitingConfirmation },
  );

  // Separate Esc binding for manual /compact. Reason for a second
  // useInput rather than folding it into the one above: /compact
  // runs with ``busy === false`` (it's not a /turn), so reusing
  // the turn binding's gate would force ``busy`` to be true during
  // /compact, which would re-enable the LoadingIndicator and lock
  // the InputPrompt for the wrong reason. Two narrow bindings keep
  // each side's "what does Esc mean here?" explicit.
  useInput(
    (input, key) => {
      const isExitKey = key.escape || (key.ctrl && input === "c");
      if (!isExitKey) return;
      cancelManualCompact();
    },
    { isActive: manualCompactInFlight },
  );

  // Ctrl+O — toggle ``constrainHeight``. Always active so the user
  // can flip pending-item height-cap regardless of stream state. The
  // dispatch is a single boolean flip; downstream MainContent /
  // LoadingIndicator / ToolMessage / AgentMessage read the flag and
  // either route ``availableTerminalHeight`` through MaxSizedBox or
  // bypass the cap entirely.
  //
  // Two-shape match because Ink's keypress decoder is inconsistent
  // across terminal emulators for control combos: most terminals
  // surface ``key.ctrl=true`` + ``input="o"``, but macOS Terminal
  // (and a few others) deliver the literal SO control byte
  // ```` (Ctrl+O = 15 = 0x0f) with ``key.ctrl=false``. Catch
  // both so the binding works everywhere.
  useInput(
    (input, key) => {
      const isCtrlO =
        (key.ctrl && input === "o") || input === "";
      if (isCtrlO) {
        dispatch({ type: "CONSTRAIN_HEIGHT_TOGGLED" });
      }
    },
    { isActive: true },
  );

  // ConfirmMessage's Select dispatches CONFIRM_USER_DECIDED into
  // ``state.pendingDecision``. We pick it up here, run the network
  // side-effects on Composer's useStream instance, and clear the
  // slot. Two-step for the feedback case: first ``resolveConfirm``
  // closes the confirm gate (server resumes graph with rejected),
  // then ``submitTurn`` fires a fresh user turn with the typed text
  // so the agent treats it as the next message in the conversation.
  //
  // We pass ``supersedePrevious: true`` to the feedback follow-up
  // so submitTurn:
  //   - dispatches TURN_TRANSITION first (commits OLD turn's
  //     pending — most importantly, the resolved confirm card —
  //     to history before TURN_STARTED's ``pending: []`` clear
  //     wipes it),
  //   - marks its abort of OLD's SSE as a graceful handoff so
  //     OLD's catch silently exits instead of dispatching
  //     TURN_ABORTED ("Cancelled by user" error + streamState
  //     corruption to "idle" mid-stream).
  //
  // Esc/Ctrl+C cancellation (the cancelTurn path) does NOT use
  // this flag — the user-facing TURN_ABORTED + error item is the
  // correct UX there.
  useEffect(() => {
    if (!pendingDecision) return;
    const { taskId, answer, feedback } = pendingDecision;
    let cancelled = false;
    void (async () => {
      try {
        await resolveConfirm(taskId, answer);
        if (cancelled) return;
        if (feedback != null && feedback.trim().length > 0) {
          await submitTurn(feedback, { supersedePrevious: true });
        }
      } finally {
        if (!cancelled) {
          dispatch({ type: "CONFIRM_DECISION_CONSUMED" });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pendingDecision, dispatch, resolveConfirm, submitTurn]);

  // Phrase cycler driver — ticks ``PHRASE_TICK`` every 8s while a
  // turn is in flight so the LoadingIndicator's fallback header label
  // rotates through the i18n phrase pool. Without this rotation,
  // dead-air windows (between thinking sessions, before the first
  // tool, after a tool ends but before the next event) sit on a
  // static label and the user has no liveness signal beyond the
  // second counter.
  //
  // 8s cadence trade-off: long enough that the eye registers the
  // change as deliberate (qwen-code uses 15s, which felt sluggish for
  // our typical 30-60s inject turns); short enough that even ~10s
  // replies see at least one rotation. Going below ~5s starts to
  // feel like distracting flicker.
  //
  // Gating:
  //   * ``streamState === "responding"`` — same condition that makes
  //     the LoadingIndicator visible. No point cycling when nothing
  //     is consuming the value.
  //   * ``!compactionInFlight`` — during memory compaction the
  //     ``MemoryCompactingIndicator`` owns the spinner slot
  //     (single-spinner mutex). Pausing here prevents a phrase tick
  //     leaking out at the moment compaction ends.
  //
  // De-dup with ``useRef``: the previous phrase is tracked in a ref
  // (not in the effect's closed-over state) so successive ticks on
  // the same active session see the *latest* picked phrase rather
  // than the snapshot at effect-mount time. Without the ref the
  // ``pickRandomDistinct`` call would always see ``""`` and degrade
  // to plain uniform random — losing the "no immediate repeat"
  // guarantee. Adding ``state.idlePhrase`` to deps would fix the
  // staleness but tear down + rebuild the interval on every tick,
  // breaking the 8s cadence.
  //
  // The reducer stays pure: ``Math.random`` is invoked here in the
  // dispatcher and the chosen phrase is pre-baked into the action
  // payload. The reducer also early-returns when the new phrase
  // equals the current one (degenerate single-entry pools), avoiding
  // a no-op re-render.
  const lastPhraseRef = useRef<string>("");
  useEffect(() => {
    const active = streamState === "responding" && !compactionInFlight;
    if (!active) return;
    const tick = () => {
      const pool = getPool();
      const next = pickRandomDistinct(pool, lastPhraseRef.current);
      lastPhraseRef.current = next;
      dispatch({ type: "PHRASE_TICK", phrase: next });
    };
    // Fire immediately so the first phrase change happens at turn
    // start (instead of 8s in). Without this, the user sees the
    // first-pool-entry fallback until the first interval boundary.
    tick();
    const id = setInterval(tick, 8_000);
    return () => clearInterval(id);
  }, [streamState, compactionInFlight, dispatch]);

  // Stable ``onExit`` reference so React.memo on InputPrompt can
  // shallow-compare props successfully. Inline ``() => app.exit()``
  // creates a new lambda each Composer render → memo bails →
  // InputPrompt re-renders for nothing.
  const handleExit = useCallback(() => app.exit(), [app]);

  const handleSubmit = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;

      // Branch order: only treat the input as a slash COMMAND when
      // it both starts with ``/`` AND parses against the registry.
      // ``parseSlashLine`` is the cheap pre-check (handles plain
      // ``/`` / blank-after-slash), then ``parseSlashCommand`` does
      // the real registry-aware resolution (alias→canonical, sub
      // detection). A leading slash whose root doesn't resolve
      // (e.g. user typed ``/notes meeting``) is still handled in the
      // slash branch — we echo the typed line and surface a
      // "unknown command" warning rather than silently sending it
      // to the agent as NL. That matches Python's semantics and
      // avoids surprising NL turns from a typo.
      const slashRaw = parseSlashLine(trimmed);
      if (slashRaw) {
        const parsed = parseSlashCommand(trimmed, registry);
        // Synthetic slash echo: pushes the typed slash line through
        // the same TURN_STARTED → TURN_DONE pair that NL turns use,
        // so it shows up in history with the user-prompt styling.
        // Wrapped in a closure because the call sites below need to
        // gate on whether the command will dispatch its own real turn:
        //   - Unknown / blocked / synchronous commands → echo here.
        //   - ``dispatchesOwnTurn`` commands (``/run``, ``/retry``,
        //     ``/inject``) → SKIP, because their handler chains through
        //     ``ctx.submitTurn(...)`` which itself fires TURN_STARTED
        //     with the unwrapped NL string. Echoing both produces two
        //     consecutive history entries for one keystroke
        //     (``/run inject CPU`` immediately followed by
        //     ``inject CPU``). Suppression keeps history a faithful
        //     transcript of one user message per submit.
        const echoSlashLine = () => {
          dispatch({ type: "TURN_STARTED", input: trimmed });
          dispatch({ type: "TURN_DONE" });
        };

        if (!parsed) {
          // Unknown root: still echo so the user can see what they
          // typed alongside the warning. Without this they'd be
          // staring at a bare "✗ unknown command" line.
          echoSlashLine();
          dispatch({
            type: "LOG_APPENDED",
            level: "warn",
            text: t("replay.unknown_command", { name: slashRaw.name }),
          });
          return;
        }

        // ``parsed.root`` is canonical (alias resolved); ``registry.get``
        // therefore returns the same command. Cached here once so we
        // don't double-lookup.
        const cmd = registry.get(parsed.root)!;
        const matchedSub = parsed.sub
          ? cmd.subcommands?.[parsed.sub]
          : undefined;

        // Stream-safe gate. Block destructive / mutating commands
        // mid-stream (any state other than ``idle``). Mirrors
        // Python's ``_STREAM_SAFE`` set in
        // ``tui/controllers/commands.py``: each specific
        // ``(root, sub)`` combo is independently classified.
        //
        // Per-tuple strict semantics:
        //   - When a sub matched, the SUB's ``streamSafe`` alone
        //     decides — a streamSafe parent cannot whitelist an
        //     unsafe sub by inheritance. This matters for future
        //     commands like ``/skills`` where bare ``/skills`` shows
        //     a list (safe) but ``/skills install`` mutates state
        //     (unsafe); without the strict per-sub gate, marking
        //     ``/skills`` streamSafe at the root would silently
        //     allow ``install`` mid-stream.
        //   - When no sub matched, the parent's ``streamSafe``
        //     decides as before.
        //
        // The handler is also free to do its own per-state gating
        // — ``/retry`` does this — but the gate stops obvious cases
        // at the edge. (In practice this gate rarely fires because
        // ``InputPrompt.enterLocked`` already blocks Enter during
        // streaming. Defense-in-depth for non-prompt code paths
        // that might one day dispatch through here.)
        // Read state once at command-invoke time; the handler ctx
        // gets the same snapshot. Equivalent to the old behaviour
        // (whole-tree subscription + .streamState read), but without
        // a live subscription that re-renders Composer on every
        // unrelated state change.
        const stateSnapshot = getAppState();
        if (stateSnapshot.streamState !== "idle") {
          const allowed = parsed.sub
            ? !!matchedSub?.streamSafe
            : !!cmd.streamSafe;
          if (!allowed) {
            // Blocked at the gate: echo + log so the user sees the
            // attempted command and the refusal reason side by side.
            // ``dispatchesOwnTurn`` doesn't apply here — the handler
            // never runs, so there's no real turn coming to dedupe
            // against.
            echoSlashLine();
            dispatch({
              type: "LOG_APPENDED",
              level: "warn",
              text: t("command.busy_block"),
            });
            return;
          }
        }

        // Echo unless the command will fire its own ``submitTurn``
        // and produce its own TURN_STARTED — see the closure comment
        // above. Decision uses the matched-sub's flag if a sub
        // resolved; otherwise the root's flag. Most commands don't
        // set the flag and fall through to echo as before.
        const ownTurn = matchedSub
          ? !!matchedSub.dispatchesOwnTurn
          : !!cmd.dispatchesOwnTurn;
        if (!ownTurn) {
          echoSlashLine();
        }

        const ctx: SlashCommandContext = {
          client,
          sessionId,
          state: stateSnapshot,
          registry,
          dispatch,
          exit: handleExit,
          beginReplay,
          beginManualCompact,
          submitTurn,
        };

        // Subcommand match → dispatch to the sub's handler. The sub
        // receives only the args AFTER the sub token (parsed.args),
        // mirroring Python's ``cmd.subcommands[sub].handler(args)``
        // contract. Bare-root handler runs otherwise.
        const handler = matchedSub ? matchedSub.handler : cmd.handler;

        handler(ctx, parsed.args).catch((err) => {
          const msg = err instanceof Error ? err.message : String(err);
          const nameForLog = parsed.sub
            ? `${parsed.root} ${parsed.sub}`
            : parsed.root;
          dispatch({
            type: "LOG_APPENDED",
            level: "warn",
            text: t("command.handler_failed", { name: nameForLog, msg }),
          });
        });
        return;
      }

      void submitTurn(trimmed);
    },
    // ``getAppState`` is referentially stable (Store identity for the
    // Provider lifetime), so its presence in deps doesn't churn the
    // callback. Replaces the prior ``state`` slot which used to
    // change on every reducer dispatch and rebuilt this callback
    // each time. ``handleExit`` is also useCallback'd → stable.
    //
    // ``app`` stays in deps because it's still referenced inside the
    // closure transitively (handleExit captures it). Keeping it here
    // makes the lint-friendly "all referenced bindings are deps"
    // contract visible even though removing it wouldn't actually
    // break anything.
    [
      submitTurn,
      app,
      client,
      sessionId,
      getAppState,
      dispatch,
      registry,
      handleExit,
    ],
  );

  return (
    <Box flexDirection="column" marginTop={1} ref={attachControlsRef}>
      {/* Order matters: LoadingIndicator first, then the sticky todo
          list, then InputPrompt. The user reads the strip "right
          above where I type"; thinking / replying chrome belongs
          above that strip so the dynamic spinner row doesn't push
          it away from the input. When ``activeStepper`` is null
          (chat-only turn, /command, idle) the strip slot is empty
          and LoadingIndicator falls naturally to the row directly
          above InputPrompt. */}
      {/* Phase 4 spinner mutex: while a memory compaction is in
          flight, MemoryCompactingIndicator owns the spinner slot
          (LoadingIndicator's hook nullifies its own visibility). The
          two never both render — keeps the "what's happening"
          signal singular.
          ManualCompactIndicator (client-driven, runs for the whole
          /compact lifetime) takes priority over MemoryCompactingIndicator
          (server-driven, only the LLM call window) so a /compact-
          initiated compaction shows ONE continuous spinner rather
          than the server-side one flashing in and out mid-operation. */}
      {manualCompactInFlight ? (
        <ManualCompactIndicator />
      ) : (
        compactionInFlight && <MemoryCompactingIndicator />
      )}
      {/* Sub-control wrappers — each strip lives in its own Box so
       *  the overflow probe (only active when BLADE_AI_DEBUG_OVERFLOW=1)
       *  can ``measureElement`` it independently. The wrapping Box is
       *  ``flexDirection="column"`` (the Ink default) so it does NOT
       *  add any layout cost — Yoga collapses single-child columns
       *  into the child's own dimensions. Production builds ignore
       *  the refs entirely; cost is just the callback invocation
       *  Ink would do anyway for any ref-bearing element. */}
      <Box flexDirection="column" ref={setProbeLoadingRef}>
        <LoadingIndicator />
      </Box>
      <Box flexDirection="column" ref={setProbeStepperRef}>
        {activeStepper && <PhaseStepperCard item={activeStepper} />}
      </Box>
      <Box flexDirection="column" ref={setProbeInputRef}>
        <InputPrompt
          disabled={awaitingConfirmation}
          enterLocked={busy && !awaitingConfirmation}
          registry={registry}
          onSubmit={handleSubmit}
          onExit={handleExit}
        />
      </Box>
      <Box flexDirection="column" ref={setProbeFooterRef}>
        <Footer />
      </Box>
    </Box>
  );
};
