/**
 * Finalised memory-compaction history row (Phase 4).
 *
 * Visual (success):
 *   ✓ 已压缩 12000 → 4500 tokens · 节省 7500 (62%) · 用时 6.3s
 *
 * Visual (failure):
 *   ✗ 记忆压缩失败：<原因> · 用时 1.2s
 *
 * Materialised from ``MEMORY_COMPACTION_COMPLETED`` /
 * ``MEMORY_COMPACTION_FAILED`` reducer cases into pending → committed
 * to history at TURN_DONE alongside thinking + tool_group + (optional)
 * turn_usage rows. Sits in scrollback as an honest record of when the
 * compactor ran and how much it bought.
 *
 * Sister of ``TurnUsageMessage`` — same visual weight (single dim
 * row, no border), so a turn that ran a compaction shows two
 * "metadata" rows at its tail: compaction first (chronologically
 * earlier in the turn), usage second.
 */

import { Box, Text } from "ink";
import { t } from "../../i18n/index.js";
import { Theme } from "../../theme/colors.js";
import type { MemoryCompactionItem } from "../../state/types.js";

/** Compact token formatter — same convention as TurnUsageMessage so
 *  the two related rows render with consistent number widths.
 *  ≥ 1000 → ``X.Yk``; < 1000 → exact. */
function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(1)}k`;
}

/** Wall-clock formatter — keeps short durations precise (``1.2s``),
 *  drops the decimal once we're past 10s where the precision is
 *  noise. Capped at minutes since compaction longer than that means
 *  something's wrong and the user should care about the digit
 *  anyway. */
function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const sec = ms / 1000;
  if (sec < 10) return `${sec.toFixed(1)}s`;
  if (sec < 60) return `${Math.round(sec)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return `${m}m${s.toString().padStart(2, "0")}s`;
}

export const MemoryCompactionMessage: React.FC<{ item: MemoryCompactionItem }> = ({
  item,
}) => {
  if (!item.succeeded) {
    // Failure path. Surface the server's error message so the user
    // can act on it (e.g. an LLM rate-limit string is actionable;
    // a generic "failed" isn't). Keep the duration so the user
    // knows whether the failure was instant (config error) or after
    // a long stall (LLM timeout).
    const duration = formatDuration(item.durationMs);
    const reason = item.errorMessage || t("compaction.failure_unknown");
    return (
      <Box paddingLeft={2} marginTop={1}>
        <Text color={Theme.status.err}>
          {t("compaction.failure_line", { reason, duration })}
        </Text>
      </Box>
    );
  }

  // Success path. Two computed bits the user reads at a glance:
  // ``saved`` (delta) and ``percent`` (ratio). Both keep a positive
  // floor so a degenerate (after >= before) compactor doesn't print
  // negative noise — clamp to 0 / 0% which the renderer simply
  // doesn't show.
  const saved = Math.max(0, item.tokensBefore - item.tokensAfter);
  const percent =
    item.tokensBefore > 0
      ? Math.floor((saved * 100) / item.tokensBefore)
      : 0;
  const before = formatTokens(item.tokensBefore);
  const after = formatTokens(item.tokensAfter);
  const savedFmt = formatTokens(saved);
  const duration = formatDuration(item.durationMs);

  return (
    <Box paddingLeft={2} marginTop={1}>
      <Text color={Theme.text.secondary}>
        {t("compaction.success_line", {
          before,
          after,
          saved: savedFmt,
          percent,
          duration,
          messages: item.messagesCompacted,
        })}
      </Text>
    </Box>
  );
};
