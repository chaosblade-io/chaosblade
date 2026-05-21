/**
 * System / hint message — quiet italic gray, no leading glyph.
 * These are notifications, not first-class content.
 */

import { Box, Text } from "ink";
import { memo } from "react";
import type { SystemItem } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";

const SystemMessageInternal: React.FC<{ item: SystemItem }> = ({ item }) => (
  <Box paddingLeft={2} marginTop={1}>
    <Text color={Theme.text.secondary} italic wrap="wrap">
      {item.text}
    </Text>
  </Box>
);

// React.memo: trivial render but called in the streaming hot loop.
// Shallow compare on ``item`` ref short-circuits the re-render.
export const SystemMessage = memo(SystemMessageInternal);
