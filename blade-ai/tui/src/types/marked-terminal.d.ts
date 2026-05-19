/**
 * Minimal local declarations for marked-terminal@7.
 *
 * The package ships no types and ``@types/marked-terminal`` lags
 * behind marked@14. We only consume the ``markedTerminal`` extension
 * factory, which returns a ``MarkedExtension`` shape that ``marked.use``
 * accepts. The renderer is treated as opaque — we don't reach into it.
 */

declare module "marked-terminal" {
  import type { MarkedExtension } from "marked";

  export interface MarkedTerminalOptions {
    width?: number;
    reflowText?: boolean;
    showSectionPrefix?: boolean;
    tab?: number;
    tableOptions?: Record<string, unknown>;
    unescape?: boolean;
    emoji?: boolean;
  }

  export function markedTerminal(
    options?: MarkedTerminalOptions,
    highlightOptions?: Record<string, unknown>,
  ): MarkedExtension;

  export default markedTerminal;
}
