/**
 * Boot-time card listing tasks in non-terminal states. When empty,
 * shows a single "no pending tasks" line; when non-empty, lists
 * task_id + state + fault_type per row so the user can `/replay <id>`
 * or `blade-ai recover <id>` to resume.
 */

import { Box, Text } from "ink";
import { t } from "../../i18n/index.js";
import type { PendingTasksCardItem } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";
import { BootCardFrame } from "./BootCardFrame.js";

function stateColor(state: string): string {
  switch (state) {
    case "injected":
    case "running":
      return Theme.status.warn;
    case "pending_confirmation":
    case "interrupted":
      return Theme.text.accent;
    case "failed":
      return Theme.status.err;
    default:
      return Theme.text.secondary;
  }
}

export const PendingTasksCard: React.FC<{ item: PendingTasksCardItem }> = ({
  item,
}) => {
  return (
    <BootCardFrame>
      <Box marginBottom={1}>
        <Text color={Theme.text.accent} bold>
          {Icons.thinking} {t("boot.pending.title")}
        </Text>
      </Box>
      {item.tasks.length === 0 ? (
        <Box>
          <Text color={Theme.text.secondary}>{t("boot.pending.empty")}</Text>
        </Box>
      ) : (
        item.tasks.map((row) => (
          // Glyph fixed-width + state fixed-width + task_id flexible
          // + fault_type fills remaining space. task_id is the most
          // valuable column for /replay / blade-ai recover invocations,
          // so we give it the bigger share via flexGrow=2.
          <Box key={row.taskId}>
            <Box minWidth={3} flexShrink={0}>
              <Text color={stateColor(row.state)}>•</Text>
            </Box>
            <Box minWidth={16} flexShrink={0}>
              <Text color={stateColor(row.state)}>{row.state}</Text>
            </Box>
            <Box flexGrow={2} flexBasis={0} paddingRight={2}>
              <Text color={Theme.text.primary} wrap="truncate-end">
                {row.taskId}
              </Text>
            </Box>
            {row.faultType ? (
              <Box flexGrow={1} flexBasis={0}>
                <Text color={Theme.text.secondary} wrap="truncate-end">
                  {row.faultType}
                </Text>
              </Box>
            ) : (
              <Box flexGrow={1} flexBasis={0} />
            )}
          </Box>
        ))
      )}
    </BootCardFrame>
  );
};
