/**
 * Result card — final outcome of a fault-injection turn.
 *
 * Forge × Operator redesign: shares the double-border + forge.iron
 * colour family with Confirm Layer 2 (the "hard check" tier) so the
 * user feels "my decision in the gate card flowed straight into
 * this result frame". Same colour, same border weight, no chrome
 * difference — only the status banner at the top tells whether the
 * inject succeeded, partially recovered, or failed.
 *
 *   ╔═════ ▓ ✓ INJECTED ▓ · t-abc123 ════════════════════════╗
 *   ║                                                        ║
 *   ║   fault type   node-cpu-fullload                       ║
 *   ║   blade uid    6158c2f6c326e943                        ║
 *   ║   duration     9m31s                                   ║
 *   ║                                                        ║
 *   ║   ──────────────                                       ║
 *   ║   effect       <verification line>                     ║   (only when present)
 *   ║                                                        ║
 *   ║   ──────────────                                       ║
 *   ║   cause        <failure cause>                         ║   (only on failure)
 *   ║   hint         <llm hint>                              ║
 *   ╚════════════════════════════════════════════════════════╝
 *     /replay t-abc123 instant — for full timeline
 *
 * Status is carried by an inverse-coloured chip (▓ glyph + label ▓)
 * rather than a coloured-text label, matching the operator-vocabulary
 * chip family used by Confirm-resolved markers (▓ ● ARMED ▓), Tool
 * name chips, and Safety badges. One chip language across the whole
 * decision/result family keeps scanning the timeline quick.
 */

import { Box, Text } from "ink";
import { useBootCardWidth } from "../boot/BootCardFrame.js";
import { t } from "../../i18n/index.js";
import type { ResultItem } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";

const ROW_LABEL_WIDTH = 14;

function statusVisuals(status: ResultItem["status"]) {
  switch (status) {
    case "success":
      return {
        color: Theme.status.ok,
        glyph: Icons.success,
        label: t("result.status.success").toUpperCase(),
      };
    case "partial":
      return {
        color: Theme.status.warn,
        glyph: Icons.warning,
        label: t("result.status.partial").toUpperCase(),
      };
    case "failed":
      return {
        color: Theme.status.err,
        glyph: Icons.fail,
        label: t("result.status.failed").toUpperCase(),
      };
    default:
      return {
        color: Theme.gray[500],
        glyph: Icons.bullet,
        label: t("result.status.unknown").toUpperCase(),
      };
  }
}

/** Field row — same shape as ConfirmMessage's Field. Skips when value
 *  is empty so a recover-only result doesn't show "(none)" placeholders.
 */
const Field: React.FC<{
  label: string;
  value: string;
  valueColor?: string;
  labelColor?: string;
  /** ``true`` to wrap multi-line values (cause text / verification
   *  summary). ``false`` truncates single-line metadata. */
  wrap?: boolean;
}> = ({
  label,
  value,
  valueColor = Theme.text.primary,
  labelColor = Theme.text.secondary,
  wrap = false,
}) => {
  if (!value) return null;
  return (
    <Box>
      <Box minWidth={ROW_LABEL_WIDTH}>
        <Text color={labelColor}>{label}</Text>
      </Box>
      <Box flexGrow={1}>
        <Text color={valueColor} wrap={wrap ? "wrap" : "truncate-end"}>
          {value}
        </Text>
      </Box>
    </Box>
  );
};

/** Thin horizontal rule that separates body sections. Matches
 *  ConfirmMessage's SectionRule visually so the two card styles
 *  share the same internal sectioning grammar. */
const SectionRule: React.FC = () => (
  <Box
    marginTop={1}
    borderStyle="single"
    borderTop={true}
    borderBottom={false}
    borderLeft={false}
    borderRight={false}
    borderColor={Theme.text.secondary}
  />
);

export const ResultCard: React.FC<{ item: ResultItem }> = ({ item }) => {
  const { color, glyph, label } = statusVisuals(item.status);
  const width = useBootCardWidth();

  const hasEffect = !!item.summary;
  const hasFailure = !!item.cause;

  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="column">
      <Box
        flexDirection="column"
        // ``bold`` (``┏━━━┓``) — Result-only border style; every
        // other card family in the TUI uses round / double. Edge
        // colour follows the result status so the user can read
        // "succeeded / partial / failed" from the frame alone.
        borderStyle="bold"
        borderColor={color}
        paddingX={2}
        paddingY={1}
        width={width}
      >
        {/* Title row — [E#] · ▓ status chip ▓ · taskId
         *
         * Status now lives in an inverse-coloured chip (matches the
         * Confirm-resolved chip family and ToolNameChip). Locator
         * ``[E#]`` stays as the dim prefix the user copies for
         * /show / /copy / /rerun lookups. */}
        <Box>
          {item.locator && (
            <Box marginRight={1}>
              <Text color={Theme.gray[500]}>[{item.locator}]</Text>
            </Box>
          )}
          <Text inverse color={color} bold>
            {` ${glyph} ${label} `}
          </Text>
          {item.taskId && (
            <Text color={Theme.gray[500]}>{`  ·  ${item.taskId}`}</Text>
          )}
        </Box>

        {/* Metadata block — fault type / uid / duration */}
        <Box marginTop={1} flexDirection="column">
          <Field label={t("result.label.task")} value={item.faultType} />
          <Field label={t("result.label.uid")} value={item.bladeUid} />
          <Field label={t("result.label.duration")} value={item.duration} />
        </Box>

        {/* Effect / verification section — divided so the eye knows
         *  this is "what happened to the cluster" vs "what we shipped". */}
        {hasEffect && (
          <>
            <SectionRule />
            <Box marginTop={1}>
              <Field
                label={t("result.label.effect")}
                value={item.summary}
                wrap
              />
            </Box>
          </>
        )}

        {/* Failure diagnosis — only on failed results. Cause is red,
         *  hint is dim (advisory). */}
        {hasFailure && (
          <>
            <SectionRule />
            <Box marginTop={1} flexDirection="column">
              <Field
                label={t("result.label.cause")}
                value={item.cause ?? ""}
                labelColor={Theme.status.err}
                valueColor={Theme.status.err}
                wrap
              />
              <Field
                label={t("result.label.hint")}
                value={item.hint ?? ""}
                wrap
              />
            </Box>
          </>
        )}
      </Box>

      {/* Replay hint sits OUTSIDE the box (matches the original card's
       *  affordance — a slash command suggestion the user can copy). */}
      {item.taskId && (
        <Box paddingLeft={2}>
          <Text color={Theme.text.secondary}>
            {t("result.show_for_timeline", { id: item.taskId })}
          </Text>
        </Box>
      )}
    </Box>
  );
};
