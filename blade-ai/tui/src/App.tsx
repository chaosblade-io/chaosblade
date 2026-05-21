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
 *
 * Perf note: ``App`` deliberately does NOT subscribe to AppState. Two
 * subscriptions used to live here:
 *   · ``<StateExporter/>`` mirrored state to ``sessionStatsRef`` —
 *     now lives inside ``StoreProvider`` itself.
 *   · ``useOverflowProbe()`` subscribed to 9-10 state slices for a
 *     debug-only feature gated on ``BLADE_AI_DEBUG_OVERFLOW=1`` —
 *     now mounted as a separate ``<OverflowProbeMount/>`` only when
 *     the env var is set, so prod App has zero state subscriptions.
 *
 * Combined effect: App.render() runs once per <App> mount, not per
 * reducer action. Any future subscription added here will re-introduce
 * the cascade — keep this component subscription-free.
 */

import { Box } from "ink";
import type { BladeClient } from "./api/client.js";
import { BootOrchestrator } from "./components/boot/BootOrchestrator.js";
import { Composer } from "./components/Composer.js";
import { MainContent } from "./components/MainContent.js";
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
 * Lazy-mounted overflow probe. Only rendered when
 * ``BLADE_AI_DEBUG_OVERFLOW=1`` is set at process start — production
 * App never mounts this, so the probe's 9 ``useAppSelector``
 * subscriptions never attach to the Context graph and don't trigger
 * App-tree re-renders on every reducer dispatch.
 *
 * Pattern: import the hook statically (cheap one-time module load),
 * only CALL it inside the component, and only mount the component
 * when the env var is set. The unused import in prod resolves to a
 * function definition that's never invoked → no React subscriptions.
 */
const OVERFLOW_PROBE_ENABLED =
  process.env["BLADE_AI_DEBUG_OVERFLOW"] === "1";

const OverflowProbeMount: React.FC = () => {
  useOverflowProbe();
  return null;
};

export const App: React.FC<AppProps> = ({
  client,
  sessionId,
  serverUrl,
  version,
  bootCapturedAt,
}) => {
  return (
    <Box flexDirection="column">
      {/* Debug-only — see OVERFLOW_PROBE_ENABLED above. Not mounted
       *  in production (env var unset) so the probe's 9
       *  ``useAppSelector`` subscriptions never attach. */}
      {OVERFLOW_PROBE_ENABLED && <OverflowProbeMount />}
      {/* BootOrchestrator and Composer require a reachable backend.
          While ``BootRunner`` is still doing the spawn / health / create
          handshake, ``client`` is null and we render only MainContent
          — which shows the boot spinner sourced from
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
