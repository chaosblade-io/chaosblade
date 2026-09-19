/**
 * Parse ``--resume <sid>`` from the TUI process argv.
 *
 * The flag is injected by the Python CLI's ``blade-ai resume -i <sid>``
 * subcommand (execvp hands the terminal over with the extra args), and
 * tells BootRunner to take over that session instead of creating a
 * fresh one.
 *
 * Returns:
 *  - ``string`` — the sid that follows ``--resume``
 *  - ``null`` — no ``--resume`` flag present (normal boot)
 *  - ``undefined`` — the flag is present but its value is missing
 *    (``--resume`` at the end of argv, or an empty ``--resume=``)
 *
 * The sid is NOT validated here — the server's SESSION_ID_PATTERN does
 * that on the resume route; an invalid sid fails loud through the
 * normal onFailed path.
 */
export function parseResumeArgv(
  argv: readonly string[],
): string | null | undefined {
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === undefined) continue;
    if (arg === "--resume") {
      const next = argv[i + 1];
      return next !== undefined && next !== "" ? next : undefined;
    }
    if (arg.startsWith("--resume=")) {
      const sid = arg.slice("--resume=".length);
      return sid !== "" ? sid : undefined;
    }
  }
  return null;
}
