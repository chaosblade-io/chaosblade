/**
 * blade-ai TUI palette — **Forge × Operator**.
 *
 * Brand mood (vs. Claude Code's "muji 工具，柔琥珀消隐" muji-tool approach):
 *   blade-ai is a chaos-engineering control panel. The operator is
 *   about to push real disruption into a real Kubernetes cluster —
 *   the TUI should feel like a flight-deck HUD or a blacksmith
 *   forge, not a quiet writing aid. Two-line summary:
 *
 *     • Forge   — deep warm orange/iron, the colour of heated metal.
 *                 Carries chrome, decisions, status accents.
 *     • Operator — high-contrast section dividers, status indicator
 *                 lamps (●/◐/◯), inverse chips for armed states.
 *
 * Design rules (locked in for the Forge × Operator overhaul):
 *
 *   forge.fire (#E87841)  — primary brand. Headings, accents, agent
 *                           leader glyph, soft-decision borders.
 *                           Saturation deliberately ~65% — higher
 *                           than Claude Code's #D4A27F (~40%) because
 *                           blade-ai *should not* disappear, it
 *                           should read as "you are operating a
 *                           dangerous tool".
 *   forge.iron (#A8451E)  — hard-decision borders (Layer 2 confirm,
 *                           Result). Deeper, "this is final".
 *   forge.ember (#FFB870) — alert-tint background (rare).
 *
 *   slate.* — cool, dim accent for dividers / shadows. Used sparingly
 *             to balance the warm forge tones.
 *
 *   gray.100..900 — 8 grey shades, replaces the previous single
 *                   ``secondary``. Body text, hints, metadata,
 *                   rules, dim decorations each get their own shade.
 *
 *   status.* — semantic colours, kept saturation-matched to forge so
 *              the palette has one visual temperature family. ok is
 *              biased blue-green (forge's complement) so success
 *              reads with weight against the warm chrome.
 *
 * The TUI itself paints no background — Ink renders foreground text
 * only, the user's terminal owns the canvas. Background values exist
 * only as reference for future inverse-chip backgrounds.
 *
 * ──────────────────────────────────────────────────────────────────
 * Backwards-compatibility aliases (text.primary / text.secondary /
 * text.accent / border.default / border.focused / border.tool /
 * border.result / border.diagnostic / status.warnDim / status.errDim
 * / ui.comment / ui.gradient / ui.background) are preserved at the
 * bottom of this file so the 20+ components that still reference the
 * old names continue to compile. The Forge × Operator overhaul
 * progressively migrates each consumer to the new token names; this
 * shim lets that migration land file-by-file without a giant atomic
 * rename.
 * ──────────────────────────────────────────────────────────────────
 */

const forge = {
  // Glow / alert tint — rarely used, only for highlight backgrounds.
  ember: "#FFB870",
  // Primary brand — chrome, accents, leader glyphs, soft decisions.
  fire: "#E87841",
  // Heated-iron deep — hard decisions, result frames, "this is final".
  iron: "#A8451E",
  // Dim fire (forge.fire desaturated ~30%) — reserved for container
  // borders that need a brand-warm tint without competing with the
  // saturated chips / buttons inside. First user: ConfirmMessage
  // frames (both soft + hard tiers share this single token now, so
  // tier is signaled by chip + glyph rather than by border colour).
  dim: "#A87050",
} as const;

const slate = {
  // Dim cool rules / decorative bars.
  light: "#3D4654",
  // Faint emphasis background (future inverse-chip surface).
  mid: "#252C36",
  // Deepest shadow accent — reserved for left-rail brand bar.
  dark: "#1B2530",
} as const;

const gray = {
  // Body text on dark terminals (~off-white). On light terminals the
  // user's default fg takes over via Ink's no-color path.
  100: "#EEEEEE",
  // Secondary body text — values, labels you still want crisp.
  300: "#BFBFBF",
  // Metadata / hint — second-class chrome (Footer, timestamps).
  500: "#7E848C",
  // Decorative rules, separators, section dividers.
  700: "#4A4F55",
  // Very dim — used only for "almost invisible" markers.
  900: "#252525",
} as const;

const status = {
  // Drill state words double as colour semantics so the indicator
  // language matches the operator vocabulary.
  armed: forge.fire,      // pending fire-button press
  executing: forge.iron,  // pushing real disruption
  // Success — sage / yellow-green. Lives in the warm half of the
  // colour wheel so it sits beside forge.fire (the brand orange)
  // without clashing. The previous blue-green ``#5BB371`` read as
  // cold against the all-orange chrome and pulled the eye away from
  // the chrome itself. Sage keeps the "this is a result, not chrome"
  // separation but stays in the same temperature family.
  ok: "#A4D55C",
  // Caution amber — used for "low confidence", "warn this turn".
  warn: "#E8B341",
  // Failure red — slightly muted so it doesn't strobe.
  err: "#C44545",
  // Cool info — calm, factual, for advisory metadata.
  info: "#5A8A9A",
  // Dim variants for non-loud uses (e.g. "running" tool border).
  warnDim: "#8B7530",
  errDim: "#8B3A4A",
} as const;

const border = {
  // chrome — boot cards, headers, anything that's "framework"
  chrome: forge.fire,
  // info / completion — tool group rail colour
  tool: forge.fire,
  // soft decision — Confirm Layer 1
  confirmSoft: forge.fire,
  // hard decision — Confirm Layer 2 & ResultCard share the "final"
  // colour so the user feels "my decision flowed straight into the
  // result frame".
  confirmHard: forge.iron,
  result: forge.iron,
  // diagnostic — runtime /doctor card. Now aliased to forge.fire so
  // it pairs with BootDoctorCard (also forge.fire) as the "doctor
  // family". The earlier violet (#7C3AED) was visually distinct but
  // arrived as a brand-foreign colour in an otherwise warm palette;
  // the two doctor cards never appear together in scrollback (boot
  // doctor at splash time, runtime doctor on user-triggered /doctor),
  // so the "must be unique" original rationale doesn't hold.
  diagnostic: forge.fire,
  // dim — phase stepper rule, decorative box edges, alternate panels
  dim: gray[700],
  // legacy alias kept for the few consumers still asking for "default"
  default: gray[700],
  // legacy "focus" — used to be cold blue; now alias to chrome so
  // input-prompt focus also reads warm.
  focused: forge.fire,
} as const;

const text = {
  // Body text. ``undefined`` lets the user's terminal pick its
  // default foreground (dark terminals get off-white, light terminals
  // get black). Explicit ``gray[100]`` is intentionally NOT used here
  // because that would paint pale gray on light backgrounds and read
  // as near-invisible.
  primary: undefined as string | undefined,
  // Secondary text. Was a single value before; now aliased to gray
  // 500 (metadata level). Components that need a slightly louder
  // secondary should migrate to ``gray.300`` explicitly.
  secondary: gray[500],
  // Brand accent — chrome titles, agent leader glyph, focus state.
  accent: forge.fire,
  // Links (rare in TUI but kept for parity with the old palette).
  link: "#1E88E5",
  // Inline code colour (rare; markdown bodies handle their own).
  code: "#1976D2",
} as const;

const ui = {
  // Comment-style dim — currently mirrors gray.500 to stay consistent.
  comment: gray[500],
  // Logo gradient stops. Renders left-to-right via per-char blend.
  // Updated to the forge family so the logo reads warm-deep instead
  // of the old gold→coral that didn't track the new accent.
  gradient: [forge.ember, forge.fire] as const,
  // Reference background; Ink does not paint this.
  background: slate.dark,
} as const;

export const Theme = {
  forge,
  slate,
  gray,
  status,
  border,
  text,
  ui,
} as const;

/**
 * Phrase pool for the loading indicator. Cycled every 8s while the
 * agent is in the Responding state.
 *
 * The actual phrases live in the i18n dictionaries under
 * ``thinking.phrases`` — see ``../i18n/en.ts`` / ``zh.ts``. We keep
 * a tiny English fallback here in case the phrase pool helper
 * (``utils/phrasePool.ts::getPool``) runs before i18n module init —
 * e.g. tests that import this file directly.
 */
export const ThinkingPhrases: readonly string[] = [
  "thinking",
  "decomposing",
  "considering",
  "weighing blast radius",
] as const;
