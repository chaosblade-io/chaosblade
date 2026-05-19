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
 * Right side — minimal session signals: mode + ns. The token meter is
 *              intentionally NOT here — it lives in LoadingIndicator
 *              while a turn is running, and is otherwise off-screen
 *              until /status surfaces it on demand.
 */

import { Box, Text } from "ink";
import { useAppSelector } from "../state/store.js";
import { useTerminalSize } from "../hooks/useTerminalSize.js";
import { t } from "../i18n/index.js";
import { Theme } from "../theme/colors.js";

export const Footer: React.FC = () => {
  const session = useAppSelector((s) => s.session);
  const config = useAppSelector((s) => s.config);
  const { columns } = useTerminalSize();

  if (columns < 40) return null;

  const hint = t("footer.help_hint");
  const right = `${config.permissionMode} · ns:${session.namespace ?? "default"}`;

  return (
    <Box paddingLeft={2} paddingRight={2} marginTop={1} justifyContent="space-between">
      <Text color={Theme.text.secondary}>{hint}</Text>
      <Text color={Theme.text.secondary}>{right}</Text>
    </Box>
  );
};
