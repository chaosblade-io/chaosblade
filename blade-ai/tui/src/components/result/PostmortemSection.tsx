/**
 * PostmortemSection — renders the LLM-generated postmortem markdown
 * underneath ResultCard's main box (between the box and the Replay
 * hint). Sits in its OWN rounded box so the visual frame says
 * "this is a separate artefact from the live experiment result".
 *
 * Uses ``markdownRenderer.parseMarkdown`` to handle the constrained
 * markdown surface the postmortem prompt produces. Anything the
 * renderer can't categorise falls through as plain text — no
 * exceptions, no rendering loss.
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
    // Level-2 headings get the section divider treatment (matching
    // ResultCard's SectionHeading); level-3 headings are inline bold.
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

interface PostmortemSectionProps {
  markdown: string;
  /** On-disk file path for the share-friendly footer line. */
  path: string;
}

const PostmortemSectionInternal: React.FC<PostmortemSectionProps> = ({
  markdown,
  path,
}) => {
  const width = useBootCardWidth();
  const blocks = React.useMemo(() => parseMarkdown(markdown), [markdown]);

  return (
    // No ``paddingLeft`` here — ResultCard's outer Box already applies
    // ``paddingLeft={2}``, and PostmortemSection is rendered as its
    // sibling. Adding another 2-col indent here pushed the postmortem
    // frame 2 columns to the right of the result frame, breaking the
    // "sibling cards" visual alignment.
    <Box marginTop={1} flexDirection="column">
      <Box
        flexDirection="column"
        borderStyle="round"
        borderColor={Theme.gray[500]}
        paddingX={2}
        paddingY={0}
        width={width}
      >
        {/* Title row — matches ResultCard's titleRow shape so the two
            cards read as siblings, not parent/child. */}
        <Box>
          <Text color={Theme.status.info}>{"📝 "}</Text>
          <Text bold>{t("postmortem.title")}</Text>
        </Box>

        {/* Body — block tokens from parsed markdown. */}
        <Box flexDirection="column" marginTop={1}>
          {blocks.map((block, i) => (
            <RenderBlock key={i} block={block} index={i} />
          ))}
        </Box>

        {/* File path footer — lets the user know where to find the
            shareable markdown without re-running ``blade-ai
            postmortem <id>``. */}
        <Box marginTop={1}>
          <Text color={Theme.gray[500]}>
            {t("postmortem.saved_at", { path })}
          </Text>
        </Box>
      </Box>
    </Box>
  );
};

// React.memo: markdown is committed once at result time and never
// mutates; shallow compare short-circuits re-renders.
export const PostmortemSection = memo(PostmortemSectionInternal);
