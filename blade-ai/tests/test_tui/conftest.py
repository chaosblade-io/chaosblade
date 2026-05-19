"""Shared fixtures for the TUI test suite.

Two things live here, both intentionally narrow in scope:

1. ``captured_console`` — a ``ChaosConsole`` that writes to ``StringIO``
   with ``force_terminal=False`` and ``width=80``. The width pin is what
   makes layout-sensitive tests deterministic across terminal sizes, and
   ``force_terminal=False`` strips ANSI so snapshot files diff cleanly.
   Multiple test modules already build this by hand — promoting it to a
   fixture removes that duplication for snapshot tests without forcing
   a refactor of the existing tests.

2. ``snapshot`` — the §17.3 snapshot-test infrastructure (PR-B5). It
   compares a string against a golden file under ``snapshots/<name>.txt``.
   First run creates the file; subsequent runs assert exact equality.
   Set ``UPDATE_SNAPSHOTS=1`` in the env to regenerate after an
   intentional layout change. The diff message points the user at the
   exact command to accept the new output, so updates are one keystroke
   away rather than a rebuild step.

Why snapshot tests instead of substring asserts: layout regressions
(reordered metadata rows, dropped blank lines, padding shifts) don't
fail substring asserts because every individual word is still present.
Snapshots lock the *whole shape* of the output. The cost is that any
intentional layout change forces an update step — that's the right
default for a TUI where the visual is part of the product.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from rich.console import Console

from chaos_agent.tui.console import ChaosConsole

_SNAPSHOTS_DIR = Path(__file__).parent / "snapshots"


@pytest.fixture
def captured_console() -> ChaosConsole:
    """A ``ChaosConsole`` writing to in-memory ``StringIO`` at width=80."""
    cc = ChaosConsole()
    cc._console = Console(file=io.StringIO(), force_terminal=False, width=80)
    return cc


@pytest.fixture
def require_unicode_locale() -> None:
    """Skip a test if the active palette isn't ``IconsUnicode``.

    Snapshots are recorded against the Unicode glyph set. Running the
    test under POSIX / a non-UTF-8 locale would render the ASCII palette
    instead and fail comparisons for unrelated reasons. We skip rather
    than fail so a minimal-locale dev box doesn't see false negatives.
    """
    from chaos_agent.tui.theme import Icons, IconsUnicode

    if Icons is not IconsUnicode:
        pytest.skip("snapshot tests require Unicode locale (LANG=*.UTF-8)")


class _Snapshotter:
    """Compare-or-write helper bound to a single test invocation."""

    def __init__(self, request: pytest.FixtureRequest) -> None:
        self._request = request

    def assert_match(self, name: str, content: str) -> None:
        path = _SNAPSHOTS_DIR / f"{name}.txt"
        update = os.environ.get("UPDATE_SNAPSHOTS", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if update or not path.exists():
            _SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return
        expected = path.read_text(encoding="utf-8")
        if content == expected:
            return
        # Build a developer-friendly diff message that names the file and
        # the env-var override. Without this, accepting an intentional
        # layout change is a guessing game.
        marker = "─" * 60
        msg = (
            f"Snapshot mismatch for {name!r}\n"
            f"{marker}\n"
            f"expected ({path}):\n{expected}\n"
            f"{marker}\n"
            f"actual:\n{content}\n"
            f"{marker}\n"
            f"To accept this as the new golden:\n"
            f"  UPDATE_SNAPSHOTS=1 uv run pytest "
            f"{self._request.node.nodeid}"
        )
        raise AssertionError(msg)


@pytest.fixture
def snapshot(request: pytest.FixtureRequest) -> _Snapshotter:
    return _Snapshotter(request)
