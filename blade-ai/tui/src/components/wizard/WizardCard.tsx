/**
 * Wizard card — first-run configuration UI (Ink, all-TS).
 *
 * Architecture:
 *   · Pure rendering. ALL business logic (URL/key/kubeconfig
 *     validation, kube-context discovery, model presets, config
 *     persistence) lives in Python and is reached via
 *     ``api/wizard.ts`` HTTP client.
 *   · Local state via ``useReducer(wizardReducer, initialWizardState)``.
 *     Side effects are dispatched as async ops (validation calls,
 *     preset fetch, save) inside ``useEffect`` blocks.
 *   · Visual system matches v3 token set:
 *       - border:    forge.dim round
 *       - title:     [glyph CHIP] bracket chip + same-color bold title
 *       - sections:  ── label
 *       - fields:    14-col gutter
 *       - select:    [A][B][C] letter chips
 *       - stepper:   [1✓]─[2◐]─[3 ] lamp + number
 *
 * Keyboard:
 *   Enter          advance (or commit text input)
 *   ←  / Shift+Tab back one step
 *   1-8            jump to that step (only if already completed)
 *   A-F            radio shortcut (model: A-E presets + F custom)
 *   Esc            cancel wizard (in-text-mode: bail out of input)
 */

import { Box, Text, useInput } from "ink";
import InkSpinner from "ink-spinner";
import { useEffect, useReducer, useState } from "react";

import { WizardClient, type ValidationResult } from "../../api/wizard.js";
import { Icons } from "../../theme/icons.js";
import { Theme } from "../../theme/colors.js";
import { t } from "../../i18n/index.js";
import {
  STEP_ORDER,
  canAdvanceFrom,
  emptyValues,
  initialWizardState,
  stepIndex,
  stepNumber,
  wizardReducer,
  type StepKey,
  type WizardState,
} from "../../state/wizard.js";
import { useBootCardWidth } from "../boot/BootCardFrame.js";

// ── Layout constants (mirror ConfirmMessage v3) ──────────────────────
const FIELD_LABEL_WIDTH = 14;
const LIST_GLYPH_WIDTH = 3;
const LIST_NAME_WIDTH = FIELD_LABEL_WIDTH - LIST_GLYPH_WIDTH;
const TOTAL_STEPS = STEP_ORDER.length;

// ── Shared widgets (private to wizard) ────────────────────────────────

/** [glyph CHIP]  Title — bracket chip + same-color bold title. */
const TitleChip: React.FC<{
  glyph: string;
  chipLabel: string;
  title: string;
}> = ({ glyph, chipLabel, title }) => (
  <Box>
    <Text color={Theme.gray[500]}>[</Text>
    <Text color={Theme.forge.fire} bold>
      {`${glyph} ${chipLabel}`}
    </Text>
    <Text color={Theme.gray[500]}>{"]"}</Text>
    <Text color={Theme.forge.fire} bold>{`  ${title}`}</Text>
  </Box>
);

/** ── label — dim section divider. */
const SectionHeading: React.FC<{ label: string }> = ({ label }) => (
  <Box marginTop={1}>
    <Text color={Theme.gray[500]}>{"── "}</Text>
    <Text color={Theme.gray[500]} bold>
      {label}
    </Text>
  </Box>
);

/** Field row — label 14col + value flex. */
const Field: React.FC<{
  label: string;
  value: string;
  labelColor?: string;
  valueColor?: string;
  valueBold?: boolean;
  wrap?: boolean;
}> = ({
  label,
  value,
  labelColor = Theme.gray[500],
  valueColor = Theme.text.primary,
  valueBold = false,
  wrap = true,
}) => {
  if (!value) return null;
  return (
    <Box>
      <Box minWidth={FIELD_LABEL_WIDTH} paddingRight={1} flexShrink={0}>
        <Text color={labelColor}>{label}</Text>
      </Box>
      <Box flexGrow={1}>
        <Text
          color={valueColor}
          bold={valueBold}
          wrap={wrap ? "wrap" : "truncate-end"}
        >
          {value}
        </Text>
      </Box>
    </Box>
  );
};

/** Stepper row — [1✓]─[2◐]─[3 ]─... + click-target labels. */
const StepperBar: React.FC<{
  state: WizardState;
}> = ({ state }) => {
  const labels: Record<StepKey, string> = {
    welcome: t("wizard.step.welcome"),
    model: t("wizard.step.model"),
    api_url: t("wizard.step.api_url"),
    api_key: t("wizard.step.api_key"),
    kubeconfig: t("wizard.step.kubeconfig"),
    kube_context: t("wizard.step.kube_context"),
    permission: t("wizard.step.permission"),
    summary: t("wizard.step.summary"),
  };
  const currentIdx = stepIndex(state.currentStep);
  return (
    <Box>
      {STEP_ORDER.map((key, i) => {
        const isCurrent = i === currentIdx;
        const isCompleted = state.completedSteps.has(key);
        const num = i + 1;
        let cell: React.ReactNode;
        if (isCurrent) {
          cell = (
            <Text color={Theme.forge.fire} bold>{`[${num}◐]`}</Text>
          );
        } else if (isCompleted) {
          cell = (
            <Text color={Theme.status.ok}>
              {`[${num}`}
              <Text bold>{"✓"}</Text>
              {`]`}
            </Text>
          );
        } else {
          cell = <Text color={Theme.gray[500]}>{`[${num} ]`}</Text>;
        }
        const sep =
          i < STEP_ORDER.length - 1 ? (
            <Text color={Theme.gray[700]}>─</Text>
          ) : null;
        return (
          <Box key={key}>
            {cell}
            {sep}
          </Box>
        );
      })}
      <Box flexGrow={1} />
      <Text color={Theme.gray[500]}>
        {`${labels[state.currentStep]} · ${currentIdx + 1}/${TOTAL_STEPS}`}
      </Text>
    </Box>
  );
};

/** Validation status row — glyph + colored message (+ spinner on busy). */
const ValidationStatus: React.FC<{ step: StepKey; state: WizardState }> = ({
  step,
  state,
}) => {
  const v = state.validations[step];
  if (v.status === "idle") return null;
  let color: string;
  switch (v.status) {
    case "ok":
      color = Theme.status.ok;
      break;
    case "warn":
      color = Theme.status.warn;
      break;
    case "error":
      color = Theme.status.err;
      break;
    case "busy":
    default:
      color = Theme.forge.fire;
  }
  return (
    <Box>
      <Box minWidth={LIST_GLYPH_WIDTH} flexShrink={0}>
        {v.status === "busy" ? (
          <Text color={color}>
            <InkSpinner type="dots" />
          </Text>
        ) : (
          <Text color={color}>
            {v.status === "ok"
              ? Icons.success
              : v.status === "warn"
                ? Icons.warning
                : Icons.fail}
          </Text>
        )}
      </Box>
      <Box flexGrow={1}>
        <Text color={color} wrap="wrap">
          {v.message || t("wizard.validation.in_progress")}
        </Text>
      </Box>
    </Box>
  );
};

/** Returned-from hint banner (when user jumped back to this step). */
const ReturnedHint: React.FC<{ state: WizardState; step: StepKey }> = ({
  state,
  step,
}) => {
  if (state.returnedFromStep !== step) return null;
  return (
    <Box>
      <Text color={Theme.status.warn}>{Icons.warning}</Text>
      <Text color={Theme.status.warn}>{` ${t("wizard.returned_hint")}`}</Text>
    </Box>
  );
};

/** Hint footer (dim gray small text). */
const HintRow: React.FC<{ text: string }> = ({ text }) => (
  <Box marginTop={1}>
    <Text color={Theme.text.secondary} wrap="wrap">
      {text}
    </Text>
  </Box>
);

// ── Radio row (matches Select.tsx ``[A] label`` chip style) ───────────

interface RadioOption {
  letter: string;
  label: string;
  hint?: string;
  selected: boolean;
  /** ``true`` means the focused row, distinct from the "selected" mark. */
  focused: boolean;
}

const RadioRow: React.FC<{ option: RadioOption }> = ({ option }) => {
  const color = option.focused ? Theme.forge.fire : Theme.text.secondary;
  return (
    <Box>
      <Box minWidth={4} flexShrink={0}>
        <Text color={color} bold={option.focused}>
          {`[${option.letter}] `}
        </Text>
      </Box>
      <Box flexGrow={1}>
        <Text color={color} bold={option.focused}>
          {option.label}
        </Text>
        {option.hint && (
          <Text color={Theme.gray[500]}>{`  ${option.hint}`}</Text>
        )}
      </Box>
    </Box>
  );
};

// ── Text input row ────────────────────────────────────────────────────

const InputRow: React.FC<{
  label: string;
  value: string;
  placeholder?: string;
  mask?: boolean;
}> = ({ label, value, placeholder, mask = false }) => {
  const masked = mask ? "•".repeat(Math.max(0, value.length - 4)) + value.slice(-4) : value;
  return (
    <Box>
      <Box minWidth={FIELD_LABEL_WIDTH} flexShrink={0}>
        <Text color={Theme.gray[500]}>{label}</Text>
      </Box>
      <Box flexGrow={1}>
        {value.length === 0 ? (
          <>
            <Text color={Theme.forge.fire}>▌</Text>
            {placeholder && (
              <Text color={Theme.text.secondary}>{` ${placeholder}`}</Text>
            )}
          </>
        ) : (
          <>
            <Text bold>{masked}</Text>
            <Text color={Theme.forge.fire}>▌</Text>
          </>
        )}
      </Box>
    </Box>
  );
};

// ── Step renderers ────────────────────────────────────────────────────

function renderWelcomeStep() {
  return (
    <>
      <SectionHeading label={t("wizard.welcome.section")} />
      <Box marginTop={1} flexDirection="column">
        <Text color={Theme.text.secondary} wrap="wrap">
          {t("wizard.welcome.body1")}
        </Text>
        <Text color={Theme.text.secondary} wrap="wrap">
          {t("wizard.welcome.body2")}
        </Text>
      </Box>
      <SectionHeading label={t("wizard.welcome.fields_section")} />
      <Box marginTop={1} flexDirection="column">
        <Field label="1." value={t("wizard.step.model")} />
        <Field label="2." value={t("wizard.step.api_url")} />
        <Field label="3." value={t("wizard.step.api_key")} />
        <Field label="4." value={t("wizard.step.kubeconfig")} />
        <Field label="5." value={t("wizard.step.kube_context")} />
        <Field label="6." value={t("wizard.step.permission")} />
      </Box>
    </>
  );
}

function renderModelStep(
  state: WizardState,
  focusedRadioIdx: number,
) {
  const presets = state.modelPresets;
  if (state.values.model_is_custom) {
    return (
      <>
        <SectionHeading label={t("wizard.model.custom_section")} />
        <Box marginTop={1}>
          <InputRow
            label={t("wizard.model.label")}
            value={state.values.model_name}
            placeholder={t("wizard.model.placeholder")}
          />
        </Box>
        <Box marginTop={1}>
          <ValidationStatus step="model" state={state} />
        </Box>
      </>
    );
  }
  return (
    <>
      <SectionHeading label={t("wizard.model.recommended_section")} />
      <Box marginTop={1} flexDirection="column">
        {presets.map((p, i) => {
          const letter = String.fromCharCode(65 + i);
          return (
            <RadioRow
              key={p.id}
              option={{
                letter,
                label: p.label,
                hint: `${p.vendor} · ${p.hint}`,
                selected: state.values.model_name === p.id,
                focused: i === focusedRadioIdx,
              }}
            />
          );
        })}
      </Box>
      <SectionHeading label={t("wizard.model.other_section")} />
      <Box marginTop={1}>
        <RadioRow
          option={{
            letter: "F",
            label: t("wizard.model.custom_option"),
            hint: t("wizard.model.custom_hint"),
            selected: state.values.model_is_custom,
            focused: focusedRadioIdx === presets.length,
          }}
        />
      </Box>
    </>
  );
}

function renderApiUrlStep(state: WizardState) {
  return (
    <>
      <SectionHeading label={t("wizard.api_url.section")} />
      <Box marginTop={1}>
        <InputRow
          label={t("wizard.api_url.label")}
          value={state.values.api_base_url}
          placeholder="https://api.example.com/v1"
        />
      </Box>
      <Box marginTop={1}>
        <ValidationStatus step="api_url" state={state} />
      </Box>
    </>
  );
}

function renderApiKeyStep(state: WizardState) {
  return (
    <>
      <SectionHeading label={t("wizard.api_key.section")} />
      <Box marginTop={1}>
        <InputRow
          label={t("wizard.api_key.label")}
          value={state.values.llm_api_key}
          placeholder="sk-..."
          mask
        />
      </Box>
      <Box marginTop={1}>
        <ValidationStatus step="api_key" state={state} />
      </Box>
    </>
  );
}

function renderKubeconfigStep(state: WizardState) {
  return (
    <>
      <SectionHeading label={t("wizard.kubeconfig.section")} />
      <Box marginTop={1}>
        <InputRow
          label={t("wizard.kubeconfig.label")}
          value={state.values.kubeconfig_path}
          placeholder="~/.kube/config"
        />
      </Box>
      <Box marginTop={1}>
        <ValidationStatus step="kubeconfig" state={state} />
      </Box>
    </>
  );
}

function renderKubeContextStep(
  state: WizardState,
  focusedRadioIdx: number,
) {
  const ctxs = state.discoveredContexts;
  return (
    <>
      <SectionHeading
        label={`${t("wizard.kube_context.section")} (${ctxs.length})`}
      />
      <Box marginTop={1} flexDirection="column">
        {ctxs.map((c, i) => {
          const letter = String.fromCharCode(65 + i);
          return (
            <RadioRow
              key={c}
              option={{
                letter,
                label: c,
                selected: state.values.kube_context === c,
                focused: i === focusedRadioIdx,
              }}
            />
          );
        })}
      </Box>
    </>
  );
}

function renderPermissionStep(state: WizardState, focusedRadioIdx: number) {
  const options = [
    {
      letter: "A",
      label: t("wizard.permission.confirm_label"),
      hint: t("wizard.permission.confirm_hint"),
      value: true,
    },
    {
      letter: "B",
      label: t("wizard.permission.auto_label"),
      hint: t("wizard.permission.auto_hint"),
      value: false,
    },
  ];
  return (
    <>
      <SectionHeading label={t("wizard.permission.section")} />
      <Box marginTop={1} flexDirection="column">
        {options.map((o, i) => (
          <RadioRow
            key={o.letter}
            option={{
              letter: o.letter,
              label: o.label,
              hint: o.hint,
              selected: state.values.confirmation_required === o.value,
              focused: i === focusedRadioIdx,
            }}
          />
        ))}
      </Box>
    </>
  );
}

function renderSummaryStep(state: WizardState) {
  const v = state.values;
  return (
    <>
      <SectionHeading label={t("wizard.summary.section_config")} />
      <Box marginTop={1} flexDirection="column">
        <Field
          label={t("wizard.summary.model")}
          value={
            v.model_name +
            (v.model_is_custom ? `  ${t("wizard.summary.custom_tag")}` : "")
          }
          valueBold
        />
        <Field
          label={t("wizard.summary.api_url")}
          value={v.api_base_url}
        />
        <Field
          label={t("wizard.summary.api_key")}
          value={
            v.llm_api_key
              ? "•".repeat(Math.max(0, v.llm_api_key.length - 4)) +
                v.llm_api_key.slice(-4)
              : ""
          }
        />
        <Field
          label={t("wizard.summary.kubeconfig")}
          value={v.kubeconfig_path}
        />
        <Field
          label={t("wizard.summary.kube_context")}
          value={v.kube_context || t("wizard.summary.kube_context_default")}
        />
        <Field
          label={t("wizard.summary.permission")}
          value={
            v.confirmation_required
              ? t("wizard.permission.confirm_label")
              : t("wizard.permission.auto_label")
          }
          valueBold
        />
      </Box>
      {state.saveResult && (
        <>
          <SectionHeading label={t("wizard.summary.section_result")} />
          <Box marginTop={1} flexDirection="column">
            {state.saveResult.status === "success" ? (
              <>
                <Field
                  label={t("wizard.summary.saved_to")}
                  value={state.saveResult.savedPath}
                  valueColor={Theme.status.ok}
                />
                <Field
                  label={t("wizard.summary.saved_keys")}
                  value={state.saveResult.savedKeys.join(", ")}
                />
              </>
            ) : (
              <Field
                label={t("wizard.summary.save_error")}
                value={state.saveResult.message}
                labelColor={Theme.status.err}
                valueColor={Theme.status.err}
                wrap
              />
            )}
          </Box>
        </>
      )}
    </>
  );
}

// ── Step metadata (chip glyph + chip label + title) ──────────────────

interface StepMeta {
  glyph: string;
  chipLabel: string;
  titleKey: string;
}

const STEP_META: Record<StepKey, StepMeta> = {
  welcome: { glyph: "✻", chipLabel: "WELCOME", titleKey: "wizard.welcome.title" },
  model: { glyph: "⚙", chipLabel: "MODEL", titleKey: "wizard.model.title" },
  api_url: { glyph: "⚙", chipLabel: "URL", titleKey: "wizard.api_url.title" },
  api_key: { glyph: "🔑", chipLabel: "KEY", titleKey: "wizard.api_key.title" },
  kubeconfig: { glyph: "⎈", chipLabel: "KUBE", titleKey: "wizard.kubeconfig.title" },
  kube_context: { glyph: "⎈", chipLabel: "CTX", titleKey: "wizard.kube_context.title" },
  permission: { glyph: "⛔", chipLabel: "PERM", titleKey: "wizard.permission.title" },
  summary: { glyph: "✓", chipLabel: "REVIEW", titleKey: "wizard.summary.title" },
};

// ── Main component ────────────────────────────────────────────────────

export const WizardCard: React.FC<{
  serverUrl: string;
  onExit: (saved: boolean) => void;
}> = ({ serverUrl, onExit }) => {
  const [state, dispatch] = useReducer(wizardReducer, undefined, () =>
    initialWizardState(),
  );
  const [focusedRadioIdx, setFocusedRadioIdx] = useState(0);
  const [inputBuffer, setInputBuffer] = useState("");
  const width = useBootCardWidth();
  const client = useRef<WizardClient>(new WizardClient(serverUrl)).current;

  // Per-step option-count bound. Used to clamp ``focusedRadioIdx``
  // when the step's list shrinks mid-flow (async presets reload,
  // discovered-context refresh, …). Without the clamp, nav handlers
  // would walk a stale React-state value down through "no visible
  // change" presses before the cursor actually moves — same bug
  // class fixed in InputPrompt's slash menu and in shared/Select.
  // Steps without a radio (text input, summary) bound to 1 so the
  // clamp degenerates to 0.
  const stepBound = (() => {
    switch (state.currentStep) {
      case "model":
        return state.modelPresets.length + 1; // +1 for [F] custom
      case "kube_context":
        return Math.max(1, state.discoveredContexts.length);
      case "permission":
        return 2;
      default:
        return 1;
    }
  })();
  const safeFocusedIdx = Math.min(
    Math.max(0, focusedRadioIdx),
    stepBound - 1,
  );

  // Load presets on mount.
  useEffect(() => {
    let active = true;
    void (async () => {
      const presets = await client.fetchModelPresets();
      if (active) dispatch({ type: "PRESETS_LOADED", presets });
    })();
    return () => {
      active = false;
    };
  }, [client]);

  // Reset transient state when step changes.
  useEffect(() => {
    // For RADIO steps: pre-focus the row matching the currently-stored
    // value. Without this, returning to a step where the user had
    // already chosen something shows focus on row 0 (misleading —
    // the selected row is somewhere else).
    let nextFocus = 0;
    if (state.currentStep === "model" && !state.values.model_is_custom) {
      const idx = state.modelPresets.findIndex(
        (p) => p.id === state.values.model_name,
      );
      nextFocus = idx >= 0 ? idx : state.modelPresets.length; // [F] custom slot
    } else if (state.currentStep === "kube_context") {
      const idx = state.discoveredContexts.indexOf(state.values.kube_context);
      nextFocus = idx >= 0 ? idx : 0;
    } else if (state.currentStep === "permission") {
      nextFocus = state.values.confirmation_required ? 0 : 1;
    }
    setFocusedRadioIdx(nextFocus);

    // Pre-fill input buffer from existing value so the user can edit
    // a previously-typed entry when they return to a step.
    if (state.currentStep === "api_url") setInputBuffer(state.values.api_base_url);
    else if (state.currentStep === "api_key") setInputBuffer(state.values.llm_api_key);
    else if (state.currentStep === "kubeconfig") setInputBuffer(state.values.kubeconfig_path);
    else if (state.currentStep === "model" && state.values.model_is_custom)
      setInputBuffer(state.values.model_name);
    else setInputBuffer("");
    // Deliberately gated on currentStep + the custom-model toggle —
    // re-running on every value change would clobber the user's
    // in-progress typing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.currentStep, state.values.model_is_custom]);

  // Auto-clear "returned from" hint after 3 seconds.
  useEffect(() => {
    if (state.returnedFromStep === null) return;
    const t = setTimeout(
      () => dispatch({ type: "CLEAR_RETURNED_HINT" }),
      3000,
    );
    return () => clearTimeout(t);
  }, [state.returnedFromStep]);

  // Notify caller on done.
  useEffect(() => {
    if (state.done) onExit(state.exitReason === "save");
  }, [state.done, state.exitReason, onExit]);

  // ── Async commit handlers ───────────────────────────────────────────

  async function commitApiUrl() {
    dispatch({ type: "VALUE_SET", key: "api_base_url", value: inputBuffer });
    dispatch({ type: "VALIDATION_START", step: "api_url" });
    const r = await client.validateUrl(inputBuffer);
    dispatch({ type: "VALIDATION_DONE", step: "api_url", result: r });
    if (r.status !== "error") dispatch({ type: "STEP_NEXT" });
  }

  async function commitApiKey() {
    dispatch({ type: "VALUE_SET", key: "llm_api_key", value: inputBuffer });
    dispatch({ type: "VALIDATION_START", step: "api_key" });
    const r = await client.validateApiKey({
      apiKey: inputBuffer,
      baseUrl: state.values.api_base_url,
      model: state.values.model_name,
    });
    dispatch({ type: "VALIDATION_DONE", step: "api_key", result: r });
    if (r.status !== "error") dispatch({ type: "STEP_NEXT" });
  }

  async function commitKubeconfig() {
    dispatch({
      type: "VALUE_SET",
      key: "kubeconfig_path",
      value: inputBuffer,
    });
    dispatch({ type: "VALIDATION_START", step: "kubeconfig" });
    const r: ValidationResult = await client.validateKubeconfig(inputBuffer);
    dispatch({ type: "VALIDATION_DONE", step: "kubeconfig", result: r });
    const contexts = r.metadata?.["contexts"];
    if (Array.isArray(contexts)) {
      dispatch({
        type: "DISCOVERED_CONTEXTS",
        contexts: contexts.filter((c): c is string => typeof c === "string"),
      });
    }
    if (r.status !== "error") dispatch({ type: "STEP_NEXT" });
  }

  function commitCustomModel() {
    if (!inputBuffer.trim()) {
      // Surface as an inline validation error instead of silently
      // ignoring Enter — otherwise the user presses Enter on an
      // empty Model ID and sees nothing happen.
      dispatch({
        type: "VALIDATION_DONE",
        step: "model",
        result: {
          status: "error",
          message: t("wizard.model.empty_error"),
          block: true,
          metadata: {},
        },
      });
      return;
    }
    dispatch({ type: "VALUE_SET", key: "model_name", value: inputBuffer });
    dispatch({ type: "STEP_NEXT" });
  }

  async function commitSave() {
    const result = await client.saveConfig({
      model_name: state.values.model_name,
      api_base_url: state.values.api_base_url,
      llm_api_key: state.values.llm_api_key,
      kubeconfig_path: state.values.kubeconfig_path,
      kube_context: state.values.kube_context,
      confirmation_required: state.values.confirmation_required
        ? "true"
        : "false",
    });
    dispatch({ type: "SAVE_DONE", result });
  }

  // ── Keyboard handler ────────────────────────────────────────────────

  useInput((input, key) => {
    if (state.done) return;
    // Esc: cancel wizard (with one exception — exit custom-model TEXT mode)
    if (key.escape) {
      if (
        state.currentStep === "model" &&
        state.values.model_is_custom
      ) {
        dispatch({ type: "TOGGLE_CUSTOM_MODEL", on: false });
        return;
      }
      dispatch({ type: "CANCEL" });
      return;
    }
    // ← / Shift+Tab: back one step
    if (key.leftArrow || (key.tab && key.shift)) {
      dispatch({ type: "STEP_BACK" });
      return;
    }
    // 1-8 digit: jump to completed step
    if (input && /^[1-9]$/.test(input)) {
      const idx = parseInt(input, 10) - 1;
      if (idx < STEP_ORDER.length) {
        const target = STEP_ORDER[idx];
        if (target) dispatch({ type: "STEP_JUMP", step: target });
      }
      return;
    }
    // Step-specific handling
    switch (state.currentStep) {
      case "welcome": {
        if (key.return) dispatch({ type: "STEP_NEXT" });
        break;
      }
      case "model": {
        if (state.values.model_is_custom) {
          handleTextInput(input, key, setInputBuffer);
          if (key.return) commitCustomModel();
          break;
        }
        const presets = state.modelPresets;
        const totalOptions = presets.length + 1; // +1 for [F] custom
        if (key.upArrow) {
          setFocusedRadioIdx(Math.max(0, safeFocusedIdx - 1));
          break;
        }
        if (key.downArrow) {
          setFocusedRadioIdx(
            Math.min(totalOptions - 1, safeFocusedIdx + 1),
          );
          break;
        }
        if (input && /^[a-fA-F]$/.test(input)) {
          const idx = input.toUpperCase().charCodeAt(0) - 65;
          if (idx < totalOptions) {
            setFocusedRadioIdx(idx);
          }
          break;
        }
        if (key.return) {
          if (safeFocusedIdx === presets.length) {
            // [F] custom
            dispatch({ type: "TOGGLE_CUSTOM_MODEL", on: true });
          } else {
            const preset = presets[safeFocusedIdx];
            if (preset) {
              dispatch({
                type: "VALUE_SET",
                key: "model_name",
                value: preset.id,
              });
              dispatch({ type: "STEP_NEXT" });
            }
          }
        }
        break;
      }
      case "api_url": {
        handleTextInput(input, key, setInputBuffer);
        if (key.return) void commitApiUrl();
        break;
      }
      case "api_key": {
        handleTextInput(input, key, setInputBuffer);
        if (key.return) void commitApiKey();
        break;
      }
      case "kubeconfig": {
        handleTextInput(input, key, setInputBuffer);
        if (key.return) void commitKubeconfig();
        break;
      }
      case "kube_context": {
        const ctxs = state.discoveredContexts;
        if (key.upArrow) {
          setFocusedRadioIdx(Math.max(0, safeFocusedIdx - 1));
          break;
        }
        if (key.downArrow) {
          setFocusedRadioIdx(Math.min(ctxs.length - 1, safeFocusedIdx + 1));
          break;
        }
        if (input && /^[a-zA-Z]$/.test(input)) {
          const idx = input.toUpperCase().charCodeAt(0) - 65;
          if (idx < ctxs.length) setFocusedRadioIdx(idx);
          break;
        }
        if (key.return) {
          const picked = ctxs[safeFocusedIdx];
          if (picked) {
            dispatch({ type: "VALUE_SET", key: "kube_context", value: picked });
          }
          dispatch({ type: "STEP_NEXT" });
        }
        break;
      }
      case "permission": {
        if (key.upArrow) {
          setFocusedRadioIdx(Math.max(0, safeFocusedIdx - 1));
          break;
        }
        if (key.downArrow) {
          setFocusedRadioIdx(Math.min(1, safeFocusedIdx + 1));
          break;
        }
        if (input && /^[abAB]$/.test(input)) {
          setFocusedRadioIdx(input.toUpperCase() === "A" ? 0 : 1);
          break;
        }
        if (key.return) {
          dispatch({
            type: "VALUE_SET",
            key: "confirmation_required",
            value: safeFocusedIdx === 0,
          });
          dispatch({ type: "STEP_NEXT" });
        }
        break;
      }
      case "summary": {
        // Trigger save on Enter for two cases:
        //   1. First press (no prior result) — initial save.
        //   2. Retry after a failure — saveResult exists with
        //      status="error". Without this branch the user would
        //      be stuck on a failed save with no way to retry
        //      (CANCEL would lose all wizard input).
        // We deliberately don't retry on status="success" — done=true
        // is already set in that case and onExit fires immediately.
        const canCommit =
          !state.saveResult || state.saveResult.status === "error";
        if (key.return && canCommit) void commitSave();
        break;
      }
    }
  });

  // ── Render ───────────────────────────────────────────────────────────

  const meta = STEP_META[state.currentStep];
  const stepBody = (() => {
    switch (state.currentStep) {
      case "welcome":
        return renderWelcomeStep();
      case "model":
        return renderModelStep(state, safeFocusedIdx);
      case "api_url":
        return renderApiUrlStep(state);
      case "api_key":
        return renderApiKeyStep(state);
      case "kubeconfig":
        return renderKubeconfigStep(state);
      case "kube_context":
        return renderKubeContextStep(state, safeFocusedIdx);
      case "permission":
        return renderPermissionStep(state, safeFocusedIdx);
      case "summary":
        return renderSummaryStep(state);
      default:
        return null;
    }
  })();

  const hint = (() => {
    switch (state.currentStep) {
      case "welcome":
        return t("wizard.hint.welcome");
      case "model":
        return state.values.model_is_custom
          ? t("wizard.hint.model_custom")
          : t("wizard.hint.radio_with_back");
      case "api_url":
      case "api_key":
      case "kubeconfig":
        return t("wizard.hint.text_with_back");
      case "kube_context":
      case "permission":
        return t("wizard.hint.radio_with_back");
      case "summary":
        return state.saveResult
          ? state.saveResult.status === "success"
            ? t("wizard.hint.saved")
            : t("wizard.hint.save_failed")
          : t("wizard.hint.summary");
    }
  })();

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
          glyph={meta.glyph}
          chipLabel={meta.chipLabel}
          title={t(meta.titleKey)}
        />
        <Box marginTop={1}>
          <StepperBar state={state} />
        </Box>
        <Box marginTop={1}>
          <ReturnedHint state={state} step={state.currentStep} />
        </Box>
        {stepBody}
        <HintRow text={hint} />
      </Box>
    </Box>
  );
};

// ── Helpers ───────────────────────────────────────────────────────────

/** Handle character / backspace / delete on a text input buffer. */
function handleTextInput(
  input: string,
  key: { backspace?: boolean; delete?: boolean; return?: boolean; escape?: boolean; ctrl?: boolean; meta?: boolean; tab?: boolean; upArrow?: boolean; downArrow?: boolean; leftArrow?: boolean; rightArrow?: boolean },
  setBuffer: React.Dispatch<React.SetStateAction<string>>,
) {
  if (key.backspace || key.delete) {
    setBuffer((s) => s.slice(0, -1));
    return;
  }
  if (
    key.return ||
    key.escape ||
    key.ctrl ||
    key.meta ||
    key.tab ||
    key.upArrow ||
    key.downArrow ||
    key.leftArrow ||
    key.rightArrow
  ) {
    return;
  }
  if (input && input.length > 0) {
    setBuffer((s) => s + input);
  }
}

// Re-export for cli.tsx convenience.
import { useRef } from "react";
export { useRef };
