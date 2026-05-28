/**
 * Result card — final outcome of a fault-injection turn.
 *
 * v3 redesign: aligns with the ConfirmMessage / ToolMessage chrome
 * grammar that landed earlier in this session —
 *
 *   · Single-line ``╭──╮`` round border (not the old ``┏━━━┓`` bold).
 *     Border colour stays status-coded (sage / warn / err) — unique to
 *     ResultCard so a user scanning history can spot the outcome from
 *     the frame alone without reading the chip.
 *   · ``[E#]`` locator → bracket chip with secondary brackets +
 *     gray.300 bold inner text (matches ToolMessage's ``[T#]``).
 *   · Status badge → bracket chip ``[✓ SUCCESS]`` + same-coloured bold
 *     title (matches ConfirmMessage's ``[✻ INTENT]`` / ``[⚠ EXECUTE]``).
 *   · ``SectionHeading`` (``── Outcome`` / ``── Effect verified`` /
 *     ``── Failure analysis``) replaces the old unlabelled SectionRule.
 *   · ``partial`` status now has its own ``── Recovery notes`` section
 *     using the existing cause/hint fields — explains *why* it was
 *     partial without piggy-backing on the failure_analysis style.
 *
 *   ╭───────────────────────────────────────────────────────╮
 *   │  [E1]  [✓ SUCCESS]  Injection succeeded  ·  task-xxx  │
 *   │                                                       │
 *   │  ── Outcome                                           │
 *   │    Fault       node-cpu-fullload                      │
 *   │    Blade UID   b02c7d1a745dcd54                       │
 *   │    Duration    13m14s                                 │
 *   │                                                       │
 *   │  ── Effect verified                                   │
 *   │    Summary     <verification line>                    │
 *   │                                                       │
 *   │  ── Recovery notes      (partial only)                │
 *   │    Why partial <reason>                               │
 *   │    Hint        <llm hint>                             │
 *   │                                                       │
 *   │  ── Failure analysis    (failed only)                 │
 *   │    Cause       <failure cause>                        │
 *   │    Hint        <llm hint>                             │
 *   ╰───────────────────────────────────────────────────────╯
 *     /replay task-xxx instant — for full timeline
 */

import { Box, Text } from "ink";
import { memo } from "react";
import { useBootCardWidth } from "../boot/BootCardFrame.js";
import { PostmortemSection } from "./PostmortemSection.js";
import { t } from "../../i18n/index.js";
import type { ResultItem } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";

const ROW_LABEL_WIDTH = 14;

interface StatusVisuals {
  color: string;
  glyph: string;
  chipLabel: string;
  title: string;
}

function statusVisuals(status: ResultItem["status"]): StatusVisuals {
  switch (status) {
    case "success":
      return {
        color: Theme.status.ok,
        glyph: Icons.success,
        chipLabel: t("result.chip.success"),
        title: t("result.status.success"),
      };
    case "partial":
      return {
        color: Theme.status.warn,
        glyph: Icons.warning,
        chipLabel: t("result.chip.partial"),
        title: t("result.status.partial"),
      };
    case "failed":
      return {
        color: Theme.status.err,
        glyph: Icons.fail,
        chipLabel: t("result.chip.failed"),
        title: t("result.status.failed"),
      };
    default:
      return {
        color: Theme.gray[500],
        glyph: Icons.bullet,
        chipLabel: t("result.chip.unknown"),
        title: t("result.status.unknown"),
      };
  }
}

/** Title row — [E#] · [glyph CHIP] · bold same-coloured title · taskId.
 *  Mirrors ConfirmMessage's TitleChip so the result + confirm cards
 *  share one title vocabulary. ``glyphColor`` (== status color) is
 *  reused for both the chip inner text and the title text, making the
 *  top line read as a single "this is the result" unit. */
const TitleRow: React.FC<{
  locator?: string;
  statusColor: string;
  glyph: string;
  chipLabel: string;
  title: string;
  taskId?: string;
}> = ({ locator, statusColor, glyph, chipLabel, title, taskId }) => (
  <Box>
    {locator && (
      <Box marginRight={1}>
        <Text>
          <Text color={Theme.text.secondary}>[</Text>
          <Text color={Theme.gray[300]} bold>
            {locator}
          </Text>
          <Text color={Theme.text.secondary}>]</Text>
        </Text>
      </Box>
    )}
    <Text color={Theme.text.secondary}>[</Text>
    <Text color={statusColor} bold>{`${glyph} ${chipLabel}`}</Text>
    <Text color={Theme.text.secondary}>]</Text>
    <Text color={statusColor} bold>{`  ${title}`}</Text>
    {taskId && <Text color={Theme.gray[500]}>{`  ·  ${taskId}`}</Text>}
  </Box>
);

/** Section heading — dim ``── label`` divider. Same shape as
 *  ConfirmMessage's SectionHeading so cards in different families
 *  share one sectioning grammar. */
const SectionHeading: React.FC<{ label: string }> = ({ label }) => (
  <Box marginTop={1}>
    <Text color={Theme.gray[500]}>{"── "}</Text>
    <Text color={Theme.gray[500]} bold>
      {label}
    </Text>
  </Box>
);

/** Field row — label gutter + value. Skips when value is empty so a
 *  recover-only result doesn't show "(none)" placeholders. */
const Field: React.FC<{
  label: string;
  value: string;
  valueColor?: string;
  labelColor?: string;
  /** ``true`` to wrap multi-line values (cause text / verification
   *  summary). ``false`` truncates single-line metadata. */
  wrap?: boolean;
  /** ``true`` to render the value bold (used for headline metadata
   *  like fault type / duration so the eye picks them out). */
  valueBold?: boolean;
}> = ({
  label,
  value,
  valueColor = Theme.text.primary,
  labelColor = Theme.text.secondary,
  wrap = false,
  valueBold = false,
}) => {
  if (!value) return null;
  return (
    <Box>
      <Box minWidth={ROW_LABEL_WIDTH}>
        <Text color={labelColor}>{label}</Text>
      </Box>
      <Box flexGrow={1}>
        <Text
          color={valueColor}
          bold={valueBold}
          wrap={wrap ? "wrap" : "truncate-end"}
        >
          {value}
        </Text>
      </Box>
    </Box>
  );
};

const ResultCardInternal: React.FC<{ item: ResultItem }> = ({ item }) => {
  const { color, glyph, chipLabel, title } = statusVisuals(item.status);
  const width = useBootCardWidth();

  const hasEffect = !!item.summary;
  const hasCause = !!item.cause;
  const isFailed = item.status === "failed";
  const isPartial = item.status === "partial";
  // Pre-format the live target spec for display. Empty when neither
  // namespace nor names are populated — the Target field is then
  // skipped (Field component already returns null on empty value).
  let targetStr = "";
  if (item.target) {
    const ns = item.target.namespace || "";
    const names = item.target.names ?? [];
    const namesStr = names.join(", ");
    if (ns && namesStr) targetStr = `${ns} · ${namesStr}`;
    else if (ns) targetStr = ns;
    else if (namesStr) targetStr = namesStr;
  }
  const hasSideEffects = !!item.sideEffects && item.sideEffects.length > 0;
  const hasSideEffectsSummary = !!item.sideEffectsSummary;
  const showSideEffects = item.status === "success" || item.status === "partial";
  const replanCount = item.replanCount ?? 0;
  // Guard: skip the Outcome section entirely when no metadata field
  // is populated. Without this guard, a malformed payload (no
  // faultType / bladeUid / duration / target / replanCount) would
  // render a dangling "── Outcome" heading followed by an empty
  // Box — visual noise with no information. Mirrors the same guard
  // pattern landed in ExecutionConfirmCard (hasPlanContent).
  const hasOutcome = Boolean(
    item.faultType ||
      item.bladeUid ||
      item.duration ||
      targetStr ||
      replanCount > 0,
  );

  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="column">
      <Box
        flexDirection="column"
        // ``round`` (``╭───╮``) — matches the ConfirmMessage frame
        // shape. Edge colour follows the result status so "succeeded
        // / partial / failed" reads from the frame alone, even at a
        // glance. (The old ``bold`` ``┏━━━┓`` was visually too heavy
        // for a card the user lands on every turn.)
        borderStyle="round"
        borderColor={color}
        paddingX={2}
        paddingY={0}
        width={width}
      >
        <TitleRow
          locator={item.locator}
          statusColor={color}
          glyph={glyph}
          chipLabel={chipLabel}
          title={title}
          taskId={item.taskId}
        />

        {/* Outcome — fault / target / uid / duration / attempts
         *  (whole section skipped when all empty) */}
        {hasOutcome && (
          <>
            <SectionHeading label={t("result.section.outcome")} />
            <Box marginTop={1} flexDirection="column">
              <Field
                label={t("result.label.fault")}
                value={item.faultType}
                valueBold
              />
              {/* Target — surfaced so the user can verify "we did
               *  hit the intended pod/node" without re-scrolling. */}
              <Field
                label={t("result.label.target")}
                value={targetStr}
                valueBold
              />
              <Field label={t("result.label.uid")} value={item.bladeUid} />
              <Field
                label={t("result.label.duration")}
                value={item.duration}
                valueBold
              />
              {/* Attempts — only when LLM auto-replanned. Coloured
               *  warn so the user sees "this is not a clean first-try"
               *  even though the overall status is success. */}
              {replanCount > 0 && (
                <Field
                  label={t("result.label.attempts")}
                  value={t("result.attempts.label", { n: replanCount })}
                  valueColor={Theme.status.warn}
                  labelColor={Theme.status.warn}
                />
              )}
            </Box>
          </>
        )}

        {/* Effect — verification summary (skipped when absent) */}
        {hasEffect && (
          <>
            <SectionHeading label={t("result.section.effect")} />
            <Box marginTop={1}>
              <Field
                label={t("result.label.summary")}
                value={item.summary}
                wrap
              />
            </Box>
          </>
        )}

        {/* Side effects — always shown on successful injection.
         *  Body uses backend-assembled summary (covers all detector
         *  categories dynamically); falls back to item list when
         *  specific effects were detected. */}
        {showSideEffects && (
          <>
            <SectionHeading label={t("result.section.side_effects")} />
            <Box marginTop={1} flexDirection="column">
              {hasSideEffects ? (
                item.sideEffects!.map((effect, i) => (
                  <Box key={i}>
                    <Box minWidth={ROW_LABEL_WIDTH}>
                      <Text color={Theme.text.secondary}>
                        {i === 0 ? t("result.label.side_effect_item") : ""}
                      </Text>
                    </Box>
                    <Box flexGrow={1}>
                      <Text color={Theme.gray[300]} wrap="wrap">
                        {effect}
                      </Text>
                    </Box>
                  </Box>
                ))
              ) : (
                <Field
                  label={t("result.label.side_effect_item")}
                  value={item.sideEffectsSummary || t("result.side_effects_none")}
                  wrap
                />
              )}
            </Box>
          </>
        )}

        {/* Recovery notes — partial only. Reuses cause/hint payload
         *  but presents under the partial-specific section so the
         *  user understands "this is why it only partly worked",
         *  not "this is why it failed". */}
        {isPartial && hasCause && (
          <>
            <SectionHeading label={t("result.section.recovery_notes")} />
            <Box marginTop={1} flexDirection="column">
              <Field
                label={t("result.label.why_partial")}
                value={item.cause ?? ""}
                labelColor={Theme.status.warn}
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

        {/* Failure analysis — failed only. Cause label + value are
         *  red to draw the eye; hint is dim (advisory). */}
        {isFailed && hasCause && (
          <>
            <SectionHeading label={t("result.section.failure_analysis")} />
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

      {/* T6 — PostmortemSection sits BETWEEN the main result box and
       *  the Replay hint. Rendered only when the server attached a
       *  postmortem payload (success / qualifying failure with LLM
       *  generation enabled). Absent when disabled / timed out — the
       *  card collapses cleanly to its original shape. */}
      {item.postmortem && (
        <PostmortemSection
          markdown={item.postmortem.markdown}
          path={item.postmortem.path}
        />
      )}

      {/* Replay hint sits OUTSIDE the box (matches the original
       *  card's affordance — a slash command suggestion the user
       *  can copy). */}
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

// React.memo: ResultCard has a heavy layout (multiple SectionHeadings,
// Field rows, status visuals lookup). ResultItem is committed once at
// turn end and never mutated; shallow compare on the ``item`` ref
// short-circuits re-renders during downstream streaming activity.
export const ResultCard = memo(ResultCardInternal);
