/**
 * Live preflight check list — shared by {@link BootDoctorCard} (the
 * one-shot snapshot rendered at startup) and {@link RuntimeDoctorCard}
 * (the on-demand ``/doctor`` re-probe). Both surface the same seven
 * ``CheckResult`` rows from ``server/routes/preflight.py``, so the row
 * grammar lives here once.
 *
 * Visual contract:
 *   - One row per check, with status glyph + name + message
 *   - Glyph + message colour coded by status:
 *       · passed     → ``Theme.status.ok`` (yellow-green)
 *       · warning    → ``Theme.status.warn`` (gold)
 *       · blocking   → ``Theme.status.err`` (coral red)
 *   - Name column uses terminal-default fg so it adapts to dark/light
 *     terminals (``Theme.text.primary`` is intentionally undefined).
 *   - Optional "fixes" block below — shows the ``fix`` hints for any
 *     row that didn't pass and carries one. Empty for an all-green run.
 */

import { Box, Text } from "ink";
import { t } from "../../i18n/index.js";
import type { BootDoctorCheck } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";

const NAME_COL_WIDTH = 30;
const GLYPH_COL_WIDTH = 3;

function glyphFor(c: BootDoctorCheck): string {
  if (c.passed) return Icons.success;
  return c.severity === "warning" ? Icons.warning : Icons.fail;
}

function colorFor(c: BootDoctorCheck): string {
  if (c.passed) return Theme.status.ok;
  return c.severity === "warning" ? Theme.status.warn : Theme.status.err;
}

export const CheckList: React.FC<{ checks: BootDoctorCheck[] }> = ({
  checks,
}) => {
  const fixes = checks.filter((c) => !c.passed && c.fix);
  return (
    <Box flexDirection="column">
      {checks.map((c) => {
        const color = colorFor(c);
        return (
          <Box key={c.name}>
            <Box minWidth={GLYPH_COL_WIDTH}>
              <Text color={color}>{glyphFor(c)}</Text>
            </Box>
            <Box minWidth={NAME_COL_WIDTH}>
              <Text>{c.name}</Text>
            </Box>
            <Box flexGrow={1}>
              <Text color={color} wrap="truncate-end">
                {c.passed
                  ? c.message?.trim() || t("boot.doctor.passed_short")
                  : c.message}
              </Text>
            </Box>
          </Box>
        );
      })}
      {fixes.length > 0 && (
        <Box marginTop={1} flexDirection="column">
          <Box>
            <Text color={Theme.text.accent} bold>
              {t("boot.doctor.fixes_header")}
            </Text>
          </Box>
          {fixes.map((c) => (
            <Box key={c.name} paddingLeft={2}>
              <Text color={Theme.text.secondary} wrap="wrap">
                {c.fix}
              </Text>
            </Box>
          ))}
        </Box>
      )}
    </Box>
  );
};
