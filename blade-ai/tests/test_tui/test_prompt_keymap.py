"""Tests for the context-sensitive keymap footer (PR-D6).

Locks four behaviors that aren't visible in unit tests of state alone
because they live in the prompt-toolkit toolbar renderer:

1. ``calm`` mode hides the keymap row entirely — that's *the* point of
   the calm density (lower visual mass), so a regression that smuggled
   ``shift+tab`` back in defeats the mode.
2. ``working`` (default) shows the compact two-key hint.
3. ``dense`` shows the verbose hint with explanations.
4. Streaming overrides every mode and shows ``ctrl+c`` so a user can
   always discover how to interrupt mid-flight.

Also pins the new "密度:<label>" segment of the status bar that surfaces
non-default densities (so calm users can see why the experiment card is
gone).
"""

from __future__ import annotations

import pytest

from chaos_agent.tui.prompt import _render_keymap_footer, _render_status_bar
from chaos_agent.tui.state import DisplayMode, SessionState


def _join(parts: list[tuple[str, str]]) -> str:
    """Concatenate just the visible text from a FormattedText parts list."""
    return "".join(text for _, text in parts)


class TestKeymapFooterByMode:
    def test_calm_returns_empty(self):
        state = SessionState()
        state.display_mode = DisplayMode.CALM
        parts = _render_keymap_footer(state)
        # Calm explicitly drops the keymap to lower visual mass; an empty
        # list lets the status bar render without a trailing separator.
        assert parts == []

    def test_working_shows_compact_keymap(self):
        state = SessionState()
        state.display_mode = DisplayMode.WORKING
        text = _join(_render_keymap_footer(state))
        assert "shift+tab" in text
        assert "ctrl+g" in text
        # Working stays compact: no Chinese explanations.
        assert "\u5bc6\u5ea6" not in text  # 密度
        assert "/help" not in text

    def test_dense_shows_verbose_keymap(self):
        state = SessionState()
        state.display_mode = DisplayMode.DENSE
        text = _join(_render_keymap_footer(state))
        assert "shift+tab" in text
        assert "ctrl+g" in text
        assert "mode" in text
        assert "/help" in text


class TestKeymapFooterStreaming:
    def test_streaming_overrides_calm(self):
        state = SessionState()
        state.display_mode = DisplayMode.CALM
        state.is_streaming = True
        text = _join(_render_keymap_footer(state))
        # Calm normally returns nothing — but mid-stream the ctrl+c
        # interrupt has to be discoverable.
        assert "ctrl+c" in text
        assert "\u4e2d\u65ad" in text  # 中断

    def test_streaming_overrides_dense(self):
        state = SessionState()
        state.display_mode = DisplayMode.DENSE
        state.is_streaming = True
        text = _join(_render_keymap_footer(state))
        assert "ctrl+c" in text
        # Dense's normal hints are replaced; we shouldn't see "shift+tab".
        assert "shift+tab" not in text


class TestStatusBarDensityIndicator:
    """The status bar surfaces non-default densities so a user can see
    *why* the differentiating renderers are missing in calm mode."""

    def test_working_default_omits_density(self):
        state = SessionState()
        # Already DisplayMode.WORKING by default; status bar should NOT
        # add a density chip (signal would be noise for the default).
        text = _join(_render_status_bar(state))
        assert "\u5bc6\u5ea6" not in text  # 密度

    def test_calm_shows_density_chip(self):
        state = SessionState()
        state.display_mode = DisplayMode.CALM
        text = _join(_render_status_bar(state))
        assert "\u5bc6\u5ea6:\u6781\u7b80" in text  # 密度:极简

    def test_dense_shows_density_chip(self):
        state = SessionState()
        state.display_mode = DisplayMode.DENSE
        text = _join(_render_status_bar(state))
        assert "\u5bc6\u5ea6:\u5168\u5f00" in text  # 密度:全开
