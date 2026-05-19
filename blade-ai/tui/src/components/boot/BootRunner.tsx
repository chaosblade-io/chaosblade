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

import { useEffect, useState } from "react";
import { App } from "../../App.js";
import {
  BladeClient,
  TUI_PROTOCOL_VERSION,
} from "../../api/client.js";
import {
  resolveServer,
  type ServerHandle,
} from "../../api/server-process.js";
import { t } from "../../i18n/index.js";
import { useAppDispatch, useAppSelector } from "../../state/store.js";
import type { HistoryItem } from "../../state/types.js";

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

  useEffect(() => {
    let cancelled = false;
    let spawnedServer: ServerHandle | null = null;
    // ``resolvedToCli`` tracks ownership of ``spawnedServer``. Once we
    // call ``onResolved`` cli.tsx owns the handle and is responsible
    // for shutting it down via ``finalize`` → ``cleanup``. Until then,
    // BootRunner's effect-cleanup is the only thing that can stop the
    // child process if Ink unmounts mid-boot. We track this via a
    // local ``let`` (not a useState/useRef) because the cleanup
    // closure has to see the *current* value at unmount time, which
    // closed-over let-vars give us — useState would only ever expose
    // the value at effect-mount (empty deps), i.e. always false.
    let resolvedToCli = false;

    const run = async () => {
      try {
        // -- Phase 1: spawn server --------------------------------
        // Progress text is already showing from cli.tsx's pre-seed,
        // so skip the redundant SHOW dispatch here.
        spawnedServer = await resolveServer();
        if (cancelled) {
          // User Ctrl+C'd while we were spawning. Best-effort kill
          // so the embedded Python process doesn't outlive the TUI.
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
        const ok = await waitForHealth(c, 10_000);
        if (cancelled) {
          spawnedServer.shutdown().catch(() => undefined);
          return;
        }
        if (!ok) {
          spawnedServer.shutdown().catch(() => undefined);
          throw new Error(
            `backend at ${spawnedServer.url} did not pass /health within 10s`,
          );
        }

        // -- Phase 3: createSession + state -----------------------
        dispatch({
          type: "BOOT_PROGRESS_SHOW",
          text: t("boot.progress.session"),
        });
        const sid = await c.createSession({});
        if (cancelled) return;

        let sessionState: Record<string, unknown> = {};
        try {
          sessionState = await c.getSessionState(sid);
        } catch {
          // Header falls back to defaults; non-fatal.
        }
        if (cancelled) return;

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
        // shut it down on exit. Set ``resolvedToCli`` BEFORE the
        // setStates so a synchronous unmount-during-render race
        // wouldn't double-shutdown via the effect cleanup.
        resolvedToCli = true;
        // Notify cli.tsx so it can route SIGINT cleanup, etc.
        onResolved(spawnedServer, c, sid);
        setClient(c);
        setSessionId(sid);
        setServerUrl(spawnedServer.url);
      } catch (err) {
        if (cancelled) return;
        dispatch({ type: "BOOT_PROGRESS_HIDE" });
        onFailed(formatError(err));
      }
    };

    void run();

    return () => {
      cancelled = true;
      // If we never reached "resolved" but did manage to spawn the
      // server, kill it so it doesn't outlive the parent. If we
      // already called onResolved, cli.tsx owns shutdown — don't
      // double-fire.
      if (spawnedServer && !resolvedToCli) {
        spawnedServer.shutdown().catch(() => undefined);
      }
    };
    // Deliberately empty deps: this effect runs exactly once per mount.
    // ``permissionMode`` etc. are read from the closure but locking
    // them at mount time matches the original cli.tsx behavior (the
    // welcome card's ``permissionMode`` was captured at startup, not
    // re-read on every state change).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
