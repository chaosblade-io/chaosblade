/**
 * Module-level DOM ref shared between the component that *owns* the
 * chrome below the pending area (``Composer``) and the component that
 * needs to know its measured pixel height (``MainContent`` — for the
 * per-pending-item ``availableTerminalHeight`` budget).
 *
 * Why a module-level ref instead of React context or prop drilling:
 *
 *   - ``MainContent`` and ``Composer`` are sibling subtrees under
 *     ``App.tsx``. There is no shared ancestor below ``App`` that
 *     would be a natural Context provider, and lifting the ref to
 *     ``App`` would require a forwardRef ladder + extra renders.
 *
 *   - The ref is read in a ``useLayoutEffect`` inside ``MainContent``
 *     (post-commit, after Composer's ref callback has fired in the
 *     same React commit phase). Module-level access keeps the read
 *     site outside React's data-flow entirely.
 *
 *   - Mirrors the established ``sessionStatsRef`` / ``streamingRefs``
 *     pattern in this codebase — module-level mutable refs are the
 *     canonical "data Ink needs but React shouldn't re-render for"
 *     container.
 *
 * Lifetime:
 *
 *   - Composer's outermost Box wires its Ink ``ref`` callback to
 *     ``setChromeMeasureRef`` (in addition to whatever debug probe
 *     ref the same Box already exposes). The callback fires on every
 *     mount/unmount; ``null`` on unmount tells the reader to fall
 *     back to a conservative default.
 *
 *   - ``MainContent`` reads via ``getChromeMeasureRef`` inside a
 *     ``useLayoutEffect`` whose deps fire whenever the chrome height
 *     could realistically change: ``streamState``, ``activeStepper``,
 *     ``compactionInFlight``, terminal ``rows`` / ``columns``.
 *
 *   - The first render's read is ``null`` (Composer hasn't mounted
 *     yet); ``MainContent`` falls back to its hard-coded
 *     ``CHROME_ROWS_RESERVE`` default. The next render — after
 *     Composer's commit — re-runs the effect, ``measureElement``
 *     returns a real height, and the budget updates to the precise
 *     value.
 */

import type { DOMElement } from "ink";

let chromeRef: DOMElement | null = null;
const refListeners = new Set<() => void>();

/**
 * Coalesce flag for the notify microtask. Rapid ref churn (mount →
 * unmount → mount within one event-loop turn, e.g. WebSocket
 * reconnect + Composer remount, or StrictMode double-invoke) used
 * to schedule one microtask per ``setChromeMeasureRef`` call —
 * each microtask iterated every listener and triggered a separate
 * ``setChromeRefVersion`` bump, snowballing into multiple
 * re-renders for what is logically a single "ref changed" event
 * (listeners only care about the *latest* ``chromeRef``, not the
 * intermediate transitions). The flag flips false → true on the
 * first transition, schedules one microtask, and resets inside
 * that microtask just before the fan-out. Subsequent transitions
 * in the same turn observe ``true`` and skip the redundant
 * scheduling, but still update ``chromeRef`` so the eventual
 * notification sees the final value.
 */
let notifyScheduled = false;

/**
 * Composer wires this on its outermost ``Box`` ref.
 *
 * Why we also notify listeners on every ref transition: ``MainContent``
 * reads the ref in a ``useLayoutEffect`` that runs in commit order
 * with its siblings. React commits children in JSX order — MainContent
 * is mounted *before* Composer — so on the very first render
 * MainContent's layout effect fires while the chrome ref is still
 * ``null``. Without an explicit notification the effect's deps
 * wouldn't change until the first ``streamState`` / ``pending.length``
 * transition, leaving ``chromeHeight`` stuck on the conservative
 * fallback for the entire first turn. Bumping a notification here
 * makes MainContent re-fire its layout effect right after Composer
 * commits, so the measurement lands one microtask after the first
 * render rather than one user action later.
 */
export function setChromeMeasureRef(el: DOMElement | null): void {
  // Dedup: Ink calls callback refs on every render of the host Box;
  // most calls land with the SAME element. Comparing here avoids
  // a re-notification storm during streaming (Composer's render
  // cadence picks up briefly each tick).
  if (chromeRef === el) return;
  chromeRef = el;
  // Coalesce notifications inside a single event-loop turn — see
  // ``notifyScheduled`` docstring for the reconnect/StrictMode
  // scenarios this guards. Only schedule a microtask on the first
  // transition; subsequent transitions update ``chromeRef`` but
  // share the already-pending microtask, so listeners see exactly
  // ONE notification per turn no matter how many ref changes
  // happened, and they observe the latest ``chromeRef`` value.
  if (notifyScheduled) return;
  notifyScheduled = true;
  // queueMicrotask defers notification past the current React commit
  // phase. Calling listener callbacks directly here would let them
  // trigger ``setState`` inside another component's render/commit,
  // which React legitimately warns about (and in concurrent mode
  // can corrupt batching). The microtask still fires *before* any
  // useEffect or paint, so MainContent observes the new ref on the
  // very next render — no perceptible delay for the user.
  queueMicrotask(() => {
    // Reset BEFORE fan-out so a listener that synchronously triggers
    // another ``setChromeMeasureRef`` call (rare but theoretically
    // possible during error recovery) can schedule a fresh
    // notification rather than getting silently coalesced into this
    // one — that recurrence would be a *new* event we DO want to
    // surface.
    notifyScheduled = false;
    for (const cb of refListeners) {
      try {
        cb();
      } catch {
        // A listener throwing must not poison the rest of the
        // notification fan-out.
      }
    }
  });
}

/** ``MainContent`` reads this inside ``useLayoutEffect`` to pass into
 *  ``measureElement``. Returns ``null`` until Composer has mounted. */
export function getChromeMeasureRef(): DOMElement | null {
  return chromeRef;
}

/**
 * Subscribe to ref transitions. ``MainContent`` registers a callback
 * that bumps a local state version, which in turn re-fires its
 * layout-effect chain so the new ref (or its absence on unmount)
 * gets a fresh measurement pass. Returns an unsubscribe handle that
 * the caller MUST invoke on cleanup — without it the listener set
 * leaks Composer-instance closures across hot reloads.
 *
 * Catch-up: if Composer has ALREADY mounted and called
 * ``setChromeMeasureRef`` before the subscriber registers, that
 * notification fired into an empty listener set and was lost. To
 * cover this race we schedule one immediate catch-up notification
 * for the new subscriber whenever ``chromeRef`` is already non-null
 * at subscribe time. The notification still goes through the same
 * queueMicrotask hop so the subscriber's setState lands outside
 * the React commit phase (no setState-during-render warnings) and
 * the cb runs identically to a real ref-change event. The cost is
 * one extra microtask + one extra measure call, with the result
 * being deduplicated by ``setChromeHeight``'s same-value guard.
 */
export function subscribeChromeMeasureRef(cb: () => void): () => void {
  refListeners.add(cb);
  if (chromeRef !== null) {
    queueMicrotask(() => {
      // Re-check membership in case the caller unsubscribed during
      // the same microtask flush (StrictMode double-invoke etc.).
      if (refListeners.has(cb)) {
        try {
          cb();
        } catch {
          // Mirror the resilience of the regular notify path —
          // one listener throwing must not poison the catch-up.
        }
      }
    });
  }
  return () => {
    refListeners.delete(cb);
  };
}
