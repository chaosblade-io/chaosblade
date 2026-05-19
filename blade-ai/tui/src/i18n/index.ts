/**
 * Tiny i18n layer.
 *
 *   t(key)            → string lookup with {param} interpolation
 *   tArr(key)         → string[] lookup (thinking phrases, suggestion bullets)
 *   ACTIVE_LANG       → resolved language code, exposed for diagnostics
 *
 * Resolution order (first match wins):
 *   1. ``BLADE_AI_LANG`` env override (any value starting with ``zh`` /
 *      ``en``, case-insensitive — supports ``zh-CN``, ``en_US`` etc.)
 *   2. ``LC_ALL`` / ``LANG`` starting with ``zh`` → zh
 *   3. fallback → en
 *
 * Note: ``zh_TW`` (traditional) routes to the simplified zh dictionary.
 * Acceptable trade-off — simplified Chinese still beats English for a
 * traditional reader. A separate zh-TW dictionary is left to a future
 * milestone if real users ask.
 *
 * **No runtime swap.** Language is captured once at module import.
 * A mid-session switch would require a full Ink restart because
 * Header text is burn-in'd into Ink's ``<Static>`` on first render.
 * This matches Qwen Code's i18n behavior. To test multiple languages,
 * spawn separate child processes with the env override set.
 *
 * Missing keys: t() returns the key itself (visible "untranslated"
 * marker). Falls back to ``en`` first if the active dict lacks the
 * key, so a missing zh entry shows the en string rather than the bare key.
 */

import { en } from "./en.js";
import { zh } from "./zh.js";

export type LangCode = "en" | "zh";
export type Dict = Record<string, string | readonly string[]>;

const dicts: Record<LangCode, Dict> = { en, zh };

function detectLang(): LangCode {
  // ``startsWith`` (not ``===``) so ``BLADE_AI_LANG=zh-CN`` /
  // ``zh_TW.UTF-8`` / ``en-US`` all route correctly. M9 self-check
  // caught this — the original ``=== "zh"`` rejected anything past
  // the bare two-letter code.
  const forced = (process.env["BLADE_AI_LANG"] ?? "").toLowerCase().trim();
  if (forced.startsWith("zh")) return "zh";
  if (forced.startsWith("en")) return "en";
  // ``||`` (not ``??``) so an empty-string ``LC_ALL=""`` falls
  // through to ``LANG`` — many shells treat ``LC_ALL=`` as "I want
  // LANG to win" rather than as "force C locale". Each field is
  // trimmed independently before the OR so a blank-but-non-empty
  // value (just whitespace) doesn't hijack the chain.
  const localeRaw =
    (process.env["LC_ALL"] ?? "").trim() ||
    (process.env["LANG"] ?? "").trim() ||
    "";
  const locale = localeRaw.toLowerCase();
  // POSIX locales are well-formed: ``<lang>[_<COUNTRY>][.<charset>]``.
  // ``startsWith("zh")`` is the only correct prefix test — there's no
  // legitimate locale where ``zh`` appears after an underscore or dot.
  if (locale.startsWith("zh")) return "zh";
  return "en";
}

export const ACTIVE_LANG: LangCode = detectLang();
const active: Dict = dicts[ACTIVE_LANG];
const fallback: Dict = dicts.en;

/** ``t("error.init_failed.label")`` etc. */
export function t(
  key: string,
  params?: Record<string, string | number>,
): string {
  const value = active[key] ?? fallback[key];
  if (typeof value !== "string") {
    // Either missing or it's an array — t() is for strings only.
    return key;
  }
  if (!params) return value;
  return value.replace(/\{(\w+)\}/g, (_m, name: string) =>
    params[name] !== undefined ? String(params[name]) : `{${name}}`,
  );
}

/** Array lookup for things like thinking phrases / suggestion bullets. */
export function tArr(key: string): readonly string[] {
  const value = active[key] ?? fallback[key];
  if (Array.isArray(value)) return value;
  return [];
}

// (Removed in M9 self-check: a ``_setLangForTesting`` helper that
// wrote to ``globalThis`` but ``t()`` / ``tArr()`` never read from
// there — the function had no observable effect. Tests now spawn
// child processes with ``BLADE_AI_LANG=...`` instead, which is the
// only correct way to exercise both dictionaries.)
