/**
 * Reusable yes/no/feedback prompt — three options on top of the
 * generic ``Select`` primitive:
 *
 *   ❯ Yes                                  ← affirmative
 *     No                                   ← negative
 *     Tell me something else…              ← hasFeedback (free-form)
 *
 * Use this whenever the UI asks the user a binary question that
 * SHOULD ALSO accept a free-form reply (e.g. confirm dialogs,
 * destructive-action gates, "ready to continue?" prompts). The
 * caller wires the answer + optional feedback to whatever the
 * domain needs — this primitive doesn't dispatch reducer actions
 * itself.
 *
 * Defaults: labels fall through to i18n keys ``select.yesno.yes``,
 * ``select.yesno.no``, ``select.yesno.feedback``. Override on a
 * per-call basis (e.g. ConfirmMessage uses scenario-specific
 * labels like "提交意图" / "调整意图").
 *
 * Why this is a thin wrapper around ``Select<T>`` rather than
 * inlined: the three-option Y/N/feedback pattern recurs every time
 * we add an interactive prompt. Hosting it as a separate primitive
 * means future callers don't re-derive the items array, the
 * "feedback" → free-form-text Enter handling, or the i18n
 * defaults. ``Select<T>`` stays generic for cases that need 4+
 * options or non-Y/N semantics.
 */

import { Select, type SelectItem } from "./Select.js";
import { t } from "../../i18n/index.js";

/**
 * Discriminator returned to the caller. ``"yes"`` / ``"no"`` mean
 * the user committed without typing; ``"feedback"`` means they
 * picked the third option AND typed text. Empty-string feedback
 * is still passed through (caller decides what empty means —
 * ConfirmMessage treats it as plain "no" with no follow-up turn).
 */
export type YesNoFeedbackAnswer = "yes" | "no" | "feedback";

export interface YesNoFeedbackSelectProps {
  /** Override the default "Yes" label (i18n: ``select.yesno.yes``). */
  yesLabel?: string;
  /** Override the default "No" label (i18n: ``select.yesno.no``). */
  noLabel?: string;
  /** Override the default feedback label (i18n: ``select.yesno.feedback``). */
  feedbackLabel?: string;
  isFocused: boolean;
  initialIndex?: number;
  /**
   * Fired when the user commits a choice. ``feedback`` is set
   * only when ``answer === "feedback"`` AND the user pressed
   * Enter inside feedback mode. The string may be empty —
   * callers decide whether empty == cancel-feedback or send-empty.
   */
  onConfirm: (answer: YesNoFeedbackAnswer, feedback?: string) => void;
  /**
   * Fired on Esc / Ctrl+C in options mode. Esc inside feedback
   * mode bounces back to options without firing this callback —
   * see ``Select`` for the rationale.
   */
  onCancel: () => void;
}

export const YesNoFeedbackSelect: React.FC<YesNoFeedbackSelectProps> = ({
  yesLabel,
  noLabel,
  feedbackLabel,
  isFocused,
  initialIndex,
  onConfirm,
  onCancel,
}) => {
  const items: SelectItem<YesNoFeedbackAnswer>[] = [
    { value: "yes", label: yesLabel ?? t("select.yesno.yes") },
    { value: "no", label: noLabel ?? t("select.yesno.no") },
    {
      value: "feedback",
      label: feedbackLabel ?? t("select.yesno.feedback"),
      hasFeedback: true,
    },
  ];
  return (
    <Select<YesNoFeedbackAnswer>
      items={items}
      isFocused={isFocused}
      initialIndex={initialIndex}
      onSelect={(value, feedback) => onConfirm(value, feedback)}
      onCancel={onCancel}
    />
  );
};
