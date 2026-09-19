/**
 * Confirm gate — the web counterpart of the TUI's ConfirmMessage.
 *
 * Two item kinds, same contract as the TUI:
 *
 *   confirm_context (history, read-only)
 *     — the "what would happen" card. Tier is signaled by the node:
 *     ``intent_confirm`` (L1 soft: "did I read your intent right?")
 *     vs ``confirmation_gate`` / ``tool_screener`` /
 *     ``plan_change_confirm`` (hard: "this hits production"). Generic
 *     fallback for pre-payload servers. Fields follow the
 *     always-render discipline: empty values show ``confirm.none`` in
 *     a faint colour — the row never hides.
 *
 *   confirm_prompt (pending, interactive)
 *     — approve / reject / feedback buttons, or the server-supplied
 *     ``payload.options`` list (plan_builder). Collapses to a one-line
 *     chip once resolved (● ARMED · proceeding / ● ABORTED · stopped).
 *
 * Interaction contract (shared with the TUI): the card only dispatches
 * ``CONFIRM_USER_DECIDED``; Composer's pendingDecision effect runs the
 * network calls (resolveConfirm + optional feedback turn). Two
 * feedback semantics, mirroring the TUI:
 *   - L1/L2 feedback → answer "rejected" + feedback field (gate closes
 *     as rejected, then the text fires as a fresh user turn)
 *   - plan_builder free_input → the typed text IS the answer (raw
 *     resume value), no rejected wrapper
 *
 * Card family (six bodies under one frame, dispatched by node +
 * payload gates — mirrors the TUI's ConfirmContextMessage):
 * intent (single/batch fields + risk + confidence + clarification),
 * execution (plan fields + params/duration + health/feasibility
 * tri-states + conflicts + safety score + plan markdown), target-change
 * drift diff, plan-change fault-type diff, plan-builder question, and a
 * generic fallback. ``item.content`` renders only in the generic
 * fallback — structured cards carry their fields, same as the TUI.
 */
import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { ConfirmContextItem, ConfirmPromptItem } from "@blade-ai/core";
import { t, useAppDispatch, useAppSelector } from "@blade-ai/core";
import { ui } from "../../lib/uiText";
import { Markdown } from "./Markdown";

// ── payload accessors (same defensive shape as the TUI) ─────────────

function asString(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v != null && typeof v === "object" && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : null;
}

function asArray(v: unknown): unknown[] | null {
  return Array.isArray(v) ? v : null;
}

function kvJoin(rec: Record<string, unknown> | null): string {
  if (!rec) return "";
  return Object.entries(rec)
    .map(([k, v]) => `${k}=${asString(v)}`)
    .join(", ");
}

function durationStr(v: unknown): string {
  return typeof v === "number" && Number.isFinite(v) && v > 0
    ? `${v}s`
    : "";
}

/** One widened-contract entry as a single line — the TS twin of
 *  Python's ``format_mechanism_writes_for_display`` row: scope/ns +
 *  names (long lists collapse to ``N: a, b …(+M)``) or a 'prefix'
 *  (prefix) selector. Blank names fall back to ``*``. */
function mechanismWriteLine(entry: Record<string, unknown>): string {
  const scope = asString(entry["scope"]) || "?";
  const ns = asString(entry["namespace"]) || "<cluster>";
  const names = (asArray(entry["names"]) ?? [])
    .map(asString)
    .filter(Boolean);
  let sel: string;
  if (names.length > 4) {
    sel = `${names.length}: ${names.slice(0, 4).join(", ")} …(+${names.length - 4})`;
  } else if (names.length > 0) {
    sel = names.join(", ");
  } else {
    const prefix = asString(entry["name_prefix"]);
    sel = prefix ? `'${prefix}' (prefix)` : "*";
  }
  return `${scope}/${ns}: ${sel}`;
}

// ── shared bits ─────────────────────────────────────────────────────

function StatusDot({ className }: { className: string }) {
  return (
    <span
      className={`inline-block size-2 shrink-0 rounded-full ${className}`}
    />
  );
}

/** Always-render field row: empty values show the placeholder
 *  (default confirm.none) in a faint colour — the row never hides.
 *  ``tone`` paints label+value for audit rows that must stand out
 *  (a warn-coloured clarification count, a danger-level score). */
function Field({
  label,
  value,
  placeholder,
  tone,
}: {
  label: string;
  value: string;
  placeholder?: string;
  tone?: "warn" | "danger";
}) {
  const empty = !value;
  return (
    <>
      <dt
        className={
          !empty && tone === "warn" ? "text-warning" : "text-forge-text-faint"
        }
      >
        {label}
      </dt>
      <dd
        className={
          empty
            ? "text-forge-text-faint"
            : tone === "warn"
              ? "font-mono break-words text-warning"
              : tone === "danger"
                ? "font-mono break-words text-danger"
                : "font-mono break-words text-forge-text-secondary"
        }
      >
        {value || placeholder || t("confirm.none")}
      </dd>
    </>
  );
}

/** L1 intent fields — mirrors the TUI's single-fault field order.
 *  Rows only (no <dl> wrapper) so callers compose them inside the
 *  shared FieldGrid. */
function FaultIntentRows({ fi }: { fi: Record<string, unknown> }) {
  const names =
    asArray(fi["names"])
      ?.map(asString)
      .filter(Boolean)
      .join(", ") ?? "";
  return (
    <>
      <Field label={t("confirm.field.fault_type")} value={asString(fi["fault_type"])} />
      <Field
        label={t("confirm.field.case_resource_path")}
        value={asString(fi["case_resource_path"])}
      />
      <Field label={t("confirm.field.scope")} value={asString(fi["scope"])} />
      <Field label={t("confirm.field.target")} value={asString(fi["target"])} />
      <Field label={t("confirm.field.action")} value={asString(fi["action"])} />
      <Field label={t("confirm.field.namespace")} value={asString(fi["namespace"])} />
      <Field
        label={t("confirm.field.duration")}
        value={durationStr(fi["duration_seconds"])}
      />
      <Field label={t("confirm.field.labels")} value={kvJoin(asRecord(fi["labels"]))} />
      <Field label={t("confirm.field.names")} value={names} />
      <Field label={t("confirm.field.params")} value={kvJoin(asRecord(fi["params"]))} />
      <Field
        label={t("confirm.field.user_description")}
        value={asString(fi["user_description"])}
      />
    </>
  );
}

/** Five-way node switch (mirrors the TUI): the title answers "which
 *  gate is this". Default is the generic confirm title — never the
 *  resolved chip's "answered" label. */
function titleKeyFor(node: string): string {
  switch (node) {
    case "intent_confirm":
      return "confirm.intent.title";
    case "confirmation_gate":
      return "confirm.execution.title";
    case "tool_screener":
      return "confirm.targetChange.title";
    case "plan_change_confirm":
      return "confirm.planChange.title";
    case "plan_builder":
      return "confirm.plan_builder.title";
    default:
      return "confirm.title";
  }
}

function isHardTier(node: string): boolean {
  return (
    node === "confirmation_gate" ||
    node === "tool_screener" ||
    node === "plan_change_confirm"
  );
}

/** Dim guidance line under the title (same i18n keys as the TUI
 *  frames). plan_builder carries none — the question itself leads. */
function preambleKeyFor(node: string, isBatch: boolean): string {
  switch (node) {
    case "intent_confirm":
      return isBatch
        ? "confirm.intent.batch_preamble"
        : "confirm.intent.preamble";
    case "confirmation_gate":
      return "confirm.execution.preamble";
    case "tool_screener":
      return "confirm.targetChange.preamble";
    case "plan_change_confirm":
      return "confirm.planChange.preamble";
    case "plan_builder":
      return "";
    default:
      return "confirm.generic.preamble";
  }
}

// ── payload usability gates (same dispatch contract as the TUI) ─────

function hasIntentContent(payload: Record<string, unknown>): boolean {
  const fi = asRecord(payload["fault_intent"]);
  if (fi) {
    if (
      asString(fi["fault_type"]) ||
      asString(fi["scope"]) ||
      asString(fi["target"]) ||
      asString(fi["action"]) ||
      asString(fi["namespace"]) ||
      asString(fi["user_description"])
    ) {
      return true;
    }
    const labels = asRecord(fi["labels"]);
    if (labels && Object.keys(labels).length > 0) return true;
    const names = asArray(fi["names"]);
    if (names && names.length > 0) return true;
    const params = asRecord(fi["params"]);
    if (params && Object.keys(params).length > 0) return true;
  }
  // Batch payloads may carry only ``batch_faults`` — the TUI relies on
  // fault_intent/confidence being present, this is a defensive superset.
  const batch = asArray(payload["batch_faults"]);
  if (batch && batch.length > 0) return true;
  const conf = payload["intent_confidence"];
  return typeof conf === "number" && Number.isFinite(conf);
}

function hasExecutionContent(payload: Record<string, unknown>): boolean {
  if (asString(payload["skill_name"])) return true;
  if (asString(payload["plan_summary"])) return true;
  if (asString(payload["safety_status"])) return true;
  if (asString(payload["safety_reason"])) return true;
  const target = asRecord(payload["target"]);
  if (target && Object.keys(target).length > 0) return true;
  // A widened-contract card rides the structured execution body even
  // when every other field is empty — the manifest entries are the
  // one thing the approving human must not miss.
  const writes = asArray(payload["mechanism_writes"]);
  if (writes && writes.length > 0) return true;
  return false;
}

// ── risk / confidence (logic mirrored from the TUI) ─────────────────

const LOW_CONFIDENCE_THRESHOLD = 0.7;
const RISK_TIER_LOW_MAX = 2;
const RISK_TIER_MID_MAX = 9;

interface RiskInfo {
  kind: "concrete" | "bounded" | "unbounded";
  target: string;
  count: number;
  descriptor: string;
  sample: string;
}

function computeRiskInfo(fi: Record<string, unknown>): RiskInfo | null {
  const target = asString(fi["target"]) || "resource";
  const names = asArray(fi["names"]);
  if (names && names.length > 0) {
    const head = names.slice(0, 3).map(asString).filter(Boolean);
    let sample = head.join(", ");
    if (names.length > 3) sample += `, … (+${names.length - 3})`;
    return { kind: "concrete", target, count: names.length, descriptor: "", sample };
  }
  const params = asRecord(fi["params"]) ?? {};
  const rawCount = params["count"] ?? params["Count"];
  const bounded =
    typeof rawCount === "number" && Number.isFinite(rawCount) && rawCount > 0
      ? Math.floor(rawCount)
      : typeof rawCount === "string" && /^\d+$/.test(rawCount)
        ? parseInt(rawCount, 10)
        : null;
  if (bounded != null && bounded > 0) {
    return { kind: "bounded", target, count: bounded, descriptor: "", sample: "" };
  }
  if (fi["labels"]) {
    return { kind: "unbounded", target, count: 0, descriptor: "labels", sample: "" };
  }
  if ("percent" in params) {
    return {
      kind: "unbounded",
      target,
      count: 0,
      descriptor: `percent:${asString(params["percent"])}`,
      sample: "",
    };
  }
  if (asString(fi["scope"]).toLowerCase() === "namespace") {
    return { kind: "unbounded", target, count: 0, descriptor: "namespace", sample: "" };
  }
  return null;
}

function riskTierKey(count: number): "low" | "medium" | "high" {
  if (count <= RISK_TIER_LOW_MAX) return "low";
  if (count <= RISK_TIER_MID_MAX) return "medium";
  return "high";
}

function confidenceTierKey(c: number): "low" | "medium" | "high" {
  if (c < 0.5) return "low";
  if (c < LOW_CONFIDENCE_THRESHOLD) return "medium";
  return "high";
}

function lowConfidenceHint(
  fi: Record<string, unknown>,
  confidence: number,
): string {
  const namespace = asString(fi["namespace"]) || "default";
  const target = asString(fi["target"]) || "?";
  const action = asString(fi["action"]) || "?";
  const lead =
    confidence < 0.5
      ? t("confirm.confidence.warn_strong")
      : t("confirm.confidence.warn_soft");
  let msg = `${lead}: namespace=${namespace} · target=${target} · action=${action}`;
  const nsLower = namespace.toLowerCase();
  if (nsLower.includes("prod") || nsLower.includes("production")) {
    msg += `; ${t("confirm.confidence.warn_prod")}`;
  }
  return msg;
}

// ── shared card-body primitives ─────────────────────────────────────

type DotTone = "ok" | "warn" | "danger" | "dim" | "accent";

const DOT_CLASS: Record<DotTone, string> = {
  ok: "bg-success-dot",
  warn: "bg-warning-dot",
  danger: "bg-danger-dot",
  dim: "bg-forge-text-faint",
  accent: "bg-forge-accent",
};

/** Status row — Forge discipline: a small dot + text, never a pill. */
function StatusRow({
  tone,
  children,
}: {
  tone: DotTone;
  children: ReactNode;
}) {
  return (
    <div className="flex items-start gap-1.5 text-xs">
      <span
        className={`mt-[5px] inline-block size-1.5 shrink-0 rounded-full ${DOT_CLASS[tone]}`}
      />
      <div className="min-w-0 break-words text-forge-text-secondary">
        {children}
      </div>
    </div>
  );
}

/** Card body: vertical stack with breathing room between blocks. */
function CardBody({ children }: { children: ReactNode }) {
  return <div className="mt-2 flex flex-col gap-2 text-xs">{children}</div>;
}

/** Two-column field grid (label gutter + value). */
function FieldGrid({ children }: { children: ReactNode }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">{children}</dl>
  );
}

// ── intent body (Layer 1, soft tier) ────────────────────────────────

function RiskRow({ risk }: { risk: RiskInfo }) {
  if (risk.kind === "unbounded") {
    let descriptor: string;
    if (risk.descriptor === "labels") {
      descriptor = t("confirm.risk.scope.labels");
    } else if (risk.descriptor === "namespace") {
      descriptor = t("confirm.risk.scope.namespace");
    } else if (risk.descriptor.startsWith("percent:")) {
      descriptor = t("confirm.risk.scope.percent", {
        value: risk.descriptor.slice("percent:".length),
      });
    } else {
      descriptor = risk.descriptor;
    }
    return (
      <StatusRow tone="warn">
        <span className="font-medium text-warning">
          {t("confirm.field.risk")}: {risk.target} · {descriptor}
        </span>{" "}
        <span className="text-forge-text-faint">
          ({t("confirm.risk.runtime")})
        </span>
      </StatusRow>
    );
  }
  const tierKey = riskTierKey(risk.count);
  const tone: DotTone =
    tierKey === "high" ? "danger" : tierKey === "medium" ? "warn" : "ok";
  const countLabel =
    risk.kind === "bounded"
      ? `≤ ${risk.count} ${risk.target}`
      : `${risk.count} ${risk.target}`;
  return (
    <StatusRow tone={tone}>
      <span className="font-medium">
        {t("confirm.field.risk")}: {t(`confirm.tier.${tierKey}`)} · {countLabel}
      </span>
      {risk.sample ? (
        <span className="text-forge-text-faint">{`  (${risk.sample})`}</span>
      ) : null}
    </StatusRow>
  );
}

function ConfidenceRows({
  confidence,
  fi,
}: {
  confidence: number;
  fi: Record<string, unknown>;
}) {
  const tierKey = confidenceTierKey(confidence);
  const tone: DotTone =
    tierKey === "low" ? "danger" : tierKey === "medium" ? "warn" : "ok";
  const pct = `${(confidence * 100).toFixed(0)}%`;
  return (
    <div className="flex flex-col gap-1">
      <StatusRow tone={tone}>
        <span className="font-medium">
          {t("confirm.field.intent_confidence")}: {pct} ·{" "}
          {t(`confirm.tier.${tierKey}`)}
        </span>
      </StatusRow>
      {confidence < LOW_CONFIDENCE_THRESHOLD ? (
        <div
          className={`pl-3.5 ${tone === "danger" ? "text-danger" : "text-warning"}`}
        >
          {lowConfidenceHint(fi, confidence)}
        </div>
      ) : null}
    </div>
  );
}

function IntentContent({ payload }: { payload: Record<string, unknown> }) {
  const fi = asRecord(payload["fault_intent"]) ?? {};
  const batch = asArray(payload["batch_faults"]);
  const isBatch = batch != null && batch.length > 0;
  const confRaw = payload["intent_confidence"];
  const confidence =
    typeof confRaw === "number" && Number.isFinite(confRaw)
      ? Math.max(0, Math.min(1, confRaw))
      : 0;
  const reasoning = asString(payload["intent_reasoning"]);
  const roundRaw = payload["clarification_round"];
  const round =
    typeof roundRaw === "number" && Number.isFinite(roundRaw)
      ? Math.max(0, Math.floor(roundRaw))
      : 0;
  const showReasoning =
    reasoning.length > 0 && confidence < LOW_CONFIDENCE_THRESHOLD;
  const risk = isBatch ? null : computeRiskInfo(fi);

  return (
    <CardBody>
      {isBatch ? (
        <FieldGrid>
          {batch.map((raw, i) => {
            const f = asRecord(raw) ?? {};
            const names =
              asArray(f["names"])?.map(asString).filter(Boolean).join(", ") ||
              "*";
            const dur = durationStr(f["duration_seconds"]);
            return (
              <Fragment key={i}>
                <dt className="text-forge-text-faint">
                  {t("confirm.intent.fault_index", { n: i + 1 })}
                </dt>
                <dd className="font-mono break-words text-forge-text-secondary">
                  {`${asString(f["scope"])}-${asString(f["target"])}-${asString(f["action"])}`}
                  <span className="text-forge-text-faint">
                    {`  @ ${asString(f["namespace"])}/${names}${dur ? ` (${dur})` : ""}`}
                  </span>
                </dd>
              </Fragment>
            );
          })}
        </FieldGrid>
      ) : (
        <FieldGrid>
          <FaultIntentRows fi={fi} />
        </FieldGrid>
      )}
      {risk ? <RiskRow risk={risk} /> : null}
      {confidence > 0 ? <ConfidenceRows confidence={confidence} fi={fi} /> : null}
      <FieldGrid>
        {showReasoning ? (
          <Field label={t("confirm.field.intent_reasoning")} value={reasoning} />
        ) : null}
        {/* Clarification round is ALWAYS rendered (0 included) — the
            same constant-render rule as the TUI: "no revision needed"
            must be distinguishable from "metric doesn't exist". */}
        <Field
          label={t("confirm.field.clarification_round")}
          value={
            round > 0
              ? t("confirm.clarification.label", { n: round })
              : t("confirm.clarification.zero")
          }
          tone={round > 0 ? "warn" : undefined}
        />
      </FieldGrid>
    </CardBody>
  );
}

// ── execution body (Layer 2 confirmation_gate, hard tier) ───────────

function safetyTone(status: string): DotTone {
  switch (status) {
    case "safe":
    case "passed":
      return "ok";
    case "blocked":
    case "rejected":
      return "danger";
    default:
      return "warn";
  }
}

function safetyBadgeLabel(status: string): string {
  if (status === "safe" || status === "passed") return t("confirm.safety.safe");
  if (status === "blocked" || status === "rejected")
    return t("confirm.safety.blocked");
  return t("confirm.safety.warning");
}

function ExecutionContent({ payload }: { payload: Record<string, unknown> }) {
  const skill = asString(payload["skill_name"]);
  const target = asRecord(payload["target"]);
  const safetyStatus = asString(payload["safety_status"]);
  const safetyReason = asString(payload["safety_reason"]);
  const safetyCheckedDetail = asString(payload["safety_checked_detail"]);

  let targetStr = "";
  if (target) {
    const ns = asString(target["namespace"]);
    const namesStr =
      asArray(target["names"])?.map(asString).filter(Boolean).join(", ") ?? "";
    if (ns && namesStr) targetStr = `namespace=${ns}, names=[${namesStr}]`;
    else if (ns) targetStr = `namespace=${ns}`;
    else if (namesStr) targetStr = `names=[${namesStr}]`;
  }

  // The TUI drops "k=" pairs whose value is empty.
  const paramsRec = asRecord(payload["params"]);
  const paramsStr = paramsRec
    ? Object.entries(paramsRec)
        .map(([k, v]) => `${k}=${asString(v)}`)
        .filter((s) => !s.endsWith("="))
        .join(", ")
    : "";

  const duration = durationStr(payload["duration_seconds"]);

  const faultIntent = asRecord(payload["fault_intent"]);
  const faultType = asString(faultIntent?.["fault_type"]);
  let faultBrief = "";
  if (faultType) {
    const triple = [
      asString(faultIntent?.["scope"]),
      asString(faultIntent?.["target"]),
      asString(faultIntent?.["action"]),
    ]
      .filter(Boolean)
      .join("/");
    faultBrief = triple ? `${faultType}  (${triple})` : faultType;
  }

  const planPath = asString(payload["plan_path"]);
  const attemptRaw = payload["pipeline_attempt"];
  const attempt =
    typeof attemptRaw === "number" && Number.isFinite(attemptRaw)
      ? Math.max(0, Math.floor(attemptRaw))
      : 0;
  const isComplex = payload["is_complex"] === true;

  // target_health_report — three states: issues / check ran + all
  // clear / check not run (payload field null).
  const healthReport = asRecord(payload["target_health_report"]);
  const healthOverall = asString(healthReport?.["overall"]);
  const healthCheckedDetail = asString(healthReport?.["checked_detail"]);
  const healthIssues = (asArray(healthReport?.["issues"]) ?? [])
    .map((r) => asRecord(r))
    .filter((r): r is Record<string, unknown> => r !== null);
  const hasHealthIssues =
    healthIssues.length > 0 || (healthOverall !== "" && healthOverall !== "ok");

  // feasibility_report — same three-state pattern.
  const feas = asRecord(payload["feasibility_report"]);
  const feasSeverity = asString(feas?.["severity"]);
  const feasMessage = asString(feas?.["message"]);
  const feasRecommendation = asString(feas?.["recommendation"]);
  const hasFeasIssue =
    feas != null && feasSeverity !== "" && feasSeverity !== "ok";

  const conflictUids = (asArray(payload["conflict_uids"]) ?? [])
    .map(asString)
    .filter(Boolean);

  // Widened write-set contract (CASE manifest): entries the case
  // legislated BEYOND the victim target. Danger-toned — approving
  // authorizes these cluster writes, so they must be read before the
  // buttons are touched.
  const mechanismWrites = (asArray(payload["mechanism_writes"]) ?? [])
    .map((r) => asRecord(r))
    .filter((r): r is Record<string, unknown> => r !== null);

  const safetyScore = asRecord(payload["safety_score"]);

  const hasProblem =
    safetyStatus === "warning" ||
    safetyStatus === "confirm_required" ||
    safetyStatus === "blocked" ||
    safetyStatus === "rejected";

  const planMarkdown = asString(payload["plan_preview_markdown"]);

  return (
    <CardBody>
      {/* Adaptive top alert — only for non-safe statuses, so the user
          sees the problem before scanning the plan (TUI v3). */}
      {hasProblem && safetyStatus ? (
        <StatusRow tone={safetyTone(safetyStatus)}>
          <span className="font-medium">{t("confirm.field.safety")}</span>
          {": "}
          {safetyReason || safetyCheckedDetail || safetyBadgeLabel(safetyStatus)}
        </StatusRow>
      ) : null}

      {/* Plan section — hidden entirely when there is no plan content
          (early policy-block payloads), mirroring the TUI guard. */}
      {faultBrief || skill || targetStr || planPath ? (
        <FieldGrid>
          {faultBrief ? (
            <Field label={t("confirm.field.fault")} value={faultBrief} />
          ) : null}
          <Field label={t("confirm.field.skill")} value={skill} />
          <Field label={t("confirm.field.target")} value={targetStr} />
          {planPath ? (
            <Field
              label={t("confirm.field.plan_path")}
              value={t("confirm.plan_saved", { path: planPath })}
            />
          ) : null}
          {attempt > 1 ? (
            <Field
              label={t("confirm.field.attempt")}
              value={t("confirm.attempt.label", { n: attempt })}
              tone="warn"
            />
          ) : null}
          {isComplex ? (
            <Field
              label={t("confirm.field.complexity")}
              value={t("confirm.complexity.complex")}
              tone="warn"
            />
          ) : null}
        </FieldGrid>
      ) : null}

      {/* Params + duration are ALWAYS rendered — "we did look at
          parameters" / the effective auto-recovery bound must be
          visible at this last gate before execution. */}
      <FieldGrid>
        <Field
          label={t("confirm.field.params")}
          value={paramsStr}
          placeholder={t("confirm.params.none")}
        />
        <Field label={t("confirm.field.duration")} value={duration} />
      </FieldGrid>

      <div className="flex flex-col gap-1">
        {hasHealthIssues ? (
          healthIssues.map((issue, i) => {
            const sev = asString(issue["severity"]);
            const code = asString(issue["code"]);
            const message = asString(issue["message"]);
            const hint = asString(issue["duration_hint"]);
            const detail = [message, hint && `(${hint})`]
              .filter(Boolean)
              .join(" ");
            return (
              <StatusRow
                key={i}
                tone={
                  sev === "block"
                    ? "danger"
                    : sev === "warn"
                      ? "warn"
                      : sev === "ok"
                        ? "ok"
                        : "dim"
                }
              >
                <span className="font-medium">{t("confirm.field.health")}</span>
                {": "}
                {detail ? `${detail} [${code}]` : code}
              </StatusRow>
            );
          })
        ) : healthReport === null ? (
          <StatusRow tone="dim">
            {t("confirm.field.health")}: {t("confirm.health.not_run")}
          </StatusRow>
        ) : (
          <StatusRow tone="ok">
            {t("confirm.field.health")}:
            {healthCheckedDetail
              ? ` ${t("confirm.health.all_clear")} (${healthCheckedDetail})`
              : ` ${t("confirm.health.all_clear")}`}
          </StatusRow>
        )}
      </div>

      <div className="flex flex-col gap-1">
        {hasFeasIssue ? (
          <>
            <StatusRow
              tone={
                feasSeverity === "impossible"
                  ? "danger"
                  : feasSeverity === "skipped"
                    ? "dim"
                    : "warn"
              }
            >
              {t("confirm.field.feasibility")}: {feasMessage}
            </StatusRow>
            {feasRecommendation ? (
              <div className="pl-3.5 text-forge-text-secondary">
                {feasRecommendation}
              </div>
            ) : null}
          </>
        ) : feas === null ? (
          <StatusRow tone="dim">
            {t("confirm.field.feasibility")}: {t("confirm.feasibility.not_run")}
          </StatusRow>
        ) : (
          <StatusRow tone="ok">
            {t("confirm.field.feasibility")}: {t("confirm.feasibility.all_clear")}
            {` (${t("confirm.feasibility.headroom_detail", {
              headroom:
                typeof feas?.["headroom"] === "number"
                  ? `${Math.round((feas["headroom"] as number) * 100)}%`
                  : "—",
              current: asString(feas?.["current_value"]) || "—",
              target: asString(feas?.["target_value"]) || "—",
            })})`}
          </StatusRow>
        )}
      </div>

      {mechanismWrites.length > 0 ? (
        <div className="flex flex-col gap-0.5">
          <StatusRow tone="danger">
            <span className="font-medium">
              {t("confirm.field.mechanism_writes")}
            </span>
          </StatusRow>
          {mechanismWrites.map((entry, i) => (
            <div
              key={i}
              className="pl-3.5 font-mono break-words text-danger"
            >
              {mechanismWriteLine(entry)}
            </div>
          ))}
          <div className="pl-3.5 text-forge-text-faint">
            {t("confirm.mechanism_writes.hint")}
          </div>
        </div>
      ) : null}

      {conflictUids.length > 0 ? (
        <div className="flex flex-col gap-0.5">
          <StatusRow tone="warn">
            {t("confirm.field.conflicts")}:{" "}
            <span className="font-mono">{conflictUids.join("  ")}</span>
          </StatusRow>
          <div className="pl-3.5 text-forge-text-faint">
            {t("confirm.conflicts.hint")}
          </div>
        </div>
      ) : null}

      {safetyScore ? (
        <FieldGrid>
          {(() => {
            const overall = asString(safetyScore["overall"]) || "0";
            const level = asString(safetyScore["level"]) || "low";
            const tone =
              level === "critical"
                ? ("danger" as const)
                : level === "high" || level === "medium"
                  ? ("warn" as const)
                  : undefined;
            return (
              <Field
                label={t("safety_score.overall")}
                value={`${overall}/100 (${t(`safety_score.level.${level}`)})`}
                tone={tone}
              />
            );
          })()}
          {(["blast_radius", "frequency", "time", "topology"] as const).map(
            (dim) => {
              const d = asRecord(safetyScore[dim]);
              if (!d) return null;
              return (
                <Field
                  key={dim}
                  label={t(`safety_score.${dim}`)}
                  value={`${asString(d["value"]) || "0"} — ${asString(d["explanation"])}`}
                />
              );
            },
          )}
        </FieldGrid>
      ) : null}

      {/* Bottom safety line — quiet placement when no top alert fired
          (avoids showing safety twice). */}
      {!hasProblem && safetyStatus ? (
        <StatusRow tone={safetyTone(safetyStatus)}>
          {t("confirm.field.safety")}:
          {` ${safetyCheckedDetail || safetyReason || t("confirm.safety.all_clear")}`}
        </StatusRow>
      ) : null}

      {/* Full plan markdown — the TUI renders it above the card; the
          web card keeps it in a bounded, scrollable block instead. */}
      {planMarkdown ? (
        <div className="max-h-64 overflow-auto rounded-input border border-forge-border bg-forge-bg px-3 py-2 text-forge-text-secondary">
          <Markdown text={planMarkdown} />
        </div>
      ) : null}
    </CardBody>
  );
}

// ── target-change body (tool_screener, hard tier) ───────────────────

function targetLine(target: Record<string, unknown> | null): string {
  if (!target) return "";
  const ns = asString(target["namespace"]) || "default";
  const parts: string[] = [`ns=${ns}`];
  const names = asArray(target["names"]);
  if (names && names.length > 0) {
    parts.push(`names=[${names.map(asString).filter(Boolean).join(", ")}]`);
  }
  const labels = asRecord(target["labels"]);
  if (labels && Object.keys(labels).length > 0) {
    parts.push(`labels={${kvJoin(labels)}}`);
  }
  return parts.join("  ");
}

function TargetChangeContent({
  payload,
}: {
  payload: Record<string, unknown>;
}) {
  const reason = asString(payload["reason"]);
  const agentReason = asString(payload["agent_reason"]);
  const original = asRecord(payload["original"]);
  const proposed = asRecord(payload["proposed"]);
  return (
    <CardBody>
      <p className="text-forge-text-secondary">
        <span className="text-forge-text-faint">
          {t("confirm.targetChange.agentReason")}:{" "}
        </span>
        {agentReason || t("confirm.targetChange.agentReasonEmpty")}
      </p>
      {reason ? <p className="text-forge-text-faint">{reason}</p> : null}
      <FieldGrid>
        <Field
          label={t("confirm.targetChange.original")}
          value={targetLine(original)}
          placeholder="—"
        />
        <Field
          label={t("confirm.targetChange.proposed")}
          value={targetLine(proposed)}
          placeholder="—"
        />
      </FieldGrid>
    </CardBody>
  );
}

// ── plan-change body (plan_change_confirm, hard tier) ───────────────

function faultTypeLine(ft: Record<string, unknown> | null): string {
  if (!ft) return "";
  const head = [
    asString(ft["scope"]),
    asString(ft["fault_target"]),
    asString(ft["fault_action"]),
  ]
    .filter(Boolean)
    .join("-");
  const dur = durationStr(asRecord(ft["fault_spec"])?.["duration_seconds"]);
  return dur ? `${head}  ·  ${dur}` : head;
}

function PlanChangeContent({
  payload,
}: {
  payload: Record<string, unknown>;
}) {
  const reason = asString(payload["reason"]);
  const original = asRecord(payload["original"]);
  const proposed = asRecord(payload["proposed"]);
  return (
    <CardBody>
      <FieldGrid>
        <Field label={t("confirm.planChange.reason")} value={reason} />
        <Field
          label={t("confirm.planChange.original")}
          value={faultTypeLine(original)}
          placeholder="—"
        />
        <Field
          label={t("confirm.planChange.proposed")}
          value={faultTypeLine(proposed)}
          placeholder="—"
        />
      </FieldGrid>
    </CardBody>
  );
}

// ── plan-builder / generic bodies (soft tier) ───────────────────────

function PlanSelectionContent({
  payload,
}: {
  payload: Record<string, unknown>;
}) {
  const question = asString(payload["question"]);
  return (
    <p className="mt-2 whitespace-pre-wrap text-sm text-forge-text-secondary">
      {question || t("confirm.plan_builder.default_question")}
    </p>
  );
}

function GenericContent({ content }: { content: string }) {
  return (
    <p className="mt-2 whitespace-pre-wrap text-sm text-forge-text-secondary">
      {content.trim() || t("confirm.body_empty")}
    </p>
  );
}

// ── context card (read-only, lives in history) ──────────────────────

export function ConfirmContextView({ item }: { item: ConfirmContextItem }) {
  const node = item.node ?? "";
  const payload = item.payload;
  const hard = isHardTier(node);

  const batchCount =
    node === "intent_confirm"
      ? (asArray(payload?.["batch_faults"])?.length ?? 0)
      : 0;
  const isBatch = batchCount > 0;
  const baseTitle = t(titleKeyFor(node));
  const title = isBatch
    ? `${baseTitle}  ·  ${t("confirm.intent.batch_count", { n: batchCount })}`
    : baseTitle;
  const preambleKey = preambleKeyFor(node, isBatch);

  // Body dispatch mirrors the TUI's ConfirmContextMessage exactly:
  // node + payload-shape gates, generic fallback for anything else.
  const body = ((): ReactNode => {
    if (payload && node === "plan_builder") {
      return <PlanSelectionContent payload={payload} />;
    }
    if (payload && node === "intent_confirm" && hasIntentContent(payload)) {
      return <IntentContent payload={payload} />;
    }
    if (
      payload &&
      node === "confirmation_gate" &&
      hasExecutionContent(payload)
    ) {
      return <ExecutionContent payload={payload} />;
    }
    if (
      payload &&
      node === "tool_screener" &&
      asString(payload["type"]) === "target_change"
    ) {
      return <TargetChangeContent payload={payload} />;
    }
    if (
      payload &&
      node === "plan_change_confirm" &&
      asString(payload["type"]) === "plan_change"
    ) {
      return <PlanChangeContent payload={payload} />;
    }
    return <GenericContent content={item.content} />;
  })();

  return (
    <div
      className={`rounded-card border bg-forge-card px-4 py-3 shadow-card ${
        hard ? "border-forge-amber-border" : "border-forge-border"
      }`}
    >
      {/* mock confirm-head: hard tier carries the warning triangle,
          soft tier a calm accent dot; the right-aligned "safety gate"
          badge is the execution gate's alone (mock sub). Risk detail
          lives in the body rows, not the header — the mock's risk-tag
          is inline in the params value, not a header element. */}
      <div className="flex items-center gap-2 text-sm font-medium text-forge-text">
        {hard ? (
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            className="shrink-0 text-warning-dot"
          >
            <path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
          </svg>
        ) : (
          <StatusDot className="bg-forge-accent" />
        )}
        {title}
        {item.autoApproved ? (
          <span className="text-xs font-normal text-forge-text-faint">
            {t("confirm.auto_approved")}
          </span>
        ) : null}
        {node === "confirmation_gate" ? (
          <span className="ml-auto text-xs font-normal text-forge-text-faint">
            {t("confirm.gate_badge")}
          </span>
        ) : null}
      </div>
      {preambleKey ? (
        <p className="mt-1.5 text-xs text-forge-text-faint">{t(preambleKey)}</p>
      ) : null}
      {body}
    </div>
  );
}

// ── prompt (interactive, lives in pending until resolved) ───────────

interface ConfirmOption {
  key: string;
  label: string;
  description?: string;
  recommended?: boolean;
}

export function ConfirmPromptView({ item }: { item: ConfirmPromptItem }) {
  const dispatch = useAppDispatch();
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedbackText, setFeedbackText] = useState("");
  const node = item.node ?? "";
  const isPlanBuilder = node === "plan_builder";
  const hard = isHardTier(node);
  const options = (asArray(item.payload?.["options"]) ?? []) as ConfirmOption[];
  // One gate, one answer: guards both double-clicks and held-key
  // repeat (Enter/Esc auto-repeat fires keydown at ~30Hz). The
  // reducer's resolved flag lags by a render; this ref is synchronous.
  const decidedRef = useRef(false);

  // Multi-prompt race (reducer L1492: an L1 intent_confirm left
  // unresolved when the L2 confirmation_gate arrives stays live in
  // pending alongside it). Only the FIRST unresolved prompt answers
  // the keyboard — otherwise one Enter would approve every open gate
  // at once. Mirrors the TUI MainContent's firstUnresolvedPromptId.
  // (Buttons stay clickable on every card: a click names its target
  // explicitly, so the user's intent is unambiguous there.)
  const isFirstUnresolved = useAppSelector(
    (s) =>
      s.pending.find((it) => it.kind === "confirm_prompt" && !it.resolved)
        ?.id === item.id,
  );

  // plan_builder with no server-supplied options: free input IS the
  // only control (the TUI's PlanSelectionPrompt fallback) — an
  // approve/reject button row would send answers ("approved") the
  // plan_builder resume contract can't understand.
  const freeInputOnly = isPlanBuilder && options.length === 0;

  const decide = useCallback(
    (answer: string, feedback?: string) => {
      if (decidedRef.current) return;
      decidedRef.current = true;
      dispatch({
        type: "CONFIRM_USER_DECIDED",
        taskId: item.taskId,
        answer,
        ...(feedback ? { feedback } : {}),
      });
    },
    [dispatch, item.taskId],
  );

  // Keyboard gate — Enter approves / Esc rejects, the web counterpart
  // of the TUI confirm navigation and the mock's ``Enter ↵ 确认 · Esc
  // 取消`` hint. Only for the plain approve/reject shape: options
  // lists (plan_builder) have no default choice, and while the
  // feedback textarea is open Enter/Esc belong to it. No conflict
  // with Composer: its send is gated by busy, its Esc listener steps
  // aside while awaitingConfirmation.
  useEffect(() => {
    if (item.resolved || options.length > 0 || feedbackOpen) return;
    // No default choice to bless while free input is the only control.
    if (freeInputOnly) return;
    if (!isFirstUnresolved) return;
    const onKeyDown = (e: KeyboardEvent) => {
      // An overlay (⌘K palette) already claimed this keystroke; held
      // keys must not machine-gun the gate.
      if (e.defaultPrevented || e.repeat) return;
      if (e.isComposing) return;
      // Yield to whatever holds focus: typing in the composer and
      // pressing Enter must NEVER approve a production injection, and
      // a focused button's native activation (Enter on the reject
      // button = reject) beats the global shortcut.
      const active = document.activeElement;
      if (
        active instanceof HTMLElement &&
        active.closest("button, input, textarea, select, a[href]")
      ) {
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        decide("approved");
      } else if (e.key === "Escape") {
        e.preventDefault();
        decide("rejected");
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [
    item.resolved,
    options.length,
    feedbackOpen,
    freeInputOnly,
    isFirstUnresolved,
    decide,
  ]);

  // Resolved → one-line chip in scrollback (dot + text, never a pill).
  if (item.resolved) {
    const answer = item.answer ?? "";
    const dot =
      answer === "approved"
        ? "bg-success-dot"
        : answer === "rejected"
          ? "bg-forge-text-faint"
          : "bg-forge-accent";
    const text =
      answer === "approved"
        ? `${t("confirm.armed_chip")} · ${t("confirm.armed_tail")}`
        : answer === "rejected"
          ? `${t("confirm.aborted_chip")} · ${t("confirm.aborted_tail")}`
          : `${t("confirm.answered")} · ${answer}`;
    return (
      <div className="flex items-center gap-2 text-xs text-forge-text-faint">
        <StatusDot className={dot} />
        {text}
      </div>
    );
  }

  // Button labels mirror the TUI's five-way node switch exactly.
  let yesLabel: string;
  let noLabel: string;
  if (node === "intent_confirm") {
    yesLabel = t("confirm.intent.proceed");
    noLabel = t("confirm.intent.refine");
  } else if (node === "confirmation_gate") {
    yesLabel = t("confirm.execution.proceed");
    noLabel = t("confirm.execution.cancel");
  } else if (node === "tool_screener") {
    yesLabel = t("confirm.targetChange.approve");
    noLabel = t("confirm.targetChange.reject");
  } else if (node === "plan_change_confirm") {
    yesLabel = t("confirm.planChange.approve");
    noLabel = t("confirm.planChange.reject");
  } else {
    yesLabel = t("confirm.proceed");
    noLabel = t("confirm.refine");
  }

  const submitFeedback = () => {
    const text = feedbackText.trim();
    if (!text) return;
    // plan_builder free_input: the text IS the resume value. L1/L2:
    // reject the gate and let Composer re-fire the text as a turn.
    if (isPlanBuilder) {
      decide(text);
    } else {
      decide("rejected", text);
    }
  };

  // Unresolved → action card, same frame as the context card so the
  // pair reads as one gate (mock confirm-card: actions live INSIDE
  // the card, not as a bare button row below it).
  return (
    <div
      className={`flex flex-col gap-2 rounded-card border bg-forge-card px-4 py-3 shadow-card ${
        hard ? "border-forge-amber-border" : "border-forge-border"
      }`}
    >
      {options.length > 0 ? (
        <div className="flex flex-wrap gap-2">
          {options.map((opt) =>
            opt.key === "free_input" ? (
              <button
                key={opt.key}
                type="button"
                onClick={() => setFeedbackOpen(true)}
                className="rounded-button border border-forge-border px-3 py-1.5 text-sm text-forge-text-secondary transition-colors hover:border-forge-accent"
              >
                {opt.label}
              </button>
            ) : (
              <button
                key={opt.key}
                type="button"
                onClick={() => decide(opt.key)}
                title={opt.description}
                className="rounded-button border border-forge-border px-3 py-1.5 text-sm text-forge-text transition-colors hover:border-forge-accent"
              >
                {opt.label}
                {opt.recommended ? " ⭐" : ""}
              </button>
            ),
          )}
        </div>
      ) : freeInputOnly ? null : (
        <div className="flex items-center gap-2">
          {/* mock btn-primary: deep-ink fill, the ONE filled button on
              the card; reject stays a ghost. */}
          <button
            type="button"
            onClick={() => decide("approved")}
            className="rounded-button bg-forge-ink px-4 py-1.5 text-sm font-medium text-white transition-colors hover:bg-black"
          >
            {yesLabel}
          </button>
          <button
            type="button"
            onClick={() => decide("rejected")}
            className="rounded-button border border-forge-border px-4 py-1.5 text-sm text-forge-text transition-colors hover:border-forge-accent"
          >
            {noLabel}
          </button>
          <button
            type="button"
            onClick={() => setFeedbackOpen((v) => !v)}
            className="px-2 py-1.5 text-sm text-forge-text-faint transition-colors hover:text-forge-accent"
          >
            {t("confirm.option.feedback")}
          </button>
          {/* Hint mirrors the keyboard gate's activation contract —
              hidden while the feedback box owns Enter/Esc. */}
          {!feedbackOpen ? (
            <span className="ml-auto text-xs text-forge-text-faint">
              {t("confirm.kbd_hint")}
            </span>
          ) : null}
        </div>
      )}
      {feedbackOpen || freeInputOnly ? (
        <div className="flex items-end gap-2">
          <textarea
            value={feedbackText}
            onChange={(e) => setFeedbackText(e.target.value)}
            rows={2}
            autoFocus
            className="flex-1 resize-none rounded-input border border-forge-border bg-forge-bg px-3 py-2 text-sm text-forge-text outline-none placeholder:text-forge-text-faint focus:border-forge-accent"
            placeholder={
              freeInputOnly
                ? t("confirm.plan_builder.free_input")
                : t("confirm.option.feedback")
            }
          />
          <button
            type="button"
            onClick={submitFeedback}
            disabled={!feedbackText.trim()}
            className="rounded-button bg-forge-accent px-4 py-1.5 text-sm font-medium text-white transition-colors hover:bg-forge-accent-hover disabled:cursor-not-allowed disabled:opacity-40"
          >
            {ui().send}
          </button>
        </div>
      ) : null}
    </div>
  );
}
