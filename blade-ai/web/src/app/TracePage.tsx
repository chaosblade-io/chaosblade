/**
 * Trace route (/trace, /trace/$taskId) — the per-task process audit
 * view: a master list of tasks on the left, the full audit detail of
 * the selected task on the right.
 *
 * The detail body is the former TaskDetailPage (/tasks/$taskId),
 * absorbed here by the 2026-08-20 IA decision (the trace page replaced
 * the replay page and became the single audit surface; the old detail
 * route is a redirect). Section order follows the trace reading flow:
 * overview → span waterfall → postmortem timeline → verification
 * verdicts → postmortem → feasibility → error dump.
 *
 * The lifecycle/metrics portion mirrors the CLI's ``metric --task-id``
 * render (cli/metrics_render.py) — field set, ordering, and span
 * colour semantics included. The trace-narrative sections (timeline,
 * checklist, recovery verdict, postmortem, feasibility) go beyond the
 * CLI: the CLI is a quick-glance surface, this page is the audit
 * surface. The metric envelope exposes the raw evidence fields
 * (task_store.get_metric passes fault_spec / postmortem /
 * feasibility_report through verbatim); every rendering transform —
 * timeline extraction from the markdown included — lives here, per
 * the project rule that the backend serves data and presentation
 * stays on the client. Where the CLI truncates to fit a terminal,
 * this page shows the full text.
 *
 * Polling: the list refreshes every 5s; the detail polls at the same
 * cadence while the selected task is still active (a drill in flight
 * grows spans without any user action), reusing core's
 * ``passesTasksFilter`` so "active" means exactly what /tasks says.
 */
import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "@tanstack/react-router";
import { formatFaultType, passesTasksFilter } from "@blade-ai/core";
import { useBoot } from "./bootContext";
import { ui } from "../lib/uiText";
import { asString } from "../lib/utils";
import { formatCreated, formatDuration } from "../lib/format";
import { SpanWaterfall, type Span } from "../components/trace/SpanWaterfall";

const REFRESH_MS = 5000;

type TaskRow = Record<string, unknown>;
type TaskDetail = Record<string, unknown>;

/** Small-dot colour — one mapping shared by the list rows and the
 *  detail header. Forge: semantic status is a dot + text pair, never
 *  a filled pill. */
function statusDot(status: string): string {
  switch (status.toLowerCase()) {
    case "success":
      return "bg-success-dot";
    case "failed":
    case "error":
      return "bg-danger";
    case "in_progress":
      return "bg-warning-dot";
    default:
      return "bg-forge-text-faint";
  }
}

/** Port of the CLI's ``_format_target``: namespace/identifier plus
 *  resource type and up to four params in parentheses. */
function formatTarget(target: unknown, params: unknown): string {
  if (!target || typeof target !== "object") return "—";
  const t = target as Record<string, unknown>;
  const ns = asString(t["namespace"]) || "?";
  const names = Array.isArray(t["names"]) ? t["names"] : [];
  const labels =
    t["labels"] && typeof t["labels"] === "object"
      ? (t["labels"] as Record<string, unknown>)
      : {};
  const rtype = asString(t["resource_type"]);
  let ident: string;
  if (names.length > 0) {
    ident = names
      .slice(0, 3)
      .map(String)
      .join(", ");
    if (names.length > 3) ident += ` +${names.length - 3} more`;
  } else if (Object.keys(labels).length > 0) {
    ident =
      "labels " +
      Object.entries(labels)
        .map(([k, v]) => `${k}=${v}`)
        .join(",");
  } else {
    ident = "?";
  }
  let line = `${ns}/${ident}`;
  const extras: string[] = rtype ? [rtype] : [];
  if (params && typeof params === "object") {
    for (const [k, v] of Object.entries(params as Record<string, unknown>).slice(0, 4)) {
      extras.push(`${k}=${v}`);
    }
  }
  if (extras.length > 0) line += ` (${extras.join(", ")})`;
  return line;
}

/** "2026-08-13T16:32:02.801372+08:00" → "2026-08-13 16:32:02". Second
 *  resolution — the detail view is the audit surface. */
function fullTs(raw: unknown): string {
  const s = asString(raw);
  return s ? s.replace("T", " ").slice(0, 19) : "";
}

/** One definition-grid row; absent values render "—" except where the
 *  caller drops the row entirely (model/cost/experiment uid). */
function Field({ label, mono, children }: {
  label: string;
  mono?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="flex gap-4 py-1">
      <dt className="w-24 shrink-0 pt-px text-xs text-forge-text-faint">
        {label}
      </dt>
      <dd
        className={`min-w-0 flex-1 text-sm ${
          mono ? "font-mono text-xs" : ""
        } break-words`}
      >
        {children}
      </dd>
    </div>
  );
}

/** Verification layer status → text colour. CLI maps passed/failed/
 *  skipped to green/red/yellow; the forge tokens for that trio are
 *  success/danger/warning. */
function layerStatusColor(status: string): string {
  switch (status.toLowerCase()) {
    case "passed":
      return "text-success";
    case "failed":
      return "text-danger";
    case "skipped":
      return "text-forge-text-faint";
    default:
      return "text-warning";
  }
}

/** Holistic verdict word → layer-status token. Inject and recover
 *  spell the same three-way outcome differently: overall
 *  (verified / partial / unverified) vs level (recovered / partial /
 *  unverified / unrecovered — the unverified member joined the recover
 *  vocabulary in round-14). "unverified" is honest ignorance — the
 *  observation channel was unavailable, which is NOT counter-evidence:
 *  colouring it red like "unrecovered" would visually translate "cannot
 *  tell" back into "failed". It shares the caution colour with "partial". */
function holisticStatus(level: string): string {
  if (level === "verified" || level === "recovered") return "passed";
  if (level === "unrecovered") return "failed";
  return "partial";
}

/** Checklist status → glyph + colour; reuses the layer palette. */
function checklistGlyph(status: string): [string, string] {
  switch (status.toLowerCase()) {
    case "passed":
      return ["✓", "text-success"];
    case "failed":
      return ["✗", "text-danger"];
    case "skipped":
      return ["○", "text-forge-text-faint"];
    default:
      return ["—", "text-forge-text-faint"];
  }
}

/** One verdict block — used for both ``verification`` (inject side) and
 *  ``recover_verification`` (recover side), which share the layer1 /
 *  layer2 / checklist / warnings shape. Both carry a holistic verdict
 *  shown next to the title — inject side ``overall`` (verified /
 *  partial / unverified), recover side ``level`` (recovered / partial /
 *  unverified / unrecovered). */
function VerdictSection({ title, verdict }: { title: string; verdict: unknown }) {
  if (!verdict || typeof verdict !== "object") return null;
  const v = verdict as Record<string, unknown>;
  const level = asString(v["level"]) || asString(v["overall"]);
  const layers: React.ReactNode[] = [];
  for (const [key, label] of [
    ["layer1", "L1"],
    ["layer2", "L2"],
  ] as const) {
    const layer = v[key];
    if (!layer || typeof layer !== "object") continue;
    const l = layer as Record<string, unknown>;
    const st = asString(l["status"]) || "?";
    const details = asString(l["details"]).trim();
    layers.push(
      <div key={key} className="py-1">
        <p className="text-sm">
          <span className="font-medium">{label}</span>{" "}
          <span className={layerStatusColor(st)}>{st}</span>
        </p>
        {details && (
          <p className="mt-0.5 whitespace-pre-wrap text-xs text-forge-text-secondary">
            {details}
          </p>
        )}
      </div>,
    );
  }
  const checklist =
    v["checklist"] && typeof v["checklist"] === "object"
      ? (v["checklist"] as Record<string, unknown>)
      : {};
  const items = Array.isArray(checklist["items"]) ? checklist["items"] : [];
  const warnings = Array.isArray(v["warnings"]) ? v["warnings"] : [];
  if (layers.length === 0 && items.length === 0 && warnings.length === 0 && !level)
    return null;
  return (
    <section className="mt-6">
      <h2 className="text-sm font-medium">
        {title}
        {level && (
          <span className={`ml-2 font-normal ${layerStatusColor(holisticStatus(level))}`}>
            {level}
          </span>
        )}
      </h2>
      <div className="mt-1">{layers}</div>
      {items.length > 0 && (
        <div className="mt-2">
          <p className="text-xs text-forge-text-faint">{ui().taskDetailChecklist}</p>
          <div className="mt-1 divide-y divide-forge-border/60">
            {items.map((raw, i) => {
              const item = (raw ?? {}) as Record<string, unknown>;
              const st = asString(item["status"]) || "?";
              const [glyph, color] = checklistGlyph(st);
              return (
                <div key={i} className="flex gap-2 py-1.5 text-sm">
                  <span className={`w-4 shrink-0 font-medium ${color}`}>{glyph}</span>
                  <div className="min-w-0 flex-1">
                    <p>
                      #{asString(item["step"]) || i + 1}{" "}
                      <span className={layerStatusColor(st)}>{st}</span>
                    </p>
                    {asString(item["evidence"]).trim() && (
                      <p className="mt-0.5 whitespace-pre-wrap text-xs text-forge-text-secondary">
                        {asString(item["evidence"])}
                      </p>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
      {warnings.length > 0 && (
        <div className="mt-2">
          <p className="text-xs text-warning">
            ⚠ {warnings.length} {ui().taskDetailWarnings}
          </p>
          <ul className="mt-1 list-inside list-disc text-xs text-forge-text-secondary">
            {warnings.map((w, i) => (
              <li key={i}>{String(w)}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

/** Parse the postmortem markdown's ``## Timeline`` section into
 *  ``[{ts, desc}]`` rows. The chronology lives inside the LLM-written
 *  report as ``- **HH:MM:SS** text`` bullets (the prompt contract in
 *  agent/postmortem/generator.py); only that section is scanned —
 *  other sections use the same ``- **label**:`` bullet shape (e.g.
 *  Background), and a whole-document scan would sweep them in.
 *  Presentation-side parsing by design: the backend exposes the raw
 *  postmortem, rendering concerns stay here. */
function parsePostmortemTimeline(markdown: string): { ts: string; desc: string }[] {
  const items: { ts: string; desc: string }[] = [];
  let inTimeline = false;
  for (const line of markdown.split("\n")) {
    const stripped = line.trim();
    const section = /^##\s+(.+?)\s*$/.exec(stripped);
    if (section) {
      inTimeline = section[1].toLowerCase() === "timeline";
      continue;
    }
    if (!inTimeline) continue;
    const m = /^- \*\*(\d{2}:\d{2}:\d{2})\*\*\s*(.+)$/.exec(stripped);
    if (m) items.push({ ts: m[1], desc: m[2].trim() });
  }
  return items;
}

/** Failure semantics in a timeline entry: the postmortem author marks
 *  regressions with words like 失败/failed — those entries get the
 *  danger-coloured dot so a scan of the chronology finds the break
 *  point first. */
function isBadTimelineEntry(desc: string): boolean {
  const d = desc.toLowerCase();
  return d.includes("失败") || d.includes("fail") || d.includes("error");
}

/** Postmortem timeline — parsed from the report markdown's
 *  ``## Timeline`` section. Renders only when the report exists; an
 *  existing-but-empty timeline says so explicitly. */
function TimelineSection({ postmortem }: { postmortem: unknown }) {
  if (!postmortem || typeof postmortem !== "object") return null;
  const pm = postmortem as Record<string, unknown>;
  const timeline = parsePostmortemTimeline(asString(pm["markdown"]));
  return (
    <section className="mt-6">
      <h2 className="text-sm font-medium">{ui().taskDetailTimeline}</h2>
      {timeline.length === 0 ? (
        <p className="mt-1 text-sm text-forge-text-faint">
          {ui().taskDetailTimelineNone}
        </p>
      ) : (
        <ol className="mt-2 border-l border-forge-border pl-4">
          {timeline.map((item, i) => (
            <li key={i} className="relative pb-2 last:pb-0">
              <span
                className={`absolute -left-[21px] top-1.5 inline-block size-1.5 rounded-full ${
                  isBadTimelineEntry(item.desc) ? "bg-danger" : "bg-forge-accent"
                }`}
              />
              <span className="font-mono text-xs font-medium text-forge-accent">
                {item.ts}
              </span>{" "}
              <span className="text-sm">{item.desc}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

/** Postmortem summary + the full markdown report behind a disclosure. */
function PostmortemSection({ postmortem }: { postmortem: unknown }) {
  if (!postmortem || typeof postmortem !== "object") return null;
  const pm = postmortem as Record<string, unknown>;
  const summary = asString(pm["summary"]).trim();
  const markdown = asString(pm["markdown"]).trim();
  if (!summary && !markdown) return null;
  return (
    <section className="mt-6">
      <h2 className="text-sm font-medium">{ui().taskDetailPostmortem}</h2>
      {summary && (
        <p className="mt-1 whitespace-pre-wrap text-sm">{summary}</p>
      )}
      {markdown && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-forge-text-faint hover:text-forge-text">
            {ui().taskDetailFullReport}
          </summary>
          <pre className="mt-2 overflow-x-auto whitespace-pre-wrap rounded-md border border-forge-border bg-forge-surface p-3 font-mono text-xs text-forge-text-secondary">
            {markdown}
          </pre>
        </details>
      )}
    </section>
  );
}

/** Feasibility pre-check verdict — severity colours follow the same
 *  ok / warn / critical semantics as the safety gate. */
function FeasibilitySection({ feasibility }: { feasibility: unknown }) {
  if (!feasibility || typeof feasibility !== "object") return null;
  const f = feasibility as Record<string, unknown>;
  const severity = asString(f["severity"]);
  const message = asString(f["message"]).trim();
  const recommendation = asString(f["recommendation"]).trim();
  if (!severity && !message) return null;
  const sevColor =
    severity === "ok"
      ? "text-success"
      : severity === "critical" || severity === "error"
        ? "text-danger"
        : "text-warning";
  return (
    <section className="mt-6">
      <h2 className="text-sm font-medium">
        {ui().taskDetailFeasibility}
        {severity && (
          <span className={`ml-2 font-normal ${sevColor}`}>{severity}</span>
        )}
      </h2>
      {message && (
        <p className="mt-1 whitespace-pre-wrap text-sm">{message}</p>
      )}
      {recommendation && (
        <p className="mt-1 whitespace-pre-wrap text-xs text-forge-text-secondary">
          {recommendation}
        </p>
      )}
    </section>
  );
}


/* ------------------------------------------------------------------ */
/* Master list (left column)                                           */
/* ------------------------------------------------------------------ */

function TraceList({
  tasks,
  selectedId,
}: {
  tasks: TaskRow[];
  selectedId: string;
}) {
  const navigate = useNavigate();
  return (
    <aside className="flex h-full w-60 shrink-0 flex-col border-r border-forge-border bg-forge-sidebar">
      <header className="flex h-10 shrink-0 items-center border-b border-forge-border px-3">
        <span className="text-xs font-medium text-forge-text-secondary">
          {ui().traceListTitle}
        </span>
      </header>
      <ul className="min-h-0 flex-1 overflow-y-auto p-1.5">
        {tasks.map((row, i) => {
          const taskId = asString(row["task_id"]);
          const status = asString(row["status"]) || "—";
          const on = taskId === selectedId;
          return (
            <li key={taskId || i}>
              <button
                type="button"
                aria-current={on || undefined}
                onClick={() => {
                  if (!taskId) return;
                  void navigate({ to: "/trace/$taskId", params: { taskId } });
                }}
                className={`flex w-full flex-col gap-0.5 rounded-button px-2 py-1.5 text-left ${
                  on
                    ? "bg-forge-accent-soft"
                    : "hover:bg-forge-bg"
                }`}
              >
                <span className="flex items-center gap-1.5 text-xs">
                  <span
                    className={`inline-block size-1.5 shrink-0 rounded-full ${statusDot(status)}`}
                  />
                  <span
                    className={`truncate font-mono ${
                      on ? "text-forge-accent" : "text-forge-text"
                    }`}
                  >
                    {formatFaultType(row) || taskId || "?"}
                  </span>
                </span>
                <span className="flex items-center justify-between pl-3 text-[11px] text-forge-text-faint">
                  <span className="truncate">
                    {asString(row["operation"]) || status}
                  </span>
                  <span className="shrink-0 font-mono">
                    {formatCreated(
                      asString(row["gmt_create"]) || asString(row["created_at"]),
                    )}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </aside>
  );
}

/* ------------------------------------------------------------------ */
/* Detail (right column)                                               */
/* ------------------------------------------------------------------ */

function TraceDetail({ taskId }: { taskId: string }) {
  const { client } = useBoot();
  const query = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => client.getTaskMetric(taskId),
    refetchInterval: (q) => {
      const data = q.state.data;
      return data && passesTasksFilter(data, "active") ? REFRESH_MS : false;
    },
  });

  if (query.isPending) {
    return (
      <p className="py-12 text-center text-sm text-forge-text-faint">
        {ui().taskDetailLoading}
      </p>
    );
  }
  if (query.isError) {
    // Not-found arrives as an envelope failure whose message is the
    // server's "Task not found: …" — shown as-is under the title.
    return (
      <div className="py-12 text-center">
        <p className="text-sm font-medium text-danger">
          {ui().taskDetailLoadFailed}
        </p>
        <p className="mt-1 font-mono text-xs text-forge-text-faint">
          {query.error instanceof Error
            ? query.error.message
            : String(query.error)}
        </p>
      </div>
    );
  }

  const data: TaskDetail = query.data;
  const status = asString(data["status"]) || "?";
  const fault = formatFaultType(data);
  const faultSpec = (data["fault_spec"] ?? {}) as Record<string, unknown>;
  const useCase =
    asString(faultSpec["case_resource_path"]) ||
    asString(faultSpec["use_case_name"]);
  const intent = asString(faultSpec["user_description"]).trim();
  const plannedSec = Number(faultSpec["duration_seconds"] ?? 0);
  const safetyReason = asString(data["safety_reason"]).trim();
  const postmortem = data["postmortem"];
  const summary = (data["summary"] ?? {}) as Record<string, unknown>;
  const tokIn = Number(summary["total_token_input"] ?? 0);
  const tokOut = Number(summary["total_token_output"] ?? 0);
  const llmCalls = Number(summary["total_llm_calls"] ?? 0);
  const toolCalls = Number(summary["total_tool_calls"] ?? 0);
  const hasCost = tokIn > 0 || tokOut > 0 || llmCalls > 0 || toolCalls > 0;
  const created = fullTs(data["gmt_create"]);
  const finished = fullTs(data["finished_at"]);
  const durationMs = Number(data["duration_ms"] ?? 0);
  let window_ = created || "—";
  if (finished) window_ += ` → ${finished}`;
  if (durationMs > 0) window_ += `  (${formatDuration(durationMs)})`;
  const model = asString(data["model_name"]);
  const experimentUid = asString(data["experiment_uid"]);
  const errorText = asString(data["error"]).trim();
  const spans = Array.isArray(data["spans"]) ? (data["spans"] as Span[]) : [];

  return (
    <>
      <h1 className="flex items-center gap-2 text-base font-medium">
        <span
          className={`inline-block size-1.5 rounded-full ${statusDot(status)}`}
        />
        <span>{status}</span>
        {fault && (
          <span className="font-mono text-sm text-forge-text-secondary">
            · {fault}
          </span>
        )}
      </h1>
      <p className="mt-1 font-mono text-xs text-forge-text-faint">
        {asString(data["task_id"]) || taskId}
      </p>

      <dl className="mt-4 border-t border-forge-border pt-3">
        <Field label={ui().taskDetailTarget} mono>
          {formatTarget(data["target"], data["params"])}
        </Field>
        {useCase && <Field label={ui().taskDetailUseCase}>{useCase}</Field>}
        {intent && <Field label={ui().taskDetailIntent}>{intent}</Field>}
        <Field label={ui().taskDetailPhase}>
          {asString(data["phase"]) || "—"} · safety{" "}
          {asString(data["safety_status"]) || "?"}
          {safetyReason && (
            <span className="text-forge-text-faint"> — {safetyReason}</span>
          )}
        </Field>
        <Field label={ui().taskDetailWindow} mono>
          {window_}
        </Field>
        {plannedSec > 0 && (
          <Field label={ui().taskDetailPlanned}>{plannedSec}s</Field>
        )}
        {model && <Field label={ui().taskDetailModel}>{model}</Field>}
        {hasCost && (
          <Field label={ui().taskDetailCost}>
            {tokIn}↓ {tokOut}↑ tokens · LLM ×{llmCalls} · tools ×{toolCalls}
          </Field>
        )}
        {experimentUid && (
          <Field label={ui().taskDetailExperimentUid} mono>
            {experimentUid}
          </Field>
        )}
      </dl>

      {/* Section order = the trace reading flow: WHAT ran (waterfall)
          → WHEN it happened (timeline) → WHETHER it held (verdicts)
          → WHY it went that way (postmortem, feasibility). */}
      <section className="mt-6">
        <h2 className="text-sm font-medium">{ui().taskDetailSpans}</h2>
        <SpanWaterfall spans={spans} />
      </section>

      <TimelineSection postmortem={postmortem} />

      <VerdictSection
        title={ui().taskDetailVerification}
        verdict={data["verification"]}
      />

      <VerdictSection
        title={ui().taskDetailRecoverVerification}
        verdict={data["recover_verification"]}
      />

      <PostmortemSection postmortem={postmortem} />

      <FeasibilitySection feasibility={data["feasibility_report"]} />

      {errorText && (
        <section className="mt-6">
          <h2 className="text-sm font-medium text-danger">
            {ui().taskDetailError}
          </h2>
          <pre className="mt-1 whitespace-pre-wrap font-mono text-xs text-forge-text-secondary">
            {errorText}
          </pre>
        </section>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ */
/* Page                                                                */
/* ------------------------------------------------------------------ */

export function TracePage() {
  const { client } = useBoot();
  const navigate = useNavigate();
  // strict:false — the same component serves /trace (no param) and
  // /trace/$taskId.
  const params = useParams({ strict: false }) as { taskId?: string };
  const selectedId = params.taskId ?? "";

  const listQuery = useQuery({
    queryKey: ["tasks"],
    queryFn: () => client.listTasks(),
    refetchInterval: REFRESH_MS,
  });
  const tasks: TaskRow[] = Array.isArray(listQuery.data?.["tasks"])
    ? (listQuery.data?.["tasks"] as TaskRow[])
    : [];

  // /trace with a non-empty list resolves to the newest task's deep
  // link — replace, so the redirect doesn't litter the history stack.
  useEffect(() => {
    if (selectedId || tasks.length === 0) return;
    const first = asString(tasks[0]?.["task_id"]);
    if (first) {
      void navigate({
        to: "/trace/$taskId",
        params: { taskId: first },
        replace: true,
      });
    }
  }, [selectedId, tasks, navigate]);

  let right: React.ReactNode;
  if (listQuery.isPending) {
    right = (
      <p className="py-12 text-center text-sm text-forge-text-faint">
        {ui().tasksLoading}
      </p>
    );
  } else if (listQuery.isError) {
    right = (
      <p className="py-12 text-center text-sm text-danger">
        {ui().tasksLoadFailed}
      </p>
    );
  } else if (tasks.length === 0 && !selectedId) {
    // Bare /trace with no tasks at all — the page-level empty state.
    // A deep-linked /trace/$taskId skips this: the detail query is the
    // authority for whether the task exists (list/detail timing can
    // lag, and the not-found envelope belongs to the detail).
    right = (
      <p className="py-12 text-center text-sm text-forge-text-faint">
        {ui().traceEmpty}
      </p>
    );
  } else if (!selectedId) {
    // Brief state while the auto-select navigation above fires.
    right = (
      <p className="py-12 text-center text-sm text-forge-text-faint">
        {ui().traceSelectHint}
      </p>
    );
  } else {
    right = <TraceDetail taskId={selectedId} />;
  }

  return (
    <div className="flex h-full">
      <TraceList tasks={tasks} selectedId={selectedId} />
      <div className="h-full min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl px-6 py-6">{right}</div>
      </div>
    </div>
  );
}
