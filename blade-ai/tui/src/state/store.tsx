/**
 * Lightweight global store via React Context + useReducer.
 *
 * We don't pull zustand for this — at this scale the bundle savings
 * matter and Context with a single split (state vs dispatch) avoids
 * re-renders for components that only need ``dispatch``.
 *
 * Usage:
 *   const state = useAppState();
 *   const dispatch = useAppDispatch();
 *
 * Using two contexts (state + dispatch) keeps event handlers stable
 * across renders — components that call only ``useAppDispatch()`` won't
 * re-render when state changes.
 */

import React, {
  createContext,
  useContext,
  useMemo,
  useReducer,
  type Dispatch,
  type ReactNode,
} from "react";
import { reducer, type Action } from "./reducer.js";
import { initialAppState, type AppState } from "./types.js";

const StateCtx = createContext<AppState | null>(null);
const DispatchCtx = createContext<Dispatch<Action> | null>(null);

export interface StoreProviderProps {
  children: ReactNode;
  /** Optional initial state override (for tests). */
  initial?: Partial<AppState>;
}

export const StoreProvider: React.FC<StoreProviderProps> = ({
  children,
  initial,
}) => {
  const [state, dispatch] = useReducer(
    reducer,
    initial ? { ...initialAppState, ...initial } : initialAppState,
  );

  // Memo'd dispatch is referentially stable already (React guarantees);
  // we still wrap with useMemo for readability.
  const dispatchValue = useMemo(() => dispatch, [dispatch]);

  return (
    <StateCtx.Provider value={state}>
      <DispatchCtx.Provider value={dispatchValue}>
        {children}
      </DispatchCtx.Provider>
    </StateCtx.Provider>
  );
};

export function useAppState(): AppState {
  const ctx = useContext(StateCtx);
  if (ctx === null) {
    throw new Error("useAppState must be used inside <StoreProvider>");
  }
  return ctx;
}

export function useAppDispatch(): Dispatch<Action> {
  const ctx = useContext(DispatchCtx);
  if (ctx === null) {
    throw new Error("useAppDispatch must be used inside <StoreProvider>");
  }
  return ctx;
}

/**
 * Selector hook — pull a derived slice from state. Doesn't optimize
 * re-renders (Context propagates always); for hot paths we'd switch
 * to ``useSyncExternalStore``. M2 keeps it simple.
 */
export function useAppSelector<T>(selector: (s: AppState) => T): T {
  return selector(useAppState());
}
