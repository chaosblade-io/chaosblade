/**
 * Top-of-session brand header — intentionally rendered as ``null``.
 *
 * The earlier brand-bar header (``▎ ✻ BLADE-AI v… / cluster · ns ·
 * model``) was dropped per user request: the boot deck (WelcomeCard
 * + BootDoctor + PendingTasks) already carries the brand identity
 * and runtime metadata on first paint, so a separate persistent
 * header on top of that read as redundant chrome.
 *
 * The component is kept (instead of removed) so MainContent's
 * ``<Static>`` items array stays stable: dropping the header from
 * the items array mid-session would force Static to re-emit every
 * subsequent history item, which would burn the entire scrollback
 * a second time. Returning ``null`` here means Static still has a
 * ``header`` entry — just with no visible output — preserving the
 * stable items contract.
 *
 * The ``Props`` type is preserved so callers (MainContent) don't
 * need to change; ``// eslint-disable-next-line @typescript-
 * eslint/no-unused-vars`` is not required because the props are
 * spread into the (unused) function signature but unreferenced —
 * TypeScript's default ``noUnusedParameters`` rule is satisfied by
 * destructuring all three names.
 */

import type { SessionInfo } from "../state/types.js";

interface Props {
  version: string;
  session: SessionInfo;
  serverUrl: string;
}

export const Header: React.FC<Props> = (_props) => null;
