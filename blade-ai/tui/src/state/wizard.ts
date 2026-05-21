/**
 * Wizard state machine — self-contained, drives the Ink wizard UI.
 *
 * Kept out of the main app reducer because:
 *   1. Wizard is a transient mode (first-run / re-config only).
 *   2. None of the main-app slices (history / pending / streamState /
 *      tools / confirms) are relevant to wizard rendering.
 *   3. Easier to test in isolation.
 *
 * Components consume this via ``useReducer(reducer, initialState)``
 * inside the WizardCard container. Side effects (HTTP calls to the
 * Python validator endpoints) are dispatched as async actions that
 * the container resolves via ``api/wizard.ts``.
 */

import type { ValidationResult, ModelPreset, SaveResult } from "../api/wizard.js";

// ── Step model ─────────────────────────────────────────────────────────

/** Stable step IDs — match the Python ``onboarding._build_steps`` order. */
export type StepKey =
  | "welcome"
  | "model"
  | "api_url"
  | "api_key"
  | "kubeconfig"
  | "kube_context"
  | "permission"
  | "summary";

export const STEP_ORDER: StepKey[] = [
  "welcome",
  "model",
  "api_url",
  "api_key",
  "kubeconfig",
  "kube_context",
  "permission",
  "summary",
];

export function stepIndex(key: StepKey): number {
  return STEP_ORDER.indexOf(key);
}

/** Numeric position (1-based) — for stepper display. */
export function stepNumber(key: StepKey): number {
  return stepIndex(key) + 1;
}

// ── Per-step value storage ─────────────────────────────────────────────

/**
 * Captured input across the wizard's lifetime. Each key maps to a
 * Python-side config field name (matches ConfigStore keys).
 */
export interface WizardValues {
  model_name: string;
  /** True when the user picked "[F] custom" — model_name then holds
   *  the user-typed string instead of a preset id. UI uses this flag
   *  to decide whether to show the radio list or the text input. */
  model_is_custom: boolean;
  api_base_url: string;
  llm_api_key: string;
  kubeconfig_path: string;
  kube_context: string;
  /** ``true`` = confirm before fault injection, ``false`` = auto. */
  confirmation_required: boolean;
}

export function emptyValues(): WizardValues {
  return {
    model_name: "",
    model_is_custom: false,
    api_base_url: "",
    llm_api_key: "",
    kubeconfig_path: "",
    kube_context: "",
    confirmation_required: true,
  };
}

// ── Validation state ───────────────────────────────────────────────────

export interface ValidationState {
  status: "idle" | "busy" | "ok" | "warn" | "error";
  message: string;
  metadata: Record<string, unknown>;
}

const IDLE_VALIDATION: ValidationState = {
  status: "idle",
  message: "",
  metadata: {},
};

// ── Top-level wizard state ─────────────────────────────────────────────

export interface WizardState {
  /** Currently visible step. */
  currentStep: StepKey;
  /** Captured field values. */
  values: WizardValues;
  /** Validation state per step (only relevant for validated steps). */
  validations: Record<StepKey, ValidationState>;
  /** Set of step keys the user has visited + advanced past. Drives
   *  which steps the stepper bar renders as ``[N✓]`` (jumpable). */
  completedSteps: Set<StepKey>;
  /** Discovered kube contexts (populated by kubeconfig validation). */
  discoveredContexts: string[];
  /** Model presets fetched from server on init. Empty until loaded. */
  modelPresets: ModelPreset[];
  /** Save outcome — populated only after the user submits step 8. */
  saveResult: SaveResult | null;
  /** True when the user reached an exit state (saved or cancelled). */
  done: boolean;
  /** ``cancel`` / ``save`` — UI shows different farewell messages. */
  exitReason: "cancel" | "save" | null;
  /** Set to a step key when the user jumped back; UI shows a "已返回此
   *  步骤" hint above the input. Cleared on next advance. */
  returnedFromStep: StepKey | null;
}

export function initialWizardState(): WizardState {
  const validations: Record<StepKey, ValidationState> = {} as Record<
    StepKey,
    ValidationState
  >;
  for (const k of STEP_ORDER) {
    validations[k] = { ...IDLE_VALIDATION };
  }
  return {
    currentStep: "welcome",
    values: emptyValues(),
    validations,
    completedSteps: new Set(),
    discoveredContexts: [],
    modelPresets: [],
    saveResult: null,
    done: false,
    exitReason: null,
    returnedFromStep: null,
  };
}

// ── Actions ────────────────────────────────────────────────────────────

export type WizardAction =
  | { type: "PRESETS_LOADED"; presets: ModelPreset[] }
  | { type: "VALUE_SET"; key: keyof WizardValues; value: string | boolean }
  | { type: "TOGGLE_CUSTOM_MODEL"; on: boolean }
  | { type: "VALIDATION_START"; step: StepKey }
  | {
      type: "VALIDATION_DONE";
      step: StepKey;
      result: ValidationResult;
    }
  | { type: "DISCOVERED_CONTEXTS"; contexts: string[] }
  | { type: "STEP_NEXT" }
  | { type: "STEP_BACK" }
  | { type: "STEP_JUMP"; step: StepKey }
  | { type: "SAVE_DONE"; result: SaveResult }
  | { type: "CANCEL" }
  | { type: "CLEAR_RETURNED_HINT" };

// ── Reducer ────────────────────────────────────────────────────────────

export function wizardReducer(
  state: WizardState,
  action: WizardAction,
): WizardState {
  switch (action.type) {
    case "PRESETS_LOADED": {
      return { ...state, modelPresets: action.presets };
    }

    case "VALUE_SET": {
      return {
        ...state,
        values: { ...state.values, [action.key]: action.value },
      };
    }

    case "TOGGLE_CUSTOM_MODEL": {
      // Switching INTO custom mode clears any preset choice from
      // model_name so the user starts with an empty input. Switching
      // OUT preserves the typed text — gives them a clean "Esc to
      // change my mind" without losing what they typed.
      return {
        ...state,
        values: {
          ...state.values,
          model_is_custom: action.on,
          model_name: action.on ? "" : state.values.model_name,
        },
      };
    }

    case "VALIDATION_START": {
      return {
        ...state,
        validations: {
          ...state.validations,
          [action.step]: {
            status: "busy",
            message: "校验中…",
            metadata: {},
          },
        },
      };
    }

    case "VALIDATION_DONE": {
      const r = action.result;
      return {
        ...state,
        validations: {
          ...state.validations,
          [action.step]: {
            status: r.status,
            message: r.message,
            metadata: r.metadata,
          },
        },
      };
    }

    case "DISCOVERED_CONTEXTS": {
      return { ...state, discoveredContexts: action.contexts };
    }

    case "STEP_NEXT": {
      const idx = stepIndex(state.currentStep);
      // Find the next non-skipped step. The only auto-skip case in
      // the current wizard is ``kube_context`` when zero contexts
      // were discovered — keep this list explicit so adding a new
      // skip rule means editing one switch.
      let nextIdx = idx + 1;
      while (nextIdx < STEP_ORDER.length) {
        const nextKey = STEP_ORDER[nextIdx];
        if (nextKey === undefined) break;
        if (
          nextKey === "kube_context" &&
          state.discoveredContexts.length <= 1
        ) {
          nextIdx++;
          continue;
        }
        break;
      }
      if (nextIdx >= STEP_ORDER.length) return state;
      const completed = new Set(state.completedSteps);
      completed.add(state.currentStep);
      const nextKey = STEP_ORDER[nextIdx];
      if (!nextKey) return state;
      return {
        ...state,
        completedSteps: completed,
        currentStep: nextKey,
        returnedFromStep: null,
      };
    }

    case "STEP_BACK": {
      const idx = stepIndex(state.currentStep);
      if (idx <= 0) return state;
      let prevIdx = idx - 1;
      while (prevIdx > 0) {
        const prevKey = STEP_ORDER[prevIdx];
        if (
          prevKey === "kube_context" &&
          state.discoveredContexts.length <= 1
        ) {
          prevIdx--;
          continue;
        }
        break;
      }
      const prevKey = STEP_ORDER[prevIdx];
      if (!prevKey) return state;
      return {
        ...state,
        currentStep: prevKey,
        returnedFromStep: prevKey,
      };
    }

    case "STEP_JUMP": {
      // Only allow jumping to steps the user has already completed,
      // or to the current step (no-op). Future steps are gated until
      // the linear advance reaches them — preserves the validation
      // order (api-key needs api-url, kube-context needs kubeconfig,
      // etc.).
      if (
        action.step !== state.currentStep &&
        !state.completedSteps.has(action.step)
      ) {
        return state;
      }
      return {
        ...state,
        currentStep: action.step,
        returnedFromStep:
          action.step !== state.currentStep ? action.step : null,
      };
    }

    case "SAVE_DONE": {
      return {
        ...state,
        saveResult: action.result,
        done: action.result.status === "success",
        exitReason: action.result.status === "success" ? "save" : null,
      };
    }

    case "CANCEL": {
      return { ...state, done: true, exitReason: "cancel" };
    }

    case "CLEAR_RETURNED_HINT": {
      if (state.returnedFromStep === null) return state;
      return { ...state, returnedFromStep: null };
    }

    default:
      return state;
  }
}

// ── Helpers (UI side) ──────────────────────────────────────────────────

/**
 * Decide whether the user is allowed to advance from the given step.
 * Most steps require their validation to not be in error state and
 * (where applicable) to have a non-empty value.
 */
export function canAdvanceFrom(state: WizardState, step: StepKey): boolean {
  const v = state.validations[step];
  if (v.status === "error") return false;
  if (v.status === "busy") return false;
  switch (step) {
    case "welcome":
      return true;
    case "model":
      return state.values.model_name.trim().length > 0;
    case "api_url":
      return state.values.api_base_url.trim().length > 0;
    case "api_key":
      return state.values.llm_api_key.trim().length > 0;
    case "kubeconfig":
      // Empty kubeconfig path is acceptable (kubectl falls back to
      // its default behaviour); validator returns ``warn`` rather
      // than ``error`` for that case so the gate already passes.
      return true;
    case "kube_context":
      return true; // optional select
    case "permission":
      return true;
    case "summary":
      return true;
    default:
      return true;
  }
}
