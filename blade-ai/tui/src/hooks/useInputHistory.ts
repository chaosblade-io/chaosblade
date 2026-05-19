/**
 * Persistent line history for InputPrompt.
 *
 * - Reads ``~/.blade-ai/history`` once on mount (best-effort; missing
 *   file = empty history).
 * - ``push(line)`` appends to memory + the file. Duplicates of the
 *   most-recent entry are skipped so spamming Enter doesn't fill it.
 * - ``prev(current)`` walks one step back; saves the current draft so
 *   coming back to the bottom restores it.
 * - ``next()`` walks one step forward; returns the saved draft when
 *   passing the latest entry.
 * - ``reset()`` clears the cursor (called after a successful submit).
 *
 * The hook keeps its index in a ref so calling prev/next never
 * triggers a re-render — only ``setValue`` in the consumer does.
 */

import { promises as fs } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { useEffect, useRef } from "react";

// Distinct from the Python TUI's ``~/.blade-ai/history`` file, which
// uses prompt_toolkit's FileHistory format (lines prefixed with ``+``,
// metadata lines prefixed with ``#``). Reading that into our plain-line
// store would surface the metadata as fake history entries.
const HISTORY_FILE = join(homedir(), ".blade-ai", "tui-history");
const MAX_LINES = 1000;

export interface InputHistoryApi {
  push: (line: string) => void;
  prev: (current: string) => string | null;
  next: (current: string) => string | null;
  reset: () => void;
}

export function useInputHistory(): InputHistoryApi {
  // entries[0] is the oldest line, entries[len-1] the newest.
  const entries = useRef<string[]>([]);
  // -1 means "the user is editing a fresh draft, not browsing".
  const cursor = useRef<number>(-1);
  // The draft the user had when they started browsing — restored
  // when they walk back below the newest entry.
  const draft = useRef<string>("");

  useEffect(() => {
    void (async () => {
      try {
        const raw = await fs.readFile(HISTORY_FILE, "utf8");
        const lines = raw
          .split("\n")
          .map((l) => l.replace(/\r$/, ""))
          .filter((l) => l.length > 0);
        entries.current = lines.slice(-MAX_LINES);
      } catch {
        // No file yet — that's fine.
      }
    })();
  }, []);

  const push = (line: string): void => {
    const trimmed = line;
    if (!trimmed) return;
    const last = entries.current[entries.current.length - 1];
    if (last !== trimmed) {
      entries.current.push(trimmed);
      if (entries.current.length > MAX_LINES) {
        entries.current.splice(0, entries.current.length - MAX_LINES);
      }
      void appendToFile(trimmed);
    }
    cursor.current = -1;
    draft.current = "";
  };

  const prev = (current: string): string | null => {
    if (entries.current.length === 0) return null;
    if (cursor.current === -1) {
      // Stash the current draft so ``next`` can restore it.
      draft.current = current;
      cursor.current = entries.current.length - 1;
    } else if (cursor.current > 0) {
      cursor.current -= 1;
    } else {
      // Already at the oldest entry.
      return null;
    }
    return entries.current[cursor.current] ?? null;
  };

  const next = (_current: string): string | null => {
    if (cursor.current === -1) return null;
    if (cursor.current >= entries.current.length - 1) {
      // Past the newest entry → restore the original draft.
      cursor.current = -1;
      const d = draft.current;
      draft.current = "";
      return d;
    }
    cursor.current += 1;
    return entries.current[cursor.current] ?? null;
  };

  const reset = (): void => {
    cursor.current = -1;
    draft.current = "";
  };

  return { push, prev, next, reset };
}

async function appendToFile(line: string): Promise<void> {
  try {
    await fs.mkdir(dirname(HISTORY_FILE), { recursive: true });
    await fs.appendFile(HISTORY_FILE, `${line}\n`, "utf8");
  } catch {
    // Best-effort: a missing $HOME or read-only fs shouldn't break
    // input handling.
  }
}
