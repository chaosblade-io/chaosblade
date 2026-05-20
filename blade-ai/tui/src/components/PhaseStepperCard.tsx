/**
 * PhaseStepperCard — five-row todo list for inject pipeline turns.
 *
 * Lives in ``state.currentPhaseStepper`` (a dedicated slot, not in
 * pending) for the duration of the turn so its mid-turn mutation
 * doesn't block the leading-stable flush in TOKEN_APPENDED. Composer
 * pins the strip directly above the InputPrompt; ``commitPending``
 * appends the finalised snapshot to history at TURN_DONE / TURN_ABORTED.
 *
 * Visual (success path):
 *
 *   ╭ ⚡ 故障注入待办 ──────────────────────────────╮
 *   │ 1. ●  意图识别                                 │
 *   │ 2. ●  计划编排                                 │
 *   │ 3. ●  安全检查                                 │
 *   │ 4. ◐  故障注入                                 │
 *   │ 5. ◯  注入验证                                 │
 *   ╰─────────────────────────────────────────────────╯
 *
 * Aborted mid-inject (TURN_ABORTED) — active row's lamp blows out
 * (◉ red); later rows stay grey, so the strip honestly records
 * "we got this far, then broke" instead of misleading "all done":
 *
 *   ╭ ⚡ 故障注入待办 ──────────────────────────────╮
 *   │ 1. ●  意图识别                                 │
 *   │ 2. ◉  计划编排                                 │
 *   │ 3. ◯  安全检查                                 │
 *   │ 4. ◯  故障注入                                 │
 *   │ 5. ◯  注入验证                                 │
 *   ╰─────────────────────────────────────────────────╯
 *
 * Indicator lamps (Forge × Operator HUD language — replaces the
 * earlier ⚡/✔/○/✗ mix so all status indicators across the TUI
 * share the same "fuel-lamp / blown-out / off" vocabulary):
 *   ●  completed   Theme.status.ok       (green, lit)
 *   ◐  in_progress Theme.forge.fire      (orange, half-lit)
 *   ◉  failed      Theme.status.err      (red, blown out)
 *   ◯  pending     Theme.gray[500]       (off)
 *
 * The ⚡ glyph in the card title remains the blade-ai family signal
 * — kept as the brand mark so the strip is instantly recognisable
 * as "the inject HUD".
 */

import { Box, Text } from "ink";
import { useBootCardWidth } from "./boot/BootCardFrame.js";
import { t } from "../i18n/index.js";
import type { PhaseStep, PhaseStepperItem } from "../state/types.js";
import { Theme } from "../theme/colors.js";

const LAMP_GLYPH: Record<PhaseStep["status"], string> = {
  completed: "●",
  in_progress: "◐",
  failed: "◉",
  pending: "◯",
};

function lampColor(status: PhaseStep["status"]): string {
  if (status === "completed") return Theme.status.ok;
  if (status === "in_progress") return Theme.forge.fire;
  if (status === "failed") return Theme.status.err;
  return Theme.gray[500];
}

function labelColor(status: PhaseStep["status"]): string {
  if (status === "in_progress") return Theme.forge.fire;
  if (status === "failed") return Theme.status.err;
  if (status === "completed") return Theme.gray[300];
  return Theme.gray[500];
}

const StepRow: React.FC<{ index: number; step: PhaseStep }> = ({
  index,
  step,
}) => {
  const lamp = lampColor(step.status);
  const label = labelColor(step.status);
  const isActive = step.status === "in_progress";
  const isFailed = step.status === "failed";
  const labelBold = isActive || isFailed;
  const labelKey = `phase.label.${step.phase}` as const;
  const labelText = t(labelKey) as string;
  return (
    <Box flexDirection="row" height={1}>
      <Box width={4}>
        <Text color={Theme.gray[500]}>{index + 1}.</Text>
      </Box>
      <Box width={3}>
        <Text color={lamp} bold={step.status !== "pending"}>
          {LAMP_GLYPH[step.status]}
        </Text>
      </Box>
      <Box flexGrow={1}>
        <Text color={label} bold={labelBold} wrap="truncate-end">
          {labelText}
        </Text>
      </Box>
    </Box>
  );
};

export const PhaseStepperCard: React.FC<{ item: PhaseStepperItem }> = ({
  item,
}) => {
  const width = useBootCardWidth();
  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="column">
      <Box
        flexDirection="column"
        borderStyle="round"
        // Match the InputPrompt fence colour (Theme.text.secondary
        // = gray.500) so the stepper card visually merges with the
        // composer chrome below it instead of pulling forge.fire
        // attention away from the live tool / agent output.
        borderColor={Theme.text.secondary}
        paddingX={1}
        width={width}
      >
        <Box>
          <Text color={Theme.forge.fire} bold>
            ⚡ {t("phase.stepper.title")}
          </Text>
        </Box>
        <Box flexDirection="column" marginTop={0}>
          {item.steps.map((step, idx) => (
            <StepRow key={step.phase} index={idx} step={step} />
          ))}
        </Box>
      </Box>
    </Box>
  );
};
