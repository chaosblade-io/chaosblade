/**
 * Single source of truth for the TUI's package version.
 *
 * Imports directly from ``package.json`` — esbuild (via tsup) inlines
 * the JSON at bundle time, and tsx (dev mode) resolves it at runtime,
 * so both paths read whatever ``npm version`` last wrote. No more
 * literal-syncing across cli.tsx / commands.ts / package.json.
 *
 * The ``with { type: "json" }`` import attribute is the modern Node
 * 22 syntax; ``resolveJsonModule`` in tsconfig.json keeps tsc happy.
 */

import pkg from "../../package.json" with { type: "json" };

export const PKG_VERSION: string = (pkg as { version: string }).version;
