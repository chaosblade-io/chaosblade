/**
 * Live indicator for an in-flight manual ``/compact``.
 *
 *   ⠋ 正在压缩当前会话上下文… (3s · esc 取消)
 *
 * Mounted by ``Composer`` only when ``state.currentManualCompact`` is
 * non-null. Distinct from ``MemoryCompactingIndicator``:
 *
 *   - ``MemoryCompactingIndicator`` is server-event-driven, mounts
 *     ONLY while the LLM summariser is actually running (silent for
 *     noop / strip-only paths).
 *   - ``ManualCompactIndicator`` is client-driven, mounts from the
 *     moment the /compact slash handler opens ``streamCompactSession``
 *     to the moment it returns. The user pressed /compact; they get
 *     continuous visual feedback regardless of which internal path
 *     the hook ends up taking.
 *
 * Both share the same screen slot — the Composer renders this one
 * with priority when set so two spinners can never appear at once.
 *
 * Lifecycle:
 *   - reducer sets ``currentManualCompact`` on COMPACT_MANUAL_STARTED
 *     (dispatched by the /compact slash handler before its for-await)
 *   - this component mounts via Composer's conditional render
 *   - 1Hz timer ticks the elapsed-seconds tail
 *   - reducer clears ``currentManualCompact`` on COMPACT_MANUAL_DONE
 *     (dispatched in the handler's ``finally``) → component unmounts;
 *     the slash handler's ``formatCompactResult`` line lands in
 *     scrollback right after.
 */

import { Box, Text } from "ink";
import { useEffect, useState } from "react";
import { t } from "../i18n/index.js";
import { useAppSelector } from "../state/store.js";
import { Theme } from "../theme/colors.js";
import { ThinkingSpinner } from "../theme/spinners.js";
import { Spinner } from "./shared/Spinner.js";

export const ManualCompactIndicator: React.FC = () => {
  // Narrow selector — only re-render when the slot itself transitions
  // (null ↔ object). The reducer always replaces the slot wholesale
  // on STARTED so object-identity equality is fine.
  const compact = useAppSelector((s) => s.currentManualCompact);

  // Local 1Hz tick for the elapsed-seconds tail. Mirror of
  // MemoryCompactingIndicator's pattern — gated on the slot being
  // non-null so an unmounted/finished compact stops the timer cleanly.
  const [elapsedSec, setElapsedSec] = useState(0);
  useEffect(() => {
    if (!compact) {
      setElapsedSec(0);
      return;
    }
    const tick = () => {
      const ms = Date.now() - compact.startedAt;
      setElapsedSec(Math.max(0, Math.floor(ms / 1000)));
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [compact]);

  if (!compact) return null;

  const elapsed = formatElapsed(elapsedSec);
  const meta = t("compact.indicator_meta", { elapsed });

  return (
    <Box paddingLeft={2}>
      <Box marginRight={1}>
        <Spinner type={ThinkingSpinner.type} color={Theme.status.warn} />
      </Box>
      <Text color={Theme.text.accent} wrap="truncate-end">
        {t("compact.indicator_label")}
      </Text>
      <Text color={Theme.text.secondary}> {meta}</Text>
    </Box>
  );
};

/** Mirror of MemoryCompactingIndicator.formatElapsed — kept inline
 *  so the two indicators can drift independently if one ever grows
 *  a richer time format (e.g. ms precision for sub-second compacts). */
function formatElapsed(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m${s.toString().padStart(2, "0")}s`;
}
