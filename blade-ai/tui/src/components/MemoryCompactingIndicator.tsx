/**
 * Live indicator for an in-flight memory compaction (Phase 4).
 *
 *   ⠋ 记忆压缩中 (12000 tokens · 6s)
 *
 * Mounted by ``Composer`` only when ``state.currentCompaction`` is
 * non-null. Replaces the regular ``LoadingIndicator`` for the
 * duration of the compaction call (single-spinner UX — see the
 * mutex in ``useLoadingIndicator``).
 *
 * Why a dedicated component instead of stuffing this into
 * LoadingIndicator: the spinner row is the most-watched UI element
 * during a turn and conflating "thinking" with "compacting" would
 * make the header label semantics fuzzy. Keeping them separate also
 * lets each component subscribe to a narrow slice of state with a
 * tight selector (here, just ``s.currentCompaction``) so token-flood
 * during streaming doesn't trigger needless re-renders here.
 *
 * Lifecycle:
 *   - reducer sets ``currentCompaction`` on MEMORY_COMPACTION_STARTED
 *   - this component mounts via Composer's conditional render
 *   - 1Hz timer ticks the elapsed seconds tail
 *   - reducer clears ``currentCompaction`` on COMPLETED / FAILED /
 *     TURN_STARTED (defensive) → component unmounts; the finalised
 *     ``MemoryCompactionItem`` lands in pending and shows in
 *     scrollback at TURN_DONE.
 */

import { Box, Text } from "ink";
import { useEffect, useState } from "react";
import { t } from "../i18n/index.js";
import { useAppSelector } from "../state/store.js";
import { Theme } from "../theme/colors.js";
import { ThinkingSpinner } from "../theme/spinners.js";
import { Spinner } from "./shared/Spinner.js";

export const MemoryCompactingIndicator: React.FC = () => {
  // Narrow selector — only re-render when the compaction slot itself
  // changes, NOT on every token / phase event. Object equality check
  // is fine because reducer always replaces the slot with a fresh
  // object on STARTED and ``null`` on COMPLETED/FAILED.
  const compaction = useAppSelector((s) => s.currentCompaction);

  // Local 1Hz tick for the elapsed-seconds tail. Mirrors
  // LoadingIndicator's pattern. Gated on the slot being non-null so
  // an unmounted/finished compaction stops the timer cleanly.
  const [elapsedSec, setElapsedSec] = useState(0);
  useEffect(() => {
    if (!compaction) {
      setElapsedSec(0);
      return;
    }
    const tick = () => {
      const ms = Date.now() - compaction.startedAt;
      setElapsedSec(Math.max(0, Math.floor(ms / 1000)));
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [compaction]);

  if (!compaction) return null;

  const elapsed = formatElapsed(elapsedSec);
  // Wire token counter only when it's a positive number — server-
  // emitted ``tokens_before`` is zero on extremely-rare edge cases
  // (the compactor decided to act on a phantom message list); show
  // just elapsed time then to avoid "0 tokens" misleading text.
  const meta =
    compaction.tokensBefore > 0
      ? t("compaction.indicator_meta", {
          tokens: compaction.tokensBefore,
          elapsed,
        })
      : `(${elapsed})`;

  return (
    <Box paddingLeft={2}>
      <Box marginRight={1}>
        <Spinner type={ThinkingSpinner.type} color={Theme.status.warn} />
      </Box>
      <Text color={Theme.text.accent} wrap="truncate-end">
        {t("compaction.indicator_label")}
      </Text>
      <Text color={Theme.text.secondary}> {meta}</Text>
    </Box>
  );
};

/** Mirror of LoadingIndicator.formatElapsed — kept inline rather
 *  than imported so the two indicators can drift independently if
 *  one ever grows a richer time format. */
function formatElapsed(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m${s.toString().padStart(2, "0")}s`;
}
