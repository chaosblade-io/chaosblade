/**
 * Error line — red ✗ leader + message + optional next-step hints.
 *
 * When the message matches a known keyword pattern, we surface a
 * compact "next-step" block beneath the error so the user has a
 * clear recovery path instead of a dead-end stack trace. Mirrors the
 * legacy Python TUI's actionable-error UX.
 */

import { Box, Text } from "ink";
import { t } from "../../i18n/index.js";
import type { ErrorItem } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";
import { suggestionsForError } from "../../utils/errorHints.js";

export const ErrorMessage: React.FC<{ item: ErrorItem }> = ({ item }) => {
  const hint = suggestionsForError(item.text);
  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="column">
      <Box>
        <Text color={Theme.status.err} bold>
          {Icons.fail}
          {hint ? ` ${hint.label}: ` : " "}
        </Text>
        <Box flexGrow={1}>
          <Text color={Theme.status.err} wrap="wrap">
            {item.text}
          </Text>
        </Box>
      </Box>
      {item.taskId && (
        <Box paddingLeft={2}>
          <Text color={Theme.text.secondary}>(task: {item.taskId})</Text>
        </Box>
      )}
      {hint && (
        <Box paddingLeft={2} flexDirection="column" marginTop={1}>
          <Text color={Theme.text.secondary}>{t("error.next_label")}</Text>
          {hint.suggestions.map((s, i) => (
            <Box key={i} paddingLeft={2}>
              <Text color={Theme.text.secondary}>{Icons.bullet} {s}</Text>
            </Box>
          ))}
        </Box>
      )}
    </Box>
  );
};
