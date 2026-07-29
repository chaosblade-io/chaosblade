"""The TS placeholder window must mirror the Python fallback.

The Footer needs a window size before the first server figure lands, so the TS
bundle carries ``DEFAULT_CONTEXT_MAX_TOKENS``. It is only a placeholder — the real
value arrives twice over, from ``GET /preflight``'s ``context_max_tokens`` at boot
and from every ``context_size`` event afterwards — but it is what the user reads
if preflight is unavailable.

The placeholder is meant to be the SAME number as Python's global fallback
``settings.context_max_tokens``, i.e. "the window we assume when we cannot resolve
the model". Nothing enforces that: the two live in different files in different
languages with no compile-time link, so changing the Python default would leave
the TS bundle quietly showing the old one and no test would fail.

Deliberately NOT asserted against ``resolve_context_budget()``. That returns the
CURRENT model's real window (131,072 for qwen3-max) and is a different quantity —
conflating the two is how this file came to be written in the first place.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from chaos_agent.config.settings import Settings

_TS_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "tui"
    / "src"
    / "utils"
    / "formatContextSize.ts"
)
_DECLARATION = re.compile(
    r"export\s+const\s+DEFAULT_CONTEXT_MAX_TOKENS\s*=\s*([\d_]+)\s*;"
)


def _ts_placeholder() -> int:
    if not _TS_SOURCE.exists():
        pytest.skip(f"TS bundle source not present: {_TS_SOURCE}")
    match = _DECLARATION.search(_TS_SOURCE.read_text(encoding="utf-8"))
    assert match, (
        f"DEFAULT_CONTEXT_MAX_TOKENS declaration not found in {_TS_SOURCE.name} — "
        f"it was renamed or reshaped, so this guard is no longer watching anything"
    )
    return int(match.group(1).replace("_", ""))


def test_ts_placeholder_matches_the_python_fallback():
    assert _ts_placeholder() == Settings().context_max_tokens, (
        "the TS Footer placeholder drifted from Python's context_max_tokens "
        "fallback; a user with preflight unavailable would be shown a window "
        "size this backend no longer assumes"
    )


def test_placeholder_is_not_confused_with_the_resolved_window():
    """Guards the concept, not just the number.

    If someone "fixes" the placeholder to the configured model's real window, it
    stops being a model-agnostic fallback and becomes wrong for every other model.
    """
    settings = Settings()
    resolved, _ = settings.resolve_context_budget(settings.model_name)
    if resolved == settings.context_max_tokens:
        pytest.skip("current model resolves to the fallback; nothing to separate")
    assert _ts_placeholder() != resolved, (
        "the placeholder was set to the current model's resolved window; it must "
        "stay the model-agnostic fallback, since the real value already arrives "
        "via preflight and context_size events"
    )
