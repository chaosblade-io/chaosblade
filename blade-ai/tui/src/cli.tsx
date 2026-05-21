/**
 * blade-ai TUI v2 — process entry.
 *
 * Resolution order:
 *   1. ``BLADE_AI_TUI=legacy`` → exec the legacy Python TUI and exit.
 *   2. ``BLADE_AI_SERVER=...`` → connect to the remote server, no spawn.
 *   3. otherwise → spawn ``python -m chaos_agent.server.app`` embedded.
 *
 * Boot UX:
 *   - cli.tsx renders Ink ~immediately after the bundle loads, with
 *     ``bootProgress`` pre-seeded so the user sees a spinner with
 *     "Starting blade-ai backend…" within ~400 ms of launching the
 *     command — instead of staring at a black terminal for ~2 s
 *     while the embedded Python server cold-imports langgraph /
 *     langchain / FastAPI / etc.
 *   - The actual handshake (spawn → /health → createSession →
 *     getSessionState) is driven inside React by ``BootRunner``,
 *     which dispatches ``BOOT_PROGRESS_SHOW`` for each phase, then
 *     ``SESSION_INITIALIZED`` + ``HISTORY_APPENDED`` to seat the
 *     header / welcome card into ``<Static>`` once the backend is
 *     ready. ``BootOrchestrator`` mounts at that point and takes
 *     over the spinner with its preflight + tasks phases.
 *   - cli.tsx still owns the ``ServerHandle`` (so signal handlers can
 *     shut down the spawned Python child) and the exit-time
 *     ``finalize()`` which prints the goodbye card + flushes session
 *     stats + stops the server. ``BootRunner`` writes the handle
 *     into closure vars via the ``onResolved`` callback.
 *
 * On signal / Ink exit we tear the spawned server down deterministically
 * (SIGTERM with 1.5s grace, then SIGKILL).
 */

import { spawnSync } from "node:child_process";
import process from "node:process";
import { render } from "ink";
import React from "react";
import { BootRunner } from "./components/boot/BootRunner.js";
import type { BladeClient } from "./api/client.js";
import type { ServerHandle } from "./api/server-process.js";
import { t } from "./i18n/index.js";
import { sessionStatsRef } from "./state/sessionStats.js";
import { StoreProvider } from "./state/store.js";
import { TerminalBgProvider } from "./theme/TerminalBgContext.js";
import { printGoodbye } from "./utils/printGoodbye.js";
import { installSynchronizedOutput } from "./utils/synchronizedOutput.js";
import { detectTerminalBg } from "./utils/terminalBg.js";
import { installTerminalRedrawOptimizer } from "./utils/terminalRedrawOptimizer.js";
import { PKG_VERSION } from "./utils/version.js";

async function main(): Promise<void> {
  // Legacy escape hatch — let users opt back into the Python TUI.
  if (process.env["BLADE_AI_TUI"] === "legacy") {
    const py = process.env["BLADE_AI_PYTHON"] ?? "python";
    const result = spawnSync(py, ["-m", "chaos_agent.cli.main"], {
      stdio: "inherit",
    });
    process.exit(result.status ?? 0);
  }

  // Refuse to run without a TTY on stdin. Ink's useInput / useApp
  // require raw-mode capable input; piping or redirecting stdin from
  // ``/dev/null`` triggers a misleading "Raw mode is not supported"
  // stack trace. Catch it early with a friendly message that points
  // the user at the right invocation.
  if (!process.stdin.isTTY) {
    fail(
      "stdin is not a TTY — blade-ai-tui needs an interactive terminal.\n" +
        "  Run `blade-ai-tui` directly in a terminal session.\n" +
        "  Background / pipe / redirected stdin is not supported.\n" +
        "  Set BLADE_AI_TUI=legacy to fall back to the Python TUI.",
    );
    return;
  }

  // First-run config gate is now handled inside BootRunner — after
  // the embedded Python server is up and /health responds, BootRunner
  // checks ``GET /api/v1/wizard/needs-setup`` and, if true, renders
  // the in-Ink ``WizardCard`` (talking to the server over HTTP) before
  // continuing to session creation. The old pre-Ink ``runConfigWizard``
  // helper (Python Rich subprocess) is kept for the standalone
  // ``blade-ai config-wizard`` CLI command — see ``utils/configGate.ts``.

  const debug = process.env["BLADE_AI_DEBUG"] === "1";

  // ──────────────────────────────────────────────────────────────────
  // Mutable closure vars for finalize() / signal handlers.
  // BootRunner writes here via onResolved once the handshake succeeds.
  // Until then they're null / "" and finalize() skips the steps that
  // require them (no session to flush, no server to shutdown).
  // ──────────────────────────────────────────────────────────────────
  let server: ServerHandle | null = null;
  let client: BladeClient | null = null;
  let sessionId = "";
  let bootError: string | null = null;

  // Capture timestamp BEFORE rendering so the doctor card's
  // ``captured_at HH:MM:SS`` reflects when the user launched the TUI,
  // not when the preflight call completed. On a slow cluster the two
  // differ by several seconds and would otherwise leave the user
  // thinking the clock skewed.
  const capturedAt = new Date().toISOString();

  // Terminal background detection — happens BEFORE the Ink render
  // call below so stdin isn't owned yet (OSC 11 probe needs raw IO).
  // Always resolves within ~100ms (the OSC 11 timeout); any user
  // keystrokes that arrived during the probe are unshifted back into
  // stdin so Ink doesn't lose them. See utils/terminalBg.ts for the
  // detection cascade (env override → COLORFGBG → OSC 11 → fallback).
  // The result drives <TerminalBgProvider> seeding below; consumers
  // currently include just <UserMessage> (bubble bg/fg) but the
  // Context shape supports adding more without further wiring.
  const terminalBg = await detectTerminalBg();

  // Two stdout monkey-patches that materially smooth out streaming
  // output. Install BEFORE Ink's render() so every byte Ink ever
  // writes goes through them. Restored at the END of cleanup() so
  // every write up to that point — including the goodbye-card
  // print — goes through the patches (they're transparent for
  // non-eraseLine text, just defensive bookkeeping).
  //
  //   1. Terminal redraw optimizer: folds Ink's per-line
  //      ``eraseLine + cursorUp`` storm into one bounded jump.
  //      Kills "scrollback bouncing" when the user mouse-wheels
  //      during streaming.
  //   2. Synchronized output (DEC mode 2026): tells iTerm2 /
  //      WezTerm / kitty to atomically commit each tick's worth
  //      of writes. Eliminates the blank-frame flash between
  //      eraseLines and the new lines arriving.
  //
  // Both no-op on a non-TTY stdout (--help piping, CI capture) or
  // a terminal not in the supported set; both honour escape-hatch
  // env vars (BLADE_AI_LEGACY_ERASE_LINES=1,
  // BLADE_AI_DISABLE_SYNCHRONIZED_OUTPUT=1).
  const restoreRedraw = process.stdout.isTTY
    ? installTerminalRedrawOptimizer(process.stdout)
    : () => {};
  const restoreSync = process.stdout.isTTY
    ? installSynchronizedOutput(process.stdout)
    : () => {};

  // Step instrumentation for ``finalize`` — gated on BLADE_AI_DEBUG=1.
  // Per-fetch and per-signal bounds live inside their respective
  // helpers; this helper only annotates timing for diagnostics.
  const step = async <T,>(label: string, p: Promise<T>): Promise<T> => {
    if (!debug) return p;
    const start = Date.now();
    process.stderr.write(`[blade-ai-tui] ${label}: starting\n`);
    try {
      const v = await p;
      process.stderr.write(
        `[blade-ai-tui] ${label}: done in ${Date.now() - start}ms\n`,
      );
      return v;
    } catch (e) {
      process.stderr.write(
        `[blade-ai-tui] ${label}: failed in ${Date.now() - start}ms (${formatError(e)})\n`,
      );
      throw e;
    }
  };

  const cleanup = async () => {
    // Skip the session flush if BootRunner never reached the
    // createSession step — there's nothing on disk to PATCH/DELETE.
    if (client && sessionId) {
      const stats = sessionStatsRef.current;
      const patchCall = stats
        ? client
            .patchSessionStats(sessionId, {
              message_count: stats.messageCount,
              injection_count: stats.injectionCount,
              injection_success: stats.injectionSuccess,
              injection_fail: stats.injectionFail,
              recovery_count: stats.recoveryCount,
            })
            .catch(() => undefined)
        : Promise.resolve();
      const deleteCall = client.deleteSession(sessionId).catch(() => undefined);
      await step(
        "session-flush",
        Promise.all([patchCall, deleteCall]).then(() => undefined),
      );
    }

    // Server shutdown — SIGTERM with 1.5s grace then SIGKILL, all
    // bounded inside shutdownChild. We let it run to completion so
    // uvicorn's lifespan handlers flush the server stderr log
    // cleanly to ~/.blade-ai/logs/tui-server-<pid>.log instead of
    // truncating mid-write.
    if (server) {
      await step("server-shutdown", server.shutdown().catch(() => undefined));
    }

    // Restore the original stdout.write so the goodbye-card writes
    // that follow this cleanup() go through Node directly. Order
    // matters: redraw optimizer wrapped first, sync output wrapped
    // second — restore in reverse so the unwrapping is symmetric.
    // Both are idempotent no-ops if their install bailed.
    try {
      restoreSync();
      restoreRedraw();
    } catch {
      // stdout closed mid-shutdown; the patch references die with
      // the process anyway.
    }
  };

  // Exit-path coordination. Three things can end the TUI: ``/exit``
  // slash command (Ink's app.exit), Esc-on-empty-prompt, and signals
  // (SIGINT / SIGTERM). All three converge on ``finalize`` so the
  // goodbye card prints once + cleanup runs once, regardless of which
  // path tripped first.
  let exiting = false;
  let pendingExitCode = 0;
  let inkInstance: ReturnType<typeof render> | null = null;

  const finalize = async (code: number): Promise<void> => {
    if (exiting) return;
    exiting = true;

    // Failsafe: no matter what happens in printGoodbye / cleanup
    // (fetch wedged on a half-closed socket, Ink unmount stuck,
    // server.shutdown hanging on SIGKILL race, …), guarantee the
    // process exits within 10s of the user asking it to.
    const FAILSAFE_MS = 10_000;
    const failsafe = setTimeout(() => {
      process.stderr.write(
        `\nblade-ai-tui: cleanup exceeded ${FAILSAFE_MS}ms, force-exiting.\n`,
      );
      process.exit(code);
    }, FAILSAFE_MS);
    failsafe.unref();

    // Goodbye first — but only if we have a state worth summarizing
    // (i.e. boot completed and the user actually used the TUI).
    // Showing the goodbye card after a boot failure or an
    // immediately-cancelled boot would confuse the user.
    if (sessionId) {
      await step("printGoodbye", printGoodbye(sessionStatsRef.current));
    }
    await cleanup();
    clearTimeout(failsafe);
    if (debug) {
      process.stderr.write(`[blade-ai-tui] finalize: done, exiting ${code}\n`);
    }
    process.exit(code);
  };

  const onSignal = (code: number): void => {
    pendingExitCode = code;
    if (inkInstance && !exiting) {
      // Trigger a clean React unmount; the main path's
      // ``await ink.waitUntilExit()`` below will resolve and the
      // post-await call to finalize() prints goodbye + cleans up with
      // the right exit code from pendingExitCode.
      try {
        inkInstance.unmount();
      } catch {
        // Already unmounted — fall through to direct finalize.
        void finalize(code);
      }
    } else {
      // Signal fired before render mounted (or after exiting started).
      void finalize(code);
    }
  };
  process.on("SIGINT", () => onSignal(130));
  process.on("SIGTERM", () => onSignal(143));

  // ──────────────────────────────────────────────────────────────────
  // Render Ink immediately. The store is pre-seeded with the boot
  // spinner text so the very first frame already shows activity to
  // the user — no useEffect tick required. BootRunner takes over from
  // there and dispatches further BOOT_PROGRESS_SHOW + the
  // SESSION_INITIALIZED / HISTORY_APPENDED that seats the header and
  // welcome card into <Static>.
  // ──────────────────────────────────────────────────────────────────
  inkInstance = render(
    <TerminalBgProvider initial={terminalBg}>
      <StoreProvider
        initial={{
          bootProgress: t("boot.progress.spawning"),
        }}
      >
        <BootRunner
          version={PKG_VERSION}
          bootCapturedAt={capturedAt}
          debug={debug}
          onResolved={(s, c, sid) => {
            server = s;
            client = c;
            sessionId = sid;
          }}
          onFailed={(msg) => {
            // Stash the message so the post-unmount block writes it
            // to stderr and exits 1 — we can't write to stderr here
            // because Ink owns the terminal until unmount.
            bootError = msg;
            try {
              inkInstance?.unmount();
            } catch {
              // already unmounted
            }
          }}
        />
      </StoreProvider>
    </TerminalBgProvider>,
    {
      // ``incrementalRendering`` is intentionally NOT enabled.
      // qwen-code (Ink v7) doesn't enable it either — they rely on
      // strict height-capping to keep the dynamic frame ≤
      // ``stdout.rows``, same strategy we use. Enabling incremental
      // would leave "ghost" frames when content shifts vertically
      // (e.g. a confirm card pushes the LoadingIndicator from row R
      // to row R+25 and the old row R copy isn't erased), which is
      // exactly the "two stacked ⠴ thinking lines with different
      // elapsed counters" the user reported.
      //
      // ``maxFps: 30`` matches Ink's default and qwen-code (which
      // also leaves it unset, defaulting to 30). Why not the previous
      // 4 fps:
      //
      //   - ``ink-spinner`` ticks internally at 12.5 fps ("dots"
      //     profile, 80ms per frame). At 4 fps stdout writes only
      //     surface every 3rd internal frame — the rotation reads
      //     as "skipping" rather than spinning. ≥ 13 fps is needed
      //     for the spinner to look smooth; 30 lines up with the
      //     animation cadence and matches what users expect from
      //     other Ink-based agent CLIs.
      //   - Input echo lag at 4 fps is up to 250ms per keystroke
      //     (worst case, when a state-changing render is queued).
      //     30 fps caps that at ~33ms — below the perception
      //     threshold for typing latency.
      //   - The selection-stability and flicker-tail concerns the
      //     old comment cited are now handled structurally: the
      //     reducer's ``flushLeadingStable`` peels stable items out
      //     of pending so the dynamic frame never grows past the
      //     viewport, and the streaming-event throttle in
      //     ``useStream`` (50ms / 20 fps for both token and thinking
      //     events) bounds how often reducer work fires regardless
      //     of paint rate.
      //
      // Static appends (history flushes) bypass maxFps so they
      // remain immediate; only the "redraw the dynamic frame"
      // path is throttled.
      maxFps: 30,
    },
  );

  await inkInstance.waitUntilExit();

  // Boot-time failure path: BootRunner threw before reaching
  // SESSION_INITIALIZED. Print the message and exit non-zero. We do
  // NOT run finalize() because there's nothing to clean up —
  // ``server`` may or may not be set; BootRunner's effect-cleanup
  // already shutdownChild'd if it spawned but never resolved.
  if (bootError) {
    fail(bootError);
    return;
  }

  // Natural exit (Ink unmounted on its own — /exit, Esc on empty
  // prompt, signal handler). ``finalize`` is idempotent via the
  // ``exiting`` gate; if a signal raced us here, it already kicked
  // off finalize and this call no-ops.
  await finalize(pendingExitCode);
}

function fail(msg: string): void {
  process.stderr.write(`\nblade-ai-tui: ${msg}\n`);
  process.exit(1);
}

function formatError(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

main().catch((err) => {
  fail(formatError(err));
});
