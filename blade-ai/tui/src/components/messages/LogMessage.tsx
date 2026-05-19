/**
 * Slash-command output line. Renders simple markdown-style emphasis
 * (``**bold**``) inline; otherwise plain text with level-tinted color.
 *
 * Doesn't go through marked because /help and /tasks emit only one or
 * two emphasis spans per line — the cost of importing marked here
 * isn't worth it. We do a tiny inline parser instead.
 */

import { Box, Text } from "ink";
import type { LogItem } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";

function levelColor(level: LogItem["level"]): string | undefined {
  switch (level) {
    case "warn":
      return Theme.status.warn;
    case "ok":
      return Theme.status.ok;
    default:
      return Theme.text.primary;
  }
}

/**
 * Split text into runs alternating plain / bold for ``**…**`` markers.
 * ``\\*`` literal stars are preserved. No fancy emphasis nesting.
 */
function splitBold(text: string): Array<{ text: string; bold: boolean }> {
  const out: Array<{ text: string; bold: boolean }> = [];
  let i = 0;
  let buf = "";
  while (i < text.length) {
    if (text[i] === "\\" && text[i + 1] === "*") {
      buf += "*";
      i += 2;
      continue;
    }
    if (text[i] === "*" && text[i + 1] === "*") {
      // Find closing.
      const close = text.indexOf("**", i + 2);
      if (close < 0) {
        buf += "**";
        i += 2;
        continue;
      }
      if (buf) out.push({ text: buf, bold: false });
      out.push({ text: text.slice(i + 2, close), bold: true });
      buf = "";
      i = close + 2;
      continue;
    }
    buf += text[i];
    i += 1;
  }
  if (buf) out.push({ text: buf, bold: false });
  return out;
}

export const LogMessage: React.FC<{ item: LogItem }> = ({ item }) => {
  const color = levelColor(item.level);
  const lines = item.text.split("\n");
  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="column">
      {lines.map((line, i) => {
        const runs = splitBold(line);
        return (
          <Box key={`${item.id}-${i}`}>
            <Text color={color}>
              {runs.map((r, j) =>
                r.bold ? (
                  <Text key={j} bold>
                    {r.text}
                  </Text>
                ) : (
                  <Text key={j}>{r.text}</Text>
                ),
              )}
            </Text>
          </Box>
        );
      })}
    </Box>
  );
};
