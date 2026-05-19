/**
 * Embedded Python server lifecycle.
 *
 * Two modes:
 *
 *   - **remote**: when ``BLADE_AI_SERVER`` is set, we just point the
 *     client at it and make ``shutdown`` a no-op. Used for `npm run dev`
 *     against an already-running ``blade-ai-server``.
 *
 *   - **embedded**: spawn ``python -m chaos_agent.server.app --port 0
 *     --ready-stdout``, parse the ``BLADE_AI_READY port=N`` line we
 *     printed from app.py, and connect to ``http://127.0.0.1:N``.
 *     Wired to SIGTERM → SIGKILL on shutdown so a hung server can't
 *     orphan past the TUI.
 *
 * Streams:
 *   - stdout: piped, scanned for the ready line, then drained.
 *   - stderr: piped to ``~/.blade-ai/logs/tui-server-<pid>.log``. We
 *     can't ``inherit`` because Ink owns the parent terminal — any
 *     traceback written to fd 2 would corrupt the active render.
 */

import { type ChildProcess, spawn } from "node:child_process";
import { createWriteStream, mkdirSync, type WriteStream } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";

export interface ServerHandle {
  /** Base URL the client should hit, e.g. http://127.0.0.1:54123 */
  url: string;
  /** True when this handle is just a remote pointer (nothing to shutdown). */
  remote: boolean;
  /** Tear down the embedded server. No-op for remote handles. */
  shutdown: () => Promise<void>;
}

const READY_PREFIX = "BLADE_AI_READY port=";
const SPAWN_TIMEOUT_MS = 20_000;

/**
 * Resolve the server handle based on environment.
 */
export async function resolveServer(): Promise<ServerHandle> {
  const remote = process.env["BLADE_AI_SERVER"];
  if (remote) {
    return {
      url: remote.replace(/\/+$/, ""),
      remote: true,
      shutdown: async () => {
        // intentionally a no-op
      },
    };
  }
  return startEmbeddedServer();
}

export async function startEmbeddedServer(): Promise<ServerHandle> {
  const py = process.env["BLADE_AI_PYTHON"] ?? "python";
  // Force --host 127.0.0.1 for two reasons:
  //   1. The embedded server is local-only by design — binding 0.0.0.0
  //      would expose the agent to the LAN, which is a real risk for
  //      a tool that can run kubectl / chaos-blade.
  //   2. On macOS, the run_server() port-discovery dance binds + closes
  //      a socket on 0.0.0.0 then asks uvicorn to re-bind the same
  //      port. That occasionally fails silently (uvicorn ends up on a
  //      different port). 127.0.0.1 sidesteps the whole issue.
  const args = [
    "-m",
    "chaos_agent.server.app",
    "--host",
    "127.0.0.1",
    "--port",
    "0",
    "--ready-stdout",
  ];

  // Pipe stderr into a per-pid log file so server tracebacks don't
  // tear up Ink's render. Best-effort: any FS error falls back to
  // discarding stderr entirely (the user can still re-run with
  // ``BLADE_AI_SERVER=...`` against a server they started themselves).
  const stderrLog = openStderrLog();

  const child = spawn(py, args, {
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
  });

  if (child.stderr) {
    if (stderrLog) {
      child.stderr.pipe(stderrLog);
    } else {
      child.stderr.resume();
      child.stderr.on("data", () => undefined);
    }
  }

  const port = await new Promise<number>((resolve, reject) => {
    if (!child.stdout) {
      reject(new Error("server child has no stdout"));
      return;
    }

    const rl = createInterface({ input: child.stdout });
    const timer = setTimeout(() => {
      rl.close();
      reject(
        new Error(
          `blade-ai server did not signal ready within ${SPAWN_TIMEOUT_MS}ms`,
        ),
      );
    }, SPAWN_TIMEOUT_MS);

    rl.on("line", (line: string) => {
      const idx = line.indexOf(READY_PREFIX);
      if (idx >= 0) {
        const portStr = line.slice(idx + READY_PREFIX.length).trim();
        const parsed = Number(portStr);
        if (Number.isFinite(parsed) && parsed > 0) {
          clearTimeout(timer);
          rl.close();
          resolve(parsed);
        }
      }
    });

    child.once("exit", (code, signal) => {
      clearTimeout(timer);
      reject(
        new Error(
          `blade-ai server exited before ready (code=${code ?? "?"}, signal=${signal ?? "?"})`,
        ),
      );
    });

    child.once("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
  });

  // After ready, drain stdout so the pipe buffer doesn't fill and block
  // uvicorn. We don't currently surface non-ready stdout lines (uvicorn
  // sends most output to stderr anyway).
  if (child.stdout) {
    child.stdout.resume();
    child.stdout.on("data", () => undefined);
  }

  return {
    url: `http://127.0.0.1:${port}`,
    remote: false,
    shutdown: () => shutdownChild(child, stderrLog),
  };
}

function openStderrLog(): WriteStream | null {
  try {
    const dir = join(homedir(), ".blade-ai", "logs");
    mkdirSync(dir, { recursive: true });
    const path = join(dir, `tui-server-${process.pid}.log`);
    const stream = createWriteStream(path, { flags: "a" });
    stream.write(`\n=== TUI server start @ ${new Date().toISOString()} (parent pid=${process.pid}) ===\n`);
    return stream;
  } catch {
    return null;
  }
}

function shutdownChild(
  child: ChildProcess,
  stderrLog: WriteStream | null,
): Promise<void> {
  const debug = process.env["BLADE_AI_DEBUG"] === "1";
  const t0 = Date.now();
  const log = (msg: string): void => {
    if (debug) {
      process.stderr.write(
        `[blade-ai-tui] shutdown +${Date.now() - t0}ms ${msg}\n`,
      );
    }
  };

  const closeLog = () => {
    if (stderrLog) {
      try {
        stderrLog.end();
      } catch {
        // ignore
      }
    }
  };
  if (child.exitCode !== null || child.signalCode !== null) {
    log("child already exited, skipping signal");
    closeLog();
    return Promise.resolve();
  }
  return new Promise<void>((resolve) => {
    let settled = false;
    let sigtermTimer: ReturnType<typeof setTimeout> | null = null;
    let sigkillTimer: ReturnType<typeof setTimeout> | null = null;
    const finish = (reason: string) => {
      if (settled) return;
      settled = true;
      if (sigtermTimer) clearTimeout(sigtermTimer);
      if (sigkillTimer) clearTimeout(sigkillTimer);
      log(`child reaped (${reason})`);
      closeLog();
      resolve();
    };
    child.once("exit", () => finish("exit-event"));

    // SIGTERM first. uvicorn handles SIGTERM cleanly: it calls each
    // ``lifespan.shutdown`` handler, drains in-flight requests, closes
    // the socket, then exits. We give it 1500ms of grace because:
    //
    //   - In practice uvicorn finishes its shutdown sequence in
    //     ~50–150ms on a quiet server (no in-flight turns).
    //   - The TUI's exit path has already finished its DELETE on
    //     /sessions/:id by the time we get here (see ``cleanup`` in
    //     cli.tsx), so there are no pending requests to drain.
    //   - 1500ms leaves ample headroom for slower systems while
    //     staying well under the parent's 10s failsafe.
    //
    // Why not skip SIGTERM and SIGKILL straight away: SIGKILL severs
    // the process abruptly, leaving uvicorn no chance to flush its
    // log buffers (those go to ``~/.blade-ai/logs/tui-server-<pid>.log``
    // via the stderr pipe — losing the tail of a server log makes
    // post-mortem debugging needlessly hard).
    try {
      child.kill("SIGTERM");
      log("SIGTERM sent");
    } catch {
      // already dead — finish() will fire from the exit listener
      log("SIGTERM throw (likely already dead)");
    }

    // Escalate to SIGKILL if SIGTERM didn't take within the grace.
    sigtermTimer = setTimeout(() => {
      if (settled) return;
      log("SIGTERM grace expired, escalating to SIGKILL");
      try {
        child.kill("SIGKILL");
      } catch {
        // ignore — already dead
      }
      // Final 300ms cap after SIGKILL. Kernel reaping is normally
      // instant after the signal is delivered, but some kernels can
      // be lazy if the process is uninterruptibly sleeping (rare,
      // but possible during a syscall like ``kubectl exec``).
      sigkillTimer = setTimeout(() => finish("sigkill-timeout"), 300);
      sigkillTimer.unref?.();
    }, 1500);
    sigtermTimer.unref?.();
  });
}
