"""§8.3.2 / §19 底线 — Okabe-Ito colorblind-safe palette.

The doc lists colorblind-safe color choices as one of the four
non-negotiable a11y baselines. Roughly 8% of men have some form of
red-green color vision deficiency; the classic Material green/red
pair (``#66bb6a / #ef5350``) is the canonical "looks identical to
deuteranopes" case. Okabe-Ito is the standard accessibility-conscious
swap: ``#009E73`` (bluish-green) and ``#D55E00`` (vermilion).

Tests pin both **values** (so a future hex drift fails loudly) and
**non-equality** with the old Material pair (so a regression that
silently restored the original colors gets caught even if the
``state_ok`` / ``state_err`` constants stay populated).

We also pin that downstream renderers route through ``Theme.state_ok``
/ ``Theme.state_err`` rather than holding their own hex copies — that
was the Phase 1 PR-B3 contract, and a colorblind-safe theme switch
silently being half-applied because some renderer kept the old hex
is exactly the kind of regression this test class is here to catch.
"""

from __future__ import annotations


# Canonical Okabe-Ito palette values pinned by ``§8.3.2``. These are
# *the* a11y baseline; if the values ever change we want a test
# review, not a silent paint difference.
OKABE_ITO_BLUISH_GREEN = "#009E73"
OKABE_ITO_VERMILION = "#D55E00"

# The classic Material pair we replaced. A regression that restored
# them must fail.
MATERIAL_GREEN_400 = "#66bb6a"
MATERIAL_RED_400 = "#ef5350"


class TestThemeOkabeItoValues:
    def test_state_ok_is_okabe_ito_bluish_green(self):
        from chaos_agent.tui.theme import Theme

        assert Theme.state_ok == OKABE_ITO_BLUISH_GREEN

    def test_state_err_is_okabe_ito_vermilion(self):
        from chaos_agent.tui.theme import Theme

        assert Theme.state_err == OKABE_ITO_VERMILION

    def test_borders_mirror_state_colors(self):
        # The border tokens exist so a renderer can outline a panel
        # in the same family as the state inside it. They MUST track
        # the Okabe-Ito values too — a panel with a Material green
        # border around an Okabe-Ito green body would cause an
        # ugly two-tone artifact for sighted users on top of the
        # colorblind issue.
        from chaos_agent.tui.theme import Theme

        assert Theme.border_ok == OKABE_ITO_BLUISH_GREEN
        assert Theme.border_err == OKABE_ITO_VERMILION

    def test_state_colors_are_not_classic_material(self):
        # Belt-and-braces: even if a future contributor re-hardcoded
        # the classic Material values into a different constant name,
        # this catches them before the a11y regression ships.
        from chaos_agent.tui.theme import Theme

        assert Theme.state_ok != MATERIAL_GREEN_400
        assert Theme.state_err != MATERIAL_RED_400


class TestColorsAndBordersAliasOkabeIto:
    """The legacy ``Colors`` / ``Borders`` shims must resolve through
    ``Theme`` rather than holding their own hex. PR-B3 promised this
    surface as a transitional alias; if anything broke that, the
    Okabe-Ito values wouldn't propagate to renderers still using the
    legacy import path.
    """

    def test_colors_success_routes_through_theme(self):
        from chaos_agent.tui.theme import Colors, Theme

        assert Colors.SUCCESS == Theme.state_ok == OKABE_ITO_BLUISH_GREEN

    def test_colors_error_routes_through_theme(self):
        from chaos_agent.tui.theme import Colors, Theme

        assert Colors.ERROR == Theme.state_err == OKABE_ITO_VERMILION

    def test_borders_tool_success_routes_through_theme(self):
        from chaos_agent.tui.theme import Borders, Theme

        assert Borders.TOOL_SUCCESS == Theme.border_ok == OKABE_ITO_BLUISH_GREEN

    def test_borders_tool_error_routes_through_theme(self):
        from chaos_agent.tui.theme import Borders, Theme

        assert Borders.TOOL_ERROR == Theme.border_err == OKABE_ITO_VERMILION

    def test_borders_result_success_routes_through_theme(self):
        from chaos_agent.tui.theme import Borders, Theme

        assert Borders.RESULT_SUCCESS == Theme.border_ok

    def test_borders_result_fail_routes_through_theme(self):
        from chaos_agent.tui.theme import Borders, Theme

        assert Borders.RESULT_FAIL == Theme.border_err


class TestRendererOkabeItoBindings:
    """Pin that the renderers that previously hardcoded ``#66bb6a`` /
    ``#ef5350`` now resolve through ``Theme``. A grep would catch a
    re-introduced hex literal too, but a static test gives a friendly
    failure message instead of relying on a periodic spot-check.
    """

    def test_preflight_constants_track_theme(self):
        from chaos_agent.tui.renderers import preflight
        from chaos_agent.tui.theme import Theme

        assert preflight._OK == Theme.state_ok
        assert preflight._FAIL == Theme.state_err

    def test_goodbye_constants_track_theme(self):
        from chaos_agent.tui.renderers import goodbye
        from chaos_agent.tui.theme import Theme

        assert goodbye._OK == Theme.state_ok
        assert goodbye._FAIL == Theme.state_err

    def test_onboarding_constants_track_theme(self):
        from chaos_agent.tui.renderers import onboarding
        from chaos_agent.tui.theme import Theme

        assert onboarding._OK == Theme.state_ok
        assert onboarding._FAIL == Theme.state_err

    def test_no_legacy_hex_in_tui_source(self):
        # Final guard: scan production TUI source for the old hex
        # literals. Comments are allowed (they explain the change),
        # but any non-comment occurrence indicates a renderer that
        # bypassed Theme. Strings inside docstrings/comments are
        # detected by checking whether the line is comment-only or
        # contains a Python ``#`` before the hex.
        import pathlib

        root = pathlib.Path(__file__).parent.parent.parent / "src" / "chaos_agent" / "tui"
        offenders: list[tuple[str, int, str]] = []
        for path in root.rglob("*.py"):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"'):
                    continue
                # Allow the literal in a comment after code (anywhere
                # ``#`` precedes the hex on the line).
                code_part = line.split("#", 1)[0]
                # Allow the hex only if it appears in a "was Material"
                # explanatory comment — those are caught by the split
                # above. Otherwise flag.
                if MATERIAL_GREEN_400 in code_part or MATERIAL_RED_400 in code_part:
                    offenders.append((str(path), lineno, line))
        assert offenders == [], (
            "Legacy Material hex literals found in TUI source — "
            "every renderer should route through Theme.state_ok / "
            "Theme.state_err for colorblind-safe consistency:\n  "
            + "\n  ".join(f"{p}:{n}: {ln}" for p, n, ln in offenders)
        )
