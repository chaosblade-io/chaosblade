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
      // ``incrementalRendering: true`` — line-level diff in
      // ``log-update.js``. Ink writes each rendered line, compares
      // it to the same row of the previous frame, and **skips
      // unchanged lines** (only ``cursorNextLine`` advances past
      // them) instead of rewriting the whole dynamic frame. Source
      // (node_modules/ink/build/log-update.js:185-205):
      //
      //     // We do not write lines if the contents are the same.
      //     // This prevents flickering during renders.
      //     if (nextLines[i] === previousLines[i]) {
      //       buffer.push(ansiEscapes.cursorNextLine);
      //       continue;
      //     }
      //
      // **Why this matters on Apple Terminal / non-DEC-2026
      // terminals**: standard mode writes
      // ``eraseLines(N) + N rows of new content`` on every frame. The
      // terminal first erases all N rows (visibly blanking them),
      // then fills back. A user mid-selection sees their selection
      // wiped on each spinner tick because the cells transit
      // through "blank". With incremental mode the spinner row is
      // the only one rewritten — the other 14 rows of the dynamic
      // frame keep their selection state. Same fix Ink itself
      // documents for "flickering during renders".
      //
      // **Why we previously avoided it**: comments cited a
      // "ghost frame on vertical shift" hazard. That was Ink
      // issue #909, fixed in #910 (https://github.com/vadimdemedes/
      // ink/pull/910), shipped in v6.0.x. We're on v7.0.3 — the
      // incremental code now correctly emits ``eraseLines(prev -
      // visible)`` + ``cursorUp`` when the row count shrinks, and
      // ``\x1b[J`` (eraseEndLine) per row per write to keep
      // overflow bytes from dangling. Verified by reading the
      // source.
      //
      // **Escape hatch**: ``BLADE_AI_LEGACY_RENDERING=1`` falls
      // back to standard mode for users on terminals that
      // misbehave with the incremental cursor sequences. Should
      // not be needed on iTerm2 / WezTerm / kitty / Apple Terminal
      // / Windows Terminal / xterm — all of which implement the
      // CSI cursor commands incremental mode relies on. If a
      // future terminal proves problematic, escape via env var
      // instead of ripping out the optimization.
      //
      // ``maxFps: 30`` — see prior history; matches Ink default
      // and qwen-code. Caps spinner-driven repaints at the same
      // rate as ink-spinner's 12.5 fps animation cadence. With
      // incremental mode, hitting the cap is far cheaper because
      // unchanged lines don't get written at all.
      incrementalRendering:
        process.env["BLADE_AI_LEGACY_RENDERING"] !== "1",
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
