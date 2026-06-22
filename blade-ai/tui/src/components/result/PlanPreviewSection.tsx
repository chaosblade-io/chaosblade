/**
 * PlanPreviewSection — renders a markdown plan preview (e.g. injection plan
 * or alternatives) in its own rounded box, visually sibling to ResultCard.
 *
 * Unlike PostmortemSection:
 *   - No emoji prefix
 *   - No file-path footer
 *   - Supports an optional `title` prop (defaults to i18n key)
 */
import { Box, Text } from "ink";
import React, { memo } from "react";

import { t } from "../../i18n/index.js";
import { Theme } from "../../theme/colors.js";
import { useBootCardWidth } from "../boot/BootCardFrame.js";
import {
  type Block,
  type InlineSpan,
  parseMarkdown,
} from "./markdownRenderer.js";

/** Inline span renderer — text / bold / `code`. */
const RenderSpan: React.FC<{ span: InlineSpan }> = ({ span }) => {
  if (span.kind === "bold") return <Text bold>{span.value}</Text>;
  if (span.kind === "code")
    return <Text color={Theme.status.info}>{span.value}</Text>;
  return <Text>{span.value}</Text>;
};

/** Spans line — concatenates inline spans into a wrapping row. */
const SpansLine: React.FC<{ spans: InlineSpan[] }> = ({ spans }) => (
  <Text wrap="wrap">
    {spans.map((s, i) => (
      <RenderSpan key={i} span={s} />
    ))}
  </Text>
);

/** Single block renderer — heading / list / paragraph / blank. */
const RenderBlock: React.FC<{ block: Block; index: number }> = ({
  block,
  index,
}) => {
  if (block.kind === "blank") {
    return <Box height={1} />;
  }
  if (block.kind === "heading") {
    if (block.level === 2) {
      return (
        <Box marginTop={index === 0 ? 0 : 1}>
          <Text color={Theme.gray[500]}>{"── "}</Text>
          <Text color={Theme.gray[500]} bold>
            <SpansLine spans={block.spans} />
          </Text>
        </Box>
      );
    }
    return (
      <Box marginTop={1}>
        <Text bold>
          <SpansLine spans={block.spans} />
        </Text>
      </Box>
    );
  }
  if (block.kind === "list") {
    return (
      <Box flexDirection="column" marginTop={0}>
        {block.items.map((item, i) => (
          <Box key={i} flexDirection="row">
            <Text color={Theme.gray[500]}>{"  • "}</Text>
            <Box flexGrow={1}>
              <SpansLine spans={item} />
            </Box>
          </Box>
        ))}
      </Box>
    );
  }
  // paragraph
  return (
    <Box marginTop={0}>
      <SpansLine spans={block.spans} />
    </Box>
  );
};

interface PlanPreviewSectionProps {
  markdown: string;
  title?: string;
}

const PlanPreviewSectionInternal: React.FC<PlanPreviewSectionProps> = ({
  markdown,
  title,
}) => {
  const width = useBootCardWidth();
  const blocks = React.useMemo(() => parseMarkdown(markdown), [markdown]);

  return (
    <Box marginTop={1} flexDirection="column">
      <Box
        flexDirection="column"
        borderStyle="round"
        borderColor={Theme.gray[500]}
        paddingX={2}
        paddingY={0}
        width={width}
      >
        <Box>
          <Text bold>{title ?? t("plan_preview.title")}</Text>
        </Box>
        <Box flexDirection="column" marginTop={1}>
          {blocks.map((block, i) => (
            <RenderBlock key={i} block={block} index={i} />
          ))}
        </Box>
      </Box>
    </Box>
  );
};

export const PlanPreviewSection = memo(PlanPreviewSectionInternal);
