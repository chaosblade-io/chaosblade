/**
 * System / hint message — quiet italic gray, no leading glyph.
 * These are notifications, not first-class content.
 */

import { Box, Text } from "ink";
import type { SystemItem } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";

export const SystemMessage: React.FC<{ item: SystemItem }> = ({ item }) => (
  <Box paddingLeft={2} marginTop={1}>
    <Text color={Theme.text.secondary} italic wrap="wrap">
      {item.text}
    </Text>
  </Box>
);
