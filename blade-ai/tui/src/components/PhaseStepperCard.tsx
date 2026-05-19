/**
 * PhaseStepperCard — five-row todo list for inject pipeline turns.
 *
 * Lives in ``state.currentPhaseStepper`` (a dedicated slot, not in
 * pending) for the duration of the turn so its mid-turn mutation
 * doesn't block the leading-stable flush in TOKEN_APPENDED. Composer
 * pins the strip directly above the InputPrompt; ``commitPending``
 * appends the finalised snapshot to history at TURN_DONE / TURN_ABORTED.
 * Inspired by Qwen Code's StickyTodoList (LLM-driven via todo_write),
 * but ours is graph-driven via ``dispatch_phase_started`` events.
 *
 * The five rows mirror the actual graph node sequence in
 * ``src/chaos_agent/agent/graph.py`` — see ``mapNodeToStep`` in
 * ``state/reducer.ts`` for how server ``(node, phase)`` pairs are
 * translated to these five buckets:
 *
 *   intent      → intent_clarification (incl. Layer-1 confirm wait)
 *   agent_loop  → agent_loop (planning, phase1 tools)
 *   safety      → safety_check / confirmation_gate (Layer-2 confirm)
 *   execute     → baseline_capture / execute_loop / direct_execute
 *   verify      → verifier_loop
 *
 * Visual (success path, reaching TURN_DONE):
 *
 *   ╭ ⚡ 故障注入待办 ──────────────────────────────╮
 *   │ 1. ✔  意图识别                                 │
 *   │ 2. ✔  计划编排                                 │
 *   │ 3. ✔  安全检查                                 │
 *   │ 4. ⚡  故障注入                                 │
 *   │ 5. ○  注入验证                                 │
 *   ╰─────────────────────────────────────────────────╯
 *
 * Aborted mid-inject (TURN_ABORTED) — the active row turns red and
 * later rows stay grey, so the strip honestly records "we got this
 * far, then broke" instead of misleading "all ✓":
 *
 *   ╭ ⚡ 故障注入待办 ──────────────────────────────╮
 *   │ 1. ✔  意图识别                                 │
 *   │ 2. ✗  计划编排                                 │
 *   │ 3. ○  安全检查                                 │
 *   │ 4. ○  故障注入                                 │
 *   │ 5. ○  注入验证                                 │
 *   ╰─────────────────────────────────────────────────╯
 *
 * Status palette:
 *   completed   ✔  Theme.status.ok       (green, bold)
 *   in_progress ⚡  Theme.text.accent     (amber, bold)
 *   failed      ✗  Theme.status.err      (red, bold)
 *   pending     ○  Theme.text.secondary  (dim grey)
 *
 * The ⚡ glyph is the ``blade-ai`` family signal (chaos-engineering
 * sparks). Picked over ``◐`` so the active row is unmistakable in
 * the terminal even at small font sizes.
 */

import { Box, Text } from "ink";
import { useBootCardWidth } from "./boot/BootCardFrame.js";
import { t } from "../i18n/index.js";
import type { PhaseStep, PhaseStepperItem } from "../state/types.js";
import { Theme } from "../theme/colors.js";

const STATUS_GLYPH: Record<PhaseStep["status"], string> = {
  completed: "✔",
  in_progress: "⚡",
  failed: "✗",
  pending: "○",
};

function statusColor(status: PhaseStep["status"]): string {
  if (status === "completed") return Theme.status.ok;
  if (status === "in_progress") return Theme.text.accent;
  if (status === "failed") return Theme.status.err;
  return Theme.text.secondary;
}

const StepRow: React.FC<{ index: number; step: PhaseStep }> = ({
  index,
  step,
}) => {
  const color = statusColor(step.status);
  const labelKey = `phase.label.${step.phase}` as const;
  const label = t(labelKey) as string;
  const isActive = step.status === "in_progress";
  const isCompleted = step.status === "completed";
  const isFailed = step.status === "failed";
  // ``failed`` rows demand the same visual weight as ``in_progress``
  // — they're the row the user needs to read first when scanning a
  // post-mortem ResultCard. Glyph stays bold for completed too so
  // the ✓ prefix lines up vertically with the active / failed glyph.
  const glyphBold = isActive || isCompleted || isFailed;
  const labelBold = isActive || isFailed;
  return (
    <Box flexDirection="row" height={1}>
      <Box width={4}>
        <Text color={Theme.text.secondary}>{index + 1}.</Text>
      </Box>
      <Box width={3}>
        <Text color={color} bold={glyphBold}>
          {STATUS_GLYPH[step.status]}
        </Text>
      </Box>
      <Box flexGrow={1}>
        <Text color={color} bold={labelBold} wrap="truncate-end">
          {label}
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
        borderColor={Theme.border.default}
        paddingX={1}
        width={width}
      >
        <Box>
          <Text color={Theme.text.accent} bold>
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
