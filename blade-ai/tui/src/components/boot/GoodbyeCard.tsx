/**
 * Farewell card — printed once on TUI exit with a session summary.
 *
 * Mirrors the Python TUI's ``tui/renderers/goodbye.py`` so a user
 * switching between the two front-ends sees a consistent send-off.
 * Visual style reuses the existing ``BootCardFrame`` (same lavender
 * border, same width policy as Welcome / EnvCheck / PendingTasks) so
 * the card slots in as the natural visual bookend to the boot panel.
 *
 *   ╭──────────────────────────────────────────────────────────╮
 *   │  ✻ 再见                                                  │
 *   │                                                          │
 *   │  感谢使用 blade-ai，期待下次再见                          │
 *   │                                                          │
 *   │  会话概览                                                 │
 *   │      会话 ID          sess_abc123                         │
 *   │      持续时间          15m 23s                            │
 *   │      集群 / 命名空间    k8s / default                      │
 *   │                                                          │
 *   │  活动统计                                                 │
 *   │      消息交互          12 次                              │
 *   │      故障注入          3 次  (✓ 2  ✗ 1)                  │
 *   │      故障恢复          1 次                               │
 *   ╰──────────────────────────────────────────────────────────╯
 *
 * Layout choices:
 *   - 标题在框内首行（不嵌入边框），跟 BootDoctorCard 一致 — keeps the
 *     component dirt-simple, no manual top-border rendering.
 *   - K/V table uses fixed ``minWidth`` on the key column so the values
 *     visually align like a 2-column table without a real Table widget.
 *   - Counts use the i18n template ``{n} 次`` (zh) / ``{n}`` (en) so a
 *     future locale swap doesn't need code changes.
 */

import { Box, Text } from "ink";
import stringWidth from "string-width";
import type { AppState } from "../../state/types.js";
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";
import { t } from "../../i18n/index.js";
import { BootCardFrame } from "./BootCardFrame.js";

export interface GoodbyeCardProps {
  /**
   * Latest reducer snapshot. Read once at exit by ``printGoodbye`` —
   * the component itself never re-renders (it's mounted into a
   * one-shot Ink instance that unmounts immediately after the first
   * frame commits).
   */
  state: AppState;
}

// Visual width (in terminal cells, CJK = 2) of the value-column left
// edge. Must exceed the widest label across BOTH locales:
//   en: "Cluster / namespace" → 19 cells
//   zh: "集群 / 命名空间"     → 17 cells (7 CJK × 2 + 3 ASCII)
// 22 leaves a 3-cell gap after the English worst-case and a 5-cell gap
// after the Chinese worst-case. Yoga can't measure CJK width — it
// counts code points — so we pre-pad the label string ourselves
// instead of relying on Box ``minWidth`` (the original Python TUI uses
// Rich, which IS CJK-aware, hence "looks right out of the box" over
// there).
const KEY_LABEL_COLS = 22;

/** Format seconds → "Xh Ym Zs" / "Ym Zs" / "Zs", matching Python's
 * ``_fmt_duration`` in goodbye.py exactly. */
function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

/** Right-pad a label with spaces so its visual width fills
 * ``KEY_LABEL_COLS`` cells. CJK-aware via ``string-width``. */
function padLabel(s: string): string {
  const w = stringWidth(s);
  if (w >= KEY_LABEL_COLS) return s + " ";
  return s + " ".repeat(KEY_LABEL_COLS - w);
}

const KvRow: React.FC<{ label: string; children: React.ReactNode }> = ({
  label,
  children,
}) => (
  <Box>
    <Text color={Theme.text.secondary}>{"      " + padLabel(label)}</Text>
    <Box>{children}</Box>
  </Box>
);

const SectionHeader: React.FC<{ children: string }> = ({ children }) => (
  <Box marginTop={1}>
    <Text color={Theme.text.accent} bold>
      {"   "}
      {children}
    </Text>
  </Box>
);

/** Render the "故障注入 N 次 (✓ N ✗ N)" cell. Splits the breakdown
 * into coloured chunks so green-ok / red-fail pop without dragging
 * chalk into the component. Mirrors goodbye.py's _injection_value. */
const InjectionValue: React.FC<{ state: AppState }> = ({ state }) => (
  <Box>
    <Text bold>{t("goodbye.value.count", { n: state.injectionCount })}</Text>
    {state.injectionCount > 0 && (
      <>
        <Text color={Theme.text.secondary}>{"  ("}</Text>
        <Text color={Theme.status.ok}>{`✓ ${state.injectionSuccess}`}</Text>
        {state.injectionFail > 0 && (
          <>
            <Text color={Theme.text.secondary}>{"  ·  "}</Text>
            <Text color={Theme.status.err}>{`✗ ${state.injectionFail}`}</Text>
          </>
        )}
        <Text color={Theme.text.secondary}>{")"}</Text>
      </>
    )}
  </Box>
);

export const GoodbyeCard: React.FC<GoodbyeCardProps> = ({ state }) => {
  const durationSec = Math.max(
    0,
    Math.floor((Date.now() - state.sessionStartTs) / 1000),
  );
  const clusterName = state.session.cluster?.trim() || t("goodbye.cluster_auto");
  const namespace = state.session.namespace?.trim() || "default";
  const clusterNs = `${clusterName} / ${namespace}`;

  return (
    <BootCardFrame paddingY={1}>
      <Box marginBottom={1}>
        <Text color={Theme.text.accent} bold>
          {Icons.thinking} {t("goodbye.title")}
        </Text>
      </Box>

      <Box>
        <Text color={Theme.text.primary}>
          {"   "}
          {t("goodbye.farewell")}
        </Text>
      </Box>

      <SectionHeader>{t("goodbye.section.overview")}</SectionHeader>
      <KvRow label={t("goodbye.label.session_id")}>
        <Text color={Theme.text.secondary}>{state.session.id || "—"}</Text>
      </KvRow>
      <KvRow label={t("goodbye.label.duration")}>
        <Text color={Theme.text.primary}>{formatDuration(durationSec)}</Text>
      </KvRow>
      <KvRow label={t("goodbye.label.cluster_ns")}>
        <Text color={Theme.text.primary}>{clusterNs}</Text>
      </KvRow>

      <SectionHeader>{t("goodbye.section.activity")}</SectionHeader>
      <KvRow label={t("goodbye.label.messages")}>
        <Text color={Theme.text.primary}>
          {t("goodbye.value.count", { n: state.messageCount })}
        </Text>
      </KvRow>
      <KvRow label={t("goodbye.label.injections")}>
        <InjectionValue state={state} />
      </KvRow>
      <KvRow label={t("goodbye.label.recoveries")}>
        <Text color={Theme.text.primary}>
          {t("goodbye.value.count", { n: state.recoveryCount })}
        </Text>
      </KvRow>
    </BootCardFrame>
  );
};
