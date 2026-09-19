/**
 * Live indicator for the fault-window hold (``turn_hold_fault_window``).
 *
 *   ⠋ 故障窗口进行中 · 剩余 4m30s (ctrl+r 立即恢复 · esc 退出)
 *
 * Mounted by ``Composer`` only while ``state.faultWindow`` is non-null
 * (the server's hold loop emitted ``fault_window`` enter). Takes the
 * spinner slot from the regular ``LoadingIndicator`` for the duration
 * (single-spinner mutex — same family as ``MemoryCompactingIndicator``).
 *
 * Continuation, not replacement: same spinner glyph, same frame rate,
 * only the label changes — the visual transition from the verify
 * wrap-up into the hold is zero-jump. The user asked for exactly this:
 * the spinner must keep ROTATING through the hold (same icon as the
 * thinking-stream spinner), never freeze into a static countdown.
 *
 * Countdown discipline: the local 1Hz ticker interpolates off
 * ``deadlineAt`` (client clock) so the number moves every second; each
 * server ``tick`` (~30s) re-bases the deadline, bounding drift to one
 * interval. Server truth wins, client interpolation fills the gaps.
 *
 * Lifecycle: reducer sets the slot on FAULT_WINDOW_ENTERED, re-bases it
 * on FAULT_WINDOW_TICKED, clears it on FAULT_WINDOW_EXITED / TURN_DONE
 * / TURN_STARTED (defensive) — then the recover graph streams down the
 * same SSE turn and the regular LoadingIndicator re-own the slot.
 */

import { Box, Text } from "ink";
import { useEffect, useState } from "react";
import { t } from "@blade-ai/core";
import { useAppSelector } from "@blade-ai/core";
import { isNarrow, useTerminalSize } from "../hooks/useTerminalSize.js";
import { Theme } from "../theme/colors.js";
import { ThinkingSpinner } from "../theme/spinners.js";
import { Spinner } from "./shared/Spinner.js";

export const FaultWindowIndicator: React.FC = () => {
  // Narrow selector — the slot object is replaced on every server tick
  // (deadline re-base), so the component re-renders ~2/min from ticks
  // plus 1Hz from the local countdown timer. Token events during the
  // hold don't exist (the pipeline is done), so no further narrowing
  // is needed.
  const faultWindow = useAppSelector((s) => s.faultWindow);
  const { columns } = useTerminalSize();
  const narrow = isNarrow(columns);

  // Local 1Hz countdown. ``remainingSec`` state mirrors
  // ``deadlineAt - now``; the effect re-derives it when the slot (and
  // its deadline) is replaced by a server tick.
  const [remainingSec, setRemainingSec] = useState(0);
  useEffect(() => {
    if (!faultWindow) {
      setRemainingSec(0);
      return;
    }
    const tick = () => {
      const ms = faultWindow.deadlineAt - Date.now();
      setRemainingSec(Math.max(0, Math.floor(ms / 1000)));
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [faultWindow]);

  if (!faultWindow) return null;

  const meta = `(${t("fault_window.keys_hint")})`;
  const remaining = t("fault_window.remaining", {
    duration: formatRemaining(remainingSec),
  });

  return (
    <Box paddingLeft={2} flexDirection="column">
      <Box
        flexDirection={narrow ? "column" : "row"}
        alignItems={narrow ? "flex-start" : "center"}
      >
        <Box>
          <Box marginRight={1}>
            <Spinner type={ThinkingSpinner.type} color={Theme.text.primary} />
          </Box>
          <Text color={Theme.text.accent} wrap="truncate-end">
            {t("fault_window.indicator")}
          </Text>
          {!narrow && (
            <Text color={Theme.text.secondary}>
              {" "}
              · {remaining} {meta}
            </Text>
          )}
        </Box>
      </Box>
      {narrow && (
        <Box>
          <Text color={Theme.text.secondary}>
            {remaining} {meta}
          </Text>
        </Box>
      )}
    </Box>
  );
};

/** Countdown format — mirror of LoadingIndicator.formatElapsed so the
 *  hold's "4m30s" reads identically to the spinner's elapsed "4m30s". */
function formatRemaining(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m${s.toString().padStart(2, "0")}s`;
}
