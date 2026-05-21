/**
 * DEC mode 2026 — atomic frame buffering at the terminal level.
 *
 * **What it does**: monkey-patches ``stdout.write`` so the FIRST write
 * of an event-loop tick emits ``\x1b[?2026h`` (begin synchronized
 * update), schedules a microtask that emits ``\x1b[?2026l`` (end
 * synchronized update) after the tick drains, and lets every other
 * write through unchanged. Result: terminals that understand the
 * sequence buffer the entire tick's worth of output and commit it
 * atomically when they see the end marker. No partial frames hit
 * the screen.
 *
 * **Why we care**: Ink renders each frame as ``erase prev N lines``
 * + ``write N new lines``. Without synchronization the terminal
 * paints the eraseLines (the screen blanks) *before* the new lines
 * arrive (~milliseconds later on slow ssh, ~microseconds on local
 * tty). For a streaming agent that triggers a render every 60-180ms
 * the eye picks up the blank window as flicker. With sync mode 2026
 * the terminal holds both phases and swaps them in one paint —
 * zero perceived flicker.
 *
 * Supported terminals (gate matches qwen-code's allow-list):
 *   · iTerm2 (TERM_PROGRAM=iTerm.app)
 *   · WezTerm (TERM_PROGRAM=WezTerm)
 *   · kitty (KITTY_WINDOW_ID set, or TERM contains "kitty")
 *
 * SSH / TMUX disabled by default because nested emulators tend to
 * eat or mis-handle the sequence. Override via
 * ``BLADE_AI_SYNCHRONIZED_OUTPUT=1`` (force on) or
 * ``BLADE_AI_DISABLE_SYNCHRONIZED_OUTPUT=1`` (force off).
 *
 * Lifted from qwen-code (Apache-2.0) at
 * ``packages/cli/src/ui/utils/synchronizedOutput.ts`` — same
 * algorithm, env-vars renamed, stats counters trimmed.
 */

const ESC = String.fromCharCode(0x1b);
export const BEGIN_SYNCHRONIZED_UPDATE = `${ESC}[?2026h`;
export const END_SYNCHRONIZED_UPDATE = `${ESC}[?2026l`;

let installed = false;

export function terminalSupportsSynchronizedOutput(
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  // Explicit overrides take precedence — useful for testing on a
  // terminal that supports it but isn't in our allow-list, or
  // disabling it on a misbehaving emulator.
  if (
    env["BLADE_AI_DISABLE_SYNCHRONIZED_OUTPUT"] === "1" ||
    env["BLADE_AI_SYNCHRONIZED_OUTPUT"] === "0"
  ) {
    return false;
  }
  if (
    env["BLADE_AI_FORCE_SYNCHRONIZED_OUTPUT"] === "1" ||
    env["BLADE_AI_SYNCHRONIZED_OUTPUT"] === "1"
  ) {
    return true;
  }

  // Nested terminals tend to mishandle the sequence — bail by default,
  // user can force on via env var if their setup happens to work.
  if (env["TMUX"] || env["SSH_TTY"] || env["SSH_CLIENT"]) {
    return false;
  }

  const termProgram = env["TERM_PROGRAM"];
  if (termProgram === "WezTerm" || termProgram === "iTerm.app") {
    return true;
  }

  const term = env["TERM"];
  return Boolean(env["KITTY_WINDOW_ID"] || term?.includes("kitty"));
}

/**
 * Install the sync-output wrapper on ``stdout``. No-op + returns
 * empty restore when:
 *   - the patch is already installed (idempotent)
 *   - stdout isn't a TTY (piping output → no terminal to sync)
 *   - the active terminal isn't in the supported set
 *
 * Caller is expected to invoke the returned restore on exit so a
 * test teardown or hot-reload doesn't leak the patched write.
 */
export function installSynchronizedOutput(
  stdout: NodeJS.WriteStream = process.stdout,
  env: NodeJS.ProcessEnv = process.env,
): () => void {
  if (installed || !stdout.isTTY || !terminalSupportsSynchronizedOutput(env)) {
    return () => {};
  }

  const originalWrite = stdout.write;
  let inFrame = false;

  const writeControlSequence = (sequence: string) => {
    originalWrite.call(stdout, sequence);
  };

  const endFrame = () => {
    if (!inFrame) return;
    inFrame = false;
    writeControlSequence(END_SYNCHRONIZED_UPDATE);
  };

  const patchedWrite = function (
    this: NodeJS.WriteStream,
    chunk: unknown,
    encodingOrCallback?: BufferEncoding | ((error?: Error | null) => void),
    callback?: (error?: Error | null) => void,
  ) {
    // First write of the tick → wrap. The microtask scheduled here
    // runs after all synchronous writes in the same tick finish,
    // giving us "buffer everything in this tick, commit atomically".
    if (!inFrame) {
      inFrame = true;
      writeControlSequence(BEGIN_SYNCHRONIZED_UPDATE);
      queueMicrotask(endFrame);
    }
    return originalWrite.call(
      this,
      chunk as string | Uint8Array,
      encodingOrCallback as BufferEncoding,
      callback,
    );
  } as typeof stdout.write;

  const exitHandler = () => {
    try {
      endFrame();
    } catch {
      // stdout may already be closed during process shutdown — we
      // can't surface the error anyway, swallow.
    }
  };

  stdout.write = patchedWrite;
  installed = true;
  process.once("exit", exitHandler);

  return () => {
    if (stdout.write === patchedWrite) {
      endFrame();
      stdout.write = originalWrite;
    }
    process.removeListener("exit", exitHandler);
    installed = false;
  };
}
