/**
 * Inline confirmation dialog — Forge × Operator redesign.
 *
 * Two tiers, by stakes:
 *
 *   Layer 1 ``intent_confirm`` (soft check)
 *     — no surrounding box. Top + bottom ━ rule, banner +
 *       ⓵ INTENT CHECK headline + body. Forge.fire (#E87841) is the
 *       chrome colour. The "soft" framing matches what the user is
 *       being asked: "did I read your intent right?"
 *
 *   Layer 2 ``confirmation_gate`` (hard check)
 *     — double-line border, forge.iron (#A8451E) — the same family
 *       hue as forge.fire but deeper, the "heated iron" shade. The
 *       border carries the same colour as ResultCard so the user
 *       reads "my decision flows straight into the result". The
 *       hard framing — "EXECUTE · this hits production" — matches
 *       the operator vocabulary of a flight-deck arming sequence.
 *
 *   Generic fallback (pre-payload server) — soft frame, generic
 *     preamble.
 *
 * Glyph language:
 *   ⓵ ⓶  numbered step indicators on the banner ("first/second gate")
 *   ●     filled lamp — used inside resolved-state chip
 *
 * Resolved state collapses the prompt to a one-line chip:
 *   "● ARMED · proceeding"   (approved — forge.fire / forge.iron)
 *   "● ABORTED · stopped"    (rejected — status.err / gray.500)
 * preserving a permanent marker in scrollback without re-occupying
 * the full card area.
 *
 * Key handling: ConfirmMessage itself does NOT capture keystrokes.
 * Composer owns useInput when streamState is ``waiting_confirmation``
 * so two components don't race for the same key.
 */

import { Box, Text } from "ink";
import { useBootCardWidth } from "../boot/BootCardFrame.js";
import {
  YesNoFeedbackSelect,
  type YesNoFeedbackAnswer,
} from "../shared/YesNoFeedbackSelect.js";
import { t } from "../../i18n/index.js";
import { useAppDispatch } from "../../state/store.js";
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";

// ---------------------------------------------------------------------------
// Tunables (shared with Python TUI's intent_confirm renderer)
// ---------------------------------------------------------------------------

const LOW_CONFIDENCE_THRESHOLD = 0.7;
const RISK_TIER_LOW_MAX = 2;
const RISK_TIER_MID_MAX = 9;

// ---------------------------------------------------------------------------
// Type-safe payload accessors
// ---------------------------------------------------------------------------

type Payload = Record<string, unknown> | undefined;

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

// ---------------------------------------------------------------------------
// Risk meter
// ---------------------------------------------------------------------------

interface RiskInfo {
  kind: "concrete" | "bounded" | "unbounded";
  target: string;
  count: number;
  descriptor: string;
  sample: string;
}

function computeRiskInfo(faultIntent: Record<string, unknown>): RiskInfo | null {
  const target = asString(faultIntent["target"]) || "resource";

  const names = asArray(faultIntent["names"]);
  if (names && names.length > 0) {
    const head = names.slice(0, 3).map(asString).filter(Boolean);
    let sample = head.join(", ");
    if (names.length > 3) sample += `, … (+${names.length - 3})`;
    return { kind: "concrete", target, count: names.length, descriptor: "", sample };
  }

  const params = asRecord(faultIntent["params"]) ?? {};
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

  if (faultIntent["labels"]) {
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
  if ((asString(faultIntent["scope"])).toLowerCase() === "namespace") {
    return { kind: "unbounded", target, count: 0, descriptor: "namespace", sample: "" };
  }
  return null;
}

interface RiskTier {
  color: string;
  label: string;
  sparkline: string;
}

function riskTier(count: number): RiskTier {
  if (count <= RISK_TIER_LOW_MAX) {
    return { color: Theme.status.ok, label: t("confirm.tier.low"), sparkline: "▁▂▃" };
  }
  if (count <= RISK_TIER_MID_MAX) {
    return { color: Theme.status.warn, label: t("confirm.tier.medium"), sparkline: "▃▅▆" };
  }
  return { color: Theme.status.err, label: t("confirm.tier.high"), sparkline: "▆▇█" };
}

// ---------------------------------------------------------------------------
// Confidence styling
// ---------------------------------------------------------------------------

function confidenceColor(c: number): string {
  if (c < 0.5) return Theme.status.err;
  if (c < LOW_CONFIDENCE_THRESHOLD) return Theme.status.warn;
  return Theme.status.ok;
}

function confidenceTierLabel(c: number): string {
  if (c < 0.5) return t("confirm.tier.low");
  if (c < LOW_CONFIDENCE_THRESHOLD) return t("confirm.tier.medium");
  return t("confirm.tier.high");
}

function lowConfidenceHint(
  faultIntent: Record<string, unknown>,
  confidence: number,
): string {
  const namespace = asString(faultIntent["namespace"]) || "default";
  const target = asString(faultIntent["target"]) || "?";
  const action = asString(faultIntent["action"]) || "?";
  const lead =
    confidence < 0.5
      ? t("confirm.confidence.warn_strong")
      : t("confirm.confidence.warn_soft");
  let msg = `${lead}：namespace=${namespace} · target=${target} · action=${action}`;
  const nsLower = namespace.toLowerCase();
  if (nsLower.includes("prod") || nsLower.includes("production")) {
    msg += `；${t("confirm.confidence.warn_prod")}`;
  }
  return msg;
}

// ---------------------------------------------------------------------------
// Safety badge
// ---------------------------------------------------------------------------

interface SafetyBadge {
  color: string;
  glyph: string;
  label: string;
}

function safetyBadge(status: string): SafetyBadge | null {
  if (!status) return null;
  switch (status) {
    case "safe":
    case "passed":
      return {
        color: Theme.status.ok,
        glyph: Icons.success,
        label: t("confirm.safety.safe"),
      };
    case "warning":
    case "confirm_required":
      return {
        color: Theme.status.warn,
        glyph: Icons.warning,
        label: t("confirm.safety.warning"),
      };
    case "blocked":
    case "rejected":
      return {
        color: Theme.status.err,
        glyph: Icons.fail,
        label: t("confirm.safety.blocked"),
      };
    default:
      return {
        color: Theme.status.warn,
        glyph: Icons.warning,
        label: status,
      };
  }
}

// ---------------------------------------------------------------------------
// Shared sub-components
// ---------------------------------------------------------------------------

const Field: React.FC<{ label: string; value: string }> = ({ label, value }) => {
  if (!value) return null;
  return (
    <Box>
      <Box minWidth={14} paddingRight={1}>
        <Text color={Theme.gray[500]}>{label}</Text>
      </Box>
      <Box flexGrow={1}>
        <Text color={Theme.text.primary} wrap="wrap">
          {value}
        </Text>
      </Box>
    </Box>
  );
};

/** Confidence progress bar — 10 segments of ▓/░ filled by the confidence
 *  value. Helps the user read the value at a glance instead of parsing
 *  the digits. Width-stable: always exactly 10 cells. */
const ConfidenceBar: React.FC<{ value: number; color: string }> = ({
  value,
  color,
}) => {
  const filled = Math.max(0, Math.min(10, Math.round(value * 10)));
  const empty = 10 - filled;
  return (
    <Text color={color} bold>
      {"█".repeat(filled)}
      <Text color={Theme.gray[700]}>{"░".repeat(empty)}</Text>
    </Text>
  );
};

/** Inverse chip — the operator badge family. Shared with ToolMessage's
 *  chip / future StatusChip primitive. Width-stable, CJK-safe.
 *  ``inverse + color`` paints a coloured background block; the
 *  surrounding spaces give it visible padding so it reads as a
 *  labelled tag rather than coloured text. */
const Chip: React.FC<{ color: string; children: string }> = ({
  color,
  children,
}) => (
  <Text inverse color={color} bold>
    {` ${children} `}
  </Text>
);

/** Bold horizontal rule — used by soft-tier frame as top/bottom edges
 *  and as section separators. Implementation note: a width-bound
 *  ``<Text>`` of ``━`` is more reliable than a ``Box`` with only
 *  ``borderTop`` for our ``cardWidth`` use case — Ink renders the
 *  Text at exact width, no border-rendering gotchas. */
const RuleHeavy: React.FC<{ width: number }> = ({ width }) => (
  <Text color={Theme.gray[700]}>{"━".repeat(Math.max(8, width))}</Text>
);

/** Light section rule inside a card body — separates "details" from
 *  "risk meter" from "actions". */
const SectionRule: React.FC = () => (
  <Box
    marginTop={1}
    borderStyle="single"
    borderTop
    borderBottom={false}
    borderLeft={false}
    borderRight={false}
    borderColor={Theme.gray[700]}
  />
);

// ---------------------------------------------------------------------------
// Frame — two tiers ("soft" / "hard") sharing the original glyph +
// title + preamble three-line header. The operator-vocabulary
// "banner + headline" pair from an earlier iteration was reverted
// per user feedback ("confirm-gate 卡片很丑陋，改回以前的，只是边
// 框颜色还是用这个") — the redesign read as too sparse, with the
// banner alone taking a row, a row of blank, then the rule, then the
// select. The pre-redesign tighter "glyph + title + preamble + body"
// stack is denser and more honest.
// ---------------------------------------------------------------------------

interface FrameProps {
  /** Title-row glyph (✻ for Layer 1 intent, ⚠ for Layer 2 gate). */
  glyph: string;
  /** Colour for the glyph — usually matches the tier accent so the
   *  glyph and title read as one unit. */
  glyphColor: string;
  /** Title text (no UPPERCASE / no banner format — plain title
   *  case, e.g. "Confirm intent" / "Confirm execution plan"). */
  title: string;
  taskId?: string;
  /** Dim subtitle line under the title. Plain language, optional. */
  preamble: string;
  /** Card body — fielded rows / risk / safety / etc. */
  children: React.ReactNode;
  /** Optional Select widget. When omitted, the action footer is
   *  skipped (used by ConfirmContextMessage — the prompt lives in a
   *  separate pending item below it). */
  actionPrompt?: React.ReactNode;
}

/** Soft tier (Layer 1 intent_confirm + generic fallback) — round
 *  border in forge.fire. Same chrome family as the boot cards /
 *  Tool cards. */
const ConfirmFrameSoft: React.FC<FrameProps> = ({
  glyph,
  glyphColor,
  title,
  taskId,
  preamble,
  children,
  actionPrompt,
}) => {
  const width = useBootCardWidth();
  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="column">
      <Box
        flexDirection="column"
        borderStyle="round"
        borderColor={Theme.forge.fire}
        paddingX={2}
        paddingY={0}
        width={width}
      >
        {/* Title row: glyph + title · taskId */}
        <Box>
          <Text color={glyphColor} bold>
            {glyph}{" "}
          </Text>
          <Text color={Theme.forge.fire} bold>
            {title}
          </Text>
          {taskId && (
            <Text color={Theme.gray[500]}>{`  ·  ${taskId}`}</Text>
          )}
        </Box>
        {/* Preamble: dim subtitle */}
        {preamble && (
          <Box marginTop={1}>
            <Text color={Theme.gray[500]}>{preamble}</Text>
          </Box>
        )}
        {/* Body */}
        <Box marginTop={1} flexDirection="column">
          {children}
        </Box>
        {/* Optional action footer */}
        {actionPrompt !== undefined && (
          <>
            <SectionRule />
            <Box marginTop={1} flexDirection="column">
              {actionPrompt}
            </Box>
          </>
        )}
      </Box>
    </Box>
  );
};

/** Hard tier (Layer 2 confirmation_gate) — double border in
 *  forge.iron, same colour family as ResultCard ("your decision
 *  flows straight into the result"). Same internal stack as the
 *  soft tier; only the border style + colour differ. */
const ConfirmFrameHard: React.FC<FrameProps> = ({
  glyph,
  glyphColor,
  title,
  taskId,
  preamble,
  children,
  actionPrompt,
}) => {
  const width = useBootCardWidth();
  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="column">
      <Box
        flexDirection="column"
        borderStyle="double"
        borderColor={Theme.forge.iron}
        paddingX={2}
        paddingY={0}
        width={width}
      >
        <Box>
          <Text color={glyphColor} bold>
            {glyph}{" "}
          </Text>
          <Text color={Theme.forge.iron} bold>
            {title}
          </Text>
          {taskId && (
            <Text color={Theme.gray[500]}>{`  ·  ${taskId}`}</Text>
          )}
        </Box>
        {preamble && (
          <Box marginTop={1}>
            <Text color={Theme.gray[500]}>{preamble}</Text>
          </Box>
        )}
        <Box marginTop={1} flexDirection="column">
          {children}
        </Box>
        {actionPrompt !== undefined && (
          <>
            <SectionRule />
            <Box marginTop={1} flexDirection="column">
              {actionPrompt}
            </Box>
          </>
        )}
      </Box>
    </Box>
  );
};

// ---------------------------------------------------------------------------
// Select wiring (unchanged from previous design)
// ---------------------------------------------------------------------------

function useConfirmSelect(
  taskId: string,
  yesLabel: string,
  noLabel: string,
  isFocused: boolean,
): React.JSX.Element {
  const dispatch = useAppDispatch();
  const handleConfirm = (
    answer: YesNoFeedbackAnswer,
    feedback?: string,
  ) => {
    if (answer === "feedback") {
      dispatch({
        type: "CONFIRM_USER_DECIDED",
        taskId,
        answer: "rejected",
        feedback: feedback ?? "",
      });
      return;
    }
    dispatch({
      type: "CONFIRM_USER_DECIDED",
      taskId,
      answer: answer === "yes" ? "approved" : "rejected",
    });
  };
  const handleCancel = () => {
    dispatch({ type: "CONFIRM_USER_DECIDED", taskId, answer: "rejected" });
  };
  return (
    <YesNoFeedbackSelect
      yesLabel={yesLabel}
      noLabel={noLabel}
      feedbackLabel={t("confirm.option.feedback")}
      isFocused={isFocused}
      onConfirm={handleConfirm}
      onCancel={handleCancel}
    />
  );
}

// ---------------------------------------------------------------------------
// Layer 1 — intent_confirm (soft tier)
// ---------------------------------------------------------------------------

const RiskMeterRow: React.FC<{ risk: RiskInfo }> = ({ risk }) => {
  if (risk.kind === "unbounded") {
    let descriptor: string;
    if (risk.descriptor === "labels") {
      descriptor = t("confirm.risk.scope.labels");
    } else if (risk.descriptor === "namespace") {
      descriptor = t("confirm.risk.scope.namespace");
    } else if (risk.descriptor.startsWith("percent:")) {
      const value = risk.descriptor.slice("percent:".length);
      descriptor = t("confirm.risk.scope.percent", { value });
    } else {
      descriptor = risk.descriptor;
    }
    return (
      <Box>
        <Box minWidth={14} paddingRight={1}>
          <Text color={Theme.gray[500]}>{t("confirm.field.risk")}</Text>
        </Box>
        <Box flexGrow={1}>
          <Text color={Theme.status.warn} bold>
            {risk.target} · {descriptor}
          </Text>
          <Text color={Theme.gray[500]}>
            {"  ("}
            {t("confirm.risk.runtime")}
            {")"}
          </Text>
        </Box>
      </Box>
    );
  }
  const tier = riskTier(risk.count);
  const countLabel =
    risk.kind === "bounded"
      ? `≤ ${risk.count} ${risk.target}`
      : `${risk.count} ${risk.target}`;
  return (
    <Box>
      <Box minWidth={14} paddingRight={1}>
        <Text color={Theme.gray[500]}>{t("confirm.field.risk")}</Text>
      </Box>
      <Box flexGrow={1}>
        <Text color={tier.color} bold>
          {tier.sparkline} {tier.label}
        </Text>
        <Text color={Theme.gray[500]}>{" · "}</Text>
        <Text color={tier.color} bold>
          {countLabel}
        </Text>
        {risk.sample && (
          <Text color={Theme.gray[500]}>{`  (${risk.sample})`}</Text>
        )}
      </Box>
    </Box>
  );
};

const ConfidenceRow: React.FC<{
  confidence: number;
  faultIntent: Record<string, unknown>;
}> = ({ confidence, faultIntent }) => {
  const color = confidenceColor(confidence);
  const tierText = confidenceTierLabel(confidence);
  const pct = `${(confidence * 100).toFixed(0)}%`;
  const lowConf = confidence < LOW_CONFIDENCE_THRESHOLD;
  return (
    <Box flexDirection="column">
      <Box>
        <Box minWidth={14} paddingRight={1}>
          <Text color={Theme.gray[500]}>
            {t("confirm.field.intent_confidence")}
          </Text>
        </Box>
        <Box flexGrow={1}>
          <ConfidenceBar value={confidence} color={color} />
          <Text color={color} bold>{`  ${pct}`}</Text>
          <Text color={Theme.gray[500]}>{`  ${tierText}`}</Text>
        </Box>
      </Box>
      {lowConf && (
        <Box>
          <Box minWidth={14} />
          <Box flexGrow={1}>
            <Text color={Theme.gray[500]}>{"└─ "}</Text>
            <Text color={color} bold>
              {Icons.warning}{" "}
            </Text>
            <Text color={color} wrap="wrap">
              {lowConfidenceHint(faultIntent, confidence)}
            </Text>
          </Box>
        </Box>
      )}
    </Box>
  );
};

const IntentConfirmCard: React.FC<{
  payload: Payload;
  taskId?: string;
}> = ({ payload, taskId }) => {
  const fi = asRecord(payload?.["fault_intent"]) ?? {};
  const params = asRecord(fi["params"]);
  const labels = asRecord(fi["labels"]);
  const names = asArray(fi["names"]);
  const confidenceRaw = payload?.["intent_confidence"];
  const confidence =
    typeof confidenceRaw === "number" && Number.isFinite(confidenceRaw)
      ? Math.max(0, Math.min(1, confidenceRaw))
      : 0;

  const paramsStr = params
    ? Object.entries(params)
        .map(([k, v]) => `${k}=${asString(v)}`)
        .join(", ")
    : "";
  const labelsStr = labels
    ? Object.entries(labels)
        .map(([k, v]) => `${k}=${asString(v)}`)
        .join(", ")
    : "";
  const namesStr = names
    ? names.map(asString).filter(Boolean).join(", ")
    : "";

  const risk = computeRiskInfo(fi);

  return (
    <ConfirmFrameSoft
      glyph={Icons.thinking}
      glyphColor={Theme.forge.fire}
      title={t("confirm.intent.title")}
      preamble={t("confirm.intent.preamble")}
      taskId={taskId}
    >
      <Field label={t("confirm.field.fault_type")} value={asString(fi["fault_type"])} />
      <Field label={t("confirm.field.scope")} value={asString(fi["scope"])} />
      <Field label={t("confirm.field.target")} value={asString(fi["target"])} />
      <Field label={t("confirm.field.action")} value={asString(fi["action"])} />
      <Field
        label={t("confirm.field.namespace")}
        value={asString(fi["namespace"])}
      />
      <Field label={t("confirm.field.labels")} value={labelsStr} />
      <Field label={t("confirm.field.names")} value={namesStr} />
      <Field label={t("confirm.field.params")} value={paramsStr} />
      <Field
        label={t("confirm.field.user_description")}
        value={asString(fi["user_description"])}
      />
      {risk && (
        <Box marginTop={1} flexDirection="column">
          <RiskMeterRow risk={risk} />
        </Box>
      )}
      {confidence > 0 && (
        <Box marginTop={1} flexDirection="column">
          <ConfidenceRow confidence={confidence} faultIntent={fi} />
        </Box>
      )}
    </ConfirmFrameSoft>
  );
};

// ---------------------------------------------------------------------------
// Layer 2 — confirmation_gate (hard tier)
// ---------------------------------------------------------------------------

const SafetyBadgeRow: React.FC<{ status: string; reason: string }> = ({
  status,
  reason,
}) => {
  const badge = safetyBadge(status);
  if (!badge) return null;
  return (
    <Box>
      <Box flexShrink={0}>
        <Chip color={badge.color}>{`${badge.glyph} ${badge.label}`}</Chip>
      </Box>
      {reason && (
        <Text color={Theme.gray[500]} wrap="wrap">{`  — ${reason}`}</Text>
      )}
    </Box>
  );
};

/** Plan summary inside a nested round box — visually anchors the
 *  most-scrutinized text in a Layer 2 confirm card. */
const PlanBlock: React.FC<{ summary: string; width: number }> = ({
  summary,
  width,
}) => (
  <Box flexDirection="column">
    <Box>
      <Text color={Theme.gray[500]}>── plan ──</Text>
    </Box>
    <Box
      marginTop={0}
      paddingX={1}
      paddingY={0}
      borderStyle="round"
      borderColor={Theme.gray[700]}
      width={Math.max(20, width - 8)}
    >
      <Text color={Theme.text.primary} wrap="wrap">
        {summary}
      </Text>
    </Box>
  </Box>
);

const ExecutionConfirmCard: React.FC<{ payload: Payload; taskId?: string }> = ({
  payload,
  taskId,
}) => {
  const width = useBootCardWidth();
  const skill = asString(payload?.["skill_name"]);
  const target = asRecord(payload?.["target"]);
  const planSummary = asString(payload?.["plan_summary"]);
  const safetyStatus = asString(payload?.["safety_status"]);
  const safetyReason = asString(payload?.["safety_reason"]);

  let targetStr = "";
  if (target) {
    const ns = asString(target["namespace"]);
    const namesArr = asArray(target["names"]);
    const namesStr = namesArr
      ? namesArr.map(asString).filter(Boolean).join(", ")
      : "";
    if (ns && namesStr) targetStr = `namespace=${ns}, names=[${namesStr}]`;
    else if (ns) targetStr = `namespace=${ns}`;
    else if (namesStr) targetStr = `names=[${namesStr}]`;
  }

  return (
    <ConfirmFrameHard
      glyph={Icons.warning}
      glyphColor={Theme.status.warn}
      title={t("confirm.execution.title")}
      preamble={t("confirm.execution.preamble")}
      taskId={taskId}
    >
      <Field label={t("confirm.field.skill")} value={skill} />
      <Field label={t("confirm.field.target")} value={targetStr} />
      {planSummary && (
        <Box marginTop={1}>
          <PlanBlock summary={planSummary} width={width} />
        </Box>
      )}
      {safetyStatus && (
        <Box marginTop={1} flexDirection="column">
          <SafetyBadgeRow status={safetyStatus} reason={safetyReason} />
        </Box>
      )}
    </ConfirmFrameHard>
  );
};

// ---------------------------------------------------------------------------
// Generic fallback (soft tier)
// ---------------------------------------------------------------------------

const GenericConfirmCard: React.FC<{ content: string; taskId?: string }> = ({
  content,
  taskId,
}) => {
  const body = content.trim() || t("confirm.body_empty");
  return (
    <ConfirmFrameSoft
      glyph={Icons.thinking}
      glyphColor={Theme.forge.fire}
      title={t("confirm.title")}
      preamble={t("confirm.generic.preamble")}
      taskId={taskId}
    >
      <Text color={Theme.text.primary} wrap="wrap">
        {body}
      </Text>
    </ConfirmFrameSoft>
  );
};

// ---------------------------------------------------------------------------
// Payload usability gates (unchanged)
// ---------------------------------------------------------------------------

function hasIntentContent(payload: Record<string, unknown>): boolean {
  const fi = asRecord(payload["fault_intent"]);
  if (!fi) return false;
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
  const conf = payload["intent_confidence"];
  if (typeof conf === "number" && Number.isFinite(conf)) return true;
  return false;
}

function hasExecutionContent(payload: Record<string, unknown>): boolean {
  if (asString(payload["skill_name"])) return true;
  if (asString(payload["plan_summary"])) return true;
  if (asString(payload["safety_status"])) return true;
  if (asString(payload["safety_reason"])) return true;
  const target = asRecord(payload["target"]);
  if (target && Object.keys(target).length > 0) return true;
  return false;
}

// ---------------------------------------------------------------------------
// Top-level dispatchers — Context (Static) + Prompt (pending)
// ---------------------------------------------------------------------------

export const ConfirmContextMessage: React.FC<{
  item: import("../../state/types.js").ConfirmContextItem;
}> = ({ item }) => {
  if (
    item.payload &&
    item.node === "intent_confirm" &&
    hasIntentContent(item.payload)
  ) {
    return <IntentConfirmCard payload={item.payload} taskId={item.taskId} />;
  }
  if (
    item.payload &&
    item.node === "confirmation_gate" &&
    hasExecutionContent(item.payload)
  ) {
    return <ExecutionConfirmCard payload={item.payload} taskId={item.taskId} />;
  }
  return <GenericConfirmCard content={item.content} taskId={item.taskId} />;
};

/**
 * Pending-only renderer for the live confirm select widget.
 *
 * Layer 2 lives inside its own bordered frame (matched to
 * ExecutionConfirmCard's chrome) so the prompt visually attaches to
 * the context card. Layer 1 / generic use a soft frame for the same
 * reason.
 *
 * Resolved-state branch: once the user answers, the prompt collapses
 * to a single-line chip ("● ARMED · proceeding" / "● ABORTED · stopped")
 * so the timeline keeps a permanent operator-vocabulary marker
 * without re-occupying the full card area.
 */
export const ConfirmPromptMessage: React.FC<{
  item: import("../../state/types.js").ConfirmPromptItem;
  isFocused?: boolean;
}> = ({ item, isFocused = true }) => {
  let yesLabel: string;
  let noLabel: string;
  if (item.node === "intent_confirm") {
    yesLabel = t("confirm.intent.proceed");
    noLabel = t("confirm.intent.refine");
  } else if (item.node === "confirmation_gate") {
    yesLabel = t("confirm.execution.proceed");
    noLabel = t("confirm.execution.cancel");
  } else {
    yesLabel = t("confirm.proceed");
    noLabel = t("confirm.refine");
  }
  const select = useConfirmSelect(item.taskId, yesLabel, noLabel, isFocused);
  // Width is read unconditionally so the hook call order stays stable
  // across resolved→unresolved transitions. Only the unresolved branch
  // uses it; the resolved chip line doesn't need a width.
  const cardWidth = useBootCardWidth();

  if (item.resolved) {
    const ok = item.answer === "approved";
    const chipColor = ok ? Theme.forge.fire : Theme.status.err;
    const chipText = ok ? t("confirm.armed_chip") : t("confirm.aborted_chip");
    const tail = ok ? t("confirm.armed_tail") : t("confirm.aborted_tail");
    return (
      <Box paddingLeft={2} marginTop={1}>
        <Text color={Theme.gray[700]}>{"╰─▶  "}</Text>
        <Chip color={chipColor}>{`● ${chipText}`}</Chip>
        <Text color={Theme.gray[500]}>{`  ·  ${tail}`}</Text>
      </Box>
    );
  }

  // Active prompt — render the select widget alone inside a small
  // round-bordered box. Banner / headline / taskId are intentionally
  // OMITTED: the corresponding context card right above this prompt
  // already carries them, so repeating them here would double-print
  // the title.
  //
  // Border colour is **dim gray** regardless of tier, not the
  // forge.fire / forge.iron used by the context card above. The
  // visual reasoning: the context card carries the *content* the
  // user must read (fault details, plan, safety status) — that
  // earns the loud brand border. The prompt is just the *action
  // surface* (select Y/N/feedback). Painting it in the same loud
  // colour produces the user-reported "审美疲劳" — two big
  // forge-coloured frames stacked feels noisy and makes the prompt
  // visually compete with the content it's responding to. The dim
  // gray frame fades into the background, so the user's eye lands
  // on the highlighted ❯ option and the bold select labels instead
  // of the chrome.
  //
  // Border style still tracks tier (round soft / double hard) so
  // the "soft check vs hard check" hierarchy is preserved.
  const isHard = item.node === "confirmation_gate";
  const tierColor = Theme.gray[700];
  const tierStyle: "double" | "round" = isHard ? "double" : "round";
  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="column">
      <Box
        flexDirection="column"
        borderStyle={tierStyle}
        borderColor={tierColor}
        paddingX={2}
        paddingY={0}
        width={cardWidth}
      >
        {select}
      </Box>
    </Box>
  );
};
