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
} from "../../api/client.js";
import {
  resolveServer,
  type ServerHandle,
} from "../../api/server-process.js";
import { WizardClient } from "../../api/wizard.js";
import { t } from "../../i18n/index.js";
import { useAppDispatch, useAppSelector } from "../../state/store.js";
import type { HistoryItem } from "../../state/types.js";
import { WizardCard } from "../wizard/WizardCard.js";

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
        // returns false on transport error), matching the legacy
        // configGate behaviour of "fail open and let the user reach
        // the TUI even when validators can't run".
        const wizardClient = new WizardClient(spawnedServer.url);
        const needsSetup = await wizardClient.needsWizardSetup();
        if (cancelledRef.current) {
          spawnedServer.shutdown().catch(() => undefined);
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
          permissionMode,
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
  }, [phase, onResolved, onFailed, dispatch, debug, permissionMode, version]);

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
    />
  );
};
