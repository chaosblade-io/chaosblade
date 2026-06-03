"""Exit-code-based config gate for first-run detection.

Mirrors the Python TUI's missing-fields check in ``tui/app.py:160-170``
verbatim — three fields, ``(val or "").strip()`` rule, same field
ordering. The TS TUI launcher calls this before deciding whether to
spawn ``blade-ai config-wizard``.

Why not let TS read ``~/.blade-ai/config.json`` directly:
    Python's ``Settings`` class layers env > config.json > built-in
    defaults. Two of the three required fields ship with non-empty
    defaults (``model_name`` and ``api_base_url``), which TS can't
    know about without duplicating constants. By spawning this
    command, TS delegates the entire resolution to the same
    ``Settings`` instance the running TUI / server will use — single
    source of truth, zero drift.

Exit codes:
    0  all required fields resolved to non-empty (after env > file > default)
    1  one or more fields missing — names listed on stderr as
       ``missing: name1,name2``
    2  reserved for "settings unavailable" (import error etc.)
"""

from __future__ import annotations

import sys


REQUIRED_FIELDS = ("llm_api_key", "model_name", "api_base_url")


def config_check_command() -> None:
    """Print missing field names and exit non-zero when any are blank."""
    try:
        from chaos_agent.config.settings import settings
    except Exception as e:
        sys.stderr.write(f"config-check: settings import failed: {e}\n")
        sys.exit(2)

    missing = [
        name
        for name in REQUIRED_FIELDS
        if not (getattr(settings, name, "") or "").strip()
    ]
    if missing:
        sys.stderr.write(f"missing: {','.join(missing)}\n")
        sys.exit(1)
    sys.exit(0)
