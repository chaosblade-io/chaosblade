/**
 * Blade-AI TUI palette — derived from the ChaosBlade brand family
 * (warm coral / amber). Originally adapted from Qwen Code's
 * ``qwen-dark`` theme, but the lavender accent has been replaced
 * with ``Forged Amber`` so the TUI no longer carries Qwen's brand
 * visually and instead reads as a chaos-engineering tool (warm,
 * precise, sits naturally next to the existing gold→coral gradient).
 *
 * Design rules (locked in for M1; M2+ follow the same):
 *
 *   text.accent (amber)    — the *only* content-emphasis color. Used
 *                            for thinking subject, tool name highlights,
 *                            agent identifier glyph, card borders and
 *                            section headers. Anything that "should pop"
 *                            lands here.
 *   border.focused (blue)  — used only for container focus (dialogs,
 *                            inputs in focus state). Never for content.
 *   text.secondary / muted — everything ambient: timestamps, hints,
 *                            metadata, divider rules.
 *
 * Background colors are listed for reference but the TUI itself does
 * NOT paint a background — Ink renders foreground text only and lets
 * the user's terminal own the canvas. The values are kept so dialog /
 * diff highlights have a reference point.
 */

export const Theme = {
  text: {
    // Body text intentionally has no explicit color so the terminal owns
    // it: dark terminals get their default light fg (~off-white), light
    // terminals get black. The previous fixed ``#bfbdb6`` rendered as
    // pale-gray text on white terminals (near-invisible) and as a gray
    // block whenever it appeared inside an ``inverse`` chip.
    primary: undefined as string | undefined,
    secondary: "#646A71", // soft gray — readable on both light and dark
    // Forged Amber — sits between ``ui.gradient[0]`` (#FFD700 gold) and
    // ``ui.gradient[1]`` (#da7959 coral), so accents and the gradient
    // belong to the same warm family. Distinct from Claude Code's
    // brighter orange and from Qwen Code's lavender. Deepened from the
    // earlier ``#F2A65A`` (which fell to ~2:1 on white terminals — the
    // welcome / boot / pending-tasks borders washed out and the
    // section headers struggled to read). The current value preserves
    // the same hue but lowers lightness ~15 stops to land at ~3.4:1 on
    // white while still reading clearly amber on dark.
    accent: "#D88A2E",
    link: "#1E88E5", // deeper brand blue — links readable on both bg
    code: "#1976D2", // saturated blue — inline code
  },
  border: {
    default: "#3D4149", // dim gray — default panel border (intentional)
    // Confirm-dialog focused border. Deeper than the earlier ``#39BAE6``
    // which sat at ~2.4:1 on white. ``#1976D2`` (Material blue 700) hits
    // ~5:1 on white and ~5:1 on dark — balanced for both.
    focused: "#1976D2",
    // Tool-call group border (success / done state). Deeper turquoise
    // (~4.4:1 on white vs ~2.3:1 the earlier ``#4ECDC4`` had) so
    // completed tool blocks pop on light terminals too. Hue family
    // preserved — distinct from accent amber, focused blue,
    // diagnostic violet, and result coral.
    tool: "#0E9594",
    // ResultCard border. Coral — the second hue of the logo
    // gradient (``ui.gradient[1]``). Brand-coherent, warm like
    // amber but with a redder cast so the eye distinguishes
    // ResultCard ("operation outcome") from WelcomeCard /
    // BootCardFrame ("brand chrome / boot context") at a glance.
    // Deepened from ``#da7959`` (~3.3:1 on white) to ``#C45838``
    // (~4.4:1) — same hue family, more saturated.
    result: "#C45838",
    // Runtime ``/doctor`` diagnostic card. Deep violet — sits in a
    // hue family no other border occupies (welcome amber, confirm
    // brand-blue, tool turquoise, result coral, default dim-gray),
    // so the diagnostic panel is recognisable at a glance even when
    // the user has multiple cards stacked in scrollback. Deepened
    // from ``#B392F0`` (~2.2:1 on white, near-invisible) to
    // ``#7C3AED`` (~4.5:1 on white, still vibrant on dark).
    diagnostic: "#7C3AED",
  },
  status: {
    // Mid grass-green. Deeper than the previous ``#AAD94C`` (which read
    // as washed-out lime on white terminals — the ✓ glyph and the
    // status-coloured value text both fell below ~2.5:1 contrast on
    // light backgrounds). ``#22A55C`` lifts the contrast to ~3:1+ on
    // white while staying clearly green (not yellow-green) and clearly
    // mid-tone (not the dark forest green that would lose punch on
    // dark terminals).
    ok: "#22A55C",
    warn: "#FFD700", // gold — warning
    err: "#F26D78", // coral red — error (gentle)
    warnDim: "#8B7530", // muted variant
    errDim: "#8B3A4A",
  },
  ui: {
    comment: "#646A71",
    /** Logo gradient stops. Renders left-to-right via per-char blend. */
    gradient: ["#FFD700", "#da7959"] as const,
    /** Reference background; Ink does not paint this — kept for diff highlights. */
    background: "#0b0e14",
  },
} as const;

/**
 * Phrase pool for the loading indicator. Cycled every 15s while the
 * agent is in the Responding state.
 *
 * The actual phrases live in the i18n dictionaries under
 * ``thinking.phrases`` — see ``../i18n/en.ts`` / ``zh.ts``. We keep
 * a tiny English fallback here in case ``usePhraseCycler`` runs before
 * i18n module init (e.g. tests that import this file directly).
 */
export const ThinkingPhrases: readonly string[] = [
  "thinking",
  "decomposing",
  "considering",
  "weighing blast radius",
] as const;
