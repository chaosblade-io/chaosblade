/**
 * Terminal background-colour Context.
 *
 * Sources its initial value from ``detectTerminalBg`` (run once at
 * boot in cli.tsx, before Ink starts owning stdin). Components that
 * need adaptive contrast subscribe via ``useTerminalBg()`` (returns
 * the kind directly) or ``useTerminalBgInfo()`` (returns the full
 * record + a mutator).
 *
 * Why a Provider (vs. a module-level singleton): the Provider wraps
 * the value in React state so a future ``setKind('light')`` call
 * from a slash command (e.g. ``/theme light``) automatically
 * re-renders every consumer. A module singleton would require an
 * ad-hoc subscriber mechanism for the same outcome.
 *
 * Current consumers: ``UserMessage`` (bubble bg/fg). Future
 * candidates: AgentMessage's ⏺ accent glyph, ConfirmCard borders,
 * any place whose colour was hard-coded against a single canvas.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react";
import type { TerminalBgInfo, TerminalBgKind } from "../utils/terminalBg.js";

interface TerminalBgContextValue {
  /** Current resolved kind. Drives palette selection in consumers. */
  kind: TerminalBgKind;
  /** Where the value came from — env override / COLORFGBG / OSC 11 /
   *  fallback / manual. Useful for /doctor display and for callers
   *  who want to skip auto-detection results in favour of explicit
   *  user choice. */
  source: TerminalBgInfo["source"] | "manual";
  /** Mutator. Slash commands like ``/theme light`` should call this
   *  with ``source: "manual"`` to flip the value at runtime. Triggers
   *  re-render of every ``useTerminalBg()`` consumer in the tree. */
  setKind: (kind: TerminalBgKind, source?: "manual") => void;
  /** Raw RGB if the source was OSC 11 — surfaced verbatim in /doctor
   *  so users can sanity-check the probed value matches their
   *  terminal theme. Undefined for other sources. */
  rgb?: TerminalBgInfo["rgb"];
  /** How long the initial detection took. Surfaced in /doctor. */
  detectMs: number;
}

const Ctx = createContext<TerminalBgContextValue>({
  kind: "dark",
  source: "fallback",
  setKind: () => {
    // no-op default — only reachable when a consumer renders outside
    // any Provider (which shouldn't happen in production, but tests
    // sometimes render components in isolation).
  },
  detectMs: 0,
});

/** Subscribe to the current kind only. Returns 'light' or 'dark'. */
export function useTerminalBg(): TerminalBgKind {
  return useContext(Ctx).kind;
}

/** Full info + mutator. Used by /doctor and by future /theme handler. */
export function useTerminalBgInfo(): TerminalBgContextValue {
  return useContext(Ctx);
}

/** Wrap the React tree with this. ``initial`` should come from a
 *  call to ``detectTerminalBg()`` made BEFORE the Ink render — the
 *  Provider seeds its internal state from it. */
export const TerminalBgProvider: React.FC<{
  initial: TerminalBgInfo;
  children: React.ReactNode;
}> = ({ initial, children }) => {
  const [state, setState] = useState<{
    kind: TerminalBgKind;
    source: TerminalBgContextValue["source"];
    rgb?: TerminalBgInfo["rgb"];
  }>({
    kind: initial.kind,
    source: initial.source,
    rgb: initial.rgb,
  });

  const setKind = useCallback<TerminalBgContextValue["setKind"]>(
    (kind, source = "manual") => {
      setState((prev) =>
        prev.kind === kind && prev.source === source
          ? prev
          : { kind, source, rgb: undefined },
      );
    },
    [],
  );

  const value = useMemo<TerminalBgContextValue>(
    () => ({
      kind: state.kind,
      source: state.source,
      rgb: state.rgb,
      detectMs: initial.detectMs,
      setKind,
    }),
    [state.kind, state.source, state.rgb, initial.detectMs, setKind],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
};
