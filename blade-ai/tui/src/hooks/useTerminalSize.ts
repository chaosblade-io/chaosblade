/**
 * Track the terminal's columns × rows. Re-fires on SIGWINCH so the layout
 * adapts mid-session (split a tmux pane, resize the window, etc.).
 *
 * Why a module-level singleton subscription instead of one
 * ``process.stdout.on("resize", ...)`` per hook instance: there are 6+
 * components (Footer, ToolMessage, AgentMessage, WelcomeCard,
 * BootCardFrame, LoadingIndicator's useLoadingIndicator) that read the
 * size, and ToolMessage/AgentMessage are *per-instance* — a turn with
 * a handful of pending tool cards plus a streaming agent reply easily
 * pushes past Node's default ``MaxListeners = 10`` cap on the stdout
 * EventEmitter, which surfaces as the
 *
 *     MaxListenersExceededWarning: 11 resize listeners added to
 *     [WriteStream]. MaxListeners is 10.
 *
 * line in the user's terminal. The fix is structural: keep exactly one
 * SIGWINCH subscription on the stream, fan out to React subscribers via
 * an in-process Set. Subscriber churn (mount / unmount) only touches
 * the Set, never the stream's listener list.
 */

import { useEffect, useState } from "react";

export interface TerminalSize {
  columns: number;
  rows: number;
}

function read(): TerminalSize {
  return {
    columns: process.stdout.columns ?? 80,
    rows: process.stdout.rows ?? 24,
  };
}

type Subscriber = (size: TerminalSize) => void;
const subscribers = new Set<Subscriber>();
let listenerInstalled = false;

function broadcast(): void {
  const next = read();
  for (const sub of subscribers) sub(next);
}

function ensureListener(): void {
  if (listenerInstalled) return;
  process.stdout.on("resize", broadcast);
  listenerInstalled = true;
}

export function useTerminalSize(): TerminalSize {
  // Initial state reads fresh from stdout so callers (and tests that
  // mutate ``process.stdout.columns`` before render) get the current
  // dimensions on the first paint.
  const [size, setSize] = useState<TerminalSize>(read);

  useEffect(() => {
    ensureListener();
    // Each subscriber bails out via ``Object.is``-style equality on the
    // size fields so a no-op resize event (some terminals emit
    // SIGWINCH on focus changes) doesn't churn React state for every
    // mounted consumer.
    const subscriber: Subscriber = (next) => {
      setSize((prev) =>
        prev.columns === next.columns && prev.rows === next.rows ? prev : next,
      );
    };
    subscribers.add(subscriber);
    // Sync once after subscribing in case a resize fired between the
    // initial ``useState(read)`` and this effect running.
    subscriber(read());
    return () => {
      subscribers.delete(subscriber);
    };
  }, []);

  return size;
}

/** Threshold below which the layout collapses to single-column. */
export const NARROW_THRESHOLD = 60;

export function isNarrow(columns: number): boolean {
  return columns <= NARROW_THRESHOLD;
}
