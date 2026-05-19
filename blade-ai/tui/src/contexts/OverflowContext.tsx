/**
 * Overflow tracking context — pending items report when their content
 * was truncated to fit the dynamic-frame budget; ``ShowMoreLines``
 * reads the aggregated state to render the "Press ctrl-s to show
 * more lines" hint.
 *
 * Shape mirrors qwen-code's contexts/OverflowContext.tsx — same
 * provider pattern, same Set<string> id-tracking, just adapted to
 * blade-ai's import paths and zero external deps.
 */

import { createContext, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

interface OverflowState {
  /** IDs (component-allocated) of items currently overflowing. */
  overflowingIds: Set<string>;
}

interface OverflowActions {
  addOverflowing: (id: string) => void;
  removeOverflowing: (id: string) => void;
}

const OverflowStateContext = createContext<OverflowState | undefined>(
  undefined,
);
const OverflowActionsContext = createContext<OverflowActions | undefined>(
  undefined,
);

export const OverflowProvider: React.FC<{ children: ReactNode }> = ({
  children,
}) => {
  const [overflowingIds, setOverflowingIds] = useState<Set<string>>(
    () => new Set(),
  );

  const actions = useMemo<OverflowActions>(
    () => ({
      addOverflowing: (id: string) =>
        setOverflowingIds((prev) => {
          if (prev.has(id)) return prev;
          const next = new Set(prev);
          next.add(id);
          return next;
        }),
      removeOverflowing: (id: string) =>
        setOverflowingIds((prev) => {
          if (!prev.has(id)) return prev;
          const next = new Set(prev);
          next.delete(id);
          return next;
        }),
    }),
    [],
  );

  const state = useMemo<OverflowState>(
    () => ({ overflowingIds }),
    [overflowingIds],
  );

  return (
    <OverflowStateContext.Provider value={state}>
      <OverflowActionsContext.Provider value={actions}>
        {children}
      </OverflowActionsContext.Provider>
    </OverflowStateContext.Provider>
  );
};

/** Read the current overflow set. ``undefined`` outside a provider —
 *  callers should treat that as "no overflow tracking". */
export function useOverflowState(): OverflowState | undefined {
  return useContext(OverflowStateContext);
}

/** Mutators. ``undefined`` outside a provider so MaxSizedBox can no-op
 *  when rendered in a context (Static / scrollback) where overflow
 *  tracking isn't meaningful. */
export function useOverflowActions(): OverflowActions | undefined {
  return useContext(OverflowActionsContext);
}
