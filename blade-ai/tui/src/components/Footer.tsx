/**
 * Bottom-of-screen status line. One row only; left/right pair separated
 * by ``flexGrow``. Hidden when the terminal is narrower than 40 cols.
 *
 * Visible across every stream state — idle, responding, and
 * waiting_confirmation. Earlier the row was suppressed during
 * streaming so the LoadingIndicator could "own" it, but the bottom
 * region now always carries the input prompt (responding-state
 * accepts typing while Enter stays locked) and the session-signals
 * line is part of that anchored frame; hiding it on busy made the
 * frame jump on every state flip.
 *
 * Left side  — single contextual hint (mutually exclusive sources).
 * Right side — minimal session signals:
 *   - ``permissionMode`` (auto / confirm)
 *   - ``state size / window (tail)`` indicator, populated by the
 *     PreReasoningHook's ``context_size`` events. ``tail`` is
 *     ``(N.N%)`` in normal mode or ``(error)`` when an ERROR_RECEIVED
 *     frame has fired since the last snapshot. Color-coded:
 *     <70% gray, 70–99% yellow, ≥100% (or error) red. Renders from
 *     boot using the baked-in ``DEFAULT_CONTEXT_MAX_TOKENS`` so the
 *     user never sees a "no data yet" placeholder.
 *
 * The token meter is intentionally separate from the
 * LoadingIndicator's per-turn input/output total — that one is
 * billing-aligned ("how many tokens did this turn cost"), this one
 * is window-pressure-aligned ("how close to auto-compaction").
 * Different question, different number.
 */

import { memo } from "react";
import { Box, Text } from "ink";
import { useAppSelector } from "../state/store.js";
import { useTerminalSize } from "../hooks/useTerminalSize.js";
import { t } from "../i18n/index.js";
import { Theme } from "../theme/colors.js";
import {
  contextSizeSeverity,
  formatContextSize,
} from "../utils/formatContextSize.js";

const FooterInternal: React.FC = () => {
  const config = useAppSelector((s) => s.config);
  const currentTokens = useAppSelector((s) => s.contextCurrentTokens);
  const maxTokens = useAppSelector((s) => s.contextMaxTokens);
  const contextError = useAppSelector((s) => s.contextError);
  const { columns } = useTerminalSize();

  if (columns < 40) return null;

  const hint = t("footer.help_hint");
  // Always renders — no ns:default fallback. ``formatContextSize``
  // substitutes ``DEFAULT_CONTEXT_MAX_TOKENS`` when max is unknown
  // (boot window before first event) so the slot always shows
  // proper numbers.
  const sizeText = formatContextSize(currentTokens, maxTokens, {
    error: contextError,
  });
  const severity = contextSizeSeverity(currentTokens, maxTokens, {
    error: contextError,
  });
  const sizeColor =
    severity === "err"
      ? Theme.status.err
      : severity === "warn"
        ? Theme.status.warn
        : Theme.text.secondary;

  return (
    <Box paddingLeft={2} paddingRight={2} marginTop={1} justifyContent="space-between">
      <Text color={Theme.text.secondary}>{hint}</Text>
      <Box>
        <Text color={Theme.text.secondary}>{config.permissionMode} · </Text>
        <Text color={sizeColor}>{sizeText}</Text>
      </Box>
    </Box>
  );
};

// React.memo: Footer has zero props, so the default shallow
// comparison always returns "props equal" → Footer only re-renders
// when its OWN ``useAppSelector`` subscriptions (session, config)
// trigger a change. Before this, every Composer re-render walked
// Footer's body too (no memo + parent re-render = child re-render
// in React).
export const Footer = memo(FooterInternal);
