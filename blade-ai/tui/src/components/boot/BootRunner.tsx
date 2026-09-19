/**
 * Drives backend startup as a side effect *inside* React so the user
 * sees an immediate boot spinner instead of staring at a black
 * terminal for ~2 s while the Python server imports langgraph,
 * langchain, FastAPI, and the rest.
 *
 * Sequence:
 *
 *   1. Mount → ``state.bootProgress`` already pre-seeded by ``cli.tsx``
 *      with ``boot.progress.spawning`` so the spinner shows on the
 *      very first paint (no useEffect tick required).
 *   2. ``resolveServer()`` (spawn + ``BLADE_AI_READY``) → progress
 *      flips to ``boot.progress.health``.
 *   3. ``waitForHealth()`` polls ``/api/v1/health`` until 200 → progress
 *      flips to ``boot.progress.session``.
 *   4. ``createSession`` + ``getSessionState`` → dispatch
 *      ``SESSION_INITIALIZED`` (Header now lands in <Static>),
 *      ``HISTORY_APPENDED`` for the welcome card, optionally another
 *      ``HISTORY_APPENDED`` for the protocol-mismatch warning, then
 *      ``setReady`` so <App> remounts BootOrchestrator (which takes
 *      over the spinner with its own preflight + tasks phases).
 *
 * Why a component rather than top-level awaits in cli.tsx:
 *   - Lets us render *before* the handshake starts. Total wall-clock
 *     time to the welcome card is the same; perceived wait drops from
 *     ~2 s of black terminal to ~0.4 s of black + spinner showing
 *     activity.
 *   - The cleanup return value cancels in-flight setup if the user
 *     Ctrl+C's mid-boot — pending dispatches no-op against a
 *     torn-down store.
 *   - cli.tsx still owns the ``ServerHandle`` for shutdown, but it
 *     gets it via the ``onResolved`` callback. Until that fires,
 *     ``finalize()`` skips server-shutdown (nothing was spawned yet).
 */

import { useEffect, useRef, useState } from "react";
import { App } from "../../App.js";
import {
  BladeClient,
  TUI_PROTOCOL_VERSION,
} from "@blade-ai/core";
import {
  resolveServer,
  type ServerHandle,
} from "../../api/server-process.js";
import { resolveServerToken } from "../../api/auth.js";
import { WizardClient } from "../../api/wizard.js";
import { t } from "@blade-ai/core";
import { useAppDispatch, useAppSelector } from "@blade-ai/core";
import type { HistoryItem } from "@blade-ai/core";
import { runSessionResume } from "@blade-ai/core";
import type { SessionResumeOutcome } from "@blade-ai/core";
import { WizardCard } from "../wizard/WizardCard.js";
import { fetchDoctorCard, fetchPendingCard } from "./bootCards.js";

export interface BootRunnerProps {
  version: string;
  /** ISO timestamp captured before BootRunner mounted; threaded
   *  through to BootOrchestrator's doctor card so its
   *  ``captured_at`` reflects when boot began, not when preflight
   *  returned. */
  bootCapturedAt: string;
  /** Stream debug noise to stderr when ``BLADE_AI_DEBUG=1`` (the
   *  ``onProtocolError`` sink on BladeClient). */
  debug: boolean;
  /** Take over a previous session instead of creating a fresh one —
   *  set by ``blade-ai resume -i <sid>`` (the Python CLI execvp's
   *  this process with ``--resume <sid>``). The takeover sequence
   *  lives in core's ``runSessionResume`` (the exact code path the
   *  ``/resume <sid>`` slash command uses), so the confirm-gate
   *  flush and the per-segment event fold behave identically. */
  resumeSid?: string;
  /** Fired exactly once when the handshake succeeds. cli.tsx stashes
   *  ``server`` so its exit-time ``cleanup()`` can call
   *  ``server.shutdown()``. */
  onResolved: (
    server: ServerHandle,
    client: BladeClient,
    sessionId: string,
  ) => void;
  /** Fired if any phase fails. cli.tsx writes a friendly message to
   *  stderr and exits 1; we don't try to recover here because the
   *  failures are mostly "Python is broken / port already bound" —
   *  the user has to fix those externally. */
  onFailed: (message: string) => void;
}

/** Promise-based wait for /health 200, identical to the helper that
 *  used to live in cli.tsx. Keeps polling at 100 ms. */
async function waitForHealth(
  client: BladeClient,
  timeoutMs: number,
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await client.health()) return true;
    await new Promise<void>((resolve) => setTimeout(resolve, 100));
  }
  return false;
}

function asString(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function formatError(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/**
 * BootRunner phase state machine.
 *
 *   spawning      → spawn server + wait /health + check needs-setup
 *     ├─ needs-setup=true  → wizard
 *     └─ needs-setup=false → sessioning
 *   wizard        → user fills config in WizardCard
 *     ├─ saved      → sessioning
 *     └─ cancelled  → onFailed (BootRunner cleanup kills server)
 *   sessioning    → createSession + welcome card + onResolved → done
 *   done          → <App> takes over (existing behaviour)
 *
 * Server spawn is irreversible from the wizard's perspective — once
 * we've started the Python child we keep it (its lifetime is bound to
 * BootRunner's effect-cleanup until ``onResolved`` transfers ownership
 * to cli.tsx).
 */
type BootPhase = "spawning" | "wizard" | "sessioning" | "done";

export const BootRunner: React.FC<BootRunnerProps> = ({
  version,
  bootCapturedAt,
  debug,
  resumeSid,
  onResolved,
  onFailed,
}) => {
  const dispatch = useAppDispatch();
  const permissionMode = useAppSelector(
    (s) => s.config.permissionMode,
  );
  // ``client`` and ``sessionId`` flow through to <App> so once they
  // become non-null/non-empty, BootOrchestrator + Composer mount.
  const [client, setClient] = useState<BladeClient | null>(null);
  const [sessionId, setSessionId] = useState<string>("");
  const [serverUrl, setServerUrl] = useState<string>("");
  const [phase, setPhase] = useState<BootPhase>("spawning");
  // Server + client captured during the spawning phase, reused by the
  // sessioning phase. Refs (not state) because the sessioning effect
  // shouldn't re-run when these populate — we trigger it via the phase
  // transition explicitly.
  const spawnedRef = useRef<ServerHandle | null>(null);
  const clientRef = useRef<BladeClient | null>(null);
  // Track ownership transfer so cleanup doesn't double-kill the
  // server after cli.tsx took it over.
  const resolvedToCliRef = useRef(false);
  const cancelledRef = useRef(false);
  // Defensive: guarantee the sessioning side effect (createSession +
  // welcome card) runs exactly once even if Effect 2 re-fires due
  // to a stable-but-changing dep. React batches setState calls in
  // an async block (R18+) so the phase→done flip happens in the
  // same render as setClient/etc — but a future code change might
  // split them; the ref keeps us safe regardless.
  const sessioningStartedRef = useRef(false);

  // -- Effect 1: spawning → health → needs-setup check ───────────────
  useEffect(() => {
    cancelledRef.current = false;

    const run = async () => {
      try {
        // -- Phase 1: spawn server --------------------------------
        const spawnedServer = await resolveServer();
        spawnedRef.current = spawnedServer;
        if (cancelledRef.current) {
          spawnedServer.shutdown().catch(() => undefined);
          return;
        }

        // -- Phase 2: health --------------------------------------
        dispatch({
          type: "BOOT_PROGRESS_SHOW",
          text: t("boot.progress.health"),
        });
        const c = new BladeClient(spawnedServer.url, {
          getAuthToken: resolveServerToken,
          onProtocolError: debug
            ? (frame, e) => {
                process.stderr.write(
                  `[blade-ai-tui] protocol error: ${formatError(e)} :: ${frame.slice(0, 200)}\n`,
                );
              }
            : undefined,
        });
        clientRef.current = c;
        const ok = await waitForHealth(c, 10_000);
        if (cancelledRef.current) {
          spawnedServer.shutdown().catch(() => undefined);
          return;
        }
        if (!ok) {
          spawnedServer.shutdown().catch(() => undefined);
          throw new Error(
            `backend at ${spawnedServer.url} did not pass /health within 10s`,
          );
        }

        // -- Phase 2.5: needs-setup gate --------------------------
        // Ask the server whether the wizard should run. Server-side
        // check keeps the gating rules (which keys are essential)
        // out of the TS layer. Network failures fail-open (server
        // returns ``{needsSetup:false, configError:null}`` on transport
        // error), matching the legacy configGate behaviour of "fail
        // open and let the user reach the TUI even when validators
        // can't run".
        //
        // ``configError`` (non-null) means config.json exists but is
        // syntactically broken (e.g. unquoted JSON). The server
        // refuses to enter the wizard in that case (would clobber the
        // user's file) — we abort boot and surface the message via
        // the same ``onFailed`` path used for /health timeout etc.
        const wizardClient = new WizardClient(spawnedServer.url);
        const { needsSetup, configError } =
          await wizardClient.needsWizardSetup();
        if (cancelledRef.current) {
          spawnedServer.shutdown().catch(() => undefined);
          return;
        }
        if (configError) {
          spawnedServer.shutdown().catch(() => undefined);
          dispatch({ type: "BOOT_PROGRESS_HIDE" });
          onFailed(configError);
          return;
        }
        if (needsSetup) {
          dispatch({ type: "BOOT_PROGRESS_HIDE" });
          setServerUrl(spawnedServer.url);
          setPhase("wizard");
          return;
        }

        // No wizard needed → straight to sessioning.
        setPhase("sessioning");
      } catch (err) {
        if (cancelledRef.current) return;
        dispatch({ type: "BOOT_PROGRESS_HIDE" });
        onFailed(formatError(err));
      }
    };

    void run();

    return () => {
      cancelledRef.current = true;
      // If we never reached the "resolved-to-cli" handoff but did
      // manage to spawn the server, kill it so it doesn't outlive
      // the parent. Once cli.tsx owns the handle (after onResolved),
      // it's responsible for shutdown.
      const s = spawnedRef.current;
      if (s && !resolvedToCliRef.current) {
        s.shutdown().catch(() => undefined);
      }
    };
    // Deliberately empty deps: this effect runs exactly once per mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // -- Effect 2: sessioning → createSession + welcome card ──────────
  useEffect(() => {
    if (phase !== "sessioning") return;
    // Idempotency guard — see comment on ``sessioningStartedRef``.
    if (sessioningStartedRef.current) return;
    sessioningStartedRef.current = true;
    const spawnedServer = spawnedRef.current;
    const c = clientRef.current;
    if (!spawnedServer || !c) {
      onFailed("internal error: server handle missing in sessioning phase");
      return;
    }

    const run = async () => {
      try {
        // -- Phase 3a: resume branch (``blade-ai resume -i <sid>``) --
        // skip createSession entirely and take over the previous
        // session through core's runSessionResume: the same takeover
        // sequence the ``/resume <sid>`` slash command uses (fold +
        // confirm-gate flush + SESSION_INITIALIZED re-bind), so the
        // two entry points can never drift apart.
        if (resumeSid) {
          dispatch({
            type: "BOOT_PROGRESS_SHOW",
            text: t("boot.progress.resuming"),
          });

          // Best-effort pre-read for the permission-mode seed and the
          // header fallbacks; runSessionResume re-reads the state
          // itself (its own Step 3 is also best-effort).
          let resumeState: Record<string, unknown> = {};
          try {
            resumeState = await c.getSessionState(resumeSid);
          } catch {
            // Non-fatal — defaults below apply.
          }
          if (cancelledRef.current) return;

          const bootMode =
            resumeState["confirmation_required"] === false
              ? "auto"
              : "confirm";
          if (bootMode !== permissionMode) {
            dispatch({ type: "MODE_TOGGLED", mode: bootMode });
          }

          // -- Phase 3a.5 (resume): lay the boot cards BEFORE the
          // replay. runSessionResume's HISTORY_CLEARED carries
          // ``preserveBootCards``, so these survive at the head of the
          // rebuilt history and the resumed boot shows the SAME card
          // order as a fresh one (welcome → doctor → pending →
          // replayed turns) instead of cards appended after N
          // replayed events. The <Static> gate (session.id) is still
          // CLOSED here — nothing burn-ins until the replay's
          // SESSION_INITIALIZED opens it, so the whole stack renders
          // exactly once, in history order.
          const welcomeCard: HistoryItem = {
            kind: "welcome_card",
            id: "boot-welcome",
            modelName: asString(resumeState["model_name"]),
            permissionMode: bootMode,
            kubeconfig: asString(resumeState["kubeconfig"]),
            namespace: asString(resumeState["namespace"]) || "default",
            version,
          };
          dispatch({ type: "HISTORY_APPENDED", item: welcomeCard });

          dispatch({
            type: "BOOT_PROGRESS_SHOW",
            text: t("boot.progress.preflight"),
          });
          const { item: doctorItem, contextMax } = await fetchDoctorCard(
            c,
            bootCapturedAt,
          );
          if (cancelledRef.current) return;
          dispatch({ type: "HISTORY_APPENDED", item: doctorItem });
          // Seed the footer's context indicator (same as the fresh
          // path — BootOrchestrator is skipped on resume, so THIS is
          // the only place the seed happens).
          if (contextMax !== null) {
            dispatch({
              type: "CONTEXT_SIZE_RECEIVED",
              currentTokens: 0,
              triggerTokens: 0,
              maxTokens: contextMax,
              messagesCount: 0,
            });
          }

          dispatch({
            type: "BOOT_PROGRESS_SHOW",
            text: t("boot.progress.tasks"),
          });
          const pendingItem = await fetchPendingCard(c);
          if (cancelledRef.current) return;
          dispatch({ type: "HISTORY_APPENDED", item: pendingItem });

          let outcome: SessionResumeOutcome;
          try {
            outcome = await runSessionResume(
              {
                client: c,
                dispatch,
                pushLog: (text, level) =>
                  dispatch({ type: "LOG_APPENDED", text, level }),
                // No clearScreen: nothing has rendered yet — only the
                // boot spinner, which App's first paint replaces.
                // preserveBootCards: the cards dispatched above must
                // survive the replay's history clear.
                preserveBootCards: true,
                fallbackHeader: {
                  cluster: asString(resumeState["cluster"]),
                  namespace:
                    asString(resumeState["namespace"]) || "default",
                  modelName: asString(resumeState["model_name"]),
                },
              },
              resumeSid,
            );
          } catch (err) {
            throw new Error(
              t("resume.failed", {
                sid: resumeSid,
                err: formatError(err),
              }),
            );
          }
          if (outcome === "no_events") {
            // The user named THIS sid on the command line — silently
            // falling back to a fresh session would be worse than a
            // loud exit that points at the listing command.
            throw new Error(t("resume.no_events", { sid: resumeSid }));
          }
          if (cancelledRef.current) return;

          // Protocol-mismatch warning — same defensive check as the
          // fresh-session path below.
          const resumeProto = c.serverProtocolVersion;
          if (resumeProto && resumeProto !== TUI_PROTOCOL_VERSION) {
            dispatch({
              type: "HISTORY_APPENDED",
              item: {
                kind: "log",
                id: "log-bootwarn",
                level: "warn",
                text: t("protocol.mismatch", {
                  tui: TUI_PROTOCOL_VERSION,
                  server: resumeProto,
                }),
              },
            });
          }

          // -- Phase 5 (resume): hand control to App -----------------
          resolvedToCliRef.current = true;
          onResolved(spawnedServer, c, resumeSid);
          setClient(c);
          setSessionId(resumeSid);
          setServerUrl(spawnedServer.url);
          setPhase("done");
          return;
        }

        // -- Phase 3: createSession + state -----------------------
        dispatch({
          type: "BOOT_PROGRESS_SHOW",
          text: t("boot.progress.session"),
        });
        const sid = await c.createSession({});
        if (cancelledRef.current) return;

        let sessionState: Record<string, unknown> = {};
        try {
          sessionState = await c.getSessionState(sid);
        } catch {
          // Header falls back to defaults; non-fatal.
        }
        if (cancelledRef.current) return;

        // Seed the runtime permission mode from the persisted
        // ``confirmation_required`` config (false → auto, true → confirm) so
        // the welcome card + first turn honour config.json instead of always
        // booting into ``confirm``. Shift+Tab / /permission still toggle it.
        // Undefined (older server) falls back to ``confirm`` — safety-first.
        const bootMode =
          sessionState["confirmation_required"] === false ? "auto" : "confirm";
        if (bootMode !== permissionMode) {
          dispatch({ type: "MODE_TOGGLED", mode: bootMode });
        }

        // -- Phase 4: dispatch session + welcome card -------------
        const namespace = asString(sessionState["namespace"]) || "default";
        dispatch({
          type: "SESSION_INITIALIZED",
          session: {
            id: sid,
            cluster: asString(sessionState["cluster"]),
            namespace,
            modelName: asString(sessionState["model_name"]),
          },
        });

        const welcomeCard: HistoryItem = {
          kind: "welcome_card",
          id: "boot-welcome",
          modelName: asString(sessionState["model_name"]),
          permissionMode: bootMode,
          kubeconfig: asString(sessionState["kubeconfig"]),
          namespace,
          version,
        };
        dispatch({ type: "HISTORY_APPENDED", item: welcomeCard });

        const serverProto = c.serverProtocolVersion;
        if (serverProto && serverProto !== TUI_PROTOCOL_VERSION) {
          dispatch({
            type: "HISTORY_APPENDED",
            item: {
              kind: "log",
              id: "log-bootwarn",
              level: "warn",
              text: t("protocol.mismatch", {
                tui: TUI_PROTOCOL_VERSION,
                server: serverProto,
              }),
            },
          });
        }

        // Don't HIDE the boot spinner here — BootOrchestrator picks
        // up immediately once it mounts (right below) and re-uses
        // the same row with its own ``boot.progress.preflight``
        // text. Letting it overwrite avoids a one-frame flicker
        // where the spinner briefly disappears.

        // -- Phase 5: hand control to App --------------------------
        // setState triggers re-render → <App> sees client/sessionId
        // → BootOrchestrator + Composer mount. ``onResolved`` then
        // transfers the ServerHandle to cli.tsx so finalize() can
        // shut it down on exit. Set the ref flag BEFORE the
        // setStates so a synchronous unmount-during-render race
        // wouldn't double-shutdown via the effect cleanup.
        resolvedToCliRef.current = true;
        // Notify cli.tsx so it can route SIGINT cleanup, etc.
        onResolved(spawnedServer, c, sid);
        setClient(c);
        setSessionId(sid);
        setServerUrl(spawnedServer.url);
        setPhase("done");
      } catch (err) {
        if (cancelledRef.current) return;
        dispatch({ type: "BOOT_PROGRESS_HIDE" });
        onFailed(formatError(err));
      }
    };

    void run();
    // Cleanup for this effect — no-op; spawning effect's cleanup
    // owns the server-shutdown logic until ``onResolved`` flips
    // ``resolvedToCliRef``.
  }, [phase, onResolved, onFailed, dispatch, debug, permissionMode, version, resumeSid, bootCapturedAt]);

  // ── Render ────────────────────────────────────────────────────────

  // Wizard phase takes over the screen — App's normal boot spinner is
  // hidden (BOOT_PROGRESS_HIDE fired above) so the WizardCard owns the
  // visual space. After the user saves we flip back to sessioning and
  // <App> picks up where it left off.
  if (phase === "wizard") {
    return (
      <WizardCard
        serverUrl={serverUrl}
        onExit={(saved) => {
          if (saved) {
            setPhase("sessioning");
          } else {
            // User cancelled the wizard. Treat as a clean exit;
            // BootRunner's effect-cleanup will shut down the server
            // when Ink unmounts.
            onFailed(t("wizard.cancel_message"));
          }
        }}
      />
    );
  }

  return (
    <App
      client={client}
      sessionId={sessionId}
      serverUrl={serverUrl}
      version={version}
      bootCapturedAt={bootCapturedAt}
      skipOrchestrator={Boolean(resumeSid)}
    />
  );
};
