/**
 * Boot-time progress row — rendered between the static welcome card
 * and the input prompt while ``BootOrchestrator`` is fetching
 * ``/preflight`` and ``/api/v1/metric``. Drives a single label that
 * the orchestrator updates as it advances through phases.
 *
 * Visual: indented Spinner glyph in accent colour + the label in
 * primary text. No border / card framing — boot-time waits are short
 * (typically <2s) and a full card here would compete with the welcome
 * card immediately above. Same vibe as the streaming
 * ``LoadingIndicator`` used during agent turns, just with a static
 * label instead of a phrase cycler.
 */

import { Box, Text } from "ink";
import { Spinner } from "../shared/Spinner.js";
import { Theme } from "../../theme/colors.js";

export interface BootProgressProps {
  text: string;
}

export const BootProgress: React.FC<BootProgressProps> = ({ text }) => (
  <Box paddingLeft={2} marginTop={1}>
    <Text color={Theme.text.accent}>
      <Spinner color={Theme.text.accent} />
      {"  "}
    </Text>
    <Text color={Theme.text.primary}>{text}</Text>
  </Box>
);
