/**
 * Map an error message to an actionable next-step list.
 *
 * Mirrors the legacy Python TUI's ``_ERROR_SUGGESTIONS`` table
 * (src/chaos_agent/tui/renderers/messages.py). Order matters —
 * specific patterns first, broad ones last.
 *
 * Localization: keywords stay English (they're matched against the
 * server's error message stream which is English). The label and
 * suggestions are looked up through i18n at render time, so /clear
 * etc. stays English (real command names) while prose translates.
 */

import { t, tArr } from "../i18n/index.js";

export interface ErrorHint {
  /** Capitalised label rendered next to the ✗ glyph. */
  label: string;
  /** Bulleted next-step list. Each entry is one line of advice. */
  suggestions: string[];
}

interface Pattern {
  keywords: string[];
  /** Dictionary key prefix; full keys are ``${key}.label`` and ``${key}.suggestions``. */
  i18nKey: string;
}

const PATTERNS: readonly Pattern[] = [
  {
    keywords: ["not initialized", "failed to initialize", "init failed"],
    i18nKey: "error.init_failed",
  },
  {
    keywords: [
      "kubeconfig",
      "kube context",
      "kube_context",
      "context invalid",
      "context not found",
      "connection refused",
    ],
    i18nKey: "error.cluster_unreachable",
  },
  {
    keywords: ["stream error", "stream interrupted", "stream timeout"],
    i18nKey: "error.stream_error",
  },
  {
    keywords: ["conversation error", "conversation failed", "failed to start"],
    i18nKey: "error.conversation_error",
  },
  {
    keywords: ["replay failed", "cannot rehydrate", "recording parse"],
    i18nKey: "error.replay_failed",
  },
  {
    keywords: ["command failed", "unknown command"],
    i18nKey: "error.command_failed",
  },
  {
    keywords: ["session not found"],
    i18nKey: "error.session_expired",
  },
];

export function suggestionsForError(message: string): ErrorHint | null {
  if (!message) return null;
  const haystack = message.toLowerCase();
  for (const pattern of PATTERNS) {
    if (pattern.keywords.some((kw) => haystack.includes(kw.toLowerCase()))) {
      return {
        label: t(`${pattern.i18nKey}.label`),
        suggestions: [...tArr(`${pattern.i18nKey}.suggestions`)],
      };
    }
  }
  return null;
}
