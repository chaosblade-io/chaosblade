/**
 * Top-of-session greeting. Three lines, indented two columns. No box,
 * no logo, no tips list — those would land in /help. Matches the
 * "Welcome to Claude Code" rhythm: short, oriented, then out of the way.
 *
 * Lines:
 *   ✻ blade-ai  v0.2 (TS preview)
 *     /help · /doctor · /mode
 *     <auth source> · model:<name> · ns:<ns>
 */

import { Box, Text } from "ink";
import { t } from "../i18n/index.js";
import type { SessionInfo } from "../state/types.js";
import { Theme } from "../theme/colors.js";
import { Icons } from "../theme/icons.js";

interface Props {
  version: string;
  session: SessionInfo;
  serverUrl: string;
}

export const Header: React.FC<Props> = ({ version, session, serverUrl }) => {
  const ns = session.namespace || "default";
  const cluster = session.cluster?.trim() || t("header.no_cluster");
  const model = session.modelName?.trim() || t("header.default_agent");
  return (
    <Box flexDirection="column" marginTop={1}>
      <Box paddingLeft={2}>
        <Text color={Theme.text.accent} bold>
          {Icons.thinking} blade-ai
        </Text>
        <Text color={Theme.text.secondary}> v{version} {t("header.brand_tag")}</Text>
      </Box>
      <Box paddingLeft={4} marginTop={1}>
        <Text color={Theme.text.secondary}>{t("header.commands_hint")}</Text>
      </Box>
      <Box paddingLeft={4}>
        <Text color={Theme.text.secondary}>
          {cluster} · ns:{ns} · {model}
        </Text>
      </Box>
      <Box paddingLeft={4}>
        <Text color={Theme.text.secondary}>
          {t("header.connected_to", { url: serverUrl })}
        </Text>
      </Box>
    </Box>
  );
};
