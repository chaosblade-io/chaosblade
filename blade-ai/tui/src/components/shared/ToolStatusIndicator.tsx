/**
 * Single-character status glyph rendered before a tool name.
 *
 *   pending   → ○ (gray)
 *   running   → animated toggle spinner ⊶⊷
 *   success   → ✓ (green)
 *   error     → ✗ (red)
 *   canceled  → ✗ (yellow)
 *
 * Width is fixed at STATUS_INDICATOR_WIDTH columns so the tool name and
 * tree branch stay aligned across status transitions.
 */

import { Box, Text } from "ink";
import InkSpinner from "ink-spinner";
import type { ToolStatus } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";
import { ToolSpinner } from "../../theme/spinners.js";

export const STATUS_INDICATOR_WIDTH = 3;

interface Props {
  status: ToolStatus;
}

export const ToolStatusIndicator: React.FC<Props> = ({ status }) => {
  return (
    <Box minWidth={STATUS_INDICATOR_WIDTH}>
      {status === "running" ? (
        <Text color={Theme.text.primary}>
          <InkSpinner type={ToolSpinner.type} />
        </Text>
      ) : status === "success" ? (
        <Text color={Theme.status.ok}>{Icons.success}</Text>
      ) : status === "error" ? (
        <Text color={Theme.status.err} bold>
          {Icons.fail}
        </Text>
      ) : status === "canceled" ? (
        <Text color={Theme.status.warn}>{Icons.fail}</Text>
      ) : (
        <Text color={Theme.text.secondary}>{Icons.pending}</Text>
      )}
    </Box>
  );
};
