"""PR-B4 / §17.2 — ASCII fallback + LANG detect.

We're testing two layers separately:

1. ``_ascii_mode()`` is the pure decision function — env in, bool out.
   Easy to drive deterministically by mutating ``os.environ`` per case.
2. ``IconsUnicode`` / ``IconsAscii`` are the two static palettes. They
   stay exported regardless of which one is active, so tests can assert
   their values without flipping the environment back and forth.

The integration — "the right palette is bound to ``Icons`` at import
time" — is covered by re-importing the module under a controlled env in
``test_active_palette_follows_env``. We use ``importlib.reload`` rather
than two test files because the module isn't import-side-effect-heavy
(it only reads env once), so reloading is cheap and explicit.
"""

import importlib

import pytest


@pytest.fixture
def clean_env(monkeypatch):
    """Strip env vars that ``_ascii_mode`` looks at, then yield the
    monkeypatch handle so tests can ``setenv`` only what they need.

    After the test, we reload ``chaos_agent.tui.theme`` under the real
    process env (which monkeypatch restores during its own teardown via a
    nested fixture handoff). This keeps the global ``Icons`` and
    ``BREATHING_DOTS`` symbols pointing at whichever palette the *real*
    environment selects — without this restore, a synthetic POSIX run
    would silently leave ``Icons = IconsAscii`` and break every
    downstream snapshot test guarded by ``require_unicode_locale``.
    """
    import importlib
    import os

    import chaos_agent.tui.theme as theme

    saved = {var: os.environ.get(var) for var in ("BLADE_AI_ASCII", "LC_ALL", "LANG")}
    for var in saved:
        monkeypatch.delenv(var, raising=False)
    try:
        yield monkeypatch
    finally:
        # Manually restore env *before* reloading, since monkeypatch's
        # own teardown runs after this fixture's teardown.
        for var, value in saved.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value
        importlib.reload(theme)


class TestAsciiModeDecision:
    def test_explicit_force_ascii_via_env_override(self, clean_env):
        from chaos_agent.tui.theme import _ascii_mode

        for value in ("1", "true", "yes", "on", "TRUE", "Yes"):
            clean_env.setenv("BLADE_AI_ASCII", value)
            assert _ascii_mode() is True, f"value={value!r} should force ASCII"

    def test_explicit_force_unicode_via_env_override(self, clean_env):
        # User on a misreporting locale (LANG empty) wants Unicode anyway.
        from chaos_agent.tui.theme import _ascii_mode

        for value in ("0", "false", "no", "off", "FALSE", "No"):
            clean_env.setenv("BLADE_AI_ASCII", value)
            # Even with empty locale (which would otherwise return True),
            # the explicit override wins.
            assert _ascii_mode() is False, f"value={value!r} should force Unicode"

    def test_lang_utf8_returns_unicode(self, clean_env):
        from chaos_agent.tui.theme import _ascii_mode

        for locale in ("en_US.UTF-8", "zh_CN.UTF-8", "C.UTF-8", "en_US.utf8"):
            clean_env.setenv("LANG", locale)
            assert _ascii_mode() is False, f"locale={locale!r} should be Unicode"

    def test_lc_all_takes_precedence_over_lang(self, clean_env):
        from chaos_agent.tui.theme import _ascii_mode

        # POSIX locale advertises no UTF-8 encoding.
        clean_env.setenv("LC_ALL", "POSIX")
        clean_env.setenv("LANG", "en_US.UTF-8")
        assert _ascii_mode() is True

    def test_non_utf8_lang_returns_ascii(self, clean_env):
        from chaos_agent.tui.theme import _ascii_mode

        for locale in ("POSIX", "C", "en_US"):
            clean_env.setenv("LANG", locale)
            assert _ascii_mode() is True, f"locale={locale!r} should be ASCII"

    def test_empty_locale_returns_ascii(self, clean_env):
        # No LANG, no LC_ALL — most likely a daemonized cron / system
        # process. Better safe than legible — assume ASCII.
        from chaos_agent.tui.theme import _ascii_mode

        assert _ascii_mode() is True


class TestIconsAsciiPalette:
    """Lock the ASCII palette values so they don't drift silently. If
    someone changes ``IconsAscii.FAIL`` from ``x`` to ``X`` the alignment
    in any panel row using these icons quietly shifts — this test makes
    that change a deliberate decision."""

    def test_status_icons_are_one_column_ascii(self):
        from chaos_agent.tui.theme import IconsAscii

        assert IconsAscii.SUCCESS == "+"
        assert IconsAscii.FAIL == "x"
        assert IconsAscii.WARNING == "!"
        assert IconsAscii.ACTIVE == "*"
        assert IconsAscii.PENDING == "o"

    def test_role_icons_are_one_column_ascii(self):
        from chaos_agent.tui.theme import IconsAscii

        # USER stays `>` — it renders fine in ASCII and matches the
        # post-B1 rail-free layout of messages.py.
        assert IconsAscii.USER == ">"
        assert IconsAscii.AGENT == "*"
        assert IconsAscii.THINKING == "*"
        assert IconsAscii.SYSTEM == "i"

    def test_streaming_marker_is_ascii(self):
        from chaos_agent.tui.theme import IconsAscii

        assert IconsAscii.MARKER == "*"

    def test_every_ascii_icon_is_seven_bit_safe(self):
        """The whole point of the palette: every glyph must encode in
        plain ASCII. A future contributor adding a "subtle" U+2022 bullet
        defeats the purpose for users on a glyph-poor terminal."""
        from chaos_agent.tui.theme import IconsAscii

        for attr in dir(IconsAscii):
            if attr.startswith("_"):
                continue
            value = getattr(IconsAscii, attr)
            if not isinstance(value, str):
                continue
            # str.isascii() is the precise check for "this is 7-bit safe".
            assert value.isascii(), f"IconsAscii.{attr}={value!r} must be ASCII"


class TestActivePaletteSelection:
    """The integration test: what does ``Icons`` actually point to under
    a given env? We reload the theme module so the import-time branch
    re-runs against the fixture's env."""

    def test_unicode_locale_binds_unicode_palette(self, clean_env):
        clean_env.setenv("LANG", "en_US.UTF-8")
        import chaos_agent.tui.theme as theme

        importlib.reload(theme)
        try:
            assert theme.Icons is theme.IconsUnicode
            assert theme.BREATHING_DOTS == theme._BREATHING_DOTS_UNICODE
        finally:
            # Restore the module to whatever the real env says, so
            # subsequent tests in this process see a consistent state.
            importlib.reload(theme)

    def test_ascii_override_binds_ascii_palette(self, clean_env):
        clean_env.setenv("BLADE_AI_ASCII", "1")
        clean_env.setenv("LANG", "en_US.UTF-8")  # explicit override wins
        import chaos_agent.tui.theme as theme

        importlib.reload(theme)
        try:
            assert theme.Icons is theme.IconsAscii
            assert theme.BREATHING_DOTS == theme._BREATHING_DOTS_ASCII
            # Sanity: nothing in the active palette is non-ASCII.
            for attr in ("SUCCESS", "FAIL", "WARNING", "MARKER"):
                assert getattr(theme.Icons, attr).isascii()
            for frame in theme.BREATHING_DOTS:
                assert frame.isascii()
        finally:
            importlib.reload(theme)

    def test_posix_locale_binds_ascii_palette(self, clean_env):
        clean_env.setenv("LANG", "POSIX")
        import chaos_agent.tui.theme as theme

        importlib.reload(theme)
        try:
            assert theme.Icons is theme.IconsAscii
        finally:
            importlib.reload(theme)
