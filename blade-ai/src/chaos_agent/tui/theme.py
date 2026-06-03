"""Unified design system — colors, borders, spacing constants.

Two layers, by intent:

1. ``Theme`` is the canonical, semantic-token layer. All new renderers and
   any rewrite of an old renderer SHOULD bind their styling to a
   ``Theme.state_ok`` / ``Theme.text_secondary`` etc. token. The point is
   that a theme swap (light, high-contrast, brand refresh) only has to
   touch this class.

2. ``Colors`` / ``Borders`` are the legacy shape preserved for the dozen
   renderers that already import ``Colors.SUCCESS``. Their values now
   resolve through ``Theme`` so the file is no longer two greens, two
   oranges. As renderers migrate, these aliases shrink.

Same idea for ``Icons``: a single canonical glyph per concept (✓ for
success, ✗ for fail, ⚠ for warn). The historical emoji duplicates
(✅ ❌ ⚠️ + variation selector) are gone — they doubled width on
some terminals and broke alignment.

Unicode vs ASCII (PR-B4 / §17.2): callers always import the ``Icons`` and
``BREATHING_DOTS`` names. Whether those resolve to the Unicode palette or
the ASCII fallback is decided **once at import time** by ``_ascii_mode()``,
which checks ``BLADE_AI_ASCII`` first and ``LC_ALL`` / ``LANG`` after.
That keeps every renderer drop-in — they don't have to know which palette
is active. ``IconsUnicode`` / ``IconsAscii`` stay exported so tests and
power users can target a specific palette regardless of the environment.
"""

from __future__ import annotations

import os


class Theme:
    """Semantic-token palette — *the* source of truth for styling.

    Tokens are named by **role** (what is this color for?), never by the
    color itself. ``state_ok`` over ``green``; ``text_accent`` over
    ``orange``. That way a theme switch (e.g. high-contrast) does not
    require renaming.

    Color-blindness safety (§8.3.2 + §19 底线): ``state_ok`` and
    ``state_err`` use the **Okabe-Ito** colorblind-safe palette
    (``#009E73`` bluish-green / ``#D55E00`` vermilion) rather than the
    classic Material green/red. Approximately 8% of men have some
    form of red-green color vision deficiency; the classic
    ``#66bb6a / #ef5350`` pair is the canonical "looks identical"
    case for them. Okabe-Ito is the standard fix and is what aider /
    most a11y-conscious TUIs use. Borders mirror states for visual
    consistency.
    """

    # Text hierarchy — three weights so the eye can navigate without
    # any single line "winning" against the others. ``primary`` carries
    # the actual content; ``secondary`` (rich's ``dim``) is for inline
    # captions and labels; ``muted`` is for chrome (timestamps, line
    # numbers, dim footers). A 4th level was tempting but in practice
    # rich's ``dim`` modifier on top of any base does the work.
    text_primary = "#e0e0e0"
    text_secondary = "dim"
    text_muted = "#6e6e6e"
    text_accent = "#4fc3f7"      # Brand light-blue — same hex as gradient_bright

    # ── Surfaces (panel chrome) ──
    # Three tiers of "border presence" so panels have visual hierarchy:
    # focus (brand-colored, for primary cards), surface (subtle gray,
    # for secondary cards) and divider (very subtle, for inline rules).
    # Before this split every panel used the same ``border_focus`` blue
    # which made every card "shout" equally.
    surface = "#454545"          # secondary panel border (preflight, goodbye)
    divider = "#3a3a3a"          # inline horizontal rules / soft separators

    # State (success / warning / error / in-progress).
    # OK / ERR are Okabe-Ito; warn keeps Material amber (orange-yellow
    # is unambiguous for both common color-blindness types).
    # ``state_active`` softened from the very-vivid ``#ff8c00`` to a
    # warmer ``#e08855`` — still readable as "in-progress orange" but
    # stops dominating the screen when several "active" indicators
    # paint simultaneously (e.g. spinner + phase stepper + tool panel).
    state_ok = "#009E73"         # Okabe-Ito bluish-green — colorblind-safe
    state_warn = "#ffb74d"       # Material amber
    state_err = "#D55E00"        # Okabe-Ito vermilion — colorblind-safe
    state_active = "#e08855"     # warm-orange (was #ff8c00 — too saturated)

    # Borders mirror state colors so a panel and its inside content
    # belong to the same visual family.
    border_focus = "#4fc3f7"     # Brand — primary surface (welcome, intent confirm)
    border_warn = "#ffb74d"
    border_err = "#D55E00"       # Mirror state_err (Okabe-Ito vermilion)
    border_ok = "#009E73"        # Mirror state_ok (Okabe-Ito bluish-green)

    # Roles (message attribution).
    # ``role_agent`` softened from saturated ``#7c4dff`` to ``#a78bfa``
    # — still purple-family for brand recognition but doesn't pull the
    # eye away from the message *content* on every reply.
    role_user = "#4fc3f7"
    role_agent = "#a78bfa"       # softer purple (was #7c4dff)
    role_system = "#ffb74d"
    role_thinking = "#6e6e6e"

    # Gradient stops — for animated/progressive text (thinking header,
    # phase labels, spinner frames). The eye reads left→right, so
    # stops transition from bright (accent) → mid (blend) → dim
    # (muted) creating a natural fade across a single line.
    gradient_bright = "#4fc3f7"    # accent blue — start of gradient
    gradient_mid = "#8eaccd"       # blue-grey blend — midpoint
    gradient_dim = "#6e6e6e"       # muted grey — end of gradient


class Colors:
    """Legacy alias surface — resolves through ``Theme``.

    New code should prefer ``Theme.state_ok`` etc. directly. Anything you
    see here pointing at a hardcoded hex is a migration TODO.
    """

    # Brand
    BRAND = Theme.gradient_bright
    ACCENT = Theme.role_agent

    # Status
    SUCCESS = Theme.state_ok
    WARNING = Theme.state_warn
    ERROR = Theme.state_err
    ACTIVE = Theme.state_active

    # Text hierarchy
    DIM = Theme.text_secondary
    MUTED = Theme.text_muted
    PRIMARY = Theme.text_primary

    # Role borders
    USER = Theme.role_user
    AGENT = Theme.role_agent
    SYSTEM = Theme.role_system
    THINKING = Theme.role_thinking


class Borders:
    """Border tokens — also legacy alias surface routed through ``Theme``."""

    TOOL_RUNNING = Theme.state_active
    TOOL_SUCCESS = Theme.border_ok
    TOOL_ERROR = Theme.border_err
    TOOL_TIMEOUT = Theme.border_warn

    RESULT_SUCCESS = Theme.border_ok
    RESULT_PARTIAL = Theme.border_warn
    RESULT_FAIL = Theme.border_err

    CONFIRM = Theme.border_warn


class Highlights:
    """Theme styles for inline chaos-eng term highlighting (PR-C3 / §9.6).

    Applied via a ``RegexHighlighter`` on the pure-prose markdown path so
    blade UIDs, IPs, namespaces and fault_type names get a *real* theme
    color (not just a backtick-wrapped inline-code background). Names
    here are the named-capture-group identifiers the highlighter uses,
    and the matching style names are registered on ``ChaosConsole`` via
    a Rich ``Theme`` so any console.print of ``Text("...", style="chaos_uid")``
    resolves correctly.
    """

    UID = f"bold {Theme.role_agent}"          # blade UID — agent purple
    IP = f"bold {Theme.gradient_bright}"       # IP — brand blue (gradient start)
    NAMESPACE = f"bold {Theme.state_active}"   # namespace — orange (scope)
    FAULT_TYPE = f"bold {Theme.state_err}"     # fault_type — red (failure)


def _ascii_mode() -> bool:
    """Decide whether to render with the ASCII fallback palette.

    Resolution order (first hit wins):

    1. ``BLADE_AI_ASCII`` env var. ``1/true/yes/on`` forces ASCII regardless
       of locale; ``0/false/no/off`` forces Unicode. Lets a user on a
       UTF-8 locale who's stuck with a glyph-poor font opt out, and lets
       a user in a misreporting locale opt back in.
    2. ``LC_ALL`` then ``LANG``. If neither advertises ``UTF-8`` /
       ``utf8``, fall back to ASCII. Conservative: an empty/unset locale
       (e.g. inside a minimal ``cron`` invocation) is treated as ASCII —
       better legible than a wall of replacement boxes.
    3. Default: Unicode.
    """
    forced = os.environ.get("BLADE_AI_ASCII", "").strip().lower()
    if forced in ("1", "true", "yes", "on"):
        return True
    if forced in ("0", "false", "no", "off"):
        return False
    locale = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower()
    if not locale:
        # Empty / unset locale — be conservative.
        return True
    return "utf-8" not in locale and "utf8" not in locale


class IconsUnicode:
    """Canonical Unicode glyphs (post-PR-B3 + B1 cleanup).

    Concept dedupe:
      - success → ``✓`` only (was ``✓ ✔ ✅``)
      - fail    → ``✗`` only (was ``✗ ❌``)
      - warn    → ``⚠`` only (no VS-16 selector → no double-width on iTerm)
      - role agent → ``⏺`` (was ``✦`` purple twinkle that only made sense
        next to the per-line ┃ rail; both the rail *and* the twinkle are
        gone — see ``messages.py`` for the post-rail role-glyph layout)
      - role user  → ``>`` (was ``❯`` arrow, which gets confused with shell
        prompts on some fonts)
      - thinking → ``✻`` (was ``💭`` emoji that rendered 1.5–2× wide)

    The ┃ ``VLINE`` constant was deleted in B1 — no renderer draws a
    quote-frame anymore. Restoring one would need a deliberate decision,
    not a silent re-add.

    ``MARKER`` is the streaming leader (⏺) emitted once at the head of an
    agent reply. The ``AGENT`` role icon is also ⏺ now — they're the same
    glyph for the same concept, but kept as two names because callers in
    ``intent_confirm`` etc. read ``AGENT`` as a role tag, not a marker.
    """

    # Status — one glyph each
    SUCCESS = "\u2713"           # ✓
    FAIL = "\u2717"              # ✗
    WARNING = "\u26a0"           # ⚠ (no VS-16 selector)
    ACTIVE = "\u25c9"            # ◉
    PENDING = "\u25cb"           # ○

    # Roles
    USER = ">"
    AGENT = "\u23fa"             # ⏺ (same shape as MARKER)
    THINKING = "\u273b"          # ✻
    SYSTEM = "\u2139"            # ℹ

    # Streaming leader
    MARKER = "\u23fa"            # ⏺ (U+23FA)

    # Inline tool-result branch (line 2 of the PR-C1 inline format)
    TREE_BRANCH = "\u23bf"       # ⎿


class IconsAscii:
    """ASCII fallback palette — used when ``_ascii_mode()`` is True.

    The mapping is a deliberate degradation, not a 1:1 reuse of the
    Unicode set: a checkmark becomes ``+``, a cross becomes ``x``, a warn
    triangle becomes ``!``. Anything that *could* render in ASCII as the
    Unicode form (``>`` for the user prompt) keeps that form so ASCII
    output isn't visually different where it doesn't need to be.

    Glyphs are 1-column wide on every terminal — that's the whole point.
    Width is what breaks alignment in ASCII fallback scenarios more often
    than the glyph identity itself.
    """

    # Status
    SUCCESS = "+"
    FAIL = "x"
    WARNING = "!"
    ACTIVE = "*"
    PENDING = "o"

    # Roles
    USER = ">"
    AGENT = "*"
    THINKING = "*"
    SYSTEM = "i"

    # Streaming leader
    MARKER = "*"

    # Inline tool-result branch
    TREE_BRANCH = "\\"


# Breathing-dot spinner frames — replaces the Braille ⠋⠙⠹… set so the
# spinner shape matches the static MARKER glyph (both filled-circle family)
# instead of breaking the visual rhythm at the boundary between "running"
# and "done". Used by ToolPanelRenderer and ThinkingPrinter.
_BREATHING_DOTS_UNICODE = ("\u00b7", "\u2722", "\u2733", "\u2736", "\u273b", "\u273d")
# ASCII spinner: a low-amplitude pulse that visually "breathes" with
# 7-bit-safe glyphs. The shape progression .oO0Oo. matches the unicode
# pulse (·✢✳✶✻✽) closely enough to keep tool-running rhythm consistent.
_BREATHING_DOTS_ASCII = (".", "o", "O", "0", "O", "o")


# Active palette — selected once at import time. Tests that need to assert
# behaviour against a *specific* palette should target IconsUnicode /
# IconsAscii / _BREATHING_DOTS_UNICODE / _BREATHING_DOTS_ASCII directly.
if _ascii_mode():
    Icons = IconsAscii
    BREATHING_DOTS = _BREATHING_DOTS_ASCII
else:
    Icons = IconsUnicode
    BREATHING_DOTS = _BREATHING_DOTS_UNICODE


class Spacing:
    """Spacing and layout constants."""

    # Tool panel output preview
    TOOL_PREVIEW_LINES = 5      # Max lines shown in collapsed tool output
    TOOL_OUTPUT_MAX_CHARS = 500  # Max chars before truncation in preview

    # Result card
    RESULT_PADDING = (1, 1)     # Panel padding (vertical, horizontal)

    # Confirm dialog
    CONFIRM_PADDING = (1, 2)


def role_style(role: str) -> str:
    """Return the Rich color style for a given role.

    Migrated to ``Theme.role_*`` tokens — old ``Colors.*`` lookups still
    work via the alias layer above, but new code should call this helper.
    """
    mapping = {
        "user": Theme.role_user,
        "agent": Theme.role_agent,
        "system": Theme.role_system,
        "thinking": Theme.role_thinking,
        "error": Theme.state_err,
    }
    return mapping.get(role, Theme.text_secondary)
