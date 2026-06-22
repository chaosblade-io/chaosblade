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
 *     --ready-stdout``, parse the ``BLADE_AI_READY port=N`` line printed
 *     by the lifespan handler AFTER startup completes (skill loading,
 *     LLM creation, checkpointer setup), and connect to
 *     ``http://127.0.0.1:N``.  Wired to SIGTERM → SIGKILL on shutdown
 *     so a hung server can't orphan past the TUI.
 *
 * Streams:
 *   - stdout: piped, scanned for the ready line, then drained.
 *   - stderr: piped to ``~/.blade-ai/logs/tui-server-<pid>.log``. We
 *     can't ``inherit`` because Ink owns the parent terminal — any
 *     traceback written to fd 2 would corrupt the active render.
 */

import { type ChildProcess, spawn } from "node:child_process";
import { createWriteStream, existsSync, mkdirSync, type WriteStream } from "node:fs";
import { homedir, platform } from "node:os";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

export interface ServerHandle {
  /** Base URL the client should hit, e.g. http://127.0.0.1:54123 */
  url: string;
  /** True when this handle is just a remote pointer (nothing to shutdown). */
  remote: boolean;
  /** Tear down the embedded server. No-op for remote handles. */
  shutdown: () => Promise<void>;
}

const READY_PREFIX = "BLADE_AI_READY port=";
// The ready signal is printed by the lifespan handler AFTER startup
// completes (Python import + port discovery + skill loading + LLM +
// checkpointer). 20s covers cold starts (~2-3s typical) with ample
// margin for slow systems. If a stale process holds the SQLite lock,
// lifespan hangs and this timeout fires — a more accurate error than
// the old "did not pass /health within 10s" which fired because the
// ready signal was printed before uvicorn started.
const SPAWN_TIMEOUT_MS = 20_000;

/**
 * Find the Python interpreter to spawn ``chaos_agent.server.app`` with.
 *
 * Resolution order (first hit wins):
 *
 *   1. ``BLADE_AI_PYTHON`` env var — explicit override, no fallback.
 *      Used by CI / containers / users with non-standard layouts.
 *
 *   2. A project-local virtualenv discovered by walking up the
 *      filesystem from this source file. We check the three common
 *      venv directory names (``.venv`` / ``venv`` / ``env``) under
 *      each ancestor. This is the path that makes ``npm run start``
 *      work out-of-the-box for a developer who set up a venv at the
 *      project root and forgot to activate it before launching the
 *      TUI — the exact failure mode that produced ``spawn python
 *      ENOENT`` on systems where ``python`` isn't on PATH (modern
 *      macOS / Debian-derived distros have ``python3`` only).
 *
 *   3. ``python3`` — falls back to the platform-conventional name.
 *      Modern macOS, Debian, Ubuntu, RHEL 9+ all expose the
 *      interpreter as ``python3``; the bare ``python`` name is no
 *      longer guaranteed to exist. The Windows fallback below
 *      overrides this when the platform is win32 because
 *      python.org installers register ``python.exe`` (no ``3``).
 *
 *   4. ``python`` — last-ditch generic fallback, kept so the
 *      behaviour matches the pre-discovery version when nothing
 *      else lights up. If neither ``python3`` nor ``python`` is on
 *      PATH the spawn will surface ENOENT, same as before.
 *
 * The PyInstaller / curl-bash distribution path is unaffected — its
 * launcher sets ``BLADE_AI_SERVER_BIN`` and we never reach this
 * resolver. See the bin/non-bin branch in ``startEmbeddedServer``.
 */
function resolvePythonExecutable(): string {
  const explicit = process.env["BLADE_AI_PYTHON"];
  if (explicit && explicit.length > 0) return explicit;

  const isWindows = platform() === "win32";
  // Walk up from this source file looking for a venv. ``.venv``
  // first (modern uv / Poetry default), then plain ``venv`` (older
  // Python tutorials), then ``env`` (some Conda exports). 8 levels
  // is plenty — anything deeper than that is malformed.
  let dir = dirname(fileURLToPath(import.meta.url));
  const venvNames = [".venv", "venv", "env"];
  const subpath = isWindows
    ? ["Scripts", "python.exe"]
    : ["bin", "python"];
  for (let i = 0; i < 8; i++) {
    for (const venv of venvNames) {
      const candidate = join(dir, venv, ...subpath);
      if (existsSync(candidate)) return candidate;
    }
    const parent = dirname(dir);
    if (parent === dir) break; // reached filesystem root
    dir = parent;
  }

  // Fallback to platform-conventional name.
  return isWindows ? "python" : "python3";
}

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
  // Force --host 127.0.0.1 for two reasons:
  //   1. The embedded server is local-only by design — binding 0.0.0.0
  //      would expose the agent to the LAN, which is a real risk for
  //      a tool that can run kubectl / chaos-blade.
  //   2. On macOS, the run_server() port-discovery dance binds + closes
  //      a socket on 0.0.0.0 then asks uvicorn to re-bind the same
  //      port. That occasionally fails silently (uvicorn ends up on a
  //      different port). 127.0.0.1 sidesteps the whole issue.
  const commonArgs = [
    "--host",
    "127.0.0.1",
    "--port",
    "0",
    "--ready-stdout",
  ];

  // Two spawn modes — picked by the parent Python launcher:
  //
  //   * PyInstaller / curl-bash install: the launcher exports
  //     ``BLADE_AI_SERVER_BIN`` pointing at the bundled blade-ai
  //     binary. There is no external ``python`` on the user's PATH
  //     in this distribution mode, so we re-invoke the same binary
  //     with the hidden ``__embedded_server__`` subcommand. It runs
  //     ``run_server()`` directly, bypassing typer's CLI dispatch
  //     overhead for the server-only fast path.
  //   * pip / npm dev / source build: env var is unset; ask
  //     ``resolvePythonExecutable`` for the interpreter (which auto-
  //     discovers a project-local venv before falling back to
  //     ``python3`` / ``python`` on PATH) and run
  //     ``-m chaos_agent.server.app``. The venv discovery makes
  //     ``npm run start`` work without manually activating a venv
  //     first, fixing the ``spawn python ENOENT`` symptom on
  //     systems whose PATH only has ``python3``.
  const bin = process.env["BLADE_AI_SERVER_BIN"];
  let cmd: string;
  let args: string[];
  if (bin) {
    cmd = bin;
    args = ["__embedded_server__", ...commonArgs];
  } else {
    cmd = resolvePythonExecutable();
    args = ["-m", "chaos_agent.server.app", ...commonArgs];
  }

  // Pipe stderr into a per-pid log file so server tracebacks don't
  // tear up Ink's render. Best-effort: any FS error falls back to
  // discarding stderr entirely (the user can still re-run with
  // ``BLADE_AI_SERVER=...`` against a server they started themselves).
  const stderrLog = openStderrLog();

  const child = spawn(cmd, args, {
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
