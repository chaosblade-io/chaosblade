/**
 * Vitest config — pure unit tests under src/**\/*.test.ts.
 *
 * Why minimal:
 *   - We're a Node ESM project (no JSDOM/browser). Default node env is
 *     correct for everything in src/state, src/utils, src/i18n.
 *   - React component tests would need ink-testing-library + a separate
 *     env; we deliberately punt on those — smoke scripts already cover
 *     end-to-end reducer / slash dispatch behaviour from outside.
 *   - tsx watches need no extra config; vitest reuses esbuild via its
 *     bundled deps, so .ts imports work out of the box.
 *
 * If you add a test that needs ink rendering, create a sibling
 * config (vitest.config.dom.ts) rather than bloating this one.
 */

import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // Tests that render Ink components are .tsx (JSX); everything
    // else is .ts. Including both globs keeps the discovery list flat.
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    environment: "node",
    // Fail-fast on first error keeps CI logs readable; loosen locally
    // by passing `vitest --reporter=verbose --bail=0`.
    bail: 0,
    reporters: ["default"],
    // Pin locale to zh for test runs. Several Ink-render assertions
    // (ConfirmMessage, YesNoFeedbackSelect) are written against the zh
    // dictionary — that's documented in the file headers as "active
    // dictionary in tests is zh". Locally on macOS this happens to
    // pass because ``LC_ALL=zh_*`` is the developer default, but on
    // GitHub-hosted Linux runners ``LC_ALL=C.UTF-8`` and i18n falls
    // through to en, breaking 8 string assertions. Forcing
    // ``BLADE_AI_LANG=zh`` here makes the test environment match the
    // assertions regardless of host locale; ``i18n/index.ts`` reads
    // this env var BEFORE LC_ALL/LANG, so it always wins.
    env: {
      BLADE_AI_LANG: "zh",
    },
  },
});
