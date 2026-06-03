/**
 * Boot-time environment self-check card. Mirrors the Python TUI's
 * "环境自检 N/M 通过" panel. Glyph per row:
 *   ✓  passed
 *   ⚠  warning (non-blocking failure)
 *   ✗  blocking failure
 *
 * Below the rows, a suggestions block lists ``fix`` text for any row
 * that failed. Empty when everything's green.
 */

import { Box, Text } from "ink";
import { memo } from "react";
import { t } from "../../i18n/index.js";
import type { BootDoctorCardItem } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";
import { CheckList } from "../shared/CheckList.js";
import { BootCardFrame } from "./BootCardFrame.js";

/** Render the ISO timestamp as ``HH:MM:SS`` in the local zone. Falls
 *  back to the raw string when parsing fails. */
function formatTimeOfDay(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

const BootDoctorCardInternal: React.FC<{ item: BootDoctorCardItem }> = ({
  item,
}) => {
  if (item.unavailable) {
    return (
      <BootCardFrame>
        <Text color={Theme.text.accent} bold>
          {Icons.thinking} {t("boot.doctor.title")}
        </Text>
        <Box marginTop={1}>
          <Text color={Theme.status.warn}>
            {t("boot.doctor.unavailable")}
          </Text>
        </Box>
      </BootCardFrame>
    );
  }

  return (
    <BootCardFrame>
      <Box marginBottom={1}>
        <Text color={Theme.text.accent} bold>
          {Icons.thinking} {t("boot.doctor.title")}{" "}
        </Text>
        <Text color={Theme.text.secondary}>
          {t("boot.doctor.summary", {
            passed: item.passedCount,
            total: item.totalCount,
          })}
        </Text>
        {item.capturedAt && (
          <Text color={Theme.text.secondary}>
            {"  · "}
            {t("boot.doctor.captured_at", {
              time: formatTimeOfDay(item.capturedAt),
            })}
          </Text>
        )}
      </Box>
      <CheckList checks={item.checks} />
    </BootCardFrame>
  );
};

// React.memo: boot doctor item is dispatched once during the boot
// sequence and never mutated.
export const BootDoctorCard = memo(BootDoctorCardInternal);
