/**
 * Module-level mirror of the latest reducer state, written from inside
 * React by ``<StateExporter/>`` and read from outside React (cli.tsx)
 * after the Ink app has unmounted.
 *
 * Why a ref instead of pulling state via the store directly:
 *   - The store is React Context-based; ``useReducer`` keeps state
 *     inside the React tree. There's no ``store.getState()`` to call
 *     from cli.tsx after unmount.
 *   - Switching the whole project to ``useSyncExternalStore`` /
 *     Zustand-style would be a much larger refactor for the sole
 *     benefit of one consumer (the goodbye card).
 *   - A single mutable ref written on every render is cheap, doesn't
 *     change rendering semantics, and is easy to reason about.
 *
 * Lifetime: written every time the reducer state changes; read once at
 * exit. Set to ``null`` initially so consumers (printGoodbye) can guard
 * "we never had a state to show" — e.g. a crash before the first
 * render.
 */

import type { AppState } from "./types.js";

export const sessionStatsRef: { current: AppState | null } = { current: null };
