/**
 * External store + ``useSyncExternalStore``-backed selector hooks.
 *
 * Why not the old Context + ``useReducer``: Context propagation forces
 * EVERY subscriber to re-render whenever the provided value changes.
 * With a single AppState object passed through Context, any reducer
 * dispatch made every ``useAppState()`` / ``useAppSelector(...)``
 * consumer re-render — the selector was decorative, the underlying
 * subscription was still "subscribe to the whole tree". That cascade
 * was the biggest source of per-keystroke latency during streaming
 * (token append → reducer → Context broadcast → 55+ subscribers
 * re-rendered together).
 *
 * The new shape:
 *   · A module-level subscription pool lives outside React. Dispatch
 *     mutates state in place and notifies listeners.
 *   · ``useAppSelector`` subscribes via ``useSyncExternalStore`` with
 *     a SELECTOR-MEMOIZED snapshot — when the slice the caller wants
 *     hasn't changed (by Object.is), the cached reference is
 *     returned, useSyncExternalStore sees the same value, no
 *     re-render is scheduled. Consumers re-render only when THEIR
 *     slice actually moves.
 *   · ``useAppDispatch`` returns a stable function reference for the
 *     lifetime of the Provider — handler dependency arrays stay tight.
 *
 * Backwards-compat: ``useAppState`` is preserved (returns the whole
 * AppState) but kept as a deprecated escape hatch. Production code
 * should always use ``useAppSelector`` with a narrow selector.
 *
 * Tests: each ``<StoreProvider>`` instance lazy-inits its own Store
 * via ``useState``, so test renders stay isolated from each other and
 * from any cli.tsx instance.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useRef,
  useState,
  useSyncExternalStore,
  type Dispatch,
  type ReactNode,
} from "react";
import { reducer, type Action } from "./reducer.js";
import { sessionStatsRef } from "./sessionStats.js";
import { initialAppState, type AppState } from "./types.js";
import { recordAction } from "../utils/overflowProbe.js";

type Listener = () => void;

/**
 * The external store. Lives outside React's reconciliation entirely;
 * React subscribes via ``useSyncExternalStore`` and is notified
 * imperatively on every state change.
 */
class Store {
  private _state: AppState;
  private listeners = new Set<Listener>();

  constructor(initial: AppState) {
    this._state = initial;
    // Seed the goodbye-card mirror with the initial state so a crash
    // before the first dispatch still has something to show.
    sessionStatsRef.current = initial;
  }

  /** Read-only snapshot accessor. Used by ``getSnapshot`` paths and
   *  by ``useAppState`` for compatibility. */
  get state(): AppState {
    return this._state;
  }

  /** Register a listener. Returns an unsubscribe function — the
   *  contract ``useSyncExternalStore`` expects. */
  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  /** Apply an action through the reducer + notify subscribers.
   *
   *  - ``recordAction`` is the debug-only telemetry hook
   *    (``BLADE_AI_DEBUG_OVERFLOW=1``); no-op in production.
   *  - ``sessionStatsRef`` mirrors the latest state for cli.tsx to
   *    read AFTER Ink unmounts (goodbye card). Writing here keeps
   *    the mirror in sync without forcing React to track it.
   *  - The reducer is allowed to return ``state === next`` for
   *    actions that don't actually change anything (the reducer
   *    doesn't always do this today, but we honour it where it
   *    does — same-reference returns skip notification entirely).
   */
  dispatch = (action: Action): void => {
    recordAction(action);
    const next = reducer(this._state, action);
    if (next === this._state) {
      return;
    }
    this._state = next;
    sessionStatsRef.current = next;
    // Snapshot the listener set to a fresh iterable so a listener
    // that subscribes / unsubscribes during its own callback can't
    // mutate the Set under us mid-iteration.
    const snapshot = [...this.listeners];
    for (const l of snapshot) {
      l();
    }
  };
}

// ── Provider + context plumbing ──────────────────────────────────────

const StoreCtx = createContext<Store | null>(null);

export interface StoreProviderProps {
  children: ReactNode;
  /** Optional initial state override (for tests). */
  initial?: Partial<AppState>;
}

export const StoreProvider: React.FC<StoreProviderProps> = ({
  children,
  initial,
}) => {
  // Lazy-init: store is created once per Provider mount, never on
  // re-render. ``useState`` is the canonical way to anchor a non-
  // serialisable object to a React subtree's lifetime.
  const [store] = useState(
    () =>
      new Store(initial ? { ...initialAppState, ...initial } : initialAppState),
  );
  return <StoreCtx.Provider value={store}>{children}</StoreCtx.Provider>;
};

function useStore(): Store {
  const ctx = useContext(StoreCtx);
  if (ctx === null) {
    throw new Error("useStore must be used inside <StoreProvider>");
  }
  return ctx;
}

// ── Public hooks ─────────────────────────────────────────────────────

/**
 * Subscribe to a derived slice of AppState. Re-renders the consumer
 * ONLY when the selected value's reference changes (by ``Object.is``).
 *
 * Important constraints:
 *   · The selector should be a pure function of state.
 *   · For object/array slices, return a STABLE reference from state
 *     (e.g. ``s.session``, ``s.pending``) — don't construct a new
 *     object inside the selector or Object.is will always be false
 *     and the optimisation is defeated.
 *   · Primitive returns (string / number / boolean) work naturally.
 *
 * The selector identity itself can change every render (typical
 * inline-lambda pattern) — we recompute the value and compare with
 * the previous cached one. No useCallback required at the call site.
 */
export function useAppSelector<T>(selector: (s: AppState) => T): T {
  const store = useStore();

  // Cache holds the LAST returned slice + the LAST selector used to
  // produce it. If a re-render happens for unrelated reasons we may
  // get the same selector identity — recompute, compare, return
  // cached on Object.is hit so useSyncExternalStore's own ref
  // comparison short-circuits.
  const cacheRef = useRef<{ snapshot: T; hasValue: boolean }>({
    snapshot: undefined as unknown as T,
    hasValue: false,
  });

  // ``getSnapshot`` is called by useSyncExternalStore during render
  // AND immediately after each subscribe notification. Object.is
  // gate keeps the returned reference stable across calls when the
  // slice didn't move — that's what skips the re-render.
  const getSnapshot = useCallback(() => {
    const next = selector(store.state);
    if (
      cacheRef.current.hasValue &&
      Object.is(next, cacheRef.current.snapshot)
    ) {
      return cacheRef.current.snapshot;
    }
    cacheRef.current = { snapshot: next, hasValue: true };
    return next;
  }, [store, selector]);

  return useSyncExternalStore(store.subscribe, getSnapshot, getSnapshot);
}

/**
 * Returns the dispatch function for the active store. Reference is
 * STABLE across renders — safe to put in useCallback / useEffect
 * dependency arrays without retriggering.
 */
export function useAppDispatch(): Dispatch<Action> {
  return useStore().dispatch;
}

/**
 * Returns a stable function that reads the LATEST AppState on each
 * call WITHOUT subscribing — the caller doesn't re-render when state
 * changes. Used by event handlers (slash command invocations, key
 * presses) that need a state snapshot at the moment they fire, NOT a
 * live binding to state changes.
 *
 * Without this, the only way to access full AppState in a handler was
 * ``const state = useAppState()`` which subscribes to the entire tree
 * and re-renders the host on every dispatch — exactly the cascade
 * Phase 1 is removing.
 *
 * The returned function is referentially stable for the Provider's
 * lifetime — safe to put in dependency arrays.
 */
export function useAppStateGetter(): () => AppState {
  const store = useStore();
  return useCallback(() => store.state, [store]);
}

/**
 * ⚠️  Deprecated escape hatch — subscribes to the WHOLE AppState and
 * re-renders on every dispatch. Use ``useAppSelector(selector)``
 * instead.
 *
 * Kept exported for backwards compatibility with code that hasn't
 * migrated yet. New call sites should NOT add this — the per-keystroke
 * cascade it triggers is exactly what Phase 1.1 of the perf overhaul
 * was meant to eliminate (see ``docs/design/tui-perf-overhaul-plan.md``).
 */
export function useAppState(): AppState {
  return useAppSelector((s) => s);
}
