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
import { memo } from "react";
import { useBootCardWidth } from "../boot/BootCardFrame.js";
import {
  YesNoFeedbackSelect,
  type YesNoFeedbackAnswer,
} from "../shared/YesNoFeedbackSelect.js";
import { Select, type SelectItem } from "../shared/Select.js";
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
// Shared layout constants — keep every row in the card aligned to a
// single 14-column "label gutter" so Field values and list-row body
// text both start at column 14 from the card's inner edge.
//
//   Field rows:    [ label .... 14col .... ] [ value flexGrow ]
//   List rows:     [ glyph 3 ][ name 11 ]   [ body  flexGrow ]
//   Indented hint: [ spacer ...... 14col .. ] [ hint  flexGrow ]
//
// The pre-cleanup code had list rows on a 28-col name gutter which
// pushed list bodies to column 31 — visually out of step with Field
// values at column 14. Unifying these makes every section vertically
// align inside the card.
// ---------------------------------------------------------------------------
const FIELD_LABEL_WIDTH = 14;
const LIST_GLYPH_WIDTH = 3;
const LIST_NAME_WIDTH = FIELD_LABEL_WIDTH - LIST_GLYPH_WIDTH;

// ---------------------------------------------------------------------------
// Shared sub-components
// ---------------------------------------------------------------------------

const Field: React.FC<{
  label: string;
  value: string;
  /** Override the label colour (default ``text.secondary``). Used by
   *  status-tinted rows like Recovery notes "Why partial" (warn) or
   *  Failure analysis "Cause" (err). */
  labelColor?: string;
  /** Override the value colour (default ``text.primary``). */
  valueColor?: string;
  /** Wrap multi-line values (default true — most confirm fields are
   *  short; setting false yields ``truncate-end`` for ultra-narrow
   *  metadata where wrap would look messy). */
  wrap?: boolean;
}> = ({
  label,
  value,
  labelColor = Theme.gray[500],
  valueColor = Theme.text.primary,
  wrap = true,
}) => {
  if (!value) return null;
  return (
    <Box>
      <Box minWidth={FIELD_LABEL_WIDTH} paddingRight={1}>
        <Text color={labelColor}>{label}</Text>
      </Box>
      <Box flexGrow={1}>
        <Text color={valueColor} wrap={wrap ? "wrap" : "truncate-end"}>
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

/** Title chip + heading row — v3 design. Mirrors the bracket-chip
 *  language used in ToolMessage (`[✓ kubectl]`), so confirm and tool
 *  cards share the same visual vocabulary.
 *
 *  Layout:  [glyph CHIP]  Title text   ·  task-id
 *           └ gray bracket
 *                 └ tier-color glyph + bold chip label (e.g. ⚠ EXECUTE)
 *                              └ same tier-color bold title — chip + title
 *                                read as one visual unit; the colour also
 *                                serves as the tier signal (fire / warn)
 *                                now that both frames share one border colour.
 *                                          └ dim task id */
const TitleChip: React.FC<{
  glyph: string;
  glyphColor: string;
  chipLabel: string;
  title: string;
  taskId?: string;
}> = ({ glyph, glyphColor, chipLabel, title, taskId }) => (
  <Box>
    <Text color={Theme.gray[500]}>[</Text>
    <Text color={glyphColor} bold>{`${glyph} ${chipLabel}`}</Text>
    <Text color={Theme.gray[500]}>{"]"}</Text>
    <Text color={glyphColor} bold>{`  ${title}`}</Text>
    {taskId && (
      <Text color={Theme.gray[500]}>{`  ·  ${taskId}`}</Text>
    )}
  </Box>
);

/** Section heading — visual delimiter inside a confirm card.
 *  Renders as `── label` in dim gray; lighter than a full SectionRule
 *  and carries a label so the user knows what the next block is.
 *  Used for "Decision signals" / "Execution plan" / "Safety check"
 *  sub-sections in the v3 layout. */
const SectionHeading: React.FC<{ label: string }> = ({ label }) => (
  <Box marginTop={1}>
    <Text color={Theme.gray[500]}>{"── "}</Text>
    <Text color={Theme.gray[500]} bold>
      {label}
    </Text>
  </Box>
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
  /** Short uppercase chip label rendered inside the [glyph LABEL]
   *  bracket chip (v3). E.g. "INTENT" / "EXECUTE" / "CONFIRM". */
  chipLabel: string;
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
 *  border in forge.dim (fire desaturated ~30%). The container
 *  itself stays warm-toned but recedes, letting the saturated
 *  forge.fire glyph + title inside carry the brand accent. */
const ConfirmFrameSoft: React.FC<FrameProps> = ({
  glyph,
  glyphColor,
  chipLabel,
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
        borderColor={Theme.forge.dim}
        paddingX={2}
        paddingY={0}
        width={width}
      >
        {/* Title row: [glyph CHIP]  title · taskId  (v3 bracket chip) */}
        <TitleChip
          glyph={glyph}
          glyphColor={glyphColor}
          chipLabel={chipLabel}
          title={title}
          taskId={taskId}
        />
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

/** Hard tier (Layer 2 confirmation_gate) — same dim-warm round
 *  border as the soft tier. Tier is now signaled by the title
 *  glyph (✻ vs ⚠) and the in-card chip colour (forge.fire vs
 *  status.warn) rather than by border weight/colour. The previous
 *  double-line in forge.iron read as too heavy and clashed with
 *  the rest of the chrome (boot cards / tool rails all use single
 *  lines too). */
const ConfirmFrameHard: React.FC<FrameProps> = ({
  glyph,
  glyphColor,
  chipLabel,
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
        borderColor={Theme.forge.dim}
        paddingX={2}
        paddingY={0}
        width={width}
      >
        <TitleChip
          glyph={glyph}
          glyphColor={glyphColor}
          chipLabel={chipLabel}
          title={title}
          taskId={taskId}
        />
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
        <Box minWidth={FIELD_LABEL_WIDTH} paddingRight={1} flexShrink={0}>
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
      <Box minWidth={FIELD_LABEL_WIDTH} paddingRight={1} flexShrink={0}>
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
        <Box minWidth={FIELD_LABEL_WIDTH} paddingRight={1} flexShrink={0}>
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
          <Box minWidth={FIELD_LABEL_WIDTH} flexShrink={0} />
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

  // P2-7: Layer 1 audit trail. ``intent_reasoning`` is the LLM's own
  // explanation for the classification; we surface it ONLY on
  // low-confidence turns (the user is most likely to second-guess
  // the call there). ``clarification_round`` tells the user we're
  // already iterating — useful so they don't think "did my message
  // get lost?". Both fields are optional; absent means no audit row.
  const intentReasoning = asString(payload?.["intent_reasoning"]);
  const clarificationRoundRaw = payload?.["clarification_round"];
  const clarificationRound =
    typeof clarificationRoundRaw === "number" && Number.isFinite(clarificationRoundRaw)
      ? Math.max(0, Math.floor(clarificationRoundRaw))
      : 0;
  const showReasoning =
    intentReasoning.length > 0 && confidence < LOW_CONFIDENCE_THRESHOLD;
  const hasAuditTrail = showReasoning || clarificationRound > 0;

  return (
    <ConfirmFrameSoft
      glyph={Icons.thinking}
      glyphColor={Theme.forge.fire}
      chipLabel={t("confirm.intent.chip")}
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
      {hasAuditTrail && (
        <Box marginTop={1} flexDirection="column">
          {showReasoning && (
            <Field
              label={t("confirm.field.intent_reasoning")}
              value={intentReasoning}
              wrap
            />
          )}
          {clarificationRound > 0 && (
            <Field
              label={t("confirm.field.clarification_round")}
              value={t("confirm.clarification.label", { n: clarificationRound })}
              labelColor={Theme.status.warn}
              valueColor={Theme.status.warn}
            />
          )}
        </Box>
      )}
    </ConfirmFrameSoft>
  );
};

// ---------------------------------------------------------------------------
// Layer 2 — confirmation_gate (hard tier)
// ---------------------------------------------------------------------------

/** One safety-check line — glyph + label + dim detail. Designed for
 *  the v3 "Safety check" section as a list item. Total prefix width
 *  (glyph 3 + name 11) sums to FIELD_LABEL_WIDTH so the detail column
 *  vertically aligns with Field values elsewhere in the card. */
const SafetyCheckRow: React.FC<{
  glyph: string;
  color: string;
  label: string;
  detail?: string;
}> = ({ glyph, color, label, detail }) => (
  <Box>
    <Box minWidth={LIST_GLYPH_WIDTH} flexShrink={0}>
      <Text color={color}>{glyph}</Text>
    </Box>
    <Box minWidth={LIST_NAME_WIDTH} flexShrink={0} paddingRight={1}>
      <Text color={Theme.gray[500]} wrap="truncate-end">
        {label}
      </Text>
    </Box>
    {detail && (
      <Box flexGrow={1}>
        <Text color={Theme.text.primary} wrap="wrap">
          {detail}
        </Text>
      </Box>
    )}
  </Box>
);

/** v3 Safety section — renders the (currently single-result) safety
 *  check as a list item. Forward-compatible: when backend payload
 *  starts carrying ``safety_checks: [{name, status, detail}]`` the
 *  list can grow without touching consumers. For now the single
 *  status/reason pair maps to one row. */
const SafetyCheckList: React.FC<{ status: string; reason: string }> = ({
  status,
  reason,
}) => {
  const badge = safetyBadge(status);
  if (!badge) return null;
  return (
    <Box flexDirection="column" marginTop={1}>
      <SafetyCheckRow
        glyph={badge.glyph}
        color={badge.color}
        label={badge.label}
        detail={reason || undefined}
      />
    </Box>
  );
};

/** Severity → display attributes for target_health_report rows. */
function healthRowVisuals(severity: string): { color: string; glyph: string } {
  switch (severity) {
    case "ok":
      return { color: Theme.status.ok, glyph: Icons.success };
    case "warn":
      return { color: Theme.status.warn, glyph: Icons.warning };
    case "block":
      return { color: Theme.status.err, glyph: Icons.fail };
    default:
      return { color: Theme.text.secondary, glyph: Icons.bullet };
  }
}

const ExecutionConfirmCard: React.FC<{ payload: Payload; taskId?: string }> = ({
  payload,
  taskId,
}) => {
  const skill = asString(payload?.["skill_name"]);
  const target = asRecord(payload?.["target"]);
  // ``plan_summary`` is intentionally NOT read — the full plan
  // markdown is too tall to render inline (was triggering Ink cursor
  // desync). Only the ``plan_path`` file pointer is surfaced; the
  // user reads the full plan off disk via ``cat`` / editor.
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

  // P0-2: structured params dict surfaced as "k=v, k=v" string under
  // the Parameters section. Mirrors how Layer 1 formats fault_intent
  // params so the two cards read consistently.
  const paramsDict = asRecord(payload?.["params"]);
  const paramsStr = paramsDict
    ? Object.entries(paramsDict)
        .map(([k, v]) => `${k}=${asString(v)}`)
        .filter((s) => !s.endsWith("="))
        .join(", ")
    : "";

  // P0-1: target_health_report — DiskPressure / Evicted / etc.
  // Schema (HealthReport.to_dict in target_health.py):
  //   { target, overall, issues: [{severity, code, message, duration_hint}], summary }
  const healthReport = asRecord(payload?.["target_health_report"]);
  const healthOverall = asString(healthReport?.["overall"]);
  const healthSummary = asString(healthReport?.["summary"]);
  const healthCheckedDetail = asString(healthReport?.["checked_detail"]);
  const healthIssuesRaw = healthReport?.["issues"];
  const healthIssues = Array.isArray(healthIssuesRaw)
    ? healthIssuesRaw
        .map((r) => (typeof r === "object" && r !== null ? (r as Record<string, unknown>) : null))
        .filter((r): r is Record<string, unknown> => r !== null)
    : [];
  // Only show the section when there's an issue to report — a clean
  // ``overall=ok`` with no issues adds no information for the user.
  const hasHealthIssues = healthIssues.length > 0 || (healthOverall && healthOverall !== "ok");

  // E18: feasibility_report — injection headroom assessment.
  const feasibilityReport = asRecord(payload?.["feasibility_report"]);
  const feasSeverity = asString(feasibilityReport?.["severity"]);
  const feasMessage = asString(feasibilityReport?.["message"]);
  const feasRecommendation = asString(feasibilityReport?.["recommendation"]);
  const feasHeadroom = feasibilityReport?.["headroom"];
  const feasCurrentValue = asString(feasibilityReport?.["current_value"]);
  const feasLimitValue = asString(feasibilityReport?.["limit_value"]);
  const feasTargetValue = asString(feasibilityReport?.["target_value"]);
  const hasFeasibilityIssue = feasibilityReport != null && feasSeverity !== "" && feasSeverity !== "ok";

  // P1-4: conflict_uids — structured list of existing experiment UIDs.
  const conflictUidsRaw = payload?.["conflict_uids"];
  const conflictUids = Array.isArray(conflictUidsRaw)
    ? conflictUidsRaw.map(asString).filter(Boolean)
    : [];

  // P2-8: pipeline_attempt / is_complex / plan_path.
  const pipelineAttemptRaw = payload?.["pipeline_attempt"];
  const pipelineAttempt =
    typeof pipelineAttemptRaw === "number" && Number.isFinite(pipelineAttemptRaw)
      ? Math.max(0, Math.floor(pipelineAttemptRaw))
      : 0;
  const planPath = asString(payload?.["plan_path"]);
  // ``is_complex`` is the formal-plan-track flag (true → the agent
  // routed through ``save_fault_plan`` and produced a multi-section
  // plan markdown). Surfaced as a small chip only when true so simple
  // plans don't carry a redundant "simple plan" badge — silence is
  // the happy-path baseline.
  const isComplex = payload?.["is_complex"] === true;

  // L1 semantic classification (fault_type / scope / target / action).
  // Previously L2 only carried ``target`` (namespace + names), so the
  // operator had to reverse-engineer "is this a mem-load?" from
  // ``params`` keys. Surfacing the L1 classification here makes the
  // fault category visible at a glance.
  //
  // ``fault_intent`` may be ``null`` (dry-run / clarification-incomplete
  // path on the producer — confirmation_gate.py sets None when
  // FaultSpec has no derivable fault_type). Render only when fault_type
  // is non-empty; the (scope/target/action) triple is informational
  // and may be empty.
  const faultIntent = asRecord(payload?.["fault_intent"]);
  const faultType = asString(faultIntent?.["fault_type"]);
  let faultBrief = "";
  if (faultType) {
    const triple = [
      asString(faultIntent?.["scope"]),
      asString(faultIntent?.["target"]),
      asString(faultIntent?.["action"]),
    ].filter(Boolean).join("/");
    faultBrief = triple ? `${faultType}  (${triple})` : faultType;
  }

  // E10 — multi-dimensional numeric safety score. The score dict, when
  // present, carries per-dimension { value, explanation } entries plus
  // an aggregated `overall` (0-100) and `level` (low/medium/high/critical).
  // Absent payload → panel is skipped (backward-compatible with older
  // server builds that don't compute the score yet).
  const safetyScore = asRecord(payload?.["safety_score"]);

  // Safety status placement is adaptive (v3): when there's a real
  // problem (warning / blocked) we float the row up to the top of
  // the card so the user sees it before scanning the plan. When the
  // status is "safe" / empty we keep it at the bottom as the closing
  // "Safety check" line — quiet confirmation, not a top-of-mind alert.
  const badge = safetyStatus ? safetyBadge(safetyStatus) : null;
  const hasProblem =
    safetyStatus === "warning" ||
    safetyStatus === "confirm_required" ||
    safetyStatus === "blocked" ||
    safetyStatus === "rejected";
  // Guard: don't emit the "Execution plan" section heading when there
  // is no plan content to render. Payloads with only a safety_status
  // (e.g. an early policy-block reaching the gate before plan compose)
  // would otherwise render a dangling heading with an empty body.
  //
  // ``planSummary`` (the inline 500-char markdown body) is intentionally
  // NOT in the guard — it's also NOT rendered anywhere below. The
  // verbose plan content lived inline before but blew the card past
  // viewport rows (50+ rows total → Ink cursor desync → ghost copies
  // in scrollback). We now surface ONLY the saved-file path
  // (``planPath``), which is one row regardless of plan length;
  // ``cat <plan_path>`` gets the full markdown on disk.
  const hasPlanContent = Boolean(skill || targetStr || planPath || faultBrief);
  // ``hasParamsContent`` retired — Parameters section now renders
  // ALWAYS, with ``—`` placeholder when the agent didn't compute
  // structured params. Keeping the section heading visible signals
  // "we did look at params" instead of leaving the reader to guess.

  return (
    <ConfirmFrameHard
      glyph={Icons.warning}
      glyphColor={Theme.status.warn}
      chipLabel={t("confirm.execution.chip")}
      title={t("confirm.execution.title")}
      preamble={t("confirm.execution.preamble")}
      taskId={taskId}
    >
      {/* Adaptive top alert — only fires for non-safe statuses */}
      {hasProblem && badge && (
        <Box marginTop={1}>
          <SafetyCheckRow
            glyph={badge.glyph}
            color={badge.color}
            label={badge.label}
            detail={safetyReason || undefined}
          />
        </Box>
      )}

      {hasPlanContent && (
        <>
          <Box marginTop={1} flexDirection="column">
            {/* Fault classification — sits ABOVE skill / target because
             *  "what fault" is the highest-level operator question.
             *  Previously the reader had to reverse-engineer the fault
             *  category from ``params`` keys (e.g. ``mem-percent`` →
             *  mem-load). Surfacing the L1-derived fault_type +
             *  (scope/target/action) triple removes that reverse-step. */}
            {faultBrief && (
              <Field
                label={t("confirm.field.fault")}
                value={faultBrief}
              />
            )}
            <Field label={t("confirm.field.skill")} value={skill} />
            <Field label={t("confirm.field.target")} value={targetStr} />
            {/* Plan body lives on disk at ``planPath`` — surfacing the
             *  path here keeps the card single-row-per-field tall
             *  regardless of how long the actual plan markdown is. The
             *  inline ``plan_summary`` field (up to 500 chars / ~15
             *  rows of markdown) was removed because it routinely
             *  pushed the card past viewport rows and triggered Ink's
             *  cursor desync (ghost copies of dyn frame in scrollback).
             *  ``cat <planPath>`` is the escape hatch for full content. */}
            {planPath && (
              <Field
                label={t("confirm.field.plan_path")}
                value={t("confirm.plan_saved", { path: planPath })}
              />
            )}
            {/* P2-8: attempt N — sits inside the plan section as an
             *  audit-style row (warn-coloured but no bold so it
             *  doesn't compete with title chip). Previously inlined
             *  into the title which made "第 2 次尝试" read as
             *  louder than the operational verb. */}
            {pipelineAttempt > 1 && (
              <Field
                label={t("confirm.field.attempt")}
                value={t("confirm.attempt.label", { n: pipelineAttempt })}
                labelColor={Theme.status.warn}
                valueColor={Theme.status.warn}
              />
            )}
            {/* is_complex chip — formal-plan-track flag (true → the
             *  agent ran save_fault_plan and produced a multi-section
             *  plan markdown). Renders only when true so simple plans
             *  don't carry a redundant "simple plan" badge. */}
            {isComplex && (
              <Field
                label={t("confirm.field.complexity")}
                value={t("confirm.complexity.complex")}
                labelColor={Theme.status.warn}
                valueColor={Theme.status.warn}
              />
            )}
          </Box>
        </>
      )}

      {/* P0-2: structured params. ALWAYS shown — even when the agent
       *  didn't compute structured params (`paramsStr` empty) the
       *  section heading + a `—` placeholder communicates "we did
       *  look at parameters, there just aren't any". Previously
       *  gated on ``hasParamsContent`` which hid the section when
       *  empty, leaving the reader to wonder whether the check
       *  happened. */}
      <Box marginTop={1}>
        <Field
          label={t("confirm.field.params")}
          value={paramsStr || t("confirm.params.none")}
          wrap
        />
      </Box>

      {/* P0-1: target_health_report — DiskPressure / Evicted / etc.
       *  ALWAYS shown (was previously gated on ``hasHealthIssues``).
       *  Three visual states for the body:
       *    - issues present       → list each issue with severity
       *      glyph + code + message (row layout: glyph(3) +
       *      code(11) + message(flex); long codes wrap to two lines)
       *    - check ran, all clear → single ✓ row "all targets healthy"
       *    - check didn't run     → single — row "check not run"
       *      (happens when ``settings.target_health_check_enabled``
       *      is false on the server side, so the payload's
       *      ``target_health_report`` field is null) */}
      <Box marginTop={1} flexDirection="column">
        {hasHealthIssues ? (
          <>
            {healthIssues.map((issue, i) => {
              const sev = asString(issue["severity"]);
              const visuals = healthRowVisuals(sev);
              const code = asString(issue["code"]);
              const message = asString(issue["message"]);
              const durationHint = asString(issue["duration_hint"]);
              const detail = [message, durationHint && `(${durationHint})`]
                .filter(Boolean)
                .join(" ");
              return (
                <SafetyCheckRow
                  key={i}
                  glyph={visuals.glyph}
                  color={visuals.color}
                  label={t("confirm.field.health")}
                  detail={detail ? `${detail} [${code}]` : code}
                />
              );
            })}
          </>
        ) : healthReport === null ? (
          // Server didn't run the check (target_health_check_enabled
          // is false / older server build). Single ``—`` row matches
          // the "Parameters" empty fallback style.
          <SafetyCheckRow
            glyph={Icons.bullet}
            color={Theme.text.secondary}
            label={t("confirm.field.health")}
            detail={t("confirm.health.not_run")}
          />
        ) : (
          <SafetyCheckRow
            glyph={Icons.success}
            color={Theme.status.ok}
            label={t("confirm.field.health")}
            detail={healthCheckedDetail ? `${t("confirm.health.all_clear")} (${healthCheckedDetail})` : t("confirm.health.all_clear")}
          />
        )}
      </Box>

      {/* E18: feasibility assessment — ALWAYS shown (matches
       *  target_health pattern: ok/issue/not-run). */}
      <Box flexDirection="column">
        {hasFeasibilityIssue ? (
          <>
            <SafetyCheckRow
              glyph={feasSeverity === "impossible" ? Icons.fail : feasSeverity === "skipped" ? "○" : Icons.warning}
              color={feasSeverity === "impossible" ? Theme.status.err : feasSeverity === "skipped" ? Theme.text.secondary : Theme.status.warn}
              label={t("confirm.field.feasibility")}
              detail={feasMessage}
            />
            {feasRecommendation && (
              <Box>
                <Box minWidth={LIST_GLYPH_WIDTH} flexShrink={0} />
                <Box flexGrow={1}>
                  <Text color={Theme.text.secondary} wrap="wrap">
                    {feasRecommendation}
                  </Text>
                </Box>
              </Box>
            )}
          </>
        ) : feasibilityReport === null ? (
          <SafetyCheckRow
            glyph={Icons.bullet}
            color={Theme.text.secondary}
            label={t("confirm.field.feasibility")}
            detail={t("confirm.feasibility.not_run")}
          />
        ) : (
          <SafetyCheckRow
            glyph={Icons.success}
            color={Theme.status.ok}
            label={t("confirm.field.feasibility")}
            detail={`${t("confirm.feasibility.all_clear")} (headroom ${typeof feasHeadroom === "number" ? `${Math.round(feasHeadroom * 100)}%` : "—"}, 当前 ${feasCurrentValue || "—"}, 目标 ${feasTargetValue || "—"})`}
          />
        )}
      </Box>

      {/* P1-4: structured conflict_uids list. UID rows use the
       *  glyph + name two-column layout but with no inline label —
       *  the uid IS the name. Hint row indents to the field-label
       *  column so it visually nests under the list. */}
      {conflictUids.length > 0 && (
        <>
          <Box marginTop={1} flexDirection="column">
            {conflictUids.map((uid, i) => (
              <Box key={i}>
                <Box minWidth={LIST_GLYPH_WIDTH} flexShrink={0}>
                  <Text color={Theme.status.warn}>{Icons.warning}</Text>
                </Box>
                <Box flexGrow={1}>
                  <Text color={Theme.gray[300]}>{uid}</Text>
                </Box>
              </Box>
            ))}
            <Box>
              <Box minWidth={LIST_GLYPH_WIDTH} flexShrink={0} />
              <Box flexGrow={1}>
                <Text color={Theme.text.secondary}>
                  {t("confirm.conflicts.hint")}
                </Text>
              </Box>
            </Box>
          </Box>
        </>
      )}

      {/* E10 — multi-dimensional safety score panel. Overall score +
       *  each dimension (blast_radius / frequency / time / topology)
       *  with one-line explanation. Color of the overall row tracks
       *  level (low=ok, medium/high=warn, critical=err). Section is
       *  skipped entirely when payload.safety_score is absent (older
       *  server build), so old confirm cards render unchanged. */}
      {safetyScore && (
        <>
          <Box marginTop={1} flexDirection="column">
            {(() => {
              const overall = asString(safetyScore["overall"]) || "0";
              const level = asString(safetyScore["level"]) || "low";
              const levelColor =
                level === "critical"
                  ? Theme.status.err
                  : level === "high" || level === "medium"
                    ? Theme.status.warn
                    : Theme.status.ok;
              return (
                <Field
                  label={t("safety_score.overall")}
                  value={`${overall}/100 (${t(`safety_score.level.${level}`)})`}
                  valueColor={levelColor}
                />
              );
            })()}
            {(["blast_radius", "frequency", "time", "topology"] as const).map(
              (dim) => {
                const d = asRecord(safetyScore[dim]);
                if (!d) return null;
                const value = asString(d["value"]) || "0";
                const explanation = asString(d["explanation"]) || "";
                return (
                  <Field
                    key={dim}
                    label={t(`safety_score.${dim}`)}
                    value={`${value} — ${explanation}`}
                    wrap
                  />
                );
              },
            )}
          </Box>
        </>
      )}

      {/* Bottom Safety check section — quiet placement, only when no
       *  prominent top alert is rendered (avoids showing safety twice). */}
      {!hasProblem && badge && (
        <SafetyCheckList status={safetyStatus} reason={safetyReason} />
      )}
    </ConfirmFrameHard>
  );
};

// ---------------------------------------------------------------------------
// Target change card (drift confirmation)
// ---------------------------------------------------------------------------

const TargetChangeCard: React.FC<{ payload: Payload; taskId?: string }> = ({
  payload,
}) => {
  const cardWidth = useBootCardWidth();
  const p = payload ?? {};
  const reason = asString(p["reason"]);
  const original = asRecord(p["original"]);
  const proposed = asRecord(p["proposed"]);

  const renderTarget = (target: Record<string, unknown> | null) => {
    if (!target) return <Text dimColor>—</Text>;
    const ns = asString(target["namespace"]) || "default";
    const names = target["names"];
    const labels = target["labels"];
    const parts: string[] = [`ns=${ns}`];
    if (Array.isArray(names) && names.length > 0) {
      parts.push(`names=[${names.join(", ")}]`);
    }
    if (labels && typeof labels === "object" && Object.keys(labels).length > 0) {
      const pairs = Object.entries(labels as Record<string, string>)
        .map(([k, v]) => `${k}=${v}`)
        .join(", ");
      parts.push(`labels={${pairs}}`);
    }
    return <Text>{parts.join("  ")}</Text>;
  };

  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="column">
      <Box
        flexDirection="column"
        borderStyle="double"
        borderColor={Theme.status.warn}
        paddingX={2}
        paddingY={0}
        width={cardWidth}
      >
        <Text bold color={Theme.status.warn}>
          {t("confirm.targetChange.title")}
        </Text>
        {reason ? <Text dimColor>{reason}</Text> : null}
        <Box marginTop={1} flexDirection="column">
          <Text bold>{t("confirm.targetChange.original")}</Text>
          {renderTarget(original)}
        </Box>
        <Box marginTop={1} flexDirection="column">
          <Text bold>{t("confirm.targetChange.proposed")}</Text>
          {renderTarget(proposed)}
        </Box>
      </Box>
    </Box>
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
      chipLabel={t("confirm.generic.chip")}
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
// Plan builder — selection card (interactive options)
// ---------------------------------------------------------------------------

const PlanSelectionCard: React.FC<{ payload: Payload; taskId?: string }> = ({
  payload,
  taskId,
}) => {
  const question = asString(payload?.["question"]);
  return (
    <ConfirmFrameSoft
      glyph={Icons.thinking}
      glyphColor={Theme.forge.fire}
      chipLabel="PLAN"
      title={t("confirm.plan_builder.title")}
      preamble=""
      taskId={taskId}
    >
      <Text color={Theme.text.primary} wrap="wrap">
        {question || t("confirm.plan_builder.default_question")}
      </Text>
    </ConfirmFrameSoft>
  );
};

function usePlanBuilderSelect(
  taskId: string,
  payload: Record<string, unknown> | undefined,
  isFocused: boolean,
): React.JSX.Element {
  const dispatch = useAppDispatch();
  const rawOptions = (payload?.["options"] ?? []) as Array<{
    key: string;
    label: string;
    description?: string;
    recommended?: boolean;
  }>;

  const items: SelectItem<string>[] = rawOptions.map((opt) => ({
    value: opt.key,
    label: `${opt.label}${opt.description ? `（${opt.description}）` : ""}${opt.recommended ? " ⭐" : ""}`,
    hasFeedback: opt.key === "free_input",
  }));

  // Fallback: if no options, show a single free-input
  if (items.length === 0) {
    items.push({ value: "free_input", label: t("confirm.plan_builder.free_input"), hasFeedback: true });
  }

  const handleSelect = (value: string, feedback?: string) => {
    // For free_input, send the typed text as the answer
    const answer = value === "free_input" && feedback != null ? feedback : value;
    dispatch({
      type: "CONFIRM_USER_DECIDED",
      taskId,
      answer,
    });
  };

  const handleCancel = () => {
    dispatch({ type: "CONFIRM_USER_DECIDED", taskId, answer: "rejected" });
  };

  return (
    <Select<string>
      items={items}
      isFocused={isFocused}
      onSelect={handleSelect}
      onCancel={handleCancel}
    />
  );
}

// ---------------------------------------------------------------------------
// Top-level dispatchers — Context (Static) + Prompt (pending)
// ---------------------------------------------------------------------------

const ConfirmContextMessageInternal: React.FC<{
  item: import("../../state/types.js").ConfirmContextItem;
}> = ({ item }) => {
  if (item.payload && item.node === "plan_builder") {
    return <PlanSelectionCard payload={item.payload} taskId={item.taskId} />;
  }
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
  if (
    item.payload &&
    item.node === "tool_screener" &&
    asString(item.payload["type"]) === "target_change"
  ) {
    return <TargetChangeCard payload={item.payload} taskId={item.taskId} />;
  }
  return <GenericConfirmCard content={item.content} taskId={item.taskId} />;
};

// React.memo: ConfirmContextMessage carries the heaviest layout
// (IntentConfirmCard / ExecutionConfirmCard with risk meters, safety
// rows, etc). Item ref is stable post-dispatch — pending churn must
// not re-render the multi-tier card chain.
export const ConfirmContextMessage = memo(ConfirmContextMessageInternal);

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
const ConfirmPromptMessageInternal: React.FC<{
  item: import("../../state/types.js").ConfirmPromptItem;
  isFocused?: boolean;
}> = ({ item, isFocused = true }) => {
  // Width is read unconditionally so the hook call order stays stable
  // across resolved→unresolved transitions.
  const cardWidth = useBootCardWidth();

  // Resolved state — collapsed chip
  if (item.resolved) {
    if (item.node === "plan_builder") {
      // Plan builder: show the selected option's label (not just the key)
      const rawAnswer = item.answer || "—";
      const opts = (item.payload?.["options"] ?? []) as Array<{key: string; label: string}>;
      const matched = opts.find((o) => o.key === rawAnswer);
      const display = matched ? matched.label : rawAnswer;
      return (
        <Box paddingLeft={2} marginTop={1}>
          <Text color={Theme.gray[700]}>{"╰─▶  "}</Text>
          <Chip color={Theme.forge.fire}>{`● ${display}`}</Chip>
        </Box>
      );
    }
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

  // Plan builder: dynamic options select
  if (item.node === "plan_builder") {
    const planSelect = usePlanBuilderSelect(item.taskId, item.payload, isFocused);
    return (
      <Box paddingLeft={2} marginTop={1} flexDirection="column">
        <Box
          flexDirection="column"
          borderStyle="round"
          borderColor={Theme.gray[700]}
          paddingX={2}
          paddingY={0}
          width={cardWidth}
        >
          {planSelect}
        </Box>
      </Box>
    );
  }

  // Standard confirm select (intent_confirm / confirmation_gate / generic)
  let yesLabel: string;
  let noLabel: string;
  if (item.node === "intent_confirm") {
    yesLabel = t("confirm.intent.proceed");
    noLabel = t("confirm.intent.refine");
  } else if (item.node === "confirmation_gate") {
    yesLabel = t("confirm.execution.proceed");
    noLabel = t("confirm.execution.cancel");
  } else if (item.node === "tool_screener") {
    yesLabel = t("confirm.targetChange.approve");
    noLabel = t("confirm.targetChange.reject");
  } else {
    yesLabel = t("confirm.proceed");
    noLabel = t("confirm.refine");
  }
  const select = useConfirmSelect(item.taskId, yesLabel, noLabel, isFocused);

  const isHard = item.node === "confirmation_gate" || item.node === "tool_screener";
  const tierColor = item.node === "tool_screener" ? Theme.status.warn : Theme.gray[700];
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

// React.memo: pending confirm prompts churn through MainContent
// re-renders on every streaming event. Default shallow compare on
// (item, isFocused) gates the re-render. useConfirmSelect's hooks
// (useAppDispatch, internal useState) stay correct because the
// component still runs on actual prop changes — only no-op renders
// are skipped.
export const ConfirmPromptMessage = memo(ConfirmPromptMessageInternal);
