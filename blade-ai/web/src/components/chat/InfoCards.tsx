/**
 * Info-card family — the web renderers for the card kinds produced by
 * core's shared slash commands (/help, /memory show, /doctor, /session,
 * /experiments, /model list) and the boot-time seeds (welcome /
 * boot doctor / pending tasks). Mirrors the TUI's card family one for
 * one (WelcomeCard / BootDoctorCard / PendingTasksCard /
 * RuntimeDoctorCard / MemoryCard / HelpCard / SessionCard /
 * ExperimentsCard / ModelCard).
 *
 * One frame grammar for all nine cards:
 *
 *   ┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐
 *     ✻ Title  · count tail · YYYY-MM-DD HH:MM:SS
 *     glyph  name        body
 *     glyph  name        body
 *   └ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘
 *
 * The frame stays neutral (forge-border) — these cards inform, they
 * don't warn (the confirm gate owns the amber treatment). Status is
 * always glyph/dot + text, never a pill (Forge discipline).
 *
 * Platform deltas vs the TUI, all deliberate:
 *   - ``welcome.tip.mode`` (Shift+Tab) is dropped — the web has no
 *     permission-mode hotkey.
 *   - The runtime doctor's ``terminal background`` row is dropped —
 *     browsers have no OSC 11 concept.
 *   - The client-version row reads "client version" (uiText), not
 *     "tui version" — the field carries THIS host's build.
 *   - ``prettyPath`` can't shorten $HOME (no env in the browser), so
 *     overlong paths collapse to ``.../<basename>`` only.
 */
import type { ReactNode } from "react";
import type {
  BootDoctorCardItem,
  BootDoctorCheck,
  ExperimentsCardItem,
  HelpCardItem,
  MemoryCardItem,
  ModelCardItem,
  PendingTasksCardItem,
  RuntimeDoctorCardItem,
  SessionCardItem,
  WelcomeCardItem,
} from "@blade-ai/core";
import { t } from "@blade-ai/core";
import { ui } from "../../lib/uiText";
import { formatDateTime, formatTimeOfDay } from "../../lib/format";

// ── shared frame ────────────────────────────────────────────────────

/** Card frame: icon + title + optional dim tails (count, timestamp). */
function InfoCardFrame({
  icon,
  title,
  tails,
  children,
}: {
  icon: string;
  title: string;
  tails?: Array<string | null | false>;
  children: ReactNode;
}) {
  const tail = (tails ?? []).filter((s): s is string => Boolean(s));
  return (
    <div className="rounded-card border border-forge-border bg-forge-card px-4 py-3 text-xs shadow-card">
      <div className="mb-2 flex flex-wrap items-baseline gap-x-2 text-sm">
        <span className="font-medium text-forge-accent">{icon}</span>
        <span className="font-medium text-forge-text">{title}</span>
        {tail.map((s, i) => (
          <span key={i} className="text-forge-text-faint">
            · {s}
          </span>
        ))}
      </div>
      {children}
    </div>
  );
}

type RowTone = "ok" | "warn" | "err" | "info";

const ROW_GLYPH: Record<RowTone, string> = {
  ok: "✓",
  warn: "⚠",
  err: "✗",
  info: "•",
};

const ROW_GLYPH_CLASS: Record<RowTone, string> = {
  ok: "text-success",
  warn: "text-warning",
  err: "text-danger",
  info: "text-forge-text-faint",
};

/** One diagnostic row: coloured status glyph + name column + body.
 *  ``bodyFaint`` mirrors the TUI CheckList convention — passing rows
 *  fade their message to grey so the eye locks onto problem rows. */
function DoctorRow({
  tone,
  name,
  body,
  bodyFaint,
}: {
  tone: RowTone;
  name: string;
  body: ReactNode;
  bodyFaint?: boolean;
}) {
  return (
    <div className="flex items-baseline gap-2">
      <span className={`w-4 shrink-0 ${ROW_GLYPH_CLASS[tone]}`}>
        {name ? ROW_GLYPH[tone] : " "}
      </span>
      <span className="w-40 shrink-0 text-forge-text">{name}</span>
      <span
        className={`min-w-0 break-words ${
          bodyFaint ? "text-forge-text-faint" : "text-forge-text-secondary"
        }`}
      >
        {body}
      </span>
    </div>
  );
}

/** Suggested-fixes block shared by both doctor cards — one dim line
 *  per failed row that carries an actionable hint. Keyed by index
 *  (matching the TUI's RuntimeDoctorCard): the boot card passes an
 *  empty name for every fix (its CheckList shows no name column), so
 *  a name key would collide the moment two checks fail together. */
function FixesBlock({ fixes }: { fixes: Array<{ name: string; fix: string }> }) {
  if (fixes.length === 0) return null;
  return (
    <div className="mt-2 flex flex-col gap-0.5">
      <div className="font-medium text-forge-accent">
        {t("boot.doctor.fixes_header")}
      </div>
      {fixes.map((f, i) => (
        <div key={i} className="flex items-baseline gap-2 pl-3">
          {f.name ? (
            <span className="w-40 shrink-0 text-forge-text-faint">{f.name}</span>
          ) : null}
          <span className="min-w-0 break-words text-forge-text-secondary">
            {f.fix}
          </span>
        </div>
      ))}
    </div>
  );
}

/** Check rows shared by the boot doctor and the runtime doctor's
 *  preflight section (same ``BootDoctorCheck`` payload). */
function checkRowTone(c: BootDoctorCheck): RowTone {
  if (c.passed) return "ok";
  return c.severity === "warning" ? "warn" : "err";
}

// ── welcome_card (boot seed) ────────────────────────────────────────

// Half-block pixel-art logo for ``BLADE AI`` (same two lines as the
// TUI — monospace web fonts render the half-block glyphs identically).
const LOGO_LINES = [
  "█▀▄ █   ▄▀█ █▀▄ █▀▀  ▄▀█ █",
  "█▀▄ █▄▄ █▀█ █▄▀ ██▄  █▀█ █",
];

/** Browser-side ``prettyPath``: no HOME env to collapse, so overlong
 *  paths shrink to ``.../<basename>`` (same fallback as the TUI). */
function prettyPath(p: string, maxLen = 40): string {
  if (!p) return "(default)";
  if (p.length <= maxLen) return p;
  const base = p.split("/").pop() ?? p;
  return ".../" + base;
}

export function WelcomeCardView({ item }: { item: WelcomeCardItem }) {
  const modeGlyph = item.permissionMode === "auto" ? "⚡" : "✗";
  const modeLabel =
    item.permissionMode === "auto"
      ? t("welcome.mode.auto")
      : t("welcome.mode.confirm");
  // The TUI shows five tips; the Shift+Tab permission-mode tip is
  // dropped here — the web has no such hotkey, and a boot card must
  // never teach a gesture that doesn't exist.
  const tips = [
    t("welcome.tip.describe"),
    t("welcome.tip.help"),
    t("welcome.tip.doctor"),
    t("welcome.tip.retry"),
  ];
  return (
    <InfoCardFrame icon="✻" title="Blade-ai" tails={[`v${item.version}`]}>
      <div className="grid gap-4 sm:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <div className="flex flex-col items-center gap-2 text-center">
          <span className="font-medium text-forge-accent">
            {t("welcome.welcome_back")}
          </span>
          <pre className="font-mono text-[10px] leading-tight text-forge-accent">
            {LOGO_LINES.join("\n")}
          </pre>
          <span className="font-medium text-forge-text">
            {item.modelName || "(unknown model)"}
          </span>
          <span className="text-forge-text-secondary">
            {t("welcome.mode_label")}:{" "}
            <span
              className={
                item.permissionMode === "auto"
                  ? "font-medium text-warning"
                  : "font-medium text-forge-accent"
              }
            >
              {modeGlyph} {modeLabel}
            </span>
          </span>
        </div>
        <div className="flex flex-col gap-1">
          <span className="font-medium text-forge-accent">
            {t("welcome.tips_header")}
          </span>
          {tips.map((tip) => (
            <span key={tip} className="text-forge-text">
              <span className="text-forge-text-faint">• </span>
              {tip}
            </span>
          ))}
          <span className="mt-1 font-medium text-forge-accent">
            {t("welcome.runtime_header")}
          </span>
          <span className="text-forge-text">
            <span className="text-forge-text-faint">kubeconfig: </span>
            {prettyPath(item.kubeconfig)}
          </span>
          <span className="text-forge-text">
            <span className="text-forge-text-faint">namespace: </span>
            {item.namespace}
          </span>
          <span className="mt-1 text-forge-text-faint">
            {t("welcome.bottom_hint")}
          </span>
        </div>
      </div>
    </InfoCardFrame>
  );
}

// ── boot_doctor_card (boot seed) ────────────────────────────────────

export function BootDoctorCardView({ item }: { item: BootDoctorCardItem }) {
  if (item.unavailable) {
    return (
      <InfoCardFrame icon="✻" title={t("boot.doctor.title")}>
        <div className="text-warning">{t("boot.doctor.unavailable")}</div>
      </InfoCardFrame>
    );
  }
  const captured = item.capturedAt
    ? t("boot.doctor.captured_at", { time: formatTimeOfDay(item.capturedAt) })
    : "";
  const fixes = item.checks
    .filter((c) => !c.passed && c.fix)
    .map((c) => ({ name: "", fix: c.fix }));
  return (
    <InfoCardFrame
      icon="✻"
      title={t("boot.doctor.title")}
      tails={[
        t("boot.doctor.summary", {
          passed: item.passedCount,
          total: item.totalCount,
        }),
        captured,
      ]}
    >
      <div className="flex flex-col gap-0.5">
        {item.checks.map((c) => (
          <DoctorRow
            key={c.name}
            tone={checkRowTone(c)}
            name={c.name}
            body={c.passed ? c.message?.trim() || t("boot.doctor.passed_short") : c.message}
            bodyFaint={c.passed}
          />
        ))}
      </div>
      <FixesBlock fixes={fixes} />
    </InfoCardFrame>
  );
}

// ── pending_tasks_card (boot seed) ──────────────────────────────────

/**
 * Per-state visual (glyph + colour + weight), mirrored from the TUI's
 * STATE_VISUALS. Sorted by "how much should the user care?":
 * active IO / awaiting input wear accent+bold, fault-live and paused
 * states wear amber, settled states green, failures red — and
 * ``rejected`` is the ONLY state that stays grey (the safety check
 * stopped it before it ran; nothing to act on).
 */
const STATE_VISUALS: Record<string, { className: string; glyph: string; bold?: boolean }> = {
  injecting: { className: "text-forge-accent", glyph: "⠿", bold: true },
  recovering: { className: "text-forge-accent", glyph: "⠿", bold: true },
  pending_confirmation: { className: "text-forge-accent", glyph: "◐", bold: true },
  waiting_input: { className: "text-forge-accent", glyph: "◐", bold: true },
  injected: { className: "text-warning", glyph: "◉" },
  running: { className: "text-warning", glyph: "◉" },
  interrupted: { className: "text-warning", glyph: "◐" },
  partial_recovered: { className: "text-warning", glyph: "◐" },
  cancelled: { className: "text-warning", glyph: "⊘" },
  // Tier 2½ — fault effect UNCONFIRMED (round-17 C1): verification ran
  // but evidence is unavailable, so the fault is suspected LIVE
  // (fail-closed). Previously 'unverified' silently fell through to
  // STATE_FALLBACK (gray) — kept only as a conscious decision in
  // round-15; now given a dedicated warn visual to match the TUI map
  // (the two STATE_VISUALS maps are pinned key-identical by the TS
  // reconciliation test, so a missing key on one side fails CI).
  unverified: { className: "text-warning", glyph: "◌" },
  // Tier 2½ — newborn anchor (round-17 D4): zero lifecycle evidence,
  // pipeline not entered. Neutral-faint family, pinned key-identical
  // with the TUI map (TaskStateOverlay.PENDING).
  pending: { className: "text-forge-text-faint", glyph: "◌" },
  recovered: { className: "text-success", glyph: "●" },
  completed: { className: "text-success", glyph: "●" },
  failed: { className: "text-danger", glyph: "✗", bold: true },
  rejected: { className: "text-forge-text-faint", glyph: "◯" },
};

const STATE_FALLBACK = { className: "text-forge-text-faint", glyph: "•" };

export function PendingTasksCardView({ item }: { item: PendingTasksCardItem }) {
  return (
    <InfoCardFrame icon="✻" title={t("boot.pending.title")}>
      {item.tasks.length === 0 ? (
        <div className="text-forge-text-faint">{t("boot.pending.empty")}</div>
      ) : (
        <div className="flex flex-col gap-0.5">
          {item.tasks.map((row) => {
            const v = STATE_VISUALS[row.state] ?? STATE_FALLBACK;
            return (
              <div key={row.taskId} className="flex items-baseline gap-2">
                <span
                  className={`w-4 shrink-0 ${v.className} ${v.bold ? "font-medium" : ""}`}
                >
                  {v.glyph}
                </span>
                <span
                  className={`w-40 shrink-0 ${v.className} ${v.bold ? "font-medium" : ""}`}
                >
                  {row.state}
                </span>
                <span className="min-w-0 truncate font-mono text-forge-text">
                  {row.taskId}
                </span>
                {row.faultType ? (
                  <span className="min-w-0 truncate text-forge-text-faint">
                    {row.faultType}
                  </span>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </InfoCardFrame>
  );
}

// ── runtime_doctor_card (/doctor) ───────────────────────────────────

interface RuntimeRow {
  tone: RowTone;
  name: string;
  body: ReactNode;
  bodyFaint?: boolean;
  fix?: string;
}

/** Translate the snapshot into the unified row list — pure, mirroring
 *  the TUI's buildRows minus the terminal-background row (a browser
 *  has no OSC 11 concept). */
function buildRuntimeRows(item: RuntimeDoctorCardItem): RuntimeRow[] {
  const rows: RuntimeRow[] = [];

  rows.push(
    item.reachable
      ? { tone: "ok", name: t("doctor.server"), body: item.serverUrl }
      : {
          tone: "err",
          name: t("doctor.server"),
          body: (
            <>
              {item.serverUrl}
              <span className="text-danger">
                {"  "}
                {t("doctor.server_unreachable")}
              </span>
            </>
          ),
          fix: t("doctor.fix.server_unreachable"),
        },
  );

  rows.push(
    item.cluster
      ? { tone: "ok", name: t("doctor.cluster"), body: item.cluster }
      : {
          tone: "info",
          name: t("doctor.cluster"),
          body: t("doctor.cluster_none"),
          bodyFaint: true,
        },
  );

  // "client version", not "tui version" — the field carries THIS host.
  rows.push({
    tone: "info",
    name: ui().doctorClientVersion,
    body: item.tuiVersion,
  });

  rows.push(
    item.serverVersion
      ? { tone: "ok", name: t("doctor.server_version"), body: item.serverVersion }
      : {
          tone: "info",
          name: t("doctor.server_version"),
          body: "?",
          bodyFaint: true,
        },
  );

  const protoMismatch =
    item.serverProtocol !== null && item.serverProtocol !== item.tuiProtocol;
  rows.push(
    protoMismatch
      ? {
          tone: "warn",
          name: t("doctor.protocol"),
          body: (
            <>
              {item.tuiProtocol}
              <span className="text-warning">{" → "}{item.serverProtocol}</span>
            </>
          ),
          fix: t("doctor.fix.protocol_mismatch"),
        }
      : { tone: "ok", name: t("doctor.protocol"), body: item.tuiProtocol },
  );

  rows.push({ tone: "info", name: t("doctor.lang"), body: item.lang });
  rows.push({ tone: "info", name: t("doctor.mode"), body: item.mode });

  if (item.preflightUnavailable) {
    // When the server is unreachable the dedicated server row above
    // already explains the gap — don't double-report.
    if (item.reachable) {
      rows.push({
        tone: "warn",
        name: t("doctor.preflight"),
        body: t("boot.doctor.unavailable"),
        fix: t("doctor.fix.preflight_unavailable"),
      });
    }
  } else {
    for (const c of item.checks) {
      rows.push({
        tone: checkRowTone(c),
        name: c.name,
        body: c.passed
          ? c.message?.trim() || t("boot.doctor.passed_short")
          : c.message,
        bodyFaint: c.passed,
        fix: !c.passed && c.fix?.trim() ? c.fix.trim() : undefined,
      });
    }
  }

  return rows;
}

export function RuntimeDoctorCardView({ item }: { item: RuntimeDoctorCardItem }) {
  const rows = buildRuntimeRows(item);
  const fixes = rows
    .filter((r) => r.fix)
    .map((r) => ({ name: r.name, fix: r.fix as string }));
  return (
    <InfoCardFrame
      icon="ℹ"
      title={t("doctor.head")}
      tails={[formatDateTime(item.capturedAt)]}
    >
      <div className="flex flex-col gap-0.5">
        {rows.map((row, i) => (
          <DoctorRow
            key={i}
            tone={row.tone}
            name={row.name}
            body={row.body}
            bodyFaint={row.bodyFaint}
          />
        ))}
      </div>
      <FixesBlock fixes={fixes} />
    </InfoCardFrame>
  );
}

// ── memory_card (/memory show) ──────────────────────────────────────

export function MemoryCardView({ item }: { item: MemoryCardItem }) {
  const rows: RuntimeRow[] = [];

  rows.push({
    tone: "info",
    name: "Session",
    body: item.sessionId || t("memory.card.unknown"),
  });
  rows.push({
    tone: item.status === "active" ? "ok" : "info",
    name: "Status",
    body: item.status || "active",
  });
  rows.push({
    tone: "info",
    name: "Started",
    body: item.startedAt ? formatDateTime(item.startedAt) : "—",
  });
  rows.push(
    item.cluster
      ? { tone: "ok", name: "Cluster", body: item.cluster }
      : {
          tone: "info",
          name: "Cluster",
          body: t("memory.card.unset"),
          bodyFaint: true,
        },
  );
  rows.push({
    tone: item.namespace && item.namespace !== "default" ? "ok" : "info",
    name: "Namespace",
    body: item.namespace ? (
      item.namespace
    ) : (
      <span className="text-forge-text-faint">{t("memory.card.unset")}</span>
    ),
  });

  // Recent tasks: header row carries the "shown/total" count + the
  // latest id; older ids follow on their own dim rows (name column
  // intentionally blank — they're a continuation, not new fields).
  const shown = item.recentTasks.length;
  rows.push({
    tone: shown === 0 ? "info" : "ok",
    name: `Tasks (${shown}/${item.totalTasks})`,
    body:
      shown === 0 ? (
        <span className="text-forge-text-faint">
          {t("memory.card.no_recent_tasks")}
        </span>
      ) : (
        item.recentTasks[shown - 1]
      ),
    bodyFaint: shown === 0,
  });
  for (let i = shown - 2; i >= 0; i--) {
    rows.push({
      tone: "info",
      name: "",
      body: <span className="font-mono">{item.recentTasks[i]}</span>,
      bodyFaint: true,
    });
  }

  const msgCount = Number(item.stats["message_count"] ?? 0);
  const injCount = Number(item.stats["injection_count"] ?? 0);
  const injOk = Number(item.stats["injection_success"] ?? 0);
  const injFail = Number(item.stats["injection_fail"] ?? 0);
  const recCount = Number(item.stats["recovery_count"] ?? 0);

  rows.push({
    tone: msgCount > 0 ? "ok" : "info",
    name: "Messages",
    body: msgCount,
  });
  rows.push({
    tone: injFail > 0 ? "warn" : injCount > 0 ? "ok" : "info",
    name: "Injections",
    body: (
      <>
        {injCount}
        {injCount > 0 ? (
          <span className="text-forge-text-faint">
            {"  ("}
            <span className="text-success">{`✓ ${injOk}`}</span>
            {" / "}
            <span className="text-danger">{`✗ ${injFail}`}</span>
            {")"}
          </span>
        ) : null}
      </>
    ),
  });
  rows.push({
    tone: recCount > 0 ? "ok" : "info",
    name: "Recoveries",
    body: recCount,
  });
  rows.push({
    tone: "info",
    name: "Memory dir",
    body: <span className="font-mono">{item.memoryDir || "—"}</span>,
    bodyFaint: true,
  });

  return (
    <InfoCardFrame
      icon="◈"
      title={t("memory.card.title")}
      tails={[formatDateTime(item.capturedAt)]}
    >
      <div className="flex flex-col gap-0.5">
        {rows.map((row, i) => (
          <DoctorRow
            key={i}
            tone={row.tone}
            name={row.name}
            body={row.body}
            bodyFaint={row.bodyFaint}
          />
        ))}
      </div>
    </InfoCardFrame>
  );
}

// ── help_card (/help) ───────────────────────────────────────────────

export function HelpCardView({ item }: { item: HelpCardItem }) {
  return (
    <InfoCardFrame
      icon="⌘"
      title={t("help.card.title")}
      tails={[formatDateTime(item.capturedAt)]}
    >
      <div className="flex flex-col gap-2">
        {item.sections.map((section, sIdx) => (
          <div key={sIdx} className="flex flex-col gap-0.5">
            <div className="text-forge-text-faint">
              ── {section.heading} {"─".repeat(30)}
            </div>
            {section.rows.map((row, rIdx) => (
              <div
                key={rIdx}
                className={`flex items-baseline gap-2 ${
                  row.kind === "top" && rIdx > 0 ? "mt-1.5" : ""
                }`}
              >
                <span
                  className={`shrink-0 font-mono ${
                    row.kind === "sub" ? "pl-4 " : ""
                  }w-64 truncate text-forge-accent ${
                    row.kind === "top" ? "font-medium" : ""
                  }`}
                >
                  {row.name}
                </span>
                <span className="min-w-0 break-words text-forge-text-secondary">
                  {row.description}
                </span>
              </div>
            ))}
          </div>
        ))}
      </div>
      {item.tip ? (
        <div className="mt-2 text-forge-text-faint">{item.tip}</div>
      ) : null}
    </InfoCardFrame>
  );
}

// ── session_card (/session) ─────────────────────────────────────────

export function SessionCardView({ item }: { item: SessionCardItem }) {
  return (
    <InfoCardFrame
      icon="◉"
      title={t("session.card.title")}
      tails={[formatDateTime(item.capturedAt)]}
    >
      <div className="flex flex-col gap-0.5">
        {item.rows.map((row, i) => (
          <div key={i} className="flex items-baseline gap-2">
            <span className="w-4 shrink-0 text-forge-text-faint">•</span>
            <span className="w-40 shrink-0 text-forge-text">{row.label}</span>
            <span
              className={`min-w-0 break-words ${
                row.dim ? "text-forge-text-faint" : "text-forge-text-secondary"
              }`}
            >
              {row.value}
            </span>
          </div>
        ))}
      </div>
    </InfoCardFrame>
  );
}

// ── experiments_card (/experiments) ─────────────────────────────────

export function ExperimentsCardView({ item }: { item: ExperimentsCardItem }) {
  return (
    <InfoCardFrame
      icon="✦"
      title={t("experiments.card.title")}
      tails={[
        t("experiments.card.count", { n: item.totalCount }),
        formatDateTime(item.capturedAt),
      ]}
    >
      <div className="flex flex-col gap-0.5">
        {item.rows.map((row, i) => (
          <div key={i} className="flex items-baseline gap-2">
            <span className="w-4 shrink-0 text-forge-text-faint">•</span>
            {/* CSS truncates CJK correctly — the TUI's string-width
                pre-padding is a terminal-layout concern only. */}
            <span className="w-64 shrink-0 truncate text-forge-text">
              {row.useCaseName}
            </span>
            <span className="min-w-0 truncate text-forge-text-faint">
              {row.faultSymptom || t("experiments.card.symptom_empty")}
            </span>
          </div>
        ))}
      </div>
    </InfoCardFrame>
  );
}

// ── model_card (/model list) ────────────────────────────────────────

export function ModelCardView({ item }: { item: ModelCardItem }) {
  return (
    <InfoCardFrame
      icon="◆"
      title={t("model.card.title")}
      tails={[
        t("model.card.count", { n: item.totalCount }),
        formatDateTime(item.capturedAt),
      ]}
    >
      {item.apiBaseUrl ? (
        <div className="mb-1.5 flex items-baseline gap-2">
          <span className="w-40 shrink-0 text-forge-text-faint">
            {t("model.base_url_label")}
          </span>
          <span className="min-w-0 truncate font-mono text-forge-text-secondary">
            {item.apiBaseUrl}
          </span>
        </div>
      ) : null}
      <div className="flex flex-col gap-2">
        {item.sections.map((section, sIdx) => (
          <div key={sIdx} className="flex flex-col gap-0.5">
            <div className="text-forge-text-faint">
              ── {section.provider} {"─".repeat(30)}
            </div>
            {section.rows.map((row, rIdx) => (
              <div key={rIdx} className="flex items-baseline gap-2">
                {/* Active row: filled glyph + accent + bold — three
                    layers of contrast carry "this is the active row". */}
                <span
                  className={`w-4 shrink-0 ${
                    row.active
                      ? "font-medium text-forge-accent"
                      : "text-forge-text-faint"
                  }`}
                >
                  {row.active ? "●" : "○"}
                </span>
                <span
                  className={`w-64 shrink-0 truncate font-mono ${
                    row.active
                      ? "font-medium text-forge-accent"
                      : "text-forge-text"
                  }`}
                >
                  {row.id}
                </span>
                {row.note ? (
                  <span className="min-w-0 truncate text-forge-text-faint">
                    {row.note}
                  </span>
                ) : null}
              </div>
            ))}
          </div>
        ))}
      </div>
      <div className="mt-2 text-forge-text-faint">{t("model.card.tip")}</div>
    </InfoCardFrame>
  );
}
