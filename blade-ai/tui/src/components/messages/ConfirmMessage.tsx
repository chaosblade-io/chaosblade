/**
 * Inline confirmation dialog rendered as a pending item.
 *
 * Three variants share one frame:
 *
 *   - Layer 1 ``intent_confirm``    — LLM's parsed fault_intent
 *                                     + risk meter + confidence tier
 *                                     + low-confidence warning tail.
 *   - Layer 2 ``confirmation_gate`` — generated plan + safety badge.
 *   - Generic fallback              — older servers without ``payload``.
 *
 * The frame uses ``borderStyle="double"`` (``╔═══╗``) to set confirm
 * cards apart from the round-bordered chrome elsewhere in the TUI —
 * confirmation is a stop-and-decide moment, the heavier border draws
 * the eye. Width matches ``useBootCardWidth`` so the card lines up
 * with the welcome / doctor / pending-tasks cards above.
 *
 * Visual structure (mirrors Python TUI's intent_confirm renderer):
 *
 *   ╔══ ✻ Confirm intent · t-abc123 ════════════════╗
 *   ║   <preamble: dim line>                          ║
 *   ║                                                  ║
 *   ║   <field rows>                                   ║
 *   ║                                                  ║
 *   ║   Risk: ▆▇█ high · 12 pods                      ║   (Layer 1 only)
 *   ║   Confidence: 0.55 中                            ║
 *   ║   └─ ⚠ <warning hint>                           ║
 *   ║                                                  ║
 *   ║   Safety: ✓ SAFE — <reason>                     ║   (Layer 2 only)
 *   ║                                                  ║
 *   ║   ─────────────────────                         ║
 *   ║   [Y] proceed   [N] cancel                      ║
 *   ╚══════════════════════════════════════════════════╝
 *
 * Key handling: ConfirmMessage itself does NOT capture keystrokes —
 * the parent ``Composer`` owns the useInput call when streamState is
 * ``waiting_confirmation``. This avoids two components racing for
 * the same key (Ink's useInput fires every active subscriber).
 */

import { Box, Text } from "ink";
import { useBootCardWidth } from "../boot/BootCardFrame.js";
import {
  YesNoFeedbackSelect,
  type YesNoFeedbackAnswer,
} from "../shared/YesNoFeedbackSelect.js";
import { t } from "../../i18n/index.js";
import { useAppDispatch } from "../../state/store.js";
// ConfirmItem was the legacy single-item type; the M2 split moved its
// fields into ``ConfirmContextItem`` (Static) + ``ConfirmPromptItem``
// (pending). Both are imported lazily from inside the new top-level
// component types below to avoid a top-of-file circular import.
import { Theme } from "../../theme/colors.js";
import { Icons } from "../../theme/icons.js";

// ---------------------------------------------------------------------------
// Tunables (shared with Python TUI's intent_confirm renderer)
// ---------------------------------------------------------------------------

/** Below this confidence value the panel surfaces a warning tail under
 *  the confidence row. Matches Python ``LOW_CONFIDENCE_THRESHOLD``. */
const LOW_CONFIDENCE_THRESHOLD = 0.7;

/** Risk-tier breakpoints by absolute count. Matches Python
 *  ``_RISK_TIER_LOW_MAX`` / ``_RISK_TIER_MID_MAX``. */
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
// Risk meter — ported from Python ``_compute_risk_info`` / ``_risk_tier``
// ---------------------------------------------------------------------------

interface RiskInfo {
  /** ``"concrete"`` (exact count from names) | ``"bounded"`` (from
   *  ``params.count``) | ``"unbounded"`` (label / namespace / percent). */
  kind: "concrete" | "bounded" | "unbounded";
  target: string;
  count: number;
  /** Only set for ``"unbounded"``: i18n key fragment ``labels`` / ``namespace`` /
   *  ``percent``, or the raw fragment for unknown scope shapes. */
  descriptor: string;
  /** Only set for ``"concrete"``: first 1–3 names, comma-joined. */
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
    // ``▁▁▁`` (Lower One Eighth Block × 3) renders as a thin baseline
    // smudge — barely visible in most terminal fonts. ``▁▂▃`` is a
    // small ascending ramp that still reads as "low" but actually
    // shows up. The ``▃`` end of low equals the ``▃`` start of
    // medium below, giving a continuous step-up across tiers.
    return { color: Theme.status.ok, label: t("confirm.tier.low"), sparkline: "▁▂▃" };
  }
  if (count <= RISK_TIER_MID_MAX) {
    // ``▃▅▆`` (was ``▁▃▅``) so the medium tier starts where low ends
    // and ends where high begins (``▆`` is the first of ``▆▇█``).
    // The visual "fill height" climbs uniformly from low → high.
    return { color: Theme.status.warn, label: t("confirm.tier.medium"), sparkline: "▃▅▆" };
  }
  return { color: Theme.status.err, label: t("confirm.tier.high"), sparkline: "▆▇█" };
}

// ---------------------------------------------------------------------------
// Confidence styling — ported from Python ``_confidence_style`` / hint
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

/** Build the field-aware warning hint shown below the confidence row.
 *  Names the specific fields the user should sanity-check, and adds a
 *  prod-namespace flag when one fires. Matches Python
 *  ``_low_confidence_hint``. */
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
// Safety badge — ported from Python ``confirm.py`` safety branch
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
      // Unknown status — show the raw value with a warning hue so the
      // user notices it's outside the known set.
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
        <Text color={Theme.text.secondary}>{label}</Text>
      </Box>
      <Box flexGrow={1}>
        <Text color={Theme.text.primary} wrap="wrap">
          {value}
        </Text>
      </Box>
    </Box>
  );
};

/** Thin horizontal rule that separates body from footer. Built as a
 *  Box with only ``borderTop`` so Ink draws ``─`` chars across the
 *  available width — no manual length math, CJK-safe. */
const SectionRule: React.FC = () => (
  <Box
    marginTop={1}
    borderStyle="single"
    borderTop={true}
    borderBottom={false}
    borderLeft={false}
    borderRight={false}
    borderColor={Theme.text.secondary}
  />
);

interface FrameProps {
  /** Glyph drawn before the title. ``Icons.thinking`` (✻) for Layer 1,
   *  ``Icons.warning`` (⚠) for Layer 2. */
  glyph: string;
  /** Glyph color — usually the title color so the two read as one
   *  visual unit. */
  glyphColor: string;
  title: string;
  taskId?: string;
  /** Dim subtitle line printed under the title. */
  preamble: string;
  /** Body content — fielded rows / risk meter / safety badge / etc. */
  children: React.ReactNode;
  /** Optional interactive Select widget. When provided, rendered below
   *  a section rule inside the bordered box. When omitted (the
   *  ``ConfirmContextMessage`` use-case where the prompt has been
   *  promoted into its own pending item), the rule + select section
   *  is skipped entirely so the context card reads as a static record
   *  in scrollback. */
  actionPrompt?: React.ReactNode;
}

const ConfirmFrame: React.FC<FrameProps> = ({
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
        borderColor={Theme.border.focused}
        paddingX={2}
        paddingY={0}
        width={width}
      >
        {/* Title row: ✻ Title · taskId */}
        <Box>
          <Text color={glyphColor} bold>
            {glyph}{" "}
          </Text>
          <Text color={Theme.text.accent} bold>
            {title}
          </Text>
          {taskId && (
            <Text color={Theme.text.secondary}> · {taskId}</Text>
          )}
        </Box>
        {/* Preamble: dim subtitle */}
        <Box marginTop={1}>
          <Text color={Theme.text.secondary}>{preamble}</Text>
        </Box>
        {/* Body */}
        <Box marginTop={1} flexDirection="column">
          {children}
        </Box>
        {/* Section rule + Select widget (replaces the static
         *  [Y] / [N] hint row — Select owns the keyboard now).
         *  Suppressed entirely when ``actionPrompt`` is omitted — the
         *  context-only render path (``ConfirmContextMessage``) drops
         *  this block because the prompt now lives in its own
         *  pending-only ``ConfirmPromptMessage`` card below. */}
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

/**
 * Hook: wire a ``YesNoFeedbackSelect`` for a given ConfirmItem.
 *
 * Returns the JSX to drop into ``ConfirmFrame``'s ``actionPrompt``
 * slot. The reusable ``YesNoFeedbackSelect`` primitive owns the
 * keyboard + the three-option layout; this hook is the
 * ConfirmMessage-specific wiring that maps the user's answer onto
 * the LangGraph resume vocabulary ("approved" / "rejected") and
 * dispatches into the pubsub slot Composer's effect watches.
 *
 * Mapping:
 *   - "yes"      → ``answer: "approved"`` (operation proceeds)
 *   - "no"       → ``answer: "rejected"`` (operation cancelled)
 *   - "feedback" → ``answer: "rejected"`` + ``feedback: <text>``;
 *                  Composer's effect fires a follow-up
 *                  ``submitTurn(text)`` so the agent treats the
 *                  user's typed reply as their next message
 *
 * Only the Y/N labels are scenario-specific (intent-confirm
 * Layer 1 vs confirmation-gate Layer 2 vs generic fallback) — the
 * "Tell agent something else…" feedback option uses the same
 * ``confirm.option.feedback`` i18n key everywhere.
 */
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
    // Esc / Ctrl+C in options mode == picking "no". The server's
    // confirmation_gate routes the graph accordingly and the SSE
    // ends naturally. The user can still hit Esc at the input
    // prompt afterwards if they want to fully bail out of the
    // session — that path remains in Composer.
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
// Layer 1 — intent_confirm
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
          <Text color={Theme.text.secondary}>{t("confirm.field.risk")}</Text>
        </Box>
        <Box flexGrow={1}>
          <Text color={Theme.status.warn} bold>
            {risk.target} · {descriptor}
          </Text>
          <Text color={Theme.text.secondary}>
            {"  ("}
            {t("confirm.risk.runtime")}
            {")"}
          </Text>
        </Box>
      </Box>
    );
  }
  // concrete | bounded
  const tier = riskTier(risk.count);
  const countLabel =
    risk.kind === "bounded"
      ? `≤ ${risk.count} ${risk.target}`
      : `${risk.count} ${risk.target}`;
  return (
    <Box>
      <Box minWidth={14} paddingRight={1}>
        <Text color={Theme.text.secondary}>{t("confirm.field.risk")}</Text>
      </Box>
      <Box flexGrow={1}>
        <Text color={tier.color} bold>
          {tier.sparkline} {tier.label}
        </Text>
        <Text color={Theme.text.secondary}>{" · "}</Text>
        <Text color={tier.color} bold>
          {countLabel}
        </Text>
        {risk.sample && (
          <Text color={Theme.text.secondary}>{`  (${risk.sample})`}</Text>
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
          <Text color={Theme.text.secondary}>
            {t("confirm.field.intent_confidence")}
          </Text>
        </Box>
        <Box flexGrow={1}>
          <Text color={color} bold>
            {pct}
          </Text>
          <Text color={Theme.text.secondary}>{`  (${tierText})`}</Text>
        </Box>
      </Box>
      {lowConf && (
        <Box>
          <Box minWidth={14} />
          <Box flexGrow={1}>
            <Text color={Theme.text.secondary}>{"└─ "}</Text>
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
}> = ({
  payload,
  taskId,
}) => {
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
    <ConfirmFrame
      glyph={Icons.thinking}
      glyphColor={Theme.text.accent}
      title={t("confirm.intent.title")}
      taskId={taskId}
      preamble={t("confirm.intent.preamble")}
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
    </ConfirmFrame>
  );
};

// ---------------------------------------------------------------------------
// Layer 2 — confirmation_gate
// ---------------------------------------------------------------------------

const SafetyBadgeRow: React.FC<{ status: string; reason: string }> = ({
  status,
  reason,
}) => {
  const badge = safetyBadge(status);
  if (!badge) return null;
  return (
    <Box>
      <Box minWidth={14} paddingRight={1}>
        <Text color={Theme.text.secondary}>{t("confirm.field.safety")}</Text>
      </Box>
      <Box flexGrow={1}>
        {/* ``flexShrink={0}`` keeps the badge label from being clipped
         *  when the reason text wraps. Ink v7's flex layout measures
         *  Text children more strictly than v5: without the explicit
         *  flexShrink the badge ``WARNING`` was getting truncated to
         *  ``WARNIN`` to make room for the wrapping reason text in
         *  the same row. The badge is short (≤8 chars) so always
         *  yielding its full width is the right call — the reason
         *  has flexGrow's slack to wrap into. */}
        <Box flexShrink={0}>
          <Text color={badge.color} bold>
            {badge.glyph} {badge.label}
          </Text>
        </Box>
        {reason && (
          <Text color={Theme.text.secondary} wrap="wrap">{`  — ${reason}`}</Text>
        )}
      </Box>
    </Box>
  );
};

const ExecutionConfirmCard: React.FC<{ payload: Payload; taskId?: string }> = ({
  payload,
  taskId,
}) => {
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
    <ConfirmFrame
      glyph={Icons.warning}
      glyphColor={Theme.status.warn}
      title={t("confirm.execution.title")}
      taskId={taskId}
      preamble={t("confirm.execution.preamble")}
    >
      <Field label={t("confirm.field.skill")} value={skill} />
      <Field label={t("confirm.field.target")} value={targetStr} />
      {planSummary && (
        <Box flexDirection="column">
          <Box>
            <Box minWidth={14} paddingRight={1}>
              <Text color={Theme.text.secondary}>
                {t("confirm.field.plan_summary")}
              </Text>
            </Box>
            <Box flexGrow={1}>
              <Text color={Theme.text.primary} wrap="wrap">
                {planSummary}
              </Text>
            </Box>
          </Box>
        </Box>
      )}
      {safetyStatus && (
        <Box marginTop={1} flexDirection="column">
          <SafetyBadgeRow status={safetyStatus} reason={safetyReason} />
        </Box>
      )}
    </ConfirmFrame>
  );
};

// ---------------------------------------------------------------------------
// Generic fallback (pre-payload server)
// ---------------------------------------------------------------------------

const GenericConfirmCard: React.FC<{ content: string; taskId?: string }> = ({
  content,
  taskId,
}) => {
  const body = content.trim() || t("confirm.body_empty");
  return (
    <ConfirmFrame
      glyph={Icons.thinking}
      glyphColor={Theme.text.accent}
      title={t("confirm.title")}
      taskId={taskId}
      preamble={t("confirm.generic.preamble")}
    >
      <Text color={Theme.text.primary} wrap="wrap">
        {body}
      </Text>
    </ConfirmFrame>
  );
};

// ---------------------------------------------------------------------------
// Payload usability gates
// ---------------------------------------------------------------------------

/** True when the intent_confirm payload carries at least one field a
 *  user can read. Defends against the chrome-only "empty card" bug
 *  that hits when ``fault_intent`` is ``{}`` or all whitelist fields
 *  are blank — without this gate, the dispatcher would still pick
 *  IntentConfirmCard, every <Field> would short-circuit to null,
 *  RiskMeterRow / ConfidenceRow would also bail, and the user would
 *  see a card with title + preamble + footer and no body.
 */
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

/** True when the confirmation_gate payload carries at least one field
 *  worth rendering. Same defense as hasIntentContent — empty payload
 *  → fall back to GenericConfirmCard so the agent's pre-formatted
 *  ``content`` body is shown instead of a blank chrome.
 */
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
// Top-level dispatcher
// ---------------------------------------------------------------------------

/**
 * Static-history renderer for the heavy confirm context (plan summary,
 * safety warning, fault intent table). Burns into scrollback once at
 * ``CONFIRM_RECEIVED`` time and never updates — the live select widget
 * lives in a separate ``ConfirmPromptMessage`` pending item below.
 */
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
 * Pending-only renderer for the live confirm select widget. Pairs with
 * ``ConfirmContextMessage`` (Static, in scrollback) — the context card
 * holds the heavy read-only plan / safety / intent table; this card
 * holds only the action prompt. Splitting the two keeps the dynamic
 * frame bounded so multi-row warnings stop pushing it past viewport
 * rows during the human-in-the-loop wait.
 *
 * Resolved-state branch: once the user answers, the prompt becomes a
 * one-line "✓ confirmed / ✗ cancelled" marker so it lands in
 * scrollback as a permanent record of the decision.
 */
export const ConfirmPromptMessage: React.FC<{
  item: import("../../state/types.js").ConfirmPromptItem;
  /** Set ``false`` for the SECOND-and-later unresolved prompt when
   *  pending happens to carry more than one (rare — server contract
   *  resolves Layer 1 before emitting Layer 2 — but defensive). Only
   *  the focused prompt's Select consumes keyboard events; the rest
   *  render as inert chrome until they bubble up. */
  isFocused?: boolean;
}> = ({ item, isFocused = true }) => {
  // Hook calls live UNCONDITIONALLY at the top of the component so a
  // single ``ConfirmPromptItem`` instance that mutates from
  // ``resolved=false`` to ``resolved=true`` (CONFIRM_RESOLVED dispatch
  // flips the flag in place before flushLeadingStable migrates the
  // item) doesn't change the hook call count between renders. React's
  // "Rendered fewer hooks than expected" guard would otherwise crash
  // the moment the user presses Enter on a confirm. Yes/No labels are
  // computed in line so the per-node branch doesn't add another hook.
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
  const width = useBootCardWidth();

  if (item.resolved) {
    const ok = item.answer === "approved";
    return (
      <Box paddingLeft={2} marginTop={1}>
        <Text color={ok ? Theme.status.ok : Theme.text.secondary}>
          {ok ? Icons.success : Icons.fail}{" "}
          {ok
            ? t("confirm.answered")
            : t("confirm.answered_rejected")}
        </Text>
      </Box>
    );
  }

  return (
    <Box paddingLeft={2} marginTop={1} flexDirection="column">
      <Box
        flexDirection="column"
        borderStyle="round"
        borderColor={Theme.border.focused}
        paddingX={2}
        paddingY={0}
        width={width}
      >
        <Box marginTop={0} flexDirection="column">
          {select}
        </Box>
      </Box>
    </Box>
  );
};
