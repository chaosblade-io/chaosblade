/**
 * Root component. Tiny by design: it composes the two main regions
 * (MainContent on top, Composer at the bottom). The session is seeded
 * into the store via ``StoreProvider initial`` BEFORE the first render
 * (in cli.tsx) — not via a useEffect dispatch — because Header lives
 * inside Ink's ``<Static>`` and only its first paint lands in the
 * burn-in scrollback. A late dispatch would never reach the user's eye.
 *
 * All business logic lives in:
 *   - state/reducer.ts   (pure state transitions)
 *   - hooks/useStream.ts (SSE bridge → reducer dispatch)
 *   - components/*       (presentational + small store hooks)
 */

import { useEffect } from "react";
import { Box } from "ink";
import type { BladeClient } from "./api/client.js";
import { BootOrchestrator } from "./components/boot/BootOrchestrator.js";
import { Composer } from "./components/Composer.js";
import { MainContent } from "./components/MainContent.js";
import { sessionStatsRef } from "./state/sessionStats.js";
import { useAppState } from "./state/store.js";
import { useOverflowProbe } from "./utils/overflowProbe.js";

interface AppProps {
  /** Null while ``BootRunner`` is still spawning the server / creating
   *  the session. Once non-null, ``BootOrchestrator`` and ``Composer``
   *  mount; until then those subtrees are skipped (they require a
   *  reachable backend) and the user sees only the dynamic-area boot
   *  spinner from MainContent. */
  client: BladeClient | null;
  /** Empty string until the session has been created on the server. */
  sessionId: string;
  serverUrl: string;
  version: string;
  /** ISO timestamp captured before the boot phases start, threaded
   *  through to ``BootOrchestrator`` so the doctor card's captured-at
   *  matches when the check started rather than when it landed. */
  bootCapturedAt: string;
}

/**
 * Mirrors the latest reducer state into a module-level ref so cli.tsx
 * can read it after Ink unmounts. Renders nothing.
 *
 * We write the ref TWICE on purpose: once synchronously during render
 * (so the very last state is visible even if Ink unmounts before the
 * effect tick fires) and again in ``useEffect`` (the canonical place
 * for side effects). The render-time write is idempotent (same value
 * yields same write) and doesn't fall under React's "no side effects
 * during render" rule — that's about effects on external systems
 * tied to React's lifecycle, not benign module-scope mirrors that
 * exist precisely to survive unmount.
 */
const StateExporter: React.FC = () => {
  const state = useAppState();
  sessionStatsRef.current = state;
  useEffect(() => {
    sessionStatsRef.current = state;
  }, [state]);
  return null;
};

export const App: React.FC<AppProps> = ({
  client,
  sessionId,
  serverUrl,
  version,
  bootCapturedAt,
}) => {
  // Overflow diagnostic. Inert unless ``BLADE_AI_DEBUG_OVERFLOW=1``;
  // when active, writes one JSON line per layout commit to
  // ``~/.blade-ai/logs/tui-overflow-debug.log`` so we can see whether
  // the dynamic frame is exceeding the viewport during inject (which
  // would explain the user-reported flicker + scroll hijack via the
  // PR-#917/#936 patch path leaking overflow rows into scrollback).
  useOverflowProbe();
  return (
    <Box flexDirection="column">
      <StateExporter />
      {/* BootOrchestrator and Composer require a reachable backend.
          While ``BootRunner`` is still doing the spawn / health / create
          handshake, ``client`` is null and we render only StateExporter
          + MainContent — the latter shows the boot spinner sourced from
          ``state.bootProgress`` (set by BootRunner during the handshake
          and by BootOrchestrator afterwards). */}
      {client && (
        <BootOrchestrator client={client} capturedAt={bootCapturedAt} />
      )}
      <MainContent version={version} serverUrl={serverUrl} />
      {client && sessionId && (
        <Composer client={client} sessionId={sessionId} />
      )}
    </Box>
  );
};
